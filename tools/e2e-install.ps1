param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [int]$Port = 8766,
    [string]$Token = "",
    [int]$HealthTimeoutSeconds = 180,
    # Pass the expected /api/tasks stats.total (37 for the install seeded with the production DB,
    # 0 for a truly fresh one). Omit to skip the assert.
    [int]$ExpectTasks = -1,
    # -Overwrite = the documented upgrade flow: unzip the new package on top of an existing
    # install dir. data\ and .env must survive untouched, and the read-only trap (C-03) shows up
    # here first, because a previous install's files may be read-only.
    [switch]$Overwrite,
    # -VerifyTrayQuit = after everything else, drive "tray -> quit" from the command line
    # (QQ-Notice-Hub.exe --quit) and assert the tray, the service child and the port are all gone.
    # Leaves the app stopped, so pass it only on the last run of a session.
    [switch]$VerifyTrayQuit
)
# ASCII only on purpose: PowerShell 5.1 reads BOM-less .ps1 as ANSI(936) and Chinese breaks the parser.
$ErrorActionPreference = 'Stop'
$script:fail = 0
$script:pass = 0
# The install's own .env is the source of truth for the token (the launcher generates one for a
# truly fresh install). -Token is only a fallback for a scratch run that seeded its own .env.
$script:tok = $Token

function Check([string]$Name, [bool]$Ok, [string]$Detail = "") {
    if ($Ok) { $script:pass++; Write-Host ("PASS  {0}{1}" -f $Name, $(if ($Detail) { "  | $Detail" } else { "" })) }
    else     { $script:fail++; Write-Host ("FAIL  {0}{1}" -f $Name, $(if ($Detail) { "  | $Detail" } else { "" })) }
}

function Api([string]$Path, [string]$Method = "GET", [string]$Body = "", [string]$Accept = "") {
    $url = "http://127.0.0.1:$Port$Path"
    $headers = @{}
    if ($script:tok) { $headers['X-Token'] = $script:tok }
    if ($Accept) { $headers['Accept'] = $Accept }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        if ($Method -eq "POST") {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
            $resp = Invoke-WebRequest $url -UseBasicParsing -Method POST -Headers $headers -Body $bytes -ContentType 'application/json; charset=utf-8' -TimeoutSec 60
        } else {
            $resp = Invoke-WebRequest $url -UseBasicParsing -Headers $headers -TimeoutSec 60
        }
        $ms = $sw.ElapsedMilliseconds
        $text = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
        return [pscustomobject]@{ Status = [int]$resp.StatusCode; Text = $text; Ms = $ms; Error = "" }
    } catch {
        $ms = $sw.ElapsedMilliseconds
        $status = 0
        $text = ""
        if ($_.Exception.Response) {
            try { $status = [int]$_.Exception.Response.StatusCode } catch { $status = 0 }
            try {
                $rs = $_.Exception.Response.GetResponseStream()
                $sr = New-Object System.IO.StreamReader($rs, [System.Text.Encoding]::UTF8)
                $text = $sr.ReadToEnd()
            } catch { $text = "" }
        }
        return [pscustomobject]@{ Status = $status; Text = $text; Ms = $ms; Error = $_.Exception.Message }
    }
}

Write-Host "=== 1. zip package shape ==="
$zipInfo = Get-Item -LiteralPath $Zip
Write-Host ("zip: {0} | {1} B | {2}" -f $zipInfo.FullName, $zipInfo.Length, $zipInfo.LastWriteTime)
Add-Type -AssemblyName 'System.IO.Compression.FileSystem'
$archive = [System.IO.Compression.ZipFile]::OpenRead($Zip)
$entries = @($archive.Entries)
$names = @($entries | ForEach-Object { $_.FullName })
$archive.Dispose()
Check "zip has entries" ($names.Count -gt 0) ("entries={0}" -f $names.Count)
Check "C-01 top level is the install root" ($names -contains 'QQ-Notice-Hub.exe') ("QQ-Notice-Hub.exe at top level = {0}" -f ($names -contains 'QQ-Notice-Hub.exe'))
Check "C-04 entries use / separators" (-not ($names | Where-Object { $_.Contains('\') })) ("backslash entries = {0}" -f @($names | Where-Object { $_.Contains('\') }).Count)
Check "no data/ or logs/ or .env inside zip" (-not ($names | Where-Object { $_ -like 'data/*' -or $_ -like 'logs/*' -or $_ -eq '.env' }))
Check "napcat helper scripts present" (($names -contains 'fix-napcat.ps1') -and (@($names | Where-Object { $_ -like '*.cmd' }).Count -ge 2)) ("cmd entries = {0}" -f @($names | Where-Object { $_ -like '*.cmd' }).Count)

Write-Host ""
Write-Host "=== 2. extract as a user would ==="
# Stop anything still running out of this install dir, or the launcher's port guard / single
# instance mutex would make the run a false negative (and leave the previous run's exe locked).
$stale = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like (Join-Path $InstallDir '*') } |
    Select-Object -ExpandProperty ProcessId)
foreach ($p in $stale) { try { Stop-Process -Id $p -Force -ErrorAction Stop; Write-Host ("stopped stale pid {0}" -f $p) } catch { } }
if ($stale.Count -gt 0) { Start-Sleep -Seconds 3 }
$dataBefore = $null
if ($Overwrite) {
    if (-not (Test-Path -LiteralPath $InstallDir)) { throw "InstallDir does not exist, so there is nothing to overwrite: $InstallDir" }
    $db = Join-Path $InstallDir 'data\digest.sqlite3'
    if (Test-Path -LiteralPath $db) { $dataBefore = (Get-Item -LiteralPath $db).Length }
    Write-Host ("overwrite mode: extracting over the existing dir (data\digest.sqlite3 before = {0} B)" -f $dataBefore)
} else {
    if (Test-Path -LiteralPath $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
Add-Type -AssemblyName 'System.IO.Compression.FileSystem'
# Model what the product does before replacing files (updater.py:406-415, and the same trap the
# shipped script handles): a read-only file in the target makes ExtractToFile throw Access denied.
# ...\assets\donate-wechat.jpg is read-only in the real production install, so this is not theory:
# a raw overwrite-extract fails with a localized access-denied error (probed 2026-10-11).
Get-ChildItem -LiteralPath $InstallDir -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.IsReadOnly } |
    ForEach-Object { $_.IsReadOnly = $false }
# Extract by hand: the .NET Framework ExtractToDirectory refuses to replace existing files (and
# this is exactly the upgrade path a real user takes, so replacing them has to work).
$zipArchive = [System.IO.Compression.ZipFile]::OpenRead($Zip)
try {
    foreach ($entry in $zipArchive.Entries) {
        if ([string]::IsNullOrEmpty($entry.Name)) { continue }
        $target = Join-Path $InstallDir $entry.FullName.Replace('/', '\')
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
    }
} finally { $zipArchive.Dispose() }
if ($Overwrite) {
    $dataAfter = $null
    if (Test-Path -LiteralPath (Join-Path $InstallDir 'data\digest.sqlite3')) { $dataAfter = (Get-Item -LiteralPath (Join-Path $InstallDir 'data\digest.sqlite3')).Length }
    Check "C-05 overwrite keeps existing data\digest.sqlite3" ([bool]($dataBefore) -and ($dataAfter -eq $dataBefore)) ("before={0} after={1}" -f $dataBefore, $dataAfter)
    Check "C-06 overwrite keeps existing .env" (Test-Path -LiteralPath (Join-Path $InstallDir '.env'))
}
$exe = Join-Path $InstallDir 'QQ-Notice-Hub.exe'
Check "QQ-Notice-Hub.exe extracted" (Test-Path -LiteralPath $exe)
Check "service exe extracted" (Test-Path -LiteralPath (Join-Path $InstallDir 'qq-live-digest.exe'))
$ro = @(Get-ChildItem -LiteralPath $InstallDir -Recurse -File | Where-Object { $_.IsReadOnly })
Check "C-03 extracted files are not read-only" ($ro.Count -eq 0) ("read-only files = {0}" -f $ro.Count)

# A fresh install writes its own .env on 8766 (launcher._ensure_env_file hardcodes that port).
# For a scratch run on another port we must seed .env first, or the launcher would collide with
# whatever already holds 8766. The real user flow (port 8766) leaves this branch untouched.
if ($Port -ne 8766 -and -not (Test-Path -LiteralPath (Join-Path $InstallDir '.env'))) {
    $seedToken = if ($Token) { $Token } else { "e2e-scratch-token" }
    $seed = @(
        "QQ_DIGEST_WEB=1"
        "QQ_DIGEST_WEB_HOST=0.0.0.0"
        "QQ_DIGEST_WEB_PORT=$Port"
        "QQ_DIGEST_WEB_TOKEN=$seedToken"
        "QQ_DIGEST_DATA_DIR=" + (Join-Path $InstallDir 'data')
        "QQ_DIGEST_LOG_DIR=" + (Join-Path $InstallDir 'logs')
    ) -join "`n"
    Set-Content -LiteralPath (Join-Path $InstallDir '.env') -Value $seed -Encoding UTF8
    Write-Host ("seeded .env for scratch port {0} (harness step, not part of the user flow)" -f $Port)
}

Write-Host ""
Write-Host "=== 3. first launch (double-click equivalent) ==="
$before = @(Get-Process -Name 'QQ-Notice-Hub', 'qq-live-digest' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$proc = Start-Process -FilePath $exe -WorkingDirectory $InstallDir -PassThru
Write-Host ("launched pid {0}" -f $proc.Id)
$deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
$health = $null
while ((Get-Date) -lt $deadline) {
    $health = Api '/api/health'
    if ($health.Status -eq 200) { break }
    Start-Sleep -Seconds 2
}
Check "GET /api/health returns 200" ($health -and $health.Status -eq 200) ("status={0} ms={1} err={2}" -f $(if ($health) { $health.Status } else { 'n/a' }), $(if ($health) { $health.Ms } else { 0 }), $(if ($health) { $health.Error } else { '' }))
Start-Sleep -Seconds 3
$live = @(Get-Process -Name 'qq-live-digest' -ErrorAction SilentlyContinue)
Check "service child process is running" ($live.Count -gt 0) ("qq-live-digest pids = {0}" -f ($live.Id -join ','))
if (-not $Token) {
    $envPathEarly = Join-Path $InstallDir '.env'
    if (Test-Path -LiteralPath $envPathEarly) {
        $line = @(Get-Content -LiteralPath $envPathEarly -Encoding UTF8 | Where-Object { $_ -like 'QQ_DIGEST_WEB_TOKEN=*' })[0]
        if ($line) {
            $script:tok = $line.Split('=', 2)[1].Trim()
            Write-Host ("using token from the install's own .env (len={0})" -f $script:tok.Length)
        }
    }
}

Write-Host ""
Write-Host "=== 4. console + data ==="
$root = Api '/' 'GET' '' 'text/html'
Check "GET / returns 200 (or 401 + html hint when token mismatched)" ($root.Status -eq 200 -or ($root.Status -eq 401 -and $root.Text -like '*QQ-Notice-Hub.exe*')) ("status={0}" -f $root.Status)
$tasks = Api '/api/tasks'
Check "GET /api/tasks authorized" ($tasks.Status -eq 200) ("status={0} body_len={1}" -f $tasks.Status, $tasks.Text.Length)
if ($tasks.Status -eq 200) {
    $tj = $tasks.Text | ConvertFrom-Json
    # /api/tasks returns a view-model (today/week/later/done/pinned + stats.total), not a flat list.
    $n = if ($tj.stats) { [int]$tj.stats.total } else { -1 }
    if ($ExpectTasks -ge 0) {
        Check ("tasks payload carries the expected data (stats.total = {0})" -f $ExpectTasks) ($n -eq $ExpectTasks) ("stats.total={0}" -f $n)
    } else {
        Write-Host ("    stats.total={0} (no -ExpectTasks given, count not asserted)" -f $n)
    }
}
$ann = Api '/api/announcements'
Check "GET /api/announcements returns 200" ($ann.Status -eq 200) ("status={0} body={1}" -f $ann.Status, $ann.Text.Substring(0, [Math]::Min(120, $ann.Text.Length)))
$notices = Api '/api/notices'
Check "GET /api/notices returns 200" ($notices.Status -eq 200) ("status={0}" -f $notices.Status)

Write-Host ""
Write-Host "=== 5. NapCat detection latency (must be fast; 44s scan was the bug) ==="
$nap = Api '/api/napcat/status'
Check "GET /api/napcat/status returns 200" ($nap.Status -eq 200) ("status={0} elapsed_ms={1}" -f $nap.Status, $nap.Ms)
Check "napcat status responds within 5s" ($nap.Ms -lt 5000) ("elapsed_ms={0}" -f $nap.Ms)
if ($nap.Status -eq 200) {
    Write-Host ("    body: {0}" -f $nap.Text.Substring(0, [Math]::Min(300, $nap.Text.Length)))
    try { $napJson = $nap.Text | ConvertFrom-Json; Check "napcat status exposes napcat_running" ($null -ne $napJson.napcat_running) ("napcat_running={0}" -f $napJson.napcat_running) } catch { Check "napcat status JSON parse" $false $_.Exception.Message }
}

Write-Host ""
Write-Host "=== 5b. NapCat doctor must name the broken link (silence is the bug) ==="
# The malignant bug was a self-check that reported nothing: the user scanned the QR, login
# succeeded, and absolutely nothing happened anywhere on the page. The old health panel only
# looked at /api/napcat/status, which itself needs the OneBot API -- so the real root cause
# (the engine config had no httpServers at all) was invisible to it forever. Invariant:
# whenever the doctor is not ok it MUST also say which link broke and what to do about it.
$doctor = Api '/api/napcat/doctor'
Check "GET /api/napcat/doctor returns 200" ($doctor.Status -eq 200) ("status={0}" -f $doctor.Status)
if ($doctor.Status -eq 200) {
    $doc = $doctor.Text | ConvertFrom-Json
    Check "doctor returns steps" (@($doc.steps).Count -gt 0) ("steps={0}" -f @($doc.steps).Count)
    $silent = (-not $doc.ok) -and ([string]::IsNullOrWhiteSpace([string]$doc.broken) -or [string]::IsNullOrWhiteSpace([string]$doc.hint))
    Check "doctor failure always carries broken + hint" (-not $silent) ("ok={0}" -f $doc.ok)
    if (-not $doc.ok) {
        Write-Host ("    broken: {0}" -f $doc.broken)
        Write-Host ("    hint:   {0}" -f $doc.hint)
    }
    # When the engine IS present the config must really carry an enabled httpServer for the
    # configured port -- that is exactly the bit that silently went missing and killed login.
    $onebot = Api '/api/napcat/status'
    if ($onebot.Status -eq 200) {
        $st = $onebot.Text | ConvertFrom-Json
        if ($st.napcat_installed) {
            Write-Host ("    engine present, doctor sees: {0}" -f $doc.broken)
        }
    }
}

Write-Host ""
Write-Host "=== 5c. P0 integration config and subscriptions ==="
# NapCatWinBootMain is the NapCat launcher process name; process ownership is machine-specific.
$napcatBootMain = @(Get-Process -Name "NapCatWinBootMain" -ErrorAction SilentlyContinue)
Write-Host ("NapCatWinBootMain processes observed: {0}" -f $napcatBootMain.Count)
$envPath = Join-Path $InstallDir '.env'
$envLinesBefore = @()
if (Test-Path -LiteralPath $envPath) { $envLinesBefore = @(Get-Content -LiteralPath $envPath -Encoding UTF8) }
$sub = Api '/api/subscriptions' 'POST' '{"groups":[],"aliases":{}}'
Check "fresh .env subscription save returns 200" ($sub.Status -eq 200) ("status={0} body={1}" -f $sub.Status, $sub.Text)
if (Test-Path -LiteralPath $envPath) {
    $envLinesAfter = @(Get-Content -LiteralPath $envPath -Encoding UTF8)
    $changed = @($envLinesBefore | Where-Object { $envLinesAfter -notcontains $_ })
    Check "subscription preserves existing .env lines" ($changed.Count -eq 0) ("removed_lines={0}" -f $changed.Count)
}
$configFiles = @(Get-ChildItem -LiteralPath (Join-Path $InstallDir 'config') -Filter 'onebot11*.json' -File -ErrorAction SilentlyContinue)
if ($configFiles.Count -eq 0) {
    Write-Host "SKIP P0-2a httpServers: NapCat config not present"
} else {
    $apiLine = @($envLinesAfter | Where-Object { $_ -like 'QQ_DIGEST_NAPCAT_API_URL=*' })[0]
    $expectedPort = 3001
    if ($apiLine) { try { $expectedPort = ([uri]$apiLine.Split('=', 2)[1]).Port } catch { } }
    $configured = $false
    foreach ($cf in $configFiles) {
        try {
            $cj = Get-Content -LiteralPath $cf.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($hs in @($cj.network.httpServers)) { if ($hs.enable -and [int]$hs.port -eq $expectedPort) { $configured = $true } }
        } catch { Write-Host ("config parse failed: {0}" -f $cf.FullName) }
    }
    Check "enabled httpServers matches QQ_DIGEST_NAPCAT_API_URL" $configured ("files={0} port={1}" -f $configFiles.Count, $expectedPort)
}
$created = Api '/api/tasks/create' 'POST' '{"summary":"gate acceptance task","deadline":""}'
Check "create task returns 200" ($created.Status -eq 200) ("status={0}" -f $created.Status)
$taskId = ''
if ($created.Status -eq 200) { try { $taskId = [string](($created.Text | ConvertFrom-Json).task_id) } catch { } }
if ($taskId) {
    $updated = Api ("/api/tasks/{0}" -f $taskId) 'POST' '{"action":"done"}'
    Check "update task returns 200" ($updated.Status -eq 200) ("status={0}" -f $updated.Status)
    $deleted = Api ("/api/tasks/{0}" -f $taskId) 'POST' '{"action":"dismiss"}'
    Check "delete task returns 200" ($deleted.Status -eq 200) ("status={0}" -f $deleted.Status)
}
$ics = Api '/api/ics'
Check "GET /api/ics returns 200" ($ics.Status -eq 200) ("status={0}" -f $ics.Status)

Write-Host ""
Write-Host "=== 6. files created under the install dir ==="
foreach ($rel in @('.env', 'data', 'logs')) {
    $p = Join-Path $InstallDir $rel
    Check ("created {0}" -f $rel) (Test-Path -LiteralPath $p) $p
}
$envPath = Join-Path $InstallDir '.env'
if (Test-Path -LiteralPath $envPath) {
    Write-Host "---- .env ----"
    Get-Content -LiteralPath $envPath -Encoding UTF8 | ForEach-Object { Write-Host ("    {0}" -f $_) }
}
$runtime = @(Get-Process -Name 'QQ-Notice-Hub', 'qq-live-digest' -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id })
Write-Host ("running new pids: {0}" -f (($runtime | Select-Object -ExpandProperty Id) -join ','))

Write-Host ""
if ($VerifyTrayQuit) {
    Write-Host "=== 7. tray quit (automated, no human at the icon) ==="
    $trayPid = $proc.Id
    $launcherExe = Join-Path $InstallDir 'QQ-Notice-Hub.exe'
    $quit = Start-Process -FilePath $launcherExe -ArgumentList '--quit' -PassThru -Wait -WindowStyle Hidden
    Check "launcher --quit exits 0" ($quit.ExitCode -eq 0) ("exit={0}" -f $quit.ExitCode)
    Start-Sleep -Seconds 3
    Check "tray process is gone" (-not (Get-Process -Id $trayPid -ErrorAction SilentlyContinue)) ("pid={0}" -f $trayPid)
    $leftover = @(Get-Process -Name 'QQ-Notice-Hub', 'qq-live-digest' -ErrorAction SilentlyContinue)
    Check "no QQ-Notice-Hub.exe / qq-live-digest.exe left" ($leftover.Count -eq 0) ("pids={0}" -f (($leftover | Select-Object -ExpandProperty Id) -join ','))
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    Check "port is released" ($listeners.Count -eq 0) ("listeners={0}" -f (($listeners | Select-Object -ExpandProperty OwningProcess) -join ','))
    $quitLog = Join-Path $InstallDir 'logs\launcher.log'
    if (Test-Path -LiteralPath $quitLog) {
        Write-Host "---- launcher.log tail ----"
        Get-Content -LiteralPath $quitLog -Tail 6 -Encoding UTF8 | ForEach-Object { Write-Host ("    {0}" -f $_) }
    }
}

Write-Host ""
Write-Host ("=== RESULT: {0} passed, {1} failed ===" -f $script:pass, $script:fail)
if (-not $VerifyTrayQuit) {
    Write-Host ("NOTE: tray-quit not verified in this run. Re-run with -VerifyTrayQuit to check it automatically (pid {0})." -f $proc.Id)
}
if ($script:fail -gt 0) { exit 1 } else { exit 0 }

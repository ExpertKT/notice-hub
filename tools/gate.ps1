param(
    [string]$Python = 'F:\qq-notice-hub\.venv\Scripts\python.exe',
    [string]$InstallZip = 'F:\qq-notice-hub\packaging\QQ-Notice-Hub.zip',
    [string]$InstallDir = '',
    [int]$Port = 0,
    [switch]$SkipL3,
    [switch]$SkipManifestCheck,
    [switch]$OnlyManifestCheck
)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$ownedInstall = [string]::IsNullOrWhiteSpace($InstallDir)
if ($ownedInstall) { $InstallDir = Join-Path $env:TEMP ('nh-gate-' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }
$failed = $false
function Run-Step([string]$Name, [scriptblock]$Action) {
    Write-Host ("=== {0} ===" -f $Name)
    # A scriptblock that never launches an external process leaves $LASTEXITCODE from
    # whatever the host session ran last. Seed it so a silent step cannot read as PASS.
    $global:LASTEXITCODE = 0
    & $Action
    $code = $LASTEXITCODE
    if ($code -ne 0) { $script:failed = $true; Write-Host ("FAIL {0} exit={1}" -f $Name, $code) } else { Write-Host ("PASS {0}" -f $Name) }
}

# L2/L3 verify the packaging artifact. An artifact older than the source makes the
# gate check a historical snapshot (false green), so assert freshness before them.
# This only asserts; it never rebuilds. Rebuilding is a human decision.
# NOTE: keep this file pure ASCII (tests/test_acceptance_gate.py reads it as ascii).
function Test-ArtifactFreshness {
    $sources = @(Get-ChildItem -Path (Join-Path $root 'qq_live_digest') -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @('.py', '.html', '.js', '.css', '.txt') })
    $sources += @(Get-ChildItem -Path $root -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -eq '.py' })
    # Mirror packaging\build.ps1 inputs; packaging\build and packaging\dist are artifacts.
    $packagingRoot = Join-Path $root 'packaging'
    $sources += @(Get-ChildItem -Path $packagingRoot -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @('.py', '.spec', '.ps1', '.txt', '.cmd') })
    $sources += @(Get-ChildItem -Path (Join-Path $root 'docs') -File -Filter '*.md' -ErrorAction SilentlyContinue)
    $sources += @(Get-ChildItem -Path (Join-Path $root 'assets') -File -ErrorAction SilentlyContinue)
    foreach ($name in @('run.ps1', 'install-task.ps1', 'uninstall-task.ps1', 'status.ps1', 'LICENSE', 'LICENSE-UPSTREAM', 'NOTICE')) {
        $path = Join-Path $root $name
        if (Test-Path -LiteralPath $path -PathType Leaf) { $sources += Get-Item -LiteralPath $path }
    }
    foreach ($name in @('watchdog-task.ps1', 'cleanup-install.ps1')) {
        $path = Join-Path (Join-Path $root 'tools') $name
        if (Test-Path -LiteralPath $path -PathType Leaf) { $sources += Get-Item -LiteralPath $path }
    }
    # ponytail: ceiling - keep this list aligned with packaging\build.ps1 copy inputs.
    $newestSource = $sources | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newestSource) { Write-Host 'FAIL freshness: no source files found'; return $false }
    $artifacts = @(
        (Join-Path $root 'packaging\QQ-Notice-Hub.zip'),
        (Join-Path $root 'packaging\dist\qq-live-digest\qq-live-digest.exe')
    )
    # ponytail: ceiling - freshness compares mtime only, so refreshing an artifact's mtime
    # without rebuilding (touch) still passes. Upgrade path: embed a source-tree sha256 in
    # the zip at build time and compare that instead of LastWriteTime.
    $stale = $false
    foreach ($artifact in $artifacts) {
        if (Test-Path -LiteralPath $artifact) {
            $info = Get-Item -LiteralPath $artifact
            $hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash
            Write-Host ("ARTIFACT {0} bytes={1} sha256={2} mtime={3:yyyy-MM-dd HH:mm:ss}" -f $info.Name, $info.Length, $hash, $info.LastWriteTime)
            if ($info.LastWriteTime -le $newestSource.LastWriteTime) { $stale = $true }
        } else {
            Write-Host ("FAIL freshness: artifact missing: {0}" -f $artifact)
            $stale = $true
        }
    }
    Write-Host ("NEWEST SOURCE {0} mtime={1:yyyy-MM-dd HH:mm:ss}" -f $newestSource.Name, $newestSource.LastWriteTime)
    if ($stale) {
        Write-Host ("FAIL freshness: artifact older than source ({0}); rebuild packaging before gating" -f $newestSource.Name)
        return $false
    }
    return $true
}

# Release guard: clients read the update manifest from raw.githubusercontent.com and the
# Gitee mirror at release/update-manifest.json. beta.11 shipped with a URL that pointed at
# the repo root, so every older client silently got no Gitee manifest and fell back to the
# GitHub API. Fetch both URLs before publishing; anything that is not HTTP 200 + JSON
# version 1 aborts the gate. -OnlyManifestCheck runs just this step (release checklist).
# Use -SkipManifestCheck for offline runs.
# NOTE: keep this file pure ASCII (tests/test_acceptance_gate.py reads it as ascii).
function Test-ManifestUrls {
    $urls = @(
        'https://raw.githubusercontent.com/ExpertKT/notice-hub/main/release/update-manifest.json',
        'https://gitee.com/ExpertKT/notice-hub-mirror/raw/master/release/update-manifest.json'
    )
    foreach ($url in $urls) {
        $code = 0
        $content = ''
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -Method Get -TimeoutSec 20
            $code = [int]$response.StatusCode
            $content = [string]$response.Content
        } catch {
            $http = $_.Exception.Response
            if ($http -and $http.StatusCode) { $code = [int]$http.StatusCode }
        }
        $ok = $false
        if ($code -eq 200) {
            try { $ok = ([int](($content | ConvertFrom-Json).version) -eq 1) } catch { $ok = $false }
        }
        Write-Host ("MANIFEST {0} -> HTTP {1} json_ok={2}" -f $url, $code, $ok)
        if (-not $ok) { return $false }
    }
    return $true
}
if ($OnlyManifestCheck) {
    if (Test-ManifestUrls) { exit 0 }
    Write-Host 'FAIL manifest urls: a published manifest URL is not reachable JSON v1'
    exit 1
}
if ($SkipManifestCheck) {
    Write-Host 'SKIP manifest url check (-SkipManifestCheck)'
} else {
    Run-Step 'manifest urls' {
        $global:LASTEXITCODE = 0
        if (-not (Test-ManifestUrls)) { $global:LASTEXITCODE = 1 }
    }
}

Run-Step 'L0 syntax' {
    & $Python (Join-Path $root 'tools\syntaxcheck.py')
}
Run-Step 'L1 unittest' {
    Push-Location $root
    try { & $Python -m unittest discover -s tests -t . } finally { Pop-Location }
}
$fresh = Test-ArtifactFreshness
if (-not $fresh) {
    $failed = $true
    Write-Host 'SKIP L2/L3 (stale artifact)'
    Write-Host 'SKIP L3 (stale artifact)'
} else {
    $exe = Join-Path $root 'packaging\dist\qq-live-digest\qq-live-digest.exe'
    $exeMissing = -not (Test-Path -LiteralPath $exe)
    if ($exeMissing) { Write-Host ("FAIL package missing: {0}" -f $exe) }
    Run-Step 'L2 package PYZ' {
        & $Python (Join-Path $root 'tools\packcheck.py') $exe
    }
    if ($exeMissing) {
        # Do not exit here: that would skip the L3 decision and the temp install cleanup.
        $script:failed = $true
    }
    if (-not $SkipL3) {
        if ($exeMissing) { Write-Host 'SKIP L3 (L2 package missing)' }
        elseif ($failed) { Write-Host 'SKIP L3 (an earlier step failed)' }
        else {
            if ($Port -le 0) {
                do {
                    $probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
                    $probe.Start(); $Port = $probe.LocalEndpoint.Port; $probe.Stop()
                } while ($Port -in @(8765, 8766))
                Write-Host ("L3 selected random free port: {0}" -f $Port)
            }
            $existing = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
            if ($existing.Count -ne 0) { Write-Host ("FAIL L3 selected port already in use: {0}" -f $Port); $script:failed = $true }
            elseif (-not (Test-Path -LiteralPath $InstallZip)) { Write-Host ("FAIL L3 package missing: {0}" -f $InstallZip); $script:failed = $true }
            else {
                Run-Step 'L3 cold install' {
                    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'tools\e2e-install.ps1') -Zip $InstallZip -InstallDir $InstallDir -Port $Port -VerifyTrayQuit
                }
            }
        }
    } else { Write-Host 'SKIP L3 cold install (explicit -SkipL3)' }
}
if ($ownedInstall -and (Test-Path -LiteralPath $InstallDir)) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host ("CLEAN temp install: {0}" -f $InstallDir)
}
if ($failed) { exit 1 }
exit 0
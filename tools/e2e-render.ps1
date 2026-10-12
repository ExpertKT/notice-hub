# e2e-render: prove the served console page actually works in a browser.
# Catches the 2026-10-11 bug class: a JS syntax error in webapp.PAGE_HTML kills the
# whole dashboard script, so the page shows a static "loading" placeholder forever
# while every API still answers 200. Status-code checks alone cannot see this.
#
# Empty-data safe: the <h1 id="headline"> is written by render() only after /api/tasks
# answers, and overview() has its own valid headline for a fresh install with no tasks.
# This check therefore asserts the placeholder was REPLACED by one of the headlines
# overview() can return, which still fails on a dead script / failed request / missing
# dashboard, instead of demanding the "today N tasks" shape that empty data never shows.
# -ExpectTasks N additionally pins the task count the headline must report.
#
# ASCII only on purpose (Windows PowerShell 5.1 misreads BOM-less UTF-8 source), so the
# expected Chinese headline fragments below are built from their codepoints.
param(
    [string]$Url = 'http://127.0.0.1:8766',
    [string]$Token,
    [string]$InstallDir,
    [string]$Edge,
    [string]$Node = 'node',
    [int]$RenderBudgetMs = 20000,
    # Number of tasks due today the headline must report. Omit (-1) to accept any headline
    # overview() can legitimately return; 0 requires the empty-state headline.
    [int]$ExpectTasks = -1
)

$ErrorActionPreference = 'Continue'
$fails = 0
function Report($ok, $label, $detail) {
    if ($ok) { Write-Host ("PASS  " + $label + "  | " + $detail) }
    else { Write-Host ("FAIL  " + $label + "  | " + $detail); $script:fails++ }
}

# Chinese headline fragments as codepoints (see the ASCII-only note above).
function Codes([int[]]$cp) { -join @($cp | ForEach-Object { [char]$_ }) }
$hToday = Codes @(0x4ECA, 0x5929)                                            # "today"
$hPiece = Codes @(0x4EF6)                                                    # counter word in "today N <piece>"
$hNotice = Codes @(0x901A, 0x77E5, 0x5F85, 0x786E, 0x8BA4)                  # "N notices pending"
$hNoDue = Codes @(0x4ECA, 0x5929, 0x6CA1, 0x6709, 0x5230, 0x671F, 0x7684, 0x4E8B)  # "nothing due today"
$hAllDone = Codes @(0x6240, 0x6709, 0x5F85, 0x529E, 0x90FD, 0x6E05, 0x7A7A, 0x4E86) # "all tasks cleared"
$reToday = '^' + [regex]::Escape($hToday) + ' \d+ ' + [regex]::Escape($hPiece)
$reNotice = '^\d+ ' + [regex]::Escape($hNotice) + '$'

if (-not $Token -and $InstallDir) {
    $envFile = Join-Path $InstallDir '.env'
    if (Test-Path $envFile) {
        $line = Select-String -Path $envFile -Pattern '^QQ_DIGEST_WEB_TOKEN=(.*)$' | Select-Object -First 1
        if ($line) { $Token = $line.Matches[0].Groups[1].Value.Trim() }
    }
}
if (-not $Token) { Write-Host 'FAIL  no token given and none found in .env'; exit 2 }

$pageUrl = $Url.TrimEnd('/') + '/?token=' + [System.Uri]::EscapeDataString($Token)
$work = Join-Path $env:TEMP ('nh-uicheck-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work -Force | Out-Null

# ---- 1. static check: every inline <script> block must parse ----
try {
    $resp = Invoke-WebRequest $pageUrl -UseBasicParsing -Headers @{ 'X-Token' = $Token; 'Accept' = 'text/html' } -TimeoutSec 30 -ErrorAction Stop
    $html = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
    Report $true 'served console page' ("status=" + $resp.StatusCode + " bytes=" + $html.Length)
} catch {
    Report $false 'served console page' $_.Exception.Message
    exit 1
}

$matches = [regex]::Matches($html, '(?s)<script[^>]*>(.*?)</script>')
$blocks = 0
$parseFails = 0
foreach ($m in $matches) {
    $blocks++
    $js = Join-Path $work ("block-" + $blocks + ".js")
    [System.IO.File]::WriteAllText($js, $m.Groups[1].Value, (New-Object System.Text.UTF8Encoding($false)))
    $out = & $Node --check $js 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        $parseFails++
        Report $false ("script block " + $blocks + " parses") (($out | Select-Object -First 3) -join ' / ')
    }
}
Report ($parseFails -eq 0) 'all inline scripts parse' ("blocks=" + $blocks + " failures=" + $parseFails + " via " + $Node)

# ---- 2. real render: headless Edge must not report an uncaught JS error ----
if (-not $Edge) {
    $Edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    if (-not (Test-Path $Edge)) { $Edge = 'C:\Program Files\Microsoft\Edge\Application\msedge.exe' }
}
$dom = Join-Path $work 'dom.html'
$con = Join-Path $work 'console.log'
if (Test-Path $Edge) {
    $profile = Join-Path $work 'profile'
    cmd /c "`"$Edge`" --headless=new --disable-gpu --no-first-run --no-default-browser-check --user-data-dir=`"$profile`" --window-size=1600,1000 --virtual-time-budget=$RenderBudgetMs --enable-logging=stderr --log-level=0 --dump-dom `"$pageUrl`" > `"$dom`" 2> `"$con`""
    if (Test-Path $dom) {
        $domText = [System.IO.File]::ReadAllText($dom, [System.Text.Encoding]::UTF8)
        $uncaught = @()
        if (Test-Path $con) {
            $uncaught = @(Select-String -Path $con -Pattern 'Uncaught (SyntaxError|TypeError|ReferenceError)' -Encoding UTF8)
        }
        $detail = 'hits=' + $uncaught.Count
        if ($uncaught.Count -gt 0) { $detail = $detail + '  ' + $uncaught[0].Line }
        Report ($uncaught.Count -eq 0) 'no uncaught JS error in browser console' $detail
        # The served page ships with a static placeholder in this element. Anything still
        # showing it means the dashboard script did not run, threw, or the request failed.
        $hm = [regex]::Match($domText, '<h1[^>]*\bid\s*=\s*"?headline"?[^>]*>(.*?)</h1>', [System.Text.RegularExpressions.RegexOptions]::Singleline)
        $headline = if ($hm.Success) { $hm.Groups[1].Value.Trim() } else { '' }
        $rendered = (
            ($headline -match $reToday) -or
            ($headline -match $reNotice) -or
            ($headline -eq $hNoDue) -or
            ($headline -eq $hAllDone)
        )
        Report $rendered 'dashboard headline rendered from /api/tasks' ('headline="' + $headline + '" dom bytes=' + $domText.Length)
        if ($ExpectTasks -ge 0) {
            if ($ExpectTasks -eq 0) {
                $countOk = ($headline -eq $hNoDue) -or ($headline -eq $hAllDone)
            } else {
                $countOk = $headline -match ('^' + [regex]::Escape($hToday) + ' ' + $ExpectTasks + ' ' + [regex]::Escape($hPiece))
            }
            Report $countOk ('headline reports ' + $ExpectTasks + ' task(s) due today') ('headline="' + $headline + '"')
        }
        Write-Host ("      workdir: " + $work)
    } else {
        Report $false 'headless render produced a DOM' 'no dom.html'
    }
} else {
    Report $false 'headless Edge available' 'msedge.exe not found'
}

if ($fails -eq 0) { Write-Host '=== UI RESULT: all checks passed ==='; exit 0 }
Write-Host ("=== UI RESULT: " + $fails + " failed ===")
exit 1

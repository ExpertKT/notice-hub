param(
    [string]$Python = 'F:\qq-notice-hub\.venv\Scripts\python.exe',
    [string]$InstallZip = 'F:\qq-notice-hub\packaging\QQ-Notice-Hub.zip',
    [string]$InstallDir = '',
    [int]$Port = 8766,
    [switch]$SkipL3
)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$ownedInstall = [string]::IsNullOrWhiteSpace($InstallDir)
if ($ownedInstall) { $InstallDir = Join-Path $env:TEMP ('nh-gate-' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }
$failed = $false
function Run-Step([string]$Name, [scriptblock]$Action) {
    Write-Host ("=== {0} ===" -f $Name)
    & $Action
    $code = $LASTEXITCODE
    if ($code -ne 0) { $script:failed = $true; Write-Host ("FAIL {0} exit={1}" -f $Name, $code) } else { Write-Host ("PASS {0}" -f $Name) }
}
Run-Step 'L0 syntax' {
    & $Python (Join-Path $root 'tools\syntaxcheck.py')
}
Run-Step 'L1 unittest' {
    Push-Location $root
    try { & $Python -m unittest discover -s tests -t . } finally { Pop-Location }
}
Run-Step 'L2 package PYZ' {
    $exe = Join-Path $root 'packaging\dist\qq-live-digest\qq-live-digest.exe'
    if (-not (Test-Path -LiteralPath $exe)) { Write-Host "FAIL package missing: $exe"; exit 1 }
    & $Python (Join-Path $root 'tools\packcheck.py') $exe
}
if (-not $SkipL3) {
    if (-not (Test-Path -LiteralPath $InstallZip)) { Write-Host ("FAIL L3 package missing: {0}" -f $InstallZip); $failed = $true }
    else {
        Run-Step 'L3 cold install' {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'tools\e2e-install.ps1') -Zip $InstallZip -InstallDir $InstallDir -Port $Port -VerifyTrayQuit
        }
    }
} else { Write-Host 'SKIP L3 cold install (explicit -SkipL3)' }
if ($ownedInstall -and (Test-Path -LiteralPath $InstallDir)) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host ("CLEAN temp install: {0}" -f $InstallDir)
}
if ($failed) { exit 1 }
exit 0

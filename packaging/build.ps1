$ErrorActionPreference = 'Stop'
# PyInstaller writes INFO progress to stderr; PowerShell 7.4+ under Stop turns native stderr into a
# terminating error, so the script would abort. Disable that (the variable is absent on older ones).
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) { $PSNativeCommandUseErrorActionPreference = $false }
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$dist = Join-Path $root 'packaging\dist'
$work = Join-Path $root 'packaging\build'
$stale = Join-Path $dist 'qq-live-digest.exe'
if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Force }
& $python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work (Join-Path $PSScriptRoot 'qq_live_digest.spec')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work (Join-Path $PSScriptRoot 'launcher.spec')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$appRoot = Join-Path $dist 'qq-live-digest'
if (-not (Test-Path -LiteralPath $appRoot -PathType Container)) { throw "Missing service package: $appRoot" }
Copy-Item (Join-Path $dist 'QQ-Notice-Hub.exe') (Join-Path $appRoot 'QQ-Notice-Hub.exe') -Force
$tutorial = Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.txt' | Select-Object -First 1
if ($null -eq $tutorial) { throw 'Missing tutorial txt' }
Copy-Item -LiteralPath $tutorial.FullName -Destination (Join-Path $appRoot $tutorial.Name) -Force
$shortcutScript = Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.cmd' | Select-Object -First 1
if ($null -eq $shortcutScript) { throw 'Missing shortcut cmd' }
Copy-Item -LiteralPath $shortcutScript.FullName -Destination (Join-Path $appRoot $shortcutScript.Name) -Force
$docs = Join-Path $appRoot 'docs'
New-Item -ItemType Directory -Path $docs -Force | Out-Null
Copy-Item (Join-Path $root 'docs\*.md') $docs -Force
# Tray/icon assets: the launcher reads <exe dir>\assets\icon-256.png and only falls back to drawing.
$assetsRoot = Join-Path $appRoot 'assets'
New-Item -ItemType Directory -Path (Join-Path $appRoot 'assets') -Force | Out-Null
Copy-Item (Join-Path $root 'assets\*') (Join-Path $appRoot 'assets') -Force
# Autostart/cleanup scripts, so an unzipped install can register scheduled tasks (docs section 12).
$rootScripts = @('run.ps1', 'install-task.ps1', 'uninstall-task.ps1', 'status.ps1')
foreach ($item in $rootScripts) {
    $source = Join-Path $root $item
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $appRoot $item) -Force }
}
$toolsSource = Join-Path $root 'tools'
$toolsTarget = Join-Path $appRoot 'tools'
foreach ($item in @('watchdog-task.ps1', 'cleanup-install.ps1')) {
    $source = Join-Path $toolsSource $item
    if (Test-Path -LiteralPath $source) {
        New-Item -ItemType Directory -Path $toolsTarget -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination (Join-Path $toolsTarget $item) -Force
    }
}
$zip = Join-Path $PSScriptRoot 'QQ-Notice-Hub.zip'
Compress-Archive -Path $appRoot -DestinationPath $zip -Force

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$dist = Join-Path $root 'packaging\dist'
$work = Join-Path $root 'packaging\build'
$stale = Join-Path $dist 'qq-live-digest.exe'
if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Force }
& $python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work (Join-Path $PSScriptRoot 'qq_live_digest.spec')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work (Join-Path $PSScriptRoot 'launcher.spec')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Copy-Item (Join-Path $dist 'QQ-Notice-Hub.exe') (Join-Path $dist 'qq-live-digest\QQ-Notice-Hub.exe') -Force

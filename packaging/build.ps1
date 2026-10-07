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
$app = Join-Path $dist 'qq-live-digest'
Copy-Item (Join-Path $dist 'QQ-Notice-Hub.exe') (Join-Path $app 'QQ-Notice-Hub.exe') -Force
$tutorial = Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.txt' | Select-Object -First 1
if ($null -eq $tutorial) { throw 'Missing tutorial txt' }
Copy-Item -LiteralPath $tutorial.FullName -Destination (Join-Path $app $tutorial.Name) -Force
$shortcutScript = Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.cmd' | Select-Object -First 1
if ($null -eq $shortcutScript) { throw 'Missing shortcut cmd' }
Copy-Item -LiteralPath $shortcutScript.FullName -Destination (Join-Path $app $shortcutScript.Name) -Force
$docs = Join-Path $app 'docs'
New-Item -ItemType Directory -Path $docs -Force | Out-Null
Copy-Item (Join-Path $root 'docs\*.md') $docs -Force
$zip = Join-Path $PSScriptRoot 'QQ-Notice-Hub.zip'
Compress-Archive -Path $app -DestinationPath $zip -Force

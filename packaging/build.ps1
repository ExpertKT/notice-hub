$ErrorActionPreference = 'Stop'
# PyInstaller 把 INFO 进度写 stderr；PowerShell 7.4+ 在 Stop 语义下会把原生命令的 stderr 当成致命错误，
# 结果脚本在第 8 行就中断。先关掉这个新默认值（变量在旧版不存在，故用 Test-Path 保护）。
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) { $PSNativeCommandUseErrorActionPreference = $false }
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
# 托盘/图标资源：launcher 运行时读 <exe 同级>\assets\icon-256.png，读不到才回退内联绘制。
$assetsRoot = Join-Path $appRoot 'assets'
New-Item -ItemType Directory -Path (Join-Path $appRoot 'assets') -Force | Out-Null
Copy-Item (Join-Path $root 'assets\*') (Join-Path $appRoot 'assets') -Force
$zip = Join-Path $PSScriptRoot 'QQ-Notice-Hub.zip'
Compress-Archive -Path $appRoot -DestinationPath $zip -Force

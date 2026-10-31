$ErrorActionPreference = 'Stop'
# PyInstaller writes INFO progress to stderr, and under ErrorActionPreference=Stop (Windows PowerShell
# 5.1 included) that aborts the script. Run it through cmd with 2>&1 so the stderr text arrives as
# ordinary stdout and only the exit code decides success.
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$dist = Join-Path $root 'packaging\dist'
# Clear prior output including read-only assets; otherwise PyInstaller cleanup can fail with WinError 5.
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
$work = Join-Path $root 'packaging\build'
$stale = Join-Path $dist 'qq-live-digest.exe'  # 旧包名的残骸，换名后仍要清一次
if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Force }
& cmd /c "`"$python`" -m PyInstaller --noconfirm --clean --distpath `"$dist`" --workpath `"$work`" `"$(Join-Path $PSScriptRoot 'noticehub.spec')`" 2>&1"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& cmd /c "`"$python`" -m PyInstaller --noconfirm --clean --distpath `"$dist`" --workpath `"$work`" `"$(Join-Path $PSScriptRoot 'launcher.spec')`" 2>&1"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$appRoot = Join-Path $dist 'noticehub'
if (-not (Test-Path -LiteralPath $appRoot -PathType Container)) { throw "Missing service package: $appRoot" }
$launcherRoot = Join-Path $dist 'QQ-Notice-Hub'
if (-not (Test-Path -LiteralPath $launcherRoot -PathType Container)) { throw "Missing launcher package: $launcherRoot" }
$launcherExe = Join-Path $launcherRoot 'QQ-Notice-Hub.exe'
$launcherInternal = Join-Path $launcherRoot '_launcher_internal'
if (-not (Test-Path -LiteralPath $launcherExe -PathType Leaf)) { throw "Missing launcher executable: $launcherExe" }
if (-not (Test-Path -LiteralPath $launcherInternal -PathType Container)) { throw "Missing launcher runtime: $launcherInternal" }
Copy-Item -LiteralPath $launcherExe -Destination (Join-Path $appRoot 'QQ-Notice-Hub.exe') -Force
Copy-Item -LiteralPath $launcherInternal -Destination (Join-Path $appRoot '_launcher_internal') -Recurse -Force
$tutorial = Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.txt' | Select-Object -First 1
if ($null -eq $tutorial) { throw 'Missing tutorial txt' }
Copy-Item -LiteralPath $tutorial.FullName -Destination (Join-Path $appRoot $tutorial.Name) -Force
# 每个 .cmd 都要进包：除了「创建桌面快捷方式.cmd」还有「如果你已经装好NapCat点这个.cmd」。
# 旧写法 `Get-ChildItem ... | Select-Object -First 1` 只会挑中一个，另一个被静默丢掉。
$shortcutScripts = @(Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.cmd')
if ($shortcutScripts.Count -eq 0) { throw 'Missing shortcut cmd' }
foreach ($script in $shortcutScripts) {
    Copy-Item -LiteralPath $script.FullName -Destination (Join-Path $appRoot $script.Name) -Force
}
# 「如果你已经装好NapCat点这个.cmd」的实际逻辑在这个 .ps1 里（中文放 PS 里，.cmd 保持纯 ASCII）。
$napcatHelper = Join-Path $PSScriptRoot 'fix-napcat.ps1'
if (-not (Test-Path -LiteralPath $napcatHelper)) { throw "Missing fix-napcat.ps1: $napcatHelper" }
Copy-Item -LiteralPath $napcatHelper -Destination (Join-Path $appRoot 'fix-napcat.ps1') -Force
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
# License/notice files must ship with every distribution (MIT and PolyForm both require the
# notice). C-05: this used to `if (Test-Path) { Copy }`, so a missing file was silently
# skipped and the build still "succeeded" -- a fresh clone (where LICENSE-UPSTREAM/NOTICE are
# not yet tracked) would ship a zip with no license at all. Fail loudly instead.
foreach ($item in @('LICENSE', 'LICENSE-UPSTREAM', 'NOTICE')) {
    $source = Join-Path $root $item
    if (-not (Test-Path -LiteralPath $source)) {
        throw ("Missing license file: {0}. It must ship with every build (MIT + PolyForm both" -f $source) + " require the notice). If it is missing from git, `git add` it before building."
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $appRoot $item) -Force
}
$zip = Join-Path $PSScriptRoot 'QQ-Notice-Hub.zip'
# An existing read-only zip would make Compress-Archive fail with WinError 5.
if (Test-Path -LiteralPath $zip) {
    attrib -R $zip
    Remove-Item -LiteralPath $zip -Force
}
# C-03: zip entries carry the file attribute bits, so a read-only source file lands in the
# install dir as ReadOnly. 先看我.txt tells users to "unzip over the old folder" and that then
# fails with PermissionError [WinError 5]. Clear the read-only bit on every file first.
$readOnlyCleared = 0
Get-ChildItem -LiteralPath $appRoot -Recurse -File | ForEach-Object {
    if ($_.IsReadOnly) {
        $_.IsReadOnly = $false
        $readOnlyCleared++
    }
}
Write-Host ("Cleared read-only attribute on {0} file(s) under {1}" -f $readOnlyCleared, $appRoot)
# C-01: -Path $appRoot would wrap everything in a top-level `noticehub\` directory, but
# install-task.ps1 registers <install root>\QQ-Notice-Hub.exe. That mismatch is how production
# ended up running the stale root-level noticehub.exe while a newer nested copy sat unused.
# Archive the directory *contents* so the zip top level IS the install root.
# C-04: entry names must use `/` (ZIP spec). Windows PowerShell 5.1 runs on .NET Framework, where
# [System.IO.Compression.ZipFile]::CreateFromDirectory writes `\` separators (measured: 280 of 293
# entries in the 2026-10-11 16:22 build) -- Linux/macOS unzip then treats the backslash as part of
# the filename and extracts everything into one broken flat mess. Compress-Archive has the same
# defect. So write the entries by hand with forward slashes.
Add-Type -AssemblyName 'System.IO.Compression.FileSystem'
Add-Type -AssemblyName 'System.IO.Compression'
$zipStream = [System.IO.File]::Open($zip, [System.IO.FileMode]::Create)
try {
    $archive = New-Object System.IO.Compression.ZipArchive($zipStream, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in (Get-ChildItem -LiteralPath $appRoot -Recurse -File)) {
            $entryName = $file.FullName.Substring($appRoot.Length + 1).Replace('\', '/')
            $entry = $archive.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
            $entryStream = $entry.Open()
            $fileStream = [System.IO.File]::OpenRead($file.FullName)
            try { $fileStream.CopyTo($entryStream) }
            finally { $fileStream.Dispose(); $entryStream.Dispose() }
        }
    }
    finally { $archive.Dispose() }
}
finally { $zipStream.Dispose() }
Write-Host ("Packed {0} files into {1}" -f (Get-ChildItem -LiteralPath $appRoot -Recurse -File).Count, $zip)

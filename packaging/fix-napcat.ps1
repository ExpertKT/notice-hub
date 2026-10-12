# 已经自己装过 NapCat 的用户：把 NapCat 的位置写进群务台的 .env（QQ_DIGEST_NAPCAT_DIR）。
# 由同目录的「如果你已经装好NapCat点这个.cmd」调用。删掉这个 .ps1 只是让那个 .cmd 失效，不影响群务台本身。
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$root = $PSScriptRoot.TrimEnd('\')
$exe = Join-Path $root 'QQ-Notice-Hub.exe'
Write-Host ''
Write-Host '=== 把已经装好的 NapCat 接给群务台 ===' -ForegroundColor Cyan
Write-Host ''
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Host "这个文件夹里没有 QQ-Notice-Hub.exe：$root" -ForegroundColor Yellow
    Write-Host '请把这两个文件放进解压出来的群务台文件夹，再双击 .cmd。'
    Read-Host '按回车关闭'
    exit 1
}
Write-Host "群务台目录：$root"

if ($env:NH_NAPCAT_SEARCH) {
    $roots = $env:NH_NAPCAT_SEARCH -split ';' | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
} else {
    $roots = @(
        [Environment]::GetFolderPath('Desktop'),
        (Join-Path $env:USERPROFILE 'OneDrive\Desktop'),
        (Join-Path $env:USERPROFILE 'Downloads'),
        [Environment]::GetFolderPath('MyDocuments'),
        $root,
        (Split-Path -Parent $root)
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
}

Write-Host '正在找 NapCat（可能要几十秒，请稍等）…'
$found = @()
foreach ($r in $roots) {
    $cands = Get-ChildItem -LiteralPath $r -Directory -Filter 'NapCat*.Shell' -Recurse -Depth 4 -ErrorAction SilentlyContinue
    foreach ($c in $cands) {
        if (-not (Test-Path -LiteralPath (Join-Path $c.FullName 'QQ.exe'))) { continue }
        $boot = Get-ChildItem -LiteralPath $c.FullName -Recurse -Filter 'NapCatWinBootMain.exe' -Depth 6 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($boot) { $found += $c.FullName }
    }
}
$found = @($found | Select-Object -Unique)

if ($found.Count -eq 0) {
    Write-Host ''
    Write-Host '没有自动找到 NapCat。' -ForegroundColor Yellow
    Write-Host '你如果是用官方「一键包」装的，打开当时装它的那个文件夹，里面会有一个叫 NapCat.<版本>.Shell 的文件夹。'
    Write-Host '把它整个复制到下面这个文件夹，再重启群务台也行：'
    Write-Host ('  ' + (Join-Path $root 'data\napcat'))
    Write-Host ''
    Write-Host '或者把那个文件夹的完整路径发回来，让别人帮你写配置。'
    Read-Host '按回车关闭'
    exit 1
}

$shell = $found[0]
if ($found.Count -gt 1) {
    Write-Host '找到多个，请选一个：'
    for ($i = 0; $i -lt $found.Count; $i++) { Write-Host ('  [{0}] {1}' -f ($i + 1), $found[$i]) }
    $pick = Read-Host '输入编号（直接回车就用第 1 个）'
    if ($pick -match '^\d+$' -and [int]$pick -ge 1 -and [int]$pick -le $found.Count) { $shell = $found[[int]$pick - 1] }
}
Write-Host ''
Write-Host "找到 NapCat：$shell" -ForegroundColor Green

$envFile = Join-Path $root '.env'
$ans = Read-Host '把它接给群务台？(直接回车=接上，输入 n=取消)'
if ($ans -match '^(n|N|no|不|否)$') {
    Write-Host '已取消，没有改动。'
    Read-Host '按回车关闭'
    exit 1
}

if (Test-Path -LiteralPath $envFile) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Copy-Item -LiteralPath $envFile -Destination ($envFile + '.bak-' + $stamp) -Force
    $lines = [System.IO.File]::ReadAllLines($envFile) | Where-Object { -not $_.TrimStart().StartsWith('QQ_DIGEST_NAPCAT_DIR') }
    Write-Host "（原配置已备份为 .env.bak-$stamp）"
} else {
    $lines = @('# 群务台配置：由「如果你已经装好NapCat点这个.cmd」生成')
}
$lines = @($lines) + ('QQ_DIGEST_NAPCAT_DIR=' + $shell)
[System.IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ''
Write-Host "已写入：$envFile" -ForegroundColor Green
Write-Host ''
Write-Host '接下来三步：' -ForegroundColor Cyan
Write-Host '  1) 群务台如果正在运行：右下角托盘图标上右键 → 退出'
Write-Host '  2) 双击 QQ-Notice-Hub.exe'
Write-Host '  3) 网页里点「启动并登录」，用手机 QQ 扫二维码'
Write-Host ''
Write-Host '如果还是提示找不到 NapCat，把上面的「找到 NapCat」那行路径发回来。'
Read-Host '按回车关闭'
exit 0

<#
把仓库里的 wiki（docs/wiki/）同步到 GitHub Wiki。

为什么要有这一步：文档以仓库里的 docs/wiki/ 为准（跟代码同版本、能 review、Gitee 也看得到），
GitHub Wiki 只是发布出去的镜像。发版时跑一次，别指望人记着。

第一次用之前，需要有人在 GitHub 仓库的 Wiki 页点一次「Create the first page」——
GitHub 只有在 wiki 仓库被创建之后才接受 clone/push，这步脚本替不了。

用法：
    powershell -File tools\wiki_sync.ps1
    powershell -File tools\wiki_sync.ps1 -Message "同步 1.1.0-beta.16"
#>
[CmdletBinding()]
param(
    [string]$Message = "docs(wiki): 从仓库 docs/wiki 同步"
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root 'docs\wiki'
$work = Join-Path $root 'nh-tmp\gh-wiki'
$remote = 'https://github.com/ExpertKT/notice-hub.wiki.git'

# 这台机器上 ~/.gitconfig 给 github.com 单独配了代理，那条线路不通；直连可用，所以这里显式绕开。
$gitArgs = @('-c', 'http.https://github.com.proxy=')

if (-not (Test-Path $source)) {
    throw "找不到 $source"
}

$pages = @(Get-ChildItem -Path $source -Filter *.md -File)
if ($pages.Count -eq 0) {
    throw "$source 里没有 markdown 页面"
}

# git 把进度写 stderr，配合 $ErrorActionPreference='Stop' 会被当成异常抛出来；
# 这里在调用期间放行输出，只看退出码。
function Invoke-Git {
    param([string[]]$GitArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & git @GitArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ Code = $code; Out = $output }
}

if (Test-Path (Join-Path $work '.git')) {
    $pulled = Invoke-Git (@('-C', $work) + $gitArgs + @('pull', '--ff-only'))
    if ($pulled.Code -ne 0) { throw '拉取 wiki 仓库失败' }
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $work) | Out-Null
    if (Test-Path $work) { Remove-Item $work -Recurse -Force }
    $cloned = Invoke-Git ($gitArgs + @('clone', $remote, $work))
    if ($cloned.Code -ne 0) {
        throw "clone wiki 仓库失败。多半是 GitHub 上还没有 wiki 页：去仓库的 Wiki 标签点一次「Create the first page」，再来跑。$($cloned.Out -join ' ')"
    }
}

# GitHub Wiki 用 Home.md 当首页，其余文件名沿用。
foreach ($page in $pages) {
    $name = $page.Name
    if ($name -ieq 'README.md') { $name = 'Home.md' }
    Copy-Item -LiteralPath $page.FullName -Destination (Join-Path $work $name) -Force
}

# 源页面里指向 README.md 的链接，在 wiki 上要指向首页。
foreach ($file in Get-ChildItem -Path $work -Filter *.md -File) {
    $text = [IO.File]::ReadAllText($file.FullName)
    $patched = $text -replace '\]\(README\.md', '](Home'
    if ($patched -ne $text) {
        [IO.File]::WriteAllBytes($file.FullName, (New-Object Text.UTF8Encoding($false)).GetBytes($patched))
    }
}

$null = Invoke-Git @('-C', $work, 'add', '-A')
$committed = Invoke-Git @('-C', $work, '-c', 'user.name=notice-hub', '-c', 'user.email=bot@local', 'commit', '-q', '-m', $Message)
if ($committed.Code -ne 0) {
    Write-Output 'wiki 没有变化，无需推送'
    return
}

$pushed = Invoke-Git (@('-C', $work) + $gitArgs + @('push', 'origin', 'HEAD'))
if ($pushed.Code -ne 0) { throw "推送 wiki 失败：$($pushed.Out -join ' ')" }
Write-Output 'wiki 已同步'

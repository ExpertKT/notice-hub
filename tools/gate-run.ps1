# 唯一允许起全量门禁的入口（规矩见 docs\routines.md 第六节）。
# 要点：
#   1. 抢锁：用操作系统的独占句柄（[System.IO.File]::Open 带 None 共享模式）占住 gate.lock，
#      进程活着别人就打不开；进程一死系统自动放开，不会留死锁，也不靠「读 pid 再看活没活」。
#   1b. 抖动不算绿：首轮 FAILED 就把那几条用例单独复跑，复跑绿了写进 flake-ledger 并 EXIT=2。
#   2. 在快照里跑：git worktree add --detach 出一份该 commit 的干净树，别人此刻提交也不影响这一轮。
#   3. 收尾：把 Ran/OK|FAILED 写进 .reports\gate-records.md，顺手刷新 .reports\state.md。
# 用法：
#   powershell -NoProfile -File tools\gate-run.ps1 -Owner 赛事 -Round r1
#   powershell -NoProfile -File tools\gate-run.ps1 -Owner 赛事 -Module tests.test_routes   # 快模块自检
param(
    [string]$Owner = 'unknown',
    [string]$Round = 'r1',
    [string]$Module = '',
    [string]$Sha = ''
)
$ErrorActionPreference = 'Stop'
# --- 环境去重：Start-Process 遇到 NO_PROXY 与 no_proxy 同时存在会抛「已添加项」 ---
foreach ($dup in @('no_proxy','http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY')) { Remove-Item -LiteralPath ('Env:' + $dup) -ErrorAction SilentlyContinue }
$repo = Split-Path -Parent $PSScriptRoot
$tmpRoot = $env:NH_GATE_TMP
if ([string]::IsNullOrWhiteSpace($tmpRoot)) { $tmpRoot = 'F:\qq-notice-hub\nh-tmp' }
$logDir = Join-Path $tmpRoot 'logs'
$lock = Join-Path $logDir 'gate.lock'
$utf8 = New-Object System.Text.UTF8Encoding($false)
# 给人看的文件（records / ledger / state）带 BOM：PowerShell 5.1 默认按 ANSI 读，没 BOM 会显示成乱码。
$utf8bom = New-Object System.Text.UTF8Encoding($true)
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# --- 抢锁：独占句柄，进程死即释放 ---
$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open($lock, [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
}
catch {
    $held = ''
    try { $held = Get-Content -LiteralPath $lock -Raw -Encoding UTF8 } catch { }
    Write-Host ("拒绝起跑：已经有一轮在跑，锁打不开（" + $held + "）。等它结束，别杀它。")
    exit 3
}

Push-Location -LiteralPath $repo
$snap = $null
try {
    if ([string]::IsNullOrWhiteSpace($Sha)) { $Sha = (& git rev-parse --short HEAD) }
    $python = Join-Path $repo '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = 'F:\qq-notice-hub\.venv\Scripts\python.exe' }
    $log = Join-Path $logDir ('gate-' + $Sha + '-' + $Round + '.log')
    $started = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $info = [ordered]@{ owner = $Owner; pid = $PID; sha = $Sha; started = $started; module = $Module; log = $log }
    $lockBytes = $utf8.GetBytes(($info | ConvertTo-Json -Compress))
    $lockStream.SetLength(0)
    $lockStream.Write($lockBytes, 0, $lockBytes.Length)
    $lockStream.Flush()

    # --- 快照 ---
    $snap = Join-Path $tmpRoot ('wt-' + $Sha)
    if (Test-Path -LiteralPath $snap) {
        & git worktree remove --force $snap *> $null
        if (Test-Path -LiteralPath $snap) { Remove-Item -LiteralPath $snap -Recurse -Force }
    }
    & git worktree prune *> $null
    & git worktree add --detach $snap $Sha *> $null
    if ($LASTEXITCODE -ne 0) { throw ("worktree add 失败，sha " + $Sha) }
    Write-Host ("起跑：" + $Owner + " 轮 " + $Round + " sha " + $Sha + " 快照 " + $snap + " => " + $log)

    # --- 跑 ---
    $safeOwner = $Owner -replace '[^\w\u4e00-\u9fa5-]', '_'
    $work = Join-Path $tmpRoot ('t-' + $safeOwner + '-gate')
    if (-not (Test-Path -LiteralPath $work)) { New-Item -ItemType Directory -Path $work -Force | Out-Null }
    $argList = @('-X', 'utf8', '-m', 'unittest')
    if ([string]::IsNullOrWhiteSpace($Module)) { $argList += @('discover', '-s', 'tests', '-t', '.') } else { $argList += $Module }
    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    $env:TEMP = $work
    $env:TMP = $work
    $logErr = $log + '.err'
    try {
        $proc = Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory $snap -NoNewWindow -Wait -PassThru -RedirectStandardOutput $log -RedirectStandardError $logErr
        $code = $proc.ExitCode
    }
    finally {
        $env:TEMP = $oldTemp
        $env:TMP = $oldTmp
    }
    if (Test-Path -LiteralPath $logErr) {
        $errText = [System.IO.File]::ReadAllText($logErr)
        if ($errText.Length -gt 0) { [System.IO.File]::AppendAllText($log, $errText, $utf8) }
        Remove-Item -LiteralPath $logErr -Force
    }

    # --- 结论 ---
    $tail = @()
    if (Test-Path -LiteralPath $log) {
        $tail = Get-Content -LiteralPath $log -Tail 60 -Encoding UTF8 | Where-Object { $_ -match '^Ran |^(OK|FAILED)|^(FAIL|ERROR):' }
    }
    $verdict = ($tail -join ' ; ')
    if ([string]::IsNullOrWhiteSpace($verdict)) { $verdict = '(日志里没有 Ran/OK/FAILED)' }

    # --- 抖动不算绿：首轮红就把那几条单独复跑，复跑绿了也只记台账、判 EXIT=2 ---
    $flake = ''
    if ($code -ne 0 -and (Test-Path -LiteralPath $log)) {
        $ids = @(Get-Content -LiteralPath $log -Encoding UTF8 |
            Where-Object { $_ -match '^(FAIL|ERROR): ' } |
            ForEach-Object { if ($_ -match '\(([\w\.]+)\)\s*$') { $matches[1] } } | Select-Object -Unique)
        if ($ids.Count -gt 0) {
            $retryLog = $log + '.retry'
            $oldTemp2 = $env:TEMP; $oldTmp2 = $env:TMP
            $env:TEMP = $work; $env:TMP = $work
            try {
                $retry = Start-Process -FilePath $python -ArgumentList (@('-X', 'utf8', '-m', 'unittest') + $ids) `
                    -WorkingDirectory $snap -NoNewWindow -Wait -PassThru `
                    -RedirectStandardOutput $retryLog -RedirectStandardError ($retryLog + '.err')
                $retryCode = $retry.ExitCode
            }
            finally { $env:TEMP = $oldTemp2; $env:TMP = $oldTmp2 }
            if ($retryCode -eq 0) {
                $flake = ('首轮红、复跑绿（' + ($ids -join ', ') + '）：抖动，不算通过')
                $ledger = Join-Path $repo '.reports\flake-ledger.md'
                [System.IO.File]::AppendAllText($ledger,
                    ('- {0} | {1} | sha {2} 轮 {3} | {4}{5}' -f $started, $Owner, $Sha, $Round, $flake, [Environment]::NewLine), $utf8bom)
                $code = 2
                $verdict = $verdict + ' ; ' + $flake
                Write-Host ("抖动：" + ($ids -join ', ') + " 复跑绿了，按不通过记。")
            }
            else {
                $flake = ('复跑仍红（' + ($ids -join ', ') + '）')
                $verdict = $verdict + ' ; ' + $flake
            }
        }
    }
    $records = Join-Path $repo '.reports\gate-records.md'
    $dir = Split-Path -Parent $records
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $line = ('- {0} | {1} | {2} | {3} | EXIT={4} | {5}' -f $started, $Owner, $Sha, $Round, $code, $verdict)
    [System.IO.File]::AppendAllText($records, $line + [Environment]::NewLine, $utf8bom)

    # --- 状态文件跟着刷 ---
    $head = (& git rev-parse --short HEAD)
    $porcelain = @(git status --porcelain)
    $count = 0
    if ($porcelain -and $porcelain.Length -gt 0) { $count = $porcelain.Length }
    $state = @(
        '# 当前状态（唯一来源，只有赛事改；由 tools\gate-run.ps1 在每轮门禁后自动刷新）',
        '',
        ('最后刷新：' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '（门禁轮次 ' + $Round + ' 收尾后）'),
        '',
        ('- HEAD：' + $head),
        ('- 工作区脏文件：' + $count + ' 个'),
        ('- 最近一轮门禁：sha ' + $Sha + ' 轮 ' + $Round + '，EXIT=' + $code + '，' + $verdict),
        '- 门禁明细见 `.reports\gate-records.md`',
        '- 打包与冷装的实测数见 `.reports\build-and-cold.md`（本文件每次刷新都会整篇重写，别往这里写别的）',
        '- 规矩见 `docs\routines.md` 第六节：不要自己量 HEAD、不要自己起门禁、不要杀 python 进程'
    ) -join [Environment]::NewLine
    [System.IO.File]::WriteAllText((Join-Path $repo '.reports\state.md'), $state, $utf8bom)

    Write-Host ("收工：EXIT=" + $code + " " + $verdict)
    exit $code
}
finally {
    if ($snap -and (Test-Path -LiteralPath $snap)) { & git worktree remove --force $snap *> $null }
    Pop-Location
    if ($lockStream) { $lockStream.Dispose() }
}
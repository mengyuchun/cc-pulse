#Requires -Version 7
# CC-Pulse - cc-switch 供应商健康检测与单模型深度诊断 · 桌面启动器
# 双击运行：层次化菜单（快速体检 / 自定义 / 拉模型 / 深度诊断 / 高级设置 / 退出）

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Read-Host 容错：stdin 关闭（管道/自动化）时 Read-Host 抛终止错误，这里吞掉返回 ""。
# 主菜单已用 [Console]::In.ReadLine() 区分 EOF；子菜单的兜底暂停用它避免崩。
function Read-HostSafe {
    param([string]$Prompt = "")
    try { return (Read-Host $Prompt) } catch { return "" }
}

# ── 交互式 TUI 基础设施（箭头 + 鼠标 + 空格多选） ──────────────
$ESC = [char]27

function _GetCursorRow {
    # ANSI DSR: 发送 ESC[6n，终端回 ESC[row;colR
    [Console]::Write("$ESC[6n")
    Start-Sleep -Milliseconds 20
    $resp = ""
    $dl = (Get-Date).AddMilliseconds(500)
    while ((Get-Date) -lt $dl -and [Console]::KeyAvailable) {
        $ch = [Console]::ReadKey($true).KeyChar
        $resp += $ch
        if ($ch -eq 'R') { break }
    }
    if ($resp -match '\[(\d+);(\d+)R') { return [int]$matches[1] }
    return $null
}

function _ReadKeySequence {
    # 读一个按键；如果是 SGR 鼠标事件（ESC[<...M/m），解析为鼠标事件
    $key = [Console]::ReadKey($true)
    if ($key.KeyChar -eq $ESC) {
        Start-Sleep -Milliseconds 5
        $seq = "$ESC"
        while ([Console]::KeyAvailable) { $seq += [Console]::ReadKey($true).KeyChar }
        if ($seq -match '^\x1b\[<(\d+);(\d+);(\d+)([Mm])$') {
            return @{
                Type   = "Mouse"
                Button = [int]$matches[1]
                Col    = [int]$matches[2]
                Row    = [int]$matches[3]
                Press  = ($matches[4] -eq 'M')
            }
        }
        return @{ Type = "Key"; Key = "Escape" }
    }
    return @{ Type = "Key"; Key = $key.Key; Char = $key.KeyChar }
}

# 交互式箭头+鼠标选择器。
# ↑↓/滚轮 移动，空格切换多选，a 全选/取消，回车确认，ESC 取消，鼠标左键点击选择。
# 返回选中项索引数组；ESC 返回 $null。仅在交互式终端调用。
function Show-ArrowSelect {
    param(
        [string[]]$Options,
        [string]$Title = "选择",
        [switch]$Multi,
        [int]$PageSize = 15
    )
    $n = $Options.Count
    if ($n -eq 0) { return @() }
    $checked = New-Object 'bool[]' $n
    $cursor = 0
    $scroll = 0
    $hide = "$ESC[?25l"; $show = "$ESC[?25h"
    $clr = "$ESC[2K"; $up = "$ESC[1A"
    $mouseOn = "$ESC[?1000h$ESC[?1006h"
    $mouseOff = "$ESC[?1000l$ESC[?1006l"
    $hint = if ($Multi) { "↑↓/滚轮  空格选择  a 全选  回车确认  ESC/右键取消" }
            else { "↑↓/滚轮  回车确认  鼠标点击  ESC/右键取消" }

    $startRow = _GetCursorRow
    Write-Host $hide$mouseOn -NoNewline
    $prev = 0
    try {
        while ($true) {
            if ($cursor -lt $scroll) { $scroll = $cursor }
            elseif ($cursor -ge $scroll + $PageSize) { $scroll = $cursor - $PageSize + 1 }
            $end = [Math]::Min($n, $scroll + $PageSize)

            if ($prev -gt 0) {
                for ($i = 0; $i -lt $prev; $i++) { Write-Host "$up$clr" -NoNewline }
            }
            Write-Host "? $Title  ($hint)"
            for ($i = $scroll; $i -lt $end; $i++) {
                $mark = if ($checked[$i]) { [char]0x2705 } else { [char]0x2B1C }
                $arr = if ($i -eq $cursor) { [char]0x25B8 } else { " " }
                Write-Host ("  {0} {1} {2}" -f $arr, $mark, $Options[$i])
            }
            if ($Multi) {
                $cnt = 0; for ($i = 0; $i -lt $n; $i++) { if ($checked[$i]) { $cnt++ } }
                Write-Host "  [$cnt/$n]"
            } else {
                Write-Host "  [$($cursor + 1)/$n]"
            }
            $prev = 1 + ($end - $scroll) + 1

            $ev = _ReadKeySequence

            # ── 鼠标事件 ──
            if ($ev.Type -eq "Mouse") {
                if (-not $ev.Press) { continue }  # 只处理按下
                if ($ev.Button -eq 2) { return $null }  # 右键 = 取消
                if ($ev.Button -eq 64) { $cursor = [Math]::Max(0, $cursor - 1); continue }  # 滚轮上
                if ($ev.Button -eq 65) { $cursor = [Math]::Min($n - 1, $cursor + 1); continue }  # 滚轮下
                if ($ev.Button -eq 0 -and $null -ne $startRow) {
                    # 左键点击：映射行号到选项索引
                    $relRow = $ev.Row - $startRow  # 1=title, 2..N+1=option, N+2=status
                    $optIdx = $relRow - 2 + $scroll  # 选项行从 relRow=2 开始
                    if ($optIdx -ge $scroll -and $optIdx -lt $end) {
                        $cursor = $optIdx
                        if ($Multi) { $checked[$cursor] = -not $checked[$cursor] }
                        else { return @($cursor) }
                    }
                }
                continue
            }

            # ── 键盘事件 ──
            $k = $ev.Key
            if ($k -eq "UpArrow")   { $cursor = ($cursor - 1 + $n) % $n }
            elseif ($k -eq "DownArrow") { $cursor = ($cursor + 1) % $n }
            elseif ($k -eq "Enter") {
                if ($Multi) {
                    $sel = @()
                    for ($i = 0; $i -lt $n; $i++) { if ($checked[$i]) { $sel += $i } }
                    # 空回车 = 确认「默认/全选」：返回 -1 sentinel，与 ESC($null) 区分
                    if ($sel.Count -eq 0) { return @(-1) }
                    return $sel
                }
                return @($cursor)
            }
            elseif ($k -eq "Escape") { return $null }
            elseif ($k -eq "Spacebar" -and $Multi) { $checked[$cursor] = -not $checked[$cursor] }
            elseif ($Multi -and $ev.Char -eq 'a') {
                $allOn = $true
                for ($i = 0; $i -lt $n; $i++) { if (-not $checked[$i]) { $allOn = $false; break } }
                for ($i = 0; $i -lt $n; $i++) { $checked[$i] = -not $allOn }
            }
        }
    } finally {
        Write-Host "$mouseOff$show" -NoNewline
    }
}

# 多选列表：交互式走箭头+鼠标，降级走数字输入。返回选中【值】数组；ESC 返回 $null。
function Select-FromList {
    param(
        [string[]]$Options,
        [string]$Title = "选择",
        [switch]$AllowLiteral,
        [string]$AllLabel = "全部",
        [switch]$DefaultAll
    )
    if ($Options.Count -eq 0) { return @() }

    if (-not [Console]::IsInputRedirected) {
        $selIdx = Show-ArrowSelect -Options $Options -Multi -Title $Title
        if ($null -eq $selIdx) { return $null }
        if ($selIdx[0] -eq -1) {
            return @(if ($DefaultAll) { $Options } else { @($Options[0]) })
        }
        return $selIdx | ForEach-Object { $Options[$_] }
    }

    # 降级：数字输入（保持与旧逻辑兼容，含字面名）
    for ($i = 0; $i -lt $Options.Count; $i++) {
        Write-Host "  [$($i + 1)] $($Options[$i])"
    }
    Write-Host "  [a] $AllLabel" -ForegroundColor Cyan
    $sel = Read-Host "输入序号（逗号分隔，如 1,3）或 a 全选（$($(if ($DefaultAll) { '空回车=全选' } else { '默认 1' }))）"
    if ([string]::IsNullOrWhiteSpace($sel)) {
        return @(if ($DefaultAll) { $Options } else { @($Options[0]) })
    }
    if ($sel -eq "a" -or $sel -eq "A") { return @($Options) }
    $idxs = $sel -split "," | ForEach-Object { $_.Trim() }
    $result = @()
    foreach ($idx in $idxs) {
        if ($idx -match '^\d+$' -and [int]$idx -ge 1 -and [int]$idx -le $Options.Count) {
            $result += $Options[[int]$idx - 1]
        } elseif ($AllowLiteral -and -not [string]::IsNullOrWhiteSpace($idx)) {
            $result += $idx
        }
    }
    if ($result.Count -eq 0) { return @($Options[0]) }
    return $result
}

# 单选菜单：交互式走箭头+鼠标，降级走数字输入。
# 返回选中项索引（0-based）；ESC 返回 -1；降级时非数字字面值返回 -2（调用方可读 $script:LastLiteral）。
function Select-MenuItem {
    param(
        [string[]]$Options,
        [string]$Title = "选择",
        [string]$Prompt = "输入序号",
        [string]$DefaultSuffix = "默认1"
    )
    $script:LastLiteral = $null
    $script:LastInput = $null
    if ($Options.Count -eq 0) { return -1 }

    if (-not [Console]::IsInputRedirected) {
        $selIdx = Show-ArrowSelect -Options $Options -Title $Title
        if ($null -eq $selIdx -or $selIdx.Count -eq 0) { return -1 }
        return $selIdx[0]
    }

    # 降级：数字输入
    for ($i = 0; $i -lt $Options.Count; $i++) {
        Write-Host "  [$($i + 1)] $($Options[$i])"
    }
    $sel = Read-Host "$Prompt（$DefaultSuffix）"
    $script:LastInput = $sel
    if ([string]::IsNullOrWhiteSpace($sel)) { return 0 }
    if ($sel -match '^\d+$' -and [int]$sel -ge 1 -and [int]$sel -le $Options.Count) {
        return [int]$sel - 1
    }
    # 非数字字面值：保存到 LastLiteral，返回 -2
    $script:LastLiteral = $sel
    return -2
}

# ── Python 与数据库路径解析 ──────────────────────────────────────
$Python = if ($env:CC_PULSE_PYTHON) {
    $env:CC_PULSE_PYTHON
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    (Get-Command python).Source
} else {
    $null
}
if (-not $Python) {
    Write-Host "未找到 Python。请将 Python 加入 PATH，或设置 CC_PULSE_PYTHON。" -ForegroundColor Red
    Read-HostSafe "按回车关闭"
    exit 2
}
$MainScript = Join-Path $ScriptDir "check_ccswitch_health.py"
if (-not (Test-Path $MainScript)) {
    Write-Host "未找到主脚本: $MainScript" -ForegroundColor Red
    Read-HostSafe "按回车关闭"
    exit 2
}
$DB = if ($env:CC_PULSE_DB) {
    $env:CC_PULSE_DB
} else {
    Join-Path $HOME ".cc-switch\cc-switch.db"
}

# ── 高级设置（进程内有效，重开需重设；日常用默认即可） ──────────
$script:AdvJson = $false
$script:AdvMaxTokens = ""
$script:AdvEnableThinking = $false
$script:AdvUserAgent = ""
$script:AdvProbeContext = "512k"   # inspect 无声明窗口时的上下文冒烟：512k | 1m
$script:AdvVision = $false        # inspect 是否附带 vision
$script:AdvStealth = $false       # check 隐身模式：降并发 + 请求间随机延迟
$script:AdvType = "claude"        # check 默认供应商类型：claude/codex/openclaw/all
$script:AdvScope = "failover"     # check 默认范围：failover(队列+当前) / all(全部)
# inspect 保真鉴别 P2 探针（默认关，烧请求时显式开）
$script:AdvAuthFull = $false             # 一键入口临时启用 --auth-full
$script:AdvAuthCacheReplay = $false      # --include cache-replay（2 次额外请求）
$script:AdvAuthKnowledgeCutoff = $false  # --include knowledge-cutoff（6 次额外请求）
$script:AdvAuthJsFingerprint = $false    # --include js-fingerprint（默认 50 次额外请求）
$script:AdvJsSamples = ""                 # --js-samples N（留空=50）
$script:AdvType = "claude"        # check 默认供应商类型：claude/codex/openclaw/all
$script:AdvScope = "failover"     # check 默认范围：failover(队列+当前) / all(全部)

function Show-Banner {
    param([string]$Title = "CC-Pulse · cc-switch 供应商健康检测与单模型深度诊断")
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  $Title" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "数据库: $DB"
    if (-not (Test-Path $DB)) {
        Write-Host "数据库不存在。" -ForegroundColor Red
        return $false
    }
    Write-Host "Python:  $Python"
    Write-Host ""
    return $true
}

function Get-AppType {
    param([string]$Default = "claude")
    $typeOptions = @("claude (默认)", "codex", "openclaw", "all")
    $idx = Select-MenuItem -Options $typeOptions -Title "请选择要检测的供应商类型"
    if ($idx -lt 0) { return $Default }
    switch ($idx) {
        1 { return "codex" }
        2 { return "openclaw" }
        3 { return "all" }
        default { return $Default }
    }
}

# 把高级设置追加到 cmdArgs（不交互，直接读 $script: 变量）。
# SubCommand 指定当前子命令，按子命令过滤不支持的参数（避免 argparse 崩溃）。
function Apply-AdvancedArgs {
    param(
        [System.Collections.Generic.List[string]]$CmdArgs,
        [string]$SubCommand = ""
    )
    # --json 只有 check 子命令支持
    if ($script:AdvJson -and $SubCommand -eq "check") { $CmdArgs.Add("--json") }
    # --stealth 只对 check 生效（降并发 + 随机延迟，弱化流量尖峰）
    if ($script:AdvStealth -and $SubCommand -eq "check") { $CmdArgs.Add("--stealth") }
    # --probe-max-tokens / --probe-enable-thinking 作用于会发探测请求的子命令
    $probeCmds = @("check", "inspect", "list-models")
    if ($probeCmds -contains $SubCommand) {
        if (-not [string]::IsNullOrWhiteSpace($script:AdvMaxTokens)) {
            $CmdArgs.Add("--probe-max-tokens"); $CmdArgs.Add($script:AdvMaxTokens)
        }
        if ($script:AdvEnableThinking) { $CmdArgs.Add("--probe-enable-thinking") }
    }
    # inspect 专属：上下文档位 + 可选 vision
    if ($SubCommand -eq "inspect") {
        if ($script:AdvAuthFull) { $CmdArgs.Add("--auth-full") }
        if (-not [string]::IsNullOrWhiteSpace($script:AdvProbeContext)) {
            $CmdArgs.Add("--probe-context"); $CmdArgs.Add($script:AdvProbeContext)
        }
        if ($script:AdvVision) {
            # vision 追加到已有 --include（自定义维度），否则用全量+vision
            $incIdx = $CmdArgs.IndexOf("--include")
            if ($incIdx -ge 0 -and $incIdx + 1 -lt $CmdArgs.Count) {
                $base = [string]$CmdArgs[$incIdx + 1]
                if ($base -notmatch '(^|,)vision($|,)') {
                    $CmdArgs[$incIdx + 1] = $base.TrimEnd(',') + ",vision"
                }
            } else {
                $CmdArgs.Add("--include")
                $CmdArgs.Add("text,streaming,model-consistency,protocol,error-classification,metadata,thinking,tools,vision")
            }
        }
        # 保真鉴别 P2 探针：追加到已有 --include（默认全关，烧请求时显式开）
        $authExtras = @()
        if ($script:AdvAuthCacheReplay) { $authExtras += "cache-replay" }
        if ($script:AdvAuthKnowledgeCutoff) { $authExtras += "knowledge-cutoff" }
        if ($script:AdvAuthJsFingerprint) { $authExtras += "js-fingerprint" }
        if ($authExtras.Count -gt 0) {
            $incIdx = $CmdArgs.IndexOf("--include")
            $extra = $authExtras -join ","
            if ($incIdx -ge 0 -and $incIdx + 1 -lt $CmdArgs.Count) {
                $base = [string]$CmdArgs[$incIdx + 1]
                $CmdArgs[$incIdx + 1] = $base.TrimEnd(',') + "," + $extra
            } else {
                $CmdArgs.Add("--include")
                $CmdArgs.Add("text,streaming,model-consistency,protocol,error-classification,metadata,thinking,tools," + $extra)
            }
            if ($script:AdvAuthJsFingerprint -and -not [string]::IsNullOrWhiteSpace($script:AdvJsSamples)) {
                $CmdArgs.Add("--js-samples"); $CmdArgs.Add($script:AdvJsSamples)
            }
        }
    }
    # --user-agent 所有子命令都支持
    if (-not [string]::IsNullOrWhiteSpace($script:AdvUserAgent)) {
        $CmdArgs.Add("--user-agent"); $CmdArgs.Add($script:AdvUserAgent)
    }
}

function Invoke-Ccpulse {
    param([string[]]$CmdArgs)
    $CmdArgs = @($CmdArgs | Where-Object { $_ -ne $null -and "$_" -ne "" })
    Write-Host "----------------------------------------"
    Write-Host "运行: $Python -u check_ccswitch_health.py $($CmdArgs -join ' ')" -ForegroundColor DarkGray
    Write-Host "----------------------------------------"
    Write-Host "探测中，预计 1-3 分钟，请勿关闭窗口…" -ForegroundColor Cyan
    # -u：Python 无缓冲，完成一个就刷一行（管道下默认块缓冲会「全跑完才显示」）
    # 不用 | Out-Host：避免对象管道二次缓冲；2>&1 仍合并 stderr（JSON 模式人类进度在 stderr）
    & $Python -u $MainScript @CmdArgs 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            Write-Host $_.ToString()
        } else {
            Write-Host $_
        }
    }
    $code = $LASTEXITCODE
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  完成（退出码: $code）" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    if ($code -ne 0) {
        $meaning = switch ($code) {
            1 { "1 = 部分失败（有供应商/模型不可用）" }
            2 { "2 = 环境/参数错误" }
            3 { "3 = 全部失败" }
            default { "$code = 未定义退出码" }
        }
        Write-Host "释义: $meaning" -ForegroundColor Yellow
        Write-Host "建议重试或查看运行日志。" -ForegroundColor Yellow
    }
    return $code
}

function Get-Timeout {
    if ($env:CC_PULSE_TIMEOUT) { return $env:CC_PULSE_TIMEOUT } else { return "45" }
}

# 跑 check 并捕获 stdout 的 JSON（stderr 的人类进度照常显示）。
# 返回 @{ Code; Data }，Data 为解析后的 PSCustomObject 或 $null。
function Invoke-CcpulseJson {
    param([string[]]$CmdArgs)
    $CmdArgs = @($CmdArgs | Where-Object { $_ -ne $null -and "$_" -ne "" })
    Write-Host "----------------------------------------" -ForegroundColor DarkGray
    Write-Host "运行: $Python -u check_ccswitch_health.py $($CmdArgs -join ' ')" -ForegroundColor DarkGray
    Write-Host "----------------------------------------" -ForegroundColor DarkGray
    # 人类进度在 stderr（check --json 模式下）→ 直接显示；stdout 仅 JSON → 捕获
    $jsonText = ""
    $errLines = [System.Collections.Generic.List[string]]::new()
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo.FileName = $Python
    $proc.StartInfo.ArgumentList.Add("-u")
    $proc.StartInfo.ArgumentList.Add($MainScript)
    foreach ($arg in $CmdArgs) { $proc.StartInfo.ArgumentList.Add([string]$arg) }
    $proc.StartInfo.UseShellExecute = $false
    $proc.StartInfo.RedirectStandardOutput = $true
    $proc.StartInfo.RedirectStandardError = $true
    $proc.StartInfo.CreateNoWindow = $true
    $null = $proc.Start()
    # stdout 只有 JSON（体量小），异步读到底；stderr 是人类进度，逐行实时转发
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadLineAsync()
    while (-not $proc.HasExited) {
        Start-Sleep -Milliseconds 40
        while ($errTask.IsCompleted -and $errTask.Result -ne $null) {
            Write-Host $errTask.Result
            $errTask = $proc.StandardError.ReadLineAsync()
        }
    }
    # 进程退出后清空管道残留的 stderr 行：等待最后一个 async 读完成再 drain（修退出竞态丢尾行）
    if (-not $errTask.IsCompleted) {
        try { $errTask.Wait(500) | Out-Null } catch { }
    }
    $rest = if ($errTask.IsCompleted) { $errTask.Result } else { $null }
    while ($null -ne $rest) { Write-Host $rest; $rest = $proc.StandardError.ReadLine() }
    $jsonText = $outTask.Result
    $code = $proc.ExitCode
    Write-Host ""
    if ($script:AdvJson -and $jsonText) { Write-Host $jsonText.TrimEnd() }
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  完成（退出码: $code）" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    $data = $null
    try {
        if ($jsonText) { $data = $jsonText | ConvertFrom-Json }
    } catch { $data = $null }
    return @{ Code = $code; Data = $data }
}

# 深挖供应商选择器：从 check 的 JSON 结果里挑出失败/可用供应商供多选。
# $okList / $failList: [PSCustomObject[]]（providers 数组切片）
# 返回选中的 provider 数组；取消返回 $null。
function Select-DeepDiveTargets {
    param(
        [object[]]$FailProviders,
        [object[]]$OkProviders
    )
    $options = [System.Collections.Generic.List[string]]::new()
    $map = [System.Collections.Generic.List[object]]::new()
    foreach ($p in @($FailProviders)) {
        $options.Add("❌ $($p.name)   ($($p.type) / 失败)")
        $map.Add($p)
    }
    foreach ($p in @($OkProviders)) {
        $best = if ($p.best_tier) { "✓$($p.best_tier)" } else { "✓" }
        $options.Add("✅ $($p.name)   ($($p.type) / $best)")
        $map.Add($p)
    }
    $selIdx = Select-FromList -Options $options.ToArray() -Title "选择要深挖的供应商（多选）" -AllLabel "全部选中"
    if ($null -eq $selIdx) { return $null }
    if ($selIdx.Count -eq 0) { return @() }
    $selected = @()
    foreach ($opt in $selIdx) {
        $idx = [Array]::IndexOf($options.ToArray(), $opt)
        if ($idx -ge 0) { $selected += $map[$idx] }
    }
    return $selected
}

# 对选中的供应商做深度诊断（inspect）。先列出全部供应商的去重模型一次多选，
# 再对每个 (供应商, 模型) 组合逐个跑。
function Invoke-DeepDive {
    param(
        [object[]]$Providers,
        [string]$Type,
        [string]$DBPath,
        [string]$PythonPath
    )
    $Providers = @($Providers | Where-Object { $_ })
    if ($Providers.Count -eq 0) { return }
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  深挖: $($Providers.Count) 家供应商" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan

    # 1. 收集全部选中供应商探测到的模型，去重保序
    $modelIds = [System.Collections.Generic.List[string]]::new()
    $seen = @{}
    foreach ($p in $Providers) {
        foreach ($a in @($p.attempts)) {
            if ($a.model -and -not $seen.ContainsKey($a.model)) {
                $seen[$a.model] = $true
                $modelIds.Add($a.model)
            }
        }
    }
    if ($modelIds.Count -eq 0) {
        Write-Host "  选中供应商无候选模型，跳过。" -ForegroundColor Yellow
        return
    }

    # 2. 全局模型多选（a 全选 / 空回车全选）
    Write-Host "  全部供应商探测到的模型:" -ForegroundColor Yellow
    $selModels = Select-FromList -Options $modelIds.ToArray() -Title "选择要深挖的模型（多选，a 全选）" -DefaultAll -AllLabel "全部模型"
    if ($null -eq $selModels) {
        Write-Host "  未选择模型，跳过。" -ForegroundColor Yellow
        return
    }

    # 3. 组合任务：每家供应商只测它自己探测过的、且被用户选中的模型
    #    （非笛卡尔积；避免测供应商压根没配的模型 → 无谓 403）
    $selSet = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($m in @($selModels)) { $null = $selSet.Add($m) }
    $tasks = [System.Collections.Generic.List[object]]::new()
    foreach ($p in $Providers) {
        $provModels = [System.Collections.Generic.HashSet[string]]::new()
        foreach ($a in @($p.attempts)) {
            if ($a.model) { $null = $provModels.Add($a.model) }
        }
        foreach ($m in $provModels) {
            if ($selSet.Contains($m)) {
                $tasks.Add([pscustomobject]@{ provider = $p.name; type = $p.type; model = $m })
            }
        }
    }
    if ($tasks.Count -eq 0) {
        Write-Host "  选中供应商均无选中模型，跳过。" -ForegroundColor Yellow
        return
    }
    Write-Host "将检测 $($tasks.Count) 个 (供应商, 模型) 组合:" -ForegroundColor Green
    foreach ($t in $tasks) { Write-Host "  · $($t.provider) -> $($t.model)" -ForegroundColor White }

    # 容量保护：组合过多（每个 ~30s+）会跑很久，超阈值需确认
    if ($tasks.Count -gt 20) {
        $estMin = [math]::Round($tasks.Count * 35 / 60)
        Write-Host ""
        Write-Host "⚠ 组合数较多（$($tasks.Count) 个），预计约 $estMin 分钟。" -ForegroundColor Yellow
        $confirm = Select-MenuItem -Options @("继续全部检测", "返回（缩小范围）") -Title "确认"
        if ($confirm -ne 0) { return }
    }

    # 4. 逐个跑
    foreach ($t in $tasks) {
        Write-Host ""
        Write-Host "========================================" -ForegroundColor Cyan
        Write-Host "  深挖: $($t.provider)  ($($t.type))" -ForegroundColor Cyan
        Write-Host "  Model: $($t.model)" -ForegroundColor Cyan
        Write-Host "========================================" -ForegroundColor Cyan
        $cmdArgs = [System.Collections.Generic.List[string]]::new()
        $cmdArgs.Add("inspect")
        $cmdArgs.Add("--provider"); $cmdArgs.Add($t.provider)
        $cmdArgs.Add("--model"); $cmdArgs.Add($t.model)
        $cmdArgs.Add("--source"); $cmdArgs.Add("manual")
        $cmdArgs.Add("--type"); $cmdArgs.Add($t.type)
        $cmdArgs.Add("--db"); $cmdArgs.Add($DBPath)
        $cmdArgs.Add("--timeout"); $cmdArgs.Add("30")
        $cmdArgs.Add("--workers"); $cmdArgs.Add("1")
        $cmdArgs.Add("--human")
        Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "inspect"
        $null = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
    }
}

# check 完成后的深挖入口：问是否深挖失败/可用供应商。
function Ask-DeepDive {
    param(
        [object]$CheckResult,
        [string]$Type,
        [string]$DBPath,
        [string]$PythonPath,
        [string[]]$RecheckCmdArgs = @()
    )
    if (-not $CheckResult -or -not $CheckResult.providers) { return }
    $failP = @($CheckResult.providers | Where-Object { -not $_.overall_ok })
    $okP = @($CheckResult.providers | Where-Object { $_.overall_ok })
    $total = $CheckResult.providers.Count
    $nFail = $failP.Count
    $nOk = $okP.Count
    if ($total -eq 0) { return }

    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  体检结果: $nOk 可用 / $nFail 失败（共 $total）" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan

    $choices = [System.Collections.Generic.List[string]]::new()
    $choiceData = [System.Collections.Generic.List[object]]::new()
    if ($nFail -gt 0) {
        $choices.Add("对失败的 $nFail 家供应商深挖（查根因: key/403/超时/站挂）")
        $choiceData.Add("fail")
    }
    if ($nOk -gt 0) {
        $choices.Add("对可用的 $nOk 家供应商深挖（查流式/工具/窗口/静默路由）")
        $choiceData.Add("ok")
    }
    if ($nFail -gt 0 -and $nOk -gt 0) {
        $choices.Add("两者都深挖")
        $choiceData.Add("both")
    }
    if ($RecheckCmdArgs.Count -gt 0) {
        $choices.Add("用相同配置再测一次")
        $choiceData.Add("recheck")
    }
    $choices.Add("不深挖，返回主菜单")
    $choiceData.Add("none")

    $idx = Select-MenuItem -Options $choices.ToArray() -Title "是否深挖？"
    if ($idx -lt 0) { return }
    if ($idx -eq -2 -and $script:LastLiteral) { $choice = $script:LastLiteral }
    else { $choice = $choiceData[$idx] }

    if ($choice -eq "none") { return }
    if ($choice -eq "recheck") {
        Write-Host ""
        Write-Host "  重新检测中（相同配置）…" -ForegroundColor DarkGray
        $res2 = Invoke-CcpulseJson -CmdArgs $RecheckCmdArgs
        if ($res2.Data -and $res2.Data.providers) {
            Ask-DeepDive -CheckResult $res2.Data -Type $Type -DBPath $DBPath `
                -PythonPath $PythonPath -RecheckCmdArgs $RecheckCmdArgs
        }
        return
    }
    if ($choice -eq "fail") {
        $targets = Select-DeepDiveTargets -FailProviders $failP -OkProviders @()
    } elseif ($choice -eq "ok") {
        $targets = Select-DeepDiveTargets -FailProviders @() -OkProviders $okP
    } else {
        $targets = Select-DeepDiveTargets -FailProviders $failP -OkProviders $okP
    }
    if ($null -eq $targets -or $targets.Count -eq 0) {
        Write-Host "未选择供应商，返回主菜单。" -ForegroundColor Yellow
        return
    }
    Invoke-DeepDive -Providers $targets -Type $Type -DBPath $DBPath -PythonPath $PythonPath
}

# ── [1] 健康检测 · 快速体检（读高级设置里的类型/范围，默认 claude/队列） ──
function Menu-HealthCheckQuick {
    $typeLabel = $script:AdvType
    $scopeLabel = if ($script:AdvScope -eq "all") { "全部" } else { "队列+当前" }
    if (-not (Show-Banner "健康检测 · 快速体检（$typeLabel / $scopeLabel）")) {
        Read-HostSafe "按回车返回主菜单" | Out-Null; return 1
    }
    $cmdArgs = [System.Collections.Generic.List[string]]::new()
    $cmdArgs.Add("check")
    $cmdArgs.Add("--type"); $cmdArgs.Add($script:AdvType)
    $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
    $cmdArgs.Add("--workers"); $cmdArgs.Add("8")
    $cmdArgs.Add("--timeout"); $cmdArgs.Add((Get-Timeout))
    if ($script:AdvScope -ne "all") { $cmdArgs.Add("--failover-only") }
    Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "check"
    # 深挖需要 check 的 JSON 结果；若高级设置已开 --json 则已含
    if ($cmdArgs -notcontains "--json") { $cmdArgs.Add("--json") }
    $res = Invoke-CcpulseJson -CmdArgs $cmdArgs.ToArray()
    # 深挖入口：check 完成后询问（Ask-DeepDive 内含"再测一次"，用 RecheckCmdArgs 原样重跑）
    if ($res.Data -and $res.Data.providers) {
        Ask-DeepDive -CheckResult $res.Data -Type $script:AdvType -DBPath $DB -PythonPath $Python `
            -RecheckCmdArgs $cmdArgs.ToArray()
    } else {
        Write-Host ""
        $again = Select-MenuItem -Options @("返回主菜单") -Title "下一步（无可用供应商）"
    }
    return $res.Code
}

# ── [2] 健康检测 · 自定义（选类型/范围） ─────────────────────────
function Menu-HealthCheckCustom {
    if (-not (Show-Banner "健康检测 · 自定义")) {
        Read-HostSafe "按回车返回主菜单" | Out-Null; return 1
    }
    $type = Get-AppType
    Write-Host ""
    $scopeIdx = Select-MenuItem -Options @(
        "只测故障转移队列 + 当前激活  (快)"
        "测全部供应商                   (完整)"
        "只测当前激活的 1 个供应商      (最快)"
        "自定义选择供应商               (多选)"
    ) -Title "请选择范围"
    if ($scopeIdx -lt 0) { return 1 }
    $scope = switch ($scopeIdx) { 1 { "2" } 2 { "3" } 3 { "4" } default { "1" } }

    $selectedProviderArg = ""
    if ($scope -eq "4") {
        Write-Host ""
        Write-Host "正在拉取供应商列表…" -ForegroundColor DarkGray
        $names = @()
        try {
            $rawJson = & $Python -u $MainScript list-models --type $type --db $DB --workers 6 --timeout 20 --json 2>$null
            $parsed = ($rawJson -join "`n") | ConvertFrom-Json
            $names = @($parsed.providers | ForEach-Object { $_.name })
        } catch {
            $names = @()
        }
        if ($names.Count -gt 0) {
            $selNames = Select-FromList -Options $names -Title "选择要检测的供应商" -AllowLiteral -AllLabel "全部供应商"
            if ($null -eq $selNames) {
                Write-Host "已取消。" -ForegroundColor Yellow
                Read-HostSafe "按回车" | Out-Null; return 1
            }
            $selectedProviderArg = $selNames -join ","
        } else {
            Write-Host "未能拉取供应商列表，请手动输入。" -ForegroundColor Yellow
            $selectedProviderArg = Read-Host "  供应商名（逗号分隔）"
        }
        if ([string]::IsNullOrWhiteSpace($selectedProviderArg)) {
            Write-Host "未选择供应商，返回主菜单。" -ForegroundColor Yellow
            Read-HostSafe "按回车" | Out-Null; return 1
        }
    }

    $cmdArgs = [System.Collections.Generic.List[string]]::new()
    $cmdArgs.Add("check")
    $cmdArgs.Add("--type"); $cmdArgs.Add($type)
    $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
    $cmdArgs.Add("--workers"); $cmdArgs.Add("8")
    $cmdArgs.Add("--timeout"); $cmdArgs.Add((Get-Timeout))
    if ($scope -eq "3") { $cmdArgs.Add("--current-only") }
    elseif ($scope -eq "4") {
        $cmdArgs.Add("--provider"); $cmdArgs.Add($selectedProviderArg)
    }
    elseif ($scope -ne "2") { $cmdArgs.Add("--failover-only") }
    Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "check"
    if ($cmdArgs -notcontains "--json") { $cmdArgs.Add("--json") }
    $res = Invoke-CcpulseJson -CmdArgs $cmdArgs.ToArray()
    # 深挖入口：check 完成后询问（Ask-DeepDive 内含"再测一次"，用 RecheckCmdArgs 原样重跑）
    if ($res.Data -and $res.Data.providers) {
        Ask-DeepDive -CheckResult $res.Data -Type $type -DBPath $DB -PythonPath $Python `
            -RecheckCmdArgs $cmdArgs.ToArray()
    } else {
        Write-Host ""
        $again = Select-MenuItem -Options @("返回主菜单", "重新选择（类型/范围）") -Title "下一步（无可用供应商）"
        if ($again -eq 1) { return (Menu-HealthCheckCustom) }
    }
    return $res.Code
}

# ── 体检入口（合并快速体检/自定义） ───────────────────────────────────
function Menu-HealthCheck {
    $mode = Select-MenuItem -Options @(
        "快速体检  用高级设置（$($script:AdvType)/$($script:AdvScope)）一键跑"
        "自定义     选类型/范围"
    ) -Title "体检模式"
    if ($mode -lt 0) { return 1 }
    if ($mode -eq 0) { return Menu-HealthCheckQuick }
    return Menu-HealthCheckCustom
}

# ── [3] 拉模型列表 ──────────────────────────────────────────────
function Menu-ListModels {
    if (-not (Show-Banner "拉取供应商 /v1/models 模型目录")) {
        Read-HostSafe "按回车返回主菜单" | Out-Null; return 1
    }
    $type = Get-AppType
    Write-Host ""
    $scopeIdx = Select-MenuItem -Options @(
        "故障转移队列 + 当前激活"
        "全部供应商"
    ) -Title "请选择范围"
    if ($scopeIdx -lt 0) { return 1 }
    $scope = if ($scopeIdx -eq 1) { "2" } else { "1" }
    Write-Host ""
    $probeIdx = Select-MenuItem -Options @(
        "只拉列表（默认，最快）"
        "轻量探测每个模型（2+3 题）"
        "深度探测（text/streaming/metadata/thinking/tools）"
    ) -Title "探测模式"
    if ($probeIdx -lt 0) { return 1 }
    $probeMode = switch ($probeIdx) { 1 { "2" } 2 { "3" } default { "1" } }
    $src = "listed"
    if ($probeMode -eq "2" -or $probeMode -eq "3") {
        $srcIdx = Select-MenuItem -Options @(
            "listed     - /v1/models 列表（默认）"
            "configured - cc-switch 配置档位"
            "both       - 合并去重"
        ) -Title "探测哪些模型"
        if ($srcIdx -lt 0) { return 1 }
        $src = switch ($srcIdx) { 1 { "configured" } 2 { "both" } default { "listed" } }
    }
    $lmTimeout = if ($probeMode -eq "3") { "60" } else { "30" }

    $cmdArgs = [System.Collections.Generic.List[string]]::new()
    $cmdArgs.Add("list-models")
    $cmdArgs.Add("--type"); $cmdArgs.Add($type)
    $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
    $cmdArgs.Add("--workers"); $cmdArgs.Add("6")
    $cmdArgs.Add("--timeout"); $cmdArgs.Add($lmTimeout)
    if ($scope -ne "2") { $cmdArgs.Add("--failover-only") }
    if ($probeMode -eq "2") { $cmdArgs.Add("--probe"); $cmdArgs.Add("--source"); $cmdArgs.Add($src) }
    elseif ($probeMode -eq "3") { $cmdArgs.Add("--deep"); $cmdArgs.Add("--source"); $cmdArgs.Add($src) }
    Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "list-models"
    $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
    Write-Host ""
    $again = Select-MenuItem -Options @("返回主菜单", "重新选择（类型/范围/探测模式）") -Title "下一步"
    if ($again -eq 1) { return (Menu-ListModels) }
    return $code
}

# ── [4] 深度诊断 inspect（精简：type → provider → model） ─────
function Menu-Inspect {
    if (-not (Show-Banner "深度诊断 (inspect)")) {
        Read-HostSafe "按回车返回主菜单" | Out-Null; return 1
    }
    $type = Get-AppType
    Write-Host ""

    Write-Host "[1/2] 选择供应商" -ForegroundColor Yellow
    Write-Host "正在拉取供应商列表…" -ForegroundColor DarkGray
    $names = @()
    try {
        $rawJson = & $Python -u $MainScript list-models --type $type --db $DB --workers 6 --timeout 20 --json 2>$null
        $parsed = ($rawJson -join "`n") | ConvertFrom-Json
        $names = @($parsed.providers | ForEach-Object { $_.name })
    } catch {
        $names = @()
    }
    $provider = ""
    $providers = @()   # 多选时存供应商名数组
    $multiProvider = $false
    if ($names.Count -gt 0) {
        $selNames = Select-FromList -Options $names -Title "选择供应商" -AllowLiteral -AllLabel "全部供应商"
        if ($null -eq $selNames) {
            Write-Host "已取消。" -ForegroundColor Yellow
            Read-HostSafe "按回车" | Out-Null; return 1
        }
        $providers = @($selNames | Select-Object -Unique)
        $provider = $providers -join ","
        $multiProvider = ($providers.Count -gt 1)
    } else {
        Write-Host "未能拉取供应商列表，请手动输入。" -ForegroundColor Yellow
        $provider = Read-Host "  供应商名（逗号分隔多选）"
        $providers = @($provider -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        $multiProvider = ($providers.Count -gt 1)
    }
    if ($providers.Count -eq 0 -or [string]::IsNullOrWhiteSpace($provider)) {
        Write-Host "未提供供应商名，返回主菜单。" -ForegroundColor Yellow
        Read-HostSafe "按回车" | Out-Null; return 1
    }
    Write-Host ""

    # 多供应商分支：选档位 -> 对每家取该档位模型 fan-out inspect
    if ($multiProvider) {
        Write-Host "多供应商已选: $($providers.Count) 家" -ForegroundColor Green
        $tierOptions = @("haiku （默认）", "sonnet", "opus", "fable", "default")
        $tierIdxs = if ([Console]::IsInputRedirected) {
            $idx = Select-MenuItem -Options $tierOptions -Title "选择档位（从每家配置中取对应模型）"
            if ($idx -lt 0) { $null } else { @($idx) }
        } else {
            Show-ArrowSelect -Options $tierOptions -Multi -Title "选择档位（从每家配置中取对应模型）"
        }
        if ($null -eq $tierIdxs) { return 1 }
        # 空回车（-1 sentinel）→ 按默认 haiku 处理
        if ($tierIdxs.Count -eq 1 -and $tierIdxs[0] -eq -1) { $tierIdxs = @() }
        $tierMap = @("haiku", "sonnet", "opus", "fable", "default")
        $selectedTiers = @()
        foreach ($i in $tierIdxs) { $selectedTiers += $tierMap[$i] }
        if ($selectedTiers.Count -eq 0) { $selectedTiers = @("haiku") }
        Write-Host "已选档位: $($selectedTiers -join ', ')" -ForegroundColor Green
        Write-Host ""

        # 对每家供应商，取所选档位的 model id（跳过未配置该档位的供应商）
        $tasks = @()
        foreach ($pn in $providers) {
            $curProv = if ($parsed) { $parsed.providers | Where-Object { $_.name -eq $pn } | Select-Object -First 1 } else { $null }
            if (-not $curProv) {
                Write-Host "  跳过 [$pn]: 未找到配置" -ForegroundColor Yellow
                continue
            }
            foreach ($tier in $selectedTiers) {
                $cm = @($curProv.configured_models) | Where-Object { $_.tier -eq $tier } | Select-Object -First 1
                if ($cm -and $cm.model) {
                    $tasks += [pscustomobject]@{ provider=$pn; model=$cm.model; tier=$tier }
                } else {
                    Write-Host "  跳过 [$pn] [$tier 档位]: 未配置" -ForegroundColor DarkGray
                }
            }
        }
        if ($tasks.Count -eq 0) {
            Write-Host "所选供应商均无对应档位模型，返回主菜单。" -ForegroundColor Yellow
            Read-HostSafe "按回车" | Out-Null; return 1
        }
        Write-Host "将检测 $($tasks.Count) 个 (供应商, 档位) 组合:" -ForegroundColor Green
        foreach ($t in $tasks) { Write-Host "  · $($t.provider) [$($t.tier)] -> $($t.model)" -ForegroundColor White }
        Write-Host ""

        $overallCode = 0
        $taskTotal = $tasks.Count
        $taskIdx = 0
        foreach ($t in $tasks) {
            $taskIdx++
            Write-Host ""
            Write-Host "========================================" -ForegroundColor Cyan
            Write-Host "  第 $taskIdx/$taskTotal 家: $($t.provider) [$($t.tier)] $($t.model)" -ForegroundColor Cyan
            Write-Host "========================================" -ForegroundColor Cyan
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("inspect")
            $cmdArgs.Add("--provider"); $cmdArgs.Add($t.provider)
            $cmdArgs.Add("--model"); $cmdArgs.Add($t.model)
            $cmdArgs.Add("--source"); $cmdArgs.Add("manual")
            $cmdArgs.Add("--type"); $cmdArgs.Add($type)
            $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--timeout"); $cmdArgs.Add("30")
            $cmdArgs.Add("--workers"); $cmdArgs.Add("1")
            $cmdArgs.Add("--human")
            Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "inspect"
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
            if ($code -ne 0 -and $overallCode -eq 0) { $overallCode = $code }
        }
        Write-Host ""
        $again = Select-MenuItem -Options @("返回主菜单", "重新选择（重走 type -> provider -> tier）") -Title "下一步"
        if ($again -eq 1) { return (Menu-Inspect) }
        return $overallCode
    }

    $modeIdx = Select-MenuItem -Options @(
        "单一模型（默认）"
        "批量检测该供应商的所有模型"
        "自定义选择模型 + 检测维度"
    ) -Title "检测模式"
    if ($modeIdx -lt 0) { return 1 }
    $batchMode = ($modeIdx -eq 1)
    $customMode = ($modeIdx -eq 2)

    if ($customMode) {
        Write-Host ""
        Write-Host "可用模型列表:" -ForegroundColor Yellow
        $curProv = if ($parsed) { $parsed.providers | Where-Object { $_.name -eq $provider } | Select-Object -First 1 } else { $null }
        $modelChoices = [System.Collections.Generic.List[object]]::new()
        $seen = @{}
        if ($curProv) {
            foreach ($cm in @($curProv.configured_models)) {
                if ($cm -and $cm.model -and -not $seen.ContainsKey($cm.model)) {
                    $seen[$cm.model] = $true
                    $modelChoices.Add([pscustomobject]@{ id = $cm.model; label = "[$($cm.tier) 档位]" })
                }
            }
            foreach ($mid in @($curProv.models)) {
                if ($mid -and -not $seen.ContainsKey($mid)) {
                    $seen[$mid] = $true
                    $modelChoices.Add([pscustomobject]@{ id = $mid; label = "" })
                }
            }
        }
        if ($modelChoices.Count -eq 0) {
            Write-Host "  （无可用模型，请手动输入）" -ForegroundColor Yellow
            $modelInput = Read-Host "  模型 ID（逗号分隔）"
            $selectedModels = $modelInput
        } else {
            $dispLabels = @()
            foreach ($mc in $modelChoices) {
                $lab = if ($mc.label) { "  $($mc.label)" } else { "" }
                $dispLabels += "$($mc.id)$lab"
            }
            $selNames = Select-FromList -Options $dispLabels -Title "选择模型" -AllLabel "全部模型"
            if ($null -eq $selNames) {
                Write-Host "已取消。" -ForegroundColor Yellow
                Read-HostSafe "按回车" | Out-Null; return 1
            }
            if ($selNames.Count -eq 0) { $selectedModels = $modelChoices[0].id }
            else {
                # selNames 是带 label 的完整字符串，需要映射回 modelChoices
                $selected = @()
                foreach ($nm in $selNames) {
                    $matched = $modelChoices | Where-Object {
                        $disp = $_.id + $(if ($_.label) { "  $($_.label)" } else { "" })
                        $disp -eq $nm
                    } | Select-Object -First 1
                    if ($matched) { $selected += $matched.id }
                }
                $selectedModels = ($selected | Select-Object -Unique) -join ","
            }
        }

        Write-Host ""
        $dimOptions = @(
            "text              文本探测"
            "streaming         流式探测"
            "model-consistency 模型路由比对"
            "metadata          元数据"
            "thinking          Thinking 能力"
            "tools             Tool use"
            "vision            视觉能力"
        )
        $dimIdxs = if ([Console]::IsInputRedirected) {
            $idx = Select-MenuItem -Options $dimOptions -Title "检测维度（默认全选）"
            if ($idx -lt 0) { $null } elseif ($null -eq $script:LastInput -or [string]::IsNullOrWhiteSpace($script:LastInput)) { @() } else { @($idx) }
        } else {
            Show-ArrowSelect -Options $dimOptions -Multi -Title "检测维度（全选=直接回车，单独选用空格）"
        }
        if ($null -eq $dimIdxs) { return 1 }
        # 空回车（-1 sentinel）→ 按默认全开处理
        if ($dimIdxs.Count -eq 1 -and $dimIdxs[0] -eq -1) { $dimIdxs = @() }
        $dimMap = @("text", "streaming", "model-consistency", "metadata", "thinking", "tools", "vision")
        if ($dimIdxs.Count -eq 0) {
            # 默认全开（vision 除外，除非高级设置开了，由 Apply-AdvancedArgs 追加）
            $selected = @("text", "streaming", "model-consistency", "protocol", "error-classification", "metadata", "thinking", "tools")
        } else {
            $selected = @()
            foreach ($i in $dimIdxs) { $selected += $dimMap[$i] }
            $selected += "protocol", "error-classification"
        }
        $include = $selected -join ","

        Write-Host ""
        Write-Host "已选模型: $selectedModels" -ForegroundColor Cyan
        Write-Host "已选维度: $include" -ForegroundColor Cyan
        Write-Host ""

        $cmdArgs = [System.Collections.Generic.List[string]]::new()
        $cmdArgs.Add("inspect")
        $cmdArgs.Add("--provider"); $cmdArgs.Add($provider)
        $cmdArgs.Add("--models"); $cmdArgs.Add($selectedModels)
        $cmdArgs.Add("--include"); $cmdArgs.Add($include)
        $cmdArgs.Add("--source"); $cmdArgs.Add("manual")
        $cmdArgs.Add("--type"); $cmdArgs.Add($type)
        $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
        $cmdArgs.Add("--timeout"); $cmdArgs.Add("30")
        $cmdArgs.Add("--workers"); $cmdArgs.Add("1")
        $cmdArgs.Add("--human")
        $cmdArgs.Add("--probe-delay"); $cmdArgs.Add("3")
        $cmdArgs.Add("--max-retries"); $cmdArgs.Add("1")
        Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "inspect"

        $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        Write-Host ""
        $again = Select-MenuItem -Options @("返回主菜单", "重新选择（重走 type → provider → 模式）") -Title "下一步"
        if ($again -eq 1) { return (Menu-Inspect) }
        return $code
    }

    if ($batchMode) {
        $srcIdx = Select-MenuItem -Options @(
            "configured - cc-switch 配置档位（默认）"
            "listed     - 供应商 /v1/models 声明"
        ) -Title "批量检测范围"
        if ($srcIdx -lt 0) { return 1 }
        $source = switch ($srcIdx) { 1 { "listed" } default { "configured" } }
        Write-Host ""

        $cmdArgs = [System.Collections.Generic.List[string]]::new()
        $cmdArgs.Add("inspect")
        $cmdArgs.Add("--provider"); $cmdArgs.Add($provider)
        $cmdArgs.Add("--all-models")
        $cmdArgs.Add("--source"); $cmdArgs.Add($source)
        $cmdArgs.Add("--type"); $cmdArgs.Add($type)
        $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
        $cmdArgs.Add("--timeout"); $cmdArgs.Add("30")
        $cmdArgs.Add("--workers"); $cmdArgs.Add("1")
        $cmdArgs.Add("--human")
        $cmdArgs.Add("--probe-delay"); $cmdArgs.Add("3")
        $cmdArgs.Add("--max-retries"); $cmdArgs.Add("1")
        Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "inspect"

        $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        Write-Host ""
        $again = Select-MenuItem -Options @("返回主菜单", "重新选择（重走 type → provider → 模式）") -Title "下一步"
        if ($again -eq 1) { return (Menu-Inspect) }
        return $code
    }

    Write-Host "[2/2] 选择模型（配置档位优先，其余来自 /v1/models）" -ForegroundColor Yellow
    $curProv = if ($parsed) { $parsed.providers | Where-Object { $_.name -eq $provider } | Select-Object -First 1 } else { $null }
    $modelChoices = [System.Collections.Generic.List[object]]::new()
    $seen = @{}
    if ($curProv) {
        foreach ($cm in @($curProv.configured_models)) {
            if ($cm -and $cm.model -and -not $seen.ContainsKey($cm.model)) {
                $seen[$cm.model] = $true
                $modelChoices.Add([pscustomobject]@{ id = $cm.model; label = "[$($cm.tier) 档位]" })
            }
        }
        foreach ($mid in @($curProv.models)) {
            if ($mid -and -not $seen.ContainsKey($mid)) {
                $seen[$mid] = $true
                $modelChoices.Add([pscustomobject]@{ id = $mid; label = "" })
            }
        }
    }
    if ($modelChoices.Count -gt 0) {
        $dispLabels = @()
        foreach ($mc in $modelChoices) {
            $lab = if ($mc.label) { "  $($mc.label)" } else { "" }
            $dispLabels += "$($mc.id)$lab"
        }
        $modelIdx = Select-MenuItem -Options $dispLabels -Title "选择模型（配置档位优先）" -Prompt "输入序号或模型 ID"
        if ($modelIdx -eq -1) {
            Write-Host "已取消。" -ForegroundColor Yellow
            Read-HostSafe "按回车" | Out-Null; return 1
        } elseif ($modelIdx -eq -2 -and $script:LastLiteral) {
            # 管道模式：字面模型 ID
            $model = $script:LastLiteral
        } else {
            $model = $modelChoices[$modelIdx].id
        }
    } else {
        Write-Host "  （未获取到模型列表，请手动输入）" -ForegroundColor Yellow
        $model = Read-Host "  模型 ID"
    }
    if ([string]::IsNullOrWhiteSpace($model)) {
        Write-Host "未提供模型 ID，返回主菜单。" -ForegroundColor Yellow
        Read-HostSafe "按回车" | Out-Null; return 1
    }
    $keepSuffix = $false
    if ($model -match '\[.+\]$') {
        $k = Read-Host "  模型 ID 含 [1M] 后缀，保留？(y/N)"
        if ($k -eq "y" -or $k -eq "Y") { $keepSuffix = $true }
    }
    $source = "manual"
    Write-Host ""

    $cmdArgs = [System.Collections.Generic.List[string]]::new()
    $cmdArgs.Add("inspect")
    $cmdArgs.Add("--provider"); $cmdArgs.Add($provider)
    $cmdArgs.Add("--model"); $cmdArgs.Add($model)
    $cmdArgs.Add("--source"); $cmdArgs.Add($source)
    $cmdArgs.Add("--type"); $cmdArgs.Add($type)
    $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
    $cmdArgs.Add("--timeout"); $cmdArgs.Add("30")
    $cmdArgs.Add("--workers"); $cmdArgs.Add("1")
    $cmdArgs.Add("--human")   # 默认人类可读输出
    if ($keepSuffix) { $cmdArgs.Add("--keep-suffix") }
    Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "inspect"

    $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
    Write-Host ""
    $again = Select-MenuItem -Options @("返回主菜单", "重新选择（重走 type → provider → model）") -Title "下一步"
    if ($again -eq 1) { return (Menu-Inspect) }
    return $code
}

# ── 深度诊断入口（合并拉模型/inspect） ───────────────────────────────────
function Menu-DeepDiag {
    $mode = Select-MenuItem -Options @(
        "拉模型列表    GET /v1/models 目录"
        "深度诊断      inspect 单一/多/对比 (provider, model)"
        "深度保真鉴别  inspect --auth-full 一键全开"
    ) -Title "深度诊断"
    if ($mode -lt 0) { return 1 }
    if ($mode -eq 0) { return Menu-ListModels }
    if ($mode -eq 1) { return Menu-Inspect }
    $oldAuthFull = $script:AdvAuthFull
    try {
        $script:AdvAuthFull = $true
        return Menu-Inspect
    } finally {
        $script:AdvAuthFull = $oldAuthFull
    }
}

# ── [5] 运行日志（只读 cc-switch 历史） ──────────────────────────
function Menu-Logs {
    if (-not (Show-Banner "运行日志 · 只读 cc-switch proxy 日志")) {
        Read-HostSafe "按回车返回主菜单" | Out-Null; return 1
    }
    $logOptions = @(
        "最近失败日志        history --fails"
        "最近全部日志        history"
        "供应商统计          stats --since 7d"
        "静默路由排行        routing --since 7d"
        "实时监控（轮询）    watch · 有新日志就打印"
        "分析报表            analyze · 按天/模型/供应商交叉"
        "返回主菜单"
    )
    $idx = Select-MenuItem -Options $logOptions -Title "请选择"
    if ($idx -lt 0) { return 1 }
    $code = 0
    switch ($idx) {
        1 {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("history"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--limit"); $cmdArgs.Add("30")
            Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "history"
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        2 {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("stats"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--since"); $cmdArgs.Add("7d")
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        3 {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("routing"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--since"); $cmdArgs.Add("7d"); $cmdArgs.Add("--limit"); $cmdArgs.Add("20")
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        4 {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("watch"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--interval"); $cmdArgs.Add("3")
            Write-Host "实时监控中，Ctrl+C 结束…" -ForegroundColor Cyan
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        5 {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("analyze"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--since"); $cmdArgs.Add("7d")
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        6 { return 0 }
        default {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("history"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--fails"); $cmdArgs.Add("--limit"); $cmdArgs.Add("30")
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
    }
    Write-Host ""
    $again = Select-MenuItem -Options @("返回主菜单", "重新选择日志子项") -Title "下一步"
    if ($again -eq 1) { return (Menu-Logs) }
    return $code
}

# ── [6] 高级设置（进程内有效） ───────────────────────────────────
function Menu-AdvancedSettings {
    Show-Banner "高级设置（本进程有效，重开需重设）" | Out-Null
    # 编号清单循环：箭头选择项号，ESC/末项返回主菜单。避免改一项须回车 9 次。
    while ($true) {
        $opts = @(
            "JSON 输出         [check]        $(if ($script:AdvJson) {'开'} else {'关（默认）'})"
            "probe-max-tokens  [check/inspect/list] $(if ($script:AdvMaxTokens) {$script:AdvMaxTokens} else {'1024（默认）'})"
            "允许 thinking     [check/inspect] $(if ($script:AdvEnableThinking) {'开'} else {'关（默认）'})"
            "user-agent        [全部子命令]   $(if ($script:AdvUserAgent) {$script:AdvUserAgent} else {'本机版本（默认）'})"
            "上下文档位        [inspect]      $($script:AdvProbeContext)（无声明时冒烟）"
            "vision 探测       [inspect]      $(if ($script:AdvVision) {'开'} else {'关（默认）'})"
            "保真·缓存回放     [inspect]      $(if ($script:AdvAuthCacheReplay) {'开'} else {'关（默认）'})"
            "保真·知识截止     [inspect]      $(if ($script:AdvAuthKnowledgeCutoff) {'开'} else {'关（默认）'})"
            "保真·分布指纹     [inspect]      $(if ($script:AdvAuthJsFingerprint) {"$($script:AdvJsSamples)次"} else {'关（默认）'})"
            "stealth 隐身      [check]        $(if ($script:AdvStealth) {'开'} else {'关（默认）'})"
            "快速体检类型      [快速体检]     $($script:AdvType)"
            "快速体检范围      [快速体检]     $(if ($script:AdvScope -eq 'all') {'全部'} else {'队列+当前（默认）'})"
            "返回主菜单"
        )
        Write-Host "当前设置（箭头选择修改，ESC 或选末项返回主菜单）:" -ForegroundColor Yellow
        $pick = Select-MenuItem -Options $opts -Title "高级设置" -Prompt "输入 1-13"
        if ($pick -lt 0 -or $pick -eq 12) { return }
        switch ($pick) {
            0 {
                $j = Read-HostSafe "JSON 输出？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($j)) { $script:AdvJson = ($j -eq "y" -or $j -eq "Y") }
            }
            1 {
                $mt = Read-HostSafe "probe-max-tokens（留空=1024；thinking 模型可调高）"
                if (-not [string]::IsNullOrWhiteSpace($mt)) { $script:AdvMaxTokens = $mt }
            }
            2 {
                $th = Read-HostSafe "允许 thinking？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($th)) { $script:AdvEnableThinking = ($th -eq "y" -or $th -eq "Y") }
            }
            3 {
                $ua = Read-HostSafe "user-agent 覆盖（留空=本机 claude 版本）"
                if (-not [string]::IsNullOrWhiteSpace($ua)) { $script:AdvUserAgent = $ua }
            }
            4 {
                $cxIdx = Select-MenuItem -Options @("512k", "1m") -Title "上下文档位（默认 512k）"
                if ($cxIdx -ge 0) { $script:AdvProbeContext = switch ($cxIdx) { 1 { "1m" } default { "512k" } } }
            }
            5 {
                $vi = Read-HostSafe "inspect 开启 vision？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($vi)) { $script:AdvVision = ($vi -eq "y" -or $vi -eq "Y") }
            }
            6 {
                $cr = Read-HostSafe "inspect 开启保真·缓存回放探针？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($cr)) { $script:AdvAuthCacheReplay = ($cr -eq "y" -or $cr -eq "Y") }
            }
            7 {
                $kc = Read-HostSafe "inspect 开启保真·知识截止探针？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($kc)) { $script:AdvAuthKnowledgeCutoff = ($kc -eq "y" -or $kc -eq "Y") }
            }
            8 {
                $js = Read-HostSafe "inspect 开启保真·分布指纹探针？(y/N，可填采样次数)"
                $jsTrim = "$js".Trim()
                if ([string]::IsNullOrWhiteSpace($jsTrim)) { $script:AdvAuthJsFingerprint = $false; $script:AdvJsSamples = "" }
                elseif ($jsTrim -eq "y" -or $jsTrim -eq "Y") { $script:AdvAuthJsFingerprint = $true; $script:AdvJsSamples = "" }
                else {
                    $script:AdvAuthJsFingerprint = $true
                    $script:AdvJsSamples = $jsTrim
                }
            }
            9 {
                $st = Read-HostSafe "check 开启 stealth 隐身？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($st)) { $script:AdvStealth = ($st -eq "y" -or $st -eq "Y") }
            }
            10 {
                $tyIdx = Select-MenuItem -Options @("claude(默认)", "codex", "openclaw", "all") -Title "快速体检类型"
                if ($tyIdx -ge 0) { $script:AdvType = switch ($tyIdx) { 1 { "codex" } 2 { "openclaw" } 3 { "all" } default { "claude" } } }
            }
            11 {
                $scIdx = Select-MenuItem -Options @("队列+当前(默认,快)", "全部(完整)") -Title "快速体检范围"
                if ($scIdx -ge 0) { $script:AdvScope = if ($scIdx -eq 1) { "all" } else { "failover" } }
            }
        }
    }
}

# ── 主菜单循环 ──────────────────────────────────────────────────
function Show-MainMenu {
    if (-not (Show-Banner)) {
        Read-HostSafe "按回车关闭" | Out-Null
        return -1
    }
    $menuOptions = @(
        "体检              快速/自定义，检测完可深挖"
        "深度诊断          拉模型列表/inspect"
        "运行日志          失败/统计/路由/实时监控"
        "高级设置          JSON/stealth/thinking/UA"
        "退出"
    )
    Write-Host "一键检查 AI 模型服务是否正常 —— 回车开始体检" -ForegroundColor Green
    return Select-MenuItem -Options $menuOptions -Title "请选择操作"
}

$script:LastMenuCode = 0
while ($true) {
    $idx = Show-MainMenu
    if ($idx -eq -1) { exit 0 }  # ESC 或数据库不存在
    $menuCode = $null
    switch ($idx) {
        0 { $menuCode = Menu-HealthCheck }
        1 { $menuCode = Menu-DeepDiag }
        2 { $menuCode = Menu-Logs }
        3 { $menuCode = Menu-AdvancedSettings }
        4 { exit 0 }
        default { continue }
    }
    if ($null -ne $menuCode) { $script:LastMenuCode = [int]$menuCode }
}

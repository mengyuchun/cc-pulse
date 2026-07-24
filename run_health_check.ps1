#Requires -Version 7
# CC-Pulse - cc-switch 供应商健康检测与单模型深度诊断 · 桌面启动器
# 双击运行：层次化菜单（快速体检 / 自定义 / 拉模型 / 深度诊断 / 高级设置 / 退出）

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

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
    Read-Host "按回车关闭"
    exit 2
}
$MainScript = Join-Path $ScriptDir "check_ccswitch_health.py"
if (-not (Test-Path $MainScript)) {
    Write-Host "未找到主脚本: $MainScript" -ForegroundColor Red
    Read-Host "按回车关闭"
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
    Write-Host "请选择要检测的供应商类型:" -ForegroundColor Yellow
    Write-Host "  [1] claude (默认)" -ForegroundColor White
    Write-Host "  [2] codex"
    Write-Host "  [3] openclaw"
    Write-Host "  [4] all"
    $c = Read-Host "输入 1-4 (默认1)"
    switch ($c) {
        "2" { return "codex" }
        "3" { return "openclaw" }
        "4" { return "all" }
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
    # --probe-max-tokens / --probe-enable-thinking 只有 check 和 inspect 用得到
    $probeCmds = @("check", "inspect")
    if ($probeCmds -contains $SubCommand) {
        if (-not [string]::IsNullOrWhiteSpace($script:AdvMaxTokens)) {
            $CmdArgs.Add("--probe-max-tokens"); $CmdArgs.Add($script:AdvMaxTokens)
        }
        if ($script:AdvEnableThinking) { $CmdArgs.Add("--probe-enable-thinking") }
    }
    # inspect 专属：上下文档位 + 可选 vision
    if ($SubCommand -eq "inspect") {
        if (-not [string]::IsNullOrWhiteSpace($script:AdvProbeContext)) {
            $CmdArgs.Add("--probe-context"); $CmdArgs.Add($script:AdvProbeContext)
        }
        if ($script:AdvVision) {
            # 在默认 include 基础上追加 vision（CLI 默认不含 vision）
            $CmdArgs.Add("--include")
            $CmdArgs.Add("text,streaming,model-consistency,protocol,error-classification,metadata,thinking,tools,vision")
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
    return $code
}

function Get-Timeout {
    if ($env:CC_PULSE_TIMEOUT) { return $env:CC_PULSE_TIMEOUT } else { return "45" }
}

# ── [1] 健康检测 · 快速体检（零子提示） ──────────────────────────
function Menu-HealthCheckQuick {
    if (-not (Show-Banner "健康检测 · 快速体检（claude / 故障转移队列）")) {
        Read-Host "按回车返回主菜单"; return 1
    }
    $cmdArgs = [System.Collections.Generic.List[string]]::new()
    $cmdArgs.Add("check")
    $cmdArgs.Add("--type"); $cmdArgs.Add("claude")
    $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
    $cmdArgs.Add("--workers"); $cmdArgs.Add("8")
    $cmdArgs.Add("--timeout"); $cmdArgs.Add((Get-Timeout))
    $cmdArgs.Add("--failover-only")
    Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "check"
    $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
    Read-Host "按回车返回主菜单"
    return $code
}

# ── [2] 健康检测 · 自定义（选类型/范围） ─────────────────────────
function Menu-HealthCheckCustom {
    if (-not (Show-Banner "健康检测 · 自定义")) {
        Read-Host "按回车返回主菜单"; return 1
    }
    $type = Get-AppType
    Write-Host ""
    Write-Host "请选择范围:" -ForegroundColor Yellow
    Write-Host "  [1] 只测故障转移队列 + 当前激活  (快)" -ForegroundColor White
    Write-Host "  [2] 测全部供应商                   (完整)" -ForegroundColor White
    $scope = Read-Host "输入 1-2 (默认1)"

    $cmdArgs = [System.Collections.Generic.List[string]]::new()
    $cmdArgs.Add("check")
    $cmdArgs.Add("--type"); $cmdArgs.Add($type)
    $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
    $cmdArgs.Add("--workers"); $cmdArgs.Add("8")
    $cmdArgs.Add("--timeout"); $cmdArgs.Add((Get-Timeout))
    if ($scope -ne "2") { $cmdArgs.Add("--failover-only") }
    Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "check"
    $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
    Read-Host "按回车返回主菜单"
    return $code
}

# ── [3] 拉模型列表 ──────────────────────────────────────────────
function Menu-ListModels {
    if (-not (Show-Banner "拉取供应商 /v1/models 模型目录")) {
        Read-Host "按回车返回主菜单"; return 1
    }
    $type = Get-AppType
    Write-Host ""
    Write-Host "请选择范围:" -ForegroundColor Yellow
    Write-Host "  [1] 故障转移队列 + 当前激活"
    Write-Host "  [2] 全部供应商"
    $scope = Read-Host "输入 1-2 (默认1)"

    $cmdArgs = [System.Collections.Generic.List[string]]::new()
    $cmdArgs.Add("list-models")
    $cmdArgs.Add("--type"); $cmdArgs.Add($type)
    $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
    $cmdArgs.Add("--workers"); $cmdArgs.Add("6")
    $cmdArgs.Add("--timeout"); $cmdArgs.Add("30")
    if ($scope -ne "2") { $cmdArgs.Add("--failover-only") }
    Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "list-models"
    $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
    Read-Host "按回车返回主菜单"
    return $code
}

# ── [4] 深度诊断 inspect（精简：type/provider/source/model） ─────
function Menu-Inspect {
    if (-not (Show-Banner "深度诊断 (inspect)")) {
        Read-Host "按回车返回主菜单"; return 1
    }
    $type = Get-AppType
    Write-Host ""

    Write-Host "[1/3] 选择供应商" -ForegroundColor Yellow
    Write-Host "  [L] 列出当前所有供应商（按 type=$type）" -ForegroundColor White
    Write-Host "  [M] 手动输入供应商名" -ForegroundColor White
    $provChoice = Read-Host "输入 L 或 M（默认 L）"
    if ($provChoice -eq "M" -or $provChoice -eq "m") {
        $provider = Read-Host "  供应商名（与 cc-switch 中一致）"
    } else {
        $la = @("list-models", "--type", $type, "--db", $DB,
                "--workers", "6", "--timeout", "20", "--failover-only")
        Invoke-Ccpulse -CmdArgs $la | Out-Null
        Write-Host ""
        $provider = Read-Host "  供应商名（从上面列表复制）"
    }
    if ([string]::IsNullOrWhiteSpace($provider)) {
        Write-Host "未提供供应商名，返回主菜单。" -ForegroundColor Yellow
        Read-Host "按回车"; return 1
    }
    Write-Host ""

    Write-Host "[2/3] 选择模型来源" -ForegroundColor Yellow
    Write-Host "  [1] configured  - cc-switch 已配置的模型档位（默认）" -ForegroundColor White
    Write-Host "  [2] listed      - 先拉 /v1/models 再查找"
    Write-Host "  [3] manual      - 强制使用下面的 model id"
    $sc = Read-Host "输入 1-3 (默认1)"
    $source = switch ($sc) { "2" { "listed" } "3" { "manual" } default { "configured" } }
    Write-Host ""

    Write-Host "[3/3] 输入模型 ID" -ForegroundColor Yellow
    Write-Host "  例如: claude-sonnet-4-5 / claude-haiku-4-5 / gpt-5" -ForegroundColor Gray
    $model = Read-Host "  模型 ID"
    if ([string]::IsNullOrWhiteSpace($model)) {
        Write-Host "未提供模型 ID，返回主菜单。" -ForegroundColor Yellow
        Read-Host "按回车"; return 1
    }
    $keepSuffix = $false
    if ($model -match '\[.+\]$') {
        $k = Read-Host "  模型 ID 含 [1M] 后缀，保留？(y/N)"
        if ($k -eq "y" -or $k -eq "Y") { $keepSuffix = $true }
    }
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
    Read-Host "按回车返回主菜单"
    return $code
}

# ── [5] 运行日志（只读 cc-switch 历史） ──────────────────────────
function Menu-Logs {
    if (-not (Show-Banner "运行日志 · 只读 cc-switch proxy 日志")) {
        Read-Host "按回车返回主菜单"; return 1
    }
    Write-Host "请选择:" -ForegroundColor Yellow
    Write-Host "  [1] 最近失败日志        history --fails" -ForegroundColor White
    Write-Host "  [2] 最近全部日志        history"
    Write-Host "  [3] 供应商统计          stats --since 7d"
    Write-Host "  [4] 静默路由排行        routing --since 7d"
    Write-Host "  [5] 实时监控（轮询）    watch · 有新日志就打印"
    Write-Host "  [6] 返回主菜单"
    $c = Read-Host "输入 1-6 (默认1)"
    switch ($c) {
        "2" {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("history"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--limit"); $cmdArgs.Add("30")
            Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "history"
            $null = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        "3" {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("stats"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--since"); $cmdArgs.Add("7d")
            $null = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        "4" {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("routing"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--since"); $cmdArgs.Add("7d"); $cmdArgs.Add("--limit"); $cmdArgs.Add("20")
            $null = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        "5" {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("watch"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--interval"); $cmdArgs.Add("3")
            Write-Host "实时监控中，Ctrl+C 结束…" -ForegroundColor Cyan
            $null = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        "6" { return 0 }
        default {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("history"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--fails"); $cmdArgs.Add("--limit"); $cmdArgs.Add("30")
            $null = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
    }
    Read-Host "按回车返回主菜单"
    return 0
}

# ── [6] 高级设置（进程内有效） ───────────────────────────────────
function Menu-AdvancedSettings {
    Show-Banner "高级设置（本进程有效，重开需重设）" | Out-Null
    Write-Host "当前设置:" -ForegroundColor Gray
    Write-Host "  JSON 输出:        $(if ($script:AdvJson) {'开'} else {'关（默认）'})" -ForegroundColor Gray
    Write-Host "  probe-max-tokens: $(if ($script:AdvMaxTokens) {$script:AdvMaxTokens} else {'20（默认）'})" -ForegroundColor Gray
    Write-Host "  允许 thinking:    $(if ($script:AdvEnableThinking) {'开'} else {'关（默认）'})" -ForegroundColor Gray
    Write-Host "  user-agent:       $(if ($script:AdvUserAgent) {$script:AdvUserAgent} else {'本机版本（默认）'})" -ForegroundColor Gray
    Write-Host "  上下文档位:       $($script:AdvProbeContext)（inspect 无声明时冒烟）" -ForegroundColor Gray
    Write-Host "  vision 探测:      $(if ($script:AdvVision) {'开'} else {'关（默认）'})" -ForegroundColor Gray
    Write-Host ""
    Write-Host "回车保留当前值。" -ForegroundColor DarkGray
    Write-Host ""

    $j = Read-Host "JSON 输出？(y/N)"
    if (-not [string]::IsNullOrWhiteSpace($j)) {
        $script:AdvJson = ($j -eq "y" -or $j -eq "Y")
    }
    $mt = Read-Host "probe-max-tokens（留空=20；thinking 模型可填 1024）"
    if (-not [string]::IsNullOrWhiteSpace($mt)) { $script:AdvMaxTokens = $mt }
    $th = Read-Host "允许 thinking？(y/N)"
    if (-not [string]::IsNullOrWhiteSpace($th)) {
        $script:AdvEnableThinking = ($th -eq "y" -or $th -eq "Y")
    }
    $ua = Read-Host "user-agent 覆盖（留空=本机 claude 版本）"
    if (-not [string]::IsNullOrWhiteSpace($ua)) { $script:AdvUserAgent = $ua }
    $cx = Read-Host "上下文档位 512k/1m（inspect 无声明时；默认 512k）"
    if (-not [string]::IsNullOrWhiteSpace($cx)) {
        $cxNorm = $cx.Trim().ToLower()
        if ($cxNorm -in @("512k", "1m")) {
            $script:AdvProbeContext = $cxNorm
        } else {
            Write-Host "  无效档位 '$cx'，保留 $($script:AdvProbeContext)" -ForegroundColor Yellow
        }
    }
    $vi = Read-Host "inspect 开启 vision？(y/N)"
    if (-not [string]::IsNullOrWhiteSpace($vi)) {
        $script:AdvVision = ($vi -eq "y" -or $vi -eq "Y")
    }

    Write-Host ""
    Write-Host "已保存。JSON/max-tokens/thinking/UA 作用于 check/inspect；上下文档位与 vision 仅 inspect。" -ForegroundColor Green
    Read-Host "按回车返回主菜单"
}

# ── 主菜单循环 ──────────────────────────────────────────────────
function Show-MainMenu {
    Show-Banner | Out-Null
    Write-Host "请选择操作:" -ForegroundColor Yellow
    Write-Host "  [1] 健康检测 · 快速体检   一键（claude/队列）" -ForegroundColor White
    Write-Host "  [2] 健康检测 · 自定义     选类型/范围"
    Write-Host "  [3] 拉模型列表            GET /v1/models 目录"
    Write-Host "  [4] 深度诊断 (inspect)    单一 (provider, model)"
    Write-Host "  [5] 运行日志              失败/统计/路由/实时监控" -ForegroundColor White
    Write-Host "  [6] 高级设置              JSON/thinking/UA/max-tokens/context/vision"
    Write-Host "  [7] 退出" -ForegroundColor White
    Write-Host ""
    return (Read-Host "输入 1-7 (默认1)")
}

while ($true) {
    $choice = Show-MainMenu
    switch ($choice) {
        ""  { $null = Menu-HealthCheckQuick }
        "1" { $null = Menu-HealthCheckQuick }
        "2" { $null = Menu-HealthCheckCustom }
        "3" { $null = Menu-ListModels }
        "4" { $null = Menu-Inspect }
        "5" { $null = Menu-Logs }
        "6" { $null = Menu-AdvancedSettings }
        "7" { exit 0 }
        default {
            Write-Host "无效输入: '$choice'" -ForegroundColor Red
            Read-Host "按回车重试"
        }
    }
}

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
            1 { "1 = 有供应商不可用" }
            2 { "2 = 环境/参数错误" }
            3 { "3 = 部分失败" }
            4 { "4 = 批量全部失败" }
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
    $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
    Write-Host ""
    Write-Host "  [1] 重新选择（类型/范围）" -ForegroundColor White
    Write-Host "  [2] 返回主菜单" -ForegroundColor White
    $again = Read-Host "选择（默认 2）"
    if ($again -eq "1") { return (Menu-HealthCheckQuick) }
    return $code
}

# ── [2] 健康检测 · 自定义（选类型/范围） ─────────────────────────
function Menu-HealthCheckCustom {
    if (-not (Show-Banner "健康检测 · 自定义")) {
        Read-HostSafe "按回车返回主菜单" | Out-Null; return 1
    }
    $type = Get-AppType
    Write-Host ""
    Write-Host "请选择范围:" -ForegroundColor Yellow
    Write-Host "  [1] 只测故障转移队列 + 当前激活  (快)" -ForegroundColor White
    Write-Host "  [2] 测全部供应商                   (完整)" -ForegroundColor White
    Write-Host "  [3] 只测当前激活的 1 个供应商      (最快)" -ForegroundColor White
    Write-Host "  [4] 自定义选择供应商               (多选数字)" -ForegroundColor White
    $scope = Read-Host "输入 1-4 (默认1)"

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
            for ($i = 0; $i -lt $names.Count; $i++) {
                Write-Host "  [$($i + 1)] $($names[$i])"
            }
            Write-Host "  [a] 全部供应商" -ForegroundColor Cyan
            $sel = Read-Host "输入序号（逗号分隔，如 1,3）或 a 全选（默认 1）"
            if ([string]::IsNullOrWhiteSpace($sel)) {
                $selectedProviderArg = $names[0]
            } elseif ($sel -eq "a" -or $sel -eq "A") {
                $selectedProviderArg = $names -join ","
            } else {
                $idxs = $sel -split "," | ForEach-Object { $_.Trim() }
                $selectedNames = @()
                foreach ($idx in $idxs) {
                    if ($idx -match '^\d+$' -and [int]$idx -ge 1 -and [int]$idx -le $names.Count) {
                        $selectedNames += $names[[int]$idx - 1]
                    } elseif (-not [string]::IsNullOrWhiteSpace($idx)) {
                        $selectedNames += $idx
                    }
                }
                if ($selectedNames.Count -eq 0) {
                    $selectedProviderArg = $names[0]
                } else {
                    $selectedProviderArg = $selectedNames -join ","
                }
            }
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
    $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
    Write-Host ""
    Write-Host "  [1] 重新选择（类型/范围）" -ForegroundColor White
    Write-Host "  [2] 返回主菜单" -ForegroundColor White
    $again = Read-Host "选择（默认 2）"
    if ($again -eq "1") { return (Menu-HealthCheckCustom) }
    return $code
}

# ── [3] 拉模型列表 ──────────────────────────────────────────────
function Menu-ListModels {
    if (-not (Show-Banner "拉取供应商 /v1/models 模型目录")) {
        Read-HostSafe "按回车返回主菜单" | Out-Null; return 1
    }
    $type = Get-AppType
    Write-Host ""
    Write-Host "请选择范围:" -ForegroundColor Yellow
    Write-Host "  [1] 故障转移队列 + 当前激活"
    Write-Host "  [2] 全部供应商"
    $scope = Read-Host "输入 1-2 (默认1)"
    Write-Host ""
    Write-Host "探测模式:" -ForegroundColor Yellow
    Write-Host "  [1] 只拉列表（默认，最快）"
    Write-Host "  [2] 轻量探测每个模型（2+3 题）"
    Write-Host "  [3] 深度探测（text/streaming/metadata/thinking/tools）"
    $probeMode = Read-Host "输入 1-3 (默认1)"
    $src = "listed"
    if ($probeMode -eq "2" -or $probeMode -eq "3") {
        Write-Host ""
        Write-Host "探测哪些模型:" -ForegroundColor Yellow
        Write-Host "  [1] listed     - /v1/models 列表（默认）"
        Write-Host "  [2] configured - cc-switch 配置档位"
        Write-Host "  [3] both       - 合并去重"
        $srcChoice = Read-Host "输入 1-3 (默认1)"
        $src = switch ($srcChoice) { "2" { "configured" } "3" { "both" } default { "listed" } }
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
    Write-Host "  [1] 重新选择（类型/范围/探测模式）" -ForegroundColor White
    Write-Host "  [2] 返回主菜单" -ForegroundColor White
    $again = Read-Host "选择（默认 2）"
    if ($again -eq "1") { return (Menu-ListModels) }
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
        for ($i = 0; $i -lt $names.Count; $i++) {
            Write-Host "  [$($i + 1)] $($names[$i])"
        }
        Write-Host "  [a] 全部供应商" -ForegroundColor Cyan
        $sel = Read-Host "输入序号（逗号分隔多选，如 1,3,5）或 a 全选（默认 1）"
        if ([string]::IsNullOrWhiteSpace($sel)) {
            $provider = $names[0]; $providers = @($names[0])
        } elseif ($sel -eq "a" -or $sel -eq "A") {
            $providers = @($names); $provider = $names -join ","; $multiProvider = $true
        } else {
            $idxs = $sel -split "," | ForEach-Object { $_.Trim() }
            $selectedNames = @()
            foreach ($idx in $idxs) {
                if ($idx -match '^\d+$' -and [int]$idx -ge 1 -and [int]$idx -le $names.Count) {
                    $selectedNames += $names[[int]$idx - 1]
                } elseif (-not [string]::IsNullOrWhiteSpace($idx)) {
                    $selectedNames += $idx
                }
            }
            if ($selectedNames.Count -eq 0) {
                $provider = $names[0]; $providers = @($names[0])
            } else {
                $providers = @($selectedNames | Select-Object -Unique)
                $provider = $providers -join ","
                $multiProvider = ($providers.Count -gt 1)
            }
        }
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
        Write-Host "选择档位（从每家 cc-switch 配置中取对应模型）:" -ForegroundColor Yellow
        Write-Host "  [1] haiku   （默认）" -ForegroundColor White
        Write-Host "  [2] sonnet" -ForegroundColor White
        Write-Host "  [3] opus" -ForegroundColor White
        Write-Host "  [4] fable" -ForegroundColor White
        Write-Host "  [5] default" -ForegroundColor White
        Write-Host "  （逗号分隔多选，如 1,2 表示 haiku+sonnet）" -ForegroundColor DarkGray
        $tierSel = Read-Host "选择档位（默认 1）"
        $tierMap = @{ "1"="haiku"; "2"="sonnet"; "3"="opus"; "4"="fable"; "5"="default" }
        if ([string]::IsNullOrWhiteSpace($tierSel)) { $tierSel = "1" }
        $selectedTiers = @()
        foreach ($t in ($tierSel -split "," | ForEach-Object { $_.Trim() })) {
            if ($tierMap.ContainsKey($t)) { $selectedTiers += $tierMap[$t] }
        }
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
        Write-Host "  [1] 重新选择（重走 type -> provider -> tier）" -ForegroundColor White
        Write-Host "  [2] 返回主菜单" -ForegroundColor White
        $again = Read-Host "选择（默认 2）"
        if ($again -eq "1") { return (Menu-Inspect) }
        return $overallCode
    }

    Write-Host "检测模式:" -ForegroundColor Yellow
    Write-Host "  [1] 单一模型（默认）" -ForegroundColor White
    Write-Host "  [2] 批量检测该供应商的所有模型" -ForegroundColor White
    Write-Host "  [3] 自定义选择模型 + 检测维度" -ForegroundColor White
    $modeChoice = Read-Host "选择（默认 1）"
    $batchMode = ($modeChoice -eq "2")
    $customMode = ($modeChoice -eq "3")

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
            for ($i = 0; $i -lt $modelChoices.Count; $i++) {
                $mc = $modelChoices[$i]
                $lab = if ($mc.label) { "  $($mc.label)" } else { "" }
                Write-Host ("  [{0}] {1}{2}" -f ($i + 1), $mc.id, $lab)
            }
            Write-Host "  [a] 全部模型" -ForegroundColor Cyan
            $msel = Read-Host "输入序号（逗号分隔，如 1,3,5）或 a 全选（默认 1）"
            if ([string]::IsNullOrWhiteSpace($msel)) {
                $selectedModels = $modelChoices[0].id
            } elseif ($msel -eq "a" -or $msel -eq "A") {
                $selectedModels = ($modelChoices | ForEach-Object { $_.id }) -join ","
            } else {
                $idxs = $msel -split "," | ForEach-Object { $_.Trim() }
                $selected = @()
                foreach ($idx in $idxs) {
                    if ($idx -match '^\d+$' -and [int]$idx -ge 1 -and [int]$idx -le $modelChoices.Count) {
                        $selected += $modelChoices[[int]$idx - 1].id
                    }
                }
                if ($selected.Count -eq 0) {
                    Write-Host "无效选择，返回主菜单。" -ForegroundColor Yellow
                    Read-HostSafe "按回车" | Out-Null; return 1
                }
                $selectedModels = $selected -join ","
            }
        }

        Write-Host ""
        Write-Host "检测维度（默认全开）:" -ForegroundColor Yellow
        Write-Host "  [1] text              文本探测"
        Write-Host "  [2] streaming         流式探测"
        Write-Host "  [3] model-consistency 模型路由比对"
        Write-Host "  [4] metadata          元数据"
        Write-Host "  [5] thinking          Thinking 能力"
        Write-Host "  [6] tools             Tool use"
        Write-Host "  [7] vision            视觉能力"
        Write-Host "  [a] 全部维度（默认）" -ForegroundColor Cyan
        $dimSel = Read-Host "输入序号（逗号分隔，如 1,2,3）或 a 全选（默认 a）"
        $dimMap = @{
            "1" = "text"
            "2" = "streaming"
            "3" = "model-consistency"
            "4" = "metadata"
            "5" = "thinking"
            "6" = "tools"
            "7" = "vision"
        }
        if ([string]::IsNullOrWhiteSpace($dimSel) -or $dimSel -eq "a" -or $dimSel -eq "A") {
            $include = "text,streaming,model-consistency,protocol,error-classification,metadata,thinking,tools"
        } else {
            $dims = $dimSel -split "," | ForEach-Object { $_.Trim() }
            $selected = @()
            foreach ($d in $dims) {
                if ($dimMap.ContainsKey($d)) {
                    $selected += $dimMap[$d]
                }
            }
            if ($selected.Count -eq 0) {
                Write-Host "无效维度，使用默认全开。" -ForegroundColor Yellow
                $include = "text,streaming,model-consistency,protocol,error-classification,metadata,thinking,tools"
            } else {
                # 自动加上 protocol 和 error-classification
                $selected += "protocol", "error-classification"
                $include = ($selected | Select-Object -Unique) -join ","
            }
        }

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
        Write-Host "  [1] 重新选择（重走 type → provider → 模式）" -ForegroundColor White
        Write-Host "  [2] 返回主菜单" -ForegroundColor White
        $again = Read-Host "选择（默认 2）"
        if ($again -eq "1") { return (Menu-Inspect) }
        return $code
    }

    if ($batchMode) {
        Write-Host ""
        Write-Host "批量检测范围:" -ForegroundColor Yellow
        Write-Host "  [1] configured - cc-switch 配置档位"
        Write-Host "  [2] listed     - 供应商 /v1/models 声明"
        Write-Host "  [3] both       - 两者合并去重"
        $srcChoice = Read-Host "选择（默认 1）"
        $source = switch ($srcChoice) { "2" { "listed" } "3" { "both" } default { "configured" } }
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
        Write-Host "  [1] 重新选择（重走 type → provider → 模式）" -ForegroundColor White
        Write-Host "  [2] 返回主菜单" -ForegroundColor White
        $again = Read-Host "选择（默认 2）"
        if ($again -eq "1") { return (Menu-Inspect) }
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
        for ($i = 0; $i -lt $modelChoices.Count; $i++) {
            $mc = $modelChoices[$i]
            $lab = if ($mc.label) { "  $($mc.label)" } else { "" }
            Write-Host ("  [{0}] {1}{2}" -f ($i + 1), $mc.id, $lab)
        }
        $msel = Read-Host "输入序号选择，或直接输入模型 ID（默认 1）"
        if ([string]::IsNullOrWhiteSpace($msel)) {
            $model = $modelChoices[0].id
        } elseif ($msel -match '^\d+$' -and [int]$msel -ge 1 -and [int]$msel -le $modelChoices.Count) {
            $model = $modelChoices[[int]$msel - 1].id
        } else {
            $model = $msel
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
    Write-Host "  [1] 重新选择（重走 type → provider → model）" -ForegroundColor White
    Write-Host "  [2] 返回主菜单" -ForegroundColor White
    $again = Read-Host "选择（默认 2）"
    if ($again -eq "1") { return (Menu-Inspect) }
    return $code
}

# ── [5] 运行日志（只读 cc-switch 历史） ──────────────────────────
function Menu-Logs {
    if (-not (Show-Banner "运行日志 · 只读 cc-switch proxy 日志")) {
        Read-HostSafe "按回车返回主菜单" | Out-Null; return 1
    }
    Write-Host "请选择:" -ForegroundColor Yellow
    Write-Host "  [1] 最近失败日志        history --fails" -ForegroundColor White
    Write-Host "  [2] 最近全部日志        history"
    Write-Host "  [3] 供应商统计          stats --since 7d"
    Write-Host "  [4] 静默路由排行        routing --since 7d"
    Write-Host "  [5] 实时监控（轮询）    watch · 有新日志就打印"
    Write-Host "  [6] 分析报表            analyze · 按天/模型/供应商交叉"
    Write-Host "  [7] 返回主菜单"
    $c = Read-Host "输入 1-7 (默认1)"
    $code = 0
    switch ($c) {
        "2" {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("history"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--limit"); $cmdArgs.Add("30")
            Apply-AdvancedArgs -CmdArgs $cmdArgs -SubCommand "history"
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        "3" {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("stats"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--since"); $cmdArgs.Add("7d")
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        "4" {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("routing"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--since"); $cmdArgs.Add("7d"); $cmdArgs.Add("--limit"); $cmdArgs.Add("20")
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        "5" {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("watch"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--interval"); $cmdArgs.Add("3")
            Write-Host "实时监控中，Ctrl+C 结束…" -ForegroundColor Cyan
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        "6" {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("analyze"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--since"); $cmdArgs.Add("7d")
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
        "7" { return 0 }
        default {
            $cmdArgs = [System.Collections.Generic.List[string]]::new()
            $cmdArgs.Add("history"); $cmdArgs.Add("--db"); $cmdArgs.Add($DB)
            $cmdArgs.Add("--fails"); $cmdArgs.Add("--limit"); $cmdArgs.Add("30")
            $code = Invoke-Ccpulse -CmdArgs $cmdArgs.ToArray()
        }
    }
    Write-Host ""
    Write-Host "  [1] 重新选择日志子项" -ForegroundColor White
    Write-Host "  [2] 返回主菜单" -ForegroundColor White
    $again = Read-Host "选择（默认 2）"
    if ($again -eq "1") { return (Menu-Logs) }
    return $code
}

# ── [6] 高级设置（进程内有效） ───────────────────────────────────
function Menu-AdvancedSettings {
    Show-Banner "高级设置（本进程有效，重开需重设）" | Out-Null
    # 编号清单循环：输入项号改单值，q/空回车返回主菜单。避免改一项须回车 9 次。
    while ($true) {
        Write-Host "当前设置（输入编号修改，q 或空回车返回主菜单）:" -ForegroundColor Yellow
        Write-Host "  [1] JSON 输出         [check]        $(if ($script:AdvJson) {'开'} else {'关（默认）'})" -ForegroundColor Gray
        Write-Host "  [2] probe-max-tokens  [check/inspect/list] $(if ($script:AdvMaxTokens) {$script:AdvMaxTokens} else {'1024（默认）'})" -ForegroundColor Gray
        Write-Host "  [3] 允许 thinking     [check/inspect] $(if ($script:AdvEnableThinking) {'开'} else {'关（默认）'})" -ForegroundColor Gray
        Write-Host "  [4] user-agent        [全部子命令]   $(if ($script:AdvUserAgent) {$script:AdvUserAgent} else {'本机版本（默认）'})" -ForegroundColor Gray
        Write-Host "  [5] 上下文档位        [inspect]      $($script:AdvProbeContext)（无声明时冒烟）" -ForegroundColor Gray
        Write-Host "  [6] vision 探测       [inspect]      $(if ($script:AdvVision) {'开'} else {'关（默认）'})" -ForegroundColor Gray
        Write-Host "  [7] stealth 隐身      [check]        $(if ($script:AdvStealth) {'开'} else {'关（默认）'})" -ForegroundColor Gray
        Write-Host "  [8] 快速体检类型      [快速体检]     $($script:AdvType)" -ForegroundColor Gray
        Write-Host "  [9] 快速体检范围      [快速体检]     $(if ($script:AdvScope -eq 'all') {'全部'} else {'队列+当前（默认）'})" -ForegroundColor Gray
        Write-Host ""
        $pick = Read-HostSafe "输入 1-9 (q 退出)"
        if ($null -eq $pick) { return }  # stdin EOF
        $pick = $pick.Trim()
        if ($pick -eq "" -or $pick -eq "q" -or $pick -eq "Q") { return }
        switch ($pick) {
            "1" {
                $j = Read-HostSafe "JSON 输出？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($j)) { $script:AdvJson = ($j -eq "y" -or $j -eq "Y") }
            }
            "2" {
                $mt = Read-HostSafe "probe-max-tokens（留空=1024；thinking 模型可调高）"
                if (-not [string]::IsNullOrWhiteSpace($mt)) { $script:AdvMaxTokens = $mt }
            }
            "3" {
                $th = Read-HostSafe "允许 thinking？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($th)) { $script:AdvEnableThinking = ($th -eq "y" -or $th -eq "Y") }
            }
            "4" {
                $ua = Read-HostSafe "user-agent 覆盖（留空=本机 claude 版本）"
                if (-not [string]::IsNullOrWhiteSpace($ua)) { $script:AdvUserAgent = $ua }
            }
            "5" {
                $cx = Read-HostSafe "上下文档位 512k/1m（默认 512k）"
                if (-not [string]::IsNullOrWhiteSpace($cx)) {
                    $cxNorm = $cx.Trim().ToLower()
                    if ($cxNorm -in @("512k", "1m")) { $script:AdvProbeContext = $cxNorm }
                    else { Write-Host "  无效档位 '$cx'，保留 $($script:AdvProbeContext)" -ForegroundColor Yellow }
                }
            }
            "6" {
                $vi = Read-HostSafe "inspect 开启 vision？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($vi)) { $script:AdvVision = ($vi -eq "y" -or $vi -eq "Y") }
            }
            "7" {
                $st = Read-HostSafe "check 开启 stealth 隐身？(y/N)"
                if (-not [string]::IsNullOrWhiteSpace($st)) { $script:AdvStealth = ($st -eq "y" -or $st -eq "Y") }
            }
            "8" {
                Write-Host "  [1] claude(默认) [2] codex [3] openclaw [4] all" -ForegroundColor Yellow
                $ty = Read-HostSafe "输入 1-4（留空保留 $($script:AdvType)）"
                if (-not [string]::IsNullOrWhiteSpace($ty)) {
                    switch ($ty.Trim()) {
                        "1" { $script:AdvType = "claude" }
                        "2" { $script:AdvType = "codex" }
                        "3" { $script:AdvType = "openclaw" }
                        "4" { $script:AdvType = "all" }
                        default { Write-Host "  无效类型 '$ty'，保留 $($script:AdvType)" -ForegroundColor Yellow }
                    }
                }
            }
            "9" {
                Write-Host "  [1] 队列+当前(默认,快) [2] 全部(完整)" -ForegroundColor Yellow
                $sc = Read-HostSafe "输入 1-2（留空保留 $(if ($script:AdvScope -eq 'all') {'全部'} else {'队列+当前'})）"
                if (-not [string]::IsNullOrWhiteSpace($sc)) {
                    switch ($sc.Trim()) {
                        "1" { $script:AdvScope = "failover" }
                        "2" { $script:AdvScope = "all" }
                        default { Write-Host "  无效范围 '$sc'，保留 $($script:AdvScope)" -ForegroundColor Yellow }
                    }
                }
            }
            default { Write-Host "无效输入: '$pick'，请输入 1-9 或 q。" -ForegroundColor Red }
        }
    }
}

# ── 主菜单循环 ──────────────────────────────────────────────────
function Show-MainMenu {
    Show-Banner | Out-Null
    Write-Host "一键检查 AI 模型服务是否正常 —— 按 1 开始体检" -ForegroundColor Green
    Write-Host "请选择操作:" -ForegroundColor Yellow
    Write-Host "  [1] 健康检测 · 快速体检   一键（claude/队列）" -ForegroundColor White
    Write-Host "  [2] 健康检测 · 自定义     选类型/范围"
    Write-Host "  [3] 拉模型列表            GET /v1/models 目录"
    Write-Host "  [4] 深度诊断 (inspect)    单一 (provider, model)"
    Write-Host "  [5] 运行日志              失败/统计/路由/实时监控" -ForegroundColor White
    Write-Host "  [6] 高级设置              JSON/stealth/thinking/UA/类型/范围"
    Write-Host "  [7] 退出" -ForegroundColor White
    Write-Host ""
    # 用 ReadLine 而非 Read-Host：EOF（stdin 关闭/管道结束）返回 $null，
    # 直接敲回车返回 ""，Read-Host 两者都给 ""，无法区分会导致主菜单死循环。
    Write-Host "输入 1-7 (默认1): " -NoNewline
    return [Console]::In.ReadLine()
}

$script:LastMenuCode = 0
while ($true) {
    $choice = Show-MainMenu
    if ($null -eq $choice) { exit $script:LastMenuCode }  # stdin 结束，退出而非重绘
    $menuCode = $null
    switch ($choice.Trim()) {
        ""  { $menuCode = Menu-HealthCheckQuick }
        "1" { $menuCode = Menu-HealthCheckQuick }
        "2" { $menuCode = Menu-HealthCheckCustom }
        "3" { $menuCode = Menu-ListModels }
        "4" { $menuCode = Menu-Inspect }
        "5" { $menuCode = Menu-Logs }
        "6" { $menuCode = Menu-AdvancedSettings }
        "7" { exit 0 }
        default {
            Write-Host "无效输入: '$choice'" -ForegroundColor Red
            Read-HostSafe "按回车重试"
            continue
        }
    }
    if ($null -ne $menuCode) { $script:LastMenuCode = [int]$menuCode }
}

# CC-Pulse tasks

# 默认解释器为 PATH 中的 python；可通过 PYTHON 覆盖
PYTHON := env_var_or_default("PYTHON", "python")
SCRIPT := "check_ccswitch_health.py"

# Default: show available commands
default:
    @just --list

# 健康检测：故障转移队列 + 当前激活（快，日常体检）
check:
    {{PYTHON}} {{SCRIPT}} check --failover-only --workers 8 --timeout 45

# 健康检测：全部供应商（完整，较慢）
check-all:
    {{PYTHON}} {{SCRIPT}} check --workers 8 --timeout 45

# 健康检测：隐身模式（降并发+随机延迟，被站点识别为测活时改用，较慢）
check-stealth:
    {{PYTHON}} {{SCRIPT}} check --failover-only --stealth --timeout 45

# 拉模型列表：故障转移队列 + 当前激活
models:
    {{PYTHON}} {{SCRIPT}} list-models --failover-only

# 拉模型列表：全部供应商
models-all:
    {{PYTHON}} {{SCRIPT}} list-models

# 拉列表 + 轻量探测每个模型（2+3 题验证真实可用）
models-probe:
    {{PYTHON}} {{SCRIPT}} list-models --failover-only --probe

# 拉列表 + 轻量探测全部供应商（configured + listed 合并）
models-probe-all:
    {{PYTHON}} {{SCRIPT}} list-models --probe --source both

# 拉列表 + 深度探测每个模型（5 维度：text/streaming/metadata/thinking/tools）
models-deep:
    {{PYTHON}} {{SCRIPT}} list-models --failover-only --deep --timeout 60

# 最近失败日志
history-fails:
    {{PYTHON}} {{SCRIPT}} history --fails --limit 30

# 7 天统计
stats:
    {{PYTHON}} {{SCRIPT}} stats --since 7d

# 静默路由排行
routing:
    {{PYTHON}} {{SCRIPT}} routing --since 7d --limit 20

# 实时监控 cc-switch 日志（Ctrl+C 结束）
watch:
    {{PYTHON}} {{SCRIPT}} watch --interval 3

# 多维分析报表（全维度，7 天）
analyze:
    {{PYTHON}} {{SCRIPT}} analyze --since 7d

# 按模型延迟分析
analyze-model:
    {{PYTHON}} {{SCRIPT}} analyze --mode model --since 7d

# 按天健康趋势
analyze-daily:
    {{PYTHON}} {{SCRIPT}} analyze --mode day --since 30d

# JSON 健康报告（管道到 jq）
check-json:
    {{PYTHON}} {{SCRIPT}} check --failover-only --json

# 运行测试套件（单元 + 端到端 mock；纯标准库，无第三方依赖）
test:
    {{PYTHON}} tests/test_ccpulse_full.py

# 运行 PS1 启动器端到端测试（需要 pwsh）
test-ps1:
    {{PYTHON}} tests/test_ps1_launcher.py

# 全部测试
test-all:
    {{PYTHON}} tests/test_ccpulse_full.py
    {{PYTHON}} tests/test_p0_protocol_fix.py
    {{PYTHON}} tests/test_ps1_launcher.py
    {{PYTHON}} tests/test_env_check.py
    {{PYTHON}} tests/test_archive_trend.py
    {{PYTHON}} tests/test_tui.py
    {{PYTHON}} tests/test_docs_consistency.py

# 探测历史趋势（归档跨次聚合）
trend:
    {{PYTHON}} {{SCRIPT}} trend --since 7d

# 环境变量覆盖检测（静默路由排查）
env-check:
    {{PYTHON}} {{SCRIPT}} env-check

# Lint with ruff
lint:
    ruff check .
    ruff format --check .

# 文档与代码一致性守卫：禁止 README 中出现 4 维 / 6 维等过时说法
lint-docs:
    {{PYTHON}} tests/test_docs_consistency.py

# Format with ruff
format:
    ruff format .
    ruff check --fix .

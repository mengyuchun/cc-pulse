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

# 拉模型列表：故障转移队列 + 当前激活
models:
    {{PYTHON}} {{SCRIPT}} list-models --failover-only

# 拉模型列表：全部供应商
models-all:
    {{PYTHON}} {{SCRIPT}} list-models

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
    {{PYTHON}} tests/test_ps1_launcher.py

# Lint with ruff
lint:
    ruff check .
    ruff format --check .

# Format with ruff
format:
    ruff format .
    ruff check --fix .

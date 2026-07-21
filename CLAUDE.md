# Project instructions for Claude Code

## Project Overview
CC-Pulse：给 cc-switch 供应商做健康检测与单模型深度诊断的 CLI。
纯标准库（Python 3.10+），零第三方依赖。

## Code Graph
CodeGraph 已初始化，用于结构化代码查询。

## Conventions
- 语言: Python 3.10+
- 格式化: ruff format
- 测试: 纯标准库脚本（tests/test_ccpulse_full.py、tests/test_ps1_launcher.py），不依赖 pytest 运行时
- 运行时零依赖；开发期可用 ruff

## Dependencies
无运行时依赖。见 `requirements.txt`。

## Notes
- 只读 cc-switch 的 SQLite（`file:...?mode=ro`），绝不改库
- 不写凭据到日志/报告；API key 仅用于请求头
- 公共参数透传（user_agent / probe_max_tokens 等），无全局可变状态
- 测试放 tests/；与业务无关的临时脚本不要提交
- 提交前：`just test` 与 `just test-ps1` 应全绿

<div align="center">

# CC-Pulse

**零依赖的 Claude Code / 编程代理中转站可用性与真实性 CLI 哨兵**

不信「HTTP 200」，只信「真能用、没换芯」。10 秒测出 API Key 真实可用性、环境变量覆盖与假中转信号。

中文 · [English](README.en.md)

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/stdlib%20only-green.svg)](#)
[![Tests](https://img.shields.io/badge/tests-595%20pass-brightgreen.svg)](#测试)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

---

## 为什么需要 CC-Pulse

- 🔇 **200 ≠ 可用**：中转站返回 HTTP 200 却带报错 JSON、空字符串、或用廉价模型冒充高价模型
- 🕵️ **静默路由覆盖**：终端环境变量（`ANTHROPIC_BASE_URL` 等）悄悄盖掉了配置文件或切换器
- 🧠 **Thinking / Tools 假兼容**：thinking 模型耗光小预算输出空文本，或口头假装支持 `tool_use`
- ☁️ **云后端来源不明**：把 AWS Bedrock / Google Vertex 冒充直连官转，或跨协议套壳换芯

---

## 10 秒快速上手（无需 `git clone`，无需 `cc-switch.db`）

### 第一步：零 Key 排雷（绝不发送任何 API Key）
```bash
uvx cc-pulse env-check --no-db
```
自动检测 shell 环境变量是否覆盖 `~/.claude/settings.json`、`~/.codex/config.toml` 或 `cc-switch.db`。

### 第二步：单端点测活（无需安装任何前置 App）
```bash
export MY_RELAY_KEY="sk-..."
uvx cc-pulse check --base-url https://relay.example.com --api-key-env MY_RELAY_KEY
```
不传 `--base-url` 时，会自动发现 `~/.cc-pulse/providers.toml`、`~/.claude/settings.json`、`~/.codex/config.toml` 或本机只读 `cc-switch.db`（`file:...?mode=ro`）。

### 第三步：单模型深度诊断 + 可分享战报卡
```bash
uvx cc-pulse inspect --base-url https://relay.example.com --api-key-env MY_RELAY_KEY \
  --model claude-sonnet-4-5 --format card
```
生成 ≤25 行**不含任何密钥或完整 URL** 的 Markdown 战报卡，包含：
- **Text / TTFT 流式 / 模型路由一致性**
- **Thinking 抑制 / 原生 `tool_use` 协议块**
- **云后端来源归因**（零额外请求识别 `Official / AWS Bedrock / Google Vertex / Unknown`）
- **P0/P1 防掺水保真**（跨协议换芯字段、`thinking` 签名存在性、`usage` 计费自洽）

> 本地源码运行：`git clone` 后用 `python check_ccswitch_health.py ...` 或 `pip install .` 即可。

---

## 核心命令与 Claude Code Skill

| 命令 | 核心用途 |
|---|---|
| `env-check` | 零 Key 排查环境变量静默覆盖 |
| `check` | 多档回退 + 动态算术题批量测活 |
| `inspect` | 8 维深度能力诊断 + 防掺水 + `--format card` / `--compare` |
| `list-models` | 拉取 `/v1/models` 目录并可选 `--probe` 逐模型验真 |
| `trend` | 读取本地 `~/.cc-pulse/probe_history.jsonl` 聚合跨次延迟/可用率趋势 |

- **Agent 原生集成**：直接加载 [skills/cc-pulse/SKILL.md](skills/cc-pulse/SKILL.md)，即可在 Claude Code 中用自然语言驱动 `cc-pulse` 诊断中转站。
- **完整参数表、TOML 配置、归档结构与个人辅助脚本**：见 [docs/cli-reference.md](docs/cli-reference.md)。

---

## 安全与只读边界

- 绝不修改 `~/.cc-switch/cc-switch.db`（始终以 `?mode=ro` 只读打开）
- 独立模式仅接收环境变量名（`--api-key-env`），绝不在命令行参数留存明文密钥
- 禁用 HTTP `3xx` 自动重定向，防止恶意节点通过 `302 Location` 窃取 `x-api-key` / `Authorization`
- 所有日志、错误摘要与 `--format card` 均经过脱敏过滤

---

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部成功（`check` 全部可用且未触发告警阈值 / `inspect` healthy / `env-check` 无冲突） |
| 1 | 部分失败或告警触发（`check` 部分不可用或低于 `--alert-threshold` / `inspect` 批量部分失败或答案不符） |
| 2 | 环境或参数错误（未找到配置或指定 `--db` 不存在 / 缺少参数 / `env-check` 检测到冲突） |
| 3 | 全部失败（`check` 或 `inspect` 批量/`--compare` 所有目标均不可用） |

---

## 测试

纯标准库单元测试 + 本地 mock HTTP 端到端测试（不触达任何真实供应商）。当前 **543 个 Python 测试 + 52 个 PS1 测试**：

| 测试文件 | 用途 | 用例数 |
|---|---|---|
| `tests/test_ccpulse_full.py` | 主套件（Mock SSE / 多协议 / 端到端） | 403 |
| `tests/test_archive_trend.py` | 探测归档与趋势聚合 | 54 |
| `tests/test_authenticity.py` | P0 换芯字段 + thinking 签名校验 | 16 |
| `tests/test_usage_consistency.py` | P1 usage 自洽 + 响应体结构化校验 | 21 |
| `tests/test_cache_replay.py` | P2-A 高熵缓存回放/钳温双发 | 10 |
| `tests/test_p0_protocol_fix.py` | P0 协议推断修复回归 | 4 |
| `tests/test_env_check.py` | 环境变量覆盖检测 | 14 |
| `tests/test_tui.py` | 跨平台 TUI 选择器 | 9 |
| `tests/test_p0_p1_fixes.py` | 安全重定向、闭套接字 SSE、独立模式与战报卡 | 12 |
| `tests/test_ps1_launcher.py` | Windows PS1 启动器交互测试 | 52 |
| `tests/test_docs_consistency.py` | 文档与代码一致性守卫 | — |

```bash
just test-all   # 运行全部 Python + PS1 测试
just lint       # ruff check + ruff format --check
just lint-docs  # 文档一致性守卫
```

# CC-Pulse CLI 完整参数与结构参考

本文档收录 CC-Pulse 的完整命令参数、JSON 输出字段、本地归档格式，以及仅供维护者本机桌面使用的辅助脚本说明（主 README 仅保留 10 秒快速上手路径）。

---

## 核心子命令

| 子命令 | 定位 | 是否需要 `cc-switch.db` | 说明 |
|---|---|---|---|
| `env-check` | 零 Key 环境变量冲突排查 | 否（自动读原生配置或 DB） | 检测 shell 环境变量是否静默覆盖 `~/.claude/settings.json`、`~/.codex/config.toml` 或 `cc-switch.db` |
| `check` | 多档回退批量可用性测活 | 否（支持 `--base-url` / 独立配置 / DB） | 按 `haiku → sonnet → opus → fable → default` 回退并校验动态算术题答案 |
| `inspect` | 单模型 8 维深度诊断 + 防掺水 | 否（支持 `--base-url` / 独立配置 / DB） | 覆盖文本、TTFT 流式、路由一致性、元数据、Thinking、原生 `tool_use`、Vision、云后端归因及 P0/P1 保真鉴别 |
| `list-models` | 模型目录拉取与轻量验真 | 否（支持 `--base-url` / 独立配置 / DB） | `GET /v1/models` 并可选 `--probe` 逐模型验证 |
| `trend` | 本地探活历史趋势聚合 | 否 | 读取 `~/.cc-pulse/probe_history.jsonl` 聚合成功率与 P50/P95 延迟 |

---

## 独立模式与无 DB 用法

1. **临时单端点探测（不写配置、Key 不落命令行历史）**：
   ```bash
   export MY_RELAY_KEY="sk-..."
   cc-pulse inspect --base-url https://relay.example.com --api-key-env MY_RELAY_KEY --model claude-sonnet-4-5 --format card
   cc-pulse check --base-url https://relay.example.com --api-key-env MY_RELAY_KEY
   ```
2. **自动发现独立配置**：
   无 `cc-switch.db`（或显式传 `--no-db`）时按顺序自动加载：
   - `~/.cc-pulse/providers.toml`
   - `~/.claude/settings.json`（`env` 块）
   - `~/.codex/config.toml` + `~/.codex/auth.json`

`~/.cc-pulse/providers.toml` 示例：
```toml
[providers.my_relay]
base_url = "https://relay.example.com"
api_key_env = "MY_RELAY_KEY"
app_type = "claude"
haiku = "claude-haiku-4-5"
sonnet = "claude-sonnet-4-5"
```

---

## 完整参数说明

### 全局公共选项
- `--db PATH`：指定 `cc-switch.db` 路径（默认 `~/.cc-switch/cc-switch.db`，严格只读 `?mode=ro`）
- `--no-db`：强制不读取 `cc-switch.db`（仅用独立配置或 `--base-url`）
- `--timeout N`：单请求超时秒数（默认 `30`）
- `--workers N`：并发数（默认 `6`）
- `--skip-tls-verify`：跳过 TLS 验证（仅用于信任的自签名测试站）
- `--user-agent STR`：覆盖 `User-Agent`
- `--stainless-version VER`：覆盖 `x-stainless-package-version`

### `check` 选项
- `--type {claude,codex,openclaw,all}`：供应商类型（默认 `claude`）
- `--failover-only` / `--current-only`：缩小检测范围
- `--provider NAME`：按名称过滤（支持逗号分隔或子串）
- `--select`：交互式 TUI 选择器（非 TTY 自动忽略）
- `--stealth`：隐身模式（并发降至 ≤ 3 并加入随机抖动）
- `--base-url URL` / `--api-key-env ENV` / `--model ID` / `--protocol {anthropic,openai_chat,openai_responses}`：独立单端点探测
- `--retry N`：网络错误重试次数（默认 `0`）
- `--probe-max-tokens N` / `--probe-enable-thinking`：探测预算与思考模式控制
- `--alert-threshold F`：可用率告警阈值（0–1，如 `0.8` 表示低于 80% 告警并触发非零退出码 `1`，供 cron / CI 使用）
- `--with-history` / `--history-since WIN`：附加本地 `cc-switch` 近 24h 日志摘要（仅 DB 存在时）
- `--archive PATH`：指定探活历史归档路径（默认 `~/.cc-pulse/probe_history.jsonl`）
- `--json`：stdout 输出结构化 JSON，人类进度输出到 stderr

### `inspect` 选项
- `--provider NAME` / `--select` / `--model ID` / `--all-models` / `--models A,B`
- `--source {configured,listed,manual}`：模型来源（默认 `configured`；`--base-url` 单模型默认 `manual`）
- `--keep-suffix`：保留 `[1M]` 等模型后缀
- `--include DIMS`：显式指定维度：`text,streaming,model-consistency,protocol,error-classification,metadata,context,thinking,tools,vision,cache-replay`（默认不含 `context`/`vision`/`cache-replay`）
- `--auth-full`：追加可选深度保真探针（`cache-replay`，不覆盖 `--include`）
- `--probe-context {512k,1m}`：配合 `--include context` 使用（不会隐式触发）
- `--format {human,json,card}` / `--human`：`card` 输出 ≤ 25 行脱敏 Markdown 战报卡
- `--compare "ProvA/m1,ProvB/m2"`：多目标横向对比
- `--quiet`：批量模式下输出纯 NDJSON（每模型一行，与 `--human` 互斥）
- `--probe-delay SEC` / `--max-retries N`：批量限速间隔与 429 重试

### `list-models` / `trend` / `env-check` 选项
- `list-models`：`--probe`、`--source {configured,listed,both}`、`--json` 及独立端点参数
- `trend`：`--since {24h,7d,30m}`、`--provider`、`--model`、`--include-test`、`--archive`、`--json`
- `env-check`：`--no-db`、`--json`

---

## 退出码契约

| 码 | 含义 |
|---|---|
| 0 | 全部成功（`check` 全部可用且未触发告警阈值 / `inspect` healthy 或 skipped / `list-models` / `trend` / `env-check` 无冲突） |
| 1 | 部分失败或告警触发（`check` 部分不可用或低于 `--alert-threshold` / `inspect` 批量部分失败或答案不符） |
| 2 | 环境或参数错误（未找到配置或指定 `--db` 不存在 / 缺少必要参数 / `env-check` 检测到环境变量冲突） |
| 3 | 全部失败（`check` 或 `inspect` 批量/`--compare` 所有目标均不可用） |

---

## 错误分类枚举（`error_category`）

`none` · `network` · `tls` · `authentication` · `rate_limit` · `model_not_found` · `protocol_incompatible` · `server_error` · `invalid_response` · `answer_mismatch` · `stream_protocol` · `ttft_timeout` · `stream_incomplete` · `unknown`

---

## 维护者个人辅助工具（非主 CLI 核心）

- **cc-switch 只读日志排查脚本**：`scripts/ccswitch_logs.py`（从主 CLI 迁出的 `history`、`stats`、`routing` 命令）：
  ```bash
  python scripts/ccswitch_logs.py history --fails --limit 30
  python scripts/ccswitch_logs.py stats --since 7d
  python scripts/ccswitch_logs.py routing --since 7d --limit 20
  ```
- **Windows 桌面交互菜单**：`run_health_check.ps1`（需 PowerShell 7+，面向个人桌面 `cc-switch.db` 一键体检，功能已冻结同步）。
- **本地归档轮转**：`trim_archive` 在 `~/.cc-pulse/probe_history.jsonl` 超过 5MB 且超过 10000 条记录时自动保留最新记录；`trend_direction` 使用 `↑` / `↓` / `→` 标注趋势方向。

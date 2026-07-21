<div align="center">

# CC-Pulse

**给 [cc-switch](https://github.com/farion1231/cc-switch) 供应商「号脉」的健康检测与单模型深度诊断工具**

纯标准库 · 零依赖 · 只读 · 跨平台

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-green.svg)](#)
[![Tests](https://img.shields.io/badge/tests-208%20pass-brightgreen.svg)](#测试)

</div>

---

## 为什么需要 CC-Pulse

cc-switch 帮你管理一堆 Claude Code / Codex 的 API 中转供应商。但中转站的水远比你想象的深：

- 🔇 **200 ≠ 可用**：有的站返回 200 却空回答、答非所问、或静默路由到更便宜的模型
- 🎭 **多档位陷阱**：haiku 能用 sonnet 不能用、opus 限流、fable 不存在
- 🔑 **认证方式各异**：有的只认 `x-api-key`，有的只认 `Authorization: Bearer`，有的校验客户端 UA
- 🧠 **thinking 模型**：DeepSeek/GLM 等用 20 token 预算去思考，啥答案都输出不了
- 📏 **上下文缩水**：声称 1M，实际 526k 就拒了
- 🛠️ **Tool/Vision 兼容性**：写代码强依赖 tool_use，但很多站根本不真支持

CC-Pulse 不信「能连上」，只信「能正确回答一道题」。并在你切换供应商前，把上述问题一眼看清。

---

## 与 cc-switch 内置检测的区别

cc-switch 自带 stream check（连通性检测），会记录 `http_status` / `response_time_ms` / `success`。CC-Pulse 与它是**互补关系**，不是替代：cc-switch 管配置与切换，CC-Pulse 专门做深度探活。

| 维度 | cc-switch 内置检测 | CC-Pulse |
|------|-------------------|----------|
| 侧重 | 连通性 / 延迟 / 运行态 | 实际可用性（认证 + 正确回答） |
| 对上游发真实模型请求 | 取决于当前 cc-switch 版本与检测配置 | ✅ 每个档位都发真实请求 |
| API key / token 无效 | 取决于检测是否覆盖该供应商认证链路 | ✅ 401 / 403 明确归为 `authentication` |
| 200 但答案空 | 可能无法从连通性结果区分 | ✅ `answer_mismatch`（不可用） |
| 200 但业务错误体 | 可能表现为「HTTP 成功」 | ✅ `invalid_response`，保留错误原文 |
| thinking 耗光 token | 不属于基础连通性检测范围 | ✅ 默认禁用 thinking，可调 `max_tokens` |
| 模型静默路由 | ❌ 不在基础检测范围 | ✅ `inspect` 比对 request / response model |
| 多档位回退 | 取决于运行时故障转移 | ✅ haiku → sonnet → opus → fable → default 主动探测 |
| 流式 / 工具 / 上下文 / vision | ❌ 不属于基础检测范围 | ✅ `inspect` 7 维度诊断 |

**典型陷阱场景**（都是真实遇到过的）：

**① HTTP 200，但 body 是业务错误**

```json
{"code":0,"msg":"旧转发链路已关闭","data":null}
```

✅ 连上 · ❌ 根本没出模型内容 → CC-Pulse 判 `invalid_response`

**② HTTP 200，但答案是空字符串**

```json
{"content":[{"type":"text","text":""}]}
```

✅ 连上 · ❌ thinking 把 token 花光，没有最终答案 → CC-Pulse 判 `answer_mismatch`

**③ key / token 错误或被吊销**

```json
{"type":"error","error":{"type":"AuthError","message":"Invalid API key."}}
```

✅ 端点活着 · ❌ 认证失败，实际用不了 → CC-Pulse 判 `authentication` 并告诉你到底哪步炸了

**④ key 还能列模型、但不能推理**

```text
GET /v1/models  → 200 ✅
POST /v1/messages → 401 Invalid API key ❌
```

cc-switch 的基础检测若只覆盖连通性维度，可能只看到前半段「能列模型」就判健康；CC-Pulse 会发真实推理请求，把后半段认证失效暴露出来。

一句话：**cc-switch 回答「能不能连」，CC-Pulse 回答「能不能用」**。

## 核心特性

### 1. 健康检测 `check` —— 多档回退 + 校验真实回答

- 按 `haiku → sonnet → opus → fable → default` 顺序探测，**首个正确回答的档位即停**
- 发 `"2+3=?"` 校验答案必须严格等于 `"5"`，**连通(200) ≠ 可用**
- 认证按 cc-switch 配置走：`ANTHROPIC_AUTH_TOKEN` → `Bearer`，`ANTHROPIC_API_KEY` → `x-api-key`
- **实时进度**：每个档位完成立即显示一行，不必等全部结束（解决「全跑完才显示」的体验问题）
- 批量并发 + 完整错误信息透传
- 结构化 JSON 报告，可被 jq / PowerShell / CI 直接消费

### 2. 模型目录 `list-models` —— 拉取供应商声明的模型清单

- `GET /v1/models`，兼容 Anthropic / OpenAI 响应格式
- 列出 ≠ 实际可用，只是供应商声称支持

### 3. 单模型深度诊断 `inspect` —— 7 维度全面体检

对指定 `(provider, model)` 跑文本 / 流式 / 元数据 / 上下文冒烟 / thinking / tool use / vision（可选），输出统一 JSON 报告：

| 维度 | 检测什么 |
|------|----------|
| **text** | 真实问题回答 + usage token 计数解析 |
| **streaming** | SSE / 非 SSE 流式、TTFT 首延迟、事件数、协议类型 |
| **metadata** | `GET /v1/models/{id}` 声明窗口/能力（标注「非实测」） |
| **context** | 无声明时按 512k/1M 字符做上下文冒烟，区分 accepted/rejected/timeout |
| **thinking** | 双发对比（disable vs enable），判定 supports/forces/rejects |
| **tools** | 最小无副作用 tool，判定 native/text_only/rejected |
| **vision** | 内嵌 1×1 PNG，验证是否接受 image（默认关） |
| **model-consistency** | 请求模型 vs 响应 model 字段，抓静默路由异常 |

---

## 快速开始

### 环境要求

- **Python 3.10+**（纯标准库，零第三方依赖）
- Windows / macOS / Linux
- 已安装并配置过 [cc-switch](https://github.com/farion1231/cc-switch)

### 安装

```bash
git clone https://github.com/mengyuchun/cc-pulse.git
cd cc-pulse
# 无需 pip install —— 纯标准库
python check_ccswitch_health.py check --help
```

### 三秒上手

```bash
# 日常体检：只测故障转移队列 + 当前激活（最快）
python check_ccswitch_health.py check --failover-only

# 全量体检
python check_ccswitch_health.py check

# JSON 报告（stdout 是 JSON，stderr 是人类可读进度）
python check_ccswitch_health.py check --failover-only --json | jq '.summary'

# 单一模型深度诊断（人类可读输出）
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" --human

# 1M 上下文冒烟 + 开启 vision
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" \
    --probe-context 1m --include text,streaming,metadata,thinking,tools,vision
```

> Windows 用户也可双击 `run_health_check.ps1` 用交互菜单，无需记参数。

---

## 子命令详解

### `check` —— 日常健康检测

按档位回退顺序探测，**首个成功档位即停**，报告所有已尝试档位。

```bash
python check_ccswitch_health.py check --failover-only        # 队列+当前（推荐）
python check_ccswitch_health.py check                          # 全部 claude
python check_ccswitch_health.py check --type all              # claude + codex + openclaw
python check_ccswitch_health.py check --failover-only --json  # 机器可读
```

**实时输出示例**：

```
进度: 每档完成立即显示，供应商完成显示汇总

  · 供应商A              haiku  [401] 1.2s Invalid API key
  · 供应商B              haiku  [ok] 2.1s 回答:"5"
[ 1/8] ✅ 供应商B                 ✓haiku 回答:"5"
  · 供应商A              sonnet [429] 1.6s Weekly limit reached
  · 供应商C              haiku  [答案不符] 2.4s "..."
[ 2/8] ❌ 供应商A                 haiku:401(...) | sonnet:429(...)
```

**参数**：

| 参数 | 说明 | 默认 |
|------|------|------|
| `--type claude\|codex\|openclaw\|all` | 检测哪类供应商 | `claude` |
| `--failover-only` | 只测故障转移队列 + 当前激活 | 关 |
| `--json` | stdout 输出结构化 JSON，stderr 保留人类文本 | 关 |
| `--workers N` | 并发数 | 6 |
| `--timeout SEC` | 单请求超时秒 | 30 |
| `--probe-max-tokens N` | 探测 token 预算（thinking 模型可调高） | 20 |
| `--probe-enable-thinking` | 允许 thinking 模式 | 关 |
| `--user-agent UA` | 覆盖 UA（默认读本机 `claude --version`） | 自动 |
| `--skip-tls-verify` | ⚠️ 跳过 TLS 证书验证 | 关 |

### `list-models` —— 拉取模型目录

```bash
python check_ccswitch_health.py list-models
python check_ccswitch_health.py list-models --failover-only --type all
```

### `inspect` —— 单模型深度诊断

```bash
# 默认：text + streaming + 路由 + metadata + thinking + tools
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6"

# 人类可读输出
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" --human

# 1M 上下文冒烟（无声明窗口时触发）
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" --probe-context 1m

# 显式开启 vision
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" \
    --include text,streaming,metadata,thinking,tools,vision
```

**参数**：

| 参数 | 说明 | 默认 |
|------|------|------|
| `--provider NAME` | 供应商名称（与 cc-switch 一致） | 必填 |
| `--model ID` | 模型 ID（可含 `[1M]` 等后缀） | 必填 |
| `--source configured\|listed\|manual` | 模型来源 | `configured` |
| `--type claude\|codex\|openclaw\|all` | 限定供应商类型 | `claude` |
| `--include LIST` | 检查项（见下表） | 默认全开（不含 vision） |
| `--probe-context 512k\|1m` | 上下文冒烟档位 | `512k` |
| `--keep-suffix` | 保留模型 ID 的 `[1M]` 后缀 | 关 |
| `--ttft-timeout SEC` | 流式首 token 超时 | 用 `--timeout` |
| `--human` | 人类可读输出（默认 JSON） | 关 |

**`--include` 检查项**：

| 项 | 默认 | 说明 |
|----|------|------|
| `text` | ✅ | 真实问题 + usage 解析 |
| `streaming` | ✅ | SSE / 非 SSE 流式、TTFT |
| `model-consistency` | ✅ | 请求 vs 响应模型比对 |
| `protocol` / `error-classification` | ✅ | 协议推断与错误分类 |
| `metadata` | ✅ | `GET /v1/models/{id}` 声明值 |
| `thinking` | ✅ | disable + enable 双发对比 |
| `tools` | ✅ | 最小无副作用 tool 协议探测 |
| `vision` | ❌ | 内嵌 1×1 PNG，`--include ...,vision` 才开 |

**`--source` 三种来源**：

| 值 | 行为 |
|---|---|
| `configured` | 在 cc-switch 配置的模型档位中精确匹配（不连网） |
| `listed` | 先 `GET /v1/models`，在返回列表中查找 |
| `manual` | 强制使用 `--model` 字面值（高级用户） |

> ⏱ **累计超时提示**：`inspect` 按默认 include 发 5-6 个串行请求，总最大耗时 ≈ N × `--timeout`。例如 `--timeout 30` 时最坏约 180 秒。如需更快，用 `--include text` 只跑单项。

---

## 输出示例

### 人类可读（`--human`）

```
============================================================
  Provider:  Relay-A
  Model:     claude-sonnet-4-6 (configured)
  Protocol:  anthropic_messages · confirmed
============================================================

[1/7] 文本探测
  状态：✅ pass · 1.24s
  答案："5" · 正确
  usage：in=20 out=3

[2/7] 流式探测
  状态：✅ pass · TTFT 0.42s · 总 1.31s

[3/7] 模型路由比对
  匹配：exact_match

[4/7] 模型元数据
  声明上下文窗口：200,000 tokens（供应商声明，非实测）

[5/7] Thinking
  verdict：supports_disable

[6/7] Tool use
  状态：✅ pass · support=native

[7/7] Vision · skipped

------------------------------------------------------------
  总结：healthy
============================================================
```

### JSON 报告字段

| 字段 | 含义 |
|------|------|
| `protocol.detected` | `anthropic_messages` / `openai_responses` / `openai_chat_completions` / `unknown` |
| `protocol.confidence` | `inferred` / `confirmed`（文本探测成功时升级） |
| `text.status` | `pass` / `fail` / `error` |
| `text.answer` / `text.correct` | 抽取的回答 / 是否等于 `"5"` |
| `streaming.ttft_seconds` | 首 token 延迟（秒） |
| `streaming.response_model` / `event_count` / `is_sse` | 流式响应模型 / 事件数 / 是否真 SSE |
| `metadata.declared_context_window` | 供应商**声明**的窗口（非实测） |
| `metadata.capabilities` | `{"image_input": true, "thinking": true, ...}` |
| `context.status` | `accepted` / `rejected` / `timeout` / `error` / `skipped` |
| `context.approx_input_chars` / `token_estimate` | 冒烟体量与上界说明 |
| `thinking.verdict` | `supports_disable` / `forces_thinking` / `rejects_thinking_field` / `breaks_on_short_budget` |
| `tools.protocol_support` | `native` / `text_only` / `rejected` / `unknown` |
| `vision.status` | `pass` / `fail` / `error` / `skipped` / `unsupported` |
| `usage.present` / `input_tokens` / `output_tokens` | 是否解析到真实 token 计数 |
| `model_consistency.match` | `exact_match` / `alias_match` / `fuzzy_match` / `mismatch` / `unverifiable` |
| `summary.verdict` | `healthy` / `available_but_wrong_answer` / `unavailable` / `skipped` |
| `summary.recommended_actions` | 基于结果的可执行建议列表 |

### 错误分类枚举（`error_category`）

每个探测结果的 `error_category` 是下列之一：

```
none | network | tls | authentication | rate_limit | model_not_found |
protocol_incompatible | server_error | invalid_response | answer_mismatch |
stream_protocol | ttft_timeout | stream_incomplete | unknown
```

---

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部健康（`check` 至少一个供应商可用 / `inspect` healthy 或 skipped / `list-models` 完成） |
| 1 | 健康检查全部失败 / `inspect` 不可用 / 答案错误 |
| 2 | 数据库不存在、没有符合条件供应商、resolve 失败（inspect 找不到目标） |

---

## Windows 桌面启动器

`run_health_check.ps1` 提供交互式菜单，双击即可，无需记参数：

```
[1] 健康检测 · 快速体检   一键（claude/队列/不JSON/不thinking）
[2] 健康检测 · 自定义     选类型/范围
[3] 拉模型列表            GET /v1/models 目录
[4] 深度诊断 (inspect)    单一 (provider, model) 诊断
[5] 高级设置              JSON/thinking/UA/max-tokens/context/vision
[6] 退出
```

- 优先用 `CC_PULSE_PYTHON` 指定的解释器，其次 PATH 中的 `python`
- 数据库路径可用 `CC_PULSE_DB` 覆盖
- 超时可用 `CC_PULSE_TIMEOUT` 覆盖（秒）
- 使用 `python -u` 无缓冲输出，进度实时可见

### 环境变量

| 变量 | 作用 |
|---|---|
| `CC_PULSE_PYTHON` | 启动器优先使用的 Python 解释器路径 |
| `CC_PULSE_DB` | 启动器默认的 cc-switch.db 路径 |
| `CC_PULSE_TIMEOUT` | 启动器默认超时秒 |
| `CC_PULSE_PWSH` | 测试用的 pwsh 路径 |

---

## 设计原则（刻意为之，非 bug）

- **只读、零侵入**：以 `file:...?mode=ro` 打开数据库，绝不修改 cc-switch
- **路径不去重**：一律 `base_url + /v1/messages`，`xxx/v1` 会拼成 `/v1/v1/messages`——故意对齐真实 Claude Code 行为
- **错误原文透传**：JSON 错误的 `message` 完整不截断；HTML/非 JSON 显示前 500 字符并标注真实长度
- **不写文件**：结果只打印到终端 / stdout，不落盘
- **Claude Code 指纹头**：附本机 `claude --version` 探测的 UA（可用 `--user-agent` 覆盖），降低 Cloudflare 1010 误判
- **默认验证 TLS**：`--skip-tls-verify` 需显式开启（会暴露认证凭据）
- **终端安全**：`say()` 输出自动剥离 ANSI 转义和控制字符，防止恶意供应商响应注入终端指令

## 诚实的局限

- `check` 以「能否回答一道简单算术题」为主；`inspect` 额外覆盖流式/元数据/上下文/thinking/tool/vision，但不覆盖多轮往返与并发承载
- `metadata.declared_context_window` 是供应商**声称**的值；无声明时的 context 冒烟按 **1 字符 ≈ 1 token 上界** 逼近，**不是精确 tokenizer 计数**
- Claude Code 指纹头并非 100% 完整，个别强校验站仍可能误判为非法客户端
- `list-models` 列出的模型 ≠ 实际可用，只是供应商声称支持的清单
- `inspect` 不会自动执行 cc-switch 故障转移，只输出**只读诊断**
- thinking 模型在默认 `max_tokens=20` 下可能误判为不可用——提高 `--probe-max-tokens` 或开 `--probe-enable-thinking` 可减少误报

## 已知场景与应对

| 场景 | 现象 | 应对 |
|------|------|------|
| thinking 模型耗光 token | 200 但答案空 | `--probe-max-tokens 256` |
| 站点校验客户端 UA | 403 `client_restricted` | `--user-agent "codex_cli_rs/0.50.0"` 等 |
| 站点只认 x-api-key | 401 `Missing API key` | cc-switch 改用 `ANTHROPIC_API_KEY` 字段 |
| 模型被静默路由 | 请求与响应模型不一致 | inspect 的 `model_consistency` 会标 `mismatch` |
| OAuth token 放错字段 | 401 `invalid x-api-key` | 用 `ANTHROPIC_AUTH_TOKEN`（Bearer），不是 `ANTHROPIC_API_KEY` |
| 上下文缩水 | 声称 1M 实测 526k 拒 | inspect 的 context 冒烟会标 `rejected` |

---

## 测试

```bash
# 运行全部测试（Python 主逻辑 + PS1 启动器）
just test && just test-ps1

# 仅 Python 主逻辑（177 个单元 + 端到端 mock）
just test

# PS1 启动器端到端（31 个，需要 pwsh）
just test-ps1
```

测试纯标准库、自带 mock HTTP server，不触达任何真实供应商。当前 **208 个测试全部通过**（177 + 31）。

## 开发

```bash
# 格式化 + lint
just format
just lint
```

使用 [ruff](https://github.com/astral-sh/ruff) 作为格式化和 lint 工具（开发期依赖，运行时零依赖）。

---

## 项目结构

```
CC-Pulse/
├── check_ccswitch_health.py   # 主脚本：check / list-models / inspect 三子命令（~2200 行）
├── run_health_check.ps1       # Windows 桌面交互菜单启动器
├── justfile                    # 常用任务（检查、格式化、lint、测试）
├── requirements.txt           # 声明：纯标准库，无运行时依赖
├── tests/
│   ├── test_ccpulse_full.py   # 单元 + 端到端（Mock SSE / 多协议 / 多类型）
│   └── test_ps1_launcher.py   # PS1 启动器交互流程
├── CLAUDE.md                   # 项目级 Claude Code 指令
├── LICENSE                     # MIT License
└── README.md
```

---

## 同类项目对比

| 项目 | 形态 | 对比 |
|------|------|------|
| [all-api-hub](https://github.com/qixing-jk/all-api-hub) | 浏览器扩展 | 功能最全、带 Cloudflare 处理，但不读 cc-switch 数据库 |
| [cc-test](https://github.com/zhoujun681/cc-test) | Rust CLI | 定位相近，但无多档回退、不校验回答内容 |
| [cc-switcher](https://github.com/jimstratus/cc-switcher) | PowerShell | 以切换为主、测活为辅 |

CC-Pulse 的取舍：**小而专，只做 cc-switch 供应商的深度探活**（多档回退 + 校验回答 + 认证按配置走 + 单一模型 7 维度诊断），不做管理、切换、界面。

---

## 贡献

欢迎提 Issue 和 PR。请确保：

1. `just test` 全绿
2. `just lint` 无新增告警
3. 新功能补对应测试
4. 遵循现有代码风格（ruff format）

## 许可证

[MIT License](LICENSE) © 2026 Yuchun Meng

<div align="center">

# CC-Pulse

**给 [cc-switch](https://github.com/farion1231/cc-switch) 供应商听「心跳」的健康检测与单模型深度诊断工具**

不信「能连上」，只信「能使用」。那么多供应商，一眼看清哪些真能用。

[中文](README.md) · [English](README.en.md)

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/stdlib%20only-green.svg)](#)
[![Tests](https://img.shields.io/badge/tests-659%20pass-brightgreen.svg)](#测试)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

---

## TL;DR 快速上手

```bash
git clone https://github.com/mengyuchun/cc-pulse.git
cd cc-pulse
python check_ccswitch_health.py check --failover-only   # 日常体检，最快
```

装了 [just](https://just.systems/) 的话，一行代替上面三条：`just check`。

> 见到结果后想深挖单模型：`python check_ccswitch_health.py inspect --provider "Relay-A" --model "claude-sonnet-4-6" --human`

---

## 术语小词典

| 术语 | 含义 |
|------|------|
| **cc-switch** | 管理多个 Claude Code / Codex / OpenClaw 中转供应商的桌面切换工具，维护一份 `cc-switch.db` |
| **中转站** | 转发 LLM API 请求的第三方服务，常常悄悄改路由、改模型、改认证 |
| **故障转移队列** | cc-switch 里排队待用的一组供应商；`--failover-only` 只探它们 + 当前激活 |
| **静默路由** | 你切了 A，但流量被导到别处——典型来源是终端环境变量（`ANTHROPIC_BASE_URL` 等）覆盖了 cc-switch 的选择 |
| **档位** | `haiku → sonnet → opus → fable → default` 模型优先级回退顺序，CC-Pulse 逐档探到首个正确回答即停 |

---

## 为什么需要 CC-Pulse

cc-switch 帮你管理一堆 Claude Code / Codex 的 API 中转供应商。但中转站的水远比你想象的深：

- 🔇 **200 ≠ 可用**：有的站返回 200 却空回答、答非所问、或静默路由到更便宜的模型
- 🎭 **多档位陷阱**：haiku 能用 sonnet 不能用、opus 限流、fable 不存在
- 🔑 **认证方式各异**：有的只认 `x-api-key`，有的只认 `Authorization: Bearer`，有的校验客户端 UA
- 🧠 **thinking 模型**：DeepSeek/GLM 等用 20 token 预算去思考，啥答案都输出不了
- 📏 **上下文缩水**：声称 1M，实际 526k 就拒了
- 🛠️ **Tool/Vision 兼容性**：写代码强依赖 tool_use，但很多站根本不真支持

CC-Pulse 不信「能连上」，只信「能使用」。那么多供应商，一眼看清哪些真能用。

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
- 从**问题池随机抽题**（算术为主，中英混合），答案宽松匹配（提取唯一数字），**连通(200) ≠ 可用**
- 认证按 cc-switch 配置走：`ANTHROPIC_AUTH_TOKEN` → `Bearer`，`ANTHROPIC_API_KEY` → `x-api-key`
- **弱指纹**：请求体贴近真实 claude-cli（`max_tokens=1024`、仅对 DeepSeek/GLM 等 thinking 模型发抑制字段），降低被中转站识别成「测活脚本」的概率
- **`--stealth` 隐身模式**：降并发 + 请求间随机延迟，弱化脚本式流量尖峰（较慢，怀疑被封时用）
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
| **authenticity** | 模型保真鉴别（见下节，默认探 P0/P1，P2 探针可选开启） |

### 4. 模型保真鉴别 `authenticity` —— 中转站有没有偷偷换芯/降智

中转站最大暗坑：标榜 Claude Sonnet 4.5，后端却换芯到 GPT / 旧版 / 量化模型 / 甚至脚本兜底。`inspect` 报告的 `authenticity` 字段用 6 个探针做概率性鉴别——**结论是疑似信号，非确定性判定**（GhostPrint 类攻击理论上可骗过）。

| 探针 | 默认 | 原理 | 信号 |
|------|------|------|------|
| **换芯字段** `crosspack` | ✅ 开 | 原生 OpenAI 回包只有 prompt/completion/total 三键；冒出 `cache_creation_input_tokens`/`usage_source: anthropic` 等不该出现的字段 → 中转把 OpenAI 请求偷转 Claude 后端再包回。零新请求，复用 text/streaming raw body | 可疑字段 → suspicious |
| **thinking 签名** `thinking_signature` | ✅ 开 | Claude 扩展思考块的 `signature` 是 Anthropic 服务端密钥签名，客户端只能验证不能生成。中转无法伪造，只能原样透传真 Claude 产出。复用 thinking enable 响应，零新请求 | 有 thinking 块但无签名 → suspicious |
| **usage 自洽** `usage_consistency` | ✅ 开 | 极短回复（只回 OK）却报高 completion_tokens → 扣费不可解释。复用 text_raw 的 usage，零新请求 | 计费不自洽 → suspicious |
| **缓存回放/钳温** `cache_replay` | ❌ `--include cache-replay` | temp=1 发两次同一 prompt，逐字完全相同 → 疑似缓存回放或强制钳温（真模型 temp=1 应有随机差异）。2 次新请求 | 双发逐字相同 → suspicious |
| **知识截止** `knowledge_cutoff` | ❌ `--include knowledge-cutoff` | before 题（2023 年前事实）所有模型都该答对，答错 → 异常；after 题（2024-2025 事件）只有较新模型该会。区分模型版本/世代。6 次新请求 | before 错 → suspicious；after 全错 → note 旧模型 |
| **分布指纹** `js_fingerprint` | ❌ `--include js-fingerprint` | 问 N 次"给个 1-100 随机数"，真 LLM 永远不均匀（有偏置），脚本 `random.randint` 才近似均匀。JSD 判定更接近均匀 than LLM 偏置参考 → 疑似非 LLM 后端。默认 50 次（`--js-samples N`），<20 不判定 | 分布近均匀 → suspicious |

```bash
# 默认：crosspack + thinking_signature + usage_consistency（零/低新请求，常开）
python check_ccswitch_health.py inspect --provider "Relay-A" --model "claude-sonnet-4-6" --human

# 全量保真鉴别（额外烧 ~58 次请求，高置信）
python check_ccswitch_health.py inspect --provider "Relay-A" --model "claude-sonnet-4-6" \
    --include text,streaming,metadata,thinking,tools,cache-replay,knowledge-cutoff,js-fingerprint \
    --js-samples 200 --human
```

报告 `authenticity.verdict` ∈ `clean` / `suspicious` / `inconclusive`，`evidence` 列出每条可疑证据。详见 [docs/PRD-authenticity-p0.md](docs/PRD-authenticity-p0.md) ~ `p2c.md`。

**能力边界**（写进报告，不夸大）：高档大模型 8-bit 量化 / 同世代邻档降智（haiku vs sonnet）少量黑盒请求测不出；GhostPrint 攻击（arXiv:2606.16100）理论上可微调弱模型骗过指纹；temperature=0 只能单向证伪不能反证保真。

---

## 快速开始

### 环境要求

- **Python 3.10+**（运行时仅用标准库，无需 `pip install`）
- Windows / macOS / Linux
- 已安装并配置过 [cc-switch](https://github.com/farion1231/cc-switch)（默认读 `~/.cc-switch/cc-switch.db`）

> 使用门槛：本机需要可用的 Python。Windows 用户若已装 Claude Code / 开发环境，通常已有 Python；也可直接双击 `run_health_check.ps1`（会自动找 PATH / `CC_PULSE_PYTHON` 里的解释器）。

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
# 装了 just 的话，等价于：just check

# 全量体检
python check_ccswitch_health.py check
# 等价于：just check-all
```

> 下方示例中的 `Relay-A` / `Relay-B` / `claude-sonnet-4-6` / `glm-5` 均为**占位符**，请替换为你自己在 cc-switch 里的供应商名与模型 ID。

```bash
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

### 场景 → 命令决策表

**最近突然不能用？先 `just env-check` 再 `just trend`**——前者查环境变量是否静默覆盖了 cc-switch 选择，后者看跨天降级趋势。

| 你想做的事 | 用哪个命令 | 一句话 |
|-----------|-----------|--------|
| 想知道现在哪家供应商能用 | `just check` | 多档回退探活 + 校验真实回答，一眼看清谁真能用 |
| 拉供应商声明的模型清单 | `just models` | `GET /v1/models`，列出的 ≠ 能用 |
| 给某供应商某模型做深度诊断 | `inspect` | 7 维度体检：text/streaming/metadata/context/thinking/tools/vision + 路由比对 |
| 看最近失败日志 / 历史成功率 | `history` / `just stats` | 只读 cc-switch 库，按供应商 / 时间窗汇总 |
| 看 cc-switch 被动健康状态 | `health` | 只读 `provider_health`，不发 HTTP；不替代主动探测 |
| 排查静默路由 | `just env-check` / `routing` | env-check 查环境变量覆盖，routing 看请求 vs 响应模型不一致排行 |
| 看跨天是否有降级趋势 | `just trend` | 读本地探测归档，按天聚合成功率 / 延迟 / 错误分类 |
| 看中转站有没有偷偷换芯/降智 | `inspect --include ...,cache-replay,knowledge-cutoff,js-fingerprint` | 6 探针保真鉴别，默认开 P0/P1，P2 烧请求时显式开 |
| 实时盯着 cc-switch 日志 | `just watch` | 每 3 秒轮询，有新日志就打印，Ctrl+C 结束 |
| 多维交叉聚合分析 | `just analyze` | 按天 / 模型 / 供应商×日期矩阵，自带 sparkline |
| check 后批量深挖失败/可用供应商 | `deep-dive` | 读 check JSON 逐个 inspect，CI 可串联 |
| cron 定时巡检 + 可用率告警 | `check --alert-threshold` | 低于阈值输出告警，cron 可据此触发 |

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
| `--failover-only` | 只测故障转移队列里的供应商（含当前激活） | 关 |
| `--current-only` | 只测当前激活的 1 个供应商（最窄；与 `--failover-only` 同时设时本项优先） | 关 |
| `--provider 名/子串` | 按供应商名过滤（逗号多选或子串） | 关 |
| `--select` | 交互式多选供应商（仅 TTY） | 关 |
| `--json` | stdout 输出结构化 JSON，stderr 保留人类文本 | 关 |
| `--workers N` | 并发数 | 6 |
| `--timeout SEC` | 单请求超时秒 | 30 |
| `--probe-max-tokens N` | 探测 token 预算（上限非实际消耗） | 1024 |
| `--probe-enable-thinking` | 允许 thinking 模式 | 关 |
| `--stealth` | 隐身模式：并发≤3 + 请求间随机延迟，弱化流量尖峰（较慢） | 关 |
| `--stainless-version V` | 覆盖 `x-stainless-package-version` 指纹头 | 内置默认 |
| `--user-agent UA` | 覆盖 UA（默认读本机 `claude --version`） | 自动 |
| `--skip-tls-verify` | ⚠️ 跳过 TLS 证书验证 | 关 |
| `--with-history` | 每个供应商后附加近 24h 日志摘要 | 关 |
| `--history-since` | 历史摘要时间窗 | `24h` |
| `--alert-threshold 0.8` | 可用率低于阈值（0-1）输出告警，cron 巡检可据此触发 | 关 |
| `--archive PATH` | 探测归档文件路径（默认 `~/.cc-pulse/probe_history.jsonl`，超 5MB/万条自动轮转） | 默认 |

### `list-models` —— 拉取模型目录

默认只执行 `GET /v1/models`；加 `--probe` 会逐模型做轻量文本验证，加 `--deep` 会逐模型执行 text / streaming / metadata / thinking / tools 五项诊断。探测模式可用 `--source configured|listed|both` 选择配置档位、接口声明或两者去重合并的模型来源。

```bash
python check_ccswitch_health.py list-models
python check_ccswitch_health.py list-models --failover-only --type all
python check_ccswitch_health.py list-models --select --probe
python check_ccswitch_health.py list-models --failover-only --deep --source both --timeout 60
```

### 交互式供应商选择 `--select`

`check` 和 `list-models` 可加 `--select`，以纯标准库的跨平台键盘选择器多选供应商：↑↓ 移动、空格切换、`a` 全选/取消全选、回车确认、Esc 取消。CLI 选择器**不支持鼠标**。`inspect` 请用 `--provider` 指定目标供应商。

它只在 stdin 和 stdout 都是 TTY 时启用；管道、CI 等非交互环境会跳过选择器，应改用 `--provider` 明确筛选。

### `deep-dive` —— check 后批量深挖（CI 可串联）

读 check 的 JSON 结果，按 `--target fail|ok|both` 过滤供应商，去重模型，逐个调 `inspect`。下沉自 PS1 的深挖流程，让 CI / 脚本能串联 `check --json | deep-dive`，不必走交互菜单。

```bash
# 与 PS1 交互等价的 CLI 串联
python check_ccswitch_health.py check --failover-only --json > check.json
python check_ccswitch_health.py deep-dive --from check.json --target fail
# 直接管道（stdin 读 check JSON）
python check_ccswitch_health.py check --failover-only --json | \
  python check_ccswitch_health.py deep-dive --from - --target both --yes
# 只看会跑哪些组合（dry-run，不执行）
python check_ccswitch_health.py deep-dive --from check.json --target both --json
```

| 参数 | 说明 | 默认 |
|------|------|------|
| `--from PATH\|-` | check JSON 文件路径，或 `-` 读 stdin | 必填 |
| `--target fail\|ok\|both` | 深挖失败 / 可用 / 全部供应商 | `fail` |
| `--models m1,m2` | 指定模型（逗号分隔，默认全部去重） | 全部 |
| `--yes` | 组合 >20 时跳过确认 | 关 |
| `--json` | 只输出任务列表 JSON，不执行 | 关 |

### `health` —— 被动健康度

只读 cc-switch 的 `provider_health` 表，查看真实代理流量汇总出的健康状态、连续失败和最近错误；不发 HTTP，也不替代 `check` 的主动答案验证。

```bash
python check_ccswitch_health.py health
python check_ccswitch_health.py health --json
```

### `history` / `stats` / `routing` / `watch` —— 只读 cc-switch 运行日志

不发 HTTP，只读 `~/.cc-switch/cc-switch.db` 里的 `proxy_request_logs`（及可选磁盘日志）。失败条目用 emoji + 颜色高亮（🔒AUTH / ⏳RATE / 📡NET / ❓MODEL / ⚠BAD / 💥5XX / ❌FAIL）。

```bash
# 最近 20 条（含路由不一致标记）
python check_ccswitch_health.py history

# 只看失败
python check_ccswitch_health.py history --fails --limit 50

# 按供应商名过滤 + 时间窗
python check_ccswitch_health.py history --provider Fengwind --since 24h

# 按供应商汇总成功率 / 主失败因 / 中位延迟 / 路由不一致率
python check_ccswitch_health.py stats --since 7d

# 静默路由排行（request_model => actual model）
python check_ccswitch_health.py routing --since 24h --limit 20

# 实时监控（每 3 秒轮询，有新日志就打印，Ctrl+C 结束）
python check_ccswitch_health.py watch
python check_ccswitch_health.py watch --fails --provider Fengwind --interval 5

# 可选：附加磁盘日志尾部（大文件只读末尾约 512KB）
python check_ccswitch_health.py history --fails \
  --log-file ~/.cc-switch/logs/cc-switch.log --log-lines 80
```

### `analyze` —— 多维度聚合分析

不发 HTTP，读 `proxy_request_logs` 做多维交叉聚合。自带 ASCII sparkline 趋势图。

```bash
# 全维度报表（按天 + 按模型 + 供应商×日期 交叉矩阵）
python check_ccswitch_health.py analyze --since 7d

# 只看按天健康趋势
python check_ccswitch_health.py analyze --mode day --since 30d

# 只看模型延迟分位数（p50/p95/p99）
python check_ccswitch_health.py analyze --mode model --since 7d

# 供应商×日期 成功率矩阵
python check_ccswitch_health.py analyze --mode provider-day --since 14d

# 单供应商深度报表（day × model 交叉）
python check_ccswitch_health.py analyze --provider Fengwind --since 30d

# JSON 输出
python check_ccswitch_health.py analyze --since 7d --json
```

| 参数 | 说明 | 适用 |
|------|------|------|
| `--limit N` | 条数 | history / routing |
| `--fails` | 只显示失败 | history |
| `--since 24h\|7d\|30m\|秒` | 时间窗口 | history / stats / routing / analyze |
| `--provider 子串` | 供应商名过滤 | history / analyze（深度模式） |
| `--mode all\|day\|model\|provider-day\|provider` | 分析维度 | analyze |
| `--json` | JSON 输出 | history / stats / routing / analyze |
| `--log-file PATH` | 磁盘日志尾部 | history |
| `--with-history` | check/inspect 后附 24h 摘要 | check / inspect |
| `--history-since` | `--with-history` 的时间窗口 | check / inspect |
| `--interval N` | 轮询间隔秒（默认 3） | watch |

失败原因会映射到与探测相同的 `error_category`（如 `authentication` / `rate_limit` / `network` / `model_not_found`）。

### `env-check` —— 环境变量覆盖检测

检测环境变量是否会覆盖 cc-switch 所选供应商——"静默路由"的最大来源（如终端里设置了 `ANTHROPIC_BASE_URL`/`AUTH_TOKEN`，会盖过你在 cc-switch 里切的供应商）。只读环境变量与配置，不发 HTTP。

```bash
# 人类可读
python check_ccswitch_health.py env-check

# JSON（findings + conflicts 计数）
python check_ccswitch_health.py env-check --json
```

退出码：有冲突（环境变量会覆盖 current provider）返回 **2**，否则 0。密钥只显示掩码（前 6 位 + `***`），绝不打印明文。

| 参数 | 说明 |
|------|------|
| `--json` | JSON 输出 |

### `trend` —— 探测历史趋势

`check`/`inspect` 每次探测会追加一行到本地归档（默认 `~/.cc-pulse/probe_history.jsonl`，绝不写 cc-switch 的库）。`trend` 读取归档，按供应商聚合成功率 / 延迟分位 / 错误分类 / 按天趋势——暴露"降级"而非单点快照。

```bash
# 近 7 天趋势
python check_ccswitch_health.py trend --since 7d

# 只看某供应商 / 某模型
python check_ccswitch_health.py trend --provider DeepSeek --since 30d

# 指定归档文件（与 check --archive 配合）
python check_ccswitch_health.py trend --archive ~/my_history.jsonl

# JSON 输出
python check_ccswitch_health.py trend --since 7d --json
```

`check`/`inspect` 可用 `--archive PATH` 覆盖归档路径，便于按项目/机器隔离历史。归档超 5MB 且 >10000 条时自动轮转保留最新（`trim_archive`），防长期膨胀。

trend 输出带趋势方向标记（`trend_direction`）：成功率按天首尾对比，`↑` 上升 / `↓` 下降 / `→` 稳定，一眼看降级。

| 参数 | 说明 | 默认 |
|------|------|------|
| `--since 24h\|7d\|30m\|秒` | 时间窗口 | `7d` |
| `--archive PATH` | 归档文件路径 | `~/.cc-pulse/probe_history.jsonl` |
| `--provider` | 只统计指定供应商 | 全部 |
| `--model` | 只统计指定模型 | 全部 |
| `--json` | JSON 输出 | 关 |

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

# 跨供应商对比（无需 --provider；默认只跑 text+streaming）
python check_ccswitch_health.py inspect \
    --compare "Relay-A/claude-sonnet-4-6,Relay-B/glm-5" --human
```

**参数**：

| 参数 | 说明 | 默认 |
|------|------|------|
| `--provider NAME` | 供应商名称（与 cc-switch 一致）；`--compare` 时可选 | 单模型必填 |
| `--model ID` | 模型 ID（可含 `[1M]` 等后缀） | 单模型必填 |
| `--compare A/m1,B/m2` | 跨供应商对比；目标自带 provider，无需 `--provider` | 关 |
| `--source configured\|listed\|manual` | 模型来源 | `configured` |
| `--type claude\|codex\|openclaw\|all` | 限定供应商类型 | `claude` |
| `--include LIST` | 检查项（见下表） | 默认全开；`--compare` 默认 `text,streaming` |
| `--probe-context 512k\|1m` | 上下文冒烟档位 | `512k` |
| `--keep-suffix` | 保留模型 ID 的 `[1M]` 后缀 | 关 |
| `--js-samples N` | `js-fingerprint` 采样次数（默认 50；建议 ≥200 高置信，<20 不判定） | `50` |
| `--ttft-timeout SEC` | 流式首 token 超时 | 用 `--timeout` |
| `--with-metadata` | 兼容旧命令；metadata 默认已开启，不会额外发请求 | 关 |
| `--probe-delay SEC` | 批量模型间延迟 | `3.0` |
| `--max-retries N` | 429 重试次数 | `1` |
| `--format human\|json` | 输出格式 | `json` |
| `--human` | 人类可读输出（默认 JSON） | 关 |
| `--quiet` | 批量静默 NDJSON + 退出码 0/1/3 | 关 |

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
| `cache-replay` | ❌ | temp=1 双发查缓存回放/钳温（2 次额外请求） |
| `knowledge-cutoff` | ❌ | before/after 题库查模型版本/世代（6 次额外请求） |
| `js-fingerprint` | ❌ | 单 token 随机数分布指纹 JSD（默认 `--js-samples` 50 次额外请求） |

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

[7/7] 保真鉴别（authenticity）
  verdict：clean
  · 换芯字段：无异常（OpenAI 响应未出现 Anthropic 专属字段）
  · thinking 签名：有效签名（确为真 Claude 服务端产出）
  · usage 自洽：completion_tokens 与回答长度匹配

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
| `authenticity.verdict` | `clean` / `suspicious` / `inconclusive`（保真鉴别汇总） |
| `authenticity.crosspack` / `thinking_signature` / `usage_consistency` | 默认开启的 P0/P1 探针（零/低新请求） |
| `authenticity.cache_replay` / `knowledge_cutoff` / `js_fingerprint` | P2 探针，`--include` 显式开启 |
| `authenticity.evidence` | 可疑证据列表（人类可读） |
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
| 0 | 全部成功（`check` 至少一个供应商可用 / `inspect` healthy 或 skipped / `list-models` 完成） |
| 1 | 部分失败（`check` 有供应商不可用 / `inspect` 部分模型失败 / 答案错误） |
| 2 | 环境/参数错误（数据库不存在、没有符合条件供应商、resolve 失败） |
| 3 | 全部失败（`inspect` 所有模型失败） |
| 4 | 用户取消（PS1 启动器 ESC 取消） |

> 批量/对比模式用 1/3 区分粒度，方便 CI 与 `&&` 链判断。配 `--quiet` 输出纯 NDJSON，每模型一行 JSON 到 stdout，关闭所有进度提示。

---

## Windows 桌面启动器

`run_health_check.ps1` 提供交互式菜单，双击即可，无需记参数（需要 PowerShell 7+）：

```
[1] 体检              快速/自定义，检测完可深挖
[2] 深度诊断          拉模型列表/inspect
[3] 运行日志          失败/统计/路由/实时监控
[4] 高级设置          JSON/stealth/thinking/UA
[5] 退出
```

### 菜单路径

| 入口 | 层级与行为 |
|------|------------|
| 快速体检 | 直接读取高级设置的类型和范围；默认 `claude` + 故障转移队列/当前激活 |
| 自定义健康检测 | 类型 → 队列+当前 / 全部 / 仅当前 / 自选供应商（可多选） |
| 拉模型列表 | 类型 → 范围 → 仅列表 / 轻量探测 / 深度探测；探测时再选 `listed/configured/both` 来源 |
| 深度诊断 | 类型 → 供应商；多供应商时选档位并逐个检测供应商×档位模型，单供应商可选单模型、全部模型或自选模型和维度 |
| 运行日志 | 最近失败、最近全部、统计、静默路由、实时监控、分析报表 |
| 高级设置 | JSON、token 预算、thinking、UA、inspect 上下文/vision、保真探针（缓存回放/知识截止/分布指纹）、check stealth，以及快速体检类型/范围；仅当前启动器进程有效，关闭窗口后重置 |

### 交互方式

交互式终端支持 ↑↓/滚轮移动、回车确认、Esc 或右键取消；多选再用空格切换、`a` 全选/取消全选，鼠标左键可选中或切换。输入被重定向时，普通菜单和列表会改走编号或文本输入兼容路径；但 inspect 的多供应商档位与自选维度分支要求交互式控制台。不要把启动器菜单当作无人值守自动化接口。

- 优先用 `CC_PULSE_PYTHON` 指定的解释器，其次 PATH 中的 `python`
- 数据库路径可用 `CC_PULSE_DB` 覆盖
- 超时可用 `CC_PULSE_TIMEOUT` 覆盖（秒；健康检测默认值）
- 使用 `python -u` 无缓冲输出，进度实时可见

### 环境变量

| 变量 | 作用 |
|---|---|
| `CC_PULSE_PYTHON` | 启动器优先使用的 Python 解释器路径 |
| `CC_PULSE_DB` | 启动器默认的 cc-switch.db 路径 |
| `CC_PULSE_TIMEOUT` | 健康检测的默认超时秒 |
| `CC_PULSE_PWSH` | 测试用的 pwsh 路径 |

`CC_PULSE_*` 是启动器变量；供应商路由与认证变量（如 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`）由 `env-check` 审计，二者并不相同。

---

## 设计原则（刻意为之，非 bug）

- **只读、零侵入**：以 `file:...?mode=ro` 打开数据库，绝不修改 cc-switch
- **路径不去重**：一律 `base_url + /v1/messages`，`xxx/v1` 会拼成 `/v1/v1/messages`——故意对齐真实 Claude Code 行为
- **错误原文透传**：JSON 错误的 `message` 完整不截断；HTML/非 JSON 显示前 500 字符并标注真实长度
- **本地归档，不改 cc-switch 数据**：数据库始终以 `file:...?mode=ro` 打开；`check`/`inspect` 会追加 CC-Pulse 自己的 `~/.cc-pulse/probe_history.jsonl` 供 `trend` 使用，`--archive PATH` 可覆盖位置，绝不写入 cc-switch 数据库
- **Claude Code 指纹头**：附本机 `claude --version` 探测的 UA（可用 `--user-agent` 覆盖），降低 Cloudflare 1010 误判；`x-stainless-*` 版本可用 `--stainless-version` 覆盖（无法从 `claude --version` 推导 SDK 版本）
- **默认验证 TLS**：`--skip-tls-verify` 需显式开启（会暴露认证凭据）
- **终端安全**：`say()` 输出自动剥离 ANSI 转义和控制字符，防止恶意供应商响应注入终端指令
- **弱化测活指纹**（默认常开）：问题池随机抽题（避免固定 prompt 被子串匹配）、`max_tokens` 用自然值 1024、仅对 thinking-prone 模型（deepseek/glm 等）发思考抑制字段。这些是「更像真实客户端且不降速」的改进
- **`--stealth` 时序伪装**（可选）：并发收敛到 ≤3 + 每请求随机延迟 0.3~1.5s，弱化脚本式流量尖峰。代价是全量体检慢 2~3 倍，仅在怀疑被识别时开

## 诚实的局限

- `check` 以「能否回答一道简单算术题」为主；`inspect` 额外覆盖流式/元数据/上下文/thinking/tool/vision，但不覆盖多轮往返与并发承载
- `metadata.declared_context_window` 是供应商**声称**的值；无声明时的 context 冒烟按 **1 字符 ≈ 1 token 上界** 逼近，**不是精确 tokenizer 计数**
- Claude Code 指纹头并非 100% 完整，个别强校验站仍可能误判为非法客户端
- `list-models` 列出的模型 ≠ 实际可用，只是供应商声称支持的清单
- `inspect` 不会自动执行 cc-switch 故障转移，只输出**只读诊断**
- thinking 模型即便默认 `max_tokens=1024`，仍可能耗光预算无最终答案——脚本已对 deepseek/glm 等 thinking-prone 模型自动发思考抑制字段；仍误判时可开 `--probe-enable-thinking`
- 弱化测活指纹只能**降低**被识别概率，不能消除：验证「能否正确回答」本质上需要答案可判定的 prompt，这类流量在真实用户中占比低、模式相对固定
- 保真鉴别是**概率信号**：8-bit 量化 / 同世代邻档降智测不出；GhostPrint 攻击理论上可骗过指纹审计；不承诺"确定性判定真伪"

## 技术路线

### 设计铁律（刻意为之，不可破）

- **零运行时第三方依赖**：纯 Python 标准库（`argparse`/`sqlite3`/`urllib`/`ssl`/`threading`/`concurrent.futures`/`contextvars`/`math`/`re`），不引 click/typer/rich/aiohttp
- **key 不出本机**：API key 仅用于请求头，绝不写日志/报告/归档；响应体经 `_sanitize_raw_body` 脱敏后才进 `raw_body`
- **只读 cc-switch SQLite**：以 `file:...?mode=ro` 打开，绝不改库；趋势数据写 CC-Pulse 自己的 `~/.cc-pulse/probe_history.jsonl`
- **不自动切换供应商**：只输出只读诊断与切换指令，不替用户做决策
- **不做 Web 端 / HTML 报告**：CLI + JSON/NDJSON 是唯一产品形态

### 保真鉴别技术路线（P0 → P2-C 已全部上线）

按"性价比"排序，先做不破零依赖、key 不出机、确定性强的判据：

| 阶段 | 探针 | 判据类型 | 请求成本 | 状态 |
|------|------|---------|---------|------|
| **P0** | thinking 签名验签 | 密码学级（不可伪造） | 0（复用 thinking 响应） | ✅ `50a6e21` |
| **P0** | 换芯字段检测 | 字段级 JSON 解析 | 0（复用 text/streaming raw body） | ✅ `50a6e21` |
| **P1** | usage 自洽静态校验 | 计费一致性 | 0（复用 text_raw usage） | ✅ `7c802c1` |
| **P2-A** | 缓存回放/钳温双发 | temp=1 双发逐字比对 | 2 次 | ✅ `1bc1e6a` |
| **P2-B** | 知识截止 before/after 题库 | 事实知识分代 | 6 次 | ✅ `ada4e60` |
| **P2-C** | 单 token 随机数分布指纹 JSD | 分布散度（论文 One Token Is Enough） | 50 次（可调） | ✅ `cf09d8a` |

**设计取舍**：
- **自包含判据优先**：P2-C 用程序化构造的 `LLM_BIAS_REFERENCE`（偏好数 ×3 / 整数 ×0.3 归一化）+ 均匀分布双假设 JSD，不内置各家专属指纹库——规避版权与维护成本
- **确定性 > 概率性**：P0 thinking 签名是密码学级铁证（中转无法伪造），优先级最高；P2 分布指纹是概率信号，放最后
- **零新请求优先**：P0/P1 全部复用已有响应体，零额外成本；P2 才开始烧请求
- **能力边界明确**：8-bit 量化 / 同世代邻档 / GhostPrint 攻击是公认天花板，写进报告不夸大

### 不做清单（YAGNI）

- 不做需要 logprobs 的方法（多数中转站不给 logprobs）
- 不做需要本地真模型的方法（RUT/IRIS/perplexity 需 torch/GPU，违背零依赖）
- 不做"确定性判定真伪"——只输出概率信号 + 证据
- 不做 Web/HTML 报告产品
- 不做供应商管理 / 切换 / 界面（cc-switch 的领域）

详见 [docs/PRD-authenticity-p0.md](docs/PRD-authenticity-p0.md) ~ [p2c.md](docs/PRD-authenticity-p2c.md)，调研结论见内部竞品调研笔记。

## 已知场景与应对

| 场景 | 现象 | 应对 |
|------|------|------|
| thinking 模型耗光 token | 200 但答案空 | 默认已对 deepseek/glm 自动抑制思考；仍空可 `--probe-enable-thinking` |
| 站点识别测活脚本后封禁 | 突然大面积 401/403 | `--stealth`（降并发 + 随机延迟）；问题池/自然 max_tokens 已默认常开 |
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

# 仅 Python 主逻辑（单元 + 端到端 mock）
just test

# PS1 启动器端到端（需要 pwsh）
just test-ps1

# 文档一致性守卫（防 README 漂移）
just lint-docs
```

测试纯标准库、自带 mock HTTP server，不触达任何真实供应商。当前 **612 个 Python 测试 + 47 个 PS1 测试**，按文件分布：

| 测试文件 | 用途 | 用例数 |
|---------|------|--------|
| `tests/test_ccpulse_full.py` | 单元 + 端到端（Mock SSE / 多协议 / 多类型） | 398 |
| `tests/test_archive_trend.py` | 探测归档与趋势聚合 | 54 |
| `tests/test_authenticity.py` | P0 换芯字段 + thinking 签名验签 | 14 |
| `tests/test_usage_consistency.py` | P1 usage 自洽静态校验 | 9 |
| `tests/test_cache_replay.py` | P2-A 缓存回放/钳温双发 | 8 |
| `tests/test_knowledge_cutoff.py` | P2-B 知识截止 before/after 题库 | 9 |
| `tests/test_js_fingerprint.py` | P2-C 单 token 随机数分布指纹 JSD | 10 |
| `tests/test_p0_protocol_fix.py` | P0 协议推断修复回归 | 2 |
| `tests/test_env_check.py` | 环境变量覆盖检测 | 14 |
| `tests/test_tui.py` | TUI 箭头/鼠标选择器 | 9 |
| `tests/test_ps1_launcher.py` | PS1 启动器交互流程 | 47 |
| `tests/test_docs_consistency.py` | 文档与代码一致性守卫 | — |

### `just` 常用命令速查

读 `justfile` 确认的命令名，装了 [just](https://just.systems/) 后可直接用：

| 命令 | 等价于 | 用途 |
|------|--------|------|
| `just check` | `check --failover-only --workers 8 --timeout 45` | 日常体检（最快） |
| `just check-all` | `check --workers 8 --timeout 45` | 全量体检 |
| `just check-stealth` | `check --failover-only --stealth` | 隐身模式（被识别时） |
| `just models` | `list-models --failover-only` | 拉队列内模型清单 |
| `just models-probe` | `list-models --failover-only --probe` | 拉清单 + 轻量探活 |
| `just models-deep` | `list-models --failover-only --deep` | 拉清单 + 五项深探 |
| `just trend` | `trend --since 7d` | 7 天探测趋势 |
| `just env-check` | `env-check` | 环境变量覆盖检测 |
| `just stats` | `stats --since 7d` | 7 天统计 |
| `just routing` | `routing --since 7d --limit 20` | 静默路由排行 |
| `just watch` | `watch --interval 3` | 实时监控 |
| `just analyze` | `analyze --since 7d` | 全维度分析 |
| `just test` | `python tests/test_ccpulse_full.py` | Python 测试 |
| `just test-ps1` | `python tests/test_ps1_launcher.py` | PS1 启动器测试 |
| `just test-all` | 7 个测试文件全跑 | 全套测试 |
| `just lint-docs` | `python tests/test_docs_consistency.py` | 文档一致性守卫 |
| `just format` / `just lint` | ruff format / ruff check | 开发期格式化与 lint |

> 没装 just 也无妨，所有命令的 `python check_ccswitch_health.py ...` 原形在上方子命令详解里。

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
├── check_ccswitch_health.py   # 主 CLI：argparse 命令编排、供应商解析、报告与日志分析
├── ccpulse_net.py             # 网络层：HTTP 请求、SSE 解析、错误分类、TLS 处理、响应体脱敏
├── ccpulse_probe.py           # 探测层：probe_tier/probe_stream、协议构造、inspect 7 维度、保真鉴别 6 探针
├── ccpulse_store.py           # 存储层：只读 cc-switch SQLite 访问、provider/tier 解析、日志查询
├── ccpulse_archive.py         # 归档层：本地 probe_history.jsonl 读写、轮转、趋势聚合
├── ccpulse_env.py             # 环境层：环境变量覆盖检测（静默路由排查）
├── ccpulse_output.py          # 输出层：ContextVar 路由、终端脱敏、ANSI 样式
├── ccpulse_tui.py             # TUI 层：跨平台箭头/鼠标多选选择器
├── run_health_check.ps1       # Windows 桌面交互菜单启动器（PowerShell 7+）
├── justfile                    # 常用任务（检查、格式化、lint、测试）
├── requirements.txt           # 声明：纯标准库，无运行时依赖
├── tests/                      # 见上方测试表
├── docs/
│   ├── CODEMAPS/              # 仓库架构地图（architecture / backend / data / dependencies）
│   ├── PRD-authenticity-p0.md ~ p2c.md   # 保真鉴别 6 探针的 PRD + TDD 验收
│   ├── product-research.md    # 内部竞品调研笔记
│   └── README.md              # docs/ 索引
├── CLAUDE.md                   # 项目级 Claude Code 指令
├── LICENSE                     # MIT License
├── README.md                   # 中文文档
└── README.en.md                # English docs
```

### 分层架构

```
┌──────────────────────────────────────────────────────────┐
│  CLI / PS1 启动器                                          │
│  check_ccswitch_health.py (argparse) / run_health_check.ps1│
└────────────┬────────────────────────────────────┬────────┘
             │ 命令分发                             │ 交互菜单
             ▼                                     ▼
┌──────────────────────────┐   ┌───────────────────────────┐
│ ccpulse_store (只读 SQLite)│   │ ccpulse_tui (选择器)        │
│ provider / tier / 日志查询 │   │ ccpulse_output (输出路由)    │
└─────────────┬────────────┘   └───────────────────────────┘
              │ Provider + ModelTier
              ▼
┌──────────────────────────────────────────────────────────┐
│  ccpulse_probe (探测层)                                    │
│  probe_tier / probe_stream / inspect 7 维度              │
│  保真鉴别: crosspack / thinking_signature / usage_c      │
│           cache_replay / knowledge_cutoff / js_fingerprint│
└────────────┬─────────────────────────────────┬───────────┘
             │ HTTP                            │ 归档
             ▼                                 ▼
┌──────────────────────────┐   ┌───────────────────────────┐
│ ccpulse_net (网络层)      │   │ ccpulse_archive (趋势归档)  │
│ HTTP / SSE / 错误分类     │   │ probe_history.jsonl         │
│ TLS / 响应体脱敏          │   └───────────────────────────┘
└──────────────────────────┘
```

四层职责清晰：`store` 只读解析配置 → `probe` 发请求做诊断 → `net` 处理传输与脱敏 → `archive` 落本地趋势。`check_ccswitch_health.py` 是唯一的命令编排入口，各层通过 `__getattr__` re-export 暴露给 importlib 测试。

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

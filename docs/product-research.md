# 产品需求调研：CC-Pulse 竞品分析

> 内部调研笔记：竞品分析，非用户文档
> 调研日期：2026-08-01 | 方法：4 路并行 agent（GitHub 仓库搜索 + README 深读 + 竞品对比）
> 结论：CC-Pulse 的"内容级答案校验 + 静默路由检测 + 7 维深诊"是**全生态独有**差异化；缺口集中在成本/趋势/告警/env 检测四个维度。

## 一、竞品矩阵

| 维度 | 产品 | star | 定位 | 与 CC-Pulse 关系 |
|---|---|---|---|---|
| 生态本体 | farion1231/cc-switch | 122K | 8 CLI 供应商切换桌面中枢 | CC-Pulse 所服务本体；内置端点测速/熔断/用量面板，但无内容校验 |
| 生态 CLI | SaladDay/cc-switch-cli | 4.5K | Rust CLI 全合一 | 定位最接近；`speedtest`/`stream-check`/`env check`/`sync-usage` 与 CC-Pulse 重叠，**无答案校验** |
| 生态 Web | Laliet/cc-switch-web | 496 | Web 版 | 流式健康检查 + 常见错误分类 |
| 本地路由 | musistudio/claude-code-router | 36K | 本地控制平面 | 请求日志含 latency/token/费用；用量面板 |
| AI 运行时 | openclaw/openclaw | 385K | 个人 AI 助手 | skill 生态含 `ping-model --compare`、`model-benchmark`、`diagnose-routing-and-fallback` |
| LLM 网关 | BerriAI/litellm | 55K | AI 网关 | `/health/endpoints` 实时健康；成本追踪 |
| 中转网关 | QuantumNous/new-api | 44K | 中转聚合 | 通道测活 + 失败自动禁用 + 自动调度 |
| 观测 | langfuse/helicone | 32K/6K | LLM 观测 | trace + 评测 + 成本 |
| 监控 | louislam/uptime-kuma | 90K | 自托管监控 | HTTP 探活 + 告警 + 历史图 + 状态页 |
| 监控 | TwiN/gatus | 11.7K | 开发者健康看板 | 单二进制、条件判定、失败阈值、webhook 告警 |
| 监控 | healthchecks/healthchecks | 10.2K | cron 死手机制 | push 式 ping + period/grace 去抖，一行 curl 接入 |
| 监控 | upptime/upptime | 17K | GitHub Actions 监控 | 配置即代码，结果提交 git 仓库作历史 |
| 基准 | idemerge/llm-api-bench | 7 | LLM 端点基准 | **最贴 CC-Pulse**：P50/P95/P99、TTFT、成本表、四级健康、历史留存 |
| 基准 | Yoosu-L/llmapibenchmark | 93 | 并发压测 | 多并发级压测 |
| 合成监控 | bluet/arguslm | 1 | 探测监控 | 主动探测 TTFT/TPS/uptime + 告警 + 历史，纯 Python（**定位最贴"只测不切"**） |

## 二、关键发现

1. **差异化确认**：全生态（含主仓 cc-switch、cc-switch-cli、openclaw、claude-code-router）无人做**内容级答案正确性校验**。CC-Pulse 发算术题验证答案是独有维度。真实痛点佐证：anthropics/claude-code#22445（用户投诉被静默切 haiku）。
2. **CC-Pulse 已领先**：并发批量 + SSE 流式 + 7 维 inspect（含静默路由、context 窗口、thinking、tools、vision）+ 只读日志分析（stats/routing/analyze 已含 P50/P95/P99）+ 批量退出码 0/3/4 + NDJSON。这些点位上多数竞品只做连通性/延迟，不做内容。
3. **主要缺口（竞品有、CC-Pulse 无）**：
   - **环境变量冲突检测**（cc-switch-cli `env check`）：ANTHROPIC_BASE_URL/AUTH_TOKEN 等环境变量会覆盖所选供应商，是"静默路由"最大真实来源。
   - **历史探测归档 + 趋势**（uptime-kuma/gatus/upptime/llm-api-bench）：当前只单次探测，无跨次趋势，无法暴露"降级"。
   - **成本估算**（litellm/cc-switch Usage/claude-code-router）：usage 解析已有 token 数，但无定价表 → 费用。
   - **告警/通知出口**（gatus/healthchecks/uptime-kuma）：失败可被 cron/webhook 消费。
   - **模型列表对账**（cc-switch-cli `provider fetch-models`）：拉广告模型清单 vs 实测返回，检出"宣称 vs 实际"。

## 三、可借鉴功能（按价值排序）

| 优先级 | 功能 | 参考 | 价值 | 落地难度 |
|---|---|---|---|---|
| **P1** | **`env check`：环境变量冲突检测** | cc-switch-cli `env check` | 高（静默路由最大来源） | 低（纯 stdlib 读 env） |
| **P1** | **探测历史归档 + 趋势子命令** | upptime/gatus/llm-api-bench | 高（补"降级"维度） | 低-中（本地追加 JSONL + 聚合） |
| **P2** | **成本估算**（内置定价表 × usage） | litellm/cc-switch Usage | 中高 | 低 |
| **P2** | **失败告警/通知出口**（`--webhook`/`[fail]` 行） | gatus/healthchecks | 中高 | 低 |
| **P3** | **模型列表对账**（宣称 vs 实测） | cc-switch-cli fetch-models | 中 | 中 |
| **P3** | **供应商健康分聚合**（多模型×7 维 → 0-100） | gatus suites | 中 | 中 |
| **P4** | **MCP 接口暴露** | — | 中 | 高 |
| **P4** | **延迟排序输出**（探测批内 TTFT/延迟排序） | openclaw ping-model | 低 | 低 |

## 四、结论

推荐实施 **P1 两项**（`env check` + 历史归档/趋势）——纯 stdlib、可测试、直接解决"静默路由排查"和"趋势缺失"两个真实痛点，且不与 CC-Pulse 定位冲突（保持只读、零依赖）。P2 两项（成本估算 + 告警出口）价值明确但需用户确认是否想要。P3/P4 留作后续。

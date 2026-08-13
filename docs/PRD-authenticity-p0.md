# PRD：模型保真鉴别 P0（thinking 签名验签 + 换芯字段检测）

> 状态：实现中 · 2026-08-13 · 零依赖铁律不变 · 调研见 memory `authenticity-probe-research`

## 1. 背景与目标

cc-switch 用户最大痛点是"配了不通/偷偷降级/静默换芯却不知坏在哪一环"（见 memory `positioning-vs-cc-switch` 引用的 issues #4682/#1638/#2864）。市面只有闭源验真 SaaS（ztest.ai/veridrop）能查，且要 key 出机。

**目标**：把两个性价比最高、不破零依赖、key 不出机的保真判据塞进 `inspect`：
- **thinking 签名验签**（Claude 专属，密码学级不可伪造）
- **换芯字段检测**（跨协议通用，纯 JSON 解析）

让 inspect 报告新增 `authenticity` 维度，输出"疑似换芯/疑似非真"的概率信号（**非确定性判定**）。

## 2. 非目标（明确不做）

- 不做需要 ~200 次采样的随机数 JS 散度指纹（P2，烧钱）。
- 不做需要 logprobs / 本地真模型的方法（RUT/IRIS/perplexity，零依赖下不可行）。
- 不承诺检测 8-bit 量化 / 同世代邻档降智（少量黑盒请求测不出，是公认天花板）。
- 不做"确定性判定真伪"——只输出概率信号 + 证据。

## 3. 两个探针的设计

### 3.1 换芯字段检测 `detect_crosspack_fields(resp_body, app_type) -> dict`

**原理**（借鉴 `canarybyte/veridrop`，AGPL-3.0，思路不受限）：原生 OpenAI 响应 `usage` 只有 `prompt_tokens`/`completion_tokens`/`total_tokens` 三键。若冒出 Anthropic 专属字段（`cache_creation_input_tokens`/`cache_read_input_tokens`/`input_tokens`/`output_tokens`，或顶层 `usage_source: anthropic`、`model` 值含 `claude`），说明中转把 OpenAI 格式请求偷转给 Claude 后端再包回 OpenAI。反之 Anthropic 响应里出现 `prompt_tokens`/`completion_tokens`/`system_fingerprint` 同理可疑。

**输出**：
```json
{
  "suspicious": true,
  "findings": [
    {"field": "usage.cache_creation_input_tokens", "reason": "Anthropic 专属字段出现在 OpenAI 格式响应"},
    {"field": "usage_source", "reason": "中转自曝后端来源"}
  ]
}
```
无可疑字段时 `suspicious: false, findings: []`。

**约束**：纯 `json` 标准库；输入已是脱敏前响应体（验签不涉及 key）；非 JSON/解析失败 → `suspicious: false, findings: [], note: "无法解析"`。

### 3.2 thinking 签名提取 `extract_thinking_signatures(resp_body) -> list[dict]`

**原理**：Claude 扩展思考模式下每个 thinking content block 带 `signature` 字段，是 Anthropic 服务端密钥签名，客户端只能验证不能生成。中转站无法伪造（密钥只在服务端），只能原样透传真 Claude 产出。

**本 P0 只做"提取 + 存在性判定"**，不做真正回传验签（回传需构造带 thinking block 的 follow-up 请求，工程量大且不同协议构造不同，留 P1）。判定逻辑：
- thinking block 存在且有 `signature` 字段且非空 → `has_valid_signature: True`（强证据：确为真 Claude 服务端产出 thinking）
- thinking block 存在但无 `signature` / 为空 → `has_valid_signature: False, reason: "thinking 无签名，疑似伪造或第三方模型伪装"`
- 无 thinking block → `has_valid_signature: None, reason: "未触发 thinking 块"`（不能下结论）

**输出**：`list[{"signature_present": True/False, "signature_length": N, "truncated": bool}]` + 汇总 `has_valid_signature`。

**约束**：纯 `json`；兼容 Anthropic content[] 与 OpenAI choices[].message.reasoning（后者无签名，`signature_present: False`）。

### 3.3 集成进 inspect

- `_inspect_one_model` 在 thinking 探测已有 `r_en`（enable thinking 的响应），**复用其 raw body** 提取签名，不新增请求。
- text/streaming raw body 跑换芯字段检测（已有 raw，不新增请求）。
- report 新增 `authenticity: {"crosspack": ..., "thinking_signature": ..., "verdict": "clean|suspicious|inconclusive", "evidence": [...]}`。
- `verdict` 汇总：换芯可疑 → `suspicious`；thinking 无签名但出现了 thinking 块 → `suspicious`；都无信号 → `clean`；都无数据（非 JSON/无 thinking 块/非 Claude 协议）→ `inconclusive`。

## 4. 能力边界（必须出现在报告/文档里）

- thinking 签名只对 **Claude 协议 + 触发了 thinking 块** 有效；OpenAI/openclaw 协议无签名，不证伪。
- 换芯字段依赖中转站"露馅"，老练中转清洗过字段就测不出。
- 结论是**疑似信号**，不是铁证；GhostPrint 类攻击（arXiv:2606.16100）理论上能骗过。
- 不做回传验签（P1）。

## 5. TDD 验收（详见 tests/test_authenticity.py）

| # | 测试 | 期望 |
|---|---|---|
| 1 | OpenAI 响应含 `cache_creation_input_tokens` | suspicious=True，findings 含该字段 |
| 2 | OpenAI 响应含 `usage_source: anthropic` | suspicious=True |
| 3 | 纯 OpenAI usage（仅三键） | suspicious=False |
| 4 | Anthropic 响应含 `system_fingerprint` | suspicious=True |
| 5 | 纯 Anthropic 响应 | suspicious=False |
| 6 | 非 JSON / 空串 | suspicious=False, note 不可解析 |
| 7 | Anthropic content[].type=thinking + signature 非空 | has_valid_signature=True |
| 8 | thinking 块无 signature | has_valid_signature=False |
| 9 | 无 thinking 块 | has_valid_signature=None |
| 10 | OpenAI reasoning_content 无 signature | has_valid_signature=False |
| 11 | verdict 汇总：换芯可疑→suspicious | 通过 |
| 12 | verdict：全 clean→clean | 通过 |
| 13 | verdict：全无数据→inconclusive | 通过 |
| 14 | 集成：inspect 报告含 authenticity 字段 | 通过（用 MockAnthropicHandler 已有 fixture）|

## 6. 实现位置

- `ccpulse_probe.py` 新增 `_detect_crosspack_fields` / `_extract_thinking_signatures` / `_authenticity_verdict`。
- `_inspect_one_model` 末尾组装 `authenticity`（复用 text_raw/streaming 的 raw body + thinking r_en 的 raw body）。
- 注意：现有 `_inspect_thinking` 没保留 `r_en` 的 raw body，需让它返回 raw body 或在 `_inspect_one_model` 直接重发——**倾向改 `_inspect_thinking` 返回 `enabled_raw_body`**，最小改动。

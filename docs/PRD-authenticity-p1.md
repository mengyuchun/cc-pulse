# PRD：模型保真鉴别 P1 - usage 自洽静态校验

> 状态：实现中 · 2026-08-13 · P0 已上线（`50a6e21`）· 调研见 memory `authenticity-probe-research`

## 1. 目标

P0 的换芯字段检测和 thinking 签名提取已上线。P1 在此基础上加一条**零新请求**的静态判据：**usage 自洽校验**。

中转站计费注水的常见手法：伪造 `total_tokens`、`output_tokens` 虚高、声称有输出实际无内容。这些可从 inspect 已有的 `text_raw`（含结构化 usage + answer 文本）静态算出，**不新增 HTTP 请求，不破零依赖**。

## 2. 非目标（本轮不做，留 P2）

- **缓存回放双发**（同前缀双发看 `cached_tokens`、temp=1 双发查逐字相同）：需 2 次新请求，工程量更大，留 P2 单独评估。
- **token padding 主动探测**（max_tokens=1 测输入计费）：需发新请求，留 P2。
- **知识截止 before/after 题库**：需题库 + 10-30 次请求，留 P2。

## 3. 探针设计：`_check_usage_consistency(usage, answer, app_type) -> dict`

输入：`extract_usage` 已解析的 usage dict、`extract_answer` 的 answer 文本、app_type。
输出：
```json
{
  "suspicious": true,
  "findings": [
    {"check": "total_inconsistent", "reason": "total_tokens(15) != prompt(10)+completion(3)=13"}
  ]
}
```
无可疑 -> `{"suspicious": false, "findings": []}`。

### 校验规则

1. **total 自洽**（OpenAI 系，最硬）：usage 有 `total_tokens` + `prompt_tokens` + `completion_tokens` 时，`total != prompt + completion` -> 可疑（usage 伪造）。允许 ±1 tokenizer 误差。
2. **output 有声明但无内容**：`output_tokens > 0` 但 answer 文本 strip 后为空 -> 可疑（扣费但无输出）。
3. **字段矛盾**：`input_tokens`/`prompt_tokens` 其中一个声明非空但另一个为 0，且协议明确应都有 -> 不报（不同协议字段名不同，易误报，跳过）。

### 边界

- 只对有 usage 的响应判断；usage 缺失（`present=False`）-> `suspicious: false, note: "无 usage"`。
- 规则 1 只对 OpenAI 系（codex/openclaw）生效，Anthropic usage 无 total_tokens。
- 结论仍是概率信号。

## 4. 集成

- `_inspect_one_model` / 主 inspect 路径在组装 `authenticity` 时，新增 `usage_consistency` 子字段。
- `_authenticity_verdict` 把 usage_consistency.suspicious 纳入 suspicious 判定。

## 5. TDD 验收（tests/test_usage_consistency.py）

| # | 测试 | 期望 |
|---|---|---|
| 1 | OpenAI total != prompt+completion | suspicious=True, total_inconsistent |
| 2 | OpenAI total == prompt+completion | suspicious=False |
| 3 | output_tokens>0 但 answer 空 | suspicious=True |
| 4 | output_tokens>0 且 answer 非空 | 不报此项 |
| 5 | usage 缺失(present=False) | suspicious=False, note 含"无 usage" |
| 6 | Anthropic usage 无 total_tokens | 不触发 total 校验 |
| 7 | verdict 汇总：usage 不自洽 -> suspicious | 通过 |
| 8 | verdict：P0 clean + usage clean -> clean | 通过 |

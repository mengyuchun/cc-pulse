# PRD：模型保真鉴别 P2-A - 缓存回放/钳温双发探测

> 状态：实现中 · 2026-08-13 · P0/P1 已上线 · 调研见 memory `authenticity-probe-research`

## 1. 目标

P0（换芯字段+thinking 签名）和 P1（usage 自洽）都是零新请求的静态分析。P2-A 加第一个**需发新请求**的判据：**缓存回放/钳温检测**。

中转站省钱手法：对相同 prompt 返回缓存结果（而非真打上游），或强制 `temperature=0` 钳温（降智）。正常 `temperature=1` 下同一 prompt 两次请求应有随机性差异；若**逐字完全相同** -> 疑似缓存回放或钳温。

## 2. 探针设计：`_probe_cache_replay(p, tier, timeout, skip_tls, ...) -> dict`

发两次相同 prompt（temperature=1, max_tokens=16, disable_thinking=True），提取两次 answer，比较是否逐字相同。

输出：
```json
{
  "suspicious": true,
  "identical": true,
  "note": "temp=1 双发逐字相同，疑似缓存回放或强制钳温",
  "first_answer": "5",
  "second_answer": "5"
}
```
- 两次都 200 且 answer 逐字相同 -> `suspicious: true, identical: true`
- 两次都 200 但不同 -> `suspicious: false, identical: false`
- 任一非 200 / 无 answer -> `suspicious: false, note: "探测失败无法判定"`

## 3. 实现改动

1. `build_probe_request` / `probe_tier` / `_build_proto_payload` 加 `temperature: float | None = None` 参数，None 不发该字段；非 None 时按协议加 `temperature`（Anthropic/OpenAI chat 都支持；Responses 协议不支持 temperature 跳过）。
2. `_probe_cache_replay`：调 `probe_tier` 两次（temperature=1, max_tokens=16, disable_thinking=True, 固定 question 不随机），比较 answer。
3. 集成进 `_inspect_one_model`：当 `--include cache-replay` 时运行，结果进 `authenticity.cache_replay`。
4. `_authenticity_verdict` 纳入 `cache_replay.suspicious`。

## 4. 非目标

- 不做随机数 JS 散度指纹（~200 请求，留 P2-C，需参考指纹库）。
- 不做知识截止题库（留 P2-B，需建题库）。
- 不判 cache_read_input_tokens 命中=欺诈（Anthropic prompt caching 是合法功能，命中不报）。

## 5. 能力边界

- temp=1 双发相同是**强信号但不绝对**：真模型极小概率也会相同（短答案如单数字）。答案越长相同越可疑。
- 探测失败（限流/超时）不能判定，标 note。
- 结论仍是概率信号。

## 6. TDD 验收（tests/test_cache_replay.py）

| # | 测试 | 期望 |
|---|---|---|
| 1 | 两次相同 answer -> suspicious=True, identical=True | 通过 |
| 2 | 两次不同 answer -> suspicious=False, identical=False | 通过 |
| 3 | 第一次非 200 -> suspicious=False, note 含失败 | 通过 |
| 4 | 第二次非 200 -> suspicious=False, note 含失败 | 通过 |
| 5 | verdict 汇总：cache_replay suspicious -> suspicious | 通过 |
| 6 | verdict：P0/P1 clean + cache clean -> clean | 通过 |
| 7 | build_probe_request 带 temperature=1 -> body 含 temperature | 通过 |
| 8 | build_probe_request temperature=None -> body 不含 temperature | 通过 |

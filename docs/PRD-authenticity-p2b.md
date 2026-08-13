# PRD：模型保真鉴别 P2-B - 知识截止 before/after 题库

> 状态：实现中 · 2026-08-13 · P0/P1/P2-A 已上线 · 调研见 memory `authenticity-probe-research`

## 1. 目标

用"知识截止日期"区分模型版本/世代。原理：模型只知道训练截止前的事实。给一道"after 题目"（发生在较新模型截止后、旧模型截止前的事件），新模型答对、旧模型答错或拒答。

实测有"准确率悬崖"：GPT-4o 在 before-2024 子集 70% → after-2025 子集 49%（RealFactBench）。

适用：区分"低版本冒充高版本"——如果供应商标榜 Claude Sonnet 4.5（截止 2025-03）却答不出 2025 年才公开的事件，疑似降级到旧模型。

## 2. 探针设计

`_probe_knowledge_cutoff(p, tier, timeout, skip_tls, *, max_tokens, user_agent) -> dict`

对一组 before/after 二选一题发问，每题 1 次请求，本地关键词判分。

题库（内置 `KNOWLEDGE_CUTOFF_QUESTIONS`）：每条 `{q, expected, era}`，era ∈ `{"before", "after"}`：
- **before 题**：所有模型都该答对（2023 年前的确定事实）。答错 = 异常，可能非真模型或被降智。
- **after 题**：只有较新模型（截止 ≥ 2024-09）才答对。答对 = 较新模型；答错/拒答 = 旧模型或被降级。

判分：二选一题用关键词匹配（如 "yes/no"、年份、人名）。**答案键内置代码不进 prompt**（防运行环境里的 Agent 抄答案，CCFingerprint 关键设计）。

输出：
```json
{
  "suspicious": false,
  "after_correct": 2,
  "after_total": 3,
  "before_correct": 3,
  "before_total": 3,
  "note": "答对 2/3 较新事件，疑似较新模型",
  "details": [{"q": "...", "era": "after", "correct": true, "answer": "..."}]
}
```
- before 错 ≥1 -> `suspicious: True`（连老事实都不对，异常）
- after 全错 -> 不报 suspicious，但 note 标"疑似旧模型/降级"（after 错不一定是造假，可能是合法的旧模型）
- 探测失败（非 200）-> note 不判定

## 3. 题库（保守、确定性强、不易过期）

before 题（2023 年前事实，所有模型该会）：
1. "2020 年奥运会在哪个城市举办？只答城市名。" -> "东京"/"tokyo"
2. "ChatGPT 首次公开发布是哪一年？只答年份。" -> "2022"
3. "AlphaGo 击败李世石是哪一年？只答年份。" -> "2016"

after 题（2024-2025 事件，截止 ≥2024-09 的模型才该会）：
1. "Claude 3.5 Sonnet 公开发布于哪一年？只答年份。" -> "2024"
2. "OpenAI 发布的 o1 模型是哪一年？只答年份。" -> "2024"
3. "DeepSeek-V3 公开发布于哪一年？只答年份。" -> "2024"（2024-12）

## 4. 集成

- `--include knowledge-cutoff` 开启（默认关，3+3=6 次额外请求）
- 结果进 `authenticity.knowledge_cutoff`
- `_authenticity_verdict` 纳入 `knowledge_cutoff.suspicious`（仅 before 错触发 suspicious）

## 5. 能力边界

- after 错不报 suspicious（旧模型合法），只给 note 供人判断
- 题库会随时间失效（2026 年的"after"到 2027 年可能所有模型都会）——需定期更新
- 拒答/过度谨慎的模型可能不直接答年份，判分要宽松（答案含目标年份即对）
- 结论仍是概率信号

## 6. TDD 验收（tests/test_knowledge_cutoff.py）

| # | 测试 | 期望 |
|---|---|---|
| 1 | 题库含 ≥3 before + ≥3 after | 通过 |
| 2 | before 全对 -> suspicious=False | 通过 |
| 3 | before 错 1 -> suspicious=True | 通过 |
| 4 | after 全错 -> suspicious=False（note 标旧模型） | 通过 |
| 5 | after 全对 -> note 标较新模型 | 通过 |
| 6 | 任一非 200 -> 不判定 note | 通过 |
| 7 | 判分宽松：答案含目标年份即对 | 通过 |
| 8 | verdict 汇总：knowledge_cutoff suspicious -> suspicious | 通过 |

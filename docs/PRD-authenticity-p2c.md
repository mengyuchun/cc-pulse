# PRD：模型保真鉴别 P2-C - 单 token 随机数分布指纹（JSD）

> 状态：实现中 · 2026-08-13 · P0/P1/P2-A/P2-B 已上线 · 调研见 memory `authenticity-probe-research`

## 1. 目标

论文 *One Token Is Enough*（arXiv:2607.10252）：每个 LLM 对"给个 1-100 随机数"有独特分布指纹，
单 token 即可区分模型。真 LLM 永远不是均匀分布——有明显偏置（回避整数、偏好 7/17/23/42/73 等）。

CC-Pulse 用途：若中转站后端是脚本 `random.randint(1,100)` 冒充 LLM，输出近似均匀——
这是非 LLM 后端的强信号。真 Claude/各家 LLM 共享"回避整数+偏好特定数"的 LLM 通用偏置。

## 2. 探针设计

`_probe_js_fingerprint(p, tier, timeout, skip_tls, *, samples, max_tokens, user_agent) -> dict`

- 问 `samples` 次："Pick a random number between 1 and 100. Reply with only the number."
- 每次独立请求，max_tokens=16（省 token）
- 正则提取首个 1-100 整数，统计频次直方图（100 桶）
- 与"LLM 通用偏置参考分布"和"均匀分布"分别算 JSD
- 判据：观测更接近均匀 than 参考 → suspicious（疑似非 LLM 后端）

## 3. 自包含判据（无需外部指纹库）

不内置各家模型专属指纹（版权+维护成本），改用**自包含双假设判据**：
- `jsd_ref = JSD(observed, LLM_BIAS_REFERENCE)` —— LLM 通用偏置参考
- `jsd_unif = JSD(observed, UNIFORM)`
- `jsd_unif < jsd_ref` → 观测更接近均匀随机 → 真正的 LLM 不会这样 → **suspicious**
- `jsd_ref` 低 → 符合 LLM 偏置特征 → clean
- 否则 → note"分布非典型/样本不足"

参考分布 `LLM_BIAS_REFERENCE`（程序化构造，零外部数据）：
- 起点：均匀 1/100
- 偏好数 {7,17,23,42,73,37,47,67,77,83} 加权 ×3
- 整数（10,20,...,100）抑制 ×0.3
- 归一化

## 4. JSD 实现（纯 math）

```
m = 0.5*(p+q)
JSD = 0.5*KL(p||m) + 0.5*KL(q||m)
KL(a||b) = Σ a_i * log2(a_i / b_i)  (a_i>0 且 b_i>0 时)
```
base 2，JSD ∈ [0,1]。

## 5. 集成

- `--include js-fingerprint` 开启（默认关，50 次额外请求）
- `--js-samples N` 调样本数（默认 50，建议 ≥200 高置信，最少 20 才判定）
- 结果进 `authenticity.js_fingerprint`
- `_authenticity_verdict` 纳入 `js_fingerprint.suspicious`

## 6. 能力边界

- 同世代邻档 LLM（haiku vs sonnet）分布相近，本探针测不出档位（只测"是否 LLM"）
- 样本少（<20）不判定
- 中转站可缓存同一回答重复返回 → 表面"分布异常"，需配合 cache-replay 看
- GhostPrint 理论上可微调弱模型模仿分布 → 结论是概率信号
- 50 次请求有成本（默认关闭，用户自担）

## 7. TDD 验收（tests/test_js_fingerprint.py）

| # | 测试 | 期望 |
|---|------|------|
| 1 | 参考分布 100 桶、和≈1.0、全>0 | 通过 |
| 2 | 均匀 JSD：JSD(uniform, uniform)=0 | 通过 |
| 3 | `_js_parse_number("是42")` == 42；`"无数字"` == None | 通过 |
| 4 | 观测=均匀 → suspicious=True（更接近均匀 than 参考） | 通过 |
| 5 | 观测=参考分布 → suspicious=False | 通过 |
| 6 | 全 429 → suspicious=False、有 note | 通过 |
| 7 | 样本<min_samples → 不判定（note 标不足） | 通过 |
| 8 | verdict 汇总：js suspicious → suspicious | 通过 |

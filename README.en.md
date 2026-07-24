<div align="center">

# CC-Pulse

**Listen to the "heartbeat" of [cc-switch](https://github.com/farion1231/cc-switch) providers — health checks and deep single-model diagnostics**

Don't trust "it connected". Trust "it works". With so many providers, see at a glance which ones you can actually use.

[中文](README.md) · English

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/stdlib%20only-green.svg)](#)
[![Tests](https://img.shields.io/badge/tests-223%20pass-brightgreen.svg)](#tests)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

---

## Why CC-Pulse

cc-switch helps you manage many Claude Code / Codex API relay providers. But relays are messier than they look:

- 🔇 **200 ≠ usable**: some providers return 200 with empty answers, wrong answers, or silently route to cheaper models
- 🎭 **Tier traps**: haiku works, sonnet fails, opus is rate-limited, fable does not exist
- 🔑 **Auth styles differ**: some accept only `x-api-key`, some only `Authorization: Bearer`, some validate client User-Agent
- 🧠 **Thinking models**: DeepSeek / GLM may burn a 20-token budget on thinking and return no final answer
- 📏 **Shrinking context**: advertised as 1M, rejected around 526k
- 🛠️ **Tool / vision gaps**: coding agents depend on `tool_use`, but many relays only pretend to support it

CC-Pulse does not stop at connectivity. It checks whether a provider can actually be used — so with so many providers, you can see at a glance which ones really work.

---

## How it differs from cc-switch's built-in checks

cc-switch includes stream / health monitoring and records fields such as `http_status` / `response_time_ms` / `success`. CC-Pulse is **complementary**, not a replacement: cc-switch manages configuration and switching; CC-Pulse focuses on deep probing.

| Dimension | cc-switch built-in checks | CC-Pulse |
|-----------|---------------------------|----------|
| Focus | Connectivity / latency / runtime state | Real usability (auth + correct answers) |
| Sends real model requests upstream | Depends on current cc-switch version and check config | ✅ Real request for every tier |
| Invalid API key / token | Depends on whether the check covers that provider's auth path | ✅ 401 / 403 clearly classified as `authentication` |
| HTTP 200 with empty answer | May not be distinguishable from a healthy connect | ✅ `answer_mismatch` (unusable) |
| HTTP 200 with business-error body | May look like "HTTP success" | ✅ `invalid_response`, keeps original error text |
| Thinking burns all tokens | Outside basic connectivity scope | ✅ Thinking disabled by default; adjustable `max_tokens` |
| Silent model routing | ❌ Outside basic check scope | ✅ `inspect` compares request / response model |
| Multi-tier fallback | Depends on runtime failover | ✅ Active probe: haiku → sonnet → opus → fable → default |
| Streaming / tools / context / vision | ❌ Outside basic check scope | ✅ 7-dimension `inspect` diagnostics |

**Typical trap scenarios** (all seen in real use):

**① HTTP 200, but body is a business error**

```json
{"code":0,"msg":"legacy forwarding path closed","data":null}
```

✅ Connected · ❌ No model content → CC-Pulse classifies as `invalid_response`

**② HTTP 200, but answer is empty**

```json
{"content":[{"type":"text","text":""}]}
```

✅ Connected · ❌ Thinking burned the budget, no final answer → CC-Pulse classifies as `answer_mismatch`

**③ Key / token invalid or revoked**

```json
{"type":"error","error":{"type":"AuthError","message":"Invalid API key."}}
```

✅ Endpoint is alive · ❌ Auth failed, unusable in practice → CC-Pulse classifies as `authentication` and shows where it failed

**④ Key can list models but cannot complete inference**

```text
GET /v1/models  → 200 ✅
POST /v1/messages → 401 Invalid API key ❌
```

If a basic check only covers connectivity, it may treat "models list works" as healthy. CC-Pulse sends a real inference request and exposes the second-half auth failure.

In one line: **cc-switch answers "can it connect?"; CC-Pulse answers "can I use it?"**

## Core features

### 1. Health check `check` — multi-tier fallback + real answer verification

- Probes in order `haiku → sonnet → opus → fable → default`, **stops at the first tier that answers correctly**
- Sends `"2+3=?"` and requires the answer to be exactly `"5"` — **HTTP 200 ≠ usable**
- Auth follows cc-switch config: `ANTHROPIC_AUTH_TOKEN` → `Bearer`, `ANTHROPIC_API_KEY` → `x-api-key`
- **Live progress**: one line as soon as each tier finishes — no waiting until everything ends
- Concurrent batching + full error text passthrough
- Structured JSON reports for jq / PowerShell / CI

### 2. Model catalog `list-models` — fetch provider-declared model lists

- `GET /v1/models`, compatible with Anthropic / OpenAI response shapes
- Listed ≠ actually usable; it is only what the provider claims to support

### 3. Single-model deep diagnostics `inspect` — 7-dimension checkup

For a given `(provider, model)`, run text / streaming / metadata / context smoke / thinking / tool use / optional vision, and emit a unified JSON report:

| Dimension | What it checks |
|-----------|----------------|
| **text** | Real question + usage token parsing |
| **streaming** | SSE / non-SSE streaming, TTFT, event count, protocol type |
| **metadata** | `GET /v1/models/{id}` declared window / capabilities (labeled "not measured") |
| **context** | When undeclared, 512k / 1M char context smoke: accepted / rejected / timeout |
| **thinking** | Dual probe (disable vs enable): supports / forces / rejects |
| **tools** | Minimal side-effect-free tool: native / text_only / rejected |
| **vision** | Embedded 1×1 PNG, checks whether images are accepted (off by default) |
| **model-consistency** | Request model vs response model field, catches silent routing |

---

## Quick start

### Requirements

- **Python 3.10+** (runtime uses the standard library only; no `pip install`)
- Windows / macOS / Linux
- [cc-switch](https://github.com/farion1231/cc-switch) installed and configured (default DB: `~/.cc-switch/cc-switch.db`)

> Entry barrier: a working local Python is required. Windows users who already use Claude Code / a dev environment usually already have Python. You can also double-click `run_health_check.ps1` (it auto-finds the interpreter via PATH / `CC_PULSE_PYTHON`).

### Install

```bash
git clone https://github.com/mengyuchun/cc-pulse.git
cd cc-pulse
# No pip install — stdlib only
python check_ccswitch_health.py check --help
```

### Get started in 3 seconds

```bash
# Daily check: failover queue + current provider only (fastest)
python check_ccswitch_health.py check --failover-only

# Full check
python check_ccswitch_health.py check

# JSON report (JSON on stdout, human progress on stderr)
python check_ccswitch_health.py check --failover-only --json | jq '.summary'

# Single-model deep diagnostics (human-readable)
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" --human

# 1M context smoke + vision
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" \
    --probe-context 1m --include text,streaming,metadata,thinking,tools,vision
```

> Windows users can also double-click `run_health_check.ps1` for an interactive menu — no need to memorize flags.

---

## Subcommands

### `check` — daily health check

Probes tiers in fallback order, **stops at the first successful tier**, and reports every attempt.

```bash
python check_ccswitch_health.py check --failover-only        # queue + current (recommended)
python check_ccswitch_health.py check                          # all claude providers
python check_ccswitch_health.py check --type all              # claude + codex + openclaw
python check_ccswitch_health.py check --failover-only --json  # machine-readable
```

**Live output example**:

```
Progress: print each tier as it finishes; print a provider summary when that provider is done

  · ProviderA            haiku  [401] 1.2s Invalid API key
  · ProviderB            haiku  [ok] 2.1s answer:"5"
[ 1/8] ✅ ProviderB               ✓haiku answer:"5"
  · ProviderA            sonnet [429] 1.6s Weekly limit reached
  · ProviderC            haiku  [wrong answer] 2.4s "..."
[ 2/8] ❌ ProviderA               haiku:401(...) | sonnet:429(...)
```

**Flags**:

| Flag | Meaning | Default |
|------|---------|---------|
| `--type claude\|codex\|openclaw\|all` | Provider type | `claude` |
| `--failover-only` | Only failover queue + current provider | off |
| `--json` | Structured JSON on stdout; human text on stderr | off |
| `--workers N` | Concurrency | 6 |
| `--timeout SEC` | Per-request timeout seconds | 30 |
| `--probe-max-tokens N` | Probe token budget (raise for thinking models) | 20 |
| `--probe-enable-thinking` | Allow thinking mode | off |
| `--user-agent UA` | Override UA (default from local `claude --version`) | auto |
| `--skip-tls-verify` | ⚠️ Skip TLS certificate verification | off |

### `list-models` — fetch model catalogs

```bash
python check_ccswitch_health.py list-models
python check_ccswitch_health.py list-models --failover-only --type all
```

### `history` / `stats` / `routing` — read-only cc-switch runtime logs

No HTTP: only reads `proxy_request_logs` in `~/.cc-switch/cc-switch.db` (optional on-disk log tail).

```bash
python check_ccswitch_health.py history
python check_ccswitch_health.py history --fails --limit 50
python check_ccswitch_health.py history --provider Fengwind --since 24h
python check_ccswitch_health.py stats --since 7d
python check_ccswitch_health.py routing --since 24h --limit 20
python check_ccswitch_health.py history --fails \
  --log-file ~/.cc-switch/logs/cc-switch.log --log-lines 80
```

| Flag | Meaning | Commands |
|------|---------|----------|
| `--limit N` | Row count | history / routing |
| `--fails` | Failures only | history |
| `--since 24h\|7d\|30m\|seconds` | Time window | history / stats / routing |
| `--provider substr` | Filter by name | history |
| `--json` | JSON output | all three |
| `--log-file PATH` | Tail on-disk log | history |
| `--with-history` | Attach 24h summary after check/inspect | check / inspect |

Failures map into the same `error_category` enum used by live probes.

### `inspect` — single-model deep diagnostics

```bash
# Default: text + streaming + routing + metadata + thinking + tools
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6"

# Human-readable output
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" --human

# 1M context smoke (when no declared window)
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" --probe-context 1m

# Explicitly enable vision
python check_ccswitch_health.py inspect \
    --provider "Relay-A" --model "claude-sonnet-4-6" \
    --include text,streaming,metadata,thinking,tools,vision
```

**Flags**:

| Flag | Meaning | Default |
|------|---------|---------|
| `--provider NAME` | Provider name (same as in cc-switch) | required |
| `--model ID` | Model ID (may include suffixes like `[1M]`) | required |
| `--source configured\|listed\|manual` | Model source | `configured` |
| `--type claude\|codex\|openclaw\|all` | Limit provider type | `claude` |
| `--include LIST` | Checks to run (see table) | all on except vision |
| `--probe-context 512k\|1m` | Context smoke tier | `512k` |
| `--keep-suffix` | Keep `[1M]`-style suffixes in model ID | off |
| `--ttft-timeout SEC` | Streaming first-token timeout | same as `--timeout` |
| `--human` | Human-readable output (default is JSON) | off |

**`--include` checks**:

| Item | Default | Meaning |
|------|---------|---------|
| `text` | ✅ | Real question + usage parsing |
| `streaming` | ✅ | SSE / non-SSE streaming, TTFT |
| `model-consistency` | ✅ | Request vs response model comparison |
| `protocol` / `error-classification` | ✅ | Protocol inference + error classification |
| `metadata` | ✅ | `GET /v1/models/{id}` declared values |
| `thinking` | ✅ | Dual probe: disable + enable |
| `tools` | ✅ | Minimal side-effect-free tool protocol probe |
| `vision` | ❌ | Embedded 1×1 PNG; enable with `--include ...,vision` |

**`--source` values**:

| Value | Behavior |
|-------|----------|
| `configured` | Exact match against tiers configured in cc-switch (no network) |
| `listed` | First `GET /v1/models`, then look up in the returned list |
| `manual` | Force the literal `--model` value (advanced) |

> ⏱ **Total timeout note**: default `inspect` issues 5–6 serial requests, so worst-case wall time ≈ N × `--timeout`. For example, `--timeout 30` can take up to ~180s. Use `--include text` for a faster single-check run.

---

## Output examples

### Human-readable (`--human`)

```
============================================================
  Provider:  Relay-A
  Model:     claude-sonnet-4-6 (configured)
  Protocol:  anthropic_messages · confirmed
============================================================

[1/7] Text probe
  Status: ✅ pass · 1.24s
  Answer: "5" · correct
  usage: in=20 out=3

[2/7] Streaming probe
  Status: ✅ pass · TTFT 0.42s · total 1.31s

[3/7] Model routing
  Match: exact_match

[4/7] Model metadata
  Declared context window: 200,000 tokens (provider-declared, not measured)

[5/7] Thinking
  verdict: supports_disable

[6/7] Tool use
  Status: ✅ pass · support=native

[7/7] Vision · skipped

------------------------------------------------------------
  Summary: healthy
============================================================
```

### JSON report fields

| Field | Meaning |
|-------|---------|
| `protocol.detected` | `anthropic_messages` / `openai_responses` / `openai_chat_completions` / `unknown` |
| `protocol.confidence` | `inferred` / `confirmed` (upgraded after successful text probe) |
| `text.status` | `pass` / `fail` / `error` |
| `text.answer` / `text.correct` | Extracted answer / equals `"5"` |
| `streaming.ttft_seconds` | Time to first token (seconds) |
| `streaming.response_model` / `event_count` / `is_sse` | Response model / event count / true SSE |
| `metadata.declared_context_window` | Provider-**declared** window (not measured) |
| `metadata.capabilities` | `{"image_input": true, "thinking": true, ...}` |
| `context.status` | `accepted` / `rejected` / `timeout` / `error` / `skipped` |
| `context.approx_input_chars` / `token_estimate` | Smoke payload size and upper-bound note |
| `thinking.verdict` | `supports_disable` / `forces_thinking` / `rejects_thinking_field` / `breaks_on_short_budget` |
| `tools.protocol_support` | `native` / `text_only` / `rejected` / `unknown` |
| `vision.status` | `pass` / `fail` / `error` / `skipped` / `unsupported` |
| `usage.present` / `input_tokens` / `output_tokens` | Whether real token counts were parsed |
| `model_consistency.match` | `exact_match` / `alias_match` / `fuzzy_match` / `mismatch` / `unverifiable` |
| `summary.verdict` | `healthy` / `available_but_wrong_answer` / `unavailable` / `skipped` |
| `summary.recommended_actions` | Actionable suggestions based on results |

### Error category enum (`error_category`)

Each probe result's `error_category` is one of:

```
none | network | tls | authentication | rate_limit | model_not_found |
protocol_incompatible | server_error | invalid_response | answer_mismatch |
stream_protocol | ttft_timeout | stream_incomplete | unknown
```

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Healthy (`check` has at least one usable provider / `inspect` healthy or skipped / `list-models` finished) |
| 1 | All health checks failed / `inspect` unusable / wrong answer |
| 2 | DB missing, no matching providers, or resolve failed (`inspect` target not found) |

---

## Windows desktop launcher

`run_health_check.ps1` provides an interactive menu — double-click, no flags required:

```
[1] Health check · quick       one-click (claude / queue / no JSON / no thinking)
[2] Health check · custom      choose type / scope
[3] List models                GET /v1/models catalog
[4] Deep diagnostics (inspect) single (provider, model) diagnosis
[5] Advanced settings          JSON / thinking / UA / max-tokens / context / vision
[6] Exit
```

- Prefer interpreter from `CC_PULSE_PYTHON`, then `python` on PATH
- DB path override: `CC_PULSE_DB`
- Timeout override: `CC_PULSE_TIMEOUT` (seconds)
- Uses `python -u` for unbuffered output so progress is live

### Environment variables

| Variable | Purpose |
|----------|---------|
| `CC_PULSE_PYTHON` | Preferred Python interpreter for the launcher |
| `CC_PULSE_DB` | Default cc-switch.db path for the launcher |
| `CC_PULSE_TIMEOUT` | Default timeout seconds for the launcher |
| `CC_PULSE_PWSH` | pwsh path used by tests |

---

## Design principles (intentional, not bugs)

- **Read-only, zero intrusion**: open the DB with `file:...?mode=ro`; never modify cc-switch
- **No path de-duplication**: always `base_url + /v1/messages`; a base ending with `/v1` becomes `/v1/v1/messages` — deliberately matching real Claude Code behavior
- **Full error text passthrough**: JSON error `message` is not truncated; HTML / non-JSON shows the first 500 chars plus true length
- **No file writes**: results go to the terminal / stdout only
- **Claude Code fingerprint headers**: UA from local `claude --version` (overridable via `--user-agent`) to reduce Cloudflare 1010 false rejects
- **TLS verified by default**: `--skip-tls-verify` must be explicit (exposes credentials if abused)
- **Terminal safety**: `say()` strips ANSI escapes and control characters to reduce malicious provider response injection

## Honest limitations

- `check` mainly verifies "can it answer a simple arithmetic question"; `inspect` adds streaming / metadata / context / thinking / tools / vision, but not multi-turn chat or sustained concurrency capacity
- `metadata.declared_context_window` is the provider's **claimed** value; undeclared context smoke uses a **1 char ≈ 1 token upper bound**, not a precise tokenizer count
- Claude Code fingerprint headers are not 100% complete; some strict validators may still reject the client
- Models from `list-models` ≠ actually usable; they are only declared support lists
- `inspect` never auto-runs cc-switch failover; it only emits **read-only diagnostics**
- Thinking models may false-fail under default `max_tokens=20` — raise `--probe-max-tokens` or use `--probe-enable-thinking` to reduce false negatives

## Known scenarios and responses

| Scenario | Symptom | Response |
|----------|---------|----------|
| Thinking model burns tokens | 200 with empty answer | `--probe-max-tokens 256` |
| Provider validates client UA | 403 `client_restricted` | `--user-agent "codex_cli_rs/0.50.0"` etc. |
| Provider only accepts x-api-key | 401 `Missing API key` | Use `ANTHROPIC_API_KEY` in cc-switch |
| Silent model routing | Request / response model mismatch | `inspect` `model_consistency` marks `mismatch` |
| OAuth token in wrong field | 401 `invalid x-api-key` | Use `ANTHROPIC_AUTH_TOKEN` (Bearer), not `ANTHROPIC_API_KEY` |
| Shrinking context | Claims 1M, rejects around 526k | `inspect` context smoke marks `rejected` |

---

## Tests

```bash
# Run all tests (Python core + PS1 launcher)
just test && just test-ps1

# Python core only (177 unit + end-to-end mocks)
just test

# PS1 launcher end-to-end (31 cases; requires pwsh)
just test-ps1
```

Tests use the standard library only, with an embedded mock HTTP server, and never hit real providers. Currently **192 Python tests + 31 PS1 tests**.

## Development

```bash
# Format + lint
just format
just lint
```

Uses [ruff](https://github.com/astral-sh/ruff) for formatting and linting (dev-time only; zero runtime deps).

---

## Project layout

```
CC-Pulse/
├── check_ccswitch_health.py   # Main script: check / list-models / inspect (~2200 lines)
├── run_health_check.ps1       # Windows interactive menu launcher
├── justfile                   # Common tasks (check, format, lint, test)
├── requirements.txt           # Declares: stdlib only, no runtime deps
├── tests/
│   ├── test_ccpulse_full.py   # Unit + e2e (mock SSE / multi-protocol / multi-type)
│   └── test_ps1_launcher.py   # PS1 launcher interaction flow
├── CLAUDE.md                  # Project-level Claude Code instructions
├── LICENSE                    # MIT License
├── README.md                  # Chinese docs
└── README.en.md               # English docs
```

---

## Related projects

| Project | Form | Comparison |
|---------|------|------------|
| [all-api-hub](https://github.com/qixing-jk/all-api-hub) | Browser extension | Most features, Cloudflare handling; does not read the cc-switch DB |
| [cc-test](https://github.com/zhoujun681/cc-test) | Rust CLI | Similar goal, but no multi-tier fallback or answer verification |
| [cc-switcher](https://github.com/jimstratus/cc-switcher) | PowerShell | Switching-first, probing secondary |

CC-Pulse's trade-off: **small and focused** — deep probing for cc-switch providers only (multi-tier fallback + answer verification + config-driven auth + 7-dimension single-model diagnostics). No provider management UI, no switching UI.

---

## Contributing

Issues and PRs are welcome. Please ensure:

1. `just test` is green
2. `just lint` introduces no new warnings
3. New features include matching tests
4. Existing style is followed (`ruff format`)

## License

[MIT License](LICENSE) © 2026 Yuchun Meng

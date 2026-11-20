<div align="center">

# CC-Pulse

**Zero-dependency CLI health check, capability diagnostics, and authenticity sentinel for Claude Code & coding-agent LLM relays**

Don't trust HTTP 200. Verify that your API key actually answers, isn't silently downgraded, and isn't overridden by stale env vars.

[中文](README.md) · English

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/stdlib%20only-green.svg)](#)
[![Tests](https://img.shields.io/badge/tests-595%20pass-brightgreen.svg)](#tests)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

---

## Why CC-Pulse

- 🔇 **HTTP 200 ≠ usable**: relays return 200 with error JSON, empty text, or silently route to cheaper models
- 🕵️ **Silent env overrides**: shell variables (`ANTHROPIC_BASE_URL`, etc.) override your IDE/CLI config
- 🧠 **Thinking & tool traps**: thinking models burn short budgets with empty output, or fake `tool_use` in plain text
- ☁️ **Opaque backend routing**: AWS Bedrock or Google Vertex proxied as direct Official API, or cross-protocol spoofing

---

## 10-Second Quick Start (No `git clone`, No `cc-switch.db` Required)

> **No Python installed?** Download a standalone single-file binary from [GitHub Releases](https://github.com/mengyuchun/cc-pulse/releases) (`cc-pulse-windows-x64.exe`, `cc-pulse-macos-arm64`, `cc-pulse-linux-x64`, or cross-platform `cc-pulse.pyz`) and replace `uvx cc-pulse` below with the downloaded binary.

### Step 1: Zero-Key Environment Check (Sends Zero HTTP Requests)
```bash
uvx cc-pulse env-check --no-db
```
Detects shell env vars overriding `~/.claude/settings.json`, `~/.codex/config.toml`, or `cc-switch.db`.

### Step 2: Standalone Endpoint Check
```bash
export MY_RELAY_KEY="sk-..."
uvx cc-pulse check --base-url https://relay.example.com --api-key-env MY_RELAY_KEY
```
Without `--base-url`, CC-Pulse auto-discovers `~/.cc-pulse/providers.toml`, `~/.claude/settings.json`, `~/.codex/config.toml`, or read-only `cc-switch.db` (`file:...?mode=ro`).

### Step 3: Deep Model Inspection & Shareable Card
```bash
uvx cc-pulse inspect --base-url https://relay.example.com --api-key-env MY_RELAY_KEY \
  --model claude-sonnet-4-5 --format card
```
Outputs a ≤25-line sanitized Markdown card (zero keys, zero full URLs) covering:
- **Text answer check / TTFT streaming / model routing consistency**
- **Thinking suppression & native `tool_use` verification**
- **Zero-request cloud backend origin** (`Official / AWS Bedrock / Google Vertex / Unknown`)
- **P0/P1 authenticity checks** (cross-protocol spoofing, thinking signature presence, usage consistency)

> From source: `python check_ccswitch_health.py ...` or `pip install .`.

---

## Core Commands & Claude Code Skill

| Command | Purpose |
|---|---|
| `env-check` | Zero-key detection of env var overrides |
| `check` | Multi-tier fallback + randomized arithmetic verification |
| `inspect` | 8-dimension deep model probe + authenticity + `--format card` / `--compare` |
| `list-models` | Fetch `/v1/models` catalog with optional `--probe` verification |
| `trend` | Aggregate latency/success trends from `~/.cc-pulse/probe_history.jsonl` |

- **Agent-Native Skill**: Load [skills/cc-pulse/SKILL.md](skills/cc-pulse/SKILL.md) to run `cc-pulse` directly inside Claude Code.
- **Full CLI flags, TOML config, archive schema & personal helper scripts**: see [docs/cli-reference.md](docs/cli-reference.md).

---

## Security & Read-Only Guarantees

- Never writes to `~/.cc-switch/cc-switch.db` (strict `?mode=ro`)
- Standalone mode reads only env var names (`--api-key-env`) so secrets never enter shell history
- Disables automatic HTTP `3xx` redirects to prevent `x-api-key` / `Authorization` leakage via `302 Location`
- Sanitizes secrets and control characters across logs, errors, and `--format card`

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All checks passed (`check` all healthy without alert / `inspect` healthy / `env-check` clean) |
| 1 | Partial failure or alert threshold breached (`check` partial fail or below `--alert-threshold` / `inspect` partial fail) |
| 2 | Environment or argument error (missing config or explicit `--db`, missing args, or `env-check` conflict) |
| 3 | All targets failed (`check` or batch/`--compare` `inspect` had 0 passing targets) |

---

## Tests

Pure-stdlib unit + local mock HTTP server end-to-end tests (zero real provider calls). Currently **543 Python tests + 52 PS1 tests**:

| Test file | Scope | Cases |
|---|---|---|
| `tests/test_ccpulse_full.py` | Core unit + E2E suite (Mock SSE / protocols) | 403 |
| `tests/test_archive_trend.py` | Probe archive & trend aggregation | 54 |
| `tests/test_authenticity.py` | P0 cross-pack & thinking signature checks | 16 |
| `tests/test_usage_consistency.py` | P1 usage & response validation | 21 |
| `tests/test_cache_replay.py` | P2-A high-entropy cache replay probe | 10 |
| `tests/test_p0_protocol_fix.py` | P0 protocol inference regression | 4 |
| `tests/test_env_check.py` | Environment override detection | 14 |
| `tests/test_tui.py` | Cross-platform TUI selector | 9 |
| `tests/test_p0_p1_fixes.py` | 3xx redirect safety, closed-socket SSE, standalone & card | 12 |
| `tests/test_ps1_launcher.py` | Windows PS1 launcher E2E | 52 |
| `tests/test_docs_consistency.py` | Docs-to-code consistency guard | — |

```bash
just test-all   # Run all Python + PS1 suites
just lint       # ruff check + ruff format --check
just lint-docs  # Docs consistency guard
```

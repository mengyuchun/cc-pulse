<!-- Generated: 2026-08-13 | Files scanned: 8 | Token estimate: ~380 -->
# Architecture

## Shape
Single-app Python 3.10+ CLI. Runtime uses only the standard library.

## Entry Points
- `check_ccswitch_health.py:main` - argparse CLI; default command is `check`.
- `run_health_check.ps1` - interactive Windows launcher; invokes Python with argument arrays.
- `justfile` - developer command aliases.

## Layered Flow
```
CLI / PS1
  -> argparse command (check_ccswitch_health.py)
  -> ccpulse_store: read cc-switch SQLite (mode=ro) -> Provider + ModelTier
  -> ccpulse_probe: HTTP probe / inspect 7 dims / authenticity 6 probes
  -> ccpulse_net: HTTP transport / SSE / error classification / sanitize
  -> ccpulse_archive: append probe_history.jsonl (trend source)
  -> ccpulse_output: human output or JSON/NDJSON (ContextVar routing)
```

## Commands
- Live HTTP: `check`, `list-models`, `inspect`, `deep-dive`.
- Read-only history: `history`, `stats`, `routing`, `watch`, `analyze`, `trend`, `health`, `env-check`.

## Authenticity Probes (in ccpulse_probe, surfaced via inspect)
- P0: `crosspack` (field-level crosspack), `thinking_signature` (crypto signature presence).
- P1: `usage_consistency` (billing self-check).
- P2-A: `cache_replay` (temp=1 twin fire), P2-B: `knowledge_cutoff` (before/after bank), P2-C: `js_fingerprint` (single-token JSD).
- `_authenticity_verdict` aggregates to `clean` / `suspicious` / `inconclusive`.

## Boundaries
- SQLite access is read-only and parameterized.
- API keys are loaded from config and used only in request headers; response bodies are sanitized before `raw_body`.
- Terminal-bound external text is sanitized by `ccpulse_output.py`.
- JSON sends progress to stderr; structured output stays on stdout.

## Key Files
- `check_ccswitch_health.py` - command orchestration, provider parsing, reports, log analytics.
- `ccpulse_probe.py` - probe/stream/metadata/tools/vision, protocol payloads, authenticity probes.
- `ccpulse_net.py` - HTTP request, SSE parsing, error classification, TLS, sanitize.
- `ccpulse_store.py` - read-only SQLite access, provider/tier parsing, log queries.
- `ccpulse_archive.py` - probe_history.jsonl append/trim/trend aggregation.
- `ccpulse_env.py` - env-var override detection (silent routing).
- `ccpulse_output.py` - ContextVar output routing, terminal sanitization, ANSI styles.
- `ccpulse_tui.py` - cross-platform arrow/mouse multi-select.
- `run_health_check.ps1` - menu UX and exit-code propagation.


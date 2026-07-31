<!-- Generated: 2026-07-31 | Files scanned: 10 | Token estimate: ~330 -->
# Architecture

## Shape
Single-app Python 3.10+ CLI. Runtime uses only the standard library.

## Entry Points
- `check_ccswitch_health.py:main` - argparse CLI; default command is `check`.
- `run_health_check.ps1` - interactive Windows launcher; invokes Python with argument arrays.
- `justfile` - developer command aliases.

## Flow
```
CLI / PS1
  -> argparse command
  -> read cc-switch SQLite (mode=ro)
  -> Provider + ModelTier
  -> HTTP probe / log query
  -> human output or JSON/NDJSON
```

## Commands
- Live HTTP: `check`, `list-models`, `inspect`.
- Read-only history: `history`, `stats`, `routing`, `watch`, `analyze`.

## Boundaries
- SQLite access is read-only and parameterized.
- API keys are loaded from config and used only in request headers.
- Terminal-bound external text is sanitized by `ccpulse_output.py`.
- JSON sends progress to stderr; structured output stays on stdout.

## Key Files
- `check_ccswitch_health.py` - command orchestration, provider parsing, probes, reports, log analytics.
- `ccpulse_output.py` - ContextVar output routing, terminal sanitization, ANSI styles.
- `run_health_check.ps1` - menu UX and exit-code propagation.

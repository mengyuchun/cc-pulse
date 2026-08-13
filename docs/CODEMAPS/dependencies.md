<!-- Generated: 2026-08-13 | Token estimate: ~250 -->
# Dependencies

## Runtime
- Python 3.10+ standard library only.
- `argparse`, `sqlite3`, `urllib`, `ssl`, `threading`, `concurrent.futures`, `contextvars`, `math`, `re`.
- No package installation is required to run CC-Pulse.

## Local Integrations
- cc-switch SQLite database, read-only: default `~/.cc-switch/cc-switch.db`.
- cc-switch log directory: default `~/.cc-switch/logs`.
- CC-Pulse probe archive: default `~/.cc-pulse/probe_history.jsonl` (auto-rotated at 5MB/10k records).
- Local `claude --version` is queried once to form the default User-Agent; fallback version is built in.

## Remote Integrations
- Provider endpoints configured by cc-switch.
- Supported request families: Anthropic Messages, OpenAI Chat Completions, OpenAI Responses.
- Optional model metadata endpoint: `GET /v1/models/{id}`.

## Development
- `ruff` for lint/format.
- `just` for recipes.
- Tests use only the Python standard library and local mock HTTP servers.


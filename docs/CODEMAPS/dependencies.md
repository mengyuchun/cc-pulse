<!-- Generated: 2026-07-31 | Files scanned: 10 | Token estimate: ~230 -->
# Dependencies

## Runtime
- Python 3.10+ standard library only.
- `argparse`, `sqlite3`, `urllib`, `ssl`, `threading`, `concurrent.futures`, `contextvars`.
- No package installation is required to run CC-Pulse.

## Local Integrations
- cc-switch SQLite database, read-only: default `~/.cc-switch/cc-switch.db`.
- cc-switch log directory: default `~/.cc-switch/logs`.
- Local `claude --version` is queried once to form the default User-Agent; fallback version is built in.

## Remote Integrations
- Provider endpoints configured by cc-switch.
- Supported request families: Anthropic Messages, OpenAI Chat Completions, OpenAI Responses.
- Optional model metadata endpoint: `GET /v1/models/{id}`.

## Development
- `ruff` for lint/format.
- `just` for recipes.
- Tests use only the Python standard library and local mock HTTP servers.

<!-- Generated: 2026-07-31 | Files scanned: 10 | Token estimate: ~400 -->
# Backend / CLI Map

## Command Paths
```
main
  -> _build_parser
  -> load_providers (live commands)
  -> run_health_check | run_list_models | run_inspect
  -> run_history | run_stats | run_routing | run_watch | run_analyze
```

## Provider and Protocols
- `load_providers` -> `parse_provider` -> immutable `Provider` / `ModelTier`.
- `detect_protocol` selects Anthropic Messages, OpenAI Chat Completions, or OpenAI Responses.
- `build_probe_request` builds endpoint, headers, and payload.
- `_http_request` performs standard-library HTTPS calls with TLS controls.

## Probe Pipeline
```
run_health_check -> ThreadPoolExecutor -> probe -> probe_tier -> _http_request
run_inspect -> text / streaming / protocol / metadata / context / thinking / tools / vision
probe_stream -> _drain_sse_stream -> parse_sse_lines
```

## Output Contract
- `ccpulse_output._output_stream` is per-context; worker probes receive the parent stream explicitly.
- `say` sanitizes external terminal text and serializes writes with a lock.
- `--json` and default machine-readable `inspect` reserve stdout for JSON.

## Key Files
- `check_ccswitch_health.py` - all CLI behavior and report shaping.
- `ccpulse_output.py` - output/sanitization utility.
- `run_health_check.ps1` - `Invoke-Ccpulse`, menu functions, saved in-process options.

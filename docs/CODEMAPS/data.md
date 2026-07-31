<!-- Generated: 2026-07-31 | Files scanned: 10 | Token estimate: ~330 -->
# Data Map

## Sources
CC-Pulse never writes cc-switch data. Connections use SQLite URI `mode=ro`.

## cc-switch Tables Read
```
providers
  name, app_type, settings_config, is_current,
  in_failover_queue, sort_index

proxy_request_logs
  request/provider/model identifiers, timestamps,
  status, latency, token use, error fields
```

## Runtime Models
- `Provider` (frozen): name, app type, base URL, API key, auth mode, model tiers, routing flags, protocol.
- `ModelTier` (frozen): tier name, clean model ID, configured raw model ID.
- Probe dictionaries carry HTTP status, error category, timing, usage, and response model.

## Read Paths
- `load_providers` reads configuration into providers.
- `query_proxy_logs` backs `history` and `watch`.
- `query_stats` summarizes provider success/latency/error data.
- `query_routing` finds requested-to-actual-model mismatches.
- `query_analyze_raw` feeds day/model/provider aggregations.

## Time Filtering
`_parse_since` accepts seconds or `smhd` suffixes. `_resolve_since_or_fail` is shared by history, stats, routing, and analyze.

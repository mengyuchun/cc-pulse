"""CC-Pulse 仓储层：只读 cc-switch SQLite + 日志/统计聚合。

从 check_ccswitch_health.py 拆出；集中 schema 耦合点。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from ccpulse_net import ErrorCategory
from ccpulse_output import _sanitize_for_terminal, say
from ccpulse_probe import (
    ModelTier,
    Protocol,
    Provider,
    TIER_ENV_KEYS,
    TIER_ORDER,
)


def _fmt_ts(ts) -> str:
    if ts is None:
        return "?"
    try:
        t = float(ts)
        if t > 1e12:
            t = t / 1e9 if t > 1e15 else t / 1e3
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ts)

# --- from check_ccswitch_health.py:160-356 ---
def load_providers(db_path: str, app_type: str) -> list:
    """只读连接 cc-switch.db，加载供应商"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # meta/notes 列在部分旧库/测试库中不存在，动态检测以兼容
        cols = [r[1] for r in cur.execute("PRAGMA table_info(providers)")]
        has_meta = "meta" in cols
        has_notes = "notes" in cols
        extra_cols = []
        if has_meta:
            extra_cols.append("meta")
        if has_notes:
            extra_cols.append("notes")
        sel = (
            "SELECT name, app_type, settings_config"
            + (", " + ", ".join(extra_cols) if extra_cols else "")
            + ", is_current, in_failover_queue "
            "FROM providers WHERE app_type=? ORDER BY sort_index"
        )
        cur.execute(sel, (app_type,))
        providers = []
        for row in cur.fetchall():
            try:
                cfg = json.loads(row["settings_config"])
                api_format = None
                custom_ua = None
                if has_meta and row["meta"]:
                    try:
                        meta_obj = json.loads(row["meta"]) or {}
                        api_format = meta_obj.get("apiFormat")
                        custom_ua = meta_obj.get("customUserAgent")
                    except (json.JSONDecodeError, TypeError):
                        pass
                notes = (row["notes"] if has_notes and row["notes"] else "") or ""
                providers.extend(
                    parse_provider(
                        row["name"],
                        row["app_type"],
                        cfg,
                        bool(row["is_current"]),
                        bool(row["in_failover_queue"]),
                        api_format,
                        custom_ua,
                        notes,
                    )
                )
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as e:
                say(f"  跳过 [{row['name']}]: {e}")
        return providers
    finally:
        conn.close()


def parse_provider(
    name,
    app_type,
    cfg,
    is_current,
    in_failover,
    api_format=None,
    custom_user_agent=None,
    notes="",
) -> list:
    """解析单个供应商的 settings_config，返回 Provider 列表

    api_format: cc-switch meta.apiFormat 字段（anthropic / openai_chat / openai_responses），
    是供应商实际的 API 协议配置，优先于 base_url 后缀与 app_type 默认推断。
    """
    out = []
    if app_type == "claude":
        env = cfg.get("env", {})
        base = env.get("ANTHROPIC_BASE_URL", "")
        # 认证头按配置走：优先 AUTH_TOKEN（中转站），其次 API_KEY（官方）
        token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        auth_mode = "authtoken"
        if not token:
            token = env.get("ANTHROPIC_API_KEY", "")
            auth_mode = "apikey"
        if not token or not base:
            return out
        # 收集所有档位模型（不去重，保留配置顺序）
        tiers = []
        for tier in TIER_ORDER:
            v = env.get(TIER_ENV_KEYS[tier], "")
            if v:
                clean = re.sub(r"\[.*?\]$", "", v)  # 去 [1M] 后缀
                tiers.append(ModelTier(tier, clean, v))
        if not tiers:
            return out
        # 协议推断优先级：meta.apiFormat 配置 > base_url 显式后缀 > app_type 默认
        # 修复：L站4w次 等配了 apiFormat=openai_chat 却被默认当 Anthropic Messages 发 403
        base_stripped = base.rstrip("/")
        proto = Protocol.UNKNOWN
        _API_FORMAT_TO_PROTO = {
            "anthropic": Protocol.ANTHROPIC_MESSAGES,
            "openai_chat": Protocol.OPENAI_CHAT_COMPLETIONS,
            "openai_responses": Protocol.OPENAI_RESPONSES,
        }
        if api_format and api_format in _API_FORMAT_TO_PROTO:
            proto = _API_FORMAT_TO_PROTO[api_format]
        elif "/chat/completions" in base_stripped:
            proto = Protocol.OPENAI_CHAT_COMPLETIONS
        elif (
            base_stripped.endswith("/v1/responses") or "/v1/responses" in base_stripped
        ):
            proto = Protocol.OPENAI_RESPONSES
        elif base_stripped.endswith("/v1/messages") or "/v1/messages" in base_stripped:
            proto = Protocol.ANTHROPIC_MESSAGES
        else:
            # 无显式后缀时，按 cc-switch 配置的 app_type 默认推断
            # 注意：不再完全信任 is_openrouter（旧粗粒度标志仍保留向后兼容）
            proto = {
                "claude": Protocol.ANTHROPIC_MESSAGES,
                "codex": Protocol.OPENAI_RESPONSES,
                "openclaw": Protocol.OPENAI_CHAT_COMPLETIONS,
            }.get(app_type, Protocol.UNKNOWN)
        # 向后兼容：is_openrouter 仍由 URL 子串判断（旧调用方依赖），protocol 才是权威
        is_or_compat = "/chat/completions" in base_stripped
        # protocol_source：apiFormat 显式配置 / base_url 后缀 / app_type 默认
        p_source = (
            "api_format"
            if api_format and api_format in _API_FORMAT_TO_PROTO
            else "url_suffix"
            if "/chat/completions" in base_stripped
            or "/v1/responses" in base_stripped
            or "/v1/messages" in base_stripped
            else ""
        )
        out.append(
            Provider(
                name,
                "claude",
                base,
                token,
                auth_mode,
                tiers,
                is_current,
                in_failover,
                is_or_compat,
                protocol=proto,
                protocol_source=p_source,
                custom_user_agent=custom_user_agent,
                notes=notes,
            )
        )
    elif app_type == "codex":
        auth = cfg.get("auth", {})
        token = auth.get("OPENAI_API_KEY", "")
        config_str = cfg.get("config", "")
        m = re.search(r'base_url\s*=\s*"([^"]+)"', config_str)
        base = m.group(1) if m else ""
        mm = re.search(r'^\s*model\s*=\s*"([^"]+)"', config_str, re.MULTILINE)
        model = mm.group(1) if mm else "gpt-5"
        if token and base:
            out.append(
                Provider(
                    name,
                    "codex",
                    base,
                    token,
                    "bearer",
                    [ModelTier("default", model, model)],
                    is_current,
                    in_failover,
                    custom_user_agent=custom_user_agent,
                    notes=notes,
                )
            )
    elif app_type == "openclaw":
        token = cfg.get("apiKey", "")
        base = cfg.get("baseUrl", "")
        tiers = [
            ModelTier(m.get("name", "default"), m["id"], m["id"])
            for m in cfg.get("models", [])
            if isinstance(m, dict) and m.get("id")
        ]
        if token and base and tiers:
            out.append(
                Provider(
                    name,
                    "openclaw",
                    base,
                    token,
                    "bearer",
                    tiers,
                    is_current,
                    in_failover,
                    custom_user_agent=custom_user_agent,
                    notes=notes,
                )
            )
    return out



# --- from check_ccswitch_health.py:1578-1934 ---
def load_provider_id_map(db_path: str) -> dict:
    """provider_id(uuid) -> name；只读。"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        out = {}
        for pid, name in conn.execute("SELECT id, name FROM providers"):
            if pid:
                out[pid] = name or pid
        return out
    finally:
        conn.close()


def resolve_provider_name(pid: str | None, id_map: dict) -> str:
    if not pid:
        return "?"
    if pid in id_map:
        return id_map[pid]
    return f"deleted:{str(pid)[:8]}"


def classify_log_error(status_code: int | None, error_message: str | None) -> str:
    """把 proxy_request_logs 的 status/error_message 映射到 ErrorCategory 字符串。"""
    msg = error_message or ""
    low = msg.lower()
    st = int(status_code or 0)

    # 关键词优先（比纯 status 更准）
    if any(
        k in low
        for k in (
            "invalid api",
            "missing api",
            "authentication",
            "unauthorized",
            "forbidden",
        )
    ):
        return ErrorCategory.AUTH.value
    if any(k in msg for k in ("余额", "预扣", "额度")) or "insufficient" in low:
        return ErrorCategory.AUTH.value  # 额度/鉴权类，归 authentication
    if (
        any(k in low for k in ("rate limit", "rate_limit", "too many", "429"))
        or st == 429
    ):
        return ErrorCategory.RATE_LIMIT.value
    if (
        any(
            k in low
            for k in (
                "model_not",
                "no available channel",
                "unknown model",
                "model does not exist",
            )
        )
        or st == 404
    ):
        return ErrorCategory.MODEL_NOT_FOUND.value
    if any(k in low for k in ("timeout", "首包超时", "ttft")):
        return ErrorCategory.TTFT_TIMEOUT.value
    if any(k in low for k in ("connect", "连接", "tls", "certificate")) or st in (
        502,
        522,
        524,
    ):
        return ErrorCategory.NETWORK.value
    if any(
        k in low
        for k in ("schema", "invalid request", "maximum prompt", "too large", "413")
    ) or st in (400, 413, 422):
        return ErrorCategory.PROTOCOL_INCOMPATIBLE.value
    if st in (401, 403, 402):
        return ErrorCategory.AUTH.value
    if st >= 500:
        return ErrorCategory.SERVER.value
    if st and st != 200:
        # 兜底：1xx/3xx 等罕见 status → protocol_incompatible（语义最接近的现有类别）
        return ErrorCategory.PROTOCOL_INCOMPATIBLE.value
    if msg.strip():
        return ErrorCategory.UNKNOWN.value
    return ErrorCategory.NONE.value


def _open_ro(db_path: str):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _table_exists(conn, name: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None


def query_proxy_logs(
    db_path: str,
    *,
    since_ts: int | None = None,
    limit: int = 20,
    fails_only: bool = False,
    provider_substr: str | None = None,
) -> list:
    """查询 proxy_request_logs，返回 dict 列表（已解析供应商名与 error_category）。"""
    id_map = load_provider_id_map(db_path)
    # reverse name filter: match provider ids whose name contains substr
    name_pids = None
    if provider_substr:
        sub = provider_substr.lower()
        name_pids = {pid for pid, n in id_map.items() if sub in (n or "").lower()}

    conn = _open_ro(db_path)
    try:
        if not _table_exists(conn, "proxy_request_logs"):
            return []
        where = []
        args: list = []
        if since_ts is not None:
            where.append("created_at >= ?")
            args.append(since_ts)
        if fails_only:
            where.append(
                "(status_code IS NULL OR status_code != 200 "
                "OR (error_message IS NOT NULL AND error_message != ''))"
            )
        if name_pids is not None:
            if not name_pids:
                return []
            placeholders = ",".join("?" * len(name_pids))
            where.append(f"provider_id IN ({placeholders})")
            args.extend(name_pids)
        sql = "SELECT * FROM proxy_request_logs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        cols = [c[1] for c in conn.execute("PRAGMA table_info(proxy_request_logs)")]
        rows = []
        for row in conn.execute(sql, args):
            d = dict(zip(cols, row))
            d["provider_name"] = resolve_provider_name(d.get("provider_id"), id_map)
            d["error_category"] = classify_log_error(
                d.get("status_code"), d.get("error_message")
            )
            d["routing_mismatch"] = bool(
                d.get("request_model")
                and d.get("model")
                and d.get("request_model") != d.get("model")
            )
            d["created_at_fmt"] = _fmt_ts(d.get("created_at"))
            rows.append(d)
        return rows
    finally:
        conn.close()


def query_stats(db_path: str, *, since_ts: int | None = None) -> list:
    """按供应商汇总：请求数、成功、失败、主失败因、中位延迟近似、路由不一致率。

    无 since_ts 时默认拉最近 30 天，防止大库全表扫描 OOM。
    """
    if since_ts is None:
        since_ts = int(time.time()) - 30 * 86400
    id_map = load_provider_id_map(db_path)
    conn = _open_ro(db_path)
    try:
        if not _table_exists(conn, "proxy_request_logs"):
            return []
        where = "WHERE created_at >= ?"
        args: list = [since_ts]
        # 拉原始行做聚合（2万级可接受）
        cols = [c[1] for c in conn.execute("PRAGMA table_info(proxy_request_logs)")]
        sql = f"SELECT * FROM proxy_request_logs {where}"
        buckets: dict[str, dict] = {}
        for row in conn.execute(sql, args):
            d = dict(zip(cols, row))
            pid = d.get("provider_id") or "?"
            b = buckets.get(pid)
            if b is None:
                b = {
                    "provider_id": pid,
                    "provider_name": resolve_provider_name(pid, id_map),
                    "total": 0,
                    "ok": 0,
                    "fail": 0,
                    "mismatch": 0,
                    "latencies": [],
                    "fail_cats": {},
                    "status_counts": {},
                    "cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
                buckets[pid] = b
            b["total"] += 1
            st = d.get("status_code")
            err = d.get("error_message")
            is_fail = st is None or st != 200 or (err is not None and err != "")
            if is_fail:
                b["fail"] += 1
                cat = classify_log_error(st, err)
                b["fail_cats"][cat] = b["fail_cats"].get(cat, 0) + 1
                key = str(st)
                b["status_counts"][key] = b["status_counts"].get(key, 0) + 1
            else:
                b["ok"] += 1
            # 成本聚合（total_cost_usd 是 TEXT，可能为空/非数字）
            cost_raw = d.get("total_cost_usd")
            if cost_raw:
                try:
                    b["cost_usd"] += float(cost_raw)
                except (TypeError, ValueError):
                    pass
            # token 用量聚合
            for tk, dk in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
                tv = d.get(dk)
                if isinstance(tv, (int, float)) and tv >= 0:
                    b[tk] += int(tv)
            if (
                d.get("request_model")
                and d.get("model")
                and d.get("request_model") != d.get("model")
            ):
                b["mismatch"] += 1
            lat = d.get("latency_ms")
            if isinstance(lat, (int, float)) and lat >= 0:
                b["latencies"].append(lat)
        out = []
        for b in buckets.values():
            lats = sorted(b["latencies"])
            med = lats[len(lats) // 2] if lats else None
            top_cat = None
            if b["fail_cats"]:
                top_cat = max(b["fail_cats"].items(), key=lambda x: x[1])[0]
            total = b["total"] or 1
            out.append(
                {
                    "provider_id": b["provider_id"],
                    "provider_name": b["provider_name"],
                    "is_deleted": b["provider_name"].startswith("deleted:"),
                    "total": b["total"],
                    "ok": b["ok"],
                    "fail": b["fail"],
                    "success_rate": round(b["ok"] / total, 4),
                    "mismatch": b["mismatch"],
                    "mismatch_rate": round(b["mismatch"] / total, 4),
                    "median_latency_ms": med,
                    "top_fail_category": top_cat,
                    "fail_categories": b["fail_cats"],
                    "status_counts": b["status_counts"],
                    "cost_usd": round(b["cost_usd"], 4),
                    "input_tokens": b["input_tokens"],
                    "output_tokens": b["output_tokens"],
                }
            )
        out.sort(key=lambda x: (-x["fail"], -x["total"]))
        return out
    finally:
        conn.close()


def query_routing(
    db_path: str, *, since_ts: int | None = None, limit: int = 20
) -> list:
    """静默路由排行：request_model -> model。"""
    conn = _open_ro(db_path)
    try:
        if not _table_exists(conn, "proxy_request_logs"):
            return []
        where = "WHERE request_model IS NOT NULL AND model IS NOT NULL AND request_model != model"
        args: list = []
        if since_ts is not None:
            where += " AND created_at >= ?"
            args.append(since_ts)
        sql = f"""
            SELECT request_model, model, COUNT(*) AS n
            FROM proxy_request_logs
            {where}
            GROUP BY request_model, model
            ORDER BY n DESC
            LIMIT ?
        """
        args.append(int(limit))
        return [
            {"request_model": a, "actual_model": b, "count": n}
            for a, b, n in conn.execute(sql, args)
        ]
    finally:
        conn.close()


def query_provider_health(db_path: str) -> list:
    """读 cc-switch 的 provider_health 表（被动流量健康度，非主动探测）。

    返回每供应商：name / is_healthy / consecutive_failures / last_error / last_success_at。
    """
    id_map = load_provider_id_map(db_path)
    conn = _open_ro(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "provider_health"):
            return []
        rows = []
        for r in conn.execute(
            "SELECT provider_id, app_type, is_healthy, consecutive_failures, "
            "last_success_at, last_failure_at, last_error, updated_at "
            "FROM provider_health"
        ):
            d = dict(r)
            d["provider_name"] = id_map.get(d.get("provider_id")) or d.get(
                "provider_id", "?"
            )
            rows.append(d)
        # 不健康的排前面，再按失败次数降序
        rows.sort(
            key=lambda x: (x.get("is_healthy", 1), -x.get("consecutive_failures", 0))
        )
        return rows
    finally:
        conn.close()


def summarize_provider_history(
    db_path: str, provider_name: str, since_ts: int | None = None
) -> dict | None:
    """单个供应商 24h 摘要，供 check/inspect 挂钩。"""
    stats = query_stats(db_path, since_ts=since_ts)
    for s in stats:
        if s["provider_name"] == provider_name:
            return s
    # 模糊
    low = provider_name.lower()
    for s in stats:
        if low in s["provider_name"].lower():
            return s
    return None


def read_log_file_tail(path: str, lines: int = 50, keyword: str | None = None) -> list:
    """读磁盘日志尾部（P3）。大文件只读末尾约 512KB。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return [f"日志文件不存在: {path}"]
    size = p.stat().st_size
    chunk = min(size, 512 * 1024)
    with open(p, "rb") as f:
        if size > chunk:
            f.seek(-chunk, 2)
        data = f.read().decode("utf-8", errors="replace")
    raw_lines = data.splitlines()
    if keyword:
        raw_lines = [ln for ln in raw_lines if keyword.lower() in ln.lower()]
    return raw_lines[-lines:]



# --- from check_ccswitch_health.py:2245-2596 ---
def _percentile(sorted_vals: list, p: float) -> float | None:
    """线性插值分位数。p 取 0-100。sorted_vals 需已排序。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


def _sparkline(values: list, width: int = 20) -> str:
    """ASCII sparkline：values 均匀采样到 width 个桶，每桶取平均。"""
    if not values:
        return "-" * width
    ticks = "▁▂▃▄▅▆▇█"
    n = len(values)
    if n <= width:
        buckets = [float(v) for v in values]
    else:
        buckets = []
        for i in range(width):
            lo = int(i * n / width)
            hi = int((i + 1) * n / width)
            seg = values[lo:hi] or [values[lo]]
            buckets.append(sum(seg) / len(seg))
    vmin, vmax = min(buckets), max(buckets)
    span = vmax - vmin
    if span <= 1e-9:
        return ticks[len(ticks) // 2] * len(buckets)
    out = []
    for v in buckets:
        idx = int((v - vmin) / span * (len(ticks) - 1))
        idx = max(0, min(len(ticks) - 1, idx))
        out.append(ticks[idx])
    return "".join(out)


def _day_key(ts) -> str | None:
    """unix 秒 → 'YYYY-MM-DD'。None → None。"""
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def query_analyze_raw(db_path: str, *, since_ts: int | None = None) -> list:
    """一次拉全分析用行；比 query_proxy_logs 更瘦（只取必要列）。

    无 since_ts 时默认拉最近 30 天，防止大库全表扫描 OOM。
    """
    if since_ts is None:
        since_ts = int(time.time()) - 30 * 86400
    conn = _open_ro(db_path)
    try:
        if not _table_exists(conn, "proxy_request_logs"):
            return []
        where = "WHERE created_at >= ?"
        args: list = [since_ts]
        sql = (
            "SELECT created_at, provider_id, status_code, error_message, "
            "request_model, model, latency_ms, first_token_ms, "
            "input_tokens, output_tokens "
            f"FROM proxy_request_logs {where}"
        )
        rows = []
        for r in conn.execute(sql, args):
            rows.append(
                {
                    "created_at": r[0],
                    "provider_id": r[1],
                    "status_code": r[2],
                    "error_message": r[3],
                    "request_model": r[4],
                    "model": r[5],
                    "latency_ms": r[6],
                    "first_token_ms": r[7],
                    "input_tokens": r[8],
                    "output_tokens": r[9],
                    "day": _day_key(r[0]),
                }
            )
        return rows
    finally:
        conn.close()


def _row_is_fail(r: dict) -> bool:
    st = r.get("status_code")
    err = r.get("error_message")
    return st is None or st != 200 or (err is not None and err != "")


def analyze_by_provider_day(rows: list, id_map: dict) -> dict:
    """供应商 × 日期 成功率矩阵。返回 {providers, days, cells, day_totals}。"""
    grid: dict[tuple, dict] = {}
    day_totals: dict[str, dict] = {}
    prov_totals: dict[str, dict] = {}
    days_set: set[str] = set()
    provs_set: set[str] = set()
    for r in rows:
        d = r.get("day")
        pid = r.get("provider_id") or "?"
        if not d:
            continue
        days_set.add(d)
        provs_set.add(pid)
        key = (pid, d)
        cell = grid.setdefault(key, {"total": 0, "ok": 0, "fail": 0})
        cell["total"] += 1
        if _row_is_fail(r):
            cell["fail"] += 1
        else:
            cell["ok"] += 1
        dt = day_totals.setdefault(d, {"total": 0, "ok": 0, "fail": 0})
        dt["total"] += 1
        if _row_is_fail(r):
            dt["fail"] += 1
        else:
            dt["ok"] += 1
        pt = prov_totals.setdefault(pid, {"total": 0, "ok": 0, "fail": 0})
        pt["total"] += 1
        if _row_is_fail(r):
            pt["fail"] += 1
        else:
            pt["ok"] += 1
    days = sorted(days_set)
    provs = sorted(provs_set, key=lambda p: -prov_totals[p]["total"])
    cells = []
    for pid in provs:
        row_cells = []
        for d in days:
            c = grid.get((pid, d))
            if c is None:
                row_cells.append(None)
            else:
                sr = c["ok"] / c["total"] if c["total"] else 0.0
                row_cells.append(
                    {
                        "total": c["total"],
                        "ok": c["ok"],
                        "fail": c["fail"],
                        "success_rate": round(sr, 4),
                    }
                )
        pt = prov_totals[pid]
        cells.append(
            {
                "provider_id": pid,
                "provider_name": resolve_provider_name(pid, id_map),
                "row_totals": {
                    "total": pt["total"],
                    "ok": pt["ok"],
                    "fail": pt["fail"],
                    "success_rate": round(pt["ok"] / pt["total"], 4)
                    if pt["total"]
                    else 0.0,
                },
                "days": row_cells,
            }
        )
    day_summary = []
    for d in days:
        t = day_totals[d]
        sr = t["ok"] / t["total"] if t["total"] else 0.0
        day_summary.append(
            {
                "date": d,
                "total": t["total"],
                "ok": t["ok"],
                "fail": t["fail"],
                "success_rate": round(sr, 4),
            }
        )
    return {"days": days, "day_summary": day_summary, "providers": cells}


def analyze_by_model(rows: list) -> list:
    """按 actual model 聚合：延迟分位数、成功率、平均 token。"""
    buckets: dict[str, dict] = {}
    for r in rows:
        m = r.get("model") or "?"
        b = buckets.setdefault(
            m,
            {
                "model": m,
                "total": 0,
                "ok": 0,
                "fail": 0,
                "latencies": [],
                "ttfts": [],
                "input_tokens_sum": 0,
                "output_tokens_sum": 0,
                "tok_count": 0,
            },
        )
        b["total"] += 1
        if _row_is_fail(r):
            b["fail"] += 1
        else:
            b["ok"] += 1
            lat = r.get("latency_ms")
            if isinstance(lat, (int, float)) and lat >= 0:
                b["latencies"].append(float(lat))
            ttft = r.get("first_token_ms")
            if isinstance(ttft, (int, float)) and ttft >= 0:
                b["ttfts"].append(float(ttft))
            it = r.get("input_tokens")
            ot = r.get("output_tokens")
            if isinstance(it, (int, float)) or isinstance(ot, (int, float)):
                b["input_tokens_sum"] += int(it or 0)
                b["output_tokens_sum"] += int(ot or 0)
                b["tok_count"] += 1
    out = []
    for b in buckets.values():
        lats = sorted(b["latencies"])
        ttfts = sorted(b["ttfts"])
        total = b["total"] or 1
        avg_in = b["input_tokens_sum"] / b["tok_count"] if b["tok_count"] else None
        avg_out = b["output_tokens_sum"] / b["tok_count"] if b["tok_count"] else None
        out.append(
            {
                "model": b["model"],
                "total": b["total"],
                "ok": b["ok"],
                "fail": b["fail"],
                "success_rate": round(b["ok"] / total, 4),
                "lat_p50": _percentile(lats, 50),
                "lat_p95": _percentile(lats, 95),
                "lat_p99": _percentile(lats, 99),
                "ttft_p50": _percentile(ttfts, 50),
                "ttft_p95": _percentile(ttfts, 95),
                "avg_input_tokens": round(avg_in, 1) if avg_in is not None else None,
                "avg_output_tokens": round(avg_out, 1) if avg_out is not None else None,
            }
        )
    out.sort(key=lambda x: -x["total"])
    return out


def analyze_by_day(rows: list) -> list:
    """按天：总请求、成功率、主失败因、独立供应商数、延迟 p50/p95。"""
    by_day: dict[str, dict] = {}
    for r in rows:
        d = r.get("day")
        if not d:
            continue
        b = by_day.setdefault(
            d,
            {
                "date": d,
                "total": 0,
                "ok": 0,
                "fail": 0,
                "latencies": [],
                "fail_cats": {},
                "provs": set(),
            },
        )
        b["total"] += 1
        if r.get("provider_id"):
            b["provs"].add(r["provider_id"])
        if _row_is_fail(r):
            b["fail"] += 1
            cat = classify_log_error(r.get("status_code"), r.get("error_message"))
            b["fail_cats"][cat] = b["fail_cats"].get(cat, 0) + 1
        else:
            b["ok"] += 1
            lat = r.get("latency_ms")
            if isinstance(lat, (int, float)) and lat >= 0:
                b["latencies"].append(float(lat))
    out = []
    for b in sorted(by_day.values(), key=lambda x: x["date"]):
        lats = sorted(b["latencies"])
        total = b["total"] or 1
        top_cat = None
        if b["fail_cats"]:
            top_cat = max(b["fail_cats"].items(), key=lambda x: x[1])[0]
        out.append(
            {
                "date": b["date"],
                "total": b["total"],
                "ok": b["ok"],
                "fail": b["fail"],
                "success_rate": round(b["ok"] / total, 4),
                "unique_providers": len(b["provs"]),
                "lat_p50": _percentile(lats, 50),
                "lat_p95": _percentile(lats, 95),
                "top_fail_category": top_cat,
            }
        )
    return out


def analyze_provider_deep(
    rows: list, provider_substr: str, id_map: dict
) -> dict | None:
    """单供应商深度：过滤后按 (day, model) 交叉。"""
    if not provider_substr:
        return None
    sub = provider_substr.lower()
    pids = {pid for pid, n in id_map.items() if sub in (n or "").lower()}
    if not pids:
        return None
    filtered = [r for r in rows if r.get("provider_id") in pids]
    if not filtered:
        return {
            "provider_substr": provider_substr,
            "match_provider_ids": sorted(pids),
            "total": 0,
            "by_day": [],
            "by_model": [],
            "by_day_model": [],
        }
    by_day = analyze_by_day(filtered)
    by_model = analyze_by_model(filtered)
    # (day, actual_model) 交叉
    cross: dict[tuple, dict] = {}
    for r in filtered:
        d = r.get("day")
        m = r.get("model") or "?"
        if not d:
            continue
        c = cross.setdefault(
            (d, m), {"date": d, "model": m, "total": 0, "ok": 0, "fail": 0}
        )
        c["total"] += 1
        if _row_is_fail(r):
            c["fail"] += 1
        else:
            c["ok"] += 1
    dm = []
    for c in cross.values():
        total = c["total"] or 1
        dm.append({**c, "success_rate": round(c["ok"] / total, 4)})
    dm.sort(key=lambda x: (x["date"], -x["total"]))
    prov_names = sorted({resolve_provider_name(pid, id_map) for pid in pids})
    return {
        "provider_substr": provider_substr,
        "match_provider_names": prov_names,
        "match_provider_ids": sorted(pids),
        "total": len(filtered),
        "by_day": by_day,
        "by_model": by_model,
        "by_day_model": dm,
    }




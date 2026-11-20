#!/usr/bin/env python3
"""独立个人辅助脚本：只读 cc-switch 代理请求日志（history / stats / routing）。

从主 CLI（check_ccswitch_health.py）迁出，使 cc-pulse 聚焦主动探活与模型诊断，
同时保留维护者本机基于 cc-switch.db 的日志排查能力。
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ccpulse_archive import parse_since
from ccpulse_output import (
    _c,
    _output_stream,
    _pad,
    _sanitize_for_terminal,
    _say_colored,
    say,
)
from ccpulse_probe import _status_badge
from ccpulse_store import (
    _provider_api_keys,
    _sanitize_log_text,
    query_proxy_logs,
    query_routing,
    query_stats,
    read_log_file_tail,
)

DB_PATH = str(Path.home() / ".cc-switch" / "cc-switch.db")


def _resolve_since_or_fail(args, output) -> tuple[int | None, int | None]:
    try:
        return parse_since(getattr(args, "since", None)), None
    except ValueError as exc:
        output(str(exc))
        return None, 2


def run_history(args, output) -> int:
    since_ts, error_code = _resolve_since_or_fail(args, output)
    if error_code is not None:
        return error_code
    limit = getattr(args, "limit", 20) or 20
    fails = getattr(args, "fails", False)
    prov = getattr(args, "provider", None) or None
    rows = query_proxy_logs(
        args.db, since_ts=since_ts, limit=limit, fails_only=fails, provider_substr=prov
    )
    report = {
        "schema_version": 1,
        "command": "history",
        "since": getattr(args, "since", None),
        "limit": limit,
        "fails_only": fails,
        "count": len(rows),
        "entries": rows,
    }
    log_file = getattr(args, "log_file", None)
    if log_file:
        secrets = _provider_api_keys(args.db, {})
        report["log_file_tail"] = [
            _sanitize_for_terminal(_sanitize_log_text(ln, secrets))
            for ln in read_log_file_tail(
                log_file,
                lines=getattr(args, "log_lines", 50) or 50,
                keyword=getattr(args, "log_keyword", None),
            )
        ]
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
    else:
        output(f"history: {len(rows)} 条{'（仅失败）' if fails else ''}")
        for i, r in enumerate(rows, 1):
            st = r.get("status_code")
            ok = st == 200 and not r.get("error_message")
            badge = _status_badge(ok, st, r.get("error_category"))
            pname = _sanitize_for_terminal(str(r.get("provider_name") or "?"))
            ts = r.get("created_at_fmt") or "?"
            _say_colored(f"[{i:02d}] {badge} {_c(ts, 'dim')}  {pname}")
            req_m = _sanitize_for_terminal(str(r.get("request_model") or "?"))
            act_m = _sanitize_for_terminal(str(r.get("model") or "?"))
            _say_colored(
                f"     {req_m} -> {act_m}  status={st}  lat={r.get('latency_ms')}ms"
            )
    return 0


def run_stats(args, output) -> int:
    since_ts, error_code = _resolve_since_or_fail(args, output)
    if error_code is not None:
        return error_code
    stats = query_stats(args.db, since_ts=since_ts)
    if not getattr(args, "include_deleted", False):
        stats = [s for s in stats if not s.get("is_deleted")]
    report = {
        "schema_version": 1,
        "command": "stats",
        "since": getattr(args, "since", None),
        "providers": stats,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        output(f"stats: {len(stats)} 个供应商")
        for s in stats:
            pname = _sanitize_for_terminal(s["provider_name"][:24])
            output(
                f"  {_pad(pname, 24)} total={s['total']} ok={s['success_rate'] * 100:.0f}% fail={s['fail']}"
            )
    return 0


def run_routing(args, output) -> int:
    since_ts, error_code = _resolve_since_or_fail(args, output)
    if error_code is not None:
        return error_code
    limit = getattr(args, "limit", 20) or 20
    pairs = query_routing(args.db, since_ts=since_ts, limit=limit)
    report = {
        "schema_version": 1,
        "command": "routing",
        "since": getattr(args, "since", None),
        "pairs": pairs,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        output(f"routing: top {len(pairs)} 静默路由")
        for i, p in enumerate(pairs, 1):
            output(
                f"[{i:02d}] {p['count']:5d}×  {p['request_model']}  =>  {p['actual_model']}"
            )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="cc-switch 只读日志辅助脚本（独立个人工具）"
    )
    ap.add_argument(
        "--db", default=DB_PATH, help=f"cc-switch.db 路径 (默认: {DB_PATH})"
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_hist = sub.add_parser("history", help="读取代理请求日志")
    p_hist.add_argument("--db", default=argparse.SUPPRESS)
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.add_argument("--fails", action="store_true")
    p_hist.add_argument("--since", default=None)
    p_hist.add_argument("--provider", default=None)
    p_hist.add_argument("--log-file", default=None)
    p_hist.add_argument("--log-lines", type=int, default=50)
    p_hist.add_argument("--log-keyword", default=None)
    p_hist.add_argument("--json", action="store_true")

    p_stats = sub.add_parser("stats", help="按供应商汇总代理统计")
    p_stats.add_argument("--db", default=argparse.SUPPRESS)
    p_stats.add_argument("--since", default="7d")
    p_stats.add_argument("--include-deleted", action="store_true")
    p_stats.add_argument("--json", action="store_true")

    p_route = sub.add_parser("routing", help="静默路由排行")
    p_route.add_argument("--db", default=argparse.SUPPRESS)
    p_route.add_argument("--since", default="7d")
    p_route.add_argument("--limit", type=int, default=20)
    p_route.add_argument("--json", action="store_true")

    args = ap.parse_args()
    token = _output_stream.set(
        sys.stderr if getattr(args, "json", False) else sys.stdout
    )
    try:
        if not Path(args.db).exists():
            say(f"数据库不存在: {args.db}")
            return 2
        if args.command == "history":
            return run_history(args, say)
        if args.command == "stats":
            return run_stats(args, say)
        if args.command == "routing":
            return run_routing(args, say)
        return 2
    finally:
        _output_stream.reset(token)


if __name__ == "__main__":
    sys.exit(main())

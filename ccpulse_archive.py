"""探测历史本地归档 + 趋势聚合（trend 子命令）。

check/inspect 每次探测后追加一行 JSONL 到本地归档（默认 ~/.cc-pulse/probe_history.jsonl）；
trend 读取归档，按供应商（可选按模型）聚合成功率/延迟分位/错误分类/按天趋势。
纯标准库，只写 CC-Pulse 自己的归档，绝不碰 cc-switch 的库。
"""

import json
import os
import re
import time

from ccpulse_output import _pad, _sanitize_for_terminal


def parse_since(s: str | None) -> int | None:
    """解析 --since：24h / 7d / 30m / 3600 → unix 秒下限；None 表示不限。"""
    if not s:
        return None
    s = str(s).strip().lower()
    now = int(time.time())
    if s.isdigit():
        return now - int(s)
    m = re.fullmatch(r"(\d+)([smhd])", s)
    if not m:
        raise ValueError(f"无法解析 --since: {s!r}（示例: 24h / 7d / 30m / 3600）")
    n, u = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
    return now - n * mult


def archive_path(override: str | None = None) -> str:
    """归档文件路径：override 非空用之；否则 ~/.cc-pulse/probe_history.jsonl。"""
    if override:
        from pathlib import Path

        p = Path(override).resolve()
        home = Path.home()
        if not (p.is_relative_to(home) or p.is_relative_to(Path.cwd())):
            raise ValueError(f"归档路径必须在 {home} 或当前目录下: {p}")
        return str(p)
    return os.path.join(os.path.expanduser("~"), ".cc-pulse", "probe_history.jsonl")


def append_record(path: str, record: dict) -> None:
    """确保父目录存在，追加一行 JSON + 换行，flush。IO 失败照常抛异常。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def load_records(path: str) -> list[dict]:
    """逐行解析 JSONL，损坏行跳过；文件不存在返回 []。"""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """线性插值分位数；p 取 0-100，sorted_vals 需已排序。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


def _day_key(ts) -> str | None:
    """unix 秒 → 'YYYY-MM-DD'；None/非法 → None。"""
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# 测试数据供应商名（从归档聚合中排除，防止测试运行污染真实趋势）
_TEST_PROVIDERS = {"Mock-Provider", "Prov-A", "Prov-B", "Prov-C"}


def build_trend(
    records: list[dict],
    since_ts: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    include_test: bool = False,
) -> dict:
    """按供应商聚合成功率/延迟分位/错误分类/按天趋势；since/provider/model 过滤收窄。

    默认排除测试数据供应商（Mock-Provider/Prov-A/B/C），避免测试运行污染真实趋势。
    """
    recs = []
    for r in records:
        if not include_test and r.get("provider") in _TEST_PROVIDERS:
            continue
        if since_ts is not None:
            try:
                if float(r.get("ts")) < since_ts:
                    continue
            except (TypeError, ValueError):
                continue
        if provider is not None and r.get("provider") != provider:
            continue
        if model is not None and r.get("model") != model:
            continue
        recs.append(r)

    buckets: dict[str, dict] = {}
    for r in recs:
        p = r.get("provider") or "?"
        b = buckets.setdefault(
            p,
            {
                "provider": p,
                "total": 0,
                "ok": 0,
                "fail": 0,
                "latencies": [],
                "ttfts": [],
                "error_categories": {},
                "by_day": {},
            },
        )
        b["total"] += 1
        ok = r.get("status") == "ok"
        if ok:
            b["ok"] += 1
            lat = r.get("latency")
            if isinstance(lat, (int, float)) and lat >= 0:
                b["latencies"].append(float(lat))
            ttft = r.get("ttft")
            if isinstance(ttft, (int, float)) and ttft >= 0:
                b["ttfts"].append(float(ttft))
        else:
            b["fail"] += 1
            cat = r.get("error_category") or "unknown"
            b["error_categories"][cat] = b["error_categories"].get(cat, 0) + 1
        d = _day_key(r.get("ts"))
        if d:
            day = b["by_day"].setdefault(d, {"date": d, "total": 0, "ok": 0, "fail": 0})
            day["total"] += 1
            if ok:
                day["ok"] += 1
            else:
                day["fail"] += 1

    providers = []
    for b in buckets.values():
        total = b["total"] or 1
        by_day = sorted(b["by_day"].values(), key=lambda x: x["date"])
        for d in by_day:
            d["success_rate"] = round(d["ok"] / d["total"], 4)
        lats = sorted(b["latencies"])
        ttfts = sorted(b["ttfts"])
        # 趋势方向：by_day 首尾成功率对比（>0.1 升 / <-0.1 降 / 其余稳定）
        direction = None
        if len(by_day) >= 2:
            first_sr = by_day[0].get("success_rate")
            last_sr = by_day[-1].get("success_rate")
            if first_sr is not None and last_sr is not None:
                diff = last_sr - first_sr
                if diff > 0.1:
                    direction = "up"
                elif diff < -0.1:
                    direction = "down"
                else:
                    direction = "stable"
        providers.append(
            {
                "provider": b["provider"],
                "total": b["total"],
                "ok": b["ok"],
                "fail": b["fail"],
                "success_rate": round(b["ok"] / total, 4),
                "lat_p50": _percentile(lats, 50),
                "lat_p95": _percentile(lats, 95),
                "ttft_p50": _percentile(ttfts, 50),
                "error_categories": b["error_categories"],
                "by_day": by_day,
                "trend_direction": direction,
            }
        )
    providers.sort(key=lambda x: -x["total"])
    window_start: str | None = None
    if since_ts is not None:
        window_start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since_ts))
    return {"providers": providers, "total": len(recs), "window_start": window_start}


def format_trend_human(trend: dict) -> str:
    """对齐表格：供应商/请求/成功%/p50/p95/主失败因/按天简示。纯文本无 ANSI。"""
    if not trend.get("providers"):
        return "（归档无记录；先运行 check 或 inspect 探测以生成归档）"
    w = trend.get("window_start")
    lines = [f"trend: {trend['total']} 条记录" + (f"  窗口≥{w}" if w else "")]
    lines.append(
        f"{_pad('供应商', 24)} {'请求':>6} {'成功%':>7} {'p50':>8} {'p95':>8} "
        f"{'主失败因':18} 按天"
    )
    lines.append("-" * 88)
    for p in trend["providers"]:
        rate = f"{p['success_rate'] * 100:.0f}%"
        dir_mark = {"up": "↑", "down": "↓", "stable": "→"}.get(p.get("trend_direction"), "")
        if dir_mark:
            rate += dir_mark
        p50 = f"{p['lat_p50']:.0f}ms" if p["lat_p50"] is not None else "-"
        p95 = f"{p['lat_p95']:.0f}ms" if p["lat_p95"] is not None else "-"
        cats = p.get("error_categories") or {}
        top = max(cats, key=cats.get) if cats else "-"
        days = " ".join(f"{d['date'][5:]}:{d['ok']}/{d['total']}" for d in p["by_day"])
        prov = _sanitize_for_terminal(p["provider"])[:24]
        lines.append(
            f"{_pad(prov, 24)} {p['total']:6d} {rate:>7} {p50:>8} {p95:>8} "
            f"{top[:18]:18} {days}"
        )
    return "\n".join(lines)


def run_trend(args, say) -> int:
    """trend 子命令入口。"""
    since_raw = getattr(args, "since", "7d")
    if not since_raw:
        since_raw = None
    try:
        since_ts = parse_since(since_raw)
    except ValueError as exc:
        say(str(exc))
        return 2
    path = archive_path(getattr(args, "archive", None))
    trend = build_trend(
        load_records(path),
        since_ts=since_ts,
        provider=getattr(args, "provider", None) or None,
        model=getattr(args, "model", None) or None,
        include_test=getattr(args, "include_test", False),
    )
    report = {
        "schema_version": 1,
        "command": "trend",
        "archive": path,
        "since": since_raw,
        "trend": trend,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
        return 0
    say(format_trend_human(trend))
    return 0

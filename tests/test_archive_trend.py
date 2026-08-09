"""CC-Pulse 归档 + 趋势聚合测试。纯标准库脚本，不引入第三方测试库。

覆盖：
  - parse_since / archive_path / append_record / load_records
  - build_trend 成功率、延迟分位、错误分类、按天、since/provider/model 过滤
  - format_trend_human 表格与空归档
  - run_trend 人类输出与 --json 报告、非法 --since、无记录
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

# 从项目根定位模块（可从任意目录运行）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SCRIPT = os.path.join(_ROOT, "ccpulse_archive.py")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

spec = importlib.util.spec_from_file_location("ccpulse_archive", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PASSED = []
FAILED = []


def test(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}: {detail}")


print("\n[Unit] parse_since / archive_path")
old_time = mod.time.time
mod.time.time = lambda: 1_000_000
try:
    test("parse_since 24h", mod.parse_since("24h") == 913_600)
    test("parse_since 7d", mod.parse_since("7d") == 395_200)
    test("parse_since 30m", mod.parse_since("30m") == 998_200)
    test("parse_since seconds", mod.parse_since("3600") == 996_400)
    test("parse_since normalizes case", mod.parse_since(" 24H ") == 913_600)
    test("parse_since None", mod.parse_since(None) is None)
    test("parse_since empty", mod.parse_since("") is None)
    try:
        mod.parse_since("7x")
        invalid = False
    except ValueError as exc:
        invalid = "无法解析 --since" in str(exc)
    test("parse_since invalid raises 无法解析", invalid)
finally:
    mod.time.time = old_time
test(
    "archive_path default",
    mod.archive_path().endswith(os.path.join(".cc-pulse", "probe_history.jsonl")),
)
# 路径限制：home/cwd 下可写，外部路径拒绝
test(
    "archive_path home 允许",
    mod.archive_path(str(Path.home() / "test_pulse.jsonl")).endswith(
        "test_pulse.jsonl"
    ),
)
try:
    mod.archive_path("D:/x/y.jsonl")
    test("archive_path 外部路径拒绝", False)
except ValueError:
    test("archive_path 外部路径拒绝", True)


print("\n[Unit] append_record / load_records")
tmp = tempfile.mkdtemp(prefix="ccpulse_archive_")
arc = os.path.join(tmp, "sub", "probe_history.jsonl")
now = int(time.time())
t1 = now - 25 * 3600  # 25h 前 → 必然跨天
t2 = now
recs = [
    {
        "ts": t1,
        "provider": "Prov-A",
        "model": "model-x",
        "status": "ok",
        "latency": 100,
        "ttft": 50,
        "error_category": None,
        "command": "check",
    },
    {
        "ts": t1,
        "provider": "Prov-A",
        "model": "model-x",
        "status": "ok",
        "latency": 200,
        "ttft": 80,
        "error_category": None,
        "command": "check",
    },
    {
        "ts": t1,
        "provider": "Prov-A",
        "model": "model-x",
        "status": "fail",
        "latency": None,
        "ttft": None,
        "error_category": "rate_limit",
        "command": "check",
    },
    {
        "ts": t2,
        "provider": "Prov-A",
        "model": "model-x",
        "status": "ok",
        "latency": 300,
        "ttft": None,
        "error_category": None,
        "command": "inspect",
    },
    {
        "ts": t1,
        "provider": "Prov-B",
        "model": "model-x",
        "status": "ok",
        "latency": 50,
        "ttft": 30,
        "error_category": None,
        "command": "check",
    },
    {
        "ts": t2,
        "provider": "Prov-B",
        "model": "model-y",
        "status": "fail",
        "latency": None,
        "ttft": None,
        "error_category": "network",
        "command": "check",
    },
]
for r in recs:
    mod.append_record(arc, r)
loaded = mod.load_records(arc)
test("load_records 回读条数一致", len(loaded) == 6, f"n={len(loaded)}")

# 损坏行跳过
with open(arc, "a", encoding="utf-8") as f:
    f.write("{not json\n")
    f.write("garbage\n")
test(
    "load_records 损坏行跳过",
    len(mod.load_records(arc)) == 6,
    f"n={len(mod.load_records(arc))}",
)
test(
    "load_records 缺失文件 → []",
    mod.load_records(os.path.join(tmp, "nope.jsonl")) == [],
)


print("\n[Unit] build_trend")
trend = mod.build_trend(loaded, include_test=True)
test("trend total=6", trend["total"] == 6, f"total={trend['total']}")
test(
    "trend 2 providers",
    len(trend["providers"]) == 2,
    f"{[p['provider'] for p in trend['providers']]}",
)
test("trend window_start None", trend["window_start"] is None)
pa = next(p for p in trend["providers"] if p["provider"] == "Prov-A")
test("trend Prov-A total=4", pa["total"] == 4)
test("trend Prov-A ok=3", pa["ok"] == 3)
test("trend Prov-A fail=1", pa["fail"] == 1)
test("trend Prov-A success_rate=0.75", pa["success_rate"] == 0.75)
test("trend Prov-A lat_p50=200", pa["lat_p50"] == 200.0, f"{pa['lat_p50']}")
test("trend Prov-A lat_p95=290", pa["lat_p95"] == 290.0, f"{pa['lat_p95']}")
test("trend Prov-A ttft_p50=65", pa["ttft_p50"] == 65.0, f"{pa['ttft_p50']}")
test(
    "trend Prov-A trend_direction 存在",
    pa.get("trend_direction") in ("up", "down", "stable", None),
    f"dir={pa.get('trend_direction')}",
)
test(
    "trend Prov-A error_categories",
    pa["error_categories"] == {"rate_limit": 1},
    f"{pa['error_categories']}",
)
d1 = next(d for d in pa["by_day"] if d["date"] == mod._day_key(t1))
test("trend by_day 跨天", len(pa["by_day"]) == 2, f"{pa['by_day']}")
test("trend by_day sorted", pa["by_day"][0]["date"] < pa["by_day"][-1]["date"])
test("trend day1 total=3", d1["total"] == 3, f"{d1}")
test("trend day1 ok=2", d1["ok"] == 2)
test("trend day1 success_rate", d1["success_rate"] == round(2 / 3, 4))
pb = next(p for p in trend["providers"] if p["provider"] == "Prov-B")
test("trend Prov-B success_rate=0.5", pb["success_rate"] == 0.5)

# 过滤
pt = mod.build_trend(loaded, provider="Prov-A", include_test=True)
test("过滤 provider 收窄", len(pt["providers"]) == 1 and pt["total"] == 4)
mt = mod.build_trend(loaded, model="model-y", include_test=True)
test(
    "过滤 model 收窄",
    mt["total"] == 1 and len(mt["providers"]) == 1 and mt["providers"][0]["ok"] == 0,
    f"{mt}",
)
st = mod.build_trend(loaded, since_ts=t2, include_test=True)
test("过滤 since 收窄", st["total"] == 2, f"total={st['total']}")
test("过滤 since 设 window_start", st["window_start"] is not None)
test(
    "过滤 since Prov-A lat_p50=300",
    next(p for p in st["providers"] if p["provider"] == "Prov-A")["lat_p50"] == 300.0,
)


print("\n[Unit] format_trend_human")
text = mod.format_trend_human(trend)
test("human 表头", "供应商" in text and "成功%" in text and "按天" in text)
test("human 含 Prov-A", "Prov-A" in text)
test("human 按天简示", ":" in text and "/" in text, f"{text.splitlines()[-1]}")
test(
    "human 空归档",
    "归档无记录" in mod.format_trend_human({"providers": [], "total": 0}),
)


print("\n[Unit] run_trend")


def _capture(*args, **kwargs):
    _capture.lines.append(" ".join(str(a) for a in args))


_capture.lines = []
args_h = types.SimpleNamespace(
    since="7d", archive=arc, provider=None, model=None, json=False, include_test=True
)
rc = mod.run_trend(args_h, _capture)
test("run_trend 人类 rc=0", rc == 0)
test(
    "run_trend 人类输出",
    any("trend: 6 条记录" in l and "Prov-A" in l for l in _capture.lines),
    f"{_capture.lines[:1]}",
)

buf = io.StringIO()
_capture.lines = []
args_j = types.SimpleNamespace(
    since="7d", archive=arc, provider=None, model=None, json=True, include_test=True
)
with contextlib.redirect_stdout(buf):
    rc = mod.run_trend(args_j, _capture)
report = json.loads(buf.getvalue()) if buf.getvalue() else {}
test("run_trend json rc=0", rc == 0)
test(
    "run_trend json 结构",
    report.get("schema_version") == 1
    and report.get("command") == "trend"
    and report.get("archive") == arc,
    f"report={report}",
)
test("run_trend json trend total", report["trend"]["total"] == 6)

_capture.lines = []
args_bad = types.SimpleNamespace(
    since="7x", archive=arc, provider=None, model=None, json=False, include_test=True
)
rc = mod.run_trend(args_bad, _capture)
test("run_trend 非法 --since rc=2", rc == 2)
test(
    "run_trend 非法 --since 提示",
    any("无法解析 --since" in l for l in _capture.lines),
    f"{_capture.lines}",
)

empty_arc = os.path.join(tmp, "empty.jsonl")
_capture.lines = []
args_e = types.SimpleNamespace(
    since="7d",
    archive=empty_arc,
    provider=None,
    model=None,
    json=False,
    include_test=True,
)
rc = mod.run_trend(args_e, _capture)
test("run_trend 无记录 rc=0", rc == 0)
test(
    "run_trend 无记录提示",
    any("归档无记录" in l for l in _capture.lines),
    f"{_capture.lines}",
)

shutil.rmtree(tmp, ignore_errors=True)

# ============ 汇总 ============

print("\n" + "=" * 60)
print(f"  PASS: {len(PASSED)}")
print(f"  FAIL: {len(FAILED)}")
print("=" * 60)
if FAILED:
    print("\n失败用例:")
    for n, d in FAILED:
        print(f"  - {n}: {d}")
    sys.exit(1)
print("\n✓ 所有测试通过")

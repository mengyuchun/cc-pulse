"""文档与代码一致性守卫。

防止 README 漂移：检查维度、关键参数和退出码是否仍与 CLI 契约一致。
CI / pre-commit / `just lint-docs` 均可调用。
退出码 0 = 通过，非 0 = 发现漂移。
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FORBIDDEN = [
    (re.compile(r"4\s*维度"), "维度数已变，请改用 '7 维度'"),
    (re.compile(r"6\s*维度"), "维度数已变，请改用 '7 维度'"),
    (re.compile(r"\b4[\- ]?dim"), "维度数已变，请改用 '7-dim' / '7 维度'"),
    (re.compile(r"\b6[\- ]?dim"), "维度数已变，请改用 '7-dim' / '7 维度'"),
    (re.compile(r"\bfour[\- ]?dim"), "维度数已变，请改用 '7-dim'"),
    (re.compile(r"\bsix[\- ]?dim"), "维度数已变，请改用 '7-dim'"),
]

DOCS = ["README.md", "README.en.md"]
REQUIRED_MARKERS = {
    "--quiet": "NDJSON",
    "--compare": "--provider",
    "--probe-context": "512k",
    "--probe-max-tokens": "--probe-enable-thinking",
    "--user-agent": "--stainless-version",
    "--since": "analyze",
    "--select": "--provider",
    "probe_history.jsonl": "cc-switch",
    "deep-dive": "--target",
    "--alert-threshold": "cron",
    "trim_archive": "5MB",
    "trend_direction": "↑",
    # b49b83c：--auth-full 快捷开关必须在中英文 README 留档
    "--auth-full": "cache-replay",
}

MENU_MARKERS = {
    "README.md": ("快速体检", "高级设置", "运行日志", "五项深探"),
    "README.en.md": ("Quick health check", "Advanced settings", "Runtime logs", "five-check"),
}


TEST_COUNT_ROW = re.compile(
    r"^\|\s*`tests/([a-z0-9_]+\.py)`\s*\|.*\|\s*(\d+)\s*\|\s*$", re.M
)
TOTAL_ZH = re.compile(r"当前 \*\*(\d+) 个 Python 测试 \+ (\d+) 个 PS1 测试\*\*")
TOTAL_EN = re.compile(r"Currently \*\*(\d+) Python tests \+ (\d+) PS1 tests\*\*")
BADGE = re.compile(r"tests-(\d+)%20pass")


def _readme_counts(text: str) -> tuple[dict, int, int, int] | None:
    """解析 README 中的徽章/合计/明细；缺失返回 None。"""
    badge = BADGE.search(text)
    total = TOTAL_ZH.search(text) or TOTAL_EN.search(text)
    if not badge or not total:
        return None
    rows = {f: int(n) for f, n in TEST_COUNT_ROW.findall(text)}
    return rows, int(badge.group(1)), int(total.group(1)), int(total.group(2))


def _check_counts(name: str, text: str) -> list[str]:
    """数字自洽：徽章 == 明细求和 == 合计（防改一处漏一处）。"""
    parsed = _readme_counts(text)
    if parsed is None:
        return [f"{name}: 缺少测试徽章或合计句"]
    rows, badge, py_n, ps1_n = parsed
    failures = []
    ps1_row = rows.get("test_ps1_launcher.py")
    if ps1_row is None:
        failures.append(f"{name}: 测试表缺少 test_ps1_launcher.py 行")
    elif ps1_row != ps1_n:
        failures.append(f"{name}: PS1 明细 {ps1_row} != 合计 {ps1_n}")
    py_sum = sum(v for k, v in rows.items() if k != "test_ps1_launcher.py")
    if py_sum != py_n:
        failures.append(f"{name}: Python 明细求和 {py_sum} != 合计 {py_n}")
    if badge != py_n + ps1_n:
        failures.append(f"{name}: 徽章 {badge} != 合计 {py_n + ps1_n}")
    return failures


def _actual_counts() -> dict[str, int]:
    """实际运行测试文件解析用例数（慢，仅 --counts / 环境变量时启用）。"""
    actual = {}
    for fname in sorted(os.listdir(os.path.join(ROOT, "tests"))):
        if not fname.endswith(".py") or fname == "test_docs_consistency.py":
            continue
        proc = subprocess.run(
            [sys.executable, os.path.join("tests", fname)],
            capture_output=True, text=True, cwd=ROOT, check=False,
        )
        out = proc.stdout + proc.stderr
        n = 0
        for pat in (r"PASS: (\d+)", r"PS1 PASS: (\d+)",
                    r"Ran (\d+) tests", r"总计: (\d+) PASS"):
            hit = re.findall(pat, out)
            if hit:
                n = int(hit[-1])
                break
        actual[fname] = n
    return actual


def _check_actual(name: str, text: str) -> list[str]:
    """明细行与实跑用例数比对。"""
    parsed = _readme_counts(text)
    if parsed is None:
        return []
    rows = parsed[0]
    actual = _actual_counts()
    failures = []
    for fname, n in actual.items():
        if fname not in rows:
            failures.append(f"{name}: 测试表缺少 {fname} 行")
        elif rows[fname] != n:
            failures.append(f"{name}: {fname} 文档 {rows[fname]} != 实跑 {n}")
    for fname in rows:
        if fname not in actual:
            failures.append(f"{name}: 测试表列了不存在的 {fname}")
    return failures


def check_doc(name: str) -> list[str]:
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        text = f.read()
    failures = []
    for pat, msg in FORBIDDEN:
        if pat.search(text):
            failures.append(f"{name}: {msg}")
    for marker, companion in REQUIRED_MARKERS.items():
        if marker not in text or companion not in text:
            failures.append(f"{name}: 缺少 {marker} 或配套文案 {companion}")
    for marker in MENU_MARKERS[name]:
        if marker not in text:
            failures.append(f"{name}: 缺少菜单文案 {marker}")
    failures.extend(_check_counts(name, text))
    if "--counts" in sys.argv or os.environ.get("CC_PULSE_VERIFY_COUNTS"):
        failures.extend(_check_actual(name, text))
    if "退出码" in text:
        exit_section = text[text.find("退出码") :]
    elif "Exit codes" in text:
        exit_section = text[text.find("Exit codes") :]
    else:
        return [*failures, f"{name}: 缺少退出码表标题"]
    for code in ("0", "1", "2", "3", "4"):
        if not re.search(rf"\|\s*{code}\s*\|", exit_section):
            failures.append(f"{name}: 退出码表缺少 {code}")
    return failures


def main() -> int:
    failures = [failure for name in DOCS for failure in check_doc(name)]
    if failures:
        for failure in failures:
            print(failure)
        print(f"\n✗ 文档漂移：{len(failures)} 处需修正")
        return 1
    print("✓ 文档与代码一致 (7 维度、关键参数、退出码)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

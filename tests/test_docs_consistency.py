"""文档与代码一致性守卫。

防止 README 漂移：检查维度、关键参数和退出码是否仍与 CLI 契约一致。
CI / pre-commit / `just lint-docs` 均可调用。
退出码 0 = 通过，非 0 = 发现漂移。
"""

import os
import re
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
}

MENU_MARKERS = {
    "README.md": ("快速体检", "高级设置", "运行日志", "五项深探"),
    "README.en.md": ("Quick health check", "Advanced settings", "Runtime logs", "five-check"),
}


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

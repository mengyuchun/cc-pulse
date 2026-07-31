"""文档与代码一致性守卫。

防止 README 漂移：禁止维度数等关键数字与代码不一致。
CI / pre-commit / `just lint-docs` 均可调用。
退出码 0 = 通过，非 0 = 发现漂移。
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 过期说法（README 已被告知升级为 7 维度）
FORBIDDEN = [
    (re.compile(r"4\s*维度"), "维度数已变，请改用 '7 维度'"),
    (re.compile(r"6\s*维度"), "维度数已变，请改用 '7 维度'"),
    (re.compile(r"\b4[\- ]?dim"), "维度数已变，请改用 '7-dim' / '7 维度'"),
    (re.compile(r"\b6[\- ]?dim"), "维度数已变，请改用 '7-dim' / '7 维度'"),
    (re.compile(r"\bfour[\- ]?dim"), "维度数已变，请改用 '7-dim'"),
    (re.compile(r"\bsix[\- ]?dim"), "维度数已变，请改用 '7-dim'"),
]

DOCS = ["README.md", "README.en.md"]


def main() -> int:
    fails = 0
    for name in DOCS:
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                for pat, msg in FORBIDDEN:
                    if pat.search(line):
                        print(f"{name}:{lineno} {msg}  ← {line.rstrip()}")
                        fails += 1
    if fails:
        print(f"\n✗ 文档漂移：{fails} 处需修正")
        return 1
    print("✓ 文档与代码一致 (7 维度)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

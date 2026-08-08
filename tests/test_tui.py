"""CC-Pulse TUI 选择器单元测试。"""
from __future__ import annotations

import importlib
import os
import sys

# 项目根目录加入 sys.path
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _reload_tui():
    """强制重新加载模块（确保从源码加载）。"""
    if "ccpulse_tui" in sys.modules:
        del sys.modules["ccpulse_tui"]
    return importlib.import_module("ccpulse_tui")


mod = _reload_tui()

PASS = 0
FAIL = 0


def test(desc: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc} {detail}")


# ============ _is_tty ============

print("\n[Unit] _is_tty 非交互环境")

test(
    "管道环境 _is_tty() 返回 False",
    not mod._is_tty(),
    f"result={mod._is_tty()}",
)


# ============ select 非 TTY 降级 ============

print("\n[Unit] select 非 TTY 降级")

result = mod.select(["a", "b", "c"], title="test")
test(
    "非 TTY 环境 select 返回 None",
    result is None,
    f"result={result!r}",
)


# ============ select_providers 非 TTY 降级 ============

print("\n[Unit] select_providers 非 TTY 降级")


class FakeProvider:
    def __init__(self, name, **kw):
        self.name = name
        self.is_current = kw.get("is_current", False)
        self.in_failover = kw.get("in_failover", False)
        self.notes = kw.get("notes", "")
        self.app_type = kw.get("app_type", "claude")


fp1 = FakeProvider("provider_a", is_current=True, notes="限制并发")
fp2 = FakeProvider("provider_b", in_failover=True)
fp3 = FakeProvider("provider_c", app_type="openclaw")

result = mod.select_providers([fp1, fp2, fp3])
test(
    "非 TTY 环境 select_providers 返回 None",
    result is None,
    f"result={result!r}",
)


# ============ select 空列表 ============

print("\n[Unit] select 空列表")

result = mod.select([], title="test")
test(
    "空列表返回空 list",
    result == [],
    f"result={result!r}",
)

result = mod.select_providers([])
test(
    "select_providers 空列表返回空 list",
    result == [],
    f"result={result!r}",
)


# ============ ANSI 常量 ============

print("\n[Unit] ANSI 常量")

test("HIDE_CURSOR 以 ESC[ 开头", mod._HIDE_CURSOR.startswith("\x1b["))
test("SHOW_CURSOR 以 ESC[ 开头", mod._SHOW_CURSOR.startswith("\x1b["))
test("CLEAR_LINE 以 ESC[ 开头", mod._CLEAR_LINE.startswith("\x1b["))
test("MOVE_UP 以 ESC[ 开头", mod._MOVE_UP.startswith("\x1b["))


# ============ 汇总 ============

print(f"\n总计: {PASS} PASS, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)

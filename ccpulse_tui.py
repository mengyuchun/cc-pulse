"""交互式 TUI 选择器（纯标准库，跨平台）。

Windows 用 msvcrt，Unix 用 termios+tty。非 TTY 环境自动降级。
"""
from __future__ import annotations

import sys

# ANSI 转义
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_CLEAR_LINE = "\x1b[2K"
_MOVE_UP = "\x1b[1A"


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _getch_unix() -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        # ESC 开头读转义序列
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                return f"\x1b[{ch3}"
            return ch + ch2
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _getch_windows() -> str:
    import msvcrt

    ch = msvcrt.getwch()
    # 特殊键前缀
    if ch in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        mapping = {"H": "up", "P": "down", "K": "left", "M": "right"}
        return mapping.get(code, f"special:{code}")
    return ch


def _getch() -> str:
    if sys.platform == "win32":
        return _getch_windows()
    return _getch_unix()


def _print_lines(lines: list[str], *, redraw_from: int = 0) -> None:
    """渲染 lines；redraw_from > 0 时先上移并清行。"""
    out = sys.stdout
    if redraw_from:
        for _ in range(redraw_from):
            out.write(_MOVE_UP + _CLEAR_LINE)
    for line in lines:
        out.write(line + "\n")
    out.flush()


def select(
    options: list[str],
    *,
    title: str = "选择",
    multi: bool = True,
    checked: list[bool] | None = None,
    page_size: int = 15,
) -> list[int] | None:
    """交互式选择。

    返回选中项的索引列表；用户取消返回 None。
    非 TTY 环境返回 None（调用方降级到 --provider）。
    """
    if not options:
        return []
    if not _is_tty():
        return None

    n = len(options)
    if checked is None:
        checked = [False] * n
    cursor = 0
    scroll = 0  # 窗口起始行

    def _visible_range() -> tuple[int, int]:
        nonlocal scroll
        if cursor < scroll:
            scroll = cursor
        elif cursor >= scroll + page_size:
            scroll = cursor - page_size + 1
        end = min(n, scroll + page_size)
        return scroll, end

    hint = (
        "↑↓ 移动  空格选择  a 全选  回车确认  ESC 取消"
        if multi
        else "↑↓ 移动  回车确认  ESC 取消"
    )

    prev_lines = 0
    sys.stdout.write(_HIDE_CURSOR)
    try:
        while True:
            start, end = _visible_range()
            lines = [f"? {title}  ({hint})"]
            for i in range(start, end):
                mark = "✅" if checked[i] else "⬚"
                arrow = "▸" if i == cursor else " "
                extra = ""
                lines.append(f"  {arrow} {mark} {options[i]}{extra}")
            lines.append(f"  [{sum(checked)}/{n}]" if multi else f"  [{cursor + 1}/{n}]")
            _print_lines(lines, redraw_from=prev_lines)
            prev_lines = len(lines)

            key = _getch()
            if key in ("up", "\x1bA"):
                cursor = (cursor - 1) % n
            elif key in ("down", "\x1bB"):
                cursor = (cursor + 1) % n
            elif key == " " and multi:
                checked[cursor] = not checked[cursor]
            elif key == "a" and multi:
                all_on = all(checked)
                checked = [not all_on] * n
            elif key in ("\r", "\n"):
                if multi:
                    if any(checked):
                        return [i for i, c in enumerate(checked) if c]
                    # 全没选时确认当前项
                    return [cursor]
                return [cursor]
            elif key == "\x1b":
                return None
    finally:
        sys.stdout.write(_SHOW_CURSOR)
        sys.stdout.flush()


def select_providers(
    providers: list,
    *,
    title: str = "选择要检测的供应商",
    show_status: bool = True,
) -> list[int] | None:
    """供应商专用选择器，展示名称 + 状态标记 + notes。"""
    if not providers:
        return []
    if not _is_tty():
        return None

    options = []
    for p in providers:
        tags = []
        if show_status:
            if getattr(p, "is_current", False):
                tags.append("当前激活")
            elif getattr(p, "in_failover", False):
                tags.append("失败队列")
            app = getattr(p, "app_type", "")
            if app:
                tags.append(app)
        notes = (getattr(p, "notes", "") or "").strip()
        if notes:
            notes_short = notes[:24] + "…" if len(notes) > 24 else notes
            tags.append(f"📝{notes_short}")
        name = p.name[:32]
        suffix = f"  [{' | '.join(tags)}]" if tags else ""
        options.append(f"{name}{suffix}")

    return select(options, title=title, multi=True)

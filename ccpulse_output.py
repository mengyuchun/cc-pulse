"""线程安全的人类可读输出与终端样式。"""

import contextvars
import os
import re
import sys
import threading
from typing import Any

_output_stream = contextvars.ContextVar("output_stream", default=None)
_say_lock = threading.Lock()
_CONTROL_RE = re.compile(
    "\x1b\\[[0-9;]*[A-Za-z]|\x1b\\][^\x07]*\x07|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]"
)
_ANSI = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "gray": "\x1b[90m",
}


def _sanitize_for_terminal(text: str) -> str:
    """剥离 ANSI 转义和 C0 控制字符，防止恶意响应注入终端指令。"""
    return _CONTROL_RE.sub("", text)


def say(*args: Any, **kwargs: Any) -> None:
    """线程安全输出人类可读进度，默认 flush 并清理控制字符。"""
    kwargs.setdefault("flush", True)
    cleaned = [_sanitize_for_terminal(str(value)) for value in args]
    stream = _output_stream.get() or sys.stdout
    with _say_lock:
        print(*cleaned, file=stream, **kwargs)


def _use_color() -> bool:
    """TTY 且未被环境变量禁用时启用颜色。"""
    if os.environ.get("NO_COLOR") or os.environ.get("CCPULSE_NO_COLOR"):
        return False
    try:
        return bool((_output_stream.get() or sys.stdout).isatty())
    except (AttributeError, OSError):
        return False


def _c(text: str, *styles: str) -> str:
    """给内部生成文本添加 ANSI 样式。"""
    if not _use_color():
        return text
    prefix = "".join(_ANSI[style] for style in styles if style in _ANSI)
    return f"{prefix}{text}{_ANSI['reset']}"


def _say_colored(text: str, **kwargs: Any) -> None:
    """输出已清洗文本并自行上 ANSI 色；不清洗输入，调用方须先传已清洗文本。"""
    kwargs.setdefault("flush", True)
    stream = _output_stream.get() or sys.stdout
    with _say_lock:
        print(text, file=stream, **kwargs)

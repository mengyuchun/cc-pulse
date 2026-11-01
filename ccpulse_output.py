"""线程安全的人类可读输出与终端样式。"""

import contextvars
import os
import re
import sys
import threading
import unicodedata
import urllib.parse
from typing import Any

_output_stream = contextvars.ContextVar("output_stream", default=None)
_say_lock = threading.Lock()
_CONTROL_RE = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|"
    r"(?:\x1b\]|\x9d)[^\x07\x1b]*(?:\x07|\x1b\\|\x9c)|"
    r"(?:\x1bP|\x1bX|\x1b\^|\x1b_|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c)|"
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]"
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
    """剥离终端控制与 Unicode 格式字符，保留换行和普通文本。"""
    cleaned = _CONTROL_RE.sub("", text).replace("\r", "")
    return "".join(ch for ch in cleaned if unicodedata.category(ch) != "Cf")


_SENSITIVE_URL_KEY_RE = re.compile(
    r"^(?:api[-_]?key|access[-_]?token|token|key|secret|"
    r"password|passwd|authorization|auth)$",
    re.IGNORECASE,
)
_CREDENTIAL_URL_SEGMENT_RE = _SENSITIVE_URL_KEY_RE
_CREDENTIAL_VALUE_RE = re.compile(
    r"^(?:sk-|rk-|xai-|gh[pousr]_ |github_pat_|AIza|xox[baprs]-).{8,}$".replace(
        "gh[pousr]_ ", "gh[pousr]_"
    ),
    re.IGNORECASE,
)


def _mask_url_secret(text: str, secret: str | None) -> str:
    """在 URL 展示组件中掩码明文、完整和混合百分号编码。"""
    if not secret:
        return text
    masked = text.replace(secret, "***")
    variants = {urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}
    for encoded in variants:
        if encoded and encoded != secret:
            masked = re.sub(re.escape(encoded), "***", masked, flags=re.IGNORECASE)

    # 每个字符允许明文或对应 %HH，覆盖混合编码而不解码普通文本。
    parts = []
    for byte in secret.encode("utf-8"):
        literal = chr(byte) if 32 <= byte < 127 else None
        hex_part = f"%{byte:02x}"
        alternatives = [re.escape(hex_part)]
        if literal:
            alternatives.insert(0, re.escape(literal))
        parts.append("(?:" + "|".join(alternatives) + ")")
    if parts:
        masked = re.sub("".join(parts), "***", masked, flags=re.IGNORECASE)
    return masked


def _sanitize_url_query(query: str, api_key: str | None) -> str:
    parts = []
    for part in query.split("&"):
        if not part:
            parts.append(part)
            continue
        key, separator, value = part.partition("=")
        if _SENSITIVE_URL_KEY_RE.fullmatch(urllib.parse.unquote_plus(key).strip()):
            parts.append(key + (separator or "=") + "***")
        else:
            parts.append(key + separator + _mask_url_secret(value, api_key))
    return "&".join(parts)


def _sanitize_url_fragment(fragment: str, api_key: str | None) -> str:
    parts = re.split(r"([&;])", _mask_url_secret(fragment, api_key))
    for i in range(0, len(parts), 2):
        parts[i] = _sanitize_url_query(parts[i], api_key)
    return "".join(parts)


def _sanitize_url_for_display(url: str, api_key: str | None = None) -> str:
    """脱敏展示 URL 中的凭据，不改变实际请求 URL。"""
    try:
        parsed = urllib.parse.urlsplit(url)
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = "***@" + netloc.rsplit("@", 1)[1]

        path = _mask_url_secret(parsed.path, api_key)
        path_parts = path.split("/")
        for i, part in enumerate(path_parts):
            previous = urllib.parse.unquote(part)
            if i and _CREDENTIAL_URL_SEGMENT_RE.fullmatch(
                urllib.parse.unquote(path_parts[i - 1])
            ):
                path_parts[i] = "***"
            elif _CREDENTIAL_VALUE_RE.fullmatch(previous):
                path_parts[i] = "***"
            else:
                path_parts[i] = _mask_url_secret(part, api_key)

        return parsed._replace(
            netloc=netloc,
            path="/".join(path_parts),
            query=_sanitize_url_query(parsed.query, api_key),
            fragment=_sanitize_url_fragment(parsed.fragment, api_key),
        ).geturl()
    except (TypeError, ValueError):
        return "<redacted-url>"


def _disp_width(text: str) -> int:
    """字符串在终端的显示宽度：CJK 全角（W/F）算 2 格，其余算 1 格。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text
    )


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格到 width 格，修正 CJK 全角导致的表格错位。"""
    return text + " " * max(0, width - _disp_width(text))


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
    """输出清洗后的文本，同时保留内部 _c() 生成的 ANSI 样式。"""
    kwargs.setdefault("flush", True)
    ansi_tokens = set(_ANSI.values())
    parts = re.split(r"(\x1b\[[0-9;]*m)", str(text))
    cleaned = "".join(
        part if part in ansi_tokens else _sanitize_for_terminal(part) for part in parts
    )
    stream = _output_stream.get() or sys.stdout
    with _say_lock:
        print(cleaned, file=stream, **kwargs)

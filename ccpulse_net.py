"""CC-Pulse 网络层：HTTP/SSE/错误分类/脱敏，无业务逻辑。

从 check_ccswitch_health.py 拆出的纯网络层，依赖 stdlib + ccpulse_output。
"""

import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum

from ccpulse_output import _sanitize_for_terminal

class ErrorCategory(str, Enum):
    """统一的错误分类枚举。用于 JSON 报告和 inspect 子命令。"""

    NONE = "none"
    NETWORK = "network"  # DNS / 连接拒绝 / 超时
    TLS = "tls"  # 证书 / 主机名
    AUTH = "authentication"  # 401 / 403
    RATE_LIMIT = "rate_limit"  # 429
    MODEL_NOT_FOUND = "model_not_found"  # 404 / invalid model
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"  # 400 schema
    SERVER = "server_error"  # 5xx
    INVALID_RESPONSE = "invalid_response"  # 200 但无法解析
    ANSWER_MISMATCH = "answer_mismatch"  # 200 但答案不对
    # 流式相关
    STREAM_PROTOCOL = "stream_protocol"  # 非 SSE / 格式异常
    TTFT_TIMEOUT = "ttft_timeout"  # 首 token 超时
    STREAM_INCOMPLETE = "stream_incomplete"  # 流中途断开
    UNKNOWN = "unknown"


def _category_from_status(http_status: int):
    """仅凭 HTTP status code 判断分类；不具区分性时返回 None。"""
    if http_status in (401, 403):
        return ErrorCategory.AUTH
    if http_status == 429:
        return ErrorCategory.RATE_LIMIT
    if http_status == 404:
        return ErrorCategory.MODEL_NOT_FOUND
    if http_status == 400:
        return ErrorCategory.PROTOCOL_INCOMPATIBLE
    if http_status >= 500:
        return ErrorCategory.SERVER
    return None


def classify_error(resp_body: str, http_status: int = 0) -> tuple:
    """根据 HTTP status code（优先）与响应体内容推断错误分类。

    返回 (category, display_text)：
      - category：ErrorCategory 枚举值
      - display_text：与原 parse_error 行为一致的可显示文本

    http_status：真实 HTTP 状态码；>0 时优先按状态码判断，避免响应体里
    出现 "400"/"unauthorized" 等业务文案导致的关键词误分类。
    """
    if not resp_body:
        # 有明确 status 时用它，否则算无法解析的空响应
        status_cat = _category_from_status(http_status)
        if status_cat is not None:
            return status_cat, f"(空响应, HTTP {http_status})"
        return ErrorCategory.INVALID_RESPONSE, "(空响应)"

    try:
        j = json.loads(resp_body)
        e = j.get("error", j)
        # 嵌套常见字段
        msg = (
            e.get("message", "")
            or e.get("type", "")
            or json.dumps(e, ensure_ascii=False)
        )
    except (json.JSONDecodeError, AttributeError):
        if len(resp_body) > 500:
            msg = resp_body[:500] + f" …(非JSON响应，共{len(resp_body)}字符，已截断)"
        else:
            msg = resp_body
        # 非 JSON：优先按状态码，否则视为无法解析
        status_cat = _category_from_status(http_status)
        if status_cat is not None:
            return status_cat, msg
        return ErrorCategory.INVALID_RESPONSE, msg

    # 有明确 status code 时优先用它（body 关键词仅作补充）
    status_cat = _category_from_status(http_status)
    if status_cat is not None:
        if status_cat == ErrorCategory.RATE_LIMIT:
            retry = _parse_retry_after(msg)
            if retry:
                msg = f"{msg} [{retry}]" if msg else retry
        return status_cat, msg

    # 无 status（如流式解析后或 status=200 异常体）：回退到关键词推断
    low = msg.lower() if isinstance(msg, str) else ""
    if any(
        k in low for k in ("rate limit", "rate_limit", "too many requests", "quota")
    ):
        retry = _parse_retry_after(msg)
        if retry:
            msg = f"{msg} [{retry}]" if msg else retry
        return ErrorCategory.RATE_LIMIT, msg
    if any(
        k in low
        for k in (
            "not found",
            "model_not_found",
            "model does not exist",
            "unknown model",
        )
    ):
        return ErrorCategory.MODEL_NOT_FOUND, msg
    if any(
        k in low
        for k in (
            "unauthorized",
            "invalid api key",
            "authentication",
            "permission",
            "forbidden",
        )
    ):
        return ErrorCategory.AUTH, msg
    if any(k in low for k in ("invalid request", "bad request", "schema")):
        return ErrorCategory.PROTOCOL_INCOMPATIBLE, msg
    if any(k in low for k in ("internal", "server error", "overloaded")):
        return ErrorCategory.SERVER, msg
    return ErrorCategory.UNKNOWN, msg


def _sanitize_raw_body(body: str, api_key: str) -> str:
    """raw_body 脱敏：替换 API key 为掩码，防止写入报告时泄露。

    同时尝试 URL 编码后的 key（中转站错误体可能回显编码形态）。
    """
    if not api_key:
        return body
    show = max(1, min(6, len(api_key) // 2))
    mask = api_key[:show] + "***"
    if api_key in body:
        body = body.replace(api_key, mask)
    # 中转站错误体可能回显 URL 编码的 key
    encoded = urllib.parse.quote(api_key, safe="")
    if encoded != api_key and encoded in body:
        body = body.replace(encoded, mask)
    return body


def _sanitize_display(text: str, api_key: str | None) -> str:
    """错误消息/evidence 脱敏：先掩码 API key，再剥 ANSI 控制字符。"""
    if not text:
        return text
    return _sanitize_for_terminal(_sanitize_raw_body(text, api_key or ""))


# 限流恢复时间提取：中转站常在 429 响应体里带"重置/恢复/retry-after/XX:XX:XX"等提示
_RATE_RESET_PATTERNS = [
    re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"),  # 2026-08-07 23:10:15
    re.compile(r"(\d{2}:\d{2}:\d{2})"),  # 23:10:15
    re.compile(r"retry[\s-]*after[:\s]+(\d+)", re.IGNORECASE),  # retry-after: 60
    re.compile(r"(?:重置|恢复|reset)[^0-9]*?(\d{2}:\d{2}:\d{2})", re.IGNORECASE),
]


def _parse_retry_after(body: str) -> str | None:
    """从 429 响应体提取限流恢复时间提示，返回可展示文本或 None。"""
    if not body:
        return None
    for pat in _RATE_RESET_PATTERNS:
        m = pat.search(body)
        if m:
            val = m.group(1)
            # 纯秒数 -> 转成"约 N 分钟后"
            if val.isdigit() and len(val) <= 6:
                secs = int(val)
                if secs >= 60:
                    return f"约 {secs // 60} 分钟后恢复"
                return f"约 {secs} 秒后恢复"
            return f"预计恢复: {val}"
    return None


def create_ssl_context(skip_tls_verify: bool) -> ssl.SSLContext:
    """默认验证 TLS 证书；仅在显式请求时跳过验证。"""
    if skip_tls_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


@dataclass
class HttpResponse:
    """统一 HTTP 响应（含错误归一化）。

    error_category 非 None 表示连接层失败（网络/TLS/超时），此时 status=0、body 空；
    error_category 为 None 表示拿到了 HTTP 响应（含 4xx/5xx），由调用方按 status/body 分类。
    """

    status: int
    body: str
    content_type: str
    error_category: str | None
    error_msg: str
    truncated: bool = False


def _read_httperror_body(e: urllib.error.HTTPError) -> tuple[str, bytes]:
    """安全读取 HTTPError 的响应体，返回 (decoded_body, raw_bytes)。

    供 _http_request 与 probe_stream 复用，避免重复 try/except e.read()。
    连接中途断开（IncompleteRead / PartialRead）时取已读部分，不抛异常。
    """
    try:
        raw = e.read()
    except (OSError, ValueError):
        raw = b""
    except Exception as exc:
        # http.client.IncompleteRead 等不在 OSError 分支里，但能从 .partial 取已读数据
        partial = getattr(exc, "partial", None)
        if isinstance(partial, bytes) and partial:
            raw = partial
        else:
            raw = b""
    return raw.decode("utf-8", errors="replace"), raw


def _http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: int = 30,
    skip_tls_verify: bool = False,
    max_retries: int = 0,
) -> HttpResponse:
    """统一非流式 HTTP 请求，归一化 HTTPError/URLError/TLS/超时。

    消除 probe_tier / fetch_models / probe_model_metadata 里重复的 urlopen 样板。
    流式探测（probe_stream）因需要逐块读取 resp，不适用本函数。
    max_retries: 网络层错误重试次数（仅对 URLError/TimeoutError/OSError 生效）。
    HTTPError 不重试（已有 status code）。URLError 涵盖 DNS/连接拒绝/TLS 握手失败等，
    对真正的证书问题重试无效但只会多耗几秒，换取对瞬时握手故障的容忍。
    """
    ctx = create_ssl_context(skip_tls_verify)
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                try:
                    raw = resp.read(2 << 20)  # 2MB 限长，防止异常响应耗尽内存
                except http.client.IncompleteRead as e:
                    raw = e.partial or b""
                    truncated = True
                else:
                    truncated = False
                    # read(amt) 不抛 IncompleteRead：声明长度 > 实际读取且未达上限 → 截断
                    declared = resp.headers.get("Content-Length")
                    if declared:
                        try:
                            declared_n = int(declared)
                            if declared_n > len(raw) and len(raw) < (2 << 20):
                                truncated = True
                        except ValueError:
                            pass
                return HttpResponse(
                    status=resp.status,
                    body=raw.decode("utf-8", errors="replace"),
                    content_type=resp.headers.get("Content-Type", ""),
                    error_category=(
                        ErrorCategory.STREAM_INCOMPLETE.value if truncated else None
                    ),
                    error_msg="响应不完整" if truncated else "",
                    truncated=truncated,
                )
        except urllib.error.HTTPError as e:
            # HTTPError 有 status code，不算连接层错误；不重试
            body_resp, _raw = _read_httperror_body(e)
            return HttpResponse(
                status=e.code,
                body=body_resp,
                content_type=e.headers.get("Content-Type", "") if e.headers else "",
                error_category=None,
                error_msg="",
            )
        except urllib.error.URLError as e:
            if attempt < max_retries:
                time.sleep(1 + attempt)
                continue
            return HttpResponse(
                0, "", "", _error_category_for_urlerror(e), f"连接失败: {e.reason}"
            )
        except (OSError, ssl.SSLError, ValueError) as e:
            if attempt < max_retries:
                time.sleep(1 + attempt)
                continue
            cat = (
                _error_category_for_urlerror(e)
                if _is_tls_error(e)
                else ErrorCategory.UNKNOWN.value
            )
            return HttpResponse(0, "", "", cat, f"异常: {type(e).__name__}: {e}")
    # 不应走到这里，但防御性兜底
    return HttpResponse(0, "", "", ErrorCategory.UNKNOWN.value, "重试耗尽")


def _is_tls_error(exc: BaseException) -> bool:
    """判断异常是否为 TLS/证书相关错误。"""
    candidates: list[BaseException] = [exc]
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        candidates.append(reason)
    for c in candidates:
        if isinstance(c, ssl.SSLError):
            return True
        name = type(c).__name__
        if "SSL" in name or "Certificate" in name or "TLS" in name:
            return True
        text = str(c).upper()
        if any(
            k in text
            for k in (
                "CERTIFICATE",
                "SSL:",
                "TLSV1",
                "CERTIFICATE_VERIFY_FAILED",
                "HOSTNAME MISMATCH",
                "CERTIFICATE VERIFY FAILED",
            )
        ):
            return True
    return False


def _error_category_for_urlerror(exc: BaseException) -> str:
    """URLError / 连接类异常 → error_category 字符串。"""
    if _is_tls_error(exc):
        return ErrorCategory.TLS.value
    return ErrorCategory.NETWORK.value


# ---------- 流式探测 ----------

# 各协议流式事件约定的"终止"信号：
#   - Anthropic Messages：event: message_stop
#   - OpenAI Chat Completions：data: [DONE]
#   - OpenAI Responses：event: response.completed
STREAM_DONE_MARKERS = {
    "anthropic_messages": ("event", "message_stop"),
    "openai_chat_completions": ("data", "[DONE]"),
    "openai_responses": ("event", "response.completed"),
    "openai_chat_openrouter": ("data", "[DONE]"),
}


class StreamEvent(dict):
    """统一流式事件结构：
    - kind: message_start | text_delta | content_block | message_stop
            | done | error | first_chunk
    - model: 该事件携带的响应模型（若有）
    - text_delta: 仅 text_delta 有效
    - raw: 原始事件文本/字典
    """


def parse_sse_lines(raw_iter, on_event, protocol: str):
    """通用 SSE 解析器（薄封装）。

    实际解析统一走 `_process_sse_event`，本函数负责：
      - 累积 buffer；
      - 按 \\r\\n\\r\\n / \\n\\n / 双 \\r 切分事件；
      - 发 first_chunk；
      - 追加 text_buf；
      - 未收到任何事件时发 error。

    存在的目的：给单元测试提供一个「一次性喂完整字节」的入口，行为
    与线上 `probe_stream` 主循环里对 `_process_sse_event` 的调用完全一致。
    """
    buffer = b""
    done_marker_field, done_marker_value = STREAM_DONE_MARKERS.get(
        protocol, ("event", "message_stop")
    )
    got_done = False
    first_event_seen = False
    text_buf: list[str] = []

    def _inner_on_event(ev: StreamEvent) -> None:
        nonlocal first_event_seen, got_done
        kind = ev.get("kind")
        if kind == "text_delta" and ev.get("text_delta"):
            text_buf.append(ev["text_delta"])
        if kind == "done":
            got_done = True
        # first_chunk 在拆出首个事件时由外层显式补发，这里不重复
        on_event(ev)

    def _try_take_event(buf: bytes) -> tuple[bytes | None, bytes]:
        """尝试从 buf 切出一个完整事件，返回 (event_bytes, remaining_buf)。
        找不到返回 (None, buf)。"""
        for sep in (b"\r\n\r\n", b"\n\n"):
            idx = buf.find(sep)
            if idx != -1:
                return buf[:idx], buf[idx + len(sep) :]
        # 双 \r 视为空行分隔（少数实现）
        idx = buf.find(b"\r\r")
        if idx != -1:
            return buf[:idx], buf[idx + 2 :]
        return None, buf

    for chunk in raw_iter:
        if isinstance(chunk, bytes):
            buffer += chunk
        else:
            buffer += chunk.encode("utf-8", errors="replace")

        while True:
            event_bytes, buffer = _try_take_event(buffer)
            if event_bytes is None:
                break
            if not event_bytes.strip():
                continue

            # 首个有效事件先发 first_chunk（与 probe_stream 保持一致）
            if not first_event_seen:
                first_event_seen = True
                evt_dict = _sse_event_to_dict(
                    event_bytes.decode("utf-8", errors="replace")
                )
                on_event(StreamEvent(kind="first_chunk", raw=evt_dict or {}))

            _process_sse_event(
                event_bytes,
                protocol,
                _inner_on_event,
                done_marker_field,
                done_marker_value,
                text_buf,
            )
            if got_done:
                # 不 return；继续消费直到 raw_iter 耗尽
                continue

    if not first_event_seen:
        on_event(StreamEvent(kind="error", raw={"reason": "no_sse_event"}))

    return got_done, "".join(text_buf)


def _sse_event_to_dict(s: str) -> dict:
    """把一段 SSE 文本解析为 {'event': ..., 'data': ...} 字典。"""
    evt = {}
    data_lines = []
    for line in s.splitlines():
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        if ":" in line:
            field, _, val = line.partition(":")
            val = val.lstrip(" ")
            if field == "data":
                data_lines.append(val)
            elif field == "event":
                evt["event"] = val
            else:
                evt.setdefault("other", []).append((field, val))
    if data_lines:
        evt["data"] = "\n".join(data_lines)
    return evt


def _process_sse_event(
    event_bytes, proto_name, on_event, done_marker_field, done_marker_value, text_buf
):
    """处理一个完整 SSE 事件（bytes 形式），调用 on_event 并写入 text_buf。

    用于在 probe_stream 主循环中按事件逐个解析，绕过 parse_sse_lines 的
    buffer 累积逻辑。
    """
    raw_str = event_bytes.decode("utf-8", errors="replace")
    evt = _sse_event_to_dict(raw_str)
    if not evt:
        return False

    # 终止标记
    for line in raw_str.splitlines():
        line = line.strip()
        if line.startswith(f"{done_marker_field}:"):
            val = line[len(done_marker_field) + 1 :].strip()
            if val == done_marker_value:
                on_event(StreamEvent(kind="done", raw=evt))
                return True

    data = evt.get("data")
    if data and proto_name == "anthropic_messages":
        try:
            j = json.loads(data) if isinstance(data, str) else data
        except (json.JSONDecodeError, TypeError):
            return False
        if j.get("type") == "content_block_delta":
            delta = j.get("delta", {})
            if delta.get("type") == "text_delta" and delta.get("text"):
                on_event(
                    StreamEvent(kind="text_delta", text_delta=delta["text"], raw=j)
                )
        elif j.get("type") == "message_start":
            msg = j.get("message", {})
            on_event(StreamEvent(kind="message_start", model=msg.get("model"), raw=j))
        elif j.get("type") == "message_delta":
            on_event(StreamEvent(kind="message_delta", raw=j))
        elif j.get("type") == "message_stop":
            on_event(StreamEvent(kind="message_stop", raw=j))
    elif data and proto_name in ("openai_chat_completions", "openai_chat_openrouter"):
        try:
            j = json.loads(data) if isinstance(data, str) else data
        except (json.JSONDecodeError, TypeError):
            return False
        if "model" in j and isinstance(j.get("choices"), list):
            delta = j["choices"][0].get("delta", {}) if j.get("choices") else {}
            chunk_text = delta.get("content") or ""
            if chunk_text:
                on_event(
                    StreamEvent(
                        kind="text_delta",
                        text_delta=chunk_text,
                        model=j.get("model"),
                        raw=j,
                    )
                )
        elif "choices" in j:
            on_event(StreamEvent(kind="message_start", model=j.get("model"), raw=j))
    elif data and proto_name == "openai_responses":
        try:
            j = json.loads(data) if isinstance(data, str) else data
        except (json.JSONDecodeError, TypeError):
            return False
        if j.get("type") in ("response.created", "response.in_progress"):
            resp = j.get("response", {})
            on_event(StreamEvent(kind="message_start", model=resp.get("model"), raw=j))
        elif j.get("type") == "response.output_text.delta":
            chunk = j.get("delta", "")
            on_event(StreamEvent(kind="text_delta", text_delta=chunk, raw=j))
        elif j.get("type") in ("response.completed", "response.failed"):
            on_event(StreamEvent(kind="message_stop", raw=j))

    # first_chunk 标记：on_event 由内部决定（已分类型处理）
    return False


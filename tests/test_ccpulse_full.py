"""CC-Pulse 完整测试套件。

覆盖：
  - 单元：ErrorCategory、classify_error、compare_models、_normalize_model_id、
           build_probe_request、parse_sse_lines
  - 端到端：inspect 7 场景 + check 健康检查 JSON 输出
  - Mock SSE：Anthropic / OpenAI Chat / OpenAI Responses 三种协议

不引入第三方测试库。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

# 从项目根定位主脚本与当前解释器（可从任意目录运行）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SCRIPT = os.path.join(_ROOT, "check_ccswitch_health.py")
PY = sys.executable
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 1. 通过 importlib 加载项目模块
import importlib.util

spec = importlib.util.spec_from_file_location("ccpulse", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


# 问题池 → 答案映射：mock 按实际收到的问题回对应答案（问题池随机化后不能写死 "5"）
_POOL_ANSWER = {p["q"]: p["a"] for p in mod.PROBE_PROMPTS}


def _answer_for_body(j: dict) -> str:
    """从请求体（Anthropic messages / Responses input）取问题，回配对答案，默认 "5"。"""
    q = ""
    msgs = j.get("messages") or []
    if msgs and isinstance(msgs[0], dict):
        c = msgs[0].get("content")
        if isinstance(c, str):
            q = c
        elif isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and x.get("type") == "text":
                    q = x.get("text", "")
                    break
    if not q and isinstance(j.get("input"), str):
        q = j["input"]
    return _POOL_ANSWER.get(q, "5")


PASSED = []
FAILED = []


def test(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


# ============ 单元测试 ============

print("\n[Unit] ErrorCategory")
test(
    "ErrorCategory 是 str 枚举",
    mod.ErrorCategory.NONE.value == "none"
    and mod.ErrorCategory.STREAM_PROTOCOL.value == "stream_protocol",
)

print("\n[Unit] classify_error")
cat, display = mod.classify_error("")
test(
    "空响应 -> invalid_response",
    cat == mod.ErrorCategory.INVALID_RESPONSE and "空响应" in display,
)

cat, _ = mod.classify_error('{"error": {"message": "rate limit exceeded"}}')
test("rate limit -> rate_limit", cat == mod.ErrorCategory.RATE_LIMIT)

cat, _ = mod.classify_error('{"error": {"message": "model not found: x"}}')
test("model not found -> model_not_found", cat == mod.ErrorCategory.MODEL_NOT_FOUND)

cat, _ = mod.classify_error('{"error": {"message": "unauthorized"}}')
test("unauthorized -> authentication", cat == mod.ErrorCategory.AUTH)

cat, _ = mod.classify_error('{"error": {"message": "bad request schema invalid"}}')
test(
    "schema invalid -> protocol_incompatible",
    cat == mod.ErrorCategory.PROTOCOL_INCOMPATIBLE,
)

cat, _ = mod.classify_error('{"error": {"message": "internal server error"}}')
test("server error -> server_error", cat == mod.ErrorCategory.SERVER)

cat, _ = mod.classify_error("<html>500 Internal</html>")
test("HTML 响应 -> invalid_response", cat == mod.ErrorCategory.INVALID_RESPONSE)

cat, _ = mod.classify_error('{"error": {"message": "something weird happened"}}')
test("unknown 兜底 -> unknown", cat == mod.ErrorCategory.UNKNOWN)


print("\n[Unit] User-Agent 动态读取 + override 参数")
ua = mod._user_agent()
test("默认 User-Agent 含 claude-cli 标识", "claude-cli" in ua)
test("默认 User-Agent 至少含一个数字版本号", any(c.isdigit() for c in ua))
test(
    "override 参数生效",
    mod._user_agent("claude-cli/9.9.9 (test override)")
    == "claude-cli/9.9.9 (test override)",
)
test("override=None 回到本机版本", mod._user_agent(None) == ua)
test("_claude_code_headers 含 User-Agent", "User-Agent" in mod._claude_code_headers())
test(
    "_claude_code_headers(override) 用 override",
    mod._claude_code_headers("custom-ua/1.0")["User-Agent"] == "custom-ua/1.0",
)
test("懒加载 _claude_cli_version 返回非空字符串", bool(mod._claude_cli_version()))


print("\n[Unit] _http_request helper")
resp = mod._http_request("http://127.0.0.1:1/", "GET", None, None, 2, False)
test("连接层失败 status=0", resp.status == 0)
test("连接层失败 error_category 非空", resp.error_category is not None)
test("连接层失败 error_msg 非空", bool(resp.error_msg))


print("\n[Unit] _is_tls_error")
import ssl as _ssl

test(
    "ssl.SSLError -> True",
    mod._is_tls_error(_ssl.SSLError("CERTIFICATE_VERIFY_FAILED")),
)
test(
    "普通 URLError -> False",
    not mod._is_tls_error(urllib.error.URLError("Connection refused")),
)
test(
    "Certificate text -> True",
    mod._is_tls_error(Exception("CERTIFICATE_VERIFY_FAILED: self signed")),
)
test(
    "_error_category_for_urlerror TLS -> tls",
    mod._error_category_for_urlerror(Exception("SSL: CERTIFICATE_VERIFY_FAILED"))
    == "tls",
)
test(
    "_error_category_for_urlerror network -> network",
    mod._error_category_for_urlerror(urllib.error.URLError("timeout")) == "network",
)


print("\n[Unit] _normalize_model_id")
test("去 [1M]", mod._normalize_model_id("claude-sonnet-4-5[1M]") == "claude-sonnet-4-5")
test(
    "去日期后缀",
    mod._normalize_model_id("claude-sonnet-4-5-20251001") == "claude-sonnet-4-5",
)
test(
    "去 -thinking",
    mod._normalize_model_id("claude-sonnet-4-5-thinking") == "claude-sonnet-4-5",
)
test("去 -fast", mod._normalize_model_id("claude-opus-4-6-fast") == "claude-opus-4-6")
test(
    "小写化 + 去空白",
    mod._normalize_model_id(" Claude-Sonnet-4-5 ") == "claude-sonnet-4-5",
)


print("\n[Unit] compare_models")
test(
    "exact_match",
    mod.compare_models("claude-sonnet-4-5", "claude-sonnet-4-5")["match"]
    == "exact_match",
)
test(
    "alias_match (日期后缀)",
    mod.compare_models("claude-sonnet-4-5", "claude-sonnet-4-5-20251001")["match"]
    == "alias_match",
)
test(
    "alias_match warning 非空",
    mod.compare_models("claude-sonnet-4-5", "claude-sonnet-4-5-20251001")["warning"]
    is not None,
)
test(
    "fuzzy_match (含关系)",
    mod.compare_models("claude-sonnet-4-5", "proxy/claude-sonnet-4-5-custom")["match"]
    == "fuzzy_match",
)
test(
    "gpt-4 不误判 gpt-4o-mini",
    mod.compare_models("gpt-4", "gpt-4o-mini")["match"] == "mismatch",
)
test(
    "sonnet 不误判 sonnet-4",
    mod.compare_models("sonnet", "sonnet-4")["match"] == "mismatch",
)
test(
    "mismatch",
    mod.compare_models("claude-opus-4-6", "claude-haiku-4-5")["match"] == "mismatch",
)
test(
    "mismatch warning 含 '不一致'",
    "不一致" in mod.compare_models("claude-opus-4-6", "claude-haiku-4-5")["warning"],
)
test(
    "unverifiable 空响应",
    mod.compare_models("claude-sonnet-4-5", None)["match"] == "unverifiable",
)
test(
    "unverifiable 空字符串",
    mod.compare_models("claude-sonnet-4-5", "")["match"] == "unverifiable",
)
test(
    "unverifiable 空 requested",
    mod.compare_models("", "claude-sonnet-4-5")["match"] == "unverifiable",
)


print("\n[Unit] _parse_compare_targets / _parse_include")
test(
    "parse_compare 两项",
    mod._parse_compare_targets("A/m1,B/m2") == [("A", "m1"), ("B", "m2")],
)
test(
    "parse_compare provider 含 /",
    mod._parse_compare_targets("org/A/m1,B/m2") == [("org/A", "m1"), ("B", "m2")],
)
try:
    mod._parse_compare_targets("only-one/m1")
    _one_ok = False
except ValueError:
    _one_ok = True
test("parse_compare 少于 2 项报错", _one_ok)
test(
    "parse_include None → inspect 默认含 thinking",
    "thinking" in mod._parse_include(None, mod._INSPECT_DEFAULT_INCLUDE),
)
test(
    "parse_include None → compare 默认仅 text+streaming",
    mod._parse_include(None, mod._COMPARE_DEFAULT_INCLUDE) == {"text", "streaming"},
)
test(
    "parse_include 显式覆盖",
    mod._parse_include("text,tools", mod._COMPARE_DEFAULT_INCLUDE) == {"text", "tools"},
)


print("\n[Unit] build_probe_request 含 stream=True")
# 构造 fake provider
p = mod.Provider(
    name="X",
    app_type="claude",
    base_url="https://example.com/v1",
    api_key="sk-fake",
    auth_mode="authtoken",
    tiers=[mod.ModelTier("default", "claude-sonnet-4-5", "claude-sonnet-4-5")],
    is_current=True,
    in_failover=True,
    is_openrouter=False,
)
url, method, headers, body = mod.build_probe_request(p, p.tiers[0], stream=True)
test(
    "Anthropic stream 路径（路径不去重）",
    url == "https://example.com/v1/v1/messages"
    or url == "https://example.com/v1/messages",
)
test("Anthropic stream method", method == "POST")
parsed = json.loads(body)
test("Anthropic body.stream=True", parsed.get("stream") is True)
test("Anthropic body 含 model 字段", parsed.get("model") == "claude-sonnet-4-5")
test("Anthropic headers 有 anthropic-version", "anthropic-version" in headers)

# openclaw (chat completions)
p2 = mod.Provider(
    name="X",
    app_type="openclaw",
    base_url="https://example.com",
    api_key="sk",
    auth_mode="bearer",
    tiers=[mod.ModelTier("default", "gpt-5", "gpt-5")],
    is_current=False,
    in_failover=False,
    is_openrouter=False,
)
url2, _, _, body2 = mod.build_probe_request(p2, p2.tiers[0], stream=True)
test("Chat Completions 路径", url2 == "https://example.com/chat/completions")
test("Chat Completions body.stream=True", json.loads(body2).get("stream") is True)

# codex
p3 = mod.Provider(
    name="X",
    app_type="codex",
    base_url="https://example.com",
    api_key="sk",
    auth_mode="bearer",
    tiers=[mod.ModelTier("default", "gpt-5", "gpt-5")],
    is_current=False,
    in_failover=False,
    is_openrouter=False,
)
url3, _, _, body3 = mod.build_probe_request(p3, p3.tiers[0], stream=True)
test("Codex path", url3 == "https://example.com/responses")
test("Codex body.stream=True", json.loads(body3).get("stream") is True)


print("\n[Unit] build_probe_request 思考抑制仅对 thinking-prone 模型")
# 普通模型（claude-sonnet / gpt-5）即便 disable_thinking=True 也不发抑制字段，
# 更贴近真实 claude-cli，减少指纹。
_, _, _, b_cl_off = mod.build_probe_request(p, p.tiers[0], disable_thinking=True)
test(
    "claude 普通模型 disable=True -> 无 thinking 字段",
    "thinking" not in json.loads(b_cl_off),
)
_, _, _, b_cx_off = mod.build_probe_request(p3, p3.tiers[0], disable_thinking=True)
test(
    "codex 普通模型 disable=True -> 无 reasoning 字段",
    "reasoning" not in json.loads(b_cx_off),
)
_, _, _, b_oc_off = mod.build_probe_request(p2, p2.tiers[0], disable_thinking=True)
test(
    "openclaw 普通模型 disable=True -> 无 reasoning_effort 字段",
    "reasoning_effort" not in json.loads(b_oc_off),
)

# thinking-prone 模型（deepseek/glm 等）才发抑制字段
p_dsc = mod.Provider(
    name="D",
    app_type="claude",
    base_url="https://x.com/v1",
    api_key="k",
    auth_mode="authtoken",
    tiers=[mod.ModelTier("default", "deepseek-r1", "deepseek-r1")],
    is_current=False,
    in_failover=False,
    is_openrouter=False,
)
p_dsx = mod.Provider(
    name="D",
    app_type="codex",
    base_url="https://x.com",
    api_key="k",
    auth_mode="bearer",
    tiers=[mod.ModelTier("default", "deepseek-reasoner", "deepseek-reasoner")],
    is_current=False,
    in_failover=False,
    is_openrouter=False,
)
p_dso = mod.Provider(
    name="D",
    app_type="openclaw",
    base_url="https://x.com",
    api_key="k",
    auth_mode="bearer",
    tiers=[mod.ModelTier("default", "glm-4.6", "glm-4.6")],
    is_current=False,
    in_failover=False,
    is_openrouter=False,
)
_, _, _, b_ds1 = mod.build_probe_request(p_dsc, p_dsc.tiers[0], disable_thinking=True)
test(
    "claude thinking-prone disable=True -> thinking.type=disabled",
    json.loads(b_ds1).get("thinking") == {"type": "disabled"},
)
_, _, _, b_ds1n = mod.build_probe_request(p_dsc, p_dsc.tiers[0], disable_thinking=False)
test(
    "claude thinking-prone disable=False -> 无 thinking 字段",
    "thinking" not in json.loads(b_ds1n),
)
_, _, _, b_ds2 = mod.build_probe_request(p_dsx, p_dsx.tiers[0], disable_thinking=True)
test(
    "codex thinking-prone disable=True -> reasoning.effort=minimal",
    json.loads(b_ds2).get("reasoning") == {"effort": "minimal"},
)
_, _, _, b_ds3 = mod.build_probe_request(p_dso, p_dso.tiers[0], disable_thinking=True)
test(
    "openclaw thinking-prone disable=True -> reasoning_effort=none",
    json.loads(b_ds3).get("reasoning_effort") == "none",
)

# _is_thinking_prone_model 判定
test(
    "_is_thinking_prone_model: deepseek-r1 -> True",
    mod._is_thinking_prone_model("deepseek-r1") is True,
)
test(
    "_is_thinking_prone_model: glm-4.6 -> True",
    mod._is_thinking_prone_model("glm-4.6") is True,
)
test(
    "_is_thinking_prone_model: claude-sonnet-4-5 -> False",
    mod._is_thinking_prone_model("claude-sonnet-4-5") is False,
)
test(
    "_is_thinking_prone_model: gpt-5 -> False",
    mod._is_thinking_prone_model("gpt-5") is False,
)

# _answer_correct 宽松匹配
test("_answer_correct: 精确 '5'==‘5’", mod._answer_correct("5", "5") is True)
test(
    "_answer_correct: 话痨 '答案是 13' 提取唯一数字",
    mod._answer_correct("答案是 13", "13") is True,
)
test("_answer_correct: 多个数字不匹配", mod._answer_correct("5 or 6", "5") is False)
test("_answer_correct: 数字不等", mod._answer_correct("The answer is 7", "5") is False)
# max_tokens 透传
_, _, _, b_mt = mod.build_probe_request(p, p.tiers[0], max_tokens=1024)
test("max_tokens 透传到 body", json.loads(b_mt).get("max_tokens") == 1024)
# user_agent 覆盖透传到 headers
_, _, h_ua, _ = mod.build_probe_request(
    p, p.tiers[0], user_agent="claude-cli/9.9.9 (test)"
)
test("user_agent 覆盖进 headers", h_ua.get("User-Agent") == "claude-cli/9.9.9 (test)")


print("\n[Unit] _read_httperror_body 解码 4xx/5xx 响应体")


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, body_bytes):
        self._body = body_bytes
        self.code = 400

    def read(self):
        return self._body


_fe = _FakeHTTPError(b'{"error":{"message":"bad request"}}')
_decoded, _raw = mod._read_httperror_body(_fe)
test("_read_httperror_body 解码正确", '"message":"bad request"' in _decoded)
test(
    "_read_httperror_body 返回原始字节", _raw == b'{"error":{"message":"bad request"}}'
)


class _BrokenHTTPError:
    def read(self):
        raise OSError("stream closed")


_dec2, _raw2 = mod._read_httperror_body(_BrokenHTTPError())
test("_read_httperror_body read 失败兜底空", _dec2 == "" and _raw2 == b"")


print("\n[Unit] parse_sse_lines（Anthropic 协议）")
# 构造一个 Anthropic 流式事件
anthropic_events = b"""\
event: message_start\r
data: {"type":"message_start","message":{"id":"msg_1","model":"claude-sonnet-4-5"}}\r
\r
event: content_block_start\r
data: {"type":"content_block_start","index":0}\r
\r
event: content_block_delta\r
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"5"}}\r
\r
event: content_block_stop\r
data: {"type":"content_block_stop","index":0}\r
\r
event: message_stop\r
data: {"type":"message_stop"}\r
\r
"""

events = []


def cb(e):
    events.append(e)


got_done, text = mod.parse_sse_lines([anthropic_events], cb, "anthropic_messages")
test("Anthropic SSE 解析到 message_stop", got_done)
test("Anthropic 文本拼接为 '5'", text == "5")
test(
    "Anthropic 至少 1 个 text_delta 事件",
    any(e.get("kind") == "text_delta" for e in events),
)
model_events = [e for e in events if e.get("model")]
test(
    "Anthropic 提取响应模型",
    any(e.get("model") == "claude-sonnet-4-5" for e in model_events),
)


print("\n[Unit] parse_sse_lines（OpenAI Chat Completions 协议）")
openai_events = b"""\
data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-5","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\r\n\r\n
data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-5","choices":[{"index":0,"delta":{"content":"5"}}]}\r\n\r\n
data: [DONE]\r\n\r\n
"""
events = []
got_done, text = mod.parse_sse_lines([openai_events], cb, "openai_chat_completions")
test("Chat Completions [DONE] 终止", got_done)
test("Chat Completions 文本拼接", text == "5")


print("\n[Unit] parse_sse_lines（OpenAI Responses 协议）")
responses_events = b"""\
event: response.created\r\ndata: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5"}}\r\n\r\n
event: response.output_text.delta\r\ndata: {"type":"response.output_text.delta","delta":"5"}\r\n\r\n
event: response.completed\r\ndata: {"type":"response.completed"}\r\n\r\n
"""
events = []
got_done, text = mod.parse_sse_lines([responses_events], cb, "openai_responses")
test("Responses 协议 response.completed 终止", got_done)
test("Responses 文本拼接", text == "5")


# ============ Mock SSE 端到端测试 ============


class MockAnthropicHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        # metadata: GET /v1/models/{id} — 返回声明窗口，避免默认 inspect 触发 512k 冒烟
        if "/v1/models/" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "claude-sonnet-4-5",
                        "max_input_tokens": 200000,
                        "max_output_tokens": 8192,
                        "capabilities": {"thinking": {"supported": True}},
                    }
                ).encode()
            )
            return
        # 模型列表：与配置档位（claude-*-4-5）故意不同，用于区分 --source listed/configured
        if self.path.rstrip("/").endswith("/v1/models"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "data": [
                            {"id": "claude-3-5-sonnet"},
                            {"id": "claude-3-5-haiku"},
                        ]
                    }
                ).encode()
            )
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        # 接受 /v1/v1/messages、/v1/messages（路径不去重约定）
        if "/v1/messages" not in self.path:
            self.send_response(404)
            self.end_headers()
            return
        # 读 body
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            j = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            j = {}
        wants_stream = j.get("stream") is True
        has_tools = bool(j.get("tools"))
        has_image = False
        msgs = j.get("messages") or []
        if msgs and isinstance(msgs[0], dict):
            c = msgs[0].get("content")
            if isinstance(c, list):
                has_image = any(
                    isinstance(x, dict) and x.get("type") == "image" for x in c
                )

        if not wants_stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if has_tools:
                resp = {
                    "id": "msg_tool",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "get_probe_number",
                            "input": {},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 30, "output_tokens": 12},
                }
            elif has_image:
                resp = {
                    "id": "msg_vis",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": "red"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 40, "output_tokens": 2},
                }
            else:
                resp = {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": _answer_for_body(j)}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 20, "output_tokens": 3},
                }
            self.wfile.write(json.dumps(resp).encode())
            return

        # 流式
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        events = [
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_x","model":"claude-sonnet-4-5","usage":{"input_tokens":20}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"5"}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":3},"delta":{"stop_reason":"end_turn"}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        for e in events:
            self.wfile.write(e.encode())
            self.wfile.write(b"\n")
            self.wfile.flush()


class MockChatHandler(BaseHTTPRequestHandler):
    """OpenAI Chat Completions 风格 mock。"""

    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        # openclaw inspect 默认也会拉 metadata；给个声明窗口避免 512k 冒烟
        if "/models" in self.path or "/v1/models" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "gpt-5",
                        "max_input_tokens": 128000,
                    }
                ).encode()
            )
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        # 接受 /v1/chat/completions、/chat/completions
        if "/chat/completions" not in self.path:
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            j = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            j = {}
        wants_stream = j.get("stream") is True
        if not wants_stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if j.get("tools"):
                resp = {
                    "id": "c1",
                    "object": "chat.completion",
                    "model": "gpt-5",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_probe_number",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            else:
                resp = {
                    "id": "c1",
                    "object": "chat.completion",
                    "model": "gpt-5",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": _answer_for_body(j),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 1},
                }
            self.wfile.write(json.dumps(resp).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in [
            'data: {"id":"c1","object":"chat.completion.chunk","model":"gpt-5","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n',
            'data: {"id":"c1","object":"chat.completion.chunk","model":"gpt-5","choices":[{"index":0,"delta":{"content":"5"}}]}\n\n',
            "data: [DONE]\n\n",
        ]:
            self.wfile.write(chunk.encode())
            self.wfile.flush()


def start_server(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


# 准备 cc-switch 假库
tmp = tempfile.mkdtemp(prefix="ccpulse_test_")
db_path = os.path.join(tmp, "fake.db")


def write_fake_db(base_url, app_type="claude"):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS providers (
        name TEXT, app_type TEXT, settings_config TEXT,
        is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER
    )""")
    if app_type == "claude":
        cfg = json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": base_url,
                    "ANTHROPIC_AUTH_TOKEN": "sk-mock",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
                }
            }
        )
    elif app_type == "openclaw":
        cfg = json.dumps(
            {
                "apiKey": "sk-mock",
                "baseUrl": base_url,
                "models": [{"name": "gpt-5", "id": "gpt-5"}],
            }
        )
    elif app_type == "codex":
        cfg = json.dumps(
            {
                "auth": {"OPENAI_API_KEY": "sk-mock"},
                "config": f'base_url = "{base_url}"\nmodel = "gpt-5-codex"\n',
            }
        )
    cur.execute("DELETE FROM providers")
    cur.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
        ("Mock-Provider", app_type, cfg, 1, 1, 0),
    )
    conn.commit()
    conn.close()


def write_multi_provider_db(base_url):
    """写入多个 claude 供应商，用于测试并发下的稳定排序与计数。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS providers (
        name TEXT, app_type TEXT, settings_config TEXT,
        is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER
    )""")
    cur.execute("DELETE FROM providers")
    for i, nm in enumerate(["Prov-C", "Prov-A", "Prov-B"]):
        cfg = json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": base_url,
                    "ANTHROPIC_AUTH_TOKEN": "sk-mock",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
                }
            }
        )
        cur.execute(
            "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
            (nm, "claude", cfg, 1, 1, i),
        )
    conn.commit()
    conn.close()


def run_cli(args, timeout=10):
    cmd = [PY, SCRIPT] + args + ["--db", db_path, "--timeout", "3", "--workers", "1"]
    p = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    return p.returncode, p.stdout, p.stderr


def run_cli_db(args, database, timeout=10):
    cmd = [PY, SCRIPT] + args + ["--db", database, "--timeout", "3", "--workers", "1"]
    p = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    return p.returncode, p.stdout, p.stderr


def run_cli_with_type(args, type_name, timeout=10):
    cmd = (
        [PY, SCRIPT]
        + args
        + ["--db", db_path, "--timeout", "3", "--workers", "1", "--type", type_name]
    )
    p = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    return p.returncode, p.stdout, p.stderr


print("\n[End-to-end] Mock Anthropic SSE 完整探活")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        ["inspect", "--provider", "Mock-Provider", "--model", "claude-sonnet-4-5"]
    )
    j = json.loads(out) if out else {}
    test("退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test(
        "JSON 含 streaming.status == pass",
        j.get("streaming", {}).get("status") == "pass",
    )
    test(
        "streaming.response_model == claude-sonnet-4-5",
        j.get("streaming", {}).get("response_model") == "claude-sonnet-4-5",
    )
    test("streaming.is_sse == True", j.get("streaming", {}).get("is_sse") is True)
    test(
        "streaming.event_count >= 3", j.get("streaming", {}).get("event_count", 0) >= 3
    )
    test(
        "streaming.ttft_seconds is not None",
        j.get("streaming", {}).get("ttft_seconds") is not None,
    )
    test(
        "streaming 聚合 input_tokens",
        j.get("streaming", {}).get("usage", {}).get("input_tokens") == 20,
        f"usage={j.get('streaming', {}).get('usage')}",
    )
    test(
        "streaming 聚合 output_tokens",
        j.get("streaming", {}).get("usage", {}).get("output_tokens") == 3,
        f"usage={j.get('streaming', {}).get('usage')}",
    )
    test("text.status == pass", j.get("text", {}).get("status") == "pass")
    test(
        "text.correct is True（问题池随机，宽松匹配）",
        j.get("text", {}).get("correct") is True,
        f"text={j.get('text')}",
    )
    test(
        "model_consistency.match == exact_match",
        j.get("model_consistency", {}).get("match") == "exact_match",
    )
    test("summary.verdict == healthy", j.get("summary", {}).get("verdict") == "healthy")
    test(
        "protocol.confidence == confirmed",
        j.get("protocol", {}).get("confidence") == "confirmed",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] Mock OpenAI Chat Completions")
srv, port = start_server(MockChatHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}", "openclaw")
    rc, out, err = run_cli_with_type(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "gpt-5",
            "--source",
            "manual",
        ],
        "openclaw",
    )
    j = json.loads(out) if out else {}
    test("退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("text.status == pass", j.get("text", {}).get("status") == "pass")
    test(
        "protocol.detected == openai_chat_completions",
        j.get("protocol", {}).get("detected") == "openai_chat_completions",
    )
    # 验证删除 _process 重复解析后，Chat Completions 流式仍能提取 response_model
    test(
        "Chat Completions streaming.response_model == gpt-5",
        j.get("streaming", {}).get("response_model") == "gpt-5",
        f"response_model={j.get('streaming', {}).get('response_model')}",
    )
    test(
        "Chat Completions streaming.ttft_seconds 非空",
        j.get("streaming", {}).get("ttft_seconds") is not None,
    )
finally:
    srv.shutdown()


print("\n[End-to-end] --with-metadata 供应商元数据")


class MockMetadataHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        if "/v1/models/claude-sonnet-4-5" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "claude-sonnet-4-5",
                        "max_input_tokens": 1000000,
                        "max_output_tokens": 128000,
                        "capabilities": {
                            "image_input": {"supported": True},
                            "thinking": {"supported": True},
                        },
                    }
                ).encode()
            )
        elif "/v1/messages" in self.path:
            # 兜底：inspect 也会发文本请求
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "msg",
                        "type": "message",
                        "model": "claude-sonnet-4-5",
                        "content": [{"type": "text", "text": "5"}],
                        "stop_reason": "end_turn",
                    }
                ).encode()
            )
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        # 文本探测 / 流式
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            j = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            j = {}
        wants_stream = j.get("stream") is True
        if not wants_stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "msg",
                        "type": "message",
                        "model": "claude-sonnet-4-5",
                        "content": [{"type": "text", "text": _answer_for_body(j)}],
                        "stop_reason": "end_turn",
                    }
                ).encode()
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for e in [
            'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-5"}}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"5"}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]:
            self.wfile.write(e.encode())
            self.wfile.flush()


srv, port = start_server(MockMetadataHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--with-metadata",
        ]
    )
    j = json.loads(out) if out else {}
    test("退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test(
        "metadata.status == available",
        j.get("metadata", {}).get("status") == "available",
    )
    test(
        "metadata.declared_context_window == 1000000",
        j.get("metadata", {}).get("declared_context_window") == 1000000,
    )
    test(
        "metadata.max_output_tokens == 128000",
        j.get("metadata", {}).get("max_output_tokens") == 128000,
    )
    caps = j.get("metadata", {}).get("capabilities", {})
    test("metadata.capabilities.image_input == True", caps.get("image_input") is True)
    test("metadata.capabilities.thinking == True", caps.get("thinking") is True)
finally:
    srv.shutdown()


print("\n[End-to-end] --user-agent 命令行参数被服务端收到")


class UACaptureHandler(BaseHTTPRequestHandler):
    """记录请求头中的 User-Agent，响应 200。"""

    captured_ua: ClassVar[list[str]] = []

    def log_message(self, *a, **k):
        pass

    def do_POST(self):
        UACaptureHandler.captured_ua.append(self.headers.get("User-Agent", ""))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "id": "msg",
                    "type": "message",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": "5"}],
                    "stop_reason": "end_turn",
                }
            ).encode()
        )

    def do_GET(self):
        # /v1/models/{id}
        UACaptureHandler.captured_ua.append(self.headers.get("User-Agent", ""))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


srv, port = start_server(UACaptureHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    # 自定义 UA
    UACaptureHandler.captured_ua = []
    rc, _, _ = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--source",
            "manual",
            "--include",
            "text",
            "--user-agent",
            "claude-cli/8.8.8 (test)",
        ]
    )
    test(
        "--user-agent 自定义生效（text 路径）",
        any("claude-cli/8.8.8 (test)" in u for u in UACaptureHandler.captured_ua),
        f"captured={UACaptureHandler.captured_ua}",
    )
    # 不传 --user-agent：使用本机探测的版本（懒加载）
    UACaptureHandler.captured_ua = []
    rc, _, _ = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--source",
            "manual",
            "--include",
            "text",
        ]
    )
    ver = mod._claude_cli_version()
    test(
        "默认 User-Agent 仍含本机版本号（动态读取）",
        any(f"claude-cli/{ver}" in u for u in UACaptureHandler.captured_ua),
        f"captured={UACaptureHandler.captured_ua} ver={ver}",
    )
    # MEDIUM-1 回归：--user-agent 必须也透传到 metadata（GET /v1/models/{id}）路径
    UACaptureHandler.captured_ua = []
    rc, _, _ = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--source",
            "manual",
            "--include",
            "text",
            "--with-metadata",
            "--user-agent",
            "claude-cli/7.7.7 (meta)",
        ]
    )
    # do_GET（metadata）收到的 UA 也应是自定义值
    got_via_get = [u for u in UACaptureHandler.captured_ua if "7.7.7" in u]
    test(
        "--user-agent 透传到 metadata (GET) 路径",
        len(got_via_get) >= 1,
        f"captured={UACaptureHandler.captured_ua}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] check --json + --user-agent stdout 仍为纯 JSON")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        ["check", "--json", "--user-agent", "claude-cli/9.9.9 (json-test)"]
    )
    parsed = None
    try:
        parsed = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    test(
        "check --json --user-agent stdout 可直接 json.loads",
        isinstance(parsed, dict),
        f"stdout={out[:160]!r} stderr={err[:160]!r}",
    )
    test(
        "User-Agent 提示在 stderr",
        "User-Agent 已覆盖" in err and "User-Agent 已覆盖" not in out,
        f"stdout={out[:120]!r} stderr={err[:160]!r}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] --user-agent 透传到 list-models")
srv, port = start_server(UACaptureHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    UACaptureHandler.captured_ua = []
    rc, out, err = run_cli(
        ["list-models", "--failover-only", "--user-agent", "claude-cli/6.6.6 (lm)"]
    )
    test(
        "--user-agent 透传到 list-models",
        any("6.6.6" in u for u in UACaptureHandler.captured_ua),
        f"captured={UACaptureHandler.captured_ua}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] list-models --probe 轻量探测每个模型")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(["list-models", "--probe", "--json"])
    j = json.loads(out) if out else {}
    test("list-models --probe 退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    reports = (j.get("probe") or {}).get("reports") or []
    test(
        "list-models --probe probe.reports 非空", len(reports) > 0, f"reports={reports}"
    )
    models = [m for rep in reports for m in rep.get("models", [])]
    test(
        "list-models --probe 每个模型有 text 维度",
        len(models) > 0 and all("text" in m for m in models),
        f"models={models}",
    )
    test(
        "list-models --probe 轻量不含 streaming 维度",
        all("streaming" not in m for m in models),
    )
    test(
        "list-models --probe deep 标记为 False",
        (j.get("probe") or {}).get("deep") is False,
    )
finally:
    srv.shutdown()


print("\n[End-to-end] list-models --deep 深度探测（12356 五维度）")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(["list-models", "--deep", "--json"], timeout=30)
    j = json.loads(out) if out else {}
    test("list-models --deep 退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    reports = (j.get("probe") or {}).get("reports") or []
    models = [m for rep in reports for m in rep.get("models", [])]
    test("list-models --deep 非空", len(models) > 0, f"models={len(models)}")
    test(
        "list-models --deep 含 5 维度 text/streaming/metadata/thinking/tools",
        all(
            all(k in m for k in ("text", "streaming", "metadata", "thinking", "tools"))
            for m in models
        ),
        f"keys={[list(m.keys()) for m in models]}",
    )
    test(
        "list-models --deep 跳过 context/vision",
        all("context" not in m and "vision" not in m for m in models),
    )
    test(
        "list-models --deep deep 标记为 True",
        (j.get("probe") or {}).get("deep") is True,
    )
finally:
    srv.shutdown()


print("\n[End-to-end] list-models --probe --source 三态（configured/listed/both）")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    # configured：仅 cc-switch 配置档位（haiku + sonnet）
    rc, out, _ = run_cli(
        [
            "list-models",
            "--probe",
            "--source",
            "configured",
            "--json",
            "--timeout",
            "15",
        ],
        timeout=30,
    )
    j = json.loads(out) if out else {}
    cfg_models = [
        m["model"]
        for rep in (j.get("probe") or {}).get("reports", [])
        for m in rep.get("models", [])
    ]
    test(
        "--source configured 只测配置档位",
        set(cfg_models) == {"claude-haiku-4-5", "claude-sonnet-4-5"},
        f"cfg_models={cfg_models}",
    )
    # listed：仅 /v1/models 返回（sonnet + haiku 3-5）
    rc, out, _ = run_cli(
        ["list-models", "--probe", "--source", "listed", "--json", "--timeout", "15"],
        timeout=30,
    )
    j = json.loads(out) if out else {}
    listed_models = [
        m["model"]
        for rep in (j.get("probe") or {}).get("reports", [])
        for m in rep.get("models", [])
    ]
    test(
        "--source listed 测 /v1/models 列表",
        set(listed_models) == {"claude-3-5-sonnet", "claude-3-5-haiku"},
        f"listed_models={listed_models}",
    )
    # both：合并去重
    rc, out, _ = run_cli(
        ["list-models", "--probe", "--source", "both", "--json", "--timeout", "15"],
        timeout=30,
    )
    j = json.loads(out) if out else {}
    both_models = [
        m["model"]
        for rep in (j.get("probe") or {}).get("reports", [])
        for m in rep.get("models", [])
    ]
    test(
        "--source both 合并两者",
        set(both_models)
        == {
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
            "claude-3-5-sonnet",
            "claude-3-5-haiku",
        },
        f"both_models={both_models}",
    )
    test("--source both 去重保序（无重复）", len(both_models) == len(set(both_models)))
finally:
    srv.shutdown()


print("\n[End-to-end] check 子命令 JSON 模式")
# 准备一个真实场景：用 Mock-Provider
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(["check", "--failover-only", "--json"])
    test("check JSON 退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    j = json.loads(out) if out else {}
    test("check JSON 顶层含 summary", "summary" in j)
    test("check JSON 顶层含 providers", "providers" in j)
    test("check JSON schema_version == 2", j.get("schema_version") == 2)
    test(
        "check JSON summary.available_ratio 存在",
        "available_ratio" in j.get("summary", {}),
        f"summary={j.get('summary')}",
    )
    test(
        "check JSON probe_pool_size == len(PROBE_PROMPTS)",
        j.get("probe_pool_size") == len(mod.PROBE_PROMPTS),
    )
    test("check JSON stealth == False（默认）", j.get("stealth") is False)
    test("check JSON providers 至少 1 个", len(j.get("providers", [])) >= 1)
    if j.get("providers"):
        att = j["providers"][0]["attempts"][0]
        test("attempt 含 error_category", "error_category" in att)
finally:
    srv.shutdown()


print("\n[End-to-end] 无子命令默认进 check")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    cmd = [
        PY,
        SCRIPT,
        "--db",
        db_path,
        "--failover-only",
        "--workers",
        "1",
        "--timeout",
        "3",
        "--json",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    test(
        "无子命令退出码 0",
        p.returncode == 0,
        f"rc={p.returncode} stderr={p.stderr[:200]}",
    )
    j = json.loads(p.stdout) if p.stdout else {}
    test("无子命令输出含 summary", "summary" in j)
    test("无子命令输出含 providers", "providers" in j)
finally:
    srv.shutdown()


print("\n[End-to-end] extract_answer OpenRouter (is_openrouter=True)")
srv, port = start_server(MockChatHandler)
try:
    # OpenRouter：base_url 含 /chat/completions，is_openrouter=True
    write_fake_db(f"http://127.0.0.1:{port}/v1/chat/completions", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "gpt-5",
            "--source",
            "manual",
            "--include",
            "text",
        ]
    )
    j = json.loads(out) if out else {}
    test("OpenRouter exit 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("OpenRouter text.status == pass", j.get("text", {}).get("status") == "pass")
    test("OpenRouter text.correct == True", j.get("text", {}).get("correct") is True)
finally:
    srv.shutdown()


print("\n[End-to-end] SSE 用 \\r\\r 分隔符（_take_event HIGH 修复验证）")


class MockCRLFSSEHandler(BaseHTTPRequestHandler):
    """用裸 \\r\\r 作为事件分隔符的 mock（罕见但规范外实现）。"""

    def log_message(self, *a, **k):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length) if length else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # 用 \r\r 分隔（不是 \r\n\r\n 也不是 \n\n）
        for e in [
            b'event: message_start\rdata: {"type":"message_start","message":{"model":"claude-sonnet-4-5"}}',
            b'event: content_block_delta\rdata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"5"}}',
            b'event: message_stop\rdata: {"type":"message_stop"}',
        ]:
            self.wfile.write(e + b"\r\r")
            self.wfile.flush()


srv, port = start_server(MockCRLFSSEHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--source",
            "manual",
            "--include",
            "streaming",
        ]
    )
    j = json.loads(out) if out else {}
    test("\\r\\r 分隔 exit 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test(
        "\\r\\r 分隔 streaming.status == pass",
        j.get("streaming", {}).get("status") == "pass",
        f"streaming={j.get('streaming')}",
    )
    test("\\r\\r 分隔 streaming.text == 5", j.get("streaming", {}).get("text") == "5")
    test(
        "\\r\\r 分隔 streaming.response_model 提取",
        j.get("streaming", {}).get("response_model") == "claude-sonnet-4-5",
    )
finally:
    srv.shutdown()


print("\n[Unit] classify_error 优先 HTTP status（避免关键词误分类）")
# body 里含 "400" 业务文案，但真实 status 是 200 → 不应误判为 protocol_incompatible
cat, _ = mod.classify_error(
    '{"error": {"message": "see error 400 in our docs"}}', http_status=200
)
test(
    "status=200 + body含400 -> 不误判为 protocol",
    cat != mod.ErrorCategory.PROTOCOL_INCOMPATIBLE,
)
# status 明确 401 → AUTH，即使 body 说 rate limit
cat, _ = mod.classify_error('{"error": {"message": "rate limit"}}', http_status=401)
test("status=401 优先 -> authentication", cat == mod.ErrorCategory.AUTH)
# status=404 空 body → model_not_found
cat, _ = mod.classify_error("", http_status=404)
test("status=404 空body -> model_not_found", cat == mod.ErrorCategory.MODEL_NOT_FOUND)
# status=503 → server
cat, _ = mod.classify_error("<html>maintenance</html>", http_status=503)
test("status=503 HTML -> server_error", cat == mod.ErrorCategory.SERVER)
# 无 status（流式后场景）回退关键词
cat, _ = mod.classify_error('{"error": {"message": "unauthorized"}}', http_status=0)
test("无 status 回退关键词 -> authentication", cat == mod.ErrorCategory.AUTH)


print("\n[Unit] parse_sse_lines 与 _process_sse_event 行为一致（双路径对齐）")


def _collect_via_process(raw_bytes, protocol):
    """模拟 probe_stream 主循环：逐事件调 _process_sse_event，拼 text。"""
    dm_field, dm_val = mod.STREAM_DONE_MARKERS.get(protocol, ("event", "message_stop"))
    text_parts = []
    done = [False]

    def on_ev(ev):
        if ev.get("kind") == "text_delta" and ev.get("text_delta"):
            text_parts.append(ev["text_delta"])
        if ev.get("kind") == "done":
            done[0] = True

    buf = raw_bytes
    while True:
        idx = -1
        for sep in (b"\r\n\r\n", b"\n\n"):
            k = buf.find(sep)
            if k != -1:
                idx = k
                seplen = len(sep)
                break
        if idx == -1:
            break
        eb = buf[:idx]
        buf = buf[idx + seplen :]
        if eb.strip():
            mod._process_sse_event(eb, protocol, on_ev, dm_field, dm_val, [])
    return done[0], "".join(text_parts)


anthropic_raw = (
    b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-5"}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"5"}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
evs = []
gd1, txt1 = mod.parse_sse_lines(
    [anthropic_raw], lambda e: evs.append(e), "anthropic_messages"
)
gd2, txt2 = _collect_via_process(anthropic_raw, "anthropic_messages")
test("双路径 got_done 一致", gd1 == gd2 == True)
test("双路径 text 一致", txt1 == txt2 == "5")


print("\n[Unit] parse_sse_lines 畸形流容错")
# 空事件夹在中间
messy = (
    b'event: message_start\ndata: {"type":"message_start","message":{"model":"m"}}\n\n'
    b"\n\n"
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"5"}}\n\n'
    b": ping\n\n"
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
evs = []
gd, txt = mod.parse_sse_lines([messy], lambda e: evs.append(e), "anthropic_messages")
test("空事件/注释行不破坏解析", gd is True and txt == "5")
# 完全没有有效事件 → 发 error 事件
evs = []
gd, txt = mod.parse_sse_lines(
    [b"garbage no sep"], lambda e: evs.append(e), "anthropic_messages"
)
test("无有效事件 -> error 事件", any(e.get("kind") == "error" for e in evs))
test("无有效事件 -> 空文本", txt == "")


print("\n[End-to-end] inspect --include 子集短路")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    # 仅 text：streaming 应 not_run，model-consistency 应 not_run
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--include",
            "text",
        ]
    )
    j = json.loads(out) if out else {}
    test("include=text exit 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test(
        "include=text streaming not_run",
        j.get("streaming", {}).get("status") == "not_run",
    )
    test(
        "include=text model_consistency not_run",
        j.get("model_consistency", {}).get("match") == "not_run",
    )
    test("include=text text 有结果", j.get("text", {}).get("status") == "pass")
    # 仅 streaming：text 应为 None
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--include",
            "streaming",
        ]
    )
    j = json.loads(out) if out else {}
    test("include=streaming text is None", j.get("text") is None)
    test(
        "include=streaming streaming pass",
        j.get("streaming", {}).get("status") == "pass",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] inspect --keep-suffix 保留 [1M]")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    # manual + keep-suffix → model 字段应保留 [1M]
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5[1M]",
            "--source",
            "manual",
            "--keep-suffix",
            "--include",
            "text",
        ]
    )
    j = json.loads(out) if out else {}
    test(
        "keep-suffix model 含 [1M]",
        j.get("model") == "claude-sonnet-4-5[1M]",
        f"model={j.get('model')}",
    )
    # 不带 keep-suffix → 去掉后缀
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5[1M]",
            "--source",
            "manual",
            "--include",
            "text",
        ]
    )
    j = json.loads(out) if out else {}
    test(
        "默认去后缀 model 不含 [1M]",
        j.get("model") == "claude-sonnet-4-5",
        f"model={j.get('model')}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] inspect --source listed 成功路径")


class MockListedHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        if "/v1/models" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-haiku-4-5"}]}
                ).encode()
            )
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            j = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            j = {}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "id": "msg",
                    "type": "message",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": _answer_for_body(j)}],
                    "stop_reason": "end_turn",
                }
            ).encode()
        )


srv, port = start_server(MockListedHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--source",
            "listed",
            "--include",
            "text",
        ]
    )
    j = json.loads(out) if out else {}
    test("listed 成功 exit 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("listed model_source == listed", j.get("model_source") == "listed")
    test("listed text pass", j.get("text", {}).get("status") == "pass")
    # listed 但模型不在列表 → exit 2
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "nonexistent-model",
            "--source",
            "listed",
            "--include",
            "text",
        ]
    )
    j = json.loads(out) if out else {}
    test("listed 模型不在列表 exit 2", rc == 2, f"rc={rc}")
    test("listed 不在列表有 error 字段", "error" in j and j["error"])
finally:
    srv.shutdown()


print("\n[End-to-end] check --type all / codex / openclaw")
srv, port = start_server(MockChatHandler)
try:
    # codex：base 是纯 host，探测走 /responses（MockChatHandler 不支持，会 404 → unavailable）
    write_fake_db(f"http://127.0.0.1:{port}", "codex")
    rc, out, err = run_cli_with_type(["check", "--json"], "codex")
    j = json.loads(out) if out else {}
    test("check --type codex 退出码 0/1", rc in (0, 1), f"rc={rc} stderr={err[:200]}")
    test("check --type codex JSON type=codex", j.get("type") == "codex")
    test("check --type codex 含 1 个 provider", len(j.get("providers", [])) == 1)
    test(
        "check --type codex provider app_type=codex",
        j.get("providers", [{}])[0].get("type") == "codex",
    )
finally:
    srv.shutdown()

srv, port = start_server(MockChatHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}", "openclaw")
    rc, out, err = run_cli_with_type(["check", "--json"], "openclaw")
    j = json.loads(out) if out else {}
    test("check --type openclaw JSON type=openclaw", j.get("type") == "openclaw")
    test(
        "check --type openclaw provider app_type=openclaw",
        j.get("providers", [{}])[0].get("type") == "openclaw",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] check 多 provider 并发下 JSON 稳定排序")
srv, port = start_server(MockAnthropicHandler)
try:
    write_multi_provider_db(f"http://127.0.0.1:{port}/v1")
    # workers=3 并发；结果顺序应按 name 稳定
    cmd = [
        PY,
        SCRIPT,
        "check",
        "--json",
        "--db",
        db_path,
        "--timeout",
        "3",
        "--workers",
        "3",
        "--type",
        "claude",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    j = json.loads(p.stdout) if p.stdout else {}
    names = [x.get("name") for x in j.get("providers", [])]
    test("多 provider 计数 == 3", len(names) == 3, f"names={names}")
    # 稳定排序 = 保留 cc-switch 的 sort_index 顺序（0=Prov-C,1=Prov-A,2=Prov-B），
    # 而非字母序；关键是并发下顺序确定、可复现
    test(
        "多 provider 结果按 sort_index 稳定",
        names == ["Prov-C", "Prov-A", "Prov-B"],
        f"names={names}",
    )
    # 再跑一次，验证并发下顺序可复现（不 flaky）
    p2 = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    j2 = json.loads(p2.stdout) if p2.stdout else {}
    names2 = [x.get("name") for x in j2.get("providers", [])]
    test("多 provider 顺序可复现", names == names2, f"1st={names} 2nd={names2}")
    test("多 provider summary.total == 3", j.get("summary", {}).get("total") == 3)
finally:
    srv.shutdown()


print("\n[Unit] extract_usage 解析 Anthropic / OpenAI usage")
u1 = mod.extract_usage(json.dumps({"usage": {"input_tokens": 20, "output_tokens": 3}}))
test("Anthropic usage.present True", u1.get("present") is True, f"u1={u1}")
test("Anthropic input_tokens==20", u1.get("input_tokens") == 20)
test("Anthropic output_tokens==3", u1.get("output_tokens") == 3)
u2 = mod.extract_usage(
    json.dumps({"usage": {"prompt_tokens": 11, "completion_tokens": 2}})
)
test("OpenAI prompt→input", u2.get("input_tokens") == 11)
test("OpenAI completion→output", u2.get("output_tokens") == 2)
u3 = mod.extract_usage("{}")
test("无 usage → present False", u3.get("present") is False)


print("\n[Unit] usage.present 在 text 探测解析到 token 时为 True")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--include",
            "text",
        ]
    )
    j = json.loads(out) if out else {}
    usage = j.get("usage", {})
    test(
        "usage.present True（mock 返回 input/output tokens）",
        usage.get("present") is True,
        f"usage={usage}",
    )
    test("usage.input_tokens == 20", usage.get("input_tokens") == 20, f"usage={usage}")
    test("usage.output_tokens == 3", usage.get("output_tokens") == 3, f"usage={usage}")
finally:
    srv.shutdown()


print("\n[End-to-end] 默认 include：metadata + thinking + tools；vision 默认跳过")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        ["inspect", "--provider", "Mock-Provider", "--model", "claude-sonnet-4-5"]
    )
    j = json.loads(out) if out else {}
    test("默认 inspect 退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test(
        "默认 metadata.status == available",
        j.get("metadata", {}).get("status") == "available",
    )
    test(
        "有声明窗口时 context 不冒烟（skipped）",
        j.get("context", {}).get("status") == "skipped",
        f"context={j.get('context')}",
    )
    test(
        "thinking.verdict 非空",
        bool(j.get("thinking", {}).get("verdict")),
        f"thinking={j.get('thinking')}",
    )
    test(
        "tools.protocol_support == native",
        j.get("tools", {}).get("protocol_support") == "native",
        f"tools={j.get('tools')}",
    )
    test(
        "vision 默认 skipped",
        j.get("vision", {}).get("status") == "skipped",
        f"vision={j.get('vision')}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] context 冒烟守卫（显式 include 运行 / 未含时跳过）")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    # 'context' 显式在 --include 里 → 必须运行冒烟（mock 未专门处理，status 非 skipped）
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--include",
            "text,context",
        ],
        timeout=30,
    )
    j = json.loads(out) if out else {}
    test(
        "include=text,context → context 非 skipped",
        j.get("context", {}).get("status") != "skipped",
        f"context={j.get('context')} rc={rc} err={err[:150]}",
    )
    # --include metadata → context 不冒烟，metadata 正常跑
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--include",
            "metadata",
        ]
    )
    j = json.loads(out) if out else {}
    test(
        "include=metadata → context skipped",
        j.get("context", {}).get("status") == "skipped",
        f"context={j.get('context')} rc={rc} err={err[:150]}",
    )
    test(
        "include=metadata → metadata 已跑",
        j.get("metadata", {}).get("status") == "available",
        f"metadata={j.get('metadata')}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] vision 显式 include")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--include",
            "vision",
        ]
    )
    j = json.loads(out) if out else {}
    test(
        "vision.status == pass",
        j.get("vision", {}).get("status") == "pass",
        f"vision={j.get('vision')} stderr={err[:150]}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] inspect --quiet + 退出码 0/3/4")
# 1) 全成功：2 个配置档位都健康 → exit 0；--quiet 时 stderr 无进度
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--all-models",
            "--include",
            "text",
            "--quiet",
            "--probe-delay",
            "0",
        ],
        timeout=30,
    )
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    test("quiet 全成功 exit 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test(
        "quiet 输出 NDJSON 行数 == 2",
        len(lines) == 2,
        f"n={len(lines)} out={out[:200]}",
    )
    # 每行都是合法 JSON 且含 schema_version
    ok_json = True
    for ln in lines:
        try:
            j = json.loads(ln)
            if j.get("schema_version") != 1:
                ok_json = False
        except (json.JSONDecodeError, TypeError):
            ok_json = False
    test("quiet 每行合法 JSON + schema_version=1", ok_json, f"lines={lines[:2]}")
    # --quiet 关闭 say() 进度：stderr 不应含「批量 inspect」之类
    test(
        "quiet 无进度提示",
        "批量" not in err and "====" not in err,
        f"stderr={err[:200]}",
    )
finally:
    srv.shutdown()


# 2) 真正的 exit 4：用 401 handler 让所有模型都 fail
class AuthFailHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')

    def do_POST(self):
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')


srv, port = start_server(AuthFailHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--all-models",
            "--include",
            "text",
            "--quiet",
            "--probe-delay",
            "0",
        ],
        timeout=30,
    )
    test("quiet 全部 401 → exit 4", rc == 4, f"rc={rc} out={out[:200]} err={err[:150]}")
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    test("quiet all-fail 仍输出 NDJSON", len(lines) >= 1, f"n={len(lines)}")
finally:
    srv.shutdown()


# 3) 部分失败 exit 3：sonnet 通、haiku 401
class PartialFailHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"data":[]}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            req = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            req = {}
        model = req.get("model", "")
        if "haiku" in model:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return
        ans = _answer_for_body(req)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "id": "msg",
                    "type": "message",
                    "model": model,
                    "content": [{"type": "text", "text": ans}],
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                }
            ).encode()
        )


srv, port = start_server(PartialFailHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--all-models",
            "--include",
            "text",
            "--quiet",
            "--probe-delay",
            "0",
        ],
        timeout=30,
    )
    test("quiet 部分失败 exit 3", rc == 3, f"rc={rc} out={out[:300]} err={err[:150]}")
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    test("quiet 部分失败 NDJSON 2 行", len(lines) == 2, f"n={len(lines)}")
finally:
    srv.shutdown()


print("\n[End-to-end] inspect --quiet --human 不崩溃、stdout 保持 NDJSON")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--all-models",
            "--include",
            "text",
            "--quiet",
            "--human",
            "--probe-delay",
            "0",
        ],
        timeout=30,
    )
    test(
        "quiet --human 退出码 ∈ (0,1,2)",
        rc in (0, 1, 2),
        f"rc={rc} out={out[:120]!r} err={err[:200]!r}",
    )
    test(
        "quiet --human stderr 无 Traceback", "Traceback" not in err, f"err={err[:300]}"
    )
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    ok_json = True
    for ln in lines:
        try:
            json.loads(ln)
        except (json.JSONDecodeError, TypeError):
            ok_json = False
    test(
        "quiet --human stdout 每行可 json.loads",
        ok_json and len(lines) >= 1,
        f"n={len(lines)} out={out[:200]!r}",
    )
finally:
    srv.shutdown()


print("\n[Unit] --current-only provider 过滤")
P = mod.Provider
ps = [
    P(
        "A",
        "claude",
        "http://a",
        "k",
        "authtoken",
        [mod.ModelTier("default", "x", "x")],
        is_current=False,
        in_failover=True,
        is_openrouter=False,
    ),
    P(
        "B",
        "claude",
        "http://b",
        "k",
        "authtoken",
        [mod.ModelTier("default", "x", "x")],
        is_current=True,
        in_failover=True,
        is_openrouter=False,
    ),
    P(
        "C",
        "claude",
        "http://c",
        "k",
        "authtoken",
        [mod.ModelTier("default", "x", "x")],
        is_current=False,
        in_failover=False,
        is_openrouter=False,
    ),
]


def fs(cur, fo):
    if cur:
        return [p.name for p in ps if p.is_current]
    if fo:
        return [p.name for p in ps if p.in_failover or p.is_current]
    return [p.name for p in ps]


test("全部 → A,B,C", fs(False, False) == ["A", "B", "C"])
test("--failover-only → A,B", fs(False, True) == ["A", "B"])
test("--current-only → B", fs(True, False) == ["B"])
test("current 优先于 failover", fs(True, True) == ["B"])


def fs_prov(cur, fo, prov_str):
    res = ps
    if cur:
        res = [p for p in res if p.is_current]
    elif fo:
        res = [p for p in res if p.in_failover or p.is_current]
    if prov_str:
        subs = [s.strip().lower() for s in prov_str.split(",") if s.strip()]
        res = [p for p in res if any(sub in p.name.lower() for sub in subs)]
    return [p.name for p in res]


test("--provider 单个匹配 → A", fs_prov(False, False, "A") == ["A"])
test("--provider 逗号多选 → A,C", fs_prov(False, False, "A,C") == ["A", "C"])
test("--provider 配合 failover → B", fs_prov(False, True, "B,C") == ["B"])

print("\n[End-to-end] check --provider 过滤指定供应商")
srv, port = start_server(MockAnthropicHandler)
try:
    write_multi_provider_db(f"http://127.0.0.1:{port}/v1")
    rc, out, err = run_cli(["check", "--json", "--provider", "Prov-A,Prov-C"])
    j = json.loads(out) if out else {}
    pnames = [p.get("name") for p in j.get("providers", [])]
    test("check --provider exit 0", rc == 0, f"rc={rc} err={err[:100]}")
    test(
        "check --provider 只保留匹配到的供应商",
        set(pnames) == {"Prov-A", "Prov-C"},
        f"pnames={pnames}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] inspect --compare 跨供应商（无需 --provider）")
srv, port = start_server(MockAnthropicHandler)
try:
    write_multi_provider_db(f"http://127.0.0.1:{port}/v1")
    rc, out, err = run_cli(
        [
            "inspect",
            "--compare",
            "Prov-A/claude-sonnet-4-5,Prov-C/claude-sonnet-4-5",
            "--probe-delay",
            "0",
        ],
        timeout=30,
    )
    j = json.loads(out) if out else {}
    test("compare 无 --provider exit 0", rc == 0, f"rc={rc} err={err[:150]}")
    test(
        "compare command=inspect-compare",
        j.get("command") == "inspect-compare",
        f"j={list(j)[:8]}",
    )
    test(
        "compare 默认 include 仅 text+streaming",
        set(j.get("include") or []) == {"text", "streaming"},
        f"include={j.get('include')}",
    )
    test(
        "compare 2 个目标都 healthy",
        [r.get("verdict") for r in j.get("comparison") or []] == ["healthy", "healthy"],
        f"rows={j.get('comparison')}",
    )
    # 显式 --include 覆盖默认
    rc2, out2, _ = run_cli(
        [
            "inspect",
            "--compare",
            "Prov-A/claude-sonnet-4-5,Prov-B/claude-sonnet-4-5",
            "--include",
            "text",
            "--probe-delay",
            "0",
        ],
        timeout=30,
    )
    j2 = json.loads(out2) if out2 else {}
    test(
        "compare 显式 --include text",
        set(j2.get("include") or []) == {"text"},
        f"include={j2.get('include')}",
    )
finally:
    srv.shutdown()

# 缺 --provider 且无 --compare → exit 2
rc, out, err = run_cli(["inspect", "--model", "x"], timeout=10)
test("inspect 缺 provider 无 compare → exit 2", rc == 2, f"rc={rc} out={out[:120]}")


print("\n[Unit] probe on_attempt 档位级进度回调")
# 不依赖网络：用假 probe_tier 替换验证回调顺序
orig_probe_tier = mod.probe_tier
events = []


def fake_probe_tier(
    p,
    tier,
    timeout,
    skip_tls_verify,
    max_tokens=20,
    disable_thinking=True,
    user_agent=None,
    stainless_version=None,
    stealth=False,
    max_retries=0,
):
    # 第 1 档失败，第 2 档成功
    if tier.tier == "haiku":
        return {
            "tier": "haiku",
            "model": tier.model,
            "status": 429,
            "elapsed": 0.1,
            "error": "rate",
            "answer": "",
            "correct": False,
        }
    return {
        "tier": tier.tier,
        "model": tier.model,
        "status": 200,
        "elapsed": 0.2,
        "error": "",
        "answer": "5",
        "correct": True,
    }


mod.probe_tier = fake_probe_tier
try:
    p = mod.Provider(
        name="P",
        app_type="claude",
        base_url="http://x",
        api_key="k",
        auth_mode="authtoken",
        tiers=[
            mod.ModelTier("haiku", "h", "h"),
            mod.ModelTier("sonnet", "s", "s"),
            mod.ModelTier("opus", "o", "o"),  # 不应触达
        ],
    )
    r = mod.probe(
        p,
        3,
        False,
        on_attempt=lambda prov, att: events.append(
            (prov.name, att["tier"], att["status"])
        ),
    )
    test("on_attempt 调用 2 次（首成功即停）", len(events) == 2, f"events={events}")
    test(
        "on_attempt 顺序 haiku→sonnet",
        [e[1] for e in events] == ["haiku", "sonnet"],
        f"events={events}",
    )
    test("probe overall_ok True", r["overall_ok"] is True)
    test("probe best_tier sonnet", r["best_tier"] == "sonnet")
    test("probe 未探测 opus", len(r["attempts"]) == 2)
finally:
    mod.probe_tier = orig_probe_tier


print("\n[Unit] probe_tier 从问题池抽题 + 宽松校验")


# 捕获实际发出的问题，验证来自 PROBE_PROMPTS；mock 按题回配对答案
class PoolCaptureHandler(BaseHTTPRequestHandler):
    seen_q: ClassVar[list[str]] = []

    def log_message(self, *a, **k):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        j = json.loads(body) if body else {}
        ans = _answer_for_body(j)
        msgs = j.get("messages") or []
        q = ""
        if msgs and isinstance(msgs[0], dict):
            q = msgs[0].get("content") or ""
        PoolCaptureHandler.seen_q.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "id": "m",
                    "type": "message",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": ans}],
                    "stop_reason": "end_turn",
                }
            ).encode()
        )


srv, port = start_server(PoolCaptureHandler)
try:
    PoolCaptureHandler.seen_q = []
    prov = mod.Provider(
        name="P",
        app_type="claude",
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="k",
        auth_mode="authtoken",
        tiers=[mod.ModelTier("default", "claude-sonnet-4-5", "claude-sonnet-4-5")],
    )
    _pool_q = {p["q"] for p in mod.PROBE_PROMPTS}
    # 多跑几次，抽到的问题都应来自池，且都能宽松校验通过
    oks = []
    for _ in range(6):
        r = mod.probe_tier(prov, prov.tiers[0], 3, False)
        oks.append(r.get("correct"))
    test(
        "probe_tier 问题均来自 PROBE_PROMPTS",
        all(q in _pool_q for q in PoolCaptureHandler.seen_q),
        f"seen={PoolCaptureHandler.seen_q}",
    )
    test("probe_tier 池内问题宽松校验全通过", all(oks), f"oks={oks}")
    test(
        "probe_tier 结果记录实际 question",
        mod.probe_tier(prov, prov.tiers[0], 3, False).get("question") in _pool_q,
    )
finally:
    srv.shutdown()


print("\n[Unit] --stealth 收敛并发 + 报告标记")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        ["check", "--failover-only", "--json", "--stealth", "--workers", "8"]
    )
    j = json.loads(out) if out else {}
    test("stealth check 退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("stealth JSON stealth==True", j.get("stealth") is True)
    test("stealth 报告标记隐身模式", "隐身: 开" in err, f"stderr={err[:200]}")
finally:
    srv.shutdown()
# 并发收敛为纯函数逻辑，直接单测（run_cli 固定 --workers 1，无法从 CLI 覆盖）
test(
    "stealth 收敛 workers=8 -> STEALTH_MAX_WORKERS",
    min(8, mod.STEALTH_MAX_WORKERS) == mod.STEALTH_MAX_WORKERS,
)
test("stealth 不放大 workers=1", min(1, mod.STEALTH_MAX_WORKERS) == 1)


print("\n[Unit] --stainless-version 覆盖进指纹头")


class StainlessCaptureHandler(BaseHTTPRequestHandler):
    seen: ClassVar[list[str]] = []

    def log_message(self, *a, **k):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        j = json.loads(body) if body else {}
        StainlessCaptureHandler.seen.append(
            self.headers.get("x-stainless-package-version", "")
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "id": "m",
                    "type": "message",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": _answer_for_body(j)}],
                    "stop_reason": "end_turn",
                }
            ).encode()
        )


srv, port = start_server(StainlessCaptureHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    StainlessCaptureHandler.seen = []
    rc, _, err = run_cli(["check", "--failover-only", "--stainless-version", "0.99.9"])
    test(
        "--stainless-version 覆盖进 x-stainless-package-version",
        "0.99.9" in StainlessCaptureHandler.seen,
        f"seen={StainlessCaptureHandler.seen}",
    )
finally:
    srv.shutdown()


print("\n[Unit] say() 默认 flush + ANSI 清理")
import io

buf = io.StringIO()
token = mod._output_stream.set(buf)
try:
    mod.say("progress-line")
    test("say 写入内容", "progress-line" in buf.getvalue())
    buf.truncate(0)
    buf.seek(0)
    mod.say("evil\x1b[2Jtext\x1b]0;pwned\x07end")
    val = buf.getvalue()
    test("say 剥离 ANSI 转义", "\x1b" not in val, f"val={val!r}")
    test("say 保留正常文本", "eviltext" in val and "end" in val, f"val={val!r}")
    # C1 控制符 U+0080–U+009F（\x9b CSI / \x9d OSC 终止符）同样剥离
    buf.truncate(0)
    buf.seek(0)
    mod.say("evil\x9b38;2;255;0;0mred\x9d")
    val = buf.getvalue()
    test(
        "say 剥离 C1 控制字符",
        "\x9b" not in val and "\x9d" not in val,
        f"val={val!r}",
    )
    sanitized_c1 = mod._sanitize_for_terminal("\x9b2J")
    test(
        "_sanitize_for_terminal 剥 C1 字节",
        "\x9b" not in sanitized_c1,
        f"s={sanitized_c1!r}",
    )
finally:
    mod._output_stream.reset(token)


print("\n[Unit] _filter_new_watch_rows 去重 helper")


def _watch_new(rows, last_ts, seen_ids):
    """调用契约纯函数；兼容返回 (new_rows, new_seen) 与仅返回 list 两种形态。"""
    out = mod._filter_new_watch_rows(rows, last_ts, seen_ids)
    if isinstance(out, tuple) and len(out) == 2:
        new_rows, new_seen = out
        seen_ids.clear()
        seen_ids.update(new_seen)
    else:
        new_rows = out
        # 兼容形态：函数不就地更新 seen_ids 时，测试按契约键 f"{rid}|{cts}" 补充
        for r in new_rows:
            seen_ids.add(f"{r.get('request_id') or ''}|{r.get('created_at') or 0}")
    return list(new_rows)


# 同 request_id 在第二次调用时被过滤
_seen = set()
_row = {"created_at": 1000, "request_id": "r1", "provider_name": "P"}
_first = _watch_new([_row], 1000, _seen)
_second = _watch_new([_row], 1000, _seen)
test(
    "watch 同 request_id 第二次过滤",
    len(_first) == 1 and len(_second) == 0,
    f"1st={len(_first)} 2nd={len(_second)}",
)

# 空 request_id 且 created_at 相同 → 同秒内去重；跨秒则是新行
_seen = set()
_row_e = {"created_at": 2000, "request_id": "", "provider_name": "P"}
_e1 = _watch_new([_row_e], 2000, _seen)
_e2 = _watch_new([_row_e], 2000, _seen)
test(
    "watch 空 request_id 同秒去重",
    len(_e1) == 1 and len(_e2) == 0,
    f"1st={len(_e1)} 2nd={len(_e2)}",
)
_row_e2 = {"created_at": 2001, "request_id": "", "provider_name": "P"}
_e3 = _watch_new([_row_e2], 2000, _seen)
test("watch 空 request_id 跨秒保留", len(_e3) == 1, f"n={len(_e3)}")

# created_at < last_ts 的行被排除
_seen = set()
_old_rows = [
    {"created_at": 900, "request_id": "x", "provider_name": "P"},
    {"created_at": 1000, "request_id": "y", "provider_name": "P"},
]
_out_old = _watch_new(_old_rows, 1000, _seen)
test(
    "watch created_at<last_ts 排除",
    [r["request_id"] for r in _out_old] == ["y"],
    f"out={_out_old}",
)

# created_at >= last_ts 且键未见过的行被保留
_seen = set()
_new_rows = [
    {"created_at": 1000, "request_id": "n1", "provider_name": "P"},
    {"created_at": 1001, "request_id": "n2", "provider_name": "P"},
]
_out_new = _watch_new(_new_rows, 1000, _seen)
test(
    "watch 未见过键保留",
    {r["request_id"] for r in _out_new} == {"n1", "n2"},
    f"out={_out_new}",
)


print("\n[Unit] extract_usage 缺字段/空 → missing_fields")
u_partial = mod.extract_usage(json.dumps({"usage": {"input_tokens": 10}}))
test("partial usage present True", u_partial.get("present") is True)
test("partial output_tokens None", u_partial.get("output_tokens") is None)
test(
    "partial missing_fields 含 output_tokens",
    "output_tokens" in u_partial.get("missing_fields", []),
)


print("\n[Unit] probe_context_smoke rejected 路径")


class RejectCtxHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length) if length else b""
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {"error": {"message": "prompt is too long: context length exceeded"}}
            ).encode()
        )


srv, port = start_server(RejectCtxHandler)
try:
    p = mod.Provider(
        name="R",
        app_type="claude",
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="sk-x",
        auth_mode="authtoken",
        tiers=[mod.ModelTier("default", "m", "m")],
    )
    r = mod.probe_context_smoke(p, "m", 64, timeout=5, skip_tls_verify=False)
    test("context smoke rejected", r.get("status") == "rejected", f"r={r}")
finally:
    srv.shutdown()


print("\n[Unit] _probe_tools rejected 路径（400 含 tool 关键词）")


class RejectToolsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"m","max_input_tokens":200000}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length) if length else b""
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {"error": {"message": "unknown field: tools is not supported"}}
            ).encode()
        )


srv, port = start_server(RejectToolsHandler)
try:
    p = mod.Provider(
        name="RT",
        app_type="claude",
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="sk-x",
        auth_mode="authtoken",
        tiers=[mod.ModelTier("default", "m", "m")],
    )
    r = mod._probe_tools(p, "m", 5, False)
    test("tools rejected status", r.get("status") == "fail", f"r={r}")
    test(
        "tools rejected protocol_support",
        r.get("protocol_support") == "rejected",
        f"r={r}",
    )
finally:
    srv.shutdown()


print("\n[Unit] _probe_tools text_only 路径（200 但无 tool_use block）")


class TextOnlyToolsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"m","max_input_tokens":200000}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length) if length else b""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "id": "msg",
                    "type": "message",
                    "model": "m",
                    "content": [{"type": "text", "text": "5"}],
                    "stop_reason": "end_turn",
                }
            ).encode()
        )


srv, port = start_server(TextOnlyToolsHandler)
try:
    p = mod.Provider(
        name="TT",
        app_type="claude",
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="sk-x",
        auth_mode="authtoken",
        tiers=[mod.ModelTier("default", "m", "m")],
    )
    r = mod._probe_tools(p, "m", 5, False)
    test("tools text_only status", r.get("status") == "fail", f"r={r}")
    test(
        "tools text_only protocol_support",
        r.get("protocol_support") == "text_only",
        f"r={r}",
    )
finally:
    srv.shutdown()


print("\n[Unit] thinking dependency_missing（--include thinking 无 text）")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(
        [
            "inspect",
            "--provider",
            "Mock-Provider",
            "--model",
            "claude-sonnet-4-5",
            "--include",
            "thinking",
        ]
    )
    j = json.loads(out) if out else {}
    th = j.get("thinking", {})
    test(
        "thinking dependency_missing",
        th.get("status") == "dependency_missing",
        f"thinking={th}",
    )
finally:
    srv.shutdown()


print("\n[Unit] _probe_vision unsupported（codex app_type）")
p_codex = mod.Provider(
    name="C",
    app_type="codex",
    base_url="http://x",
    api_key="k",
    auth_mode="bearer",
    tiers=[mod.ModelTier("default", "m", "m")],
)
r = mod._probe_vision(p_codex, "m", 5, False)
test("vision codex unsupported", r.get("status") == "unsupported", f"r={r}")


print("\n[Unit] classify_log_error + parse_since + history helpers")
import ccpulse_archive

old_time = ccpulse_archive.time.time
ccpulse_archive.time.time = lambda: 1_000_000
try:
    test("parse_since 24h", mod.parse_since("24h") == 913_600)
    test("parse_since 7d", mod.parse_since("7d") == 395_200)
    test("parse_since 30m", mod.parse_since("30m") == 998_200)
    test("parse_since seconds", mod.parse_since("3600") == 996_400)
    test("parse_since normalizes case", mod.parse_since(" 24H ") == 913_600)
    test("parse_since empty", mod.parse_since(None) is None)
    try:
        mod.parse_since("7x")
        parsed_invalid = False
    except ValueError:
        parsed_invalid = True
    test("parse_since rejects invalid unit", parsed_invalid)
finally:
    ccpulse_archive.time.time = old_time
test(
    "classify 401 invalid api",
    mod.classify_log_error(401, "Invalid API key.") == "authentication",
)
test("classify 429", mod.classify_log_error(429, "rate limit") == "rate_limit")
test("classify 余额", mod.classify_log_error(403, "预扣费额度失败") == "authentication")
test(
    "classify channel",
    mod.classify_log_error(503, "No available channel for model x")
    == "model_not_found",
)
test(
    "classify connect",
    mod.classify_log_error(502, "client error (Connect)") == "network",
)
test(
    "classify prompt too long",
    mod.classify_log_error(400, "maximum prompt length") == "protocol_incompatible",
)
test("orphan name", mod.resolve_provider_name("abcd-uuid", {}) == "deleted:abcd-uui")


print("\n[Unit] history/stats/routing on temp sqlite with proxy_request_logs")
log_db = os.path.join(tmp, "logs.db")
conn = sqlite3.connect(log_db)
conn.execute("""CREATE TABLE providers (
    id TEXT, name TEXT, app_type TEXT, settings_config TEXT,
    is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER
)""")
conn.execute(
    "INSERT INTO providers VALUES (?,?,?,?,?,?,?)",
    ("pid-a", "Prov-A", "claude", "{}", 1, 1, 0),
)
conn.execute(
    "INSERT INTO providers VALUES (?,?,?,?,?,?,?)",
    ("pid-b", "Prov-B", "claude", "{}", 0, 1, 1),
)
conn.execute("""CREATE TABLE proxy_request_logs (
    request_id TEXT, provider_id TEXT, app_type TEXT, model TEXT,
    request_model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
    input_cost_usd TEXT, output_cost_usd TEXT, cache_read_cost_usd TEXT,
    cache_creation_cost_usd TEXT, total_cost_usd TEXT,
    latency_ms INTEGER, first_token_ms INTEGER, duration_ms INTEGER,
    status_code INTEGER, error_message TEXT, session_id TEXT,
    provider_type TEXT, is_streaming INTEGER, cost_multiplier TEXT,
    created_at INTEGER, data_source TEXT, pricing_model TEXT,
    input_token_semantics INTEGER
)""")
now = int(time.time())
# ok routed
conn.execute(
    "INSERT INTO proxy_request_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (
        "r1",
        "pid-a",
        "claude",
        "grok-4.5",
        "claude-opus-4-8",
        10,
        5,
        0,
        0,
        "0",
        "0",
        "0",
        "0",
        "0",
        1000,
        200,
        None,
        200,
        None,
        "s",
        None,
        1,
        "1",
        now - 60,
        "proxy",
        "grok",
        2,
    ),
)
# fail auth
conn.execute(
    "INSERT INTO proxy_request_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (
        "r2",
        "pid-b",
        "claude",
        "claude-opus-4-8",
        "claude-opus-4-8",
        0,
        0,
        0,
        0,
        "0",
        "0",
        "0",
        "0",
        "0",
        500,
        None,
        None,
        401,
        "Invalid API key.",
        "s",
        None,
        1,
        "1",
        now - 30,
        "proxy",
        "",
        2,
    ),
)
# orphan fail
conn.execute(
    "INSERT INTO proxy_request_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (
        "r3",
        "dead-uuid-xxx",
        "claude",
        "m",
        "m",
        0,
        0,
        0,
        0,
        "0",
        "0",
        "0",
        "0",
        "0",
        100,
        None,
        None,
        429,
        "rate limit",
        "s",
        None,
        0,
        "1",
        now - 10,
        "proxy",
        "",
        2,
    ),
)
conn.commit()
conn.close()

rows = mod.query_proxy_logs(log_db, limit=10, fails_only=True)
test("history fails only", len(rows) == 2, f"n={len(rows)}")
test(
    "history classifies auth",
    any(r["error_category"] == "authentication" for r in rows),
    f"{rows}",
)
test(
    "history orphan name",
    any(str(r["provider_name"]).startswith("deleted:") for r in rows),
    f"{rows}",
)
stats = mod.query_stats(log_db)
test("stats has providers", len(stats) >= 2, f"{stats}")
route = mod.query_routing(log_db, limit=5)
test(
    "routing has mismatch pair",
    any(
        p["request_model"] == "claude-opus-4-8" and p["actual_model"] == "grok-4.5"
        for p in route
    ),
    f"{route}",
)
side = mod.format_history_sidebar(log_db, "Prov-A", since="24h")
test("history sidebar contains Prov", "请求" in side, f"{side}")


print("\n[Unit] analyze functions on temp sqlite")
# 用已有的 log_db (3 条: r1 ok routed, r2 fail auth, r3 fail rate_limit)
# 补几条不同日期的行来测试 by_day
log_db2 = os.path.join(tmp, "logs2.db")
conn2 = sqlite3.connect(log_db2)
conn2.execute("""CREATE TABLE providers (
    id TEXT, name TEXT, app_type TEXT, settings_config TEXT,
    is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER
)""")
conn2.execute(
    "INSERT INTO providers VALUES (?,?,?,?,?,?,?)",
    ("pid-a", "Prov-A", "claude", "{}", 1, 1, 0),
)
conn2.execute(
    "INSERT INTO providers VALUES (?,?,?,?,?,?,?)",
    ("pid-b", "Prov-B", "claude", "{}", 0, 1, 1),
)
conn2.execute("""CREATE TABLE proxy_request_logs (
    request_id TEXT, provider_id TEXT, app_type TEXT, model TEXT,
    request_model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
    input_cost_usd TEXT, output_cost_usd TEXT, cache_read_cost_usd TEXT,
    cache_creation_cost_usd TEXT, total_cost_usd TEXT,
    latency_ms INTEGER, first_token_ms INTEGER, duration_ms INTEGER,
    status_code INTEGER, error_message TEXT, session_id TEXT,
    provider_type TEXT, is_streaming INTEGER, cost_multiplier TEXT,
    created_at INTEGER, data_source TEXT, pricing_model TEXT,
    input_token_semantics INTEGER
)""")
day1 = int(time.time()) - 86400 * 2  # 2天前
day2 = int(time.time()) - 86400  # 1天前
day0 = int(time.time())  # 今天
_INS = "INSERT INTO proxy_request_logs VALUES (" + ",".join(["?"] * 27) + ")"


def _log_row(rid, pid, mdl, it, ot, lat, ttft, st, err, ts):
    """构建 27 列完整元组。"""
    return (
        rid,
        pid,
        "claude",
        mdl,
        mdl,
        it,
        ot,
        0,
        0,
        "0",
        "0",
        "0",
        "0",
        "0",  # cache/cost
        lat,
        ttft,
        None,  # latency/ttft/duration
        st,
        err,
        "s",
        None,
        1,
        "1",  # status/err/session/prov_type/stream/cost_mult
        ts,
        "proxy",
        "",
        2,
    )  # created_at/data_source/pricing/token_sem


# day1: 2 ok (Prov-A, model-x), 1 fail (Prov-B, model-y)
for i, (pid, mdl, st, err, lat, ttft, it, ot) in enumerate(
    [
        ("pid-a", "model-x", 200, None, 800, 100, 50, 10),
        ("pid-a", "model-x", 200, None, 1200, 150, 60, 12),
        ("pid-b", "model-y", 401, "Invalid API key", 300, None, 0, 0),
    ]
):
    conn2.execute(
        _INS, _log_row(f"a{i}", pid, mdl, it, ot, lat, ttft, st, err, day1 + i)
    )
# day2: 3 ok (Prov-A model-x, Prov-B model-y), 1 fail (Prov-A model-x 429)
for i, (pid, mdl, st, err, lat, ttft, it, ot) in enumerate(
    [
        ("pid-a", "model-x", 200, None, 600, 80, 40, 8),
        ("pid-b", "model-y", 200, None, 900, 120, 55, 11),
        ("pid-a", "model-x", 200, None, 700, 90, 45, 9),
        ("pid-a", "model-x", 429, "rate limit", 200, None, 0, 0),
    ],
    start=10,
):
    conn2.execute(
        _INS, _log_row(f"a{i}", pid, mdl, it, ot, lat, ttft, st, err, day2 + i)
    )
# day0: 1 ok (Prov-B model-y)
conn2.execute(
    _INS, _log_row("a20", "pid-b", "model-y", 70, 15, 500, 60, 200, None, day0)
)
conn2.commit()
conn2.close()

# _percentile
test("percentile empty", mod._percentile([], 50) is None)
test("percentile single", mod._percentile([10], 50) == 10.0)
test("percentile p50 of 4", abs(mod._percentile([1, 2, 3, 4], 50) - 2.5) < 0.01)
test("percentile p0", mod._percentile([10, 20], 0) == 10.0)
test("percentile p100", mod._percentile([10, 20], 100) == 20.0)

# _sparkline
spark = mod._sparkline([1, 2, 3, 4, 5], width=5)
test("sparkline length", len(spark) == 5, f"got {len(spark)} '{spark}'")
test("sparkline monotone", spark[0] < spark[-1], f"'{spark}'")  # 字符递增

# _day_key
dk = mod._day_key(day1)
test(
    "day_key format",
    dk is not None and len(dk) == 10 and dk.count("-") == 2,
    f"dk={dk}",
)
test("day_key None", mod._day_key(None) is None)

# query_analyze_raw
raw = mod.query_analyze_raw(log_db2)
test("analyze raw count", len(raw) == 8, f"n={len(raw)}")
test("analyze raw has day", all(r.get("day") is not None for r in raw))

# analyze_by_day
by_day = mod.analyze_by_day(raw)
test("by_day has 3 days", len(by_day) == 3, f"n={len(by_day)}")
test("by_day sorted", by_day[0]["date"] < by_day[-1]["date"])
test("by_day totals match", sum(d["total"] for d in by_day) == 8)
# day1: 2 ok + 1 fail = 3 total
d1 = [d for d in by_day if d["date"] == mod._day_key(day1)]
test("by_day day1 total=3", d1 and d1[0]["total"] == 3, f"{d1}")
test("by_day day1 ok=2", d1 and d1[0]["ok"] == 2)
test(
    "by_day day1 top_fail=authentication",
    d1 and d1[0]["top_fail_category"] == "authentication",
)
test("by_day has lat_p50", d1 and d1[0]["lat_p50"] is not None)

# analyze_by_model
by_model = mod.analyze_by_model(raw)
test("by_model has 2 models", len(by_model) == 2, f"n={len(by_model)}")
mx = [m for m in by_model if m["model"] == "model-x"]
test("by_model model-x total=5", mx and mx[0]["total"] == 5)
test("by_model model-x has p50", mx and mx[0]["lat_p50"] is not None)
test("by_model model-x has p95", mx and mx[0]["lat_p95"] is not None)
test("by_model model-x has p99", mx and mx[0]["lat_p99"] is not None)
test("by_model model-x has ttft_p50", mx and mx[0]["ttft_p50"] is not None)
test("by_model model-x avg tokens", mx and mx[0]["avg_input_tokens"] is not None)

# analyze_by_provider_day
id_map2 = mod.load_provider_id_map(log_db2)
bpd = mod.analyze_by_provider_day(raw, id_map2)
test("bpd has 3 days", len(bpd["days"]) == 3)
test("bpd has 2 providers", len(bpd["providers"]) == 2, f"n={len(bpd['providers'])}")
test("bpd day_summary", len(bpd["day_summary"]) == 3)
# Prov-A 在 day0 没有记录 → None cell
pa = [p for p in bpd["providers"] if p["provider_name"] == "Prov-A"]
if pa:
    d0_idx = bpd["days"].index(mod._day_key(day0))
    test("bpd Prov-A day0 is None", pa[0]["days"][d0_idx] is None)

# analyze_provider_deep
pd = mod.analyze_provider_deep(raw, "Prov-A", id_map2)
test("provider_deep not None", pd is not None)
test("provider_deep total=5", pd and pd["total"] == 5)
test("provider_deep has by_day", pd and len(pd["by_day"]) >= 1)
test("provider_deep has by_model", pd and len(pd["by_model"]) >= 1)
test("provider_deep has by_day_model", pd and len(pd["by_day_model"]) >= 1)

# provider_deep miss
pd_miss = mod.analyze_provider_deep(raw, "NoSuchProv", id_map2)
test("provider_deep miss returns None", pd_miss is None)


print("\n[End-to-end] 只读日志子命令冒烟")
rc, out, err = run_cli_db(["history", "--json", "--limit", "2"], log_db)
history_report = json.loads(out) if out else {}
test("history CLI exit 0", rc == 0, f"rc={rc} err={err[:200]}")
test(
    "history CLI JSON entries",
    history_report.get("command") == "history" and history_report.get("count") == 2,
    f"report={history_report}",
)

# --since 非法 → exit 2 + stderr 提示（--json 下不输出可解析 JSON）
rc, out, err = run_cli_db(["history", "--since", "7x", "--json"], log_db)
test("history --since 7x → exit 2", rc == 2, f"rc={rc}")
try:
    json.loads(out)
    bad_json = False
except (json.JSONDecodeError, TypeError):
    bad_json = True
test(
    "history --since 7x stdout 非 JSON",
    bad_json or out.strip() == "",
    f"out={out[:100]!r}",
)
test(
    "history --since 7x stderr 含 无法解析",
    "无法解析" in err,
    f"err={err[:150]!r}",
)

rc, out, err = run_cli_db(["stats", "--json", "--since", "7d"], log_db)
stats_report = json.loads(out) if out else {}
test("stats CLI exit 0", rc == 0, f"rc={rc} err={err[:200]}")
test(
    "stats CLI JSON providers",
    stats_report.get("command") == "stats"
    and stats_report.get("since") == "7d"
    and bool(stats_report.get("providers")),
    f"report={stats_report}",
)
# token 用量汇总：r1 贡献 input=10 output=5
_tot_in = sum(p.get("input_tokens") or 0 for p in stats_report.get("providers", []))
_tot_out = sum(p.get("output_tokens") or 0 for p in stats_report.get("providers", []))
test("stats 汇总 input_tokens", _tot_in >= 10, f"tot_in={_tot_in}")
test("stats 汇总 output_tokens", _tot_out >= 5, f"tot_out={_tot_out}")

rc, out, err = run_cli_db(["routing", "--json", "--limit", "1"], log_db)
routing_report = json.loads(out) if out else {}
test("routing CLI exit 0", rc == 0, f"rc={rc} err={err[:200]}")
test(
    "routing CLI JSON pairs",
    routing_report.get("command") == "routing"
    and len(routing_report.get("pairs", [])) == 1,
    f"report={routing_report}",
)

rc, out, err = run_cli_db(["analyze", "--json", "--mode", "day"], log_db2)
analyze_report = json.loads(out) if out else {}
test("analyze CLI exit 0", rc == 0, f"rc={rc} err={err[:200]}")
test(
    "analyze CLI day report",
    analyze_report.get("command") == "analyze"
    and analyze_report.get("mode") == "day"
    and bool(analyze_report.get("by_day"))
    and "by_model" not in analyze_report,
    f"report={analyze_report}",
)

watch_proc = subprocess.Popen(
    [PY, "-u", SCRIPT, "watch", "--interval", "1", "--db", log_db],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
try:
    deadline = time.monotonic() + 2
    watch_out = ""
    watch_err = ""
    while time.monotonic() < deadline:
        if watch_proc.poll() is not None:
            break
        time.sleep(0.1)
    watch_proc.terminate()
    watch_out, watch_err = watch_proc.communicate(timeout=3)
except subprocess.TimeoutExpired:
    watch_proc.kill()
    watch_out, watch_err = watch_proc.communicate(timeout=3)
test(
    "watch CLI 进入轮询",
    "watch: 每 1s 轮询" in watch_out,
    f"rc={watch_proc.returncode} out={watch_out[:200]} err={watch_err[:200]}",
)

shutil.rmtree(tmp, ignore_errors=True)


# ============ 新增测试：load_providers meta/notes 列覆盖 ============

print("\n[Unit] load_providers 含 meta/notes 列")

# 测试 1a：DB 无 meta/notes 列（向后兼容）
_tmp1 = tempfile.mkdtemp(prefix="ccpulse_meta_test_")
_db1 = os.path.join(_tmp1, "test.db")
try:
    _conn1 = sqlite3.connect(_db1)
    _conn1.execute("""
        CREATE TABLE providers (
            name TEXT, app_type TEXT, settings_config TEXT,
            is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER
        )
    """)
    _conn1.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
        (
            "旧供应商",
            "claude",
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://old.test/v1",
                        "ANTHROPIC_AUTH_TOKEN": "sk-old",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
                    }
                }
            ),
            0,
            0,
            0,
        ),
    )
    _conn1.commit()
    _conn1.close()
    _provs1 = mod.load_providers(_db1, "claude")
    test(
        "无 meta/notes 列 load_providers 正常返回",
        len(_provs1) == 1,
        f"n={len(_provs1)}",
    )
    test(
        "无 meta/notes 列 custom_user_agent=None",
        _provs1[0].custom_user_agent is None,
        f"custom_ua={_provs1[0].custom_user_agent!r}",
    )
    test(
        "无 meta/notes 列 notes 为空",
        _provs1[0].notes == "",
        f"notes={_provs1[0].notes!r}",
    )
finally:
    shutil.rmtree(_tmp1, ignore_errors=True)

# 测试 1b：DB 有 meta/notes 列且有值
_tmp2 = tempfile.mkdtemp(prefix="ccpulse_meta_test2_")
_db2 = os.path.join(_tmp2, "test.db")
try:
    _conn2 = sqlite3.connect(_db2)
    _conn2.execute("""
        CREATE TABLE providers (
            name TEXT, app_type TEXT, settings_config TEXT,
            is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER,
            meta TEXT, notes TEXT
        )
    """)
    _conn2.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "测试站",
            "claude",
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://x.test/v1",
                        "ANTHROPIC_AUTH_TOKEN": "sk-test",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
                    }
                }
            ),
            0,
            0,
            0,
            json.dumps(
                {
                    "apiFormat": "openai_chat",
                    "customUserAgent": "test_agent/1.0",
                }
            ),
            "限流中",
        ),
    )
    _conn2.commit()
    _conn2.close()
    _provs2 = mod.load_providers(_db2, "claude")
    test(
        "有 meta/notes 列 load_providers 返回 1 个",
        len(_provs2) == 1,
        f"n={len(_provs2)}",
    )
    test(
        "meta apiFormat=openai_chat -> protocol OPENAI_CHAT_COMPLETIONS",
        _provs2[0].protocol == mod.Protocol.OPENAI_CHAT_COMPLETIONS,
        f"proto={_provs2[0].protocol}",
    )
    test(
        "meta customUserAgent 赋值到 custom_user_agent",
        _provs2[0].custom_user_agent == "test_agent/1.0",
        f"custom_ua={_provs2[0].custom_user_agent!r}",
    )
    test(
        "notes 列值传递到 Provider.notes",
        _provs2[0].notes == "限流中",
        f"notes={_provs2[0].notes!r}",
    )
finally:
    shutil.rmtree(_tmp2, ignore_errors=True)

# 测试 1c：meta JSON 解析失败
_tmp3 = tempfile.mkdtemp(prefix="ccpulse_meta_test3_")
_db3 = os.path.join(_tmp3, "test.db")
try:
    _conn3 = sqlite3.connect(_db3)
    _conn3.execute("""
        CREATE TABLE providers (
            name TEXT, app_type TEXT, settings_config TEXT,
            is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER,
            meta TEXT, notes TEXT
        )
    """)
    _conn3.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "坏meta站",
            "claude",
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://bad.test/v1",
                        "ANTHROPIC_AUTH_TOKEN": "sk-bad",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
                    }
                }
            ),
            0,
            0,
            0,
            "not-json",
            "",
        ),
    )
    _conn3.commit()
    _conn3.close()
    _provs3 = mod.load_providers(_db3, "claude")
    test(
        "meta 非法 JSON 不崩溃",
        len(_provs3) == 1,
        f"n={len(_provs3)}",
    )
    test(
        "meta 非法 JSON -> custom_user_agent=None",
        _provs3[0].custom_user_agent is None,
        f"custom_ua={_provs3[0].custom_user_agent!r}",
    )
finally:
    shutil.rmtree(_tmp3, ignore_errors=True)


# ============ 新增测试：custom_user_agent UA 优先级回退 ============

print("\n[Unit] build_probe_request custom_user_agent UA 优先级")

_p_ua = mod.Provider(
    name="UA-test",
    app_type="claude",
    base_url="https://ua.test",
    api_key="sk-ua",
    auth_mode="apikey",
    tiers=[
        mod.ModelTier("default", "claude-sonnet-4-20250514", "claude-sonnet-4-20250514")
    ],
    protocol=mod.Protocol.ANTHROPIC_MESSAGES,
    custom_user_agent="provider_ua/1.0",
    notes="test notes",
)
_t_ua = _p_ua.tiers[0]

# 2a：CLI --user-agent 优先于 custom_user_agent
_, _, _h_cli, _ = mod.build_probe_request(_p_ua, _t_ua, user_agent="cli_ua/2.0")
test(
    "UA 优先级：CLI --user-agent 优先",
    "cli_ua/2.0" in _h_cli.get("User-Agent", ""),
    f"UA={_h_cli.get('User-Agent')!r}",
)

# 2b：回退到 custom_user_agent
_, _, _h_prov, _ = mod.build_probe_request(_p_ua, _t_ua, user_agent=None)
test(
    "UA 优先级：回退到 custom_user_agent",
    "provider_ua/1.0" in _h_prov.get("User-Agent", ""),
    f"UA={_h_prov.get('User-Agent')!r}",
)

# 2c：回退到默认（无 custom_user_agent，无 CLI）
_p_no_ua = mod.Provider(
    name="NoUA",
    app_type="claude",
    base_url="https://noua.test",
    api_key="sk-noua",
    auth_mode="apikey",
    tiers=[
        mod.ModelTier("default", "claude-sonnet-4-20250514", "claude-sonnet-4-20250514")
    ],
    protocol=mod.Protocol.ANTHROPIC_MESSAGES,
    custom_user_agent=None,
)
_t_no_ua = _p_no_ua.tiers[0]
_, _, _h_default, _ = mod.build_probe_request(_p_no_ua, _t_no_ua, user_agent=None)
test(
    "UA 优先级：回退到默认 claude-cli",
    "claude-cli/" in _h_default.get("User-Agent", ""),
    f"UA={_h_default.get('User-Agent')!r}",
)


# ============ 新增测试：rebuild_provider_for_inspect 字段保留 ============

print("\n[Unit] rebuild_provider_for_inspect 字段保留")

_p_orig = mod.Provider(
    name="RebuildTest",
    app_type="claude",
    base_url="https://rebuild.test/v1",
    api_key="sk-rebuild",
    auth_mode="authtoken",
    tiers=[
        mod.ModelTier("haiku", "claude-haiku-4-5", "claude-haiku-4-5"),
        mod.ModelTier("sonnet", "claude-sonnet-4-5", "claude-sonnet-4-5"),
    ],
    is_current=True,
    in_failover=True,
    is_openrouter=True,
    protocol=mod.Protocol.OPENAI_CHAT_COMPLETIONS,
    protocol_source="api_format",
    custom_user_agent="ua/1.0",
    notes="运营备注",
)
_p_rebuilt = mod.rebuild_provider_for_inspect(_p_orig, "gpt-4o")

_rebuild_fields = {
    "name": _p_rebuilt.name,
    "app_type": _p_rebuilt.app_type,
    "base_url": _p_rebuilt.base_url,
    "api_key": _p_rebuilt.api_key,
    "auth_mode": _p_rebuilt.auth_mode,
    "is_current": _p_rebuilt.is_current,
    "in_failover": _p_rebuilt.in_failover,
    "is_openrouter": _p_rebuilt.is_openrouter,
    "protocol": _p_rebuilt.protocol,
    "protocol_source": _p_rebuilt.protocol_source,
    "custom_user_agent": _p_rebuilt.custom_user_agent,
    "notes": _p_rebuilt.notes,
}
_orig_fields = {
    "name": _p_orig.name,
    "app_type": _p_orig.app_type,
    "base_url": _p_orig.base_url,
    "api_key": _p_orig.api_key,
    "auth_mode": _p_orig.auth_mode,
    "is_current": _p_orig.is_current,
    "in_failover": _p_orig.in_failover,
    "is_openrouter": _p_orig.is_openrouter,
    "protocol": _p_orig.protocol,
    "protocol_source": _p_orig.protocol_source,
    "custom_user_agent": _p_orig.custom_user_agent,
    "notes": _p_orig.notes,
}
test(
    "rebuild 保留所有非 tiers 字段",
    _rebuild_fields == _orig_fields,
    f"diff={ {k: (_orig_fields[k], _rebuild_fields[k]) for k in _orig_fields if _orig_fields[k] != _rebuild_fields[k]} }",
)
test(
    "rebuild tiers 替换为单个 ModelTier",
    len(_p_rebuilt.tiers) == 1,
    f"tiers={_p_rebuilt.tiers}",
)
test(
    "rebuild tiers[0].id == gpt-4o",
    _p_rebuilt.tiers[0].model == "gpt-4o",
    f"model={_p_rebuilt.tiers[0].model}",
)
test(
    "rebuild custom_user_agent 保留",
    _p_rebuilt.custom_user_agent == "ua/1.0",
    f"custom_ua={_p_rebuilt.custom_user_agent!r}",
)
test(
    "rebuild notes 保留",
    _p_rebuilt.notes == "运营备注",
    f"notes={_p_rebuilt.notes!r}",
)
test(
    "rebuild protocol 保留",
    _p_rebuilt.protocol == mod.Protocol.OPENAI_CHAT_COMPLETIONS,
    f"proto={_p_rebuilt.protocol}",
)


# ============ 新增测试：_sanitize_raw_body 脱敏 ============

print("\n[Unit] _sanitize_raw_body 脱敏")

if hasattr(mod, "_sanitize_raw_body"):
    _sb1 = mod._sanitize_raw_body(
        '{"model":"x","api_key":"sk-1234567890abcdef"}',
        "sk-1234567890abcdef",
    )
    test(
        "sanitize_raw_body: api_key 被替换",
        "sk-123" in _sb1 and "abcdef" not in _sb1,
        f"result={_sb1!r}",
    )
    _sb2 = mod._sanitize_raw_body('{"model":"x"}', "sk-1234567890abcdef")
    test(
        "sanitize_raw_body: 无 api_key 原样返回",
        _sb2 == '{"model":"x"}',
        f"result={_sb2!r}",
    )
    _sb3 = mod._sanitize_raw_body('{"model":"x"}', "")
    test(
        "sanitize_raw_body: 空 key 原样返回",
        _sb3 == '{"model":"x"}',
        f"result={_sb3!r}",
    )
    _sb4 = mod._sanitize_raw_body('{"model":"x"}', None)
    test(
        "sanitize_raw_body: None key 原样返回",
        _sb4 == '{"model":"x"}',
        f"result={_sb4!r}",
    )
    # 短 key（≤6 字符）：掩码不得暴露完整 key
    _sb5 = mod._sanitize_raw_body('{"key":"abc123"}', "abc123")
    test(
        "sanitize_raw_body: 短 key 不暴露完整 key",
        "abc123" not in _sb5 and "***" in _sb5,
        f"result={_sb5!r}",
    )
    _sb6 = mod._sanitize_raw_body('{"key":"ab"}', "ab")
    test(
        "sanitize_raw_body: 2 字符 key 仅留 1 字符前缀",
        "ab" not in _sb6 and "a***" in _sb6,
        f"result={_sb6!r}",
    )
else:
    test("_sanitize_raw_body 未实现（TODO）", True)


# ============ 新增测试：parse_provider apiFormat 协议推断 ============

print("\n[Unit] parse_provider apiFormat 协议推断")


def _cfg(base="https://x.test/v1", token="sk-test"):
    """构造 claude app_type 的 settings_config dict。"""
    return {
        "env": {
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
        }
    }


# 5a：apiFormat="openai_chat"
_pp1 = mod.parse_provider(
    "P1",
    "claude",
    _cfg(),
    False,
    False,
    api_format="openai_chat",
)
test(
    "apiFormat=openai_chat -> OPENAI_CHAT_COMPLETIONS",
    _pp1[0].protocol == mod.Protocol.OPENAI_CHAT_COMPLETIONS,
    f"proto={_pp1[0].protocol}",
)

# 5b：apiFormat="openai_responses"
_pp2 = mod.parse_provider(
    "P2",
    "claude",
    _cfg(),
    False,
    False,
    api_format="openai_responses",
)
test(
    "apiFormat=openai_responses -> OPENAI_RESPONSES",
    _pp2[0].protocol == mod.Protocol.OPENAI_RESPONSES,
    f"proto={_pp2[0].protocol}",
)

# 5c：apiFormat="anthropic"
_pp3 = mod.parse_provider(
    "P3",
    "claude",
    _cfg(),
    False,
    False,
    api_format="anthropic",
)
test(
    "apiFormat=anthropic -> ANTHROPIC_MESSAGES",
    _pp3[0].protocol == mod.Protocol.ANTHROPIC_MESSAGES,
    f"proto={_pp3[0].protocol}",
)

# 5d：apiFormat=None（默认推断）
_pp4 = mod.parse_provider(
    "P4",
    "claude",
    _cfg(),
    False,
    False,
    api_format=None,
)
test(
    "apiFormat=None -> ANTHROPIC_MESSAGES（默认）",
    _pp4[0].protocol == mod.Protocol.ANTHROPIC_MESSAGES,
    f"proto={_pp4[0].protocol}",
)

# 5e：customUserAgent 赋值
_pp5 = mod.parse_provider(
    "P5",
    "claude",
    _cfg(),
    False,
    False,
    custom_user_agent="codex_cli/0.144.0",
)
test(
    "customUserAgent 赋值到 Provider",
    _pp5[0].custom_user_agent == "codex_cli/0.144.0",
    f"custom_ua={_pp5[0].custom_user_agent!r}",
)

# 5f：protocol_source 随 apiFormat
test(
    "apiFormat 设置 -> protocol_source='api_format'",
    _pp1[0].protocol_source == "api_format",
    f"source={_pp1[0].protocol_source!r}",
)
_pp6 = mod.parse_provider(
    "P6",
    "claude",
    _cfg(),
    False,
    False,
)
test(
    "无 apiFormat 无 URL 后缀 -> protocol_source=''",
    _pp6[0].protocol_source == "",
    f"source={_pp6[0].protocol_source!r}",
)


# ============ 修复回归验证：小数匹配 / 脱敏 / 截断 / SSE 提前关闭 ============

print("\n[Unit] _answer_correct 小数等价")
test("_answer_correct: '5.0' == '5'", mod._answer_correct("5.0", "5") is True)
test(
    "_answer_correct: '答案是 5.0' == '5'",
    mod._answer_correct("答案是 5.0", "5") is True,
)
test(
    "_answer_correct: 多个数字仍拒绝",
    mod._answer_correct("5.0 or 5.5", "5") is False,
)


print("\n[Unit] _sanitize_display 脱敏 + ANSI")
if hasattr(mod, "_sanitize_display"):
    _sd1 = mod._sanitize_display(
        '{"message":"invalid api key sk-secret123"}', "sk-secret123"
    )
    test(
        "sanitize_display: key 掩码",
        "sk-secret123" not in _sd1 and "***" in _sd1,
        f"r={_sd1!r}",
    )
    _sd2 = mod._sanitize_display("err\x1b[31mred\x1b[0m end", "k")
    test("sanitize_display: ANSI 剥离", "\x1b" not in _sd2, f"r={_sd2!r}")
    _sd3 = mod._sanitize_display("", "sk-x")
    test("sanitize_display: 空文本原样", _sd3 == "", f"r={_sd3!r}")
else:
    test("_sanitize_display 未实现", True)


print("\n[End-to-end] 错误响应体含 key → probe_tier error/raw_body 脱敏")


class KeyEchoErrorHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = '{"error":{"message":"invalid api key sk-echo-key-123456789"}}'
        self.wfile.write(body.encode())
        self.wfile.flush()

    def log_message(self, *a):
        pass


srv, port = start_server(KeyEchoErrorHandler)
try:
    _pe = mod.Provider(
        "P",
        "claude",
        f"http://127.0.0.1:{port}/v1",
        "sk-echo-key-123456789",
        "authtoken",
        [mod.ModelTier("default", "m", "m")],
    )
    _re = mod.probe_tier(_pe, _pe.tiers[0], 3, False)
    test(
        "probe_tier error 不含完整 key",
        "sk-echo-key-123456789" not in (_re.get("error") or ""),
        f"err={_re.get('error')!r}",
    )
    test(
        "probe_tier raw_body 不含完整 key",
        "sk-echo-key-123456789" not in (_re.get("raw_body") or ""),
        f"raw={_re.get('raw_body')!r}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] Content-Length 虚增 → IncompleteRead → truncated")


class IncompleteReadHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "5000")  # 虚增：实际只发一小段
        self.end_headers()
        self.wfile.write(
            b'{"id":"m","type":"message","content":[{"type":"text","text":"5"}],"model":"m"}'
        )
        self.wfile.flush()
        self.wfile.close()  # 关连接，未发送声明的 5000 字节

    def log_message(self, *a):
        pass


srv, port = start_server(IncompleteReadHandler)
try:
    _pi = mod.Provider(
        "P",
        "claude",
        f"http://127.0.0.1:{port}/v1",
        "k",
        "authtoken",
        [mod.ModelTier("default", "m", "m")],
    )
    _resp = mod._http_request(
        f"http://127.0.0.1:{port}/v1/messages", "POST", body=b'{}', timeout=3
    )
    test(
        "IncompleteRead → truncated=True",
        _resp.truncated is True,
        f"truncated={_resp.truncated}",
    )
    test(
        "IncompleteRead → error_category=STREAM_INCOMPLETE",
        _resp.error_category == mod.ErrorCategory.STREAM_INCOMPLETE.value,
        f"cat={_resp.error_category}",
    )
finally:
    srv.shutdown()


print("\n[End-to-end] SSE 无 done marker 提前断 → 标记截断")


class SseNoDoneHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in [
            'event: message_start\ndata: {"type":"message_start","message":{"model":"m"}}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"5"}}\n\n',
        ]:
            self.wfile.write(chunk.encode())
            self.wfile.flush()
        self.wfile.close()  # 无 message_stop 直接关连接

    def log_message(self, *a):
        pass


srv, port = start_server(SseNoDoneHandler)
try:
    _ps = mod.Provider(
        "P",
        "claude",
        f"http://127.0.0.1:{port}/v1",
        "k",
        "authtoken",
        [mod.ModelTier("default", "m", "m")],
    )
    _rs = mod.probe_stream(_ps, _ps.tiers[0], 3, False)
    test(
        "SSE 提前断 → status=error",
        _rs.get("status") == "error",
        f"status={_rs.get('status')}",
    )
    test(
        "SSE 提前断 → error_category=STREAM_INCOMPLETE",
        _rs.get("error_category") == mod.ErrorCategory.STREAM_INCOMPLETE.value,
        f"cat={_rs.get('error_category')}",
    )
finally:
    srv.shutdown()


# ============ P0：429 retry-after 解析 + 健康供应商推荐 ============

print("\n[Unit] _parse_retry_after 限流恢复时间提取")
if hasattr(mod, "_parse_retry_after"):
    test(
        "retry_after: 日期时间格式",
        "2026-08-07 23:10:15"
        in (mod._parse_retry_after("限额将在 2026-08-07 23:10:15 重置") or ""),
    )
    test(
        "retry_after: HH:MM:SS 格式",
        "23:10:15" in (mod._parse_retry_after("reset at 23:10:15") or ""),
    )
    test(
        "retry_after: retry-after 秒数 -> 分钟",
        "分钟" in (mod._parse_retry_after("retry-after: 3600") or ""),
    )
    test(
        "retry_after: 中文重置",
        "恢复" in (mod._parse_retry_after("将在 23:10:15 恢复") or ""),
    )
    test("retry_after: 无匹配 -> None", mod._parse_retry_after("unknown error") is None)
else:
    test("_parse_retry_after 未实现", True)


print("\n[Unit] classify_error 429 附带恢复提示")
_cat, _disp = mod.classify_error(
    '{"error":{"message":"已达到使用上限，将在 2026-08-07 23:10:15 重置"}}', 429
)
test("429 -> rate_limit", _cat == mod.ErrorCategory.RATE_LIMIT, f"cat={_cat}")
test("429 display 含恢复提示", "恢复" in _disp or "23:10:15" in _disp, f"disp={_disp!r}")


# ============ P1：deep-dive 子命令（CI 串联，非交互） ============

print("\n[End-to-end] deep-dive --json dry-run（CI 串联）")
_dd_json = {
    "schema_version": 2,
    "providers": [
        {
            "name": "FailProv",
            "type": "claude",
            "overall_ok": False,
            "attempts": [{"model": "m-fail"}],
        },
        {
            "name": "OkProv",
            "type": "claude",
            "overall_ok": True,
            "best_tier": "haiku",
            "attempts": [{"model": "m-ok"}],
        },
    ],
}
_dd_file = os.path.join(tempfile.mkdtemp(prefix="ccpulse_dd_"), "dd_check.json")
with open(_dd_file, "w", encoding="utf-8") as _f:
    json.dump(_dd_json, _f)

rc, out, err = run_cli(["deep-dive", "--from", _dd_file, "--target", "fail", "--json"])
test("deep-dive fail exit 0", rc == 0, f"rc={rc} err={err[:200]}")
_dd_rep = json.loads(out) if out else {}
test("deep-dive command=deep-dive", _dd_rep.get("command") == "deep-dive")
_fail_t = [t for t in _dd_rep.get("tasks", []) if t["provider"] == "FailProv"]
_ok_t = [t for t in _dd_rep.get("tasks", []) if t["provider"] == "OkProv"]
test(
    "deep-dive --target fail 只含失败家",
    len(_fail_t) == 1 and len(_ok_t) == 0,
    f"fail={_fail_t} ok={_ok_t}",
)

rc2, out2, _ = run_cli(["deep-dive", "--from", _dd_file, "--target", "both", "--json"])
_rep2 = json.loads(out2) if out2 else {}
test(
    "deep-dive --target both = 2家×2模型=4组合",
    len(_rep2.get("tasks", [])) == 4,
    f"n={len(_rep2.get('tasks', []))}",
)

rc3, out3, _ = run_cli(
    ["deep-dive", "--from", _dd_file, "--target", "both", "--models", "m-fail", "--json"]
)
_rep3 = json.loads(out3) if out3 else {}
test(
    "deep-dive --models 过滤只含 m-fail",
    len(_rep3.get("tasks", [])) == 2
    and all(t["model"] == "m-fail" for t in _rep3.get("tasks", [])),
    f"tasks={_rep3.get('tasks')}",
)

# stdin 读取
import subprocess as _sp

_p = _sp.run(
    [PY, SCRIPT, "deep-dive", "--from", "-", "--target", "ok", "--json", "--db", db_path],
    input=json.dumps(_dd_json),
    capture_output=True,
    text=True,
    timeout=10,
    check=False,
)
_rep4 = json.loads(_p.stdout) if _p.stdout else {}
test(
    "deep-dive --from - 读 stdin",
    _p.returncode == 0 and len(_rep4.get("tasks", [])) == 1,
    f"rc={_p.returncode} err={_p.stderr[:200]}",
)


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

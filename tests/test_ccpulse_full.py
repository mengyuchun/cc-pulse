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
import re
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# 从项目根定位主脚本与当前解释器（可从任意目录运行）
_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(_HERE), "check_ccswitch_health.py")
PY = sys.executable

# 1. 通过 importlib 加载项目模块
import importlib.util
spec = importlib.util.spec_from_file_location("ccpulse", SCRIPT)
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
        print(f"  ✗ {name}  {detail}")


# ============ 单元测试 ============

print("\n[Unit] ErrorCategory")
test("ErrorCategory 是 str 枚举",
     mod.ErrorCategory.NONE.value == "none"
     and mod.ErrorCategory.STREAM_PROTOCOL.value == "stream_protocol")

print("\n[Unit] classify_error")
cat, display = mod.classify_error("")
test("空响应 -> invalid_response", cat == mod.ErrorCategory.INVALID_RESPONSE
     and "空响应" in display)

cat, _ = mod.classify_error('{"error": {"message": "rate limit exceeded"}}')
test("rate limit -> rate_limit", cat == mod.ErrorCategory.RATE_LIMIT)

cat, _ = mod.classify_error('{"error": {"message": "model not found: x"}}')
test("model not found -> model_not_found", cat == mod.ErrorCategory.MODEL_NOT_FOUND)

cat, _ = mod.classify_error('{"error": {"message": "unauthorized"}}')
test("unauthorized -> authentication", cat == mod.ErrorCategory.AUTH)

cat, _ = mod.classify_error('{"error": {"message": "bad request schema invalid"}}')
test("schema invalid -> protocol_incompatible", cat == mod.ErrorCategory.PROTOCOL_INCOMPATIBLE)

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
test("override 参数生效",
     mod._user_agent("claude-cli/9.9.9 (test override)") == "claude-cli/9.9.9 (test override)")
test("override=None 回到本机版本", mod._user_agent(None) == ua)
test("_claude_code_headers 含 User-Agent",
     "User-Agent" in mod._claude_code_headers())
test("_claude_code_headers(override) 用 override",
     mod._claude_code_headers("custom-ua/1.0")["User-Agent"] == "custom-ua/1.0")
test("懒加载 _claude_cli_version 返回非空字符串", bool(mod._claude_cli_version()))


print("\n[Unit] _http_request helper")
resp = mod._http_request("http://127.0.0.1:1/", "GET", None, None, 2, False)
test("连接层失败 status=0", resp.status == 0)
test("连接层失败 error_category 非空", resp.error_category is not None)
test("连接层失败 error_msg 非空", bool(resp.error_msg))


print("\n[Unit] _is_tls_error")
import ssl as _ssl
test("ssl.SSLError -> True",
     mod._is_tls_error(_ssl.SSLError("CERTIFICATE_VERIFY_FAILED")))
test("普通 URLError -> False",
     not mod._is_tls_error(urllib.error.URLError("Connection refused")))
test("Certificate text -> True",
     mod._is_tls_error(Exception("CERTIFICATE_VERIFY_FAILED: self signed")))
test("_error_category_for_urlerror TLS -> tls",
     mod._error_category_for_urlerror(Exception("SSL: CERTIFICATE_VERIFY_FAILED")) == "tls")
test("_error_category_for_urlerror network -> network",
     mod._error_category_for_urlerror(urllib.error.URLError("timeout")) == "network")


print("\n[Unit] _normalize_model_id")
test("去 [1M]",
     mod._normalize_model_id("claude-sonnet-4-5[1M]") == "claude-sonnet-4-5")
test("去日期后缀",
     mod._normalize_model_id("claude-sonnet-4-5-20251001") == "claude-sonnet-4-5")
test("去 -thinking",
     mod._normalize_model_id("claude-sonnet-4-5-thinking") == "claude-sonnet-4-5")
test("去 -fast",
     mod._normalize_model_id("claude-opus-4-6-fast") == "claude-opus-4-6")
test("小写化 + 去空白",
     mod._normalize_model_id(" Claude-Sonnet-4-5 ") == "claude-sonnet-4-5")


print("\n[Unit] compare_models")
test("exact_match",
     mod.compare_models("claude-sonnet-4-5", "claude-sonnet-4-5")["match"] == "exact_match")
test("alias_match (日期后缀)",
     mod.compare_models("claude-sonnet-4-5", "claude-sonnet-4-5-20251001")["match"] == "alias_match")
test("alias_match warning 非空",
     mod.compare_models("claude-sonnet-4-5", "claude-sonnet-4-5-20251001")["warning"] is not None)
test("fuzzy_match (含关系)",
     mod.compare_models("claude-sonnet-4-5", "proxy/claude-sonnet-4-5-custom")["match"] == "fuzzy_match")
test("mismatch",
     mod.compare_models("claude-opus-4-6", "claude-haiku-4-5")["match"] == "mismatch")
test("mismatch warning 含 '不一致'",
     "不一致" in mod.compare_models("claude-opus-4-6", "claude-haiku-4-5")["warning"])
test("unverifiable 空响应",
     mod.compare_models("claude-sonnet-4-5", None)["match"] == "unverifiable")
test("unverifiable 空字符串",
     mod.compare_models("claude-sonnet-4-5", "")["match"] == "unverifiable")
test("unverifiable 空 requested",
     mod.compare_models("", "claude-sonnet-4-5")["match"] == "unverifiable")


print("\n[Unit] build_probe_request 含 stream=True")
# 构造 fake provider
p = mod.Provider(name="X", app_type="claude", base_url="https://example.com/v1",
                 api_key="sk-fake", auth_mode="authtoken",
                 tiers=[mod.ModelTier("default", "claude-sonnet-4-5", "claude-sonnet-4-5")],
                 is_current=True, in_failover=True, is_openrouter=False)
url, method, headers, body = mod.build_probe_request(p, p.tiers[0], stream=True)
test("Anthropic stream 路径（路径不去重）",
     url == "https://example.com/v1/v1/messages" or url == "https://example.com/v1/messages")
test("Anthropic stream method", method == "POST")
parsed = json.loads(body)
test("Anthropic body.stream=True", parsed.get("stream") is True)
test("Anthropic body 含 model 字段", parsed.get("model") == "claude-sonnet-4-5")
test("Anthropic headers 有 anthropic-version", "anthropic-version" in headers)

# openclaw (chat completions)
p2 = mod.Provider(name="X", app_type="openclaw", base_url="https://example.com",
                  api_key="sk", auth_mode="bearer",
                  tiers=[mod.ModelTier("default", "gpt-5", "gpt-5")],
                  is_current=False, in_failover=False, is_openrouter=False)
url2, _, _, body2 = mod.build_probe_request(p2, p2.tiers[0], stream=True)
test("Chat Completions 路径", url2 == "https://example.com/chat/completions")
test("Chat Completions body.stream=True", json.loads(body2).get("stream") is True)

# codex
p3 = mod.Provider(name="X", app_type="codex", base_url="https://example.com",
                  api_key="sk", auth_mode="bearer",
                  tiers=[mod.ModelTier("default", "gpt-5", "gpt-5")],
                  is_current=False, in_failover=False, is_openrouter=False)
url3, _, _, body3 = mod.build_probe_request(p3, p3.tiers[0], stream=True)
test("Codex path", url3 == "https://example.com/responses")
test("Codex body.stream=True", json.loads(body3).get("stream") is True)


print("\n[Unit] build_probe_request disable_thinking 字段")
# claude: disable_thinking=True -> thinking:{type:disabled}
_, _, _, b_cl_off = mod.build_probe_request(p, p.tiers[0], disable_thinking=True)
test("claude 禁用 thinking -> thinking.type=disabled",
     json.loads(b_cl_off).get("thinking") == {"type": "disabled"})
_, _, _, b_cl_on = mod.build_probe_request(p, p.tiers[0], disable_thinking=False)
test("claude 允许 thinking -> 无 thinking 字段",
     "thinking" not in json.loads(b_cl_on))
# codex: disable_thinking=True -> reasoning.effort=minimal
_, _, _, b_cx_off = mod.build_probe_request(p3, p3.tiers[0], disable_thinking=True)
test("codex 禁用 thinking -> reasoning.effort=minimal",
     json.loads(b_cx_off).get("reasoning") == {"effort": "minimal"})
_, _, _, b_cx_on = mod.build_probe_request(p3, p3.tiers[0], disable_thinking=False)
test("codex 允许 thinking -> 无 reasoning 字段",
     "reasoning" not in json.loads(b_cx_on))
# openclaw: disable_thinking=True -> reasoning_effort=none
_, _, _, b_oc_off = mod.build_probe_request(p2, p2.tiers[0], disable_thinking=True)
test("openclaw 禁用 thinking -> reasoning_effort=none",
     json.loads(b_oc_off).get("reasoning_effort") == "none")
_, _, _, b_oc_on = mod.build_probe_request(p2, p2.tiers[0], disable_thinking=False)
test("openclaw 允许 thinking -> 无 reasoning_effort 字段",
     "reasoning_effort" not in json.loads(b_oc_on))
# max_tokens 透传
_, _, _, b_mt = mod.build_probe_request(p, p.tiers[0], max_tokens=1024)
test("max_tokens 透传到 body", json.loads(b_mt).get("max_tokens") == 1024)
# user_agent 覆盖透传到 headers
_, _, h_ua, _ = mod.build_probe_request(p, p.tiers[0],
                                        user_agent="claude-cli/9.9.9 (test)")
test("user_agent 覆盖进 headers",
     h_ua.get("User-Agent") == "claude-cli/9.9.9 (test)")


print("\n[Unit] _read_httperror_body 解码 4xx/5xx 响应体")
class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, body_bytes):
        self._body = body_bytes
        self.code = 400
    def read(self):
        return self._body

_fe = _FakeHTTPError('{"error":{"message":"bad request"}}'.encode("utf-8"))
_decoded, _raw = mod._read_httperror_body(_fe)
test("_read_httperror_body 解码正确",
     '"message":"bad request"' in _decoded)
test("_read_httperror_body 返回原始字节",
     _raw == '{"error":{"message":"bad request"}}'.encode("utf-8"))

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
def cb(e): events.append(e)
got_done, text = mod.parse_sse_lines([anthropic_events], cb, "anthropic_messages")
test("Anthropic SSE 解析到 message_stop", got_done)
test("Anthropic 文本拼接为 '5'", text == "5")
test("Anthropic 至少 1 个 text_delta 事件",
     any(e.get("kind") == "text_delta" for e in events))
model_events = [e for e in events if e.get("model")]
test("Anthropic 提取响应模型", any(e.get("model") == "claude-sonnet-4-5" for e in model_events))


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
    def log_message(self, *a, **k): pass

    def do_GET(self):
        # metadata: GET /v1/models/{id} — 返回声明窗口，避免默认 inspect 触发 512k 冒烟
        if "/v1/models/" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "claude-sonnet-4-5",
                "max_input_tokens": 200000,
                "max_output_tokens": 8192,
                "capabilities": {"thinking": {"supported": True}},
            }).encode())
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        # 接受 /v1/v1/messages、/v1/messages（路径不去重约定）
        if "/v1/messages" not in self.path:
            self.send_response(404); self.end_headers(); return
        # 读 body
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            j = json.loads(body) if body else {}
        except Exception:
            j = {}
        wants_stream = j.get("stream") is True
        has_tools = bool(j.get("tools"))
        has_image = False
        msgs = j.get("messages") or []
        if msgs and isinstance(msgs[0], dict):
            c = msgs[0].get("content")
            if isinstance(c, list):
                has_image = any(isinstance(x, dict) and x.get("type") == "image" for x in c)

        if not wants_stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if has_tools:
                resp = {
                    "id": "msg_tool", "type": "message", "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [{
                        "type": "tool_use", "id": "toolu_1",
                        "name": "get_probe_number", "input": {},
                    }],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 30, "output_tokens": 12},
                }
            elif has_image:
                resp = {
                    "id": "msg_vis", "type": "message", "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": "red"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 40, "output_tokens": 2},
                }
            else:
                resp = {
                    "id": "msg_mock", "type": "message", "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": "5"}],
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
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_x","model":"claude-sonnet-4-5"}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"5"}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        for e in events:
            self.wfile.write(e.encode())
            self.wfile.write(b"\n")
            self.wfile.flush()


class MockChatHandler(BaseHTTPRequestHandler):
    """OpenAI Chat Completions 风格 mock。"""
    def log_message(self, *a, **k): pass

    def do_GET(self):
        # openclaw inspect 默认也会拉 metadata；给个声明窗口避免 512k 冒烟
        if "/models" in self.path or "/v1/models" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "gpt-5", "max_input_tokens": 128000,
            }).encode())
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        # 接受 /v1/chat/completions、/chat/completions
        if "/chat/completions" not in self.path:
            self.send_response(404); self.end_headers(); return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            j = json.loads(body) if body else {}
        except Exception:
            j = {}
        wants_stream = j.get("stream") is True
        if not wants_stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if j.get("tools"):
                resp = {
                    "id": "c1", "object": "chat.completion", "model": "gpt-5",
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "call_1", "type": "function",
                                "function": {"name": "get_probe_number", "arguments": "{}"},
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            else:
                resp = {"id": "c1", "object": "chat.completion", "model": "gpt-5",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "5"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 8, "completion_tokens": 1}}
            self.wfile.write(json.dumps(resp).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in [
            'data: {"id":"c1","object":"chat.completion.chunk","model":"gpt-5","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n',
            'data: {"id":"c1","object":"chat.completion.chunk","model":"gpt-5","choices":[{"index":0,"delta":{"content":"5"}}]}\n\n',
            'data: [DONE]\n\n',
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
    cur.execute('''CREATE TABLE IF NOT EXISTS providers (
        name TEXT, app_type TEXT, settings_config TEXT,
        is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER
    )''')
    if app_type == "claude":
        cfg = json.dumps({
            "env": {
                "ANTHROPIC_BASE_URL": base_url,
                "ANTHROPIC_AUTH_TOKEN": "sk-mock",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
            }
        })
    elif app_type == "openclaw":
        cfg = json.dumps({"apiKey": "sk-mock", "baseUrl": base_url,
                          "models": [{"name": "gpt-5", "id": "gpt-5"}]})
    elif app_type == "codex":
        cfg = json.dumps({
            "auth": {"OPENAI_API_KEY": "sk-mock"},
            "config": f'base_url = "{base_url}"\nmodel = "gpt-5-codex"\n',
        })
    cur.execute("DELETE FROM providers")
    cur.execute("INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                ("Mock-Provider", app_type, cfg, 1, 1, 0))
    conn.commit()
    conn.close()


def write_multi_provider_db(base_url):
    """写入多个 claude 供应商，用于测试并发下的稳定排序与计数。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS providers (
        name TEXT, app_type TEXT, settings_config TEXT,
        is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER
    )''')
    cur.execute("DELETE FROM providers")
    for i, nm in enumerate(["Prov-C", "Prov-A", "Prov-B"]):
        cfg = json.dumps({"env": {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": "sk-mock",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
        }})
        cur.execute("INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (nm, "claude", cfg, 1, 1, i))
    conn.commit()
    conn.close()


def run_cli(args, timeout=10):
    cmd = [PY, SCRIPT] + args + ["--db", db_path, "--timeout", "3", "--workers", "1"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def run_cli_with_type(args, type_name, timeout=10):
    cmd = [PY, SCRIPT] + args + ["--db", db_path, "--timeout", "3", "--workers", "1",
          "--type", type_name]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


print("\n[End-to-end] Mock Anthropic SSE 完整探活")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5"])
    j = json.loads(out) if out else {}
    test("退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("JSON 含 streaming.status == pass",
         j.get("streaming", {}).get("status") == "pass")
    test("streaming.response_model == claude-sonnet-4-5",
         j.get("streaming", {}).get("response_model") == "claude-sonnet-4-5")
    test("streaming.is_sse == True",
         j.get("streaming", {}).get("is_sse") is True)
    test("streaming.event_count >= 3",
         j.get("streaming", {}).get("event_count", 0) >= 3)
    test("streaming.ttft_seconds is not None",
         j.get("streaming", {}).get("ttft_seconds") is not None)
    test("text.status == pass",
         j.get("text", {}).get("status") == "pass")
    test("text.answer == 5",
         j.get("text", {}).get("answer") == "5")
    test("model_consistency.match == exact_match",
         j.get("model_consistency", {}).get("match") == "exact_match")
    test("summary.verdict == healthy",
         j.get("summary", {}).get("verdict") == "healthy")
    test("protocol.confidence == confirmed",
         j.get("protocol", {}).get("confidence") == "confirmed")
finally:
    srv.shutdown()


print("\n[End-to-end] Mock OpenAI Chat Completions")
srv, port = start_server(MockChatHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}", "openclaw")
    rc, out, err = run_cli_with_type(["inspect", "--provider", "Mock-Provider",
                            "--model", "gpt-5", "--source", "manual"], "openclaw")
    j = json.loads(out) if out else {}
    test("退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("text.status == pass",
         j.get("text", {}).get("status") == "pass")
    test("protocol.detected == openai_chat_completions",
         j.get("protocol", {}).get("detected") == "openai_chat_completions")
    # 验证删除 _process 重复解析后，Chat Completions 流式仍能提取 response_model
    test("Chat Completions streaming.response_model == gpt-5",
         j.get("streaming", {}).get("response_model") == "gpt-5",
         f"response_model={j.get('streaming', {}).get('response_model')}")
    test("Chat Completions streaming.ttft_seconds 非空",
         j.get("streaming", {}).get("ttft_seconds") is not None)
finally:
    srv.shutdown()


print("\n[End-to-end] --with-metadata 供应商元数据")
class MockMetadataHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_GET(self):
        if "/v1/models/claude-sonnet-4-5" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "claude-sonnet-4-5",
                "max_input_tokens": 1000000,
                "max_output_tokens": 128000,
                "capabilities": {
                    "image_input": {"supported": True},
                    "thinking": {"supported": True},
                }
            }).encode())
        elif "/v1/messages" in self.path:
            # 兜底：inspect 也会发文本请求
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "msg", "type": "message", "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "5"}],
                "stop_reason": "end_turn"
            }).encode())
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        # 文本探测 / 流式
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            j = json.loads(body) if body else {}
        except Exception:
            j = {}
        wants_stream = j.get("stream") is True
        if not wants_stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "msg", "type": "message", "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "5"}],
                "stop_reason": "end_turn"
            }).encode())
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
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5",
                            "--with-metadata"])
    j = json.loads(out) if out else {}
    test("退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("metadata.status == available",
         j.get("metadata", {}).get("status") == "available")
    test("metadata.declared_context_window == 1000000",
         j.get("metadata", {}).get("declared_context_window") == 1000000)
    test("metadata.max_output_tokens == 128000",
         j.get("metadata", {}).get("max_output_tokens") == 128000)
    caps = j.get("metadata", {}).get("capabilities", {})
    test("metadata.capabilities.image_input == True",
         caps.get("image_input") is True)
    test("metadata.capabilities.thinking == True",
         caps.get("thinking") is True)
finally:
    srv.shutdown()


print("\n[End-to-end] --user-agent 命令行参数被服务端收到")
class UACaptureHandler(BaseHTTPRequestHandler):
    """记录请求头中的 User-Agent，响应 200。"""
    captured_ua: list[str] = []
    def log_message(self, *a, **k): pass
    def do_POST(self):
        UACaptureHandler.captured_ua.append(
            self.headers.get("User-Agent", ""))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "id": "msg", "type": "message", "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "5"}],
            "stop_reason": "end_turn"
        }).encode())
    def do_GET(self):
        # /v1/models/{id}
        UACaptureHandler.captured_ua.append(
            self.headers.get("User-Agent", ""))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

srv, port = start_server(UACaptureHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    # 自定义 UA
    UACaptureHandler.captured_ua = []
    rc, _, _ = run_cli(["inspect", "--provider", "Mock-Provider",
                        "--model", "claude-sonnet-4-5",
                        "--source", "manual", "--include", "text",
                        "--user-agent", "claude-cli/8.8.8 (test)"])
    test("--user-agent 自定义生效（text 路径）",
         any("claude-cli/8.8.8 (test)" in u
             for u in UACaptureHandler.captured_ua),
         f"captured={UACaptureHandler.captured_ua}")
    # 不传 --user-agent：使用本机探测的版本（懒加载）
    UACaptureHandler.captured_ua = []
    rc, _, _ = run_cli(["inspect", "--provider", "Mock-Provider",
                        "--model", "claude-sonnet-4-5",
                        "--source", "manual", "--include", "text"])
    ver = mod._claude_cli_version()
    test("默认 User-Agent 仍含本机版本号（动态读取）",
         any(f"claude-cli/{ver}" in u
             for u in UACaptureHandler.captured_ua),
         f"captured={UACaptureHandler.captured_ua} ver={ver}")
    # MEDIUM-1 回归：--user-agent 必须也透传到 metadata（GET /v1/models/{id}）路径
    UACaptureHandler.captured_ua = []
    rc, _, _ = run_cli(["inspect", "--provider", "Mock-Provider",
                        "--model", "claude-sonnet-4-5",
                        "--source", "manual", "--include", "text",
                        "--with-metadata",
                        "--user-agent", "claude-cli/7.7.7 (meta)"])
    # do_GET（metadata）收到的 UA 也应是自定义值
    got_via_get = [u for u in UACaptureHandler.captured_ua if "7.7.7" in u]
    test("--user-agent 透传到 metadata (GET) 路径",
         len(got_via_get) >= 1,
         f"captured={UACaptureHandler.captured_ua}")
finally:
    srv.shutdown()


print("\n[End-to-end] check --json + --user-agent stdout 仍为纯 JSON")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli([
        "check", "--json", "--user-agent", "claude-cli/9.9.9 (json-test)"
    ])
    parsed = None
    try:
        parsed = json.loads(out)
    except Exception:
        parsed = None
    test("check --json --user-agent stdout 可直接 json.loads",
         isinstance(parsed, dict), f"stdout={out[:160]!r} stderr={err[:160]!r}")
    test("User-Agent 提示在 stderr",
         "User-Agent 已覆盖" in err and "User-Agent 已覆盖" not in out,
         f"stdout={out[:120]!r} stderr={err[:160]!r}")
finally:
    srv.shutdown()


print("\n[End-to-end] --user-agent 透传到 list-models")
srv, port = start_server(UACaptureHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    UACaptureHandler.captured_ua = []
    rc, out, err = run_cli(["list-models", "--failover-only",
                            "--user-agent", "claude-cli/6.6.6 (lm)"])
    test("--user-agent 透传到 list-models",
         any("6.6.6" in u for u in UACaptureHandler.captured_ua),
         f"captured={UACaptureHandler.captured_ua}")
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
    test("check JSON schema_version == 1", j.get("schema_version") == 1)
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
    cmd = [PY, SCRIPT, "--db", db_path, "--failover-only", "--workers", "1", "--timeout", "3", "--json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    test("无子命令退出码 0", p.returncode == 0, f"rc={p.returncode} stderr={p.stderr[:200]}")
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
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "gpt-5", "--source", "manual", "--include", "text"])
    j = json.loads(out) if out else {}
    test("OpenRouter exit 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("OpenRouter text.status == pass", j.get("text", {}).get("status") == "pass")
    test("OpenRouter text.answer == 5", j.get("text", {}).get("answer") == "5")
finally:
    srv.shutdown()


print("\n[End-to-end] SSE 用 \\r\\r 分隔符（_take_event HIGH 修复验证）")
class MockCRLFSSEHandler(BaseHTTPRequestHandler):
    """用裸 \\r\\r 作为事件分隔符的 mock（罕见但规范外实现）。"""
    def log_message(self, *a, **k): pass
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
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5",
                            "--source", "manual", "--include", "streaming"])
    j = json.loads(out) if out else {}
    test("\\r\\r 分隔 exit 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("\\r\\r 分隔 streaming.status == pass",
         j.get("streaming", {}).get("status") == "pass",
         f"streaming={j.get('streaming')}")
    test("\\r\\r 分隔 streaming.text == 5",
         j.get("streaming", {}).get("text") == "5")
    test("\\r\\r 分隔 streaming.response_model 提取",
         j.get("streaming", {}).get("response_model") == "claude-sonnet-4-5")
finally:
    srv.shutdown()


print("\n[Unit] classify_error 优先 HTTP status（避免关键词误分类）")
# body 里含 "400" 业务文案，但真实 status 是 200 → 不应误判为 protocol_incompatible
cat, _ = mod.classify_error('{"error": {"message": "see error 400 in our docs"}}', http_status=200)
test("status=200 + body含400 -> 不误判为 protocol",
     cat != mod.ErrorCategory.PROTOCOL_INCOMPATIBLE)
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
                idx = k; seplen = len(sep); break
        if idx == -1:
            break
        eb = buf[:idx]; buf = buf[idx+seplen:]
        if eb.strip():
            mod._process_sse_event(eb, protocol, on_ev, dm_field, dm_val, [])
    return done[0], "".join(text_parts)

anthropic_raw = (
    b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-5"}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"5"}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
evs = []
gd1, txt1 = mod.parse_sse_lines([anthropic_raw], lambda e: evs.append(e), "anthropic_messages")
gd2, txt2 = _collect_via_process(anthropic_raw, "anthropic_messages")
test("双路径 got_done 一致", gd1 == gd2 == True)
test("双路径 text 一致", txt1 == txt2 == "5")


print("\n[Unit] parse_sse_lines 畸形流容错")
# 空事件夹在中间
messy = (
    b'event: message_start\ndata: {"type":"message_start","message":{"model":"m"}}\n\n'
    b'\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"5"}}\n\n'
    b': ping\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
evs = []
gd, txt = mod.parse_sse_lines([messy], lambda e: evs.append(e), "anthropic_messages")
test("空事件/注释行不破坏解析", gd is True and txt == "5")
# 完全没有有效事件 → 发 error 事件
evs = []
gd, txt = mod.parse_sse_lines([b"garbage no sep"], lambda e: evs.append(e), "anthropic_messages")
test("无有效事件 -> error 事件", any(e.get("kind") == "error" for e in evs))
test("无有效事件 -> 空文本", txt == "")


print("\n[End-to-end] inspect --include 子集短路")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    # 仅 text：streaming 应 not_run，model-consistency 应 not_run
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5", "--include", "text"])
    j = json.loads(out) if out else {}
    test("include=text exit 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("include=text streaming not_run",
         j.get("streaming", {}).get("status") == "not_run")
    test("include=text model_consistency not_run",
         j.get("model_consistency", {}).get("match") == "not_run")
    test("include=text text 有结果",
         j.get("text", {}).get("status") == "pass")
    # 仅 streaming：text 应为 None
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5", "--include", "streaming"])
    j = json.loads(out) if out else {}
    test("include=streaming text is None", j.get("text") is None)
    test("include=streaming streaming pass",
         j.get("streaming", {}).get("status") == "pass")
finally:
    srv.shutdown()


print("\n[End-to-end] inspect --keep-suffix 保留 [1M]")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    # manual + keep-suffix → model 字段应保留 [1M]
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5[1M]",
                            "--source", "manual", "--keep-suffix", "--include", "text"])
    j = json.loads(out) if out else {}
    test("keep-suffix model 含 [1M]",
         j.get("model") == "claude-sonnet-4-5[1M]", f"model={j.get('model')}")
    # 不带 keep-suffix → 去掉后缀
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5[1M]",
                            "--source", "manual", "--include", "text"])
    j = json.loads(out) if out else {}
    test("默认去后缀 model 不含 [1M]",
         j.get("model") == "claude-sonnet-4-5", f"model={j.get('model')}")
finally:
    srv.shutdown()


print("\n[End-to-end] inspect --source listed 成功路径")
class MockListedHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_GET(self):
        if "/v1/models" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-haiku-4-5"}]
            }).encode())
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "id": "msg", "type": "message", "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "5"}], "stop_reason": "end_turn"
        }).encode())

srv, port = start_server(MockListedHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5",
                            "--source", "listed", "--include", "text"])
    j = json.loads(out) if out else {}
    test("listed 成功 exit 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("listed model_source == listed", j.get("model_source") == "listed")
    test("listed text pass", j.get("text", {}).get("status") == "pass")
    # listed 但模型不在列表 → exit 2
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "nonexistent-model",
                            "--source", "listed", "--include", "text"])
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
    test("check --type codex 含 1 个 provider",
         len(j.get("providers", [])) == 1)
    test("check --type codex provider app_type=codex",
         j.get("providers", [{}])[0].get("type") == "codex")
finally:
    srv.shutdown()

srv, port = start_server(MockChatHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}", "openclaw")
    rc, out, err = run_cli_with_type(["check", "--json"], "openclaw")
    j = json.loads(out) if out else {}
    test("check --type openclaw JSON type=openclaw", j.get("type") == "openclaw")
    test("check --type openclaw provider app_type=openclaw",
         j.get("providers", [{}])[0].get("type") == "openclaw")
finally:
    srv.shutdown()


print("\n[End-to-end] check 多 provider 并发下 JSON 稳定排序")
srv, port = start_server(MockAnthropicHandler)
try:
    write_multi_provider_db(f"http://127.0.0.1:{port}/v1")
    # workers=3 并发；结果顺序应按 name 稳定
    cmd = [PY, SCRIPT, "check", "--json", "--db", db_path,
           "--timeout", "3", "--workers", "3", "--type", "claude"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    j = json.loads(p.stdout) if p.stdout else {}
    names = [x.get("name") for x in j.get("providers", [])]
    test("多 provider 计数 == 3", len(names) == 3, f"names={names}")
    # 稳定排序 = 保留 cc-switch 的 sort_index 顺序（0=Prov-C,1=Prov-A,2=Prov-B），
    # 而非字母序；关键是并发下顺序确定、可复现
    test("多 provider 结果按 sort_index 稳定",
         names == ["Prov-C", "Prov-A", "Prov-B"], f"names={names}")
    # 再跑一次，验证并发下顺序可复现（不 flaky）
    p2 = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    j2 = json.loads(p2.stdout) if p2.stdout else {}
    names2 = [x.get("name") for x in j2.get("providers", [])]
    test("多 provider 顺序可复现", names == names2, f"1st={names} 2nd={names2}")
    test("多 provider summary.total == 3",
         j.get("summary", {}).get("total") == 3)
finally:
    srv.shutdown()


print("\n[Unit] extract_usage 解析 Anthropic / OpenAI usage")
u1 = mod.extract_usage(json.dumps({
    "usage": {"input_tokens": 20, "output_tokens": 3}
}))
test("Anthropic usage.present True", u1.get("present") is True, f"u1={u1}")
test("Anthropic input_tokens==20", u1.get("input_tokens") == 20)
test("Anthropic output_tokens==3", u1.get("output_tokens") == 3)
u2 = mod.extract_usage(json.dumps({
    "usage": {"prompt_tokens": 11, "completion_tokens": 2}
}))
test("OpenAI prompt→input", u2.get("input_tokens") == 11)
test("OpenAI completion→output", u2.get("output_tokens") == 2)
u3 = mod.extract_usage("{}")
test("无 usage → present False", u3.get("present") is False)


print("\n[Unit] usage.present 在 text 探测解析到 token 时为 True")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5", "--include", "text"])
    j = json.loads(out) if out else {}
    usage = j.get("usage", {})
    test("usage.present True（mock 返回 input/output tokens）",
         usage.get("present") is True, f"usage={usage}")
    test("usage.input_tokens == 20", usage.get("input_tokens") == 20, f"usage={usage}")
    test("usage.output_tokens == 3", usage.get("output_tokens") == 3, f"usage={usage}")
finally:
    srv.shutdown()


print("\n[End-to-end] 默认 include：metadata + thinking + tools；vision 默认跳过")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5"])
    j = json.loads(out) if out else {}
    test("默认 inspect 退出码 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    test("默认 metadata.status == available",
         j.get("metadata", {}).get("status") == "available")
    test("有声明窗口时 context 不冒烟（skipped）",
         j.get("context", {}).get("status") == "skipped",
         f"context={j.get('context')}")
    test("thinking.verdict 非空",
         bool(j.get("thinking", {}).get("verdict")),
         f"thinking={j.get('thinking')}")
    test("tools.protocol_support == native",
         j.get("tools", {}).get("protocol_support") == "native",
         f"tools={j.get('tools')}")
    test("vision 默认 skipped",
         j.get("vision", {}).get("status") == "skipped",
         f"vision={j.get('vision')}")
finally:
    srv.shutdown()


print("\n[End-to-end] vision 显式 include")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli([
        "inspect", "--provider", "Mock-Provider",
        "--model", "claude-sonnet-4-5",
        "--include", "vision",
    ])
    j = json.loads(out) if out else {}
    test("vision.status == pass",
         j.get("vision", {}).get("status") == "pass",
         f"vision={j.get('vision')} stderr={err[:150]}")
finally:
    srv.shutdown()


print("\n[Unit] probe on_attempt 档位级进度回调")
# 不依赖网络：用假 probe_tier 替换验证回调顺序
orig_probe_tier = mod.probe_tier
events = []

def fake_probe_tier(p, tier, timeout, skip_tls_verify, max_tokens=20,
                    disable_thinking=True, user_agent=None):
    # 第 1 档失败，第 2 档成功
    if tier.tier == "haiku":
        return {"tier": "haiku", "model": tier.model, "status": 429,
                "elapsed": 0.1, "error": "rate", "answer": "", "correct": False}
    return {"tier": tier.tier, "model": tier.model, "status": 200,
            "elapsed": 0.2, "error": "", "answer": "5", "correct": True}

mod.probe_tier = fake_probe_tier
try:
    p = mod.Provider(
        name="P", app_type="claude", base_url="http://x",
        api_key="k", auth_mode="authtoken",
        tiers=[
            mod.ModelTier("haiku", "h", "h"),
            mod.ModelTier("sonnet", "s", "s"),
            mod.ModelTier("opus", "o", "o"),  # 不应触达
        ],
    )
    r = mod.probe(p, 3, False, on_attempt=lambda prov, att: events.append(
        (prov.name, att["tier"], att["status"])))
    test("on_attempt 调用 2 次（首成功即停）", len(events) == 2, f"events={events}")
    test("on_attempt 顺序 haiku→sonnet",
         [e[1] for e in events] == ["haiku", "sonnet"], f"events={events}")
    test("probe overall_ok True", r["overall_ok"] is True)
    test("probe best_tier sonnet", r["best_tier"] == "sonnet")
    test("probe 未探测 opus", len(r["attempts"]) == 2)
finally:
    mod.probe_tier = orig_probe_tier


print("\n[Unit] say() 默认 flush + ANSI 清理")
import io
buf = io.StringIO()
old = mod._human_out
mod._human_out = buf
try:
    mod.say("progress-line")
    test("say 写入内容", "progress-line" in buf.getvalue())
    buf.truncate(0); buf.seek(0)
    mod.say("evil\x1b[2Jtext\x1b]0;pwned\x07end")
    val = buf.getvalue()
    test("say 剥离 ANSI 转义", "\x1b" not in val, f"val={val!r}")
    test("say 保留正常文本", "eviltext" in val and "end" in val, f"val={val!r}")
finally:
    mod._human_out = old


print("\n[Unit] extract_usage 缺字段/空 → missing_fields")
u_partial = mod.extract_usage(json.dumps({"usage": {"input_tokens": 10}}))
test("partial usage present True", u_partial.get("present") is True)
test("partial output_tokens None", u_partial.get("output_tokens") is None)
test("partial missing_fields 含 output_tokens", "output_tokens" in u_partial.get("missing_fields", []))


print("\n[Unit] probe_context_smoke rejected 路径")
class RejectCtxHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length) if length else b""
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "error": {"message": "prompt is too long: context length exceeded"}
        }).encode())

srv, port = start_server(RejectCtxHandler)
try:
    p = mod.Provider(
        name="R", app_type="claude",
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="sk-x", auth_mode="authtoken",
        tiers=[mod.ModelTier("default", "m", "m")],
    )
    r = mod.probe_context_smoke(p, "m", 64, timeout=5, skip_tls_verify=False)
    test("context smoke rejected", r.get("status") == "rejected", f"r={r}")
finally:
    srv.shutdown()


print("\n[Unit] _probe_tools rejected 路径（400 含 tool 关键词）")
class RejectToolsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
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
        self.wfile.write(json.dumps({
            "error": {"message": "unknown field: tools is not supported"}
        }).encode())

srv, port = start_server(RejectToolsHandler)
try:
    p = mod.Provider(
        name="RT", app_type="claude",
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="sk-x", auth_mode="authtoken",
        tiers=[mod.ModelTier("default", "m", "m")],
    )
    r = mod._probe_tools(p, "m", 5, False)
    test("tools rejected status", r.get("status") == "fail", f"r={r}")
    test("tools rejected protocol_support", r.get("protocol_support") == "rejected", f"r={r}")
finally:
    srv.shutdown()


print("\n[Unit] _probe_tools text_only 路径（200 但无 tool_use block）")
class TextOnlyToolsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
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
        self.wfile.write(json.dumps({
            "id": "msg", "type": "message", "model": "m",
            "content": [{"type": "text", "text": "5"}],
            "stop_reason": "end_turn",
        }).encode())

srv, port = start_server(TextOnlyToolsHandler)
try:
    p = mod.Provider(
        name="TT", app_type="claude",
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="sk-x", auth_mode="authtoken",
        tiers=[mod.ModelTier("default", "m", "m")],
    )
    r = mod._probe_tools(p, "m", 5, False)
    test("tools text_only status", r.get("status") == "fail", f"r={r}")
    test("tools text_only protocol_support", r.get("protocol_support") == "text_only", f"r={r}")
finally:
    srv.shutdown()


print("\n[Unit] thinking dependency_missing（--include thinking 无 text）")
srv, port = start_server(MockAnthropicHandler)
try:
    write_fake_db(f"http://127.0.0.1:{port}/v1", "claude")
    rc, out, err = run_cli(["inspect", "--provider", "Mock-Provider",
                            "--model", "claude-sonnet-4-5",
                            "--include", "thinking"])
    j = json.loads(out) if out else {}
    th = j.get("thinking", {})
    test("thinking dependency_missing",
         th.get("status") == "dependency_missing",
         f"thinking={th}")
finally:
    srv.shutdown()


print("\n[Unit] _probe_vision unsupported（codex app_type）")
p_codex = mod.Provider(
    name="C", app_type="codex", base_url="http://x",
    api_key="k", auth_mode="bearer",
    tiers=[mod.ModelTier("default", "m", "m")],
)
r = mod._probe_vision(p_codex, "m", 5, False)
test("vision codex unsupported", r.get("status") == "unsupported", f"r={r}")


# 清理
import shutil
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

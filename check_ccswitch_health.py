#!/usr/bin/env python3
"""
CC-Pulse — cc-switch 供应商健康检测脚本（独立运行，不改 cc-switch 任何东西）

直接只读 cc-switch 的 SQLite 数据库，对每个供应商的上游 endpoint 发一次
真实问题探测请求，收集状态码、完整错误信息和实际回答内容。

特点:
  - 不依赖 cc-switch 运行状态（只读它的数据库）
  - 不依赖 CLIProxyAPI（直接打上游）
  - 批量并发 + 完整错误信息透传
  - 认证头按 cc-switch 配置走（AUTH_TOKEN→Bearer，API_KEY→x-api-key）
  - 路径拼接不去重（和真实 Claude Code 一致，muyuan.do/v1 → /v1/v1/messages）
  - 模型多档回退：haiku→sonnet→opus→fable→default，每档结果都报告
  - 探测真实问题：发 "2+3=?" 校验能否正确回答，而非只测连通

用法:
    python check_ccswitch_health.py
    python check_ccswitch_health.py --failover-only --workers 8
    python check_ccswitch_health.py --type claude --timeout 60
"""

import json
import sqlite3
import re
import argparse
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field

DB_PATH = str(Path.home() / ".cc-switch" / "cc-switch.db")

# 默认兜底的 claude-cli 版本（读取本机版本失败时用）
_DEFAULT_CLAUDE_CLI_VERSION = "2.1.44"

# 本机 claude-cli 版本缓存（懒加载，首次 _user_agent() 调用时探测）
_CLAUDE_CLI_VERSION_CACHE: str | None = None
_CLAUDE_VERSION_LOCK = threading.Lock()


def _detect_claude_cli_version() -> str:
    """读取本机 `claude --version`，让 User-Agent 跟随真实版本。

    muyuan.do 等中转站会校验 claude-cli 版本，写死旧版本会被拒（403）。
    失败时回退 _DEFAULT_CLAUDE_CLI_VERSION。
    """
    try:
        r = subprocess.run(["claude", "--version"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            m = re.search(r"(\d+\.\d+\.\d+)", r.stdout)
            if m:
                return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return _DEFAULT_CLAUDE_CLI_VERSION


def _claude_cli_version() -> str:
    """懒加载本机 claude-cli 版本（线程安全，首次调用时探测一次）。"""
    global _CLAUDE_CLI_VERSION_CACHE
    if _CLAUDE_CLI_VERSION_CACHE is None:
        with _CLAUDE_VERSION_LOCK:
            if _CLAUDE_CLI_VERSION_CACHE is None:
                _CLAUDE_CLI_VERSION_CACHE = _detect_claude_cli_version()
    return _CLAUDE_CLI_VERSION_CACHE


def _user_agent(override: str | None = None) -> str:
    """当前生效的 User-Agent：override 参数 > 本机版本 > 兜底。

    不再依赖模块级可变全局状态，override 由调用方（build_probe_request）透传。
    """
    if override:
        return override
    return f"claude-cli/{_claude_cli_version()} (external, sdk-cli)"


def _claude_code_headers(user_agent: str | None = None) -> dict:
    """构造 Claude Code 指纹头（User-Agent 动态）。"""
    return {
        "User-Agent": _user_agent(user_agent),
        "x-app": "cli",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
        "x-stainless-lang": "js",
        "x-stainless-package-version": "0.74.0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v24.3.0",
        "x-stainless-arch": "x64",
        "x-stainless-os": "Windows",
    }

# 探测用的真实问题（验证模型能否真正回答，而非只测连通）
PROBE_QUESTION = "What is 2+3? Reply with only the number, nothing else."
PROBE_MAX_TOKENS = 20
EXPECTED_ANSWER = "5"

# 模型档位回退顺序（与用户指定一致：haiku→sonnet→opus→fable，default 兜底）
TIER_ORDER = ["haiku", "sonnet", "opus", "fable", "default"]
# cc-switch env 变量名 → 档位名
TIER_ENV_KEYS = {
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "default": "ANTHROPIC_MODEL",
}


@dataclass
class ModelTier:
    tier: str          # haiku/sonnet/opus/fable/default
    model: str         # 干净模型名（已去 [1M]）
    raw_model: str     # 原始模型名


@dataclass
class Provider:
    name: str
    app_type: str
    base_url: str
    api_key: str
    auth_mode: str          # "authtoken"(Bearer) / "apikey"(x-api-key) / "bearer"(codex/openclaw)
    tiers: list = field(default_factory=list)   # List[ModelTier]
    is_current: bool = False
    in_failover: bool = False
    is_openrouter: bool = False   # base_url 含 /chat/completions，走 OpenAI 格式


def load_providers(db_path: str, app_type: str) -> list:
    """只读连接 cc-switch.db，加载供应商"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT name, app_type, settings_config, is_current, in_failover_queue "
            "FROM providers WHERE app_type=? ORDER BY sort_index",
            (app_type,),
        )
        providers = []
        for row in cur.fetchall():
            try:
                cfg = json.loads(row["settings_config"])
                for p in parse_provider(row["name"], row["app_type"], cfg,
                                        bool(row["is_current"]), bool(row["in_failover_queue"])):
                    providers.append(p)
            except Exception as e:
                say(f"  跳过 [{row['name']}]: {e}")
        return providers
    finally:
        conn.close()


def parse_provider(name, app_type, cfg, is_current, in_failover) -> list:
    """解析单个供应商的 settings_config，返回 Provider 列表"""
    out = []
    if app_type == "claude":
        env = cfg.get("env", {})
        base = env.get("ANTHROPIC_BASE_URL", "")
        # 认证头按配置走：优先 AUTH_TOKEN（中转站），其次 API_KEY（官方）
        token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        auth_mode = "authtoken"
        if not token:
            token = env.get("ANTHROPIC_API_KEY", "")
            auth_mode = "apikey"
        if not token or not base:
            return out
        # 收集所有档位模型（不去重，保留配置顺序）
        tiers = []
        for tier in TIER_ORDER:
            v = env.get(TIER_ENV_KEYS[tier], "")
            if v:
                clean = re.sub(r"\[.*?\]$", "", v)   # 去 [1M] 后缀
                tiers.append(ModelTier(tier, clean, v))
        if not tiers:
            return out
        is_or = "/chat/completions" in base
        out.append(Provider(name, "claude", base, token, auth_mode, tiers,
                            is_current, in_failover, is_or))
    elif app_type == "codex":
        auth = cfg.get("auth", {})
        token = auth.get("OPENAI_API_KEY", "")
        config_str = cfg.get("config", "")
        m = re.search(r'base_url\s*=\s*"([^"]+)"', config_str)
        base = m.group(1) if m else ""
        mm = re.search(r'^\s*model\s*=\s*"([^"]+)"', config_str, re.MULTILINE)
        model = mm.group(1) if mm else "gpt-5"
        if token and base:
            out.append(Provider(name, "codex", base, token, "bearer",
                                [ModelTier("default", model, model)],
                                is_current, in_failover))
    elif app_type == "openclaw":
        token = cfg.get("apiKey", "")
        base = cfg.get("baseUrl", "")
        tiers = [ModelTier(m.get("name", "default"), m["id"], m["id"])
                 for m in cfg.get("models", []) if isinstance(m, dict) and m.get("id")]
        if token and base and tiers:
            out.append(Provider(name, "openclaw", base, token, "bearer", tiers,
                               is_current, in_failover))
    return out


def build_auth_headers(p: Provider) -> dict:
    """按 auth_mode 只发一个认证头（和真实 Claude Code 一致）"""
    if p.auth_mode == "apikey":
        return {"x-api-key": p.api_key}
    else:   # authtoken / bearer
        return {"Authorization": f"Bearer {p.api_key}"}


def build_probe_request(p: Provider, tier: ModelTier, stream: bool = False,
                       max_tokens: int = PROBE_MAX_TOKENS,
                       disable_thinking: bool = True,
                       user_agent: str | None = None) -> tuple:
    """构造 (url, method, headers, body)，发真实问题，路径不去重。

    stream=True 时为协议体加 stream 字段（Anthropic/OpenAI 兼容）。
    disable_thinking=True（默认）请求体加 "thinking": {"type": "disabled"}，
    避免 DeepSeek 等中转/模型走 thinking 模式耗光 max_tokens 而无最终答案。
    max_tokens 允许调高预算（默认 PROBE_MAX_TOKENS）。
    """
    auth_h = build_auth_headers(p)

    if p.app_type == "claude":
        if p.is_openrouter:
            url = p.base_url
            payload = {
                "model": tier.raw_model or tier.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": PROBE_QUESTION}],
            }
            if stream:
                payload["stream"] = True
            if disable_thinking:
                payload["thinking"] = {"type": "disabled"}
            body = json.dumps(payload).encode()
            return url, "POST", {**auth_h, "Content-Type": "application/json"}, body
        # 路径不去重：一律 base + /v1/messages（muyuan.do/v1 → /v1/v1/messages）
        url = p.base_url.rstrip("/") + "/v1/messages"
        payload = {
            "model": tier.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": PROBE_QUESTION}],
        }
        if stream:
            payload["stream"] = True
        if disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        body = json.dumps(payload).encode()
        headers = {**_claude_code_headers(user_agent), **auth_h, "Content-Type": "application/json"}
        return url, "POST", headers, body

    if p.app_type == "codex":
        # 路径不去重：base + /responses
        url = p.base_url.rstrip("/") + "/responses"
        payload = {
            "model": tier.model,
            "input": PROBE_QUESTION,
            "max_output_tokens": max_tokens,
        }
        if stream:
            payload["stream"] = True
        if disable_thinking:
            # Responses API 用 reasoning.effort="minimal" 最大限度抑制思考
            # （取值 minimal/low/medium/high；无 disabled 档）
            payload["reasoning"] = {"effort": "minimal"}
        body = json.dumps(payload).encode()
        headers = {"User-Agent": _user_agent(user_agent),
                   **auth_h, "Content-Type": "application/json"}
        return url, "POST", headers, body

    if p.app_type == "openclaw":
        url = p.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": tier.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": PROBE_QUESTION}],
        }
        if stream:
            payload["stream"] = True
        if disable_thinking:
            # OpenAI 兼容站常用 reasoning_effort 抑制思考（DeepSeek-R1 等）
            payload["reasoning_effort"] = "none"
        body = json.dumps(payload).encode()
        headers = {"User-Agent": _user_agent(user_agent),
                   **auth_h, "Content-Type": "application/json"}
        return url, "POST", headers, body

    return "", "GET", {}, b""


def extract_answer(p: Provider, resp_body: str) -> str:
    """从响应里提取模型的实际回答文本。

    OpenRouter（claude + is_openrouter）请求走 OpenAI chat/completions，
    响应是 choices[].message.content，不能按 Anthropic content[] 解析。
    """
    try:
        j = json.loads(resp_body)
    except (json.JSONDecodeError, TypeError):
        return ""

    # OpenAI chat/completions 格式：openrouter / openclaw / 部分 codex 兼容层
    if p.is_openrouter or p.app_type in ("openclaw",):
        for ch in j.get("choices", []):
            if not isinstance(ch, dict):
                continue
            msg = ch.get("message") or {}
            if isinstance(msg, dict) and msg.get("content"):
                return str(msg["content"]).strip()
            # 少数兼容层把 content 放在 choice 顶层
            if ch.get("content"):
                return str(ch["content"]).strip()
        return ""

    if p.app_type == "claude":
        # Anthropic: {"content": [{"type":"text","text":"5"}]}
        parts = []
        for blk in j.get("content", []):
            if isinstance(blk, dict) and blk.get("text"):
                parts.append(blk["text"])
        return "".join(parts).strip()

    if p.app_type == "codex":
        # chat/completions 兼容
        for ch in j.get("choices", []):
            if not isinstance(ch, dict):
                continue
            msg = ch.get("message") or {}
            if isinstance(msg, dict) and msg.get("content"):
                return str(msg["content"]).strip()
        # Responses API: {"output":[{"content":[{"text":"5"}]}]}
        for o in j.get("output", []):
            if not isinstance(o, dict):
                continue
            for c in o.get("content", []):
                if isinstance(c, dict) and c.get("text"):
                    return c["text"].strip()
    return ""


def extract_usage(resp_body: str) -> dict:
    """从响应 JSON 提取 usage 字段（诚实：解析不到则 present=False）。

    兼容：
      - Anthropic: usage.input_tokens / output_tokens
      - OpenAI chat: usage.prompt_tokens / completion_tokens
      - Responses: usage.input_tokens / output_tokens
    """
    empty = {
        "present": False,
        "input_tokens": None,
        "output_tokens": None,
        "source": None,
        "missing_fields": ["input_tokens", "output_tokens"],
    }
    try:
        j = json.loads(resp_body)
    except (json.JSONDecodeError, TypeError):
        return empty
    if not isinstance(j, dict):
        return empty
    usage = j.get("usage")
    if not isinstance(usage, dict):
        return empty
    inp = usage.get("input_tokens")
    if inp is None:
        inp = usage.get("prompt_tokens")
    out = usage.get("output_tokens")
    if out is None:
        out = usage.get("completion_tokens")
    missing = []
    if inp is None:
        missing.append("input_tokens")
    if out is None:
        missing.append("output_tokens")
    present = inp is not None or out is not None
    return {
        "present": present,
        "input_tokens": int(inp) if isinstance(inp, (int, float)) else None,
        "output_tokens": int(out) if isinstance(out, (int, float)) else None,
        "source": "response_body" if present else None,
        "missing_fields": missing,
    }


def _response_has_thinking_signal(resp_body: str) -> bool:
    """响应体是否出现 thinking/reasoning 相关信号（字段或 content block）。"""
    if not resp_body:
        return False
    low = resp_body.lower()
    if any(k in low for k in (
        '"type":"thinking"', '"thinking"', "reasoning_content",
        '"reasoning"', "reasoning_effort",
    )):
        return True
    try:
        j = json.loads(resp_body)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(j, dict):
        return False
    for blk in j.get("content", []) or []:
        if isinstance(blk, dict) and blk.get("type") in ("thinking", "reasoning"):
            return True
    msg = (j.get("choices") or [{}])
    if msg and isinstance(msg[0], dict):
        m = msg[0].get("message") or {}
        if isinstance(m, dict) and (m.get("reasoning_content") or m.get("reasoning")):
            return True
    return False


class ErrorCategory(str, Enum):
    """统一的错误分类枚举。用于 JSON 报告和 inspect 子命令。"""
    NONE = "none"
    NETWORK = "network"                # DNS / 连接拒绝 / 超时
    TLS = "tls"                        # 证书 / 主机名
    AUTH = "authentication"            # 401 / 403
    RATE_LIMIT = "rate_limit"          # 429
    MODEL_NOT_FOUND = "model_not_found"  # 404 / invalid model
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"  # 400 schema
    SERVER = "server_error"            # 5xx
    INVALID_RESPONSE = "invalid_response"  # 200 但无法解析
    ANSWER_MISMATCH = "answer_mismatch"     # 200 但答案不对
    # 流式相关
    STREAM_PROTOCOL = "stream_protocol"      # 非 SSE / 格式异常
    TTFT_TIMEOUT = "ttft_timeout"            # 首 token 超时
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
        msg = e.get("message", "") or e.get("type", "") or json.dumps(e, ensure_ascii=False)
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
        return status_cat, msg

    # 无 status（如流式解析后或 status=200 异常体）：回退到关键词推断
    low = msg.lower() if isinstance(msg, str) else ""
    if any(k in low for k in ("rate limit", "rate_limit", "too many requests", "quota")):
        return ErrorCategory.RATE_LIMIT, msg
    if any(k in low for k in ("not found", "model_not_found", "model does not exist", "unknown model")):
        return ErrorCategory.MODEL_NOT_FOUND, msg
    if any(k in low for k in ("unauthorized", "invalid api key", "authentication",
                              "permission", "forbidden")):
        return ErrorCategory.AUTH, msg
    if any(k in low for k in ("invalid request", "bad request", "schema")):
        return ErrorCategory.PROTOCOL_INCOMPATIBLE, msg
    if any(k in low for k in ("internal", "server error", "overloaded")):
        return ErrorCategory.SERVER, msg
    return ErrorCategory.UNKNOWN, msg


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


def _read_httperror_body(e: urllib.error.HTTPError) -> tuple[str, bytes]:
    """安全读取 HTTPError 的响应体，返回 (decoded_body, raw_bytes)。

    供 _http_request 与 probe_stream 复用，避免重复 try/except e.read()。
    """
    try:
        raw = e.read()
    except Exception:
        raw = b""
    return raw.decode("utf-8", errors="replace"), raw


def _http_request(url: str, method: str = "GET", headers: dict | None = None,
                  body: bytes | None = None, timeout: int = 30,
                  skip_tls_verify: bool = False) -> HttpResponse:
    """统一非流式 HTTP 请求，归一化 HTTPError/URLError/TLS/超时。

    消除 probe_tier / fetch_models / probe_model_metadata 里重复的 urlopen 样板。
    流式探测（probe_stream）因需要逐块读取 resp，不适用本函数。
    """
    ctx = create_ssl_context(skip_tls_verify)
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            return HttpResponse(
                status=resp.status,
                body=raw.decode("utf-8", errors="replace"),
                content_type=resp.headers.get("Content-Type", ""),
                error_category=None,
                error_msg="",
            )
    except urllib.error.HTTPError as e:
        # HTTPError 有 status code，不算连接层错误；body 交给调用方 classify
        body, _raw = _read_httperror_body(e)
        return HttpResponse(
            status=e.code,
            body=body,
            content_type=e.headers.get("Content-Type", "") if e.headers else "",
            error_category=None,
            error_msg="",
        )
    except urllib.error.URLError as e:
        return HttpResponse(0, "", "", _error_category_for_urlerror(e),
                            f"连接失败: {e.reason}")
    except Exception as e:
        cat = _error_category_for_urlerror(e) if _is_tls_error(e) else ErrorCategory.UNKNOWN.value
        return HttpResponse(0, "", "", cat, f"异常: {type(e).__name__}: {e}")


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
        if any(k in text for k in (
            "CERTIFICATE", "SSL:", "TLSV1", "CERTIFICATE_VERIFY_FAILED",
            "HOSTNAME MISMATCH", "CERTIFICATE VERIFY FAILED",
        )):
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
    "anthropic_messages": ('event', 'message_stop'),
    "openai_chat_completions": ('data', '[DONE]'),
    "openai_responses": ('event', 'response.completed'),
    "openai_chat_openrouter": ('data', '[DONE]'),
}


class StreamEvent(dict):
    """统一流式事件结构：
       - kind: message_start | text_delta | content_block | message_stop
               | done | error | first_chunk
       - model: 该事件携带的响应模型（若有）
       - text_delta: 仅 text_delta 有效
       - raw: 原始事件文本/字典
    """
    pass


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
        protocol, ("event", "message_stop"))
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
                return buf[:idx], buf[idx + len(sep):]
        # 双 \r 视为空行分隔（少数实现）
        idx = buf.find(b"\r\r")
        if idx != -1:
            return buf[:idx], buf[idx + 2:]
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
                    event_bytes.decode("utf-8", errors="replace"))
                on_event(StreamEvent(kind="first_chunk", raw=evt_dict or {}))

            _process_sse_event(event_bytes, protocol, _inner_on_event,
                               done_marker_field, done_marker_value, text_buf)
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


def _process_sse_event(event_bytes, proto_name, on_event,
                       done_marker_field, done_marker_value, text_buf):
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
            val = line[len(done_marker_field) + 1:].strip()
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
                on_event(StreamEvent(kind="text_delta", text_delta=delta["text"], raw=j))
        elif j.get("type") == "message_start":
            msg = j.get("message", {})
            on_event(StreamEvent(kind="message_start", model=msg.get("model"), raw=j))
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
                on_event(StreamEvent(kind="text_delta", text_delta=chunk_text, model=j.get("model"), raw=j))
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


def _drain_non_sse_stream(resp, p: Provider) -> dict:
    """消费非 SSE 响应（供应商对 stream=true 仍返回普通 JSON）。

    返回 {text, response_model, raw_preview}。
    """
    raw = resp.read()
    text = ""
    response_model = None
    content_type = resp.headers.get("Content-Type", "")
    if content_type.startswith("application/json") and raw:
        try:
            j = json.loads(raw.decode("utf-8", errors="replace"))
            text = extract_answer(p, json.dumps(j, ensure_ascii=False))
            for path in [("model",), ("message", "model"), ("response", "model")]:
                cur = j
                for k in path:
                    if isinstance(cur, dict) and k in cur:
                        cur = cur[k]
                    else:
                        cur = None
                        break
                if cur:
                    response_model = cur
                    break
        except (json.JSONDecodeError, TypeError):
            pass
    return {"text": text, "response_model": response_model,
            "raw_preview": bytes(raw)[:200]}


def _drain_sse_stream(resp, p: Provider, proto_name: str,
                      ttft_deadline: float | None, start: float) -> dict:
    """读取 SSE 流并聚合结果。

    返回 {first_event_at, event_count, response_model, text, got_done,
          raw_preview}。
    TTFT 超时时抛 TimeoutError（由调用方捕获）。
    """
    first_event_at = None
    event_count = 0
    response_model = None
    response_text_buf: list[str] = []
    got_done = False
    sse_done = False
    raw_buf = bytearray()

    done_marker_field, done_marker_value = STREAM_DONE_MARKERS.get(
        proto_name, ("event", "message_stop"))

    def _on_event(ev: StreamEvent):
        nonlocal first_event_at, event_count, response_model, got_done
        if first_event_at is None and ev.get("kind") in ("first_chunk", "message_start", "text_delta"):
            first_event_at = time.time()
        if ev.get("kind") == "first_chunk":
            event_count += 1
            return
        event_count += 1
        if ev.get("model"):
            response_model = ev["model"]
        if ev.get("kind") == "text_delta" and ev.get("text_delta"):
            response_text_buf.append(ev["text_delta"])
        if ev.get("kind") == "done":
            got_done = True

    def _process(event_bytes: bytes):
        nonlocal sse_done
        # _process_sse_event 通过 _on_event 统一更新 first_event_at /
        # response_model / event_count / response_text_buf / got_done，
        # 这里不再重复解析（避免双实现分叉）。
        _process_sse_event(event_bytes, proto_name, _on_event,
                           done_marker_field, done_marker_value, response_text_buf)
        if got_done:
            sse_done = True

    def _take_event(buf: bytes) -> tuple:
        # 与 parse_sse_lines._try_take_event 保持一致的分隔符支持
        for sep in (b"\r\n\r\n", b"\n\n", b"\r\r"):
            idx = buf.find(sep)
            if idx != -1:
                return buf[:idx], buf[idx + len(sep):]
        return None, buf

    sse_buffer = b""
    for line in resp:
        if first_event_at is None and ttft_deadline is not None:
            if time.time() - start > ttft_deadline:
                raise TimeoutError("ttft_timeout")
        if isinstance(line, bytes):
            sse_buffer += line
        else:
            sse_buffer += line.encode("utf-8", errors="replace")
        if len(sse_buffer) > 65536:
            sse_buffer = sse_buffer[-65536:]

        while True:
            event_bytes, sse_buffer = _take_event(sse_buffer)
            if event_bytes is None:
                break
            if not event_bytes.strip():
                continue
            _process(event_bytes)
            raw_buf.extend(event_bytes)
            if len(raw_buf) > 200:
                raw_buf = raw_buf[-200:]
            if sse_done:
                break
        if sse_done:
            break

    # 末尾残留
    if not sse_done and sse_buffer.strip():
        while True:
            event_bytes, sse_buffer = _take_event(sse_buffer)
            if event_bytes is None:
                break
            if event_bytes.strip():
                _process(event_bytes)
            if sse_done:
                break
        if not sse_done and sse_buffer.strip():
            _process(sse_buffer)

    return {
        "first_event_at": first_event_at,
        "event_count": event_count,
        "response_model": response_model,
        "text": "".join(response_text_buf),
        "raw_preview": bytes(raw_buf),
    }


def probe_stream(p: Provider, tier: ModelTier, timeout: int, skip_tls_verify: bool,
                 ttft_timeout: int | None = None,
                 max_tokens: int = PROBE_MAX_TOKENS,
                 disable_thinking: bool = True,
                 user_agent: str | None = None) -> dict:
    """对单个档位进行流式探测。

    主编排：建连接 -> 区分 SSE/非SSE -> 委托 _drain_* -> 错误归一化 -> 状态判定。
    返回字段：status / http_status / elapsed_seconds / ttft_seconds /
              response_model / content_type / event_count / text / is_sse /
              error_category / error / raw_preview。
    """
    url, method, headers, body = build_probe_request(p, tier, stream=True,
                                                     max_tokens=max_tokens,
                                                     disable_thinking=disable_thinking,
                                                     user_agent=user_agent)
    if not url:
        return {"status": "error", "elapsed_seconds": 0, "ttft_seconds": None,
                "response_model": None, "content_type": None, "event_count": 0,
                "text": "", "error": "无法构造请求 URL",
                "error_category": ErrorCategory.UNKNOWN.value}

    ctx = create_ssl_context(skip_tls_verify)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    start = time.time()
    http_status = 0
    content_type = ""
    is_sse = False
    first_event_at = None
    event_count = 0
    response_model = None
    text = ""
    raw_preview = b""
    error_msg = ""
    error_category = ErrorCategory.NONE.value

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            http_status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            is_sse = "text/event-stream" in content_type.lower()

            if not is_sse:
                drain = _drain_non_sse_stream(resp, p)
                text = drain["text"]
                response_model = drain["response_model"]
                raw_preview = drain["raw_preview"]
            else:
                proto_name = detect_protocol(p)["detected"]
                ttft_deadline = ttft_timeout if ttft_timeout is not None else None
                drain = _drain_sse_stream(resp, p, proto_name, ttft_deadline, start)
                first_event_at = drain["first_event_at"]
                event_count = drain["event_count"]
                response_model = drain["response_model"]
                text = drain["text"]
                raw_preview = drain["raw_preview"]

    except urllib.error.HTTPError as e:
        http_status = e.code
        err_body, err_raw = _read_httperror_body(e)
        raw_preview = err_raw[:200]
        category, display = classify_error(err_body, e.code)
        error_category = category.value
        error_msg = f"[{e.code}] {display}" if display else f"[{e.code}]"
    except urllib.error.URLError as e:
        error_msg = f"连接失败: {e.reason}"
        error_category = _error_category_for_urlerror(e)
    except TimeoutError as e:
        # TTFT 路径已设置 TTFT_TIMEOUT；其它读超时归为 NETWORK
        if not error_msg:
            error_msg = f"TTFT 超时" if "ttft" in str(e) else f"超时: {e}"
            error_category = (ErrorCategory.TTFT_TIMEOUT.value if "ttft" in str(e)
                              else ErrorCategory.NETWORK.value)
    except Exception as e:
        error_msg = f"异常: {type(e).__name__}: {e}"
        error_category = (
            _error_category_for_urlerror(e) if _is_tls_error(e)
            else ErrorCategory.UNKNOWN.value
        )

    elapsed = round(time.time() - start, 3)
    ttft = round(first_event_at - start, 3) if first_event_at else None

    # 状态判定
    if error_msg:
        if error_category in (ErrorCategory.NONE.value, "", None):
            error_category = (ErrorCategory.STREAM_INCOMPLETE.value
                              if first_event_at is None else ErrorCategory.NETWORK.value)
        status = "error"
    elif not is_sse:
        if http_status == 200 and text.strip():
            correct = text.strip() == EXPECTED_ANSWER
            status = "pass" if correct else "fail"
            error_category = ErrorCategory.NONE.value if correct else ErrorCategory.ANSWER_MISMATCH.value
        else:
            status = "error"
            error_category = ErrorCategory.STREAM_PROTOCOL.value
            error_msg = f"非 SSE 响应，Content-Type={content_type!r}"
    else:
        if text.strip() == EXPECTED_ANSWER:
            status = "pass"
            error_category = ErrorCategory.NONE.value
        elif text.strip():
            status = "fail"
            error_category = ErrorCategory.ANSWER_MISMATCH.value
        elif first_event_at:
            status = "fail"
            error_category = ErrorCategory.ANSWER_MISMATCH.value
        else:
            status = "error"
            error_category = ErrorCategory.STREAM_INCOMPLETE.value

    return {
        "status": status,
        "http_status": http_status,
        "elapsed_seconds": elapsed,
        "ttft_seconds": ttft,
        "response_model": response_model,
        "content_type": content_type,
        "event_count": event_count,
        "text": text[:80],
        "is_sse": is_sse,
        "error": error_msg,
        "error_category": error_category,
        "raw_preview": raw_preview.decode("utf-8", errors="replace") if raw_preview else "",
        "usage": extract_usage(raw_preview.decode("utf-8", errors="replace") if raw_preview else ""),
    }


# 全局输出目标：默认 stdout；--json 模式下切换为 stderr，stdout 仅承载 JSON
_human_out = sys.stdout
_say_lock = threading.Lock()
_CONTROL_RE = re.compile("\x1b\\[[0-9;]*[A-Za-z]|\x1b\\][^\x07]*\x07|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_for_terminal(s: str) -> str:
    """剥离 ANSI 转义和 C0 控制字符，防止恶意供应商响应注入终端指令。"""
    return _CONTROL_RE.sub("", s)


def say(*a, **k):
    """人类可读进度输出。默认 flush，避免被 PowerShell 管道块缓冲吞掉。

    多线程下串行化，保证每行完整不交错；自动清理控制字符。
    """
    k.setdefault("flush", True)
    cleaned = [_sanitize_for_terminal(str(x)) for x in a]
    with _say_lock:
        print(*cleaned, file=_human_out, **k)


def probe_tier(p: Provider, tier: ModelTier, timeout: int, skip_tls_verify: bool,
               max_tokens: int = PROBE_MAX_TOKENS,
               disable_thinking: bool = True,
               user_agent: str | None = None) -> dict:
    """探测单个档位，返回结果字典（含 usage / raw_body 供 inspect 复用）。"""
    url, method, headers, body = build_probe_request(p, tier,
                                                     max_tokens=max_tokens,
                                                     disable_thinking=disable_thinking,
                                                     user_agent=user_agent)
    empty_usage = extract_usage("")
    if not url:
        return {"tier": tier.tier, "model": tier.model, "status": -1,
                "elapsed": 0, "error": "无法构造请求 URL", "answer": "",
                "usage": empty_usage, "raw_body": "", "has_thinking_signal": False}

    start = time.time()
    resp = _http_request(url, method, headers, body, timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    # 连接层失败（网络/TLS/超时）
    if resp.error_category is not None:
        return {"tier": tier.tier, "model": tier.model, "status": 0,
                "elapsed": elapsed, "error": resp.error_msg,
                "error_category": resp.error_category, "answer": "",
                "usage": empty_usage, "raw_body": "", "has_thinking_signal": False}

    if resp.status != 200:
        category, display = classify_error(resp.body, resp.status)
        return {"tier": tier.tier, "model": tier.model, "status": resp.status,
                "elapsed": elapsed, "error": display, "error_category": category.value,
                "answer": "", "usage": extract_usage(resp.body),
                "raw_body": resp.body[:4000], "has_thinking_signal": False}

    answer = extract_answer(p, resp.body)
    correct = answer.strip() == EXPECTED_ANSWER
    return {"tier": tier.tier, "model": tier.model, "status": 200,
            "elapsed": elapsed, "error": "", "answer": answer[:80],
            "correct": correct,
            "error_category": ErrorCategory.NONE.value if correct else ErrorCategory.ANSWER_MISMATCH.value,
            "usage": extract_usage(resp.body),
            "raw_body": resp.body[:8000],
            "has_thinking_signal": _response_has_thinking_signal(resp.body)}


def probe(p: Provider, timeout: int, skip_tls_verify: bool,
          max_tokens: int = PROBE_MAX_TOKENS,
          disable_thinking: bool = True,
          user_agent: str | None = None,
          on_attempt=None) -> dict:
    """按回退顺序探测档位，首个正确回答的档位即为可用档位。

    on_attempt: 可选回调 (provider, attempt_result) -> None，每档结束后立刻调用，
    用于健康检测增量进度（不等整个 provider 的全部档位跑完）。
    """
    attempts = []
    best_tier = None
    for tier in p.tiers:
        r = probe_tier(p, tier, timeout, skip_tls_verify,
                       max_tokens=max_tokens, disable_thinking=disable_thinking,
                       user_agent=user_agent)
        attempts.append(r)
        if on_attempt is not None:
            try:
                on_attempt(p, r)
            except Exception:
                pass  # 进度回调失败不影响探测结果
        if r["status"] == 200 and r.get("correct"):
            best_tier = r
            break   # 找到能正确回答的档位，停止回退
    overall_ok = best_tier is not None
    return {
        "name": p.name, "type": p.app_type, "base_url": p.base_url,
        "auth_mode": p.auth_mode, "overall_ok": overall_ok,
        "best_tier": best_tier["tier"] if best_tier else None,
        "attempts": attempts,
    }


def fetch_models(p: Provider, timeout: int, skip_tls_verify: bool,
                 user_agent: str | None = None) -> dict:
    """拉取供应商的模型列表（GET /v1/models，Anthropic/OpenAI 兼容站通用）"""
    # 路径不去重：base + /v1/models（与探测保持一致）
    if p.is_openrouter:
        # OpenRouter：base 是 .../chat/completions，模型端点是同级 /models
        url = p.base_url.rsplit("/chat/completions", 1)[0].rstrip("/") + "/models"
    else:
        url = p.base_url.rstrip("/") + "/v1/models"

    auth_h = build_auth_headers(p)
    headers = {**auth_h, "Content-Type": "application/json"}
    if p.app_type == "claude":
        headers.update(_claude_code_headers(user_agent))

    start = time.time()
    resp = _http_request(url, "GET", headers, None, timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    if resp.error_category is not None:
        return {"name": p.name, "base_url": p.base_url, "status": 0,
                "elapsed": elapsed, "error": resp.error_msg,
                "error_category": resp.error_category, "models": []}

    if resp.status != 200:
        category, display = classify_error(resp.body, resp.status)
        return {"name": p.name, "base_url": p.base_url, "status": resp.status,
                "elapsed": elapsed, "error": display, "error_category": category.value, "models": []}

    models = extract_model_ids(resp.body)
    return {"name": p.name, "base_url": p.base_url, "status": 200,
            "elapsed": elapsed, "error": "", "error_category": ErrorCategory.NONE.value,
            "models": models}


def probe_model_metadata(p: Provider, model_id: str, timeout: int,
                           skip_tls_verify: bool,
                           user_agent: str | None = None) -> dict:
    """GET /v1/models/{model_id}，提取供应商声明的窗口、能力等元数据。

    返回：
      status: 'available' | 'unavailable' | 'skipped'
      declared_context_window: int | None
      max_output_tokens: int | None
      capabilities: dict（如 {"image_input": True, "thinking": True}）
      source: 'provider_metadata'
      http_status, error_category, error
    """
    quoted_id = urllib.parse.quote(model_id, safe="")
    if p.is_openrouter:
        url = (p.base_url.rsplit("/chat/completions", 1)[0].rstrip("/")
               + f"/models/{quoted_id}")
    else:
        url = p.base_url.rstrip("/") + f"/v1/models/{quoted_id}"

    auth_h = build_auth_headers(p)
    headers = {**auth_h, "Accept": "application/json"}
    if p.app_type == "claude":
        headers.update(_claude_code_headers(user_agent))

    start = time.time()
    resp = _http_request(url, "GET", headers, None, timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    _unavail = lambda cat, msg: {"status": "unavailable", "http_status": 0,
                "declared_context_window": None, "max_output_tokens": None,
                "capabilities": {}, "source": "provider_metadata",
                "error_category": cat, "error": msg, "elapsed_seconds": elapsed}

    if resp.error_category is not None:
        return _unavail(resp.error_category, resp.error_msg)

    if resp.status != 200:
        category, display = classify_error(resp.body, resp.status)
        return {"status": "unavailable", "http_status": resp.status,
                "declared_context_window": None, "max_output_tokens": None,
                "capabilities": {}, "source": "provider_metadata",
                "error_category": category.value, "error": display,
                "elapsed_seconds": elapsed}

    # 解析：OpenAI/Anthropic 都返回 {"id", "max_input_tokens"|"context_window", ...}
    try:
        j = json.loads(resp.body)
    except (json.JSONDecodeError, TypeError):
        return {"status": "unavailable", "http_status": resp.status,
                "declared_context_window": None, "max_output_tokens": None,
                "capabilities": {}, "source": "provider_metadata",
                "error_category": ErrorCategory.INVALID_RESPONSE.value,
                "error": "响应非 JSON", "elapsed_seconds": elapsed}

    declared = (j.get("max_input_tokens")
                or j.get("context_window")
                or j.get("max_tokens"))
    max_out = j.get("max_output_tokens")
    caps = {}
    if isinstance(j.get("capabilities"), dict):
        for k, v in j["capabilities"].items():
            if isinstance(v, dict) and "supported" in v:
                caps[k] = bool(v["supported"])
            elif isinstance(v, bool):
                caps[k] = v

    return {
        "status": "available",
        "http_status": resp.status,
        "declared_context_window": declared,
        "max_output_tokens": max_out,
        "capabilities": caps,
        "source": "provider_metadata",
        "elapsed_seconds": elapsed,
        "error": None,
        "error_category": ErrorCategory.NONE.value,
    }


# ── 上下文窗口冒烟探测 ──

def _build_context_filler(target_chars: int) -> str:
    """构造约 target_chars 字符的填充文本（英文单词，空格分隔，便于 tokenizer 切分）。

    按 1 字符 ≈ 1 token 的上界逼近真·tokens，不做 tokenizer 依赖。
    """
    # 用短单词循环填充：每个 "word " 约 5 字符，token 数 ≈ 字符数 / 4
    # 为逼近 1 char ≈ 1 token，用更碎的字母+空格
    # 最终策略：重复 "a " 直到足够长度（2 字符 ≈ 1 token，保守但简单）
    repeat_unit = "a "
    count = target_chars // len(repeat_unit)
    return (repeat_unit * count)[:target_chars]


def probe_context_smoke(p: Provider, model_id: str, target_chars: int,
                          timeout: int, skip_tls_verify: bool,
                          user_agent: str | None = None) -> dict:
    """发一次大上下文请求，验证供应商是否接受该体量的输入。

    target_chars: 目标字符数（如 524288 对应 512k）
    使用 1 字符≈1 token 上界逼近真·tokens；报告写 estimate 不写假精确值。
    仅 claude（含 openrouter chat）路径；其它 app_type 返回 unsupported。
    """
    te = f"~{target_chars} chars (≥{target_chars} tokens upper bound, 1char≈1token)"
    if p.app_type not in ("claude",) and not p.is_openrouter:
        return {"status": "unsupported", "approx_input_chars": 0,
                "token_estimate": te, "http_status": None,
                "error_category": ErrorCategory.NONE.value,
                "error": f"context smoke 暂不支持 app_type={p.app_type}",
                "elapsed_seconds": 0}

    filler = _build_context_filler(target_chars)
    prompt = f"{filler}\n\nWhat is 2+3? Reply with only the number."
    url, method, headers, _ = build_probe_request(
        p, ModelTier("default", model_id, model_id),
        max_tokens=PROBE_MAX_TOKENS, disable_thinking=True, user_agent=user_agent)
    if not url:
        return {"status": "error", "approx_input_chars": len(prompt),
                "token_estimate": te, "http_status": None,
                "error_category": ErrorCategory.UNKNOWN.value,
                "error": "无法构造请求 URL", "elapsed_seconds": 0}

    payload = {"model": model_id, "max_tokens": PROBE_MAX_TOKENS,
               "messages": [{"role": "user", "content": prompt}],
               "thinking": {"type": "disabled"}}
    body = json.dumps(payload).encode()

    # 大 body 上传慢：超时至少 120s
    smoke_timeout = max(timeout, 120)
    start = time.time()
    resp = _http_request(url, method, headers, body, smoke_timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    if resp.error_category is not None:
        st = "timeout" if "time" in (resp.error_msg or "").lower() else "error"
        return {"status": st, "approx_input_chars": len(prompt),
                "token_estimate": te, "http_status": 0,
                "error_category": resp.error_category,
                "error": resp.error_msg, "elapsed_seconds": elapsed}

    if resp.status == 200:
        return {"status": "accepted", "approx_input_chars": len(prompt),
                "token_estimate": te, "http_status": 200,
                "error_category": ErrorCategory.NONE.value,
                "error": None, "elapsed_seconds": elapsed}

    low = (resp.body or "").lower()
    if resp.status in (413, 414) or (resp.status == 400 and any(
            k in low for k in ("context", "too long", "maximum", "token", "length", "payload"))):
        return {"status": "rejected", "approx_input_chars": len(prompt),
                "token_estimate": te, "http_status": resp.status,
                "error_category": ErrorCategory.PROTOCOL_INCOMPATIBLE.value,
                "error": classify_error(resp.body, resp.status)[1],
                "elapsed_seconds": elapsed}

    category, display = classify_error(resp.body, resp.status)
    return {"status": "error", "approx_input_chars": len(prompt),
            "token_estimate": te, "http_status": resp.status,
            "error_category": category.value, "error": display,
            "elapsed_seconds": elapsed}


# 极小 1x1 PNG（红色像素），base64 常量，不读外文件
_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)
_TOOL_NAME = "get_probe_number"
_TOOL_DESC = "Return the probe number 5. No side effects."


def _probe_tools(p: Provider, model_id: str, timeout: int, skip_tls_verify: bool,
                 user_agent: str | None = None) -> dict:
    """最小 tool-use 探测：要求模型调用 get_probe_number，不执行副作用。

    判定：
      native   — 协议级 tool_use / tool_calls
      text_only — 纯文本声称调用但无协议块
      rejected  — 400/协议拒 tools
      unknown   — 其它
    """
    url, method, headers, _ = build_probe_request(
        p, ModelTier("default", model_id, model_id),
        max_tokens=max(PROBE_MAX_TOKENS, 64), disable_thinking=True, user_agent=user_agent)
    if not url:
        return {"status": "error", "protocol_support": "unknown",
                "tool_name_seen": None, "http_status": None,
                "error_category": ErrorCategory.UNKNOWN.value,
                "error": "无法构造请求 URL", "evidence": ""}

    prompt = (
        f"You must call the tool {_TOOL_NAME} to answer. "
        "Do not answer with a bare number; invoke the tool."
    )
    if p.app_type == "claude" and not p.is_openrouter:
        payload = {
            "model": model_id,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{
                "name": _TOOL_NAME,
                "description": _TOOL_DESC,
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }],
            "thinking": {"type": "disabled"},
        }
    elif p.app_type in ("openclaw",) or p.is_openrouter:
        payload = {
            "model": model_id,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": _TOOL_NAME,
                    "description": _TOOL_DESC,
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            "tool_choice": "auto",
        }
    elif p.app_type == "codex":
        # Responses API function tools（简化）
        payload = {
            "model": model_id,
            "max_output_tokens": 64,
            "input": prompt,
            "tools": [{
                "type": "function",
                "name": _TOOL_NAME,
                "description": _TOOL_DESC,
                "parameters": {"type": "object", "properties": {}},
            }],
        }
    else:
        return {"status": "unsupported", "protocol_support": "unknown",
                "tool_name_seen": None, "http_status": None,
                "error_category": ErrorCategory.NONE.value,
                "error": None, "evidence": f"app_type={p.app_type}"}

    body = json.dumps(payload).encode()
    start = time.time()
    resp = _http_request(url, method, headers, body, timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    if resp.error_category is not None:
        return {"status": "error", "protocol_support": "unknown",
                "tool_name_seen": None, "http_status": 0,
                "error_category": resp.error_category, "error": resp.error_msg,
                "evidence": "", "elapsed_seconds": elapsed}

    if resp.status in (400, 422) and any(
            k in (resp.body or "").lower()
            for k in ("tool", "function", "schema", "unknown field")):
        return {"status": "fail", "protocol_support": "rejected",
                "tool_name_seen": None, "http_status": resp.status,
                "error_category": ErrorCategory.PROTOCOL_INCOMPATIBLE.value,
                "error": classify_error(resp.body, resp.status)[1],
                "evidence": (resp.body or "")[:200], "elapsed_seconds": elapsed}

    if resp.status != 200:
        cat, disp = classify_error(resp.body, resp.status)
        return {"status": "error", "protocol_support": "unknown",
                "tool_name_seen": None, "http_status": resp.status,
                "error_category": cat.value, "error": disp,
                "evidence": (resp.body or "")[:200], "elapsed_seconds": elapsed}

    body_s = resp.body or ""
    low = body_s.lower()
    tool_seen = None
    support = "unknown"
    # Anthropic tool_use
    if '"type":"tool_use"' in low or '"type": "tool_use"' in low:
        support = "native"
        if _TOOL_NAME in body_s:
            tool_seen = _TOOL_NAME
    # OpenAI tool_calls
    elif "tool_calls" in low or '"type":"function_call"' in low:
        support = "native"
        if _TOOL_NAME in body_s:
            tool_seen = _TOOL_NAME
    elif _TOOL_NAME in body_s or "call" in low:
        support = "text_only"
        tool_seen = _TOOL_NAME if _TOOL_NAME in body_s else None
    else:
        # 可能忽略 tools 直接答 5
        ans = extract_answer(p, body_s)
        if ans.strip() == EXPECTED_ANSWER:
            support = "text_only"
        else:
            support = "unknown"

    status = "pass" if support == "native" else "fail"
    return {
        "status": status,
        "protocol_support": support,
        "tool_name_seen": tool_seen,
        "http_status": 200,
        "error_category": ErrorCategory.NONE.value if status == "pass"
        else ErrorCategory.ANSWER_MISMATCH.value,
        "error": None if status == "pass" else f"protocol_support={support}",
        "evidence": body_s[:240],
        "elapsed_seconds": elapsed,
    }


def _probe_vision(p: Provider, model_id: str, timeout: int, skip_tls_verify: bool,
                  user_agent: str | None = None) -> dict:
    """可选 vision 探测：发 1x1 PNG，问主色（宽松判定）。"""
    url, method, headers, _ = build_probe_request(
        p, ModelTier("default", model_id, model_id),
        max_tokens=32, disable_thinking=True, user_agent=user_agent)
    if not url:
        return {"status": "error", "http_status": None,
                "error_category": ErrorCategory.UNKNOWN.value,
                "error": "无法构造请求 URL", "answer": "", "evidence": ""}

    q = "What is the main color of this image? Reply with one English color word only."
    if p.app_type == "claude" and not p.is_openrouter:
        payload = {
            "model": model_id,
            "max_tokens": 32,
            "thinking": {"type": "disabled"},
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _PROBE_PNG_B64,
                        },
                    },
                    {"type": "text", "text": q},
                ],
            }],
        }
    elif p.app_type in ("openclaw",) or p.is_openrouter:
        payload = {
            "model": model_id,
            "max_tokens": 32,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": q},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{_PROBE_PNG_B64}",
                        },
                    },
                ],
            }],
        }
    else:
        return {"status": "unsupported", "http_status": None,
                "error_category": ErrorCategory.NONE.value,
                "error": None, "answer": "",
                "evidence": f"app_type={p.app_type}"}

    body = json.dumps(payload).encode()
    start = time.time()
    resp = _http_request(url, method, headers, body, timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    if resp.error_category is not None:
        return {"status": "error", "http_status": 0,
                "error_category": resp.error_category, "error": resp.error_msg,
                "answer": "", "evidence": "", "elapsed_seconds": elapsed}

    if resp.status in (400, 415, 422) and any(
            k in (resp.body or "").lower()
            for k in ("image", "vision", "multimodal", "unsupported", "media")):
        return {"status": "fail", "http_status": resp.status,
                "error_category": ErrorCategory.PROTOCOL_INCOMPATIBLE.value,
                "error": classify_error(resp.body, resp.status)[1],
                "answer": "", "evidence": (resp.body or "")[:200],
                "elapsed_seconds": elapsed}

    if resp.status != 200:
        cat, disp = classify_error(resp.body, resp.status)
        return {"status": "error", "http_status": resp.status,
                "error_category": cat.value, "error": disp,
                "answer": "", "evidence": (resp.body or "")[:200],
                "elapsed_seconds": elapsed}

    ans = extract_answer(p, resp.body)
    # 宽松：有非空回答即 pass（1x1 图颜色不可靠，只验证是否接受 image）
    status = "pass" if ans.strip() else "fail"
    return {
        "status": status,
        "http_status": 200,
        "error_category": ErrorCategory.NONE.value if status == "pass"
        else ErrorCategory.ANSWER_MISMATCH.value,
        "error": None if status == "pass" else "empty vision answer",
        "answer": ans[:80],
        "evidence": "image accepted; answer not strictly validated",
        "elapsed_seconds": elapsed,
    }


def extract_model_ids(resp_body: str) -> list:
    """从 /v1/models 响应提取模型 id，兼容 OpenAI({data:[{id}]}) 和 Anthropic({data:[{id}]}) 格式"""
    try:
        j = json.loads(resp_body)
    except (json.JSONDecodeError, TypeError):
        return []
    ids = []
    # OpenAI/Anthropic 通用：{"data": [{"id": "..."}]}
    for m in j.get("data", []):
        if isinstance(m, dict) and m.get("id"):
            ids.append(m["id"])
    # 少数站直接返回 {"models": [...]} 或 [...]
    if not ids:
        raw = j.get("models", j if isinstance(j, list) else [])
        for m in raw:
            if isinstance(m, dict) and (m.get("id") or m.get("name")):
                ids.append(m.get("id") or m.get("name"))
            elif isinstance(m, str):
                ids.append(m)
    return ids


def run_list_models(args, providers, say) -> int:
    """拉取每个供应商的 /v1/models 模型目录（不进行健康探测）。"""
    scope = "故障转移队列" if getattr(args, "failover_only", False) else "全部"
    args_type = getattr(args, "type", "claude")
    say(f"从 {args.db} 加载 {len(providers)} 个供应商 ({scope})")
    say(f"拉取模型列表: GET /v1/models  并发: {args.workers}  超时: {args.timeout}s (type={args_type})\n")
    results = []
    _ua = getattr(args, "user_agent", None)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_models, p, args.timeout, args.skip_tls_verify, _ua): p for p in providers}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            if r["status"] == 200:
                say(f"[{i:>2}/{len(providers)}] ✅ {r['name'][:24]:24} {len(r['models'])} 个模型")
            else:
                say(f"[{i:>2}/{len(providers)}] ❌ {r['name'][:24]:24} [{r['status']}] {r['error'][:40]}")

    ok = [r for r in results if r["status"] == 200]
    say(f"\n{'='*60}")
    say(f"完成: ✅ {len(ok)} 个供应商返回模型列表  共 {len(results)} 个")
    say(f"{'='*60}")
    for r in ok:
        say(f"\n■ {r['name']}  ({len(r['models'])} 个模型)  {r['base_url']}")
        for mid in r["models"]:
            say(f"    {mid}")
    fail = [r for r in results if r["status"] != 200]
    if fail:
        say(f"\n未返回模型列表的供应商（{len(fail)} 个）:")
        for r in fail:
            say(f"  ❌ {r['name'][:24]:24} [{r['status']}] {r['error']}")
    return 0


def run_health_check(args, providers, say) -> int:
    """对每个供应商按档位回退顺序进行真实问题探测。

    进度策略：
      - 每个档位尝试结束立刻打印一行（on_attempt）
      - 每个供应商全部档位结束后再打印汇总行
      - say() 默认 flush，配合启动器 -u，避免管道块缓冲导致「全部结束才显示」
    """
    scope = "故障转移队列" if getattr(args, "failover_only", False) else "全部"
    say(f"从 {args.db} 加载 {len(providers)} 个供应商 ({scope})")
    say(f"并发: {args.workers}  超时: {args.timeout}s")
    say(f"探测问题: \"{PROBE_QUESTION}\"  期望回答为 \"{EXPECTED_ANSWER}\"")
    say(f"档位回退: {' → '.join(TIER_ORDER)}  认证: 按配置  路径: 不去重")
    say("进度: 每档完成立即显示，供应商完成显示汇总\n")

    results = []
    health_started = time.time()
    _mt = getattr(args, "probe_max_tokens", PROBE_MAX_TOKENS)
    _dt = not getattr(args, "probe_enable_thinking", False)
    _ua = getattr(args, "user_agent", None)

    def _on_attempt(p: Provider, r: dict) -> None:
        # 档位级增量：多线程下可能交错，但每行原子且带供应商名
        st = r.get("status", 0)
        if st == 200 and r.get("correct"):
            say(f"  · {p.name[:22]:22} {r['tier']:6} [ok] {r.get('elapsed', 0)}s "
                f"回答:\"{r.get('answer', '')}\"")
        elif st == 200:
            say(f"  · {p.name[:22]:22} {r['tier']:6} [答案不符] {r.get('elapsed', 0)}s "
                f"\"{r.get('answer', '')}\"")
        else:
            err = (r.get("error") or "")[:40]
            say(f"  · {p.name[:22]:22} {r['tier']:6} [{st}] {r.get('elapsed', 0)}s {err}")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(probe, p, args.timeout, args.skip_tls_verify,
                      _mt, _dt, _ua, _on_attempt): p
            for p in providers
        }
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            icon = "✅" if r["overall_ok"] else "❌"
            if r["overall_ok"]:
                bt = r["best_tier"]
                # 找到成功那档的答案
                ans = next((a["answer"] for a in r["attempts"] if a["tier"] == bt), "")
                say(f"[{i:>2}/{len(providers)}] {icon} {r['name'][:24]:24} ✓{bt} 回答:\"{ans}\"")
            else:
                # 列出每个失败档位的简短结果
                fails = " | ".join(f"{a['tier']}:{a['status']}({a['error'][:30]})"
                                   for a in r["attempts"])
                say(f"[{i:>2}/{len(providers)}] {icon} {r['name'][:24]:24} {fails}")
            if getattr(args, "with_history", False):
                try:
                    say(format_history_sidebar(
                        args.db, r["name"],
                        since=getattr(args, "history_since", "24h") or "24h"))
                except Exception as e:
                    say(f"  history: 读取失败 ({type(e).__name__}: {e})")

    ok = [r for r in results if r["overall_ok"]]
    fail = [r for r in results if not r["overall_ok"]]
    say(f"\n{'='*60}")
    say(f"完成: ✅ {len(ok)} 可用(能正确回答)  ❌ {len(fail)} 不可用  共 {len(results)} 个")
    say(f"{'='*60}")

    if fail:
        say("\n不可用详情（每档尝试结果）:")
        for r in fail:
            say(f"  ❌ {r['name'][:24]:24} {r['base_url']}  [auth:{r['auth_mode']}]")
            for a in r["attempts"]:
                say(f"      {a['tier']:8} {a['model']:28} [{a['status']}] {a['elapsed']}s")
                if a["error"]:
                    say(f"               → {a['error']}")

    if ok:
        say("\n可用详情:")
        for r in ok:
            bt = r["best_tier"]
            a = next(x for x in r["attempts"] if x["tier"] == bt)
            say(f"  ✅ {r['name'][:24]:24} 档位:{bt:8} {a['model']:28} {a['elapsed']}s 回答:\"{a['answer']}\"")

    # JSON 模式：最后输出结构化报告到 stdout
    if getattr(args, "json", False):
        # 结果按 providers 的输入顺序（sort_index）稳定排序，
        # 避免 as_completed 的完成序导致 JSON 每次不同；type 纳入 key，
        # 防止 --type all 下同名供应商互相覆盖。
        order = {(p.app_type, p.name): i for i, p in enumerate(providers)}
        results_sorted = sorted(
            results,
            key=lambda r: order.get((r.get("type"), r.get("name")), 1_000_000),
        )
        json_report = {
            "schema_version": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "db_path": args.db,
            "scope": "failover" if getattr(args, "failover_only", False) else "all",
            "type": getattr(args, "type", "claude"),
            "probe_question": PROBE_QUESTION,
            "expected_answer": EXPECTED_ANSWER,
            "tier_order": TIER_ORDER,
            "elapsed_seconds": round(time.time() - health_started, 2),
            "summary": {
                "total": len(results),
                "available": len(ok),
                "unavailable": len(fail),
            },
            "providers": results_sorted,
        }
        print(json.dumps(json_report, ensure_ascii=False, indent=2), flush=True)
    return 0 if ok else 1


def resolve_inspect_target(args, providers, say) -> tuple:
    """解析 inspect 子命令的目标 (Provider, model_id, error_message)。

    返回 (provider, model_id, None) 成功；或 (None, None, error_message) 失败。
    不做实际探测，只做"目标是否可识别"的判断。
    """
    name = args.provider
    model = args.model

    # 1. 找到供应商
    p = next((x for x in providers if x.name == name), None)
    if p is None:
        return None, None, f"未找到供应商: {name!r}（当前 type={args.type}）"

    # 2. 按 source 处理模型
    if args.source == "configured":
        # 精确匹配 raw_model 或 stripped model
        for t in p.tiers:
            if t.raw_model == model or t.model == model:
                # 默认用 stripped model（与 check 子命令一致，上游可能拒带 [1M] 后缀）；
                # --keep-suffix 时保留原始 raw_model
                return p, (t.raw_model if args.keep_suffix else t.model), None
        available = [t.raw_model for t in p.tiers] or ["(无档位)"]
        return None, None, (f"供应商 {name!r} 未配置模型 {model!r}；"
                            f"可用档位: {', '.join(available)}")

    if args.source == "manual":
        # 强制使用用户提供的字面值；--keep-suffix 决定是否去后缀
        raw = model
        clean = raw if args.keep_suffix else re.sub(r"\[.*?\]$", "", raw)
        return p, clean, None

    if args.source == "listed":
        # 调一次 /v1/models，找到则复用供应商
        r = fetch_models(p, args.timeout, args.skip_tls_verify,
                         user_agent=getattr(args, "user_agent", None))
        if r["status"] != 200:
            return None, None, f"拉取 /v1/models 失败: [{r['status']}] {r['error'][:80]}"
        if model not in r["models"]:
            preview = ", ".join(r["models"][:10])
            return None, None, (f"供应商 {name!r} /v1/models 中未列出 {model!r}；"
                                f"前 10 个: {preview}")
        raw = model
        clean = raw if args.keep_suffix else re.sub(r"\[.*?\]$", "", raw)
        return p, clean, None

    return None, None, f"未知 source: {args.source}"


def rebuild_provider_for_inspect(p: Provider, model_id: str) -> Provider:
    """用指定 model_id 构造单档位 default 的 Provider，用于 inspect。"""
    return Provider(
        name=p.name,
        app_type=p.app_type,
        base_url=p.base_url,
        api_key=p.api_key,
        auth_mode=p.auth_mode,
        tiers=[ModelTier("default", model_id, model_id)],
        is_current=p.is_current,
        in_failover=p.in_failover,
        is_openrouter=p.is_openrouter,
    )


def detect_protocol(p: Provider) -> dict:
    """根据 base_url 和 app_type 推断协议路径；不发送网络请求。"""
    base = p.base_url.rstrip("/")
    if "/chat/completions" in base:
        detected = "openai_chat_completions"
    elif base.endswith("/v1/responses") or "/v1/responses" in base:
        detected = "openai_responses"
    elif base.endswith("/v1/messages") or "/v1/messages" in base:
        detected = "anthropic_messages"
    else:
        # 默认按 app_type 推断
        detected = {
            "claude": "anthropic_messages",
            "codex": "openai_responses",
            "openclaw": "openai_chat_completions",
        }.get(p.app_type, "unknown")

    return {
        "detected": detected,
        "confidence": "inferred",  # 文本探测完成后会升级为 confirmed/ambiguous
        "evidence": {
            "path_suffix": base,
            "app_type": p.app_type,
        },
    }


# 模型规范化后缀：日期快照 / 上下文窗口 / 思考模式 / fast 模式
_MODEL_SUFFIX_PATTERNS = [
    r"-\d{8}$",           # -20251001
    r"-\d{4}-\d{2}-\d{2}$",  # -2025-10-01
    r"\[1M\]$",            # [1M] 上下文
    r"\[200K\]$",
    r"\[64K\]$",
    r"-thinking$",
    r"-extended$",
    r"-fast$",
    r"-preview$",
    r"-beta$",
]
_MODEL_SUFFIX_REGEX = re.compile("|".join(_MODEL_SUFFIX_PATTERNS), re.IGNORECASE)


def _normalize_model_id(s: str) -> str:
    """规范化模型 ID：去常见后缀、去 [1M]、去空白、转小写。"""
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"\[.*?\]$", "", s).strip()
    s = _MODEL_SUFFIX_REGEX.sub("", s)
    s = s.lower()
    s = re.sub(r"\s+", "", s)
    return s


def compare_models(requested: str, responded: str | None) -> dict:
    """对请求模型与响应模型做一致性比对。

    返回：
      match: exact_match | alias_match | fuzzy_match | mismatch | unverifiable
      warning: 人类可读告警（None 表示无问题）
    """
    if not responded:
        return {"match": "unverifiable", "warning": "响应中未携带模型字段，无法比对"}

    if not requested:
        return {"match": "unverifiable", "warning": "请求模型 ID 为空"}

    if requested == responded:
        return {"match": "exact_match", "warning": None}

    norm_req = _normalize_model_id(requested)
    norm_res = _normalize_model_id(responded)

    if norm_req == norm_res:
        # 规范化后相同但字面值不同 → 视为 alias_match（如日期快照后缀）
        return {"match": "alias_match",
                "warning": f"供应商去除了日期/上下文后缀：{requested!r} → {responded!r}"}

    # 模糊包含：处理版本号/别名嵌入
    if norm_req in norm_res or norm_res in norm_req:
        return {"match": "fuzzy_match",
                "warning": f"模型名不完全一致：{requested!r} vs {responded!r}"}

    return {"match": "mismatch",
            "warning": f"模型路由不一致：请求 {requested!r}，实际响应 {responded!r}"}


def run_inspect(args, providers, say) -> int:
    """单一模型深度检测（阶段 3 仅实现 text；阶段 4+ 续上 streaming/metadata/...）。"""
    p, model_id, err = resolve_inspect_target(args, providers, say)
    if p is None:
        say(err)
        print(json.dumps({
            "schema_version": 1,
            "command": "inspect",
            "error": err,
            "provider": getattr(args, "provider", None),
            "model": getattr(args, "model", None),
            "source": getattr(args, "source", None),
        }, ensure_ascii=False, indent=2))
        return 2

    # 仅当 source=listed 时 resolve 阶段已经发了网络请求；其它 source 此处不需要
    inspect_p = rebuild_provider_for_inspect(p, model_id)
    protocol = detect_protocol(p)

    include = set(x.strip() for x in args.include.split(",") if x.strip())
    _mt = getattr(args, "probe_max_tokens", PROBE_MAX_TOKENS)
    _dt = not getattr(args, "probe_enable_thinking", False)
    _ua = getattr(args, "user_agent", None)

    # --with-metadata 废弃兼容
    if getattr(args, "with_metadata", False):
        say("提示: --with-metadata 已废弃，metadata 默认包含在 --include 中")

    # 文本探测（阶段 3 必含项）
    text_result = None
    text_raw = None
    if "text" in include:
        r = probe_tier(inspect_p, inspect_p.tiers[0], args.timeout, args.skip_tls_verify,
                       max_tokens=_mt, disable_thinking=_dt, user_agent=_ua)
        text_raw = r
        text_result = {
            "status": "pass" if r.get("status") == 200 and r.get("correct") else
                      "fail" if r.get("status") == 200 else
                      "error",
            "elapsed_seconds": r.get("elapsed", 0),
            "answer": r.get("answer", ""),
            "correct": r.get("correct", False),
            "http_status": r.get("status", 0),
            "error_category": r.get("error_category", ErrorCategory.UNKNOWN.value),
            "error": r.get("error", ""),
        }

    # 流式探测（阶段 4）
    streaming_result = None
    if "streaming" in include:
        ttft = getattr(args, "ttft_timeout", None)
        sr = probe_stream(inspect_p, inspect_p.tiers[0], args.timeout,
                          args.skip_tls_verify, ttft_timeout=ttft,
                          max_tokens=_mt, disable_thinking=_dt, user_agent=_ua)
        streaming_result = sr

    # 元数据探测（默认纳入）
    metadata_result = {"status": "skipped"}
    if "metadata" in include:
        model_for_meta = re.sub(r"\[.*?\]$", "", model_id)
        metadata_result = probe_model_metadata(inspect_p, model_for_meta,
                                              args.timeout, args.skip_tls_verify,
                                              user_agent=getattr(args, "user_agent", None))

    # 上下文窗口探测：无声明时按档位触发
    context_result = {"status": "skipped"}
    if "metadata" in include:
        has_declared = (metadata_result.get("declared_context_window") is not None
                        and metadata_result.get("status") == "available")
        if not has_declared:
            _ctx_chars = {"512k": 524288, "1m": 1048576}.get(
                getattr(args, "probe_context", "512k"), 524288)
            context_result = probe_context_smoke(inspect_p, model_id, _ctx_chars,
                                                  args.timeout, args.skip_tls_verify,
                                                  user_agent=getattr(args, "user_agent", None))

    # thinking 探测（复用 text 的 disable 结果 + 再发一次 enable）
    thinking_result = {"status": "skipped"}
    if "thinking" in include and text_raw is not None:
        # disable 结果来自 text 探测
        disable_ok = (text_raw.get("status") == 200)
    elif "thinking" in include:
        # text 未跑但用户要求 thinking → 报告依赖缺失
        thinking_result = {
            "status": "dependency_missing",
            "error": "thinking 需要 text 在 --include 中（复用 disable 结果）",
        }
    if "thinking" in include and text_raw is not None:
        # enable 探测（允许 thinking，提高 max_tokens）
        r_en = probe_tier(inspect_p, inspect_p.tiers[0], args.timeout, args.skip_tls_verify,
                          max_tokens=max(_mt, 256), disable_thinking=False, user_agent=_ua)
        thinking_result = {
            "disabled": {
                "status": "pass" if disable_ok else "error",
                "http_status": text_raw.get("status"),
                "has_answer": text_raw.get("correct", False),
                "has_thinking_signal": text_raw.get("has_thinking_signal", False),
            },
            "enabled": {
                "status": "pass" if r_en.get("status") == 200 else "error",
                "http_status": r_en.get("status"),
                "has_answer": r_en.get("correct", False),
                "has_thinking_signal": r_en.get("has_thinking_signal", False),
            },
            "verdict": "unknown",
        }
        # 判定 verdict
        if r_en.get("status") == 400:
            thinking_result["verdict"] = "rejects_thinking_field"
        elif not disable_ok and r_en.get("status") == 200:
            thinking_result["verdict"] = "forces_thinking"
        elif disable_ok and r_en.get("status") == 200:
            if r_en.get("has_thinking_signal"):
                thinking_result["verdict"] = "supports_disable_and_emits_thinking"
            else:
                thinking_result["verdict"] = "supports_disable"
        elif not disable_ok and not r_en.get("correct"):
            thinking_result["verdict"] = "breaks_on_short_budget"
        else:
            thinking_result["verdict"] = "unknown"

    # tool use 探测（默认开）
    tools_result = {"status": "skipped"}
    if "tools" in include:
        tools_result = _probe_tools(inspect_p, model_id, args.timeout, args.skip_tls_verify, _ua)

    # vision 探测（默认关，仅 include 时跑）
    vision_result = {"status": "skipped"}
    if "vision" in include:
        vision_result = _probe_vision(inspect_p, model_id, args.timeout, args.skip_tls_verify, _ua)

    # 协议置信度升级：仅在文本探测成功时升级
    if text_result and text_result["status"] == "pass":
        protocol["confidence"] = "confirmed"

    # 模型一致性：仅当 include 含 model-consistency 时计算，否则 not_run
    requested_model = model_id
    responded_model = None
    if streaming_result and streaming_result.get("response_model"):
        responded_model = streaming_result["response_model"]
    elif text_result and text_result.get("http_status") == 200:
        # 非流式：extract_answer 没有返回 model 字段，留 None
        pass

    if "model-consistency" in include:
        model_consistency = {
            "requested": requested_model,
            "responded": responded_model,
            "match": "not_run" if responded_model is None else "pending",
            "warning": None,
        }
        if responded_model:
            cmp = compare_models(requested_model, responded_model)
            model_consistency["match"] = cmp["match"]
            model_consistency["warning"] = cmp["warning"]
    else:
        model_consistency = {
            "requested": requested_model,
            "responded": responded_model,
            "match": "not_run",
            "warning": None,
        }

    # usage：从 text 探测结果提取（若 text 跑了）
    usage = {"present": False, "input_tokens": None, "output_tokens": None,
             "source": None, "missing_fields": ["input_tokens", "output_tokens"]}
    if text_raw and text_raw.get("usage"):
        usage = text_raw["usage"]

    # 整体结论
    if text_result:
        if text_result["status"] == "pass":
            verdict = "healthy"
        elif text_result["status"] == "fail":
            verdict = "available_but_wrong_answer"
        else:
            verdict = "unavailable"
    else:
        verdict = "skipped"

    anomaly = (model_consistency.get("match") == "mismatch")
    recommended = []
    if model_consistency.get("match") == "mismatch":
        recommended.append("检查供应商是否将模型别名静默路由到其它模型")
    if model_consistency.get("match") == "fuzzy_match":
        recommended.append("供应商可能使用别名；确认映射是否符合预期")
    if text_result and text_result["status"] == "fail":
        recommended.append("供应商返回了 200 但答案不匹配；可能是模型降级或代理错误")
    if text_result and text_result["status"] == "error":
        recommended.append(f"该供应商探测失败 ({text_result['error_category']})，可能影响故障转移")
    if thinking_result.get("verdict") == "forces_thinking":
        recommended.append("模型强制 thinking 模式；短 max_tokens 预算下可能无最终答案")
    if thinking_result.get("verdict") == "rejects_thinking_field":
        recommended.append("供应商拒绝 thinking/reasoning 相关字段；可尝试 --probe-enable-thinking 跳过")
    if tools_result.get("status") == "error":
        recommended.append("Tool use 探测失败；Claude Code 的 tool 调用可能不可用")

    report = {
        "schema_version": 1,
        "command": "inspect",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": inspect_p.name,
        "model": model_id,
        "model_source": args.source,
        "base_url": inspect_p.base_url,
        "auth_mode": inspect_p.auth_mode,
        "protocol": protocol,
        "text": text_result,
        "streaming": streaming_result if streaming_result is not None else {"status": "not_run"},
        "metadata": metadata_result,
        "context": context_result,
        "thinking": thinking_result,
        "tools": tools_result,
        "vision": vision_result,
        "model_consistency": model_consistency,
        "usage": usage,
        "summary": {
            "verdict": verdict,
            "model_routing_anomaly": anomaly,
            "recommended_actions": recommended,
        },
    }
    if getattr(args, "with_history", False):
        try:
            since = getattr(args, "history_since", "24h") or "24h"
            report["history"] = summarize_provider_history(
                args.db, inspect_p.name, since_ts=_parse_since(since))
            report["history_since"] = since
        except Exception as e:
            report["history"] = {"error": f"{type(e).__name__}: {e}"}

    if args.human:
        # 人类可读输出到 stdout（即使 JSON 模式，也走 stdout）
        text = format_inspect_human(report)
        if report.get("history") and not report["history"].get("error"):
            h = report["history"]
            since = report.get("history_since", "24h")
            rate = f"{h.get('success_rate', 0)*100:.0f}%"
            text += (f"\n  history({since}): 请求{h.get('total')} 成功{rate} "
                     f"失败{h.get('fail')} 主因={h.get('top_fail_category') or '-'} "
                     f"路由≠{h.get('mismatch_rate', 0)*100:.0f}%")
        elif report.get("history", {}).get("error"):
            text += f"\n  history: {report['history']['error']}"
        print(text)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if verdict in ("healthy", "skipped") else 1


def format_inspect_human(r: dict) -> str:
    """把 inspect 报告格式化为人类可读文本。"""
    lines = []
    lines.append("=" * 60)
    lines.append(f"  Provider:  {r['provider']}")
    lines.append(f"  Model:     {r['model']} ({r['model_source']})")
    lines.append(f"  Protocol:  {r['protocol']['detected']} · {r['protocol']['confidence']}")
    lines.append("=" * 60)
    lines.append("")

    # [1] 文本 + usage
    if r.get("text"):
        t = r["text"]
        if t["status"] == "pass":
            lines.append("[1/7] 文本探测")
            lines.append(f"  状态：✅ pass · {t['elapsed_seconds']}s")
            lines.append(f"  答案：\"{t['answer']}\" · 正确")
        elif t["status"] == "fail":
            lines.append("[1/7] 文本探测")
            lines.append(f"  状态：⚠ fail · {t['elapsed_seconds']}s")
            lines.append(f"  答案：\"{t['answer']}\" · 不正确")
        else:
            lines.append("[1/7] 文本探测")
            lines.append(f"  状态：❌ error · {t['elapsed_seconds']}s · [{t['error_category']}]")
            if t["error"]:
                lines.append(f"  错误：{t['error']}")
    else:
        lines.append("[1/7] 文本探测 · skipped")
    u = r.get("usage") or {}
    if u.get("present"):
        lines.append(f"  usage：in={u.get('input_tokens')} out={u.get('output_tokens')}")
    else:
        lines.append("  usage：未返回 / 未解析")

    # [2] 流式
    lines.append("")
    s = r.get("streaming") or {}
    if s.get("status") in ("pass", "fail", "error"):
        ttft = s.get("ttft_seconds")
        ttft_str = f"TTFT {ttft}s" if ttft is not None else "无首 token"
        lines.append("[2/7] 流式探测")
        if s["status"] == "pass":
            lines.append(f"  状态：✅ pass · {ttft_str} · 总 {s['elapsed_seconds']}s")
        elif s["status"] == "fail":
            lines.append(f"  状态：⚠ fail · {ttft_str} · 总 {s['elapsed_seconds']}s")
        else:
            lines.append(f"  状态：❌ error · [{s.get('error_category')}]")
            if s.get("error"):
                lines.append(f"  错误：{s['error']}")
        if s.get("content_type"):
            lines.append(f"  Content-Type: {s['content_type']}")
        if s.get("response_model"):
            lines.append(f"  响应模型: {s['response_model']}")
        if s.get("event_count") is not None:
            lines.append(f"  事件数: {s['event_count']}")
    else:
        lines.append(f"[2/7] 流式探测 · {s.get('status', 'not_run')}")

    # [3] 模型路由
    lines.append("")
    m = r.get("model_consistency") or {}
    if m.get("responded"):
        warn = m.get("warning") or ""
        lines.append("[3/7] 模型路由比对")
        lines.append(f"  请求：{m['requested']}")
        lines.append(f"  响应：{m['responded']}")
        lines.append(f"  匹配：{m.get('match')} {warn}")
    else:
        lines.append(f"[3/7] 模型路由比对 · {m.get('match', 'not_run')}")

    # [4] 元数据 / 上下文
    lines.append("")
    md = r.get("metadata") or {}
    ctx = r.get("context") or {}
    if md.get("status") in ("available", "unavailable"):
        lines.append("[4/7] 模型元数据")
        if md["status"] == "available":
            cwin = md.get("declared_context_window")
            mout = md.get("max_output_tokens")
            if cwin:
                lines.append(f"  声明上下文窗口：{cwin:,} tokens（供应商声明，非实测）")
            if mout:
                lines.append(f"  声明最大输出：{mout:,} tokens")
            caps = md.get("capabilities") or {}
            if caps:
                true_caps = [k for k, v in caps.items() if v]
                if true_caps:
                    lines.append(f"  能力：{', '.join(true_caps)}")
            if not cwin:
                lines.append("  无声明窗口 → 触发上下文冒烟")
        else:
            lines.append(f"  状态：unavailable · [{md.get('error_category')}]")
            if md.get("error"):
                lines.append(f"  错误：{md['error'][:120]}")
    else:
        lines.append(f"[4/7] 模型元数据 · {md.get('status', 'not_run')}")
    if ctx.get("status") and ctx.get("status") != "skipped":
        lines.append(f"  上下文冒烟：{ctx.get('status')} · chars≈{ctx.get('approx_input_chars')} · "
                     f"{ctx.get('token_estimate', '')}")
        if ctx.get("error"):
            lines.append(f"  冒烟错误：{str(ctx['error'])[:120]}")

    # [5] Thinking
    lines.append("")
    th = r.get("thinking") or {}
    if th.get("status") == "skipped" or th.get("verdict") is None and th.get("status") == "skipped":
        lines.append(f"[5/7] Thinking · {th.get('status', 'not_run')}")
    elif th.get("verdict") or th.get("disabled") or th.get("enabled"):
        lines.append("[5/7] Thinking")
        lines.append(f"  verdict：{th.get('verdict', 'unknown')}")
        d = th.get("disabled") or {}
        e = th.get("enabled") or {}
        if d:
            lines.append(f"  disable：{d.get('status')} http={d.get('http_status')} "
                         f"answer={d.get('has_answer')} think_sig={d.get('has_thinking_signal')}")
        if e:
            lines.append(f"  enable ：{e.get('status')} http={e.get('http_status')} "
                         f"answer={e.get('has_answer')} think_sig={e.get('has_thinking_signal')}")
    else:
        lines.append(f"[5/7] Thinking · {th.get('status', 'not_run')}")

    # [6] Tools
    lines.append("")
    tools = r.get("tools") or {}
    if tools.get("status") in ("pass", "fail", "error", "unsupported"):
        lines.append("[6/7] Tool use")
        icon = {"pass": "✅", "fail": "⚠", "error": "❌", "unsupported": "·"}.get(
            tools["status"], "·")
        lines.append(f"  状态：{icon} {tools['status']} · support={tools.get('protocol_support')}")
        if tools.get("tool_name_seen"):
            lines.append(f"  tool：{tools['tool_name_seen']}")
        if tools.get("error"):
            lines.append(f"  错误：{str(tools['error'])[:120]}")
    else:
        lines.append(f"[6/7] Tool use · {tools.get('status', 'not_run')}")

    # [7] Vision
    lines.append("")
    vis = r.get("vision") or {}
    if vis.get("status") in ("pass", "fail", "error", "unsupported"):
        lines.append("[7/7] Vision")
        icon = {"pass": "✅", "fail": "⚠", "error": "❌", "unsupported": "·"}.get(
            vis["status"], "·")
        lines.append(f"  状态：{icon} {vis['status']}")
        if vis.get("answer"):
            lines.append(f"  答案：\"{vis['answer']}\"")
        if vis.get("error"):
            lines.append(f"  错误：{str(vis['error'])[:120]}")
    else:
        lines.append(f"[7/7] Vision · {vis.get('status', 'not_run')}")

    lines.append("")
    lines.append("-" * 60)
    lines.append(f"  总结：{r['summary']['verdict']}")
    for a in (r.get("summary") or {}).get("recommended_actions") or []:
        lines.append(f"  · {a}")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------- cc-switch 运行日志（只读） ----------

LOGS_DIR = str(Path.home() / ".cc-switch" / "logs")


def _parse_since(s: str | None) -> int | None:
    """解析 --since：24h / 7d / 30m / 3600 → unix 秒下限；None 表示不限。"""
    if not s:
        return None
    s = str(s).strip().lower()
    now = int(time.time())
    if s.isdigit():
        return now - int(s)
    m = re.fullmatch(r"(\d+)([smhd])", s)
    if not m:
        raise ValueError(f"无法解析 --since: {s!r}（示例: 24h / 7d / 30m / 3600）")
    n, u = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
    return now - n * mult


def _fmt_ts(ts) -> str:
    if ts is None:
        return "?"
    try:
        t = float(ts)
        if t > 1e12:
            t = t / 1e9 if t > 1e15 else t / 1e3
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    except Exception:
        return str(ts)


def load_provider_id_map(db_path: str) -> dict:
    """provider_id(uuid) -> name；只读。"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        out = {}
        for pid, name in conn.execute("SELECT id, name FROM providers"):
            if pid:
                out[pid] = name or pid
        return out
    finally:
        conn.close()


def resolve_provider_name(pid: str | None, id_map: dict) -> str:
    if not pid:
        return "?"
    if pid in id_map:
        return id_map[pid]
    return f"deleted:{str(pid)[:8]}"


def classify_log_error(status_code: int | None, error_message: str | None) -> str:
    """把 proxy_request_logs 的 status/error_message 映射到 ErrorCategory 字符串。"""
    msg = error_message or ""
    low = msg.lower()
    st = int(status_code or 0)

    # 关键词优先（比纯 status 更准）
    if any(k in low for k in ("invalid api", "missing api", "authentication",
                               "unauthorized", "forbidden")):
        return ErrorCategory.AUTH.value
    if any(k in msg for k in ("余额", "预扣", "额度")) or "insufficient" in low:
        return ErrorCategory.AUTH.value  # 额度/鉴权类，归 authentication
    if any(k in low for k in ("rate limit", "rate_limit", "too many", "429")) or st == 429:
        return ErrorCategory.RATE_LIMIT.value
    if any(k in low for k in ("model_not", "no available channel", "unknown model",
                               "model does not exist")) or st == 404:
        return ErrorCategory.MODEL_NOT_FOUND.value
    if any(k in low for k in ("timeout", "首包超时", "ttft")):
        return ErrorCategory.TTFT_TIMEOUT.value
    if any(k in low for k in ("connect", "连接", "tls", "certificate")) or st in (502, 522, 524):
        return ErrorCategory.NETWORK.value
    if any(k in low for k in ("schema", "invalid request", "maximum prompt",
                               "too large", "413")) or st in (400, 413, 422):
        return ErrorCategory.PROTOCOL_INCOMPATIBLE.value
    if st in (401, 403, 402):
        return ErrorCategory.AUTH.value
    if st >= 500:
        return ErrorCategory.SERVER.value
    if st and st != 200:
        cat = _category_from_status(st)
        if cat is not None:
            return cat.value
    if msg.strip():
        return ErrorCategory.UNKNOWN.value
    return ErrorCategory.NONE.value


def _open_ro(db_path: str):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _table_exists(conn, name: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None


def query_proxy_logs(db_path: str, *, since_ts: int | None = None, limit: int = 20,
                     fails_only: bool = False, provider_substr: str | None = None) -> list:
    """查询 proxy_request_logs，返回 dict 列表（已解析供应商名与 error_category）。"""
    id_map = load_provider_id_map(db_path)
    # reverse name filter: match provider ids whose name contains substr
    name_pids = None
    if provider_substr:
        sub = provider_substr.lower()
        name_pids = {pid for pid, n in id_map.items() if sub in (n or "").lower()}

    conn = _open_ro(db_path)
    try:
        if not _table_exists(conn, "proxy_request_logs"):
            return []
        where = []
        args: list = []
        if since_ts is not None:
            where.append("created_at >= ?")
            args.append(since_ts)
        if fails_only:
            where.append(
                "(status_code IS NULL OR status_code != 200 "
                "OR (error_message IS NOT NULL AND error_message != ''))"
            )
        if name_pids is not None:
            if not name_pids:
                return []
            placeholders = ",".join("?" * len(name_pids))
            where.append(f"provider_id IN ({placeholders})")
            args.extend(name_pids)
        sql = "SELECT * FROM proxy_request_logs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        cols = [c[1] for c in conn.execute("PRAGMA table_info(proxy_request_logs)")]
        rows = []
        for row in conn.execute(sql, args):
            d = dict(zip(cols, row))
            d["provider_name"] = resolve_provider_name(d.get("provider_id"), id_map)
            d["error_category"] = classify_log_error(d.get("status_code"), d.get("error_message"))
            d["routing_mismatch"] = bool(
                d.get("request_model") and d.get("model")
                and d.get("request_model") != d.get("model")
            )
            d["created_at_fmt"] = _fmt_ts(d.get("created_at"))
            rows.append(d)
        return rows
    finally:
        conn.close()


def query_stats(db_path: str, *, since_ts: int | None = None) -> list:
    """按供应商汇总：请求数、成功、失败、主失败因、中位延迟近似、路由不一致率。"""
    id_map = load_provider_id_map(db_path)
    conn = _open_ro(db_path)
    try:
        if not _table_exists(conn, "proxy_request_logs"):
            return []
        where = ""
        args: list = []
        if since_ts is not None:
            where = "WHERE created_at >= ?"
            args.append(since_ts)
        # 拉原始行做聚合（2万级可接受）
        cols = [c[1] for c in conn.execute("PRAGMA table_info(proxy_request_logs)")]
        sql = f"SELECT * FROM proxy_request_logs {where}"
        buckets: dict[str, dict] = {}
        for row in conn.execute(sql, args):
            d = dict(zip(cols, row))
            pid = d.get("provider_id") or "?"
            b = buckets.get(pid)
            if b is None:
                b = {
                    "provider_id": pid,
                    "provider_name": resolve_provider_name(pid, id_map),
                    "total": 0,
                    "ok": 0,
                    "fail": 0,
                    "mismatch": 0,
                    "latencies": [],
                    "fail_cats": {},
                    "status_counts": {},
                }
                buckets[pid] = b
            b["total"] += 1
            st = d.get("status_code")
            err = d.get("error_message")
            is_fail = (st is None or st != 200 or (err is not None and err != ""))
            if is_fail:
                b["fail"] += 1
                cat = classify_log_error(st, err)
                b["fail_cats"][cat] = b["fail_cats"].get(cat, 0) + 1
                key = str(st)
                b["status_counts"][key] = b["status_counts"].get(key, 0) + 1
            else:
                b["ok"] += 1
            if (d.get("request_model") and d.get("model")
                    and d.get("request_model") != d.get("model")):
                b["mismatch"] += 1
            lat = d.get("latency_ms")
            if isinstance(lat, (int, float)) and lat >= 0:
                b["latencies"].append(lat)
        out = []
        for b in buckets.values():
            lats = sorted(b["latencies"])
            med = lats[len(lats) // 2] if lats else None
            top_cat = None
            if b["fail_cats"]:
                top_cat = max(b["fail_cats"].items(), key=lambda x: x[1])[0]
            total = b["total"] or 1
            out.append({
                "provider_id": b["provider_id"],
                "provider_name": b["provider_name"],
                "total": b["total"],
                "ok": b["ok"],
                "fail": b["fail"],
                "success_rate": round(b["ok"] / total, 4),
                "mismatch": b["mismatch"],
                "mismatch_rate": round(b["mismatch"] / total, 4),
                "median_latency_ms": med,
                "top_fail_category": top_cat,
                "fail_categories": b["fail_cats"],
                "status_counts": b["status_counts"],
            })
        out.sort(key=lambda x: (-x["fail"], -x["total"]))
        return out
    finally:
        conn.close()


def query_routing(db_path: str, *, since_ts: int | None = None, limit: int = 20) -> list:
    """静默路由排行：request_model -> model。"""
    conn = _open_ro(db_path)
    try:
        if not _table_exists(conn, "proxy_request_logs"):
            return []
        where = "WHERE request_model IS NOT NULL AND model IS NOT NULL AND request_model != model"
        args: list = []
        if since_ts is not None:
            where += " AND created_at >= ?"
            args.append(since_ts)
        sql = f"""
            SELECT request_model, model, COUNT(*) AS n
            FROM proxy_request_logs
            {where}
            GROUP BY request_model, model
            ORDER BY n DESC
            LIMIT ?
        """
        args.append(int(limit))
        return [
            {"request_model": a, "actual_model": b, "count": n}
            for a, b, n in conn.execute(sql, args)
        ]
    finally:
        conn.close()


def summarize_provider_history(db_path: str, provider_name: str,
                               since_ts: int | None = None) -> dict | None:
    """单个供应商 24h 摘要，供 check/inspect 挂钩。"""
    stats = query_stats(db_path, since_ts=since_ts)
    for s in stats:
        if s["provider_name"] == provider_name:
            return s
    # 模糊
    low = provider_name.lower()
    for s in stats:
        if low in s["provider_name"].lower():
            return s
    return None


def read_log_file_tail(path: str, lines: int = 50, keyword: str | None = None) -> list:
    """读磁盘日志尾部（P3）。大文件只读末尾约 512KB。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return [f"日志文件不存在: {path}"]
    size = p.stat().st_size
    chunk = min(size, 512 * 1024)
    with open(p, "rb") as f:
        if size > chunk:
            f.seek(-chunk, 2)
        data = f.read().decode("utf-8", errors="replace")
    raw_lines = data.splitlines()
    if keyword:
        raw_lines = [ln for ln in raw_lines if keyword.lower() in ln.lower()]
    return raw_lines[-lines:]


def run_history(args, say) -> int:
    """history 子命令。"""
    try:
        since_ts = _parse_since(getattr(args, "since", None))
    except ValueError as e:
        say(str(e))
        return 2
    limit = getattr(args, "limit", 20) or 20
    fails = getattr(args, "fails", False)
    prov = getattr(args, "provider", None) or None
    rows = query_proxy_logs(
        args.db, since_ts=since_ts, limit=limit, fails_only=fails, provider_substr=prov
    )
    report = {
        "schema_version": 1,
        "command": "history",
        "since": getattr(args, "since", None),
        "limit": limit,
        "fails_only": fails,
        "count": len(rows),
        "entries": [
            {
                "created_at": r.get("created_at"),
                "created_at_fmt": r.get("created_at_fmt"),
                "provider_name": r.get("provider_name"),
                "app_type": r.get("app_type"),
                "request_model": r.get("request_model"),
                "model": r.get("model"),
                "status_code": r.get("status_code"),
                "latency_ms": r.get("latency_ms"),
                "first_token_ms": r.get("first_token_ms"),
                "input_tokens": r.get("input_tokens"),
                "output_tokens": r.get("output_tokens"),
                "error_message": r.get("error_message"),
                "error_category": r.get("error_category"),
                "routing_mismatch": r.get("routing_mismatch"),
                "data_source": r.get("data_source"),
            }
            for r in rows
        ],
    }
    # 可选磁盘日志
    log_file = getattr(args, "log_file", None)
    if log_file:
        report["log_file_tail"] = read_log_file_tail(
            log_file, lines=getattr(args, "log_lines", 50) or 50,
            keyword=getattr(args, "log_keyword", None),
        )

    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        say(f"history: {len(rows)} 条"
            f"{'（仅失败）' if fails else ''}"
            f"{' since=' + str(args.since) if getattr(args, 'since', None) else ''}")
        for i, r in enumerate(rows, 1):
            st = r.get("status_code")
            ok = st == 200 and not r.get("error_message")
            flag = "OK" if ok else "FAIL"
            say(f"[{i:02d}] {flag} {r.get('created_at_fmt')}  {r.get('provider_name')}")
            say(f"     {r.get('request_model')} -> {r.get('model')}  "
                f"status={st}  lat={r.get('latency_ms')}ms  ttft={r.get('first_token_ms')}ms")
            if r.get("routing_mismatch"):
                say(f"     !! 路由不一致: {r.get('request_model')} => {r.get('model')}")
            if r.get("error_message"):
                say(f"     [{r.get('error_category')}] {str(r.get('error_message'))[:160]}")
        if log_file and report.get("log_file_tail"):
            say(f"\n--- log file tail: {log_file} ---")
            for ln in report["log_file_tail"]:
                say(ln)
    return 0


def run_stats(args, say) -> int:
    try:
        since_ts = _parse_since(getattr(args, "since", None))
    except ValueError as e:
        say(str(e))
        return 2
    stats = query_stats(args.db, since_ts=since_ts)
    report = {
        "schema_version": 1,
        "command": "stats",
        "since": getattr(args, "since", None),
        "providers": stats,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        say(f"stats: {len(stats)} 个供应商"
            f"{' since=' + str(args.since) if getattr(args, 'since', None) else ''}")
        say(f"{'供应商':24} {'请求':>6} {'成功%':>7} {'失败':>5} {'主失败因':18} "
            f"{'中位延迟':>8} {'路由≠%':>7}")
        say("-" * 90)
        for s in stats:
            rate = f"{s['success_rate']*100:.0f}%"
            med = f"{s['median_latency_ms']:.0f}ms" if s["median_latency_ms"] is not None else "-"
            mm = f"{s['mismatch_rate']*100:.0f}%"
            cat = s.get("top_fail_category") or "-"
            say(f"{s['provider_name'][:24]:24} {s['total']:6d} {rate:>7} {s['fail']:5d} "
                f"{cat[:18]:18} {med:>8} {mm:>7}")
    return 0


def run_routing(args, say) -> int:
    try:
        since_ts = _parse_since(getattr(args, "since", None))
    except ValueError as e:
        say(str(e))
        return 2
    limit = getattr(args, "limit", 20) or 20
    pairs = query_routing(args.db, since_ts=since_ts, limit=limit)
    report = {
        "schema_version": 1,
        "command": "routing",
        "since": getattr(args, "since", None),
        "pairs": pairs,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        say(f"routing: top {len(pairs)} 静默路由"
            f"{' since=' + str(args.since) if getattr(args, 'since', None) else ''}")
        for i, p in enumerate(pairs, 1):
            say(f"[{i:02d}] {p['count']:5d}×  {p['request_model']}  =>  {p['actual_model']}")
    return 0


def run_watch(args, say) -> int:
    """轮询 proxy_request_logs，有新行就打印（Ctrl+C 退出）。"""
    interval = max(1, int(getattr(args, "interval", 3) or 3))
    fails_only = getattr(args, "fails", False)
    prov = getattr(args, "provider", None) or None
    # 起点：当前最新 created_at（避免启动时刷屏历史）
    bootstrap = query_proxy_logs(args.db, limit=1, fails_only=False, provider_substr=prov)
    last_ts = bootstrap[0]["created_at"] if bootstrap else int(time.time())
    seen_ids: set[str] = set()
    if bootstrap:
        rid = bootstrap[0].get("request_id")
        if rid:
            seen_ids.add(str(rid))
    say(f"watch: 每 {interval}s 轮询 proxy_request_logs"
        f"{'（仅失败）' if fails_only else ''}"
        f"{' provider~' + prov if prov else ''}")
    say(f"从 created_at>{last_ts} 开始；Ctrl+C 结束\n")
    try:
        while True:
            # 多取一些，按 id 去重
            rows = query_proxy_logs(
                args.db, since_ts=int(last_ts) if last_ts else None,
                limit=50, fails_only=fails_only, provider_substr=prov,
            )
            # query 是 DESC；反转让旧的先打
            new_rows = []
            for r in reversed(rows):
                rid = str(r.get("request_id") or "")
                cts = r.get("created_at") or 0
                if cts < last_ts:
                    continue
                if rid and rid in seen_ids:
                    continue
                # 同一秒内的新行：允许 cts==last_ts 但 id 未见过
                if cts == last_ts and rid and rid in seen_ids:
                    continue
                new_rows.append(r)
            for r in new_rows:
                rid = str(r.get("request_id") or "")
                if rid:
                    seen_ids.add(rid)
                st = r.get("status_code")
                ok = st == 200 and not r.get("error_message")
                flag = "OK" if ok else "FAIL"
                say(f"[{flag}] {r.get('created_at_fmt')}  {r.get('provider_name')}")
                say(f"  {r.get('request_model')} -> {r.get('model')}  "
                    f"status={st}  lat={r.get('latency_ms')}ms  ttft={r.get('first_token_ms')}ms")
                if r.get("routing_mismatch"):
                    say(f"  !! 路由: {r.get('request_model')} => {r.get('model')}")
                if r.get("error_message"):
                    say(f"  [{r.get('error_category')}] {str(r.get('error_message'))[:160]}")
                if r.get("created_at") and r["created_at"] > last_ts:
                    last_ts = r["created_at"]
            # 防止 seen 无限涨
            if len(seen_ids) > 5000:
                seen_ids = set(list(seen_ids)[-2000:])
            time.sleep(interval)
    except KeyboardInterrupt:
        say("\nwatch 已停止")
        return 0


def format_history_sidebar(db_path: str, provider_name: str, since: str = "24h") -> str:
    """给 check/inspect 附带的一行历史摘要。"""
    try:
        since_ts = _parse_since(since)
    except ValueError:
        since_ts = _parse_since("24h")
    s = summarize_provider_history(db_path, provider_name, since_ts=since_ts)
    if not s:
        return f"  history({since}): 无记录"
    rate = f"{s['success_rate']*100:.0f}%"
    med = f"{s['median_latency_ms']:.0f}ms" if s["median_latency_ms"] is not None else "-"
    mm = f"{s['mismatch_rate']*100:.0f}%"
    cat = s.get("top_fail_category") or "-"
    return (f"  history({since}): 请求{s['total']} 成功{rate} 失败{s['fail']}"
            f" 主因={cat} 中位延迟={med} 路由≠{mm}")


def _inject_default_command(argv: list[str]) -> list[str]:
    """无子命令时注入 check，兼容文档中的「可省略 check」用法。

    例：
      prog --failover-only  →  prog check --failover-only
      prog                  →  prog check
    """
    known = {"check", "list-models", "inspect", "history", "stats", "routing", "watch",
             "-h", "--help"}
    if len(argv) <= 1:
        return argv + ["check"]
    head = argv[1]
    if head in known:
        return argv
    # 以 - 开头的选项 → 默认 check 子命令
    if head.startswith("-"):
        return [argv[0], "check"] + argv[1:]
    return argv


def _build_parser():
    """构造 argparse，公共选项 + 三个子命令：check / list-models / inspect。"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DB_PATH, help=f"cc-switch.db 路径 (默认: {DB_PATH})")
    common.add_argument("--skip-tls-verify", action="store_true",
                        help="危险：跳过 TLS 证书验证，仅用于信任的自签名中转站")
    common.add_argument("--timeout", type=int, default=30, help="单请求超时秒 (默认: 30)")
    common.add_argument("--workers", type=int, default=6, help="并发数 (默认: 6)")
    common.add_argument("--user-agent", default=None,
                        help="覆盖 User-Agent（默认用本机 claude --version 探测的版本）")
    common.add_argument("--probe-max-tokens", type=int, default=20,
                        help="探测请求 max_tokens 预算（默认 20；提高可避免 thinking 模型耗光预算）")
    common.add_argument("--probe-enable-thinking", action="store_true",
                        help="允许探测请求走 thinking 模式（默认禁用，避免 DeepSeek 等 thinking 模型耗光 max_tokens）")

    ap = argparse.ArgumentParser(
        description="CC-Pulse：cc-switch 供应商健康检测与单模型深度诊断",
        parents=[common],
    )
    sub = ap.add_subparsers(dest="command")

    # check：日常健康检测（默认子命令，也可显式写 check）
    p_check = sub.add_parser("check", parents=[common],
                             help="对供应商进行真实问题探测（默认行为）")
    p_check.add_argument("--type", default="claude",
                         choices=["claude", "codex", "openclaw", "all"],
                         help="检测哪类供应商 (默认: claude)")
    p_check.add_argument("--failover-only", action="store_true",
                         help="只测故障转移队列里的供应商（含当前激活的）")
    p_check.add_argument("--json", action="store_true",
                         help="输出结构化 JSON 报告到 stdout（人类可读文本保留到 stderr）")
    p_check.add_argument("--with-history", action="store_true",
                         help="每个供应商探测结果后附加 cc-switch 近 24h 日志摘要")
    p_check.add_argument("--history-since", default="24h",
                         help="--with-history 时间窗口（默认 24h；如 7d / 30m）")

    # list-models：拉取供应商 /v1/models
    p_lm = sub.add_parser("list-models", parents=[common],
                          help="拉取每个供应商实际支持的模型列表（GET /v1/models）")
    p_lm.add_argument("--type", default="claude",
                      choices=["claude", "codex", "openclaw", "all"],
                      help="检测哪类供应商 (默认: claude)")
    p_lm.add_argument("--failover-only", action="store_true",
                      help="只测故障转移队列里的供应商（含当前激活的）")

    # inspect：单一模型深度检测
    p_inspect = sub.add_parser("inspect", parents=[common],
                               help="对单一 (provider, model) 三元组进行深度诊断")
    p_inspect.add_argument("--provider", required=True,
                           help="供应商名称（与 cc-switch 中一致）")
    p_inspect.add_argument("--model", required=True,
                           help="模型 ID（精确匹配，可包含 [1M] 等后缀）")
    p_inspect.add_argument("--source", default="configured",
                           choices=["configured", "listed", "manual"],
                           help="模型来源：configured(cc-switch 配置)、"
                                "listed(供应商 /v1/models 声明)、manual(强制) (默认: configured)")
    p_inspect.add_argument("--type", default="claude",
                           choices=["claude", "codex", "openclaw", "all"],
                           help="限定供应商类型 (默认: claude)")
    p_inspect.add_argument("--keep-suffix", action="store_true",
                           help="保留模型 ID 中的 [1M] 等后缀（默认会去后缀）")
    p_inspect.add_argument("--include", default="text,streaming,model-consistency,protocol,error-classification,metadata,thinking,tools",
                           help="要执行的检查项，逗号分隔；支持：text,streaming,"
                                "model-consistency,protocol,error-classification,metadata,thinking,tools (默认全开)。"
                                "省略某项即跳过其计算/输出")
    p_inspect.add_argument("--ttft-timeout", type=int, default=None,
                           help="流式探测首 token 超时（秒），默认使用 --timeout")
    p_inspect.add_argument("--with-metadata", action="store_true",
                           help="额外发 GET /v1/models/{id} 拉取供应商声明的窗口/能力等元数据（冗余，metadata 已默认开）")
    p_inspect.add_argument("--probe-context", choices=["512k", "1m"], default="512k",
                           help="上下文窗口探测档位：512k（默认）或 1m；仅在元数据无声明时触发")
    p_inspect.add_argument("--human", action="store_true",
                           help="以人类可读格式输出到 stdout（默认 JSON）")
    p_inspect.add_argument("--with-history", action="store_true",
                           help="报告中附加该供应商近 24h 日志摘要")
    p_inspect.add_argument("--history-since", default="24h",
                           help="--with-history 时间窗口（默认 24h）")

    # history / stats / routing：只读日志，不发 HTTP
    p_hist = sub.add_parser("history", parents=[common],
                            help="读取 cc-switch 代理请求日志（最近 N 条）")
    p_hist.add_argument("--limit", type=int, default=20, help="条数（默认 20）")
    p_hist.add_argument("--fails", action="store_true", help="只显示失败记录")
    p_hist.add_argument("--since", default=None, help="时间窗口：24h / 7d / 30m / 秒数")
    p_hist.add_argument("--provider", default=None, help="按供应商名子串过滤")
    p_hist.add_argument("--json", action="store_true", help="JSON 输出")
    p_hist.add_argument("--log-file", default=None,
                        help="可选：额外打印磁盘日志尾部（如 ~/.cc-switch/logs/cc-switch.log）")
    p_hist.add_argument("--log-lines", type=int, default=50, help="磁盘日志尾部行数（默认 50）")
    p_hist.add_argument("--log-keyword", default=None, help="磁盘日志关键词过滤")

    p_stats = sub.add_parser("stats", parents=[common],
                             help="按供应商汇总成功率/延迟/路由不一致")
    p_stats.add_argument("--since", default="7d", help="时间窗口（默认 7d）")
    p_stats.add_argument("--json", action="store_true", help="JSON 输出")

    p_route = sub.add_parser("routing", parents=[common],
                             help="静默路由排行（request_model => actual model）")
    p_route.add_argument("--since", default="7d", help="时间窗口（默认 7d）")
    p_route.add_argument("--limit", type=int, default=20, help="显示条数（默认 20）")
    p_route.add_argument("--json", action="store_true", help="JSON 输出")

    p_watch = sub.add_parser("watch", parents=[common],
                             help="实时轮询 proxy_request_logs，有新记录就打印（Ctrl+C 结束）")
    p_watch.add_argument("--interval", type=int, default=3, help="轮询间隔秒（默认 3）")
    p_watch.add_argument("--fails", action="store_true", help="只显示失败")
    p_watch.add_argument("--provider", default=None, help="按供应商名子串过滤")

    # 兜底默认（注入 check 后子解析器会覆盖这些）
    ap.set_defaults(command="check", type="claude", failover_only=False, json=False)
    return ap, common, p_check, p_lm, p_inspect, p_hist, p_stats, p_route, p_watch


def main():
    sys.argv = _inject_default_command(sys.argv)
    ap, *_ = _build_parser()
    args = ap.parse_args()

    global _human_out
    # JSON 输出时：人类可读走 stderr，stdout 仅承载 JSON
    if getattr(args, "json", False) or (
            args.command == "inspect" and not getattr(args, "human", False)):
        _human_out = sys.stderr

    if getattr(args, "user_agent", None):
        say(f"User-Agent 已覆盖: {args.user_agent}")

    if args.skip_tls_verify:
        say("警告：已跳过 TLS 证书验证，认证凭据可能遭中间人截获。")

    if not Path(args.db).exists():
        say(f"数据库不存在: {args.db}")
        return 2

    # 纯日志子命令：不加载 providers、不发 HTTP
    if args.command == "history":
        return run_history(args, say)
    if args.command == "stats":
        return run_stats(args, say)
    if args.command == "routing":
        return run_routing(args, say)
    if args.command == "watch":
        return run_watch(args, say)

    types = ["claude", "codex", "openclaw"] if getattr(args, "type", "claude") == "all" else [getattr(args, "type", "claude")]
    providers = []
    for t in types:
        providers.extend(load_providers(args.db, t))

    if getattr(args, "failover_only", False) and providers:
        before = len(providers)
        providers = [p for p in providers if p.in_failover or p.is_current]
        say(f"--failover-only: {before} → {len(providers)}（只保留队列内+当前激活）")

    if args.command == "list-models":
        if not providers:
            say("没有符合条件的供应商")
            return 2
        return run_list_models(args, providers, say)

    if args.command == "check":
        if not providers:
            say("没有符合条件的供应商")
            return 2
        return run_health_check(args, providers, say)

    if args.command == "inspect":
        return run_inspect(args, providers, say)

    say(f"未知子命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

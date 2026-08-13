"""CC-Pulse 探测层：probe/stream/metadata/tools/vision 与协议构造。

从 check_ccswitch_health.py 拆出；依赖 ccpulse_net + ccpulse_output。
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import TypedDict

from ccpulse_net import (
    ErrorCategory,
    HttpResponse,
    STREAM_DONE_MARKERS,
    StreamEvent,
    _error_category_for_urlerror,
    _http_request,
    _is_tls_error,
    _process_sse_event,
    _read_httperror_body,
    _sanitize_display,
    _sanitize_raw_body,
    _sse_event_to_dict,
    classify_error,
    create_ssl_context,
    parse_sse_lines,
)
from ccpulse_output import _c, _output_stream, _pad, _sanitize_for_terminal, _say_colored, say

# 默认兜底的 claude-cli 版本（读取本机版本失败时用）
_DEFAULT_CLAUDE_CLI_VERSION = "2.1.44"
# 本机 claude-cli 版本缓存（懒加载）；有锁保护的幂等懒缓存
_CLAUDE_CLI_VERSION_CACHE: str | None = None
_CLAUDE_VERSION_LOCK = threading.Lock()

# --- from check_ccswitch_health.py:93-327 ---
def _detect_claude_cli_version() -> str:
    """读取本机 `claude --version`，让 User-Agent 跟随真实版本。

    muyuan.do 等中转站会校验 claude-cli 版本，写死旧版本会被拒（403）。
    失败时回退 _DEFAULT_CLAUDE_CLI_VERSION。
    """
    try:
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
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


def _claude_code_headers(
    user_agent: str | None = None, stainless_version: str | None = None
) -> dict:
    """构造 Claude Code 指纹头（User-Agent 动态，stainless 版本可覆盖）。"""
    return {
        "User-Agent": _user_agent(user_agent),
        "x-app": "cli",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
        "x-stainless-lang": "js",
        "x-stainless-package-version": stainless_version or _STAINLESS_PACKAGE_VERSION,
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v24.3.0",
        "x-stainless-arch": "x64",
        "x-stainless-os": "Windows",
    }


def _is_thinking_prone_model(model_id: str) -> bool:
    """模型是否倾向走 thinking/reasoning（据此决定是否发思考抑制字段）。"""
    return bool(model_id and _THINKING_PRONE_RE.search(model_id))


def _answer_correct(answer: str, expected: str) -> bool:
    """宽松校验回答是否正确。

    max_tokens 提到 1024 后，话痨模型可能回「答案是 5」而非纯「5」，
    精确匹配会误判。策略：先 strip 精确匹配；否则当期望为数值时，
    从回答里提取所有数字（含小数/科学计数/负数），唯一且数值相等即通过。
    """
    a = (answer or "").strip()
    exp = (expected or "").strip()
    if a == exp:
        return True
    # 期望是否为数值（含整数/小数/负数/科学计数）
    try:
        exp_val = float(exp)
    except ValueError:
        return False
    # 提取回答中的数值（含小数、科学计数、负数），唯一且数值相等即通过
    nums = re.findall(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", a)
    if len(nums) == 1:
        try:
            if float(nums[0]) == exp_val:
                return True
        except ValueError:
            pass
    return False


# 探测用的真实问题（验证模型能否真正回答，而非只测连通）
# 兜底默认（问题池不可用/显式指定时用）
PROBE_QUESTION = "What is 2+3? Reply with only the number, nothing else."
EXPECTED_ANSWER = "5"

# 问题池：每次探测随机抽一条，避免固定 prompt 被中转站按子串识别成测活脚本。
# 答案均为单一确定值（数字/单词），配合 _answer_correct 宽松匹配。
PROBE_PROMPTS = [
    {"q": "What is 2+3? Reply with only the number, nothing else.", "a": "5"},
    {"q": "What is 7+6? Reply with only the number.", "a": "13"},
    {"q": "计算 4 加 5，只回答数字。", "a": "9"},
    {"q": "What is 9 minus 4? Answer with the number only.", "a": "5"},
    {"q": "What is 3 times 4? Just the number.", "a": "12"},
    {"q": "8 加 7 等于多少？只回数字。", "a": "15"},
    {"q": "What is 20 divided by 5? Number only.", "a": "4"},
    {"q": "What is 6+8? Reply with just the number.", "a": "14"},
    {"q": "计算 12 减 7，只给数字。", "a": "5"},
    {"q": "What is 5 times 3? Only the number, please.", "a": "15"},
]

# max_tokens：设为自然值（1024）而非刺眼的 20。这是上限非实际消耗，
# 回答一个数字仍只按实际输出计费；避免「20」这一测活脚本典型信号。
PROBE_MAX_TOKENS = 1024

# 会走 thinking/reasoning 的模型（保守匹配）：仅对这些模型发思考抑制字段，
# 普通 claude 模型不发（更贴近真实 claude-cli，减少指纹）。
_THINKING_PRONE_RE = re.compile(
    r"deepseek|glm|qwq|reasoner|(?:^|[-_/])r1(?:$|[-_])|qwen.*(?:think|reason)",
    re.IGNORECASE,
)

# stealth 时序伪装参数
STEALTH_MAX_WORKERS = 3  # 隐身模式并发上限
STEALTH_JITTER_MIN = 0.3  # 每档请求前随机延迟下界（秒）
STEALTH_JITTER_MAX = 1.5  # 上界

# x-stainless-* 指纹头版本（无法从 claude --version 推导 SDK 版本，可 --stainless-version 覆盖）
_STAINLESS_PACKAGE_VERSION = "0.74.0"

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


@dataclass(frozen=True)
class ModelTier:
    tier: str  # haiku/sonnet/opus/fable/default
    model: str  # 干净模型名（已去 [1M]）
    raw_model: str  # 原始模型名


# 协议类型枚举：取代 is_openrouter 的粗粒度推断，用显式协议控制请求格式
# 修复：L站0730 等实际 OpenAI 格式但被标为 claude 类型的供应商


class Protocol(str, Enum):
    ANTHROPIC_MESSAGES = "anthropic_messages"  # /v1/messages
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"  # /v1/chat/completions（含 openrouter / openclaw 兼容）
    OPENAI_RESPONSES = "openai_responses"  # /responses（codex / 部分自定义）
    UNKNOWN = "unknown"


class ProbeTierResult(TypedDict, total=False):
    tier: str
    model: str
    status: int
    elapsed: float
    error: str
    error_category: str | None
    answer: str
    correct: bool
    raw_body: str


class ProbeStreamResult(TypedDict, total=False):
    status: str  # "pass" / "fail" / "error"
    http_status: int
    elapsed_seconds: float
    ttft_seconds: float | None
    event_count: int
    content_type: str
    response_model: str
    error: str
    error_category: str | None


class FetchModelsResult(TypedDict, total=False):
    name: str
    base_url: str
    status: int
    elapsed: float
    error: str
    error_category: str | None
    models: list[str]


class InspectDimensionResult(TypedDict, total=False):
    status: str  # "pass" / "fail" / "error" / "skipped"
    elapsed_seconds: float
    answer: str
    correct: bool
    error: str
    error_category: str | None


class HistoryResult(TypedDict, total=False):
    total: int
    success: int
    fail: int
    success_rate: float
    top_fail_category: str | None
    mismatch_rate: float


@dataclass(frozen=True)
class Provider:
    name: str
    app_type: str
    base_url: str
    api_key: str
    auth_mode: str
    tiers: list = field(default_factory=list)
    is_current: bool = False
    in_failover: bool = False
    is_openrouter: bool = False  # 向后兼容：base 含 /chat/completions 时为 True
    protocol: Protocol = Protocol.UNKNOWN  # 权威协议（优先于 app_type 默认推断）
    # protocol_source: "api_format"=cc-switch meta.apiFormat 显式配置 / "url_suffix"=base_url 后缀
    # / ""=app_type 默认或未知；仅影响 detect_protocol 的 confidence 描述
    protocol_source: str = ""
    # custom_user_agent: cc-switch meta.customUserAgent（如 codex_cli/0.144.0），
    # 该供应商探测时的默认 UA；--user-agent CLI 参数优先级更高
    custom_user_agent: str | None = None
    # notes: cc-switch providers.notes（用户自记的限流/宕机等运营笔记）
    notes: str = ""


# --- from check_ccswitch_health.py:526-2196 ---
def build_auth_headers(p: Provider) -> dict:
    """按 auth_mode 只发一个认证头（和真实 Claude Code 一致）"""
    if p.auth_mode == "apikey":
        return {"x-api-key": p.api_key}
    else:  # authtoken / bearer
        return {"Authorization": f"Bearer {p.api_key}"}


def _resolve_protocol(p: Provider) -> Protocol:
    """从供应商属性解析出权威协议枚举。"""
    proto = getattr(p, "protocol", Protocol.UNKNOWN)
    if isinstance(proto, str):
        proto = (
            Protocol(proto)
            if proto in Protocol._value2member_map_
            else Protocol.UNKNOWN
        )
    if proto == Protocol.UNKNOWN:
        # 无 apiFormat 时按 app_type 推断；claude/未知 app_type 保持 UNKNOWN，
        # 由 _build_proto_url / _build_proto_payload 以 ANTHROPIC_MESSAGES 处理。
        if p.is_openrouter:
            proto = Protocol.OPENAI_CHAT_COMPLETIONS
        elif p.app_type == "codex":
            proto = Protocol.OPENAI_RESPONSES
        elif p.app_type == "openclaw":
            proto = Protocol.OPENAI_CHAT_COMPLETIONS
        elif p.app_type not in ("claude",):
            proto = Protocol.ANTHROPIC_MESSAGES
    return proto


def _build_proto_url(p: Provider, proto: Protocol) -> str:
    """按协议类型构造请求 URL。"""
    base = p.base_url.rstrip("/")
    if proto == Protocol.ANTHROPIC_MESSAGES:
        return base + "/v1/messages"
    if proto == Protocol.OPENAI_CHAT_COMPLETIONS:
        # openclaw 用 /chat/completions（无 /v1/ 前缀）
        if p.app_type == "openclaw" or p.is_openrouter:
            return (
                base
                if base.endswith("/chat/completions")
                else base + "/chat/completions"
            )
        return (
            base
            if base.endswith("/chat/completions")
            else base + "/v1/chat/completions"
        )
    if proto == Protocol.OPENAI_RESPONSES:
        return base if base.endswith("/responses") else base + "/responses"
    # UNKNOWN → Anthropic 兼容
    return base + "/v1/messages"


# TODO: probe_context_smoke / _probe_tools / _probe_vision 三处也有内联 payload 构造，
# 未来应统一到 _build_proto_payload（需先统一 headers/auth 参数接口）。
def _build_proto_payload(
    proto: Protocol,
    model_id: str,
    raw_model: str,
    question: str,
    *,
    max_tokens: int,
    stream: bool,
    suppress: bool,
    temperature: float | None = None,
) -> dict:
    """按协议类型构造 payload dict（不含 headers/body/encode）。"""
    if proto == Protocol.ANTHROPIC_MESSAGES:
        payload = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": question}],
        }
        if stream:
            payload["stream"] = True
        if suppress:
            payload["thinking"] = {"type": "disabled"}
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    if proto == Protocol.OPENAI_CHAT_COMPLETIONS:
        payload = {
            "model": raw_model or model_id,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": question}],
        }
        if stream:
            payload["stream"] = True
        if suppress:
            payload["reasoning_effort"] = "none"
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    if proto == Protocol.OPENAI_RESPONSES:
        payload = {
            "model": model_id,
            "max_output_tokens": max_tokens,
            "input": question,
        }
        if stream:
            payload["stream"] = True
        if suppress:
            payload["reasoning"] = {"effort": "minimal"}
        # Responses 协议不支持 temperature，跳过
        return payload

    # Protocol.UNKNOWN → 最小 Anthropic 兼容
    payload = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": question}],
    }
    if stream:
        payload["stream"] = True
    if suppress:
        payload["thinking"] = {"type": "disabled"}
    if temperature is not None:
        payload["temperature"] = temperature
    return payload


def build_probe_request(
    p: Provider,
    tier: ModelTier,
    stream: bool = False,
    max_tokens: int = PROBE_MAX_TOKENS,
    disable_thinking: bool = True,
    user_agent: str | None = None,
    question: str = PROBE_QUESTION,
    stainless_version: str | None = None,
    temperature: float | None = None,
) -> tuple:
    """构造 (url, method, headers, body)，发真实问题，路径不去重。

    stream=True 时为协议体加 stream 字段（Anthropic/OpenAI 兼容）。
    disable_thinking=True（默认）仅对 thinking-prone 模型（DeepSeek/GLM 等，
    见 _is_thinking_prone_model）发思考抑制字段，避免其耗光 max_tokens 而无
    最终答案；普通 claude 模型不发该字段，更贴近真实 claude-cli，减少指纹。
    question 为实际探测问题（来自问题池随机抽取）；max_tokens 允许调高预算。
    temperature 非 None 时按协议加 temperature 字段（缓存回放探测用）；None 不发。
    """
    # UA 优先级：CLI --user-agent > 供应商 meta.customUserAgent > 本机 claude-cli 版本
    user_agent = user_agent or p.custom_user_agent

    auth_h = build_auth_headers(p)
    suppress = disable_thinking and _is_thinking_prone_model(tier.model)
    proto = _resolve_protocol(p)

    url = _build_proto_url(p, proto)
    payload = _build_proto_payload(
        proto,
        tier.model,
        tier.raw_model,
        question,
        max_tokens=max_tokens,
        stream=stream,
        suppress=suppress,
        temperature=temperature,
    )
    body = json.dumps(payload).encode()

    if proto == Protocol.OPENAI_RESPONSES:
        headers = {
            "User-Agent": _user_agent(user_agent),
            **auth_h,
            "Content-Type": "application/json",
        }
    else:
        headers = {
            **_claude_code_headers(user_agent, stainless_version),
            **auth_h,
            "Content-Type": "application/json",
        }
    return url, "POST", headers, body


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
    if any(
        k in low
        for k in (
            '"type":"thinking"',
            '"thinking"',
            "reasoning_content",
            '"reasoning"',
            "reasoning_effort",
        )
    ):
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
    msg = j.get("choices") or [{}]
    if msg and isinstance(msg[0], dict):
        m = msg[0].get("message") or {}
        if isinstance(m, dict) and (m.get("reasoning_content") or m.get("reasoning")):
            return True
    return False


# ── 模型保真鉴别 P0：换芯字段检测 + thinking 签名提取 ────────────────

# Anthropic 响应里本不该出现的 OpenAI 专属字段（出现=中转套壳穿帮）
_OPENAI_ONLY_FIELDS = ("system_fingerprint", "prompt_tokens", "completion_tokens")
# OpenAI 响应里本不该出现的 Anthropic 专属 usage 字段
_ANTHROPIC_ONLY_USAGE_FIELDS = (
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "input_tokens",
    "output_tokens",
)


def _detect_crosspack_fields(resp_body: str, app_type: str) -> dict:
    """换芯字段检测：响应里出现"本协议不该有"的字段 → 中转把请求偷转给异协议后端。

    原理借鉴 veridrop（AGPL-3.0）：原生 OpenAI usage 只有 prompt/completion/total
    三键；冒出 cache_creation_input_tokens 等 Anthropic 专属字段 → 后端是 Claude。
    反之 Anthropic 响应出现 system_fingerprint → 后端是 OpenAI 系。
    纯 JSON 解析，零依赖，一次请求即可（复用已有响应体）。
    """
    empty = {"suspicious": False, "findings": []}
    if not resp_body:
        return {**empty, "note": "空响应体，无法解析"}
    try:
        j = json.loads(resp_body)
    except (json.JSONDecodeError, TypeError):
        return {**empty, "note": "响应非 JSON，无法解析"}
    if not isinstance(j, dict):
        return {**empty, "note": "响应非对象，无法解析"}

    is_openai = app_type in ("codex", "openclaw")
    is_anthropic = app_type == "claude"
    findings: list[dict] = []

    usage = j.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    if is_openai:
        # OpenAI 响应里出现 Anthropic 专属 usage 字段 → 后端疑似 Claude
        for f in _ANTHROPIC_ONLY_USAGE_FIELDS:
            if f in usage:
                findings.append(
                    {"field": f"usage.{f}", "reason": "Anthropic 专属字段出现在 OpenAI 格式响应，疑似后端换芯为 Claude"}
                )
        # 顶层 usage_source 自曝来源
        if isinstance(j.get("usage_source"), str) and "anthropic" in j["usage_source"].lower():
            findings.append(
                {"field": "usage_source", "reason": "中转自曝后端来源为 anthropic"}
            )
        # model 字段含 claude（OpenAI 响应回 claude 模型名）
        m = j.get("model")
        if isinstance(m, str) and "claude" in m.lower():
            findings.append(
                {"field": "model", "reason": "OpenAI 协议响应回 Claude 模型名，疑似后端换芯"}
            )
    elif is_anthropic:
        # Anthropic 响应里出现 OpenAI 专属字段 → 后端疑似 OpenAI 系
        for f in _OPENAI_ONLY_FIELDS:
            if f in j or f in usage:
                loc = "usage." + f if f in usage else f
                findings.append(
                    {"field": loc, "reason": "OpenAI 专属字段出现在 Anthropic 格式响应，疑似后端换芯为 OpenAI 系"}
                )
        m = j.get("model")
        if isinstance(m, str) and ("gpt" in m.lower() or "o1" in m.lower() or "o3" in m.lower()):
            findings.append(
                {"field": "model", "reason": "Anthropic 协议响应回 GPT/o 系模型名，疑似后端换芯"}
            )

    return {"suspicious": bool(findings), "findings": findings}


def _extract_thinking_signatures(resp_body: str) -> list[dict]:
    """从响应提取 thinking/reasoning 块的签名信息。

    Anthropic：content[].type == "thinking" 且带 signature（服务端密钥签名，不可伪造）。
    OpenAI：choices[].message.reasoning_content（无签名 → signature_present=False）。
    返回每块的 {signature_present, signature_length}；无 thinking 块 → 空列表。
    """
    if not resp_body:
        return []
    try:
        j = json.loads(resp_body)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(j, dict):
        return []

    out: list[dict] = []
    # Anthropic content[]
    for blk in j.get("content", []) or []:
        if isinstance(blk, dict) and blk.get("type") == "thinking":
            sig = blk.get("signature")
            out.append(
                {
                    "signature_present": bool(sig),
                    "signature_length": len(sig) if isinstance(sig, str) else 0,
                }
            )
    # OpenAI choices[].message.reasoning_content（无签名字段）
    if not out:
        for ch in j.get("choices", []) or []:
            if not isinstance(ch, dict):
                continue
            msg = ch.get("message") or {}
            if isinstance(msg, dict) and (msg.get("reasoning_content") or msg.get("reasoning")):
                out.append({"signature_present": False, "signature_length": 0})
    return out


def _has_valid_thinking_signature(sigs: list[dict]) -> bool | None:
    """汇总：任一 thinking 块有签名 → True；有块但全无签名 → False；无块 → None。"""
    if not sigs:
        return None
    return any(s.get("signature_present") for s in sigs)


def _check_usage_consistency(usage: dict, answer: str, app_type: str) -> dict:
    """usage 自洽静态校验：检测计费注水（total 伪造、output 虚高无内容）。

    纯静态分析已有 usage + answer，零新请求。
    - OpenAI 系有 total_tokens 时校验 total == prompt + completion（允许 ±1 误差）
    - output_tokens > 0 但 answer 文本为空 -> 扣费但无输出
    """
    empty = {"suspicious": False, "findings": []}
    if not isinstance(usage, dict) or not usage.get("present"):
        return {**empty, "note": "无 usage"}
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    findings: list[dict] = []

    total = usage.get("total_tokens")
    if (
        app_type in ("codex", "openclaw")
        and isinstance(total, (int, float))
        and isinstance(inp, (int, float))
        and isinstance(out, (int, float))
        and abs(total - (inp + out)) > 1
    ):
        findings.append(
            {
                "check": "total_inconsistent",
                "reason": f"total_tokens({int(total)}) != prompt({int(inp)})+completion({int(out)})={int(inp + out)}，疑似 usage 伪造",
            }
        )

    if isinstance(out, (int, float)) and out > 0 and not str(answer or "").strip():
        findings.append(
            {
                "check": "output_no_content",
                "reason": f"output_tokens={int(out)} 但回答文本为空，疑似扣费注水",
            }
        )

    return {"suspicious": bool(findings), "findings": findings}


def _probe_cache_replay(
    p: Provider,
    tier: ModelTier,
    timeout: int,
    skip_tls: bool,
    *,
    max_tokens: int,
    user_agent: str | None,
) -> dict:
    """缓存回放/钳温双发探测：temp=1 发两次同一 prompt，逐字相同 -> 疑似缓存或钳温。

    正常 temp=1 下同一 prompt 两次请求应有随机性差异；逐字完全相同是强可疑信号。
    用固定 question（不随机），max_tokens 限低（16）省 token。
    返回 {suspicious, identical, note, first_answer, second_answer}。
    """
    fixed_q = PROBE_PROMPTS[0]["q"] if PROBE_PROMPTS else "1+4="
    r1 = probe_tier(
        p, tier, timeout, skip_tls, max_tokens=max_tokens,
        disable_thinking=True, user_agent=user_agent,
        temperature=1.0, question=fixed_q,
    )
    r2 = probe_tier(
        p, tier, timeout, skip_tls, max_tokens=max_tokens,
        disable_thinking=True, user_agent=user_agent,
        temperature=1.0, question=fixed_q,
    )
    a1 = str(r1.get("answer", "") or "")
    a2 = str(r2.get("answer", "") or "")
    if r1.get("status") != 200 or r2.get("status") != 200:
        return {
            "suspicious": False,
            "identical": None,
            "note": f"探测失败无法判定（status={r1.get('status')}/{r2.get('status')}）",
            "first_answer": a1,
            "second_answer": a2,
        }
    identical = a1 == a2 and a1 != ""
    return {
        "suspicious": identical,
        "identical": identical,
        "note": "temp=1 双发逐字相同，疑似缓存回放或强制钳温" if identical else "双发答案不同，无缓存回放迹象",
        "first_answer": a1,
        "second_answer": a2,
    }


def _authenticity_verdict(auth: dict) -> tuple[str, list[str]]:
    """汇总保真判据 → (verdict, evidence)。

    verdict: clean / suspicious / inconclusive。
    结论是概率信号非铁证（GhostPrint 攻击理论上可骗过）。
    """
    crosspack = auth.get("crosspack") or {}
    tsig = auth.get("thinking_signature") or {}
    usage_c = auth.get("usage_consistency") or {}
    cache_r = auth.get("cache_replay") or {}
    cp_susp = bool(crosspack.get("suspicious"))
    uc_susp = bool(usage_c.get("suspicious"))
    cr_susp = bool(cache_r.get("suspicious"))
    has_sig = tsig.get("has_valid_signature")  # True/False/None

    evidence: list[str] = []
    for f in crosspack.get("findings", []) or []:
        evidence.append(f"换芯疑似：{f.get('field')} — {f.get('reason')}")
    if has_sig is False:
        evidence.append("thinking 块无签名，疑似非真 Claude 服务端产出或第三方模型伪装")
    for f in usage_c.get("findings", []) or []:
        evidence.append(f"计费疑似：{f.get('check')} - {f.get('reason')}")
    if cr_susp:
        evidence.append(f"缓存/钳温疑似：{cache_r.get('note', '')}")

    if cp_susp or has_sig is False or uc_susp or cr_susp:
        return "suspicious", evidence
    if has_sig is None and not crosspack:
        return "inconclusive", evidence
    if has_sig is None:
        # 换芯干净但无 thinking 块（非 Claude 协议或未触发）→ 不足以证伪，标 inconclusive
        return "inconclusive", evidence
    return "clean", evidence



def _drain_non_sse_stream(resp, p: Provider) -> dict:
    """消费非 SSE 响应（供应商对 stream=true 仍返回普通 JSON）。

    返回 {text, response_model, raw_preview}。
    """
    raw_truncated_by_error = False
    try:
        raw = resp.read(1 << 20)  # 限长 1MB，防无界读
    except (OSError, ValueError):
        raw = b""
        raw_truncated_by_error = True
    except Exception as exc:
        partial = getattr(exc, "partial", None)
        raw = partial if isinstance(partial, bytes) else b""
        raw_truncated_by_error = True
    stream_truncated = raw_truncated_by_error
    if len(raw) == 1 << 20:
        # 已到限长：先查 Content-Length 判断是否已读完
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) <= (1 << 20):
            stream_truncated = False  # 声明长度 ≤ 已读，未截断
        else:
            try:
                stream_truncated = bool(resp.read(1))
            except (OSError, ValueError):
                stream_truncated = True
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
    return {
        "text": text,
        "response_model": response_model,
        "raw_preview": bytes(raw)[:200],
        "stream_truncated": stream_truncated,
    }


def _drain_sse_stream(
    resp, p: Provider, proto_name: str, ttft_deadline: float | None, start: float
) -> dict:
    """读取 SSE 流并聚合结果。"""
    first_event_at = None
    event_count = 0
    response_model = None
    response_text_buf: list[str] = []
    usage_values = {"input_tokens": None, "output_tokens": None}
    got_done = False
    sse_done = False
    stream_truncated = False
    event_too_large = False
    raw_buf = bytearray()
    stream_socket = getattr(
        getattr(getattr(resp, "fp", None), "raw", None), "_sock", None
    )
    original_socket_timeout = (
        stream_socket.gettimeout() if stream_socket is not None else None
    )
    if stream_socket is not None and ttft_deadline is not None:
        stream_socket.settimeout(ttft_deadline)

    done_marker_field, done_marker_value = STREAM_DONE_MARKERS.get(
        proto_name, ("event", "message_stop")
    )

    def _on_event(ev: StreamEvent):
        nonlocal first_event_at, event_count, response_model, got_done
        if first_event_at is None and ev.get("kind") in (
            "first_chunk",
            "message_start",
            "text_delta",
        ):
            first_event_at = time.time()
            # TTFT 已达标：恢复原始 socket 超时，避免首事件后残余短超时误杀慢流
            if stream_socket is not None and ttft_deadline is not None:
                stream_socket.settimeout(original_socket_timeout)
        if ev.get("kind") == "first_chunk":
            event_count += 1
            return
        event_count += 1
        if ev.get("model"):
            response_model = ev["model"]
        raw = ev.get("raw")
        if isinstance(raw, dict):
            usage = (
                raw.get("usage")
                or (raw.get("message") or {}).get("usage")
                or (raw.get("response") or {}).get("usage")
            )
            if isinstance(usage, dict):
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
                usage_values["input_tokens"] = (
                    input_tokens
                    if input_tokens is not None
                    else usage.get("prompt_tokens", usage_values["input_tokens"])
                )
                usage_values["output_tokens"] = (
                    output_tokens
                    if output_tokens is not None
                    else usage.get("completion_tokens", usage_values["output_tokens"])
                )
        if ev.get("kind") == "text_delta" and ev.get("text_delta"):
            response_text_buf.append(ev["text_delta"])
        if ev.get("kind") == "done":
            got_done = True

    def _process(event_bytes: bytes):
        nonlocal sse_done
        # _process_sse_event 通过 _on_event 统一更新 first_event_at /
        # response_model / event_count / response_text_buf / got_done，
        # 这里不再重复解析（避免双实现分叉）。
        _process_sse_event(
            event_bytes,
            proto_name,
            _on_event,
            done_marker_field,
            done_marker_value,
            response_text_buf,
        )
        if got_done:
            sse_done = True

    def _take_event(buf: bytes) -> tuple:
        # 与 parse_sse_lines._try_take_event 保持一致的分隔符支持
        for sep in (b"\r\n\r\n", b"\n\n", b"\r\r"):
            idx = buf.find(sep)
            if idx != -1:
                return buf[:idx], buf[idx + len(sep) :]
        return None, buf

    sse_buffer = b""
    try:
        stream_iter = iter(resp)
        while not sse_done:
            if (
                first_event_at is None
                and ttft_deadline is not None
                and stream_socket is not None
            ):
                remaining = ttft_deadline - (time.time() - start)
                if remaining <= 0:
                    raise TimeoutError("ttft_timeout")
                stream_socket.settimeout(remaining)
            try:
                line = next(stream_iter)
            except StopIteration:
                if not sse_done:
                    stream_truncated = True  # 连接提前关闭，未收到完成标记
                break
            if (
                first_event_at is None
                and ttft_deadline is not None
                and time.time() - start > ttft_deadline
            ):
                raise TimeoutError("ttft_timeout")
            if isinstance(line, bytes):
                sse_buffer += line
            else:
                sse_buffer += line.encode("utf-8", errors="replace")
            if len(sse_buffer) > 65536:
                # 事件过大（>64KB 未找到 SSE 分隔符）：丢弃当前待处理事件，
                # 避免截断后误解析残缺 JSON。标记 stream_truncated 让调用方感知。
                stream_truncated = True
                event_too_large = True
                sse_buffer = b""

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
    except TimeoutError as e:
        if first_event_at is None and ttft_deadline is not None:
            raise TimeoutError("ttft_timeout") from e
        raise
    finally:
        if stream_socket is not None:
            stream_socket.settimeout(original_socket_timeout)

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

    missing_fields = [field for field, value in usage_values.items() if value is None]
    usage = {
        "present": any(value is not None for value in usage_values.values()),
        "input_tokens": usage_values["input_tokens"],
        "output_tokens": usage_values["output_tokens"],
        "source": "stream_events"
        if any(value is not None for value in usage_values.values())
        else None,
        "missing_fields": missing_fields,
    }
    return {
        "first_event_at": first_event_at,
        "event_count": event_count,
        "response_model": response_model,
        "text": "".join(response_text_buf),
        "raw_preview": bytes(raw_buf),
        "usage": usage,
        "stream_truncated": stream_truncated,
        "event_too_large": event_too_large,
    }


def probe_stream(
    p: Provider,
    tier: ModelTier,
    timeout: int,
    skip_tls_verify: bool,
    ttft_timeout: int | None = None,
    max_tokens: int = PROBE_MAX_TOKENS,
    disable_thinking: bool = True,
    user_agent: str | None = None,
    question: str = PROBE_QUESTION,
    expected: str = EXPECTED_ANSWER,
) -> dict:
    """对单个档位进行流式探测。

    主编排：建连接 -> 区分 SSE/非SSE -> 委托 _drain_* -> 错误归一化 -> 状态判定。
    返回字段：status / http_status / elapsed_seconds / ttft_seconds /
              response_model / content_type / event_count / text / is_sse /
              error_category / error / raw_preview。
    """
    url, method, headers, body = build_probe_request(
        p,
        tier,
        stream=True,
        max_tokens=max_tokens,
        disable_thinking=disable_thinking,
        user_agent=user_agent,
        question=question,
    )
    if not url:
        return {
            "status": "error",
            "elapsed_seconds": 0,
            "ttft_seconds": None,
            "response_model": None,
            "content_type": None,
            "event_count": 0,
            "text": "",
            "error": "无法构造请求 URL",
            "error_category": ErrorCategory.UNKNOWN.value,
        }

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
    usage = extract_usage("")
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
                usage = extract_usage(
                    raw_preview.decode("utf-8", errors="replace") if raw_preview else ""
                )
                if drain["stream_truncated"]:
                    error_category = ErrorCategory.STREAM_INCOMPLETE.value
                    error_msg = "流式响应不完整"
            else:
                proto_name = detect_protocol(p)["detected"]
                ttft_deadline = ttft_timeout if ttft_timeout is not None else None
                drain = _drain_sse_stream(resp, p, proto_name, ttft_deadline, start)
                first_event_at = drain["first_event_at"]
                event_count = drain["event_count"]
                response_model = drain["response_model"]
                text = drain["text"]
                raw_preview = drain["raw_preview"]
                usage = drain["usage"]
                if drain["stream_truncated"]:
                    error_category = ErrorCategory.STREAM_INCOMPLETE.value
                    error_msg = "流式响应不完整"

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
            error_msg = "TTFT 超时" if "ttft" in str(e) else f"超时: {e}"
            error_category = (
                ErrorCategory.TTFT_TIMEOUT.value
                if "ttft" in str(e)
                else ErrorCategory.NETWORK.value
            )
    except Exception as e:  # noqa: BLE001 - 向 CLI 报告未预期的网络层异常
        error_category = (
            _error_category_for_urlerror(e)
            if _is_tls_error(e)
            else ErrorCategory.UNKNOWN.value
        )
        error_msg = f"异常: {type(e).__name__}: {e}"

    elapsed = round(time.time() - start, 3)
    ttft = round(first_event_at - start, 3) if first_event_at else None

    # 流式与文本探测共用 _answer_correct；调用方可传入同一题目及预期答案。
    answer_text = text.strip() if text else ""
    correct = _answer_correct(answer_text, expected)

    # 状态判定
    if error_msg:
        if error_category in (ErrorCategory.NONE.value, "", None):
            error_category = (
                ErrorCategory.STREAM_INCOMPLETE.value
                if first_event_at is None
                else ErrorCategory.NETWORK.value
            )
        elif is_sse and error_category == ErrorCategory.UNKNOWN.value:
            # SSE 流遭遇未预期异常（IncompleteRead/RemoteDisconnected 等）→ 归为流不完整
            error_category = ErrorCategory.STREAM_INCOMPLETE.value
        status = "error"
    elif not is_sse:
        if http_status == 200 and answer_text:
            # P0：使用已计算的宽松 correct（不再重复硬编码比对）
            status = "pass" if correct else "fail"
            error_category = (
                ErrorCategory.NONE.value
                if correct
                else ErrorCategory.ANSWER_MISMATCH.value
            )
        else:
            status = "error"
            error_category = ErrorCategory.STREAM_PROTOCOL.value
            error_msg = f"非 SSE 响应，Content-Type={content_type!r}"
    else:
        # P0：SSE 路径同样使用已计算的宽松 correct（统一 text 和 stream 答案判定）
        if drain.get("stream_truncated"):
            # 连接提前断（无 done marker）或缓冲丢弃 → 视为不完整，即使答案已完整也不再判 pass
            status = "error"
            error_category = ErrorCategory.STREAM_INCOMPLETE.value
            error_msg = error_msg or "流式响应不完整"
        elif answer_text:
            status = "pass" if correct else "fail"
            error_category = (
                ErrorCategory.NONE.value
                if correct
                else ErrorCategory.ANSWER_MISMATCH.value
            )
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
        "text": _sanitize_for_terminal(text[:80]),
        "is_sse": is_sse,
        "error": _sanitize_display(error_msg, p.api_key),
        "error_category": error_category,
        "raw_preview": (
            _sanitize_display(
                raw_preview.decode("utf-8", errors="replace"), p.api_key
            )
            if raw_preview
            else ""
        ),
        "usage": usage,
    }


def _status_badge(ok: bool, http_status: int | None, error_category: str | None) -> str:
    """把 (ok / http_status / error_category) 映射成一个短状态徽标。"""
    if ok:
        return _c("✅ OK   ", "green", "bold")
    cat = (error_category or "").lower()
    st = http_status or 0
    if cat in ("authentication",) or st in (401, 403):
        return _c("🔒 AUTH ", "red", "bold")
    if cat in ("rate_limit",) or st == 429:
        return _c("⏳ RATE ", "yellow", "bold")
    if cat in ("network",) or st in (502, 504, 522, 524):
        return _c("📡 NET  ", "magenta", "bold")
    if cat in ("model_not_found",) or st == 404:
        return _c("❓ MODEL", "yellow", "bold")
    if cat in ("protocol_incompatible",) or st == 400 or st == 413 or st == 422:
        return _c("⚠  BAD ", "yellow", "bold")
    if cat in ("server_error",) or (st and st >= 500):
        return _c("💥 5XX ", "red", "bold")
    return _c("❌ FAIL ", "red", "bold")


def probe_tier(
    p: Provider,
    tier: ModelTier,
    timeout: int,
    skip_tls_verify: bool,
    max_tokens: int = PROBE_MAX_TOKENS,
    disable_thinking: bool = True,
    user_agent: str | None = None,
    stainless_version: str | None = None,
    stealth: bool = False,
    max_retries: int = 0,
    temperature: float | None = None,
    question: str | None = None,
) -> dict:
    """探测单个档位，返回结果字典（含 usage / raw_body 供 inspect 复用）。

    问题从 PROBE_PROMPTS 随机抽取（避免固定 prompt 被识别），按配对答案宽松校验。
    stealth=True 时请求前随机延迟 STEALTH_JITTER_MIN~MAX 秒，弱化脚本式流量尖峰。
    temperature 非 None 时透传给 build_probe_request（缓存回放探测用）。
    question 非 None 时覆盖随机抽取（双发探测需固定同一问题）。
    """
    if question is None:
        prompt = random.choice(PROBE_PROMPTS)
        question, expected = prompt["q"], prompt["a"]
    else:
        expected = ""
    url, method, headers, body = build_probe_request(
        p,
        tier,
        max_tokens=max_tokens,
        disable_thinking=disable_thinking,
        user_agent=user_agent,
        question=question,
        stainless_version=stainless_version,
        temperature=temperature,
    )
    empty_usage = extract_usage("")
    if not url:
        return {
            "tier": tier.tier,
            "model": tier.model,
            "status": -1,
            "elapsed": 0,
            "error": "无法构造请求 URL",
            "answer": "",
            "question": question,
            "usage": empty_usage,
            "raw_body": "",
            "has_thinking_signal": False,
        }

    if stealth:
        time.sleep(random.uniform(STEALTH_JITTER_MIN, STEALTH_JITTER_MAX))
    start = time.time()
    resp = _http_request(
        url, method, headers, body, timeout, skip_tls_verify, max_retries
    )
    elapsed = round(time.time() - start, 2)

    # 连接层失败（网络/TLS/超时）
    if resp.error_category is not None:
        return {
            "tier": tier.tier,
            "model": tier.model,
            "status": 0,
            "elapsed": elapsed,
            "error": resp.error_msg,
            "error_category": resp.error_category,
            "answer": "",
            "question": question,
            "usage": empty_usage,
            "raw_body": "",
            "has_thinking_signal": False,
        }

    if resp.status != 200:
        category, display = classify_error(resp.body, resp.status)
        return {
            "tier": tier.tier,
            "model": tier.model,
            "status": resp.status,
            "elapsed": elapsed,
            "error": _sanitize_display(display, p.api_key),
            "error_category": category.value,
            "answer": "",
            "question": question,
            "usage": extract_usage(resp.body),
            "raw_body": _sanitize_raw_body(resp.body[:4000], p.api_key),
            "has_thinking_signal": False,
        }

    answer = extract_answer(p, resp.body)
    correct = _answer_correct(answer, expected)
    return {
        "tier": tier.tier,
        "model": tier.model,
        "status": 200,
        "elapsed": elapsed,
        "error": "",
        "answer": answer[:80],
        "correct": correct,
        "question": question,
        "error_category": ErrorCategory.NONE.value
        if correct
        else ErrorCategory.ANSWER_MISMATCH.value,
        "usage": extract_usage(resp.body),
        "raw_body": _sanitize_raw_body(resp.body[:8000], p.api_key),
        "has_thinking_signal": _response_has_thinking_signal(resp.body),
    }


def probe(
    p: Provider,
    timeout: int,
    skip_tls_verify: bool,
    max_tokens: int = PROBE_MAX_TOKENS,
    disable_thinking: bool = True,
    user_agent: str | None = None,
    on_attempt=None,
    stainless_version: str | None = None,
    stealth: bool = False,
    max_retries: int = 0,
) -> dict:
    """按回退顺序探测档位，首个正确回答的档位即为可用档位。

    on_attempt: 可选回调 (provider, attempt_result) -> None，每档结束后立刻调用，
    用于健康检测增量进度（不等整个 provider 的全部档位跑完）。
    stealth: 透传给 probe_tier，请求前加随机延迟弱化流量尖峰。
    """
    attempts = []
    best_tier = None
    for tier in p.tiers:
        r = probe_tier(
            p,
            tier,
            timeout,
            skip_tls_verify,
            max_tokens=max_tokens,
            disable_thinking=disable_thinking,
            user_agent=user_agent,
            stainless_version=stainless_version,
            stealth=stealth,
            max_retries=max_retries,
        )
        attempts.append(r)
        if on_attempt is not None:
            try:
                on_attempt(p, r)
            except Exception as e:  # noqa: BLE001 - 回调失败不影响探测结果
                sys.stderr.write(f"警告: on_attempt 回调失败: {e}\n")
        if r["status"] == 200 and r.get("correct"):
            best_tier = r
            break  # 找到能正确回答的档位，停止回退
    overall_ok = best_tier is not None
    return {
        "name": p.name,
        "type": p.app_type,
        "base_url": p.base_url,
        "auth_mode": p.auth_mode,
        "overall_ok": overall_ok,
        "best_tier": best_tier["tier"] if best_tier else None,
        "attempts": attempts,
    }


def fetch_models(
    p: Provider,
    timeout: int,
    skip_tls_verify: bool,
    user_agent: str | None = None,
    max_retries: int = 0,
) -> dict:
    """拉取供应商的模型列表（GET /v1/models，Anthropic/OpenAI 兼容站通用）"""
    user_agent = user_agent or p.custom_user_agent
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
    resp = _http_request(
        url, "GET", headers, None, timeout, skip_tls_verify, max_retries
    )
    elapsed = round(time.time() - start, 2)

    if resp.error_category is not None:
        return {
            "name": p.name,
            "base_url": p.base_url,
            "status": 0,
            "elapsed": elapsed,
            "error": resp.error_msg,
            "error_category": resp.error_category,
            "models": [],
        }

    if resp.status != 200:
        category, display = classify_error(resp.body, resp.status)
        return {
            "name": p.name,
            "base_url": p.base_url,
            "status": resp.status,
            "elapsed": elapsed,
            "error": _sanitize_display(display, p.api_key),
            "error_category": category.value,
            "models": [],
        }

    models = extract_model_ids(resp.body)
    return {
        "name": p.name,
        "base_url": p.base_url,
        "status": 200,
        "elapsed": elapsed,
        "error": "",
        "error_category": ErrorCategory.NONE.value,
        "models": models,
    }


def probe_model_metadata(
    p: Provider,
    model_id: str,
    timeout: int,
    skip_tls_verify: bool,
    user_agent: str | None = None,
) -> dict:
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
        url = (
            p.base_url.rsplit("/chat/completions", 1)[0].rstrip("/")
            + f"/models/{quoted_id}"
        )
    else:
        url = p.base_url.rstrip("/") + f"/v1/models/{quoted_id}"

    auth_h = build_auth_headers(p)
    headers = {**auth_h, "Accept": "application/json"}
    if p.app_type == "claude":
        headers.update(_claude_code_headers(user_agent))

    start = time.time()
    resp = _http_request(url, "GET", headers, None, timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    _unavail = lambda cat, msg: {
        "status": "unavailable",
        "http_status": 0,
        "declared_context_window": None,
        "max_output_tokens": None,
        "capabilities": {},
        "source": "provider_metadata",
        "error_category": cat,
        "error": msg,
        "elapsed_seconds": elapsed,
    }

    if resp.error_category is not None:
        return _unavail(resp.error_category, resp.error_msg)

    if resp.status != 200:
        category, display = classify_error(resp.body, resp.status)
        return {
            "status": "unavailable",
            "http_status": resp.status,
            "declared_context_window": None,
            "max_output_tokens": None,
            "capabilities": {},
            "source": "provider_metadata",
            "error_category": category.value,
            "error": _sanitize_display(display, p.api_key),
            "elapsed_seconds": elapsed,
        }

    # 解析：OpenAI/Anthropic 都返回 {"id", "max_input_tokens"|"context_window", ...}
    try:
        j = json.loads(resp.body)
    except (json.JSONDecodeError, TypeError):
        return {
            "status": "unavailable",
            "http_status": resp.status,
            "declared_context_window": None,
            "max_output_tokens": None,
            "capabilities": {},
            "source": "provider_metadata",
            "error_category": ErrorCategory.INVALID_RESPONSE.value,
            "error": "响应非 JSON",
            "elapsed_seconds": elapsed,
        }

    declared = (
        j.get("max_input_tokens") or j.get("context_window") or j.get("max_tokens")
    )
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


def probe_context_smoke(
    p: Provider,
    model_id: str,
    target_chars: int,
    timeout: int,
    skip_tls_verify: bool,
    user_agent: str | None = None,
) -> dict:
    """发一次大上下文请求，验证供应商是否接受该体量的输入。

    target_chars: 目标字符数（如 524288 对应 512k）
    使用 1 字符≈1 token 上界逼近真·tokens；报告写 estimate 不写假精确值。
    仅 claude（含 openrouter chat）路径；其它 app_type 返回 unsupported。
    """
    te = f"~{target_chars} chars (≥{target_chars} tokens upper bound, 1char≈1token)"
    # P0 修复：协议枚举决定是否支持 context smoke（替代旧的仅按 app_type 限制）
    proto = getattr(p, "protocol", Protocol.UNKNOWN)
    if isinstance(proto, str):
        proto = (
            Protocol(proto)
            if proto in Protocol._value2member_map_
            else Protocol.UNKNOWN
        )
    # Anthropic messages + OpenAI chat 兼容站支持大上下文；responses（codex）暂不支持
    supported_proto = proto in (
        Protocol.ANTHROPIC_MESSAGES,
        Protocol.OPENAI_CHAT_COMPLETIONS,
        Protocol.UNKNOWN,
    )
    # 向后兼容：旧数据无 protocol 时，claude / openrouter 仍支持；codex（responses）不支持
    if proto == Protocol.UNKNOWN:
        supported_proto = (
            p.app_type == "claude" or p.is_openrouter or p.app_type == "openclaw"
        )
    if not supported_proto or (
        p.app_type not in ("claude", "openclaw")
        and not p.is_openrouter
        and proto == Protocol.OPENAI_RESPONSES
    ):
        return {
            "status": "unsupported",
            "approx_input_chars": 0,
            "token_estimate": te,
            "http_status": None,
            "error_category": ErrorCategory.NONE.value,
            "error": f"context smoke 暂不支持协议 protocol={proto.value if isinstance(proto, Protocol) else proto}, app_type={p.app_type}",
            "elapsed_seconds": 0,
        }

    filler = _build_context_filler(target_chars)
    prompt = f"{filler}\n\nWhat is 2+3? Reply with only the number."
    url, method, headers, _ = build_probe_request(
        p,
        ModelTier("default", model_id, model_id),
        max_tokens=PROBE_MAX_TOKENS,
        disable_thinking=True,
        user_agent=user_agent,
    )
    if not url:
        return {
            "status": "error",
            "approx_input_chars": len(prompt),
            "token_estimate": te,
            "http_status": None,
            "error_category": ErrorCategory.UNKNOWN.value,
            "error": "无法构造请求 URL",
            "elapsed_seconds": 0,
        }

    # P0 修复：context smoke 请求构造按协议分支（不再写死 Anthropic body）
    # 协议已在前面检查（supported_proto），构造时按 protocol 构造正确格式
    proto = getattr(p, "protocol", Protocol.UNKNOWN)
    if isinstance(proto, str):
        proto = (
            Protocol(proto)
            if proto in Protocol._value2member_map_
            else Protocol.UNKNOWN
        )
    # 构造消息内容：包含填充文本 + 问题
    content_text = f"{filler}\n\nWhat is 2+3? Reply with only the number."

    if proto == Protocol.ANTHROPIC_MESSAGES or (
        proto == Protocol.UNKNOWN and p.app_type == "claude"
    ):
        payload = {
            "model": model_id,
            "max_tokens": PROBE_MAX_TOKENS,
            "messages": [{"role": "user", "content": content_text}],
            "thinking": {"type": "disabled"},
        }
    elif proto == Protocol.OPENAI_CHAT_COMPLETIONS or (
        proto == Protocol.UNKNOWN and (p.is_openrouter or p.app_type == "openclaw")
    ):
        # OpenAI chat 兼容：不发 Anthropic 特有的 thinking.disabled
        payload = {
            "model": model_id,
            "max_tokens": PROBE_MAX_TOKENS,
            "messages": [{"role": "user", "content": content_text}],
        }
    elif proto == Protocol.OPENAI_RESPONSES:
        # Responses API 不支持 messages 格式的大上下文（暂不处理此分支，已在上面排除）
        payload = {
            "model": model_id,
            "max_output_tokens": PROBE_MAX_TOKENS,
            "input": content_text,
        }
    else:
        # �知协议兜底（同 Anthropic 格式，确保不发无效请求）
        payload = {
            "model": model_id,
            "max_tokens": PROBE_MAX_TOKENS,
            "messages": [{"role": "user", "content": content_text}],
            "thinking": {"type": "disabled"},
        }
    body = json.dumps(payload).encode()

    # 大 body 上传慢：超时至少 120s
    smoke_timeout = max(timeout, 120)
    start = time.time()
    resp = _http_request(url, method, headers, body, smoke_timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    if resp.error_category is not None:
        st = "timeout" if "time" in (resp.error_msg or "").lower() else "error"
        return {
            "status": st,
            "approx_input_chars": len(prompt),
            "token_estimate": te,
            "http_status": 0,
            "error_category": resp.error_category,
            "error": resp.error_msg,
            "elapsed_seconds": elapsed,
        }

    if resp.status == 200:
        return {
            "status": "accepted",
            "approx_input_chars": len(prompt),
            "token_estimate": te,
            "http_status": 200,
            "error_category": ErrorCategory.NONE.value,
            "error": None,
            "elapsed_seconds": elapsed,
        }

    low = (resp.body or "").lower()
    if resp.status in (413, 414) or (
        resp.status == 400
        and any(
            k in low
            for k in ("context", "too long", "maximum", "token", "length", "payload")
        )
    ):
        return {
            "status": "rejected",
            "approx_input_chars": len(prompt),
            "token_estimate": te,
            "http_status": resp.status,
            "error_category": ErrorCategory.PROTOCOL_INCOMPATIBLE.value,
            "error": _sanitize_display(classify_error(resp.body, resp.status)[1], p.api_key),
            "elapsed_seconds": elapsed,
        }

    category, display = classify_error(resp.body, resp.status)
    return {
        "status": "error",
        "approx_input_chars": len(prompt),
        "token_estimate": te,
        "http_status": resp.status,
        "error_category": category.value,
        "error": _sanitize_display(display, p.api_key),
        "elapsed_seconds": elapsed,
    }


# 极小 1x1 PNG（红色像素），base64 常量，不读外文件
_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)
_TOOL_NAME = "get_probe_number"
_TOOL_DESC = "Return the probe number 5. No side effects."


def _probe_tools(
    p: Provider,
    model_id: str,
    timeout: int,
    skip_tls_verify: bool,
    user_agent: str | None = None,
) -> dict:
    """最小 tool-use 探测：要求模型调用 get_probe_number，不执行副作用。

    判定：
      native   — 协议级 tool_use / tool_calls
      text_only — 纯文本声称调用但无协议块
      rejected  — 400/协议拒 tools
      unknown   — 其它
    """
    url, method, headers, _ = build_probe_request(
        p,
        ModelTier("default", model_id, model_id),
        max_tokens=max(PROBE_MAX_TOKENS, 64),
        disable_thinking=True,
        user_agent=user_agent,
    )
    if not url:
        return {
            "status": "error",
            "protocol_support": "unknown",
            "tool_name_seen": None,
            "http_status": None,
            "error_category": ErrorCategory.UNKNOWN.value,
            "error": "无法构造请求 URL",
            "evidence": "",
        }

    prompt = (
        f"You must call the tool {_TOOL_NAME} to answer. "
        "Do not answer with a bare number; invoke the tool."
    )
    if p.app_type == "claude" and not p.is_openrouter:
        payload = {
            "model": model_id,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "name": _TOOL_NAME,
                    "description": _TOOL_DESC,
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            ],
            "thinking": {"type": "disabled"},
        }
    elif p.app_type in ("openclaw",) or p.is_openrouter:
        payload = {
            "model": model_id,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": _TOOL_NAME,
                        "description": _TOOL_DESC,
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "auto",
        }
    elif p.app_type == "codex":
        # Responses API function tools（简化）
        payload = {
            "model": model_id,
            "max_output_tokens": 64,
            "input": prompt,
            "tools": [
                {
                    "type": "function",
                    "name": _TOOL_NAME,
                    "description": _TOOL_DESC,
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
    else:
        return {
            "status": "unsupported",
            "protocol_support": "unknown",
            "tool_name_seen": None,
            "http_status": None,
            "error_category": ErrorCategory.NONE.value,
            "error": None,
            "evidence": f"app_type={p.app_type}",
        }

    body = json.dumps(payload).encode()
    start = time.time()
    resp = _http_request(url, method, headers, body, timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    if resp.error_category is not None:
        return {
            "status": "error",
            "protocol_support": "unknown",
            "tool_name_seen": None,
            "http_status": 0,
            "error_category": resp.error_category,
            "error": resp.error_msg,
            "evidence": "",
            "elapsed_seconds": elapsed,
        }

    if resp.status in (400, 422) and any(
        k in (resp.body or "").lower()
        for k in ("tool", "function", "schema", "unknown field")
    ):
        return {
            "status": "fail",
            "protocol_support": "rejected",
            "tool_name_seen": None,
            "http_status": resp.status,
            "error_category": ErrorCategory.PROTOCOL_INCOMPATIBLE.value,
            "error": _sanitize_display(classify_error(resp.body, resp.status)[1], p.api_key),
            "evidence": _sanitize_display((resp.body or "")[:200], p.api_key),
            "elapsed_seconds": elapsed,
        }

    if resp.status != 200:
        cat, disp = classify_error(resp.body, resp.status)
        return {
            "status": "error",
            "protocol_support": "unknown",
            "tool_name_seen": None,
            "http_status": resp.status,
            "error_category": cat.value,
            "error": _sanitize_display(disp, p.api_key),
            "evidence": _sanitize_display((resp.body or "")[:200], p.api_key),
            "elapsed_seconds": elapsed,
        }

    body_s = resp.body or ""
    low = body_s.lower()
    tool_seen = None
    support = "unknown"
    # Anthropic tool_use
    if (
        '"type":"tool_use"' in low
        or '"type": "tool_use"' in low
        or "tool_calls" in low
        or '"type":"function_call"' in low
    ):
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
        "error_category": ErrorCategory.NONE.value
        if status == "pass"
        else ErrorCategory.ANSWER_MISMATCH.value,
        "error": None if status == "pass" else f"protocol_support={support}",
        "evidence": _sanitize_display(body_s[:240], p.api_key),
        "elapsed_seconds": elapsed,
    }


def _probe_vision(
    p: Provider,
    model_id: str,
    timeout: int,
    skip_tls_verify: bool,
    user_agent: str | None = None,
) -> dict:
    """可选 vision 探测：发 1x1 PNG，问主色（宽松判定）。"""
    url, method, headers, _ = build_probe_request(
        p,
        ModelTier("default", model_id, model_id),
        max_tokens=32,
        disable_thinking=True,
        user_agent=user_agent,
    )
    if not url:
        return {
            "status": "error",
            "http_status": None,
            "error_category": ErrorCategory.UNKNOWN.value,
            "error": "无法构造请求 URL",
            "answer": "",
            "evidence": "",
        }

    q = "What is the main color of this image? Reply with one English color word only."
    if p.app_type == "claude" and not p.is_openrouter:
        payload = {
            "model": model_id,
            "max_tokens": 32,
            "thinking": {"type": "disabled"},
            "messages": [
                {
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
                }
            ],
        }
    elif p.app_type in ("openclaw",) or p.is_openrouter:
        payload = {
            "model": model_id,
            "max_tokens": 32,
            "messages": [
                {
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
                }
            ],
        }
    else:
        return {
            "status": "unsupported",
            "http_status": None,
            "error_category": ErrorCategory.NONE.value,
            "error": None,
            "answer": "",
            "evidence": f"app_type={p.app_type}",
        }

    body = json.dumps(payload).encode()
    start = time.time()
    resp = _http_request(url, method, headers, body, timeout, skip_tls_verify)
    elapsed = round(time.time() - start, 2)

    if resp.error_category is not None:
        return {
            "status": "error",
            "http_status": 0,
            "error_category": resp.error_category,
            "error": resp.error_msg,
            "answer": "",
            "evidence": "",
            "elapsed_seconds": elapsed,
        }

    if resp.status in (400, 415, 422) and any(
        k in (resp.body or "").lower()
        for k in ("image", "vision", "multimodal", "unsupported", "media")
    ):
        return {
            "status": "fail",
            "http_status": resp.status,
            "error_category": ErrorCategory.PROTOCOL_INCOMPATIBLE.value,
            "error": _sanitize_display(classify_error(resp.body, resp.status)[1], p.api_key),
            "answer": "",
            "evidence": _sanitize_display((resp.body or "")[:200], p.api_key),
            "elapsed_seconds": elapsed,
        }

    if resp.status != 200:
        cat, disp = classify_error(resp.body, resp.status)
        return {
            "status": "error",
            "http_status": resp.status,
            "error_category": cat.value,
            "error": _sanitize_display(disp, p.api_key),
            "answer": "",
            "evidence": _sanitize_display((resp.body or "")[:200], p.api_key),
            "elapsed_seconds": elapsed,
        }

    ans = extract_answer(p, resp.body)
    # 宽松：有非空回答即 pass（1x1 图颜色不可靠，只验证是否接受 image）
    status = "pass" if ans.strip() else "fail"
    return {
        "status": status,
        "http_status": 200,
        "error_category": ErrorCategory.NONE.value
        if status == "pass"
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


def _collect_models_for_probe(p, fetch_result, source):
    """按 source 汇总要探测的模型 id 列表（去重保序）。

    configured: cc-switch 里配置的档位模型（p.tiers）
    listed:     GET /v1/models 返回的模型（fetch_result['models']）
    both:       两者合并
    """
    configured = [t.model for t in p.tiers]
    listed = fetch_result.get("models", []) if fetch_result.get("status") == 200 else []
    if source == "configured":
        picked = configured
    elif source == "listed":
        picked = listed if listed else configured  # 拉不到列表则降级
    else:  # both
        picked = configured + listed
    seen = set()
    out = []
    for m in picked:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _probe_one_model(p, model_id, args, deep):
    """对单个模型做轻量或深度探测，返回结构化 dict。

    轻量: 仅 text（probe_tier，2+3 题）
    深度: text + streaming + metadata + thinking + tools（12356，跳过 context/vision）
    """
    mt = getattr(args, "probe_max_tokens", PROBE_MAX_TOKENS)
    dt = not getattr(args, "probe_enable_thinking", False)
    ua = getattr(args, "user_agent", None)
    to = args.timeout
    tls = args.skip_tls_verify
    ip = rebuild_provider_for_inspect(p, model_id)
    tier = ip.tiers[0]

    text_result, text_raw = _inspect_text(
        ip, tier, to, tls, max_tokens=mt, disable_thinking=dt, user_agent=ua
    )
    out = {"model": model_id, "text": text_result}
    if not deep:
        return out

    out["streaming"] = probe_stream(
        ip, tier, to, tls, max_tokens=mt, disable_thinking=dt, user_agent=ua
    )
    out["metadata"] = probe_model_metadata(
        ip, re.sub(r"\[.*?\]$", "", model_id), to, tls, user_agent=ua
    )
    out["thinking"] = _inspect_thinking(
        ip, tier, text_raw, to, tls, max_tokens=mt, user_agent=ua
    )
    out["tools"] = _probe_tools(
        ip, re.sub(r"\[.*?\]$", "", model_id), to, tls, user_agent=ua
    )
    return out



# --- from check_ccswitch_health.py:2652-2947 ---
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
        protocol=p.protocol,  # 保留协议配置，否则探测会退回默认 Anthropic Messages
        protocol_source=p.protocol_source,
        custom_user_agent=p.custom_user_agent,
        notes=p.notes,
    )


def detect_protocol(p: Provider) -> dict:
    """根据 base_url 和 app_type 推断协议路径；不发送网络请求。"""
    # 协议推断：先检查 base_url 显式后缀，其次检查 protocol 字段（已由解析时赋值），
    # 最后回退到 app_type 默认。修复 P0 �议误判（如 L站0730）。
    base = p.base_url.rstrip("/")
    if p.protocol != Protocol.UNKNOWN:
        proto_map = {
            Protocol.ANTHROPIC_MESSAGES: "anthropic_messages",
            Protocol.OPENAI_CHAT_COMPLETIONS: "openai_chat_completions",
            Protocol.OPENAI_RESPONSES: "openai_responses",
        }
        detected = proto_map.get(p.protocol, "unknown")
        # confidence：apiFormat/base_url 显式配置 → configured；app_type 默认 → inferred（均未实测）
        confidence = (
            "configured"
            if p.protocol_source in ("api_format", "url_suffix")
            else "inferred"
        )
        return {
            "detected": detected,
            "confidence": confidence,
            "evidence": {
                "path_suffix": base,
                "app_type": p.app_type,
                "protocol_field": p.protocol.value,
                "protocol_source": p.protocol_source,
                "is_openrouter": p.is_openrouter,
            },
        }

    # 无协议字段时按 URL 后缀推断（向后兼容旧数据）
    if "/chat/completions" in base:
        detected = "openai_chat_completions"
    elif base.endswith("/v1/responses") or "/v1/responses" in base:
        detected = "openai_responses"
    elif base.endswith("/v1/messages") or "/v1/messages" in base:
        detected = "anthropic_messages"
    else:
        detected = {
            "claude": "anthropic_messages",
            "codex": "openai_responses",
            "openclaw": "openai_chat_completions",
        }.get(p.app_type, "unknown")

    return {
        "detected": detected,
        "confidence": "inferred",
        "evidence": {
            "path_suffix": base,
            "app_type": p.app_type,
            "protocol_field": p.protocol.value
            if p.protocol != Protocol.UNKNOWN
            else None,
            "is_openrouter": p.is_openrouter,
        },
    }


# 模型规范化后缀：日期快照 / 上下文窗口 / 思考模式 / fast 模式
_MODEL_SUFFIX_PATTERNS = [
    r"-\d{8}$",  # -20251001
    r"-\d{4}-\d{2}-\d{2}$",  # -2025-10-01
    r"\[1M\]$",  # [1M] 上下文
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
        return {
            "match": "alias_match",
            "warning": f"供应商去除了日期/上下文后缀：{requested!r} → {responded!r}",
        }

    # 仅允许由分隔符包围的完整模型 ID 嵌入，避免 gpt-4→gpt-4o-mini、
    # sonnet→sonnet-4 这类前缀/子串误判为同一模型。
    req_parts = [part for part in re.split(r"[-_/]+", norm_req) if part]
    res_parts = [part for part in re.split(r"[-_/]+", norm_res) if part]
    if len(req_parts) >= 2:
        for parts, whole in ((req_parts, res_parts), (res_parts, req_parts)):
            if len(parts) < 2 or len(parts) > len(whole):
                continue
            if any(
                whole[i : i + len(parts)] == parts
                for i in range(len(whole) - len(parts) + 1)
            ):
                return {
                    "match": "fuzzy_match",
                    "warning": f"模型名不完全一致：{requested!r} vs {responded!r}",
                }

    return {
        "match": "mismatch",
        "warning": f"模型路由不一致：请求 {requested!r}，实际响应 {responded!r}",
    }


def _inspect_text(
    p, tier, timeout, skip_tls, *, max_tokens, disable_thinking, user_agent
):
    """文本探测 → (result_dict, raw_probe_dict)。"""
    r = probe_tier(
        p,
        tier,
        timeout,
        skip_tls,
        max_tokens=max_tokens,
        disable_thinking=disable_thinking,
        user_agent=user_agent,
    )
    result = {
        "status": (
            "pass"
            if r.get("status") == 200 and r.get("correct")
            else "fail"
            if r.get("status") == 200
            else "error"
        ),
        "elapsed_seconds": r.get("elapsed", 0),
        "answer": r.get("answer", ""),
        "correct": r.get("correct", False),
        "http_status": r.get("status", 0),
        "error_category": r.get("error_category", ErrorCategory.UNKNOWN.value),
        "error": r.get("error", ""),
    }
    return result, r


def _inspect_thinking(p, tier, text_raw, timeout, skip_tls, *, max_tokens, user_agent):
    """thinking 双发探测（disable 复用 text_raw + enable 新发）→ result dict。"""
    if text_raw is None:
        return {
            "status": "dependency_missing",
            "error": "thinking 需要 text 在 --include 中（复用 disable 结果）",
        }
    disable_ok = text_raw.get("status") == 200
    r_en = probe_tier(
        p,
        tier,
        timeout,
        skip_tls,
        max_tokens=max(max_tokens, 256),
        disable_thinking=False,
        user_agent=user_agent,
    )
    result = {
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
    if r_en.get("status") == 400:
        result["verdict"] = "rejects_thinking_field"
    elif not disable_ok and r_en.get("status") == 200:
        result["verdict"] = "forces_thinking"
    elif disable_ok and r_en.get("status") == 200:
        if r_en.get("has_thinking_signal"):
            result["verdict"] = "supports_disable_and_emits_thinking"
        else:
            result["verdict"] = "supports_disable"
    elif not disable_ok and not r_en.get("correct"):
        result["verdict"] = "breaks_on_short_budget"
    # 暴露 enable-thinking 响应体，供保真鉴权复用（提取签名），不新增请求
    result["_enabled_raw_body"] = r_en.get("raw_body", "")
    return result


def _inspect_model_consistency(
    requested: str, responded: str | None, include: set
) -> dict:
    """模型一致性判定。"""
    if "model-consistency" not in include:
        return {
            "requested": requested,
            "responded": responded,
            "match": "not_run",
            "warning": None,
        }
    result = {
        "requested": requested,
        "responded": responded,
        "match": "not_run" if responded is None else "pending",
        "warning": None,
    }
    if responded:
        cmp = compare_models(requested, responded)
        result["match"] = cmp["match"]
        result["warning"] = cmp["warning"]
    return result


def _inspect_verdict(text_result, model_consistency, thinking_result, tools_result):
    """汇总结论 + 推荐操作 → (verdict, anomaly, recommended)。"""
    if text_result:
        if text_result["status"] == "pass":
            verdict = "healthy"
        elif text_result["status"] == "fail":
            verdict = "available_but_wrong_answer"
        else:
            verdict = "unavailable"
    else:
        verdict = "skipped"
    anomaly = model_consistency.get("match") == "mismatch"
    recommended = []
    if model_consistency.get("match") == "mismatch":
        recommended.append("检查供应商是否将模型别名静默路由到其它模型")
    if model_consistency.get("match") == "fuzzy_match":
        recommended.append("供应商可能使用别名；确认映射是否符合预期")
    if text_result and text_result["status"] == "fail":
        recommended.append("供应商返回了 200 但答案不匹配；可能是模型降级或代理错误")
    if text_result and text_result["status"] == "error":
        recommended.append(
            f"该供应商探测失败 ({text_result['error_category']})，可能影响故障转移"
        )
    if thinking_result.get("verdict") == "forces_thinking":
        recommended.append("模型强制 thinking 模式；短 max_tokens 预算下可能无最终答案")
    if thinking_result.get("verdict") == "rejects_thinking_field":
        recommended.append(
            "供应商拒绝 thinking/reasoning 相关字段；可尝试 --probe-enable-thinking 跳过"
        )
    if tools_result.get("status") == "error":
        recommended.append("Tool use 探测失败；Claude Code 的 tool 调用可能不可用")
    return verdict, anomaly, recommended


_INSPECT_DEFAULT_INCLUDE = (
    "text,streaming,model-consistency,protocol,error-classification,"
    "metadata,thinking,tools"
)
_COMPARE_DEFAULT_INCLUDE = "text,streaming"



# --- from check_ccswitch_health.py:3355-3474 ---
def _is_rate_limited(result) -> bool:
    """判断探测结果是否因 429/rate_limit 失败。"""
    if not result or not isinstance(result, dict):
        return False
    if result.get("error_category") == "rate_limit":
        return True
    if result.get("http_status") == 429 or result.get("status") == 429:
        return True
    err = str(result.get("error", "")).lower()
    return "rate" in err and ("limit" in err or "frequent" in err or "too many" in err)


def _inspect_one_model(inspect_p, model_id, args, include, _mt, _dt, _ua):
    """对单个模型跑 7 维探测，返回 report dict（不含重试；重试由 _run_inspect_all 负责）。"""
    protocol = detect_protocol(inspect_p)
    text_result, text_raw = (None, None)
    if "text" in include:
        text_result, text_raw = _inspect_text(
            inspect_p,
            inspect_p.tiers[0],
            args.timeout,
            args.skip_tls_verify,
            max_tokens=_mt,
            disable_thinking=_dt,
            user_agent=_ua,
        )
    streaming_result = None
    if "streaming" in include:
        streaming_result = probe_stream(
            inspect_p,
            inspect_p.tiers[0],
            args.timeout,
            args.skip_tls_verify,
            ttft_timeout=getattr(args, "ttft_timeout", None),
            max_tokens=_mt,
            disable_thinking=_dt,
            user_agent=_ua,
        )
    metadata_result = {"status": "skipped"}
    if "metadata" in include:
        metadata_result = probe_model_metadata(
            inspect_p,
            re.sub(r"\[.*?\]$", "", model_id),
            args.timeout,
            args.skip_tls_verify,
            user_agent=_ua,
        )
    context_result = {"status": "skipped"}
    if "context" in include or (
        getattr(args, "include", None) is None and "metadata" in include
    ):
        has_declared = (
            metadata_result.get("declared_context_window") is not None
            and metadata_result.get("status") == "available"
        )
        if not has_declared:
            _ctx = {"512k": 524288, "1m": 1048576}.get(
                getattr(args, "probe_context", "512k"), 524288
            )
            context_result = probe_context_smoke(
                inspect_p,
                model_id,
                _ctx,
                args.timeout,
                args.skip_tls_verify,
                user_agent=_ua,
            )
    thinking_result = {"status": "skipped"}
    if "thinking" in include:
        thinking_result = _inspect_thinking(
            inspect_p,
            inspect_p.tiers[0],
            text_raw,
            args.timeout,
            args.skip_tls_verify,
            max_tokens=_mt,
            user_agent=_ua,
        )
    tools_result = {"status": "skipped"}
    if "tools" in include:
        tools_result = _probe_tools(
            inspect_p, model_id, args.timeout, args.skip_tls_verify, _ua
        )
    vision_result = {"status": "skipped"}
    if "vision" in include:
        vision_result = _probe_vision(
            inspect_p, model_id, args.timeout, args.skip_tls_verify, _ua
        )

    if text_result and text_result["status"] == "pass":
        protocol["confidence"] = "confirmed"
    responded_model = (streaming_result or {}).get("response_model")
    model_consistency = _inspect_model_consistency(model_id, responded_model, include)
    verdict, anomaly, recommended = _inspect_verdict(
        text_result, model_consistency, thinking_result, tools_result
    )

    # 保真鉴别 P0：换芯字段检测（复用 text/streaming 响应体）+ thinking 签名提取（复用 thinking enable 响应体）
    auth_body = ""
    if text_raw and text_raw.get("status") == 200:
        auth_body = text_raw.get("raw_body", "")
    elif streaming_result and streaming_result.get("raw_preview"):
        auth_body = streaming_result.get("raw_preview", "")
    crosspack = _detect_crosspack_fields(auth_body, inspect_p.app_type)
    enabled_body = thinking_result.pop("_enabled_raw_body", "") if isinstance(thinking_result, dict) else ""
    sigs = _extract_thinking_signatures(enabled_body)
    thinking_signature = {
        "has_valid_signature": _has_valid_thinking_signature(sigs),
        "blocks": sigs,
    }
    # P1: usage 自洽静态校验（复用 text_raw 的 usage + answer，零新请求）
    _usage = (text_raw or {}).get("usage") if text_raw else None
    _answer = (text_raw or {}).get("answer", "") if text_raw else ""
    usage_consistency = _check_usage_consistency(_usage, _answer, inspect_p.app_type)
    # P2-A: 缓存回放/钳温双发探测（可选 --include cache-replay；2 次新请求）
    cache_replay = {"suspicious": False, "identical": None, "note": "未启用（--include cache-replay 开启）"}
    if "cache-replay" in include:
        cache_replay = _probe_cache_replay(
            inspect_p, inspect_p.tiers[0], args.timeout, args.skip_tls_verify,
            max_tokens=16, user_agent=_ua,
        )
    auth_verdict, auth_evidence = _authenticity_verdict(
        {
            "crosspack": crosspack,
            "thinking_signature": thinking_signature,
            "usage_consistency": usage_consistency,
            "cache_replay": cache_replay,
        }
    )
    if auth_verdict == "suspicious":
        recommended.append("保真告警：" + "；".join(auth_evidence))

    return {
        "schema_version": 1,
        "command": "inspect",
        "provider": inspect_p.name,
        "model": model_id,
        "protocol": protocol,
        "text": text_result,
        "streaming": streaming_result
        if streaming_result is not None
        else {"status": "not_run"},
        "metadata": metadata_result,
        "context": context_result,
        "thinking": thinking_result,
        "tools": tools_result,
        "vision": vision_result,
        "model_consistency": model_consistency,
        "authenticity": {
            "crosspack": crosspack,
            "thinking_signature": thinking_signature,
            "usage_consistency": usage_consistency,
            "cache_replay": cache_replay,
            "verdict": auth_verdict,
            "evidence": auth_evidence,
        },
        "summary": {
            "verdict": verdict,
            "model_routing_anomaly": anomaly,
            "recommended_actions": recommended,
        },
    }




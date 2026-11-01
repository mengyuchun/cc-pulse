"""P1 模型保真鉴别测试：usage 自洽静态校验。

TDD：先写测试（RED），再实现 _check_usage_consistency + 集成进 _authenticity_verdict。
覆盖 docs/PRD-authenticity-p1.md 第 5 节验收表 1-8。
"""

import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SPEC = importlib.util.spec_from_file_location(
    "ccpulse_usage", os.path.join(_ROOT, "ccpulse_probe.py")
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)


def _usage(inp, out, total=None, present=True, source="response_body"):
    return {
        "present": present,
        "input_tokens": inp,
        "output_tokens": out,
        "source": source,
        "missing_fields": [],
        # 测试用 total_tokens（OpenAI 系）放在 extra 键
        "total_tokens": total,
    }


class UsageConsistencyTest(unittest.TestCase):
    def test_openai_total_inconsistent(self):
        """total_tokens != prompt + completion -> 可疑。"""
        usage = _usage(inp=10, out=3, total=15)  # 10+3=13 != 15
        r = mod._check_usage_consistency(usage, "5", "codex")
        self.assertTrue(r["suspicious"])
        checks = [f["check"] for f in r["findings"]]
        self.assertIn("total_inconsistent", checks)

    def test_openai_total_consistent(self):
        """total_tokens == prompt + completion -> 不可疑。"""
        usage = _usage(inp=10, out=3, total=13)
        r = mod._check_usage_consistency(usage, "5", "codex")
        self.assertFalse(r["suspicious"])

    def test_output_tokens_but_empty_answer(self):
        """output_tokens > 0 但 answer 空 -> 可疑。"""
        usage = _usage(inp=10, out=5, total=15)
        r = mod._check_usage_consistency(usage, "", "codex")
        self.assertTrue(r["suspicious"])
        self.assertIn(
            "output_no_content",
            [f["check"] for f in r["findings"]],
        )

    def test_output_tokens_with_answer(self):
        """output_tokens > 0 且 answer 非空 -> 不报此项。"""
        usage = _usage(inp=10, out=3, total=13)
        r = mod._check_usage_consistency(usage, "5", "codex")
        self.assertFalse(r["suspicious"])

    def test_missing_usage_is_clean_with_note(self):
        """usage 缺失(present=False) -> 不可疑，note 标注。"""
        usage = {
            "present": False,
            "input_tokens": None,
            "output_tokens": None,
            "source": None,
            "missing_fields": ["input_tokens", "output_tokens"],
        }
        r = mod._check_usage_consistency(usage, "5", "claude")
        self.assertFalse(r["suspicious"])
        self.assertIn("note", r)

    def test_anthropic_no_total_tokens(self):
        """Anthropic usage 无 total_tokens -> 不触发 total 校验。"""
        usage = {
            "present": True,
            "input_tokens": 10,
            "output_tokens": 3,
            "source": "response_body",
            "missing_fields": [],
        }
        r = mod._check_usage_consistency(usage, "5", "claude")
        self.assertFalse(r["suspicious"])

    def test_anthropic_output_no_content_still_caught(self):
        """Anthropic 无 total 但 output>0 answer 空 -> 仍报 output_no_content。"""
        usage = {
            "present": True,
            "input_tokens": 10,
            "output_tokens": 5,
            "source": "response_body",
            "missing_fields": [],
        }
        r = mod._check_usage_consistency(usage, "  ", "claude")
        self.assertTrue(r["suspicious"])


class AuthenticityVerdictWithUsageTest(unittest.TestCase):
    def test_usage_inconsistent_yields_suspicious(self):
        """usage 不自洽 -> verdict=suspicious。"""
        auth = {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": True},
            "usage_consistency": {
                "suspicious": True,
                "findings": [{"check": "total_inconsistent", "reason": "x"}],
            },
        }
        v, ev = mod._authenticity_verdict(auth)
        self.assertEqual(v, "suspicious")
        self.assertTrue(any("usage" in e.lower() or "计费" in e for e in ev))

    def test_all_clean_yields_clean(self):
        """P0 clean + usage clean -> clean。"""
        auth = {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": True},
            "usage_consistency": {"suspicious": False, "findings": []},
        }
        v, _ev = mod._authenticity_verdict(auth)
        self.assertEqual(v, "clean")


class BatchOneResponseTest(unittest.TestCase):
    def _responses_provider(self):
        return mod.Provider(
            name="responses",
            app_type="codex",
            base_url="https://example.test",
            api_key="",
            auth_mode="none",
            protocol=mod.Protocol.OPENAI_RESPONSES,
            tiers=[mod.ModelTier("default", "model", "model")],
        )

    def test_extract_responses_output_text(self):
        provider = self._responses_provider()
        self.assertEqual(
            mod.extract_answer(provider, '{"output_text":"5"}'),
            "5",
        )

    def test_extract_responses_nested_output_text(self):
        provider = self._responses_provider()
        body = '{"output":[{"content":[{"type":"output_text","text":"5"}]}]}'
        self.assertEqual(mod.extract_answer(provider, body), "5")

    def test_extract_usage_keeps_openai_total_tokens(self):
        body = '{"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":15}}'
        usage = mod.extract_usage(body)
        self.assertEqual(usage["total_tokens"], 15)
        self.assertTrue(
            mod._check_usage_consistency(
                usage,
                mod.extract_answer(self._responses_provider(), '{"output_text":"5"}'),
                "codex",
            )["suspicious"]
        )

    def test_probe_stream_accepts_responses_json(self):
        provider = self._responses_provider()
        body = b'{"output_text":"5","usage":{"input_tokens":10,"output_tokens":1,"total_tokens":11}}'
        original = mod.urllib.request.urlopen

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit=None):
                return body

        try:
            mod.urllib.request.urlopen = lambda *args, **kwargs: FakeResponse()
            result = mod.probe_stream(
                provider,
                provider.tiers[0],
                timeout=1,
                skip_tls_verify=False,
                expected="5",
            )
        finally:
            mod.urllib.request.urlopen = original

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["text"], "5")
        self.assertEqual(result["usage"]["total_tokens"], 11)

    def test_probe_stream_structures_200_invalid_responses(self):
        provider = self._responses_provider()
        bodies = [
            "[1, 2, 3]",
            '{"error":{"message":"backend failed"}}',
            '{"status":"failed","error":{"message":"failed"}}',
            '{"status":"completed","output":[]}',
        ]
        original = mod.urllib.request.urlopen

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __init__(self, response_body):
                self.response_body = response_body.encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit=None):
                return self.response_body

        try:
            for response_body in bodies:
                with self.subTest(response_body=response_body):
                    mod.urllib.request.urlopen = (
                        lambda *args, _body=response_body, **kwargs: FakeResponse(_body)
                    )
                    result = mod.probe_stream(
                        provider,
                        provider.tiers[0],
                        timeout=1,
                        skip_tls_verify=False,
                        expected="5",
                    )
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(
                        result["error_category"],
                        mod.ErrorCategory.INVALID_RESPONSE.value,
                    )
                    self.assertTrue(result["error"])
        finally:
            mod.urllib.request.urlopen = original

    def test_drain_sse_returns_nested_responses_usage_total(self):
        provider = self._responses_provider()
        stream = [
            b'event: response.output_text.delta\r\ndata: {"type":"response.output_text.delta","delta":"5"}\r\n\r\n',
            b'event: response.completed\r\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}}}\r\n\r\n',
        ]

        result = mod._drain_sse_stream(
            stream, provider, "openai_responses", None, mod.time.time()
        )

        self.assertEqual(result["text"], "5")
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertEqual(result["usage"]["output_tokens"], 2)
        self.assertEqual(result["usage"]["total_tokens"], 12)
        self.assertFalse(result["stream_truncated"])

    def test_probe_stream_preserves_responses_failed_reason(self):
        provider = self._responses_provider()
        body = (
            b'event: response.failed\r\n'
            b'data: {"type":"response.failed","response":{"status":"failed",'
            b'"error":{"code":"server_error","message":"quota exceeded"}}}\r\n\r\n'
        )
        original = mod.urllib.request.urlopen

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter([body])

        try:
            mod.urllib.request.urlopen = lambda *args, **kwargs: FakeResponse()
            result = mod.probe_stream(
                provider,
                provider.tiers[0],
                timeout=1,
                skip_tls_verify=False,
                expected="5",
            )
        finally:
            mod.urllib.request.urlopen = original

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error_category"], mod.ErrorCategory.INVALID_RESPONSE.value
        )
        self.assertIn("quota exceeded", result["error"])
        self.assertNotEqual(
            result["error_category"], mod.ErrorCategory.STREAM_INCOMPLETE.value
        )

    def test_probe_stream_sanitizes_text(self):
        provider = self._responses_provider()
        provider = mod.Provider(
            name=provider.name,
            app_type=provider.app_type,
            base_url=provider.base_url,
            api_key="sk-stream-secret",
            auth_mode=provider.auth_mode,
            protocol=provider.protocol,
            tiers=provider.tiers,
        )
        original = mod.urllib.request.urlopen
        body = (
            b'event: response.output_text.delta\r\n'
            b'data: {"type":"response.output_text.delta",'
            b'"delta":"answer sk-stream-secret\x1b[31m"}\r\n\r\n'
            b'event: response.completed\r\n'
            b'data: {"type":"response.completed",'
            b'"response":{"usage":{"input_tokens":1,"output_tokens":1}}}\r\n\r\n'
        )

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter([body])

        try:
            mod.urllib.request.urlopen = lambda *args, **kwargs: FakeResponse()
            result = mod.probe_stream(
                provider,
                provider.tiers[0],
                timeout=1,
                skip_tls_verify=False,
                expected="answer sk-stream-secret",
            )
        finally:
            mod.urllib.request.urlopen = original
        self.assertNotIn(provider.api_key, result["text"])
        self.assertNotRegex(result["text"], r"[\x00-\x1f\x7f]")

    def test_probe_tier_sanitizes_answer(self):
        provider = self._responses_provider()
        provider = mod.Provider(
            name=provider.name,
            app_type=provider.app_type,
            base_url=provider.base_url,
            api_key="sk-ordinary-secret",
            auth_mode=provider.auth_mode,
            protocol=provider.protocol,
            tiers=provider.tiers,
        )
        original = mod._http_request
        try:
            mod._http_request = lambda *args, **kwargs: type(
                "Response",
                (),
                {
                    "status": 200,
                    "body": '{"output_text":"answer sk-ordinary-secret\x1b[31m"}',
                    "content_type": "application/json",
                    "error_category": None,
                    "error_msg": "",
                },
            )()
            result = mod.probe_tier(provider, provider.tiers[0], 1, False)
        finally:
            mod._http_request = original
        self.assertNotIn(provider.api_key, result["answer"])
        self.assertNotRegex(result["answer"], r"[\x00-\x1f\x7f]")

    def test_probe_tier_structures_200_top_level_error(self):
        provider = self._responses_provider()
        original = mod._http_request
        try:
            mod._http_request = lambda *args, **kwargs: type(
                "Response",
                (),
                {
                    "status": 200,
                    "body": '{"error":{"message":"backend failed"}}',
                    "content_type": "application/json",
                    "error_category": None,
                    "error_msg": "",
                },
            )()
            result = mod.probe_tier(provider, provider.tiers[0], 1, False)
        finally:
            mod._http_request = original
        self.assertEqual(result["status"], 200)
        self.assertEqual(
            result["error_category"], mod.ErrorCategory.INVALID_RESPONSE.value
        )
        self.assertIn("backend failed", result["error"])
class BatchTwoProtocolPayloadTest(unittest.TestCase):
    def _claude_openai_chat_provider(self):
        return mod.Provider(
            name="claude-openai-chat",
            app_type="claude",
            base_url="https://example.test",
            api_key="",
            auth_mode="none",
            protocol=mod.Protocol.OPENAI_CHAT_COMPLETIONS,
            tiers=[mod.ModelTier("default", "model", "model")],
        )

    def test_tools_payload_follows_explicit_openai_chat_protocol(self):
        provider = self._claude_openai_chat_provider()
        original = mod._http_request
        captured = {}

        def fake_request(url, method, headers, body, timeout, skip_tls_verify):
            captured["payload"] = __import__("json").loads(body)
            return type("Response", (), {"error_category": None, "status": 200, "body": '{"choices":[{"message":{"tool_calls":[{"function":{"name":"get_probe_number"}}]}}]}'})()

        try:
            mod._http_request = fake_request
            result = mod._probe_tools(provider, "model", 1, False)
        finally:
            mod._http_request = original

        payload = captured["payload"]
        self.assertEqual(result["protocol_support"], "native")
        self.assertIn("choices", __import__("json").loads(result["evidence"]))
        self.assertIn("function", payload["tools"][0])
        self.assertNotIn("input_schema", payload["tools"][0])
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertNotIn("thinking", payload)

    def test_vision_payload_follows_explicit_openai_chat_protocol(self):
        provider = self._claude_openai_chat_provider()
        original = mod._http_request
        captured = {}

        def fake_request(url, method, headers, body, timeout, skip_tls_verify):
            captured["payload"] = __import__("json").loads(body)
            return type("Response", (), {"error_category": None, "status": 200, "body": '{"choices":[{"message":{"content":"red"}}]}'})()

        try:
            mod._http_request = fake_request
            result = mod._probe_vision(provider, "model", 1, False)
        finally:
            mod._http_request = original

        content = captured["payload"]["messages"][0]["content"]
        self.assertEqual(result["status"], "pass")
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertNotIn("thinking", captured["payload"])

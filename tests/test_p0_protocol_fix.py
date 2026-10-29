"""P0 协议与流式回归测试。"""

import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SPEC = importlib.util.spec_from_file_location(
    "ccpulse_p0", os.path.join(_ROOT, "check_ccswitch_health.py")
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)


class ProtocolEnumSanity(unittest.TestCase):
    def test_provider_accepts_explicit_protocol(self):
        provider = mod.Provider(
            name="test",
            app_type="claude",
            base_url="https://example.test",
            api_key="test",
            auth_mode="bearer",
            protocol=mod.Protocol.OPENAI_CHAT_COMPLETIONS,
        )
        self.assertEqual(provider.protocol, mod.Protocol.OPENAI_CHAT_COMPLETIONS)
        self.assertEqual(
            mod.detect_protocol(provider)["detected"], "openai_chat_completions"
        )


class ProbeStreamConsistency(unittest.TestCase):
    def test_stream_uses_supplied_expected_answer(self):
        answer = "答案是 13"
        self.assertTrue(mod._answer_correct(answer, "13"))
        self.assertFalse(mod._answer_correct(answer, "5"))
        with open(
            os.path.join(_ROOT, "ccpulse_probe.py"), encoding="utf-8"
        ) as file:
            source = file.read()
        start = source.index("def probe_stream(")
        end = source.index("\n\ndef _status_badge", start)
        self.assertIn("_answer_correct(answer_text, expected)", source[start:end])


class HealthCheckWrongAnswerDetail(unittest.TestCase):
    def test_http_200_wrong_answer_shows_reason(self):
        provider = mod.Provider(
            name="WrongAnswer",
            app_type="claude",
            base_url="https://example.test",
            api_key="",
            auth_mode="none",
            tiers=[mod.ModelTier("haiku", "model", "model")],
        )
        args = type(
            "Args",
            (),
            {
                "current_only": False,
                "failover_only": False,
                "stealth": False,
                "workers": 1,
                "timeout": 1,
                "db": ":memory:",
                "probe_max_tokens": 16,
                "probe_enable_thinking": False,
                "user_agent": None,
                "stainless_version": None,
                "with_history": False,
                "json": False,
                "skip_tls_verify": False,
                "retry": 0,
            },
        )()
        result = {
            "name": "WrongAnswer",
            "type": "claude",
            "base_url": "https://example.test",
            "auth_mode": "none",
            "overall_ok": False,
            "best_tier": None,
            "attempts": [
                {
                    "tier": "haiku",
                    "model": "model",
                    "status": 200,
                    "elapsed": 0.1,
                    "error": "",
                    "correct": False,
                    "answer": "错误答案",
                }
            ],
        }
        original_probe = mod.probe
        lines = []
        try:
            mod.probe = lambda *args, **kwargs: result
            rc = mod.run_health_check(args, [provider], lines.append)
        finally:
            mod.probe = original_probe
        output = "\n".join(lines)
        self.assertEqual(rc, 1)
        self.assertIn("不可用详情", output)
        self.assertIn("答案不符，实际回答", output)
        self.assertIn("错误答案", output)


class HealthCheckProbeBinding(unittest.TestCase):
    def test_run_health_check_calls_probe_without_shadowing(self):
        calls = []
        providers = [
            mod.Provider(
                name=name,
                app_type="claude",
                base_url="https://example.test",
                api_key="",
                auth_mode="none",
                tiers=[mod.ModelTier("haiku", "model", "model")],
            )
            for name in ("Provider-A", "Provider-B")
        ]
        args = type(
            "Args",
            (),
            {
                "current_only": False,
                "failover_only": False,
                "stealth": False,
                "workers": 1,
                "timeout": 1,
                "db": ":memory:",
                "probe_max_tokens": 16,
                "probe_enable_thinking": False,
                "user_agent": None,
                "stainless_version": None,
                "with_history": False,
                "json": False,
                "skip_tls_verify": False,
                "retry": 0,
            },
        )()

        def fake_probe(provider, *probe_args, **probe_kwargs):
            calls.append(provider.name)
            return {
                "name": provider.name,
                "type": "claude",
                "base_url": provider.base_url,
                "auth_mode": "none",
                "overall_ok": True,
                "best_tier": "haiku",
                "attempts": [
                    {
                        "tier": "haiku",
                        "model": "model",
                        "status": 200,
                        "elapsed": 0.1,
                        "error": "",
                        "correct": True,
                        "answer": "5",
                    }
                ],
            }

        original_probe = mod.probe
        lines = []
        try:
            mod.probe = fake_probe
            rc = mod.run_health_check(args, providers, lines.append)
        finally:
            mod.probe = original_probe
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["Provider-A", "Provider-B"])
        self.assertIn("可用详情", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()

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
            os.path.join(_ROOT, "check_ccswitch_health.py"), encoding="utf-8"
        ) as file:
            source = file.read()
        start = source.index("def probe_stream(")
        end = source.index("\n\ndef _status_badge", start)
        self.assertIn("_answer_correct(answer_text, expected)", source[start:end])


if __name__ == "__main__":
    unittest.main()

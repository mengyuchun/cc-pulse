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
        v, ev = mod._authenticity_verdict(auth)
        self.assertEqual(v, "clean")


if __name__ == "__main__":
    unittest.main()

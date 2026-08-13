"""P0 模型保真鉴别测试：换芯字段检测 + thinking 签名提取。

TDD：先写测试（RED），再实现 ccpulse_probe 里的三个函数使其通过（GREEN）。
覆盖 docs/PRD-authenticity-p0.md 第 5 节验收表 1-13。端到端 #14 在 test_ccpulse_full.py。
"""

import importlib.util
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SPEC = importlib.util.spec_from_file_location(
    "ccpulse_auth", os.path.join(_ROOT, "ccpulse_probe.py")
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)


# ── 3.1 换芯字段检测 ──────────────────────────────────────────────

class CrosspackOpenAITest(unittest.TestCase):
    def test_anthropic_field_in_openai_usage_is_suspicious(self):
        """OpenAI 响应 usage 冒出 Anthropic 专属字段 → 可疑。"""
        body = json.dumps({
            "model": "gpt-5",
            "choices": [{"message": {"content": "5"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cache_creation_input_tokens": 5,
            },
        })
        r = mod._detect_crosspack_fields(body, "codex")
        self.assertTrue(r["suspicious"])
        fields = [f["field"] for f in r["findings"]]
        self.assertIn("usage.cache_creation_input_tokens", fields)

    def test_usage_source_anthropic_is_suspicious(self):
        """顶层 usage_source 自曝后端来源 → 可疑。"""
        body = json.dumps({
            "model": "gpt-5",
            "choices": [{"message": {"content": "5"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "usage_source": "anthropic",
        })
        r = mod._detect_crosspack_fields(body, "codex")
        self.assertTrue(r["suspicious"])
        self.assertIn("usage_source", [f["field"] for f in r["findings"]])

    def test_pure_openai_usage_is_clean(self):
        """纯 OpenAI usage（仅三键）→ 不可疑。"""
        body = json.dumps({
            "model": "gpt-5",
            "choices": [{"message": {"content": "5"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })
        r = mod._detect_crosspack_fields(body, "codex")
        self.assertFalse(r["suspicious"])
        self.assertEqual(r["findings"], [])

    def test_system_fingerprint_in_anthropic_is_suspicious(self):
        """Anthropic 响应出现 system_fingerprint（OpenAI 专属）→ 可疑。"""
        body = json.dumps({
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "5"}],
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "system_fingerprint": "fp_abc",
        })
        r = mod._detect_crosspack_fields(body, "claude")
        self.assertTrue(r["suspicious"])

    def test_pure_anthropic_is_clean(self):
        """纯 Anthropic 响应 → 不可疑。"""
        body = json.dumps({
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "5"}],
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "stop_reason": "end_turn",
        })
        r = mod._detect_crosspack_fields(body, "claude")
        self.assertFalse(r["suspicious"])

    def test_non_json_is_clean_with_note(self):
        """非 JSON / 空串 → 不可疑，但 note 标注无法解析。"""
        r = mod._detect_crosspack_fields("", "claude")
        self.assertFalse(r["suspicious"])
        self.assertIn("note", r)
        r2 = mod._detect_crosspack_fields("not json", "claude")
        self.assertFalse(r2["suspicious"])


# ── 3.2 thinking 签名提取 ──────────────────────────────────────────

class ThinkingSignatureTest(unittest.TestCase):
    def test_anthropic_thinking_with_signature(self):
        """Anthropic content[].type=thinking + signature 非空 → has_valid_signature=True。"""
        body = json.dumps({
            "model": "claude-sonnet-4-5",
            "content": [
                {"type": "thinking", "thinking": "let me compute", "signature": "EuYBCo..."},
                {"type": "text", "text": "5"},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 8},
        })
        sigs = mod._extract_thinking_signatures(body)
        self.assertTrue(sigs)
        self.assertTrue(sigs[0]["signature_present"])
        self.assertGreater(sigs[0]["signature_length"], 0)
        # 汇总 has_valid_signature（任一签名存在即 True）
        self.assertTrue(mod._has_valid_thinking_signature(sigs))

    def test_thinking_block_without_signature(self):
        """thinking 块存在但无 signature → has_valid_signature=False（疑似伪造）。"""
        body = json.dumps({
            "model": "claude-sonnet-4-5",
            "content": [
                {"type": "thinking", "thinking": "thinking but no sig"},
                {"type": "text", "text": "5"},
            ],
        })
        sigs = mod._extract_thinking_signatures(body)
        self.assertTrue(sigs)
        self.assertFalse(sigs[0]["signature_present"])
        self.assertFalse(mod._has_valid_thinking_signature(sigs))

    def test_no_thinking_block(self):
        """无 thinking 块 → 空列表，汇总 None（不能下结论）。"""
        body = json.dumps({
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "5"}],
        })
        sigs = mod._extract_thinking_signatures(body)
        self.assertEqual(sigs, [])
        self.assertIsNone(mod._has_valid_thinking_signature(sigs))

    def test_openai_reasoning_has_no_signature(self):
        """OpenAI reasoning_content 无 signature → has_valid_signature=False。"""
        body = json.dumps({
            "model": "gpt-5",
            "choices": [{"message": {"content": "5", "reasoning_content": "thinking..."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        })
        sigs = mod._extract_thinking_signatures(body)
        # OpenAI reasoning 块应被识别为 thinking，但无签名字段
        self.assertTrue(sigs)
        self.assertFalse(sigs[0]["signature_present"])
        self.assertFalse(mod._has_valid_thinking_signature(sigs))


# ── 3.3 verdict 汇总 ───────────────────────────────────────────────

class AuthenticityVerdictTest(unittest.TestCase):
    def _clean(self):
        return {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": True},
        }

    def _crosspack_suspicious(self):
        return {
            "crosspack": {
                "suspicious": True,
                "findings": [{"field": "usage_source", "reason": "x"}],
            },
            "thinking_signature": {"has_valid_signature": True},
        }

    def _no_sig(self):
        return {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": False},
        }

    def _no_data(self):
        return {
            "crosspack": {"suspicious": False, "findings": [], "note": "无法解析"},
            "thinking_signature": {"has_valid_signature": None},
        }

    def test_crosspack_suspicious_yields_suspicious(self):
        v, ev = mod._authenticity_verdict(self._crosspack_suspicious())
        self.assertEqual(v, "suspicious")
        self.assertTrue(any("换芯" in e or "字段" in e for e in ev))

    def test_thinking_no_sig_yields_suspicious(self):
        v, ev = mod._authenticity_verdict(self._no_sig())
        self.assertEqual(v, "suspicious")
        self.assertTrue(any("签名" in e for e in ev))

    def test_all_clean_yields_clean(self):
        v, ev = mod._authenticity_verdict(self._clean())
        self.assertEqual(v, "clean")
        self.assertEqual(ev, [])

    def test_all_no_data_yields_inconclusive(self):
        v, ev = mod._authenticity_verdict(self._no_data())
        self.assertEqual(v, "inconclusive")


if __name__ == "__main__":
    unittest.main()

"""P2-C 模型保真鉴别测试：单 token 随机数分布指纹（JSD）。

TDD：先写测试（RED），再实现 _probe_js_fingerprint + JSD + 参考分布 + verdict 集成。
覆盖 docs/PRD-authenticity-p2c.md 第 7 节验收表 1-8。
"""

import importlib.util
import math
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SPEC = importlib.util.spec_from_file_location(
    "ccpulse_js", os.path.join(_ROOT, "ccpulse_probe.py")
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)


def _provider():
    return mod.Provider(
        name="T",
        app_type="claude",
        base_url="http://127.0.0.1:9/v1",
        api_key="sk-test",
        auth_mode="apikey",
        tiers=[mod.ModelTier(tier="haiku", model="claude-haiku-4-5", raw_model="claude-haiku-4-5")],
    )


class ReferenceDistributionTest(unittest.TestCase):
    def test_reference_is_valid_distribution(self):
        ref = mod.LLM_BIAS_REFERENCE
        self.assertEqual(len(ref), 100)
        self.assertAlmostEqual(sum(ref), 1.0, places=6)
        for v in ref:
            self.assertGreater(v, 0.0)


class JsdUnitTest(unittest.TestCase):
    def test_uniform_jsd_is_zero(self):
        u = [1.0 / 100] * 100
        self.assertAlmostEqual(mod._jsd(u, u), 0.0, places=9)

    def test_parse_number(self):
        self.assertEqual(mod._js_parse_number("是42"), 42)
        self.assertEqual(mod._js_parse_number("The answer is 7."), 7)
        self.assertIsNone(mod._js_parse_number("无数字"))

    def test_parse_number_clamps_range(self):
        # 范围外的不当 1-100 取
        self.assertEqual(mod._js_parse_number("100"), 100)
        self.assertEqual(mod._js_parse_number("1"), 1)
        self.assertIsNone(mod._js_parse_number("0"))
        self.assertIsNone(mod._js_parse_number("101"))


class JsProbeLogicTest(unittest.TestCase):
    def _patch(self, returns):
        seq = list(returns)
        orig = mod.probe_tier

        def fake(p, tier, *a, **kw):
            return seq.pop(0) if seq else {"status": 200, "answer": "1"}

        mod.probe_tier = fake
        return orig

    def test_uniform_observed_is_suspicious(self):
        """观测=均匀随机 → 更接近均匀 than 参考 → suspicious。"""
        p = _provider()
        # 均匀分布：每个数 1 次（50 个不同数各一次，再重复）
        nums = [(i % 100) + 1 for i in range(50)]
        returns = [{"status": 200, "answer": str(n)} for n in nums]
        orig = self._patch(returns)
        try:
            r = mod._probe_js_fingerprint(
                p, p.tiers[0], timeout=5, skip_tls=False,
                samples=50, max_tokens=16, user_agent=None,
            )
            self.assertTrue(r["suspicious"], f"note={r.get('note')}")
        finally:
            mod.probe_tier = orig

    def test_reference_observed_is_clean(self):
        """观测≈参考分布 → clean。"""
        p = _provider()
        # 集中在偏好数上（7/17/23/42/73 等），刻意造非均匀偏置
        bias_nums = [7, 17, 23, 42, 73, 37, 47, 67, 77, 83] * 5
        returns = [{"status": 200, "answer": str(n)} for n in bias_nums]
        orig = self._patch(returns)
        try:
            r = mod._probe_js_fingerprint(
                p, p.tiers[0], timeout=5, skip_tls=False,
                samples=50, max_tokens=16, user_agent=None,
            )
            self.assertFalse(r["suspicious"], f"note={r.get('note')}")
        finally:
            mod.probe_tier = orig

    def test_failure_yields_note(self):
        p = _provider()
        returns = [{"status": 429, "answer": ""}] * 50
        orig = self._patch(returns)
        try:
            r = mod._probe_js_fingerprint(
                p, p.tiers[0], timeout=5, skip_tls=False,
                samples=50, max_tokens=16, user_agent=None,
            )
            self.assertFalse(r["suspicious"])
            self.assertIn("note", r)
        finally:
            mod.probe_tier = orig

    def test_low_samples_no_verdict(self):
        """样本 < min_samples → 不判定。"""
        p = _provider()
        returns = [{"status": 200, "answer": "42"}] * 5
        orig = self._patch(returns)
        try:
            r = mod._probe_js_fingerprint(
                p, p.tiers[0], timeout=5, skip_tls=False,
                samples=5, max_tokens=16, user_agent=None,
            )
            self.assertFalse(r["suspicious"])
            self.assertIn("不足", r["note"])
        finally:
            mod.probe_tier = orig


class AuthenticityVerdictWithJs(unittest.TestCase):
    def test_js_suspicious_yields_suspicious(self):
        auth = {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": True},
            "usage_consistency": {"suspicious": False, "findings": []},
            "cache_replay": {"suspicious": False, "identical": False},
            "knowledge_cutoff": {"suspicious": False, "note": ""},
            "js_fingerprint": {
                "suspicious": True,
                "jsd_unif": 0.01,
                "jsd_ref": 0.4,
                "note": "分布近似均匀，疑似非 LLM 后端",
            },
        }
        v, ev = mod._authenticity_verdict(auth)
        self.assertEqual(v, "suspicious")
        self.assertTrue(any("分布" in e or "jsd" in e.lower() for e in ev))

    def test_all_clean_yields_clean(self):
        auth = {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": True},
            "usage_consistency": {"suspicious": False, "findings": []},
            "cache_replay": {"suspicious": False, "identical": False},
            "knowledge_cutoff": {"suspicious": False, "note": ""},
            "js_fingerprint": {"suspicious": False, "note": "符合 LLM 偏置"},
        }
        v, ev = mod._authenticity_verdict(auth)
        self.assertEqual(v, "clean")


if __name__ == "__main__":
    unittest.main()

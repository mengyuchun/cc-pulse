"""P2-A 模型保真鉴别测试：缓存回放/钳温双发探测。

TDD：先写测试（RED），再实现 _probe_cache_replay + temperature 参数 + verdict 集成。
覆盖 docs/PRD-authenticity-p2a.md 第 6 节验收表 1-8。
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
    "ccpulse_cache", os.path.join(_ROOT, "ccpulse_probe.py")
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)


# ── build_probe_request temperature 参数 ────────────────────────────

class TemperatureParamTest(unittest.TestCase):
    def _provider(self):
        return mod.Provider(
            name="T",
            app_type="claude",
            base_url="http://127.0.0.1:9/v1",
            api_key="sk-test", auth_mode="apikey",
            tiers=[mod.ModelTier(tier="haiku", model="claude-haiku-4-5", raw_model="claude-haiku-4-5")],
        )

    def test_temperature_in_body_when_set(self):
        p = self._provider()
        _, _, _, body = mod.build_probe_request(
            p, p.tiers[0], temperature=1.0, disable_thinking=True
        )
        j = json.loads(body)
        self.assertEqual(j.get("temperature"), 1.0)

    def test_temperature_absent_when_none(self):
        p = self._provider()
        _, _, _, body = mod.build_probe_request(
            p, p.tiers[0], temperature=None, disable_thinking=True
        )
        j = json.loads(body)
        self.assertNotIn("temperature", j)


# ── _probe_cache_replay 判定逻辑 ────────────────────────────────────

class CacheReplayVerdictTest(unittest.TestCase):
    """_probe_cache_replay 的判定逻辑（monkeypatch probe_tier 模拟双发）。"""

    def _provider(self):
        return mod.Provider(
            name="T",
            app_type="claude",
            base_url="http://127.0.0.1:9/v1",
            api_key="sk-test", auth_mode="apikey",
            tiers=[mod.ModelTier(tier="haiku", model="claude-haiku-4-5", raw_model="claude-haiku-4-5")],
        )

    def _patch_probe_tier(self, returns):
        """让 probe_tier 按调用序返回指定结果。"""
        seq = list(returns)
        orig = mod.probe_tier

        def fake(p, tier, *a, **kw):
            return seq.pop(0) if seq else {"status": -1}

        mod.probe_tier = fake
        return orig

    def test_identical_answers_are_suspicious(self):
        p = self._provider()
        orig = self._patch_probe_tier(
            [
                {"status": 200, "answer": "5", "raw_body": "{}"},
                {"status": 200, "answer": "5", "raw_body": "{}"},
            ]
        )
        try:
            r = mod._probe_cache_replay(
                p, p.tiers[0], timeout=5, skip_tls=False, max_tokens=16, user_agent=None
            )
            self.assertTrue(r["suspicious"])
            self.assertTrue(r["identical"])
        finally:
            mod.probe_tier = orig

    def test_different_answers_are_clean(self):
        p = self._provider()
        orig = self._patch_probe_tier(
            [
                {"status": 200, "answer": "5", "raw_body": "{}"},
                {"status": 200, "answer": "7", "raw_body": "{}"},
            ]
        )
        try:
            r = mod._probe_cache_replay(
                p, p.tiers[0], timeout=5, skip_tls=False, max_tokens=16, user_agent=None
            )
            self.assertFalse(r["suspicious"])
            self.assertFalse(r["identical"])
        finally:
            mod.probe_tier = orig

    def test_first_failure_yields_note(self):
        p = self._provider()
        orig = self._patch_probe_tier(
            [
                {"status": 429, "answer": "", "raw_body": "{}"},
                {"status": 200, "answer": "5", "raw_body": "{}"},
            ]
        )
        try:
            r = mod._probe_cache_replay(
                p, p.tiers[0], timeout=5, skip_tls=False, max_tokens=16, user_agent=None
            )
            self.assertFalse(r["suspicious"])
            self.assertIn("note", r)
        finally:
            mod.probe_tier = orig

    def test_second_failure_yields_note(self):
        p = self._provider()
        orig = self._patch_probe_tier(
            [
                {"status": 200, "answer": "5", "raw_body": "{}"},
                {"status": 500, "answer": "", "raw_body": "{}"},
            ]
        )
        try:
            r = mod._probe_cache_replay(
                p, p.tiers[0], timeout=5, skip_tls=False, max_tokens=16, user_agent=None
            )
            self.assertFalse(r["suspicious"])
            self.assertIn("note", r)
        finally:
            mod.probe_tier = orig


# ── verdict 汇总 ────────────────────────────────────────────────────

class AuthenticityVerdictWithCacheTest(unittest.TestCase):
    def test_cache_suspicious_yields_suspicious(self):
        auth = {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": True},
            "usage_consistency": {"suspicious": False, "findings": []},
            "cache_replay": {
                "suspicious": True,
                "identical": True,
                "note": "temp=1 双发逐字相同",
            },
        }
        v, ev = mod._authenticity_verdict(auth)
        self.assertEqual(v, "suspicious")
        self.assertTrue(any("缓存" in e or "钳温" in e for e in ev))

    def test_all_clean_yields_clean(self):
        auth = {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": True},
            "usage_consistency": {"suspicious": False, "findings": []},
            "cache_replay": {"suspicious": False, "identical": False},
        }
        v, ev = mod._authenticity_verdict(auth)
        self.assertEqual(v, "clean")


if __name__ == "__main__":
    unittest.main()

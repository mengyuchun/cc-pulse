"""P2-B 模型保真鉴别测试：知识截止 before/after 题库。

TDD：先写测试（RED），再实现 _probe_knowledge_cutoff + 题库 + verdict 集成。
覆盖 docs/PRD-authenticity-p2b.md 第 6 节验收表 1-8。
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
    "ccpulse_kc", os.path.join(_ROOT, "ccpulse_probe.py")
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


class KnowledgeCutoffBankTest(unittest.TestCase):
    def test_bank_has_enough_questions(self):
        before = [q for q in mod.KNOWLEDGE_CUTOFF_QUESTIONS if q["era"] == "before"]
        after = [q for q in mod.KNOWLEDGE_CUTOFF_QUESTIONS if q["era"] == "after"]
        self.assertGreaterEqual(len(before), 3)
        self.assertGreaterEqual(len(after), 3)


class KnowledgeCutoffLogicTest(unittest.TestCase):
    def _patch(self, returns):
        seq = list(returns)
        orig = mod.probe_tier

        def fake(p, tier, *a, **kw):
            return seq.pop(0) if seq else {"status": 200, "answer": ""}

        mod.probe_tier = fake
        return orig

    def test_all_before_correct_is_clean(self):
        p = _provider()
        # 6 题：3 before 对 + 3 after 随意
        returns = [
            {"status": 200, "answer": "东京"},
            {"status": 200, "answer": "2022"},
            {"status": 200, "answer": "2016"},
            {"status": 200, "answer": "2024"},
            {"status": 200, "answer": "2024"},
            {"status": 200, "answer": "2024"},
        ]
        orig = self._patch(returns)
        try:
            r = mod._probe_knowledge_cutoff(
                p, p.tiers[0], timeout=5, skip_tls=False, max_tokens=32, user_agent=None
            )
            self.assertFalse(r["suspicious"])
            self.assertEqual(r["before_correct"], 3)
        finally:
            mod.probe_tier = orig

    def test_before_wrong_is_suspicious(self):
        p = _provider()
        returns = [
            {"status": 200, "answer": "北京"},  # before 错
            {"status": 200, "answer": "2022"},
            {"status": 200, "answer": "2016"},
            {"status": 200, "answer": "2024"},
            {"status": 200, "answer": "2024"},
            {"status": 200, "answer": "2024"},
        ]
        orig = self._patch(returns)
        try:
            r = mod._probe_knowledge_cutoff(
                p, p.tiers[0], timeout=5, skip_tls=False, max_tokens=32, user_agent=None
            )
            self.assertTrue(r["suspicious"])
        finally:
            mod.probe_tier = orig

    def test_after_all_wrong_is_clean_but_noted(self):
        p = _provider()
        returns = [
            {"status": 200, "answer": "东京"},
            {"status": 200, "answer": "2022"},
            {"status": 200, "answer": "2016"},
            {"status": 200, "answer": "2020"},  # after 错
            {"status": 200, "answer": "2023"},  # after 错
            {"status": 200, "answer": "2021"},  # after 错
        ]
        orig = self._patch(returns)
        try:
            r = mod._probe_knowledge_cutoff(
                p, p.tiers[0], timeout=5, skip_tls=False, max_tokens=32, user_agent=None
            )
            self.assertFalse(r["suspicious"])  # after 错不报 suspicious
            self.assertIn("note", r)
            self.assertTrue(r["after_correct"] == 0)
        finally:
            mod.probe_tier = orig

    def test_after_all_correct_noted_as_new(self):
        p = _provider()
        returns = [
            {"status": 200, "answer": "东京"},
            {"status": 200, "answer": "2022"},
            {"status": 200, "answer": "2016"},
            {"status": 200, "answer": "2024"},
            {"status": 200, "answer": "2024"},
            {"status": 200, "answer": "2024"},
        ]
        orig = self._patch(returns)
        try:
            r = mod._probe_knowledge_cutoff(
                p, p.tiers[0], timeout=5, skip_tls=False, max_tokens=32, user_agent=None
            )
            self.assertEqual(r["after_correct"], 3)
            self.assertIn("较新", r["note"] + r.get("era_note", ""))
        finally:
            mod.probe_tier = orig

    def test_failure_yields_note(self):
        p = _provider()
        returns = [{"status": 429, "answer": ""}] * 6
        orig = self._patch(returns)
        try:
            r = mod._probe_knowledge_cutoff(
                p, p.tiers[0], timeout=5, skip_tls=False, max_tokens=32, user_agent=None
            )
            self.assertFalse(r["suspicious"])
            self.assertIn("note", r)
        finally:
            mod.probe_tier = orig

    def test_loose_scoring_year_in_answer(self):
        """判分宽松：答案含目标年份即对。"""
        r = mod._kc_check_answer("这是2024年发生的事", "2024")
        self.assertTrue(r)


class AuthenticityVerdictWithKC(unittest.TestCase):
    def test_kc_suspicious_yields_suspicious(self):
        auth = {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": True},
            "usage_consistency": {"suspicious": False, "findings": []},
            "cache_replay": {"suspicious": False, "identical": False},
            "knowledge_cutoff": {
                "suspicious": True,
                "before_correct": 2,
                "before_total": 3,
                "note": "before 题答错",
            },
        }
        v, ev = mod._authenticity_verdict(auth)
        self.assertEqual(v, "suspicious")
        self.assertTrue(any("知识截止" in e or "before" in e.lower() for e in ev))

    def test_all_clean_yields_clean(self):
        auth = {
            "crosspack": {"suspicious": False, "findings": []},
            "thinking_signature": {"has_valid_signature": True},
            "usage_consistency": {"suspicious": False, "findings": []},
            "cache_replay": {"suspicious": False, "identical": False},
            "knowledge_cutoff": {"suspicious": False, "note": "较新模型"},
        }
        v, ev = mod._authenticity_verdict(auth)
        self.assertEqual(v, "clean")


if __name__ == "__main__":
    unittest.main()

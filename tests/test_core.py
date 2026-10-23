"""核心逻辑单元测试。

覆盖三件最不能出错的事：
    1. 归一化在三种方向下都正确
    2. 证据等级确实产生折减（弱证据拿不到高分）
    3. 校准指标算得对（Brier / ECE / 过度自信）
"""

from __future__ import annotations

import os
import tempfile
import unittest

from lifeplan.calibration import Prediction, compute_calibration
from lifeplan.evidence import apply_evidence_gap, infer_tier, tier_weight
from lifeplan.metrics import Metric, compute_metric_state, normalize_value


def mk_metric(**kw) -> Metric:
    base = dict(
        id=1, goal_id=1, name="m", unit="", direction="increase",
        baseline=0.0, target=100.0, tolerance=0.0, weight=1.0,
        cadence="weekly", evidence_tier=2, is_proxy=False, proxy_for="",
        baseline_at="2026-01-01",
    )
    base.update(kw)
    return Metric(**base)


class Obs:
    def __init__(self, value, observed_at="2026-10-20", tier=2, id=1):
        self.id = id
        self.metric_id = 1
        self.value = value
        self.observed_at = observed_at
        self.note = ""
        self.evidence_ref = ""
        self.tier = tier


class TestNormalize(unittest.TestCase):
    def test_increase(self):
        m = mk_metric(direction="increase", baseline=0, target=100)
        self.assertAlmostEqual(normalize_value(0, m), 0.0)
        self.assertAlmostEqual(normalize_value(50, m), 0.5)
        self.assertAlmostEqual(normalize_value(100, m), 1.0)
        self.assertAlmostEqual(normalize_value(150, m), 1.5)  # 允许超额，不截断

    def test_decrease(self):
        m = mk_metric(direction="decrease", baseline=100, target=0)
        self.assertAlmostEqual(normalize_value(100, m), 0.0)
        self.assertAlmostEqual(normalize_value(50, m), 0.5)
        self.assertAlmostEqual(normalize_value(0, m), 1.0)
        self.assertAlmostEqual(normalize_value(-20, m), 1.2)

    def test_maintain_within_tolerance(self):
        m = mk_metric(direction="maintain", baseline=7.0, target=7.0, tolerance=0.8)
        self.assertAlmostEqual(normalize_value(7.0, m), 1.0)
        self.assertAlmostEqual(normalize_value(7.6, m), 1.0)
        self.assertLess(normalize_value(9.0, m), 1.0)
        self.assertGreaterEqual(normalize_value(9.0, m), 0.0)

    def test_degenerate_baseline_equals_target(self):
        m = mk_metric(direction="increase", baseline=5, target=5)
        self.assertEqual(normalize_value(5, m), 1.0)
        self.assertEqual(normalize_value(4, m), 0.0)

    def test_no_data_metric_scores_zero_not_crash(self):
        st = compute_metric_state(mk_metric(), [], "2026-10-22")
        self.assertFalse(st.has_data)
        self.assertIsNone(st.progress)
        self.assertEqual(st.confidence, 0.0)


class TestEvidence(unittest.TestCase):
    def test_tier_ordering(self):
        self.assertLess(tier_weight(0), tier_weight(1))
        self.assertLess(tier_weight(1), tier_weight(2))
        self.assertLess(tier_weight(2), tier_weight(3))

    def test_evidence_gap_penalizes_weak_evidence(self):
        strong = apply_evidence_gap(1.0, achieved_tier=3, required_tier=3)
        weak = apply_evidence_gap(1.0, achieved_tier=1, required_tier=3)
        self.assertAlmostEqual(strong, 1.0)
        self.assertLess(weak, 1.0)

    def test_no_gap_when_tier_exceeds_requirement(self):
        self.assertAlmostEqual(
            apply_evidence_gap(1.0, achieved_tier=3, required_tier=1), 1.0
        )

    def test_infer_tier(self):
        self.assertEqual(infer_tier("https://example.com/x"), 3)
        self.assertEqual(infer_tier("logs/2026-10-20.md"), 2)
        self.assertEqual(infer_tier("", "做了点事"), 1)
        self.assertEqual(infer_tier("", ""), 0)

    def test_weak_evidence_lowers_confidence_of_metric(self):
        m = mk_metric(evidence_tier=3)
        strong = compute_metric_state(m, [Obs(50, tier=3)], "2026-10-22")
        weak = compute_metric_state(m, [Obs(50, tier=1)], "2026-10-22")
        self.assertAlmostEqual(strong.progress, weak.progress)  # 价值分相同
        self.assertGreater(strong.confidence, weak.confidence)  # 置信度不同


class TestCalibration(unittest.TestCase):
    def _preds(self, spec):
        return [
            Prediction(
                id=i, goal_id=1, goal_title="g", domain_name="d",
                horizon_date="2026-10-01", p_hit=p, statement="", created_at="",
                outcome=y,
            )
            for i, (p, y) in enumerate(spec)
        ]

    def test_perfect_calibration(self):
        # 一半说 1 全部命中，一半说 0 全部不中
        preds = self._preds([(1.0, 1)] * 5 + [(0.0, 0)] * 5)
        rep = compute_calibration(preds)
        self.assertAlmostEqual(rep.brier, 0.0)
        self.assertAlmostEqual(rep.ece, 0.0)
        self.assertAlmostEqual(rep.overconfidence, 0.0)

    def test_overconfidence_detected(self):
        preds = self._preds([(0.9, 0)] * 10)
        rep = compute_calibration(preds)
        self.assertGreater(rep.overconfidence, 0.8)
        self.assertIn("过度自信", rep.verdict)

    def test_underconfidence_detected(self):
        preds = self._preds([(0.1, 1)] * 10)
        rep = compute_calibration(preds)
        self.assertLess(rep.overconfidence, -0.8)

    def test_brier_matches_manual(self):
        preds = self._preds([(0.8, 0), (0.2, 1)])
        rep = compute_calibration(preds)
        expected = ((0.8 - 0) ** 2 + (0.2 - 1) ** 2) / 2
        self.assertAlmostEqual(rep.brier, expected)

    def test_open_predictions_counted_separately(self):
        preds = self._preds([(0.5, 1)])
        preds[0].outcome = None
        rep = compute_calibration(preds)
        self.assertEqual(rep.n_resolved, 0)
        self.assertEqual(rep.n_open, 1)
        self.assertIsNone(rep.brier)

    def test_small_sample_gets_caveat(self):
        rep = compute_calibration(self._preds([(0.5, 1)] * 3))
        self.assertTrue(any("样本量" in n for n in rep.notes))


class TestStaleness(unittest.TestCase):
    def test_stale_metric_is_flagged(self):
        m = mk_metric(cadence="weekly")
        st = compute_metric_state(m, [Obs(50, "2026-01-01", tier=3)], "2026-10-22")
        self.assertEqual(st.status, "stale")
        self.assertLess(st.confidence, 0.5)

    def test_fresh_metric_is_active(self):
        m = mk_metric(cadence="weekly")
        st = compute_metric_state(m, [Obs(50, "2026-10-21", tier=3)], "2026-10-22")
        self.assertEqual(st.status, "active")


if __name__ == "__main__":
    unittest.main()

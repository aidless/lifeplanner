"""首次引导（setup）测试：只提案不预填、校验拒绝、应用正确。

钉死三件事：
    1. 生成的模板里**没有任何数值**（baseline/target/p 全是注释）
    2. 缺值/方向矛盾/p 越界一律拒绝应用
    3. 应用后指标与预测真的进了库，基线时间戳等于目标开始日
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from lifeplan.setup import generate_template, validate


class TestTemplate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        from lifeplan.db import connect, init_db

        self.conn = connect(init_db(seed=True))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_template_contains_no_numbers(self):
        """提案里不许出现已填的 baseline/target/p —— 这是本项目的核心纪律。"""
        text = generate_template(self.conn)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for key in ("baseline", "target", "p_hit"):
                self.assertFalse(
                    stripped.startswith(key + " ="),
                    f"模板里出现了已填数值：{stripped}",
                )

    def test_template_mentions_existing_goals(self):
        text = generate_template(self.conn)
        self.assertIn("[[metrics]]", text)
        self.assertIn("[[predictions]]", text)
        self.assertIn("goal_id", text)


class TestValidate(unittest.TestCase):
    def _metric(self, **kw):
        base = {"goal_id": 1, "name": "m", "unit": "u", "direction": "increase",
                "baseline": 0.0, "target": 10.0}
        base.update(kw)
        return base

    def test_missing_value_rejected(self):
        doc = {"metrics": [self._metric(baseline=None)],
               "predictions": [{"goal_id": 1, "statement": "s",
                                "horizon_date": "2026-12-31", "p_hit": 0.5}]}
        errors = validate(doc)
        self.assertTrue(any("baseline" in e for e in errors))

    def test_direction_conflict_rejected(self):
        doc = {"metrics": [self._metric(direction="decrease", baseline=1.0, target=2.0)],
               "predictions": [{"goal_id": 1, "statement": "s",
                                "horizon_date": "2026-12-31", "p_hit": 0.5}]}
        errors = validate(doc)
        self.assertTrue(any("decrease" in e for e in errors))

    def test_p_must_be_strictly_between_zero_and_one(self):
        for bad in (0, 1, 1.5, -0.2):
            doc = {"metrics": [self._metric()],
                   "predictions": [{"goal_id": 1, "statement": "s",
                                    "horizon_date": "2026-12-31", "p_hit": bad}]}
            errors = validate(doc)
            self.assertTrue(any("p_hit" in e for e in errors), f"p={bad} 应被拒绝")

    def test_valid_doc_passes(self):
        doc = {"metrics": [self._metric()],
               "predictions": [{"goal_id": 1, "statement": "s",
                                "horizon_date": "2026-12-31", "p_hit": 0.6}]}
        self.assertEqual(validate(doc), [])


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        from lifeplan.db import connect, init_db

        self.conn = connect(init_db(seed=True))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_apply_writes_metrics_and_predictions(self):
        from lifeplan.setup import apply_setup

        before_m = self.conn.execute("SELECT COUNT(*) n FROM metrics").fetchone()["n"]
        before_p = self.conn.execute("SELECT COUNT(*) n FROM predictions").fetchone()["n"]
        doc = {
            "metrics": [{"goal_id": 1, "name": "新指标", "unit": "次",
                         "direction": "increase", "baseline": 2.0, "target": 8.0,
                         "cadence": "weekly", "tier": 2}],
            "predictions": [{"goal_id": 1, "statement": "三周内达到 8 次",
                             "horizon_date": "2026-11-15", "p_hit": 0.55,
                             "note": "结算：自记 + 截图"}],
        }
        stat = apply_setup(self.conn, doc)
        self.assertEqual(stat, {"metrics": 1, "predictions": 1})
        after_m = self.conn.execute("SELECT COUNT(*) n FROM metrics").fetchone()["n"]
        after_p = self.conn.execute("SELECT COUNT(*) n FROM predictions").fetchone()["n"]
        self.assertEqual(after_m, before_m + 1)
        self.assertEqual(after_p, before_p + 1)

        # 基线时间戳必须等于目标开始日（防基线追认）
        row = self.conn.execute(
            "SELECT m.baseline_at, g.start_date FROM metrics m "
            "JOIN goals g ON g.id = m.goal_id WHERE m.name='新指标'"
        ).fetchone()
        self.assertEqual(row["baseline_at"], row["start_date"])

        # 预测的 p 与 note 落库
        p = self.conn.execute(
            "SELECT p_hit, outcome_note FROM predictions WHERE statement='三周内达到 8 次'"
        ).fetchone()
        self.assertAlmostEqual(p["p_hit"], 0.55)
        self.assertIn("结算", p["outcome_note"])


if __name__ == "__main__":
    unittest.main()

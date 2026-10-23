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


class TestOnlyScope(unittest.TestCase):
    """`--only` 的边界：只校验一部分，就只能写这一部分。

    这是「部分校验 → 全部写入」这个经典漏洞的钉子：
    若 validate(only=4) 通过了、apply 却把没校验的 goal 3 也写进去，
    那么未填数值的指标会以 None 落库，等于绕过校验。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        from lifeplan.db import connect, init_db

        self.conn = connect(init_db(seed=True))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _mixed_doc(self):
        return {
            "metrics": [
                {"goal_id": 4, "name": "周活跃天数", "unit": "天",
                 "direction": "increase", "baseline": 2.0, "target": 4.0,
                 "cadence": "weekly", "tier": 3},
                {"goal_id": 3, "name": "成稿提交数", "unit": "篇",
                 "direction": "increase", "baseline": None, "target": None,
                 "cadence": "monthly", "tier": 3},
            ],
            "predictions": [
                {"goal_id": 4, "statement": "保持月更", "horizon_date": "2027-03-31",
                 "p_hit": 0.7, "note": "结算：git"},
            ],
        }

    def test_validate_only_ignores_other_goals(self):
        """goal 3 没填值，但只看 goal 4 时不该报错。"""
        errs = validate(self._mixed_doc(), only=4)
        self.assertEqual(errs, [], f"only 范围外的不该阻塞，实际：{errs}")

    def test_validate_without_only_catches_unfilled(self):
        """不给 only 时，goal 3 的缺值必须被抓住。"""
        errs = validate(self._mixed_doc())
        self.assertTrue(any("baseline" in e for e in errs))

    def test_apply_only_writes_scoped_goals(self):
        """apply(only=4) 只写 goal 4——未校验的 goal 3 不能落库。"""
        from lifeplan.setup import apply_setup

        stat = apply_setup(self.conn, self._mixed_doc(), only=4)
        self.assertEqual(stat["metrics"], 1)
        names = [r["name"] for r in
                 self.conn.execute("SELECT name FROM metrics").fetchall()]
        self.assertIn("周活跃天数", names)
        self.assertNotIn("成稿提交数", names)

    def test_partial_validate_cannot_enable_full_write(self):
        """显式钉死：绕过 only 直接全量 apply 未填值，指标不该以 None 落库。

        这里断言的是「校验先行」的调用约定：apply 本身不做校验，
        所以 CLI 必须在 apply 前用**同一个 only** 校验。
        """
        from lifeplan.setup import apply_setup

        doc = self._mixed_doc()
        # 全量校验必然失败
        self.assertNotEqual(validate(doc), [])
        # 所以正确的调用方不会走到全量 apply；若有人只对 goal 4 校验却全量 apply，
        # 下面这行会暴露后果（None 落库），这是留给未来重构的警示断言。
        apply_setup(self.conn, doc, only=4)
        rows = self.conn.execute(
            "SELECT COUNT(*) n FROM metrics WHERE baseline IS NULL").fetchone()
        self.assertEqual(rows["n"], 0, "未校验的 goal 不应以 NULL 落库")


if __name__ == "__main__":
    unittest.main()

"""重构后的回归测试。

钉住四件事：
    1. 进度公式只有一个来源，且 maintain 方向不再被算错
    2. 报告结构：校准在前、价值分在后
    3. 无预测时看板不崩（brier/ece 为 None）
    4. schema 版本可读、老库能自动补表
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unittest

from lifeplan.calibration import overdue_days, suggested_p_hit
from lifeplan.dashboard import render_dashboard
from lifeplan.db import (
    SCHEMA_VERSION,
    connect,
    ensure_schema,
    get_meta,
    init_db,
    schema_version,
)
from lifeplan.metrics import goal_progress, normalize_value, Metric
from lifeplan.report import render_report


class RefactorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        self.db = init_db(seed=True)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestSingleProgressFormula(RefactorTestCase):
    def test_three_directions(self):
        self.assertAlmostEqual(goal_progress(50, 0, 100, "increase"), 0.5)
        self.assertAlmostEqual(goal_progress(50, 100, 0, "decrease"), 0.5)
        self.assertAlmostEqual(goal_progress(7.0, 7.0, 7.0, "maintain", 0.8), 1.0)

    def test_overshoot_not_clipped(self):
        self.assertAlmostEqual(goal_progress(150, 0, 100, "increase"), 1.5)
        # decrease 方向：低于 target 才算超额（baseline=100 → target=0，值 -20 → 1.2）
        self.assertAlmostEqual(goal_progress(-20, 100, 0, "decrease"), 1.2)
        # 未达目标的中间值不应超过 1
        self.assertAlmostEqual(goal_progress(20, 100, 0, "decrease"), 0.8)

    def test_maintain_decays_beyond_tolerance(self):
        p = goal_progress(9.0, 7.0, 7.0, "maintain", 0.8)
        self.assertLess(p, 1.0)
        self.assertGreaterEqual(p, 0.0)

    def test_normalize_value_delegates(self):
        m = Metric(
            id=1, goal_id=1, name="m", unit="", direction="decrease",
            baseline=100, target=0, tolerance=0.0, weight=1.0, cadence="weekly",
            evidence_tier=2, is_proxy=False, proxy_for="", baseline_at="2026-01-01",
        )
        self.assertEqual(normalize_value(40, m), goal_progress(40, 100, 0, "decrease"))

    def test_audit_uses_same_formula_for_maintain(self):
        """审计模块曾有一份内联副本，且漏了 maintain —— 把"维持型"当"提升型"算。

        构造：主指标是 maintain（基准 7，容差 0.8，观测 7.2 → 满进度），
        代理指标也是 maintain 且进度高。若公式正确，不应误报"代理反噬"。
        """
        from lifeplan.audit import audit_proxy_capture

        self.conn.execute("UPDATE metrics SET archived=1")  # 隔离种子指标
        self.conn.execute(
            "INSERT INTO goals(domain_id,title,description,start_date,end_date,weight,status,created_at) "
            "VALUES (1,'t','','2026-01-01','2026-12-31',1,'active','2026-01-01')"
        )
        gid = self.conn.execute("SELECT MAX(id) FROM goals").fetchone()[0]
        for name, is_proxy, target, tol in (("sleep", 0, 7.0, 0.8), ("proxy", 1, 7.0, 0.8)):
            self.conn.execute(
                "INSERT INTO metrics(goal_id,name,unit,direction,baseline,target,tolerance,"
                "weight,cadence,evidence_tier,is_proxy,proxy_for,baseline_at,created_at,archived) "
                "VALUES (?,?,'','maintain',7.0,?,?,1,'daily',2,?,'','2026-01-01','2026-01-01',0)",
                (gid, name, target, tol, is_proxy),
            )
            mid = self.conn.execute("SELECT MAX(id) FROM metrics").fetchone()[0]
            self.conn.execute(
                "INSERT INTO observations(metric_id,value,observed_at,note,evidence_ref,tier,created_at) "
                "VALUES (?,7.1,'2026-10-20','','',2,'2026-10-20')", (mid,)
            )
        self.conn.commit()

        findings = audit_proxy_capture(self.conn)
        # 两个指标都接近满分 → 不应判为代理反噬
        self.assertFalse(any(f.kind == "proxy_capture" for f in findings))


class TestReportOrder(RefactorTestCase):
    def test_calibration_before_plan(self):
        text = render_report(self.conn)
        i_cal = text.index("## 一、校准摘要")
        i_plan = text.index("## 二、计划状态")
        self.assertLess(i_cal, i_plan)

    def test_value_score_marked_as_reference(self):
        text = render_report(self.conn)
        self.assertIn("不可当作「过得好不好」的读数", text)
        self.assertIn("自造标量", text)

    def test_reliability_table_present(self):
        text = render_report(self.conn)
        self.assertIn("可靠性分箱", text)

    def test_sample_size_gap_reported(self):
        text = render_report(self.conn)
        # 种子里只有 8 条已结算，应提示还差多少条
        self.assertRegex(text, r"距离统计意义还差 \*\*\d+\*\* 条")


class TestDashboardRobustness(unittest.TestCase):
    def test_no_predictions_does_not_crash(self):
        tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = tmp.name
        try:
            path = init_db()          # 空库：无目标、无预测
            conn = connect(path)
            html = render_dashboard(conn)
            self.assertIn("<svg", html)
            self.assertIn("—", html, "缺失的校准指标应显示为破折号")
            conn.close()
        finally:
            tmp.cleanup()

    def test_calibration_kpi_first(self):
        tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = tmp.name
        try:
            path = init_db(seed=True)
            conn = connect(path)
            html = render_dashboard(conn)
            i_brier = html.index("Brier 分数")
            i_value = html.index("价值分（参考）")
            self.assertLess(i_brier, i_value, "校准类 KPI 应排在价值分之前")
            conn.close()
        finally:
            tmp.cleanup()


class TestSchemaVersion(RefactorTestCase):
    def test_version_written_on_init(self):
        self.assertEqual(schema_version(self.conn), SCHEMA_VERSION)

    def test_seed_marked(self):
        self.assertTrue(get_meta(self.conn, "seeded_at"))

    def test_legacy_db_gets_meta_table(self):
        """模拟加 meta 表之前建的库：删表后 ensure_schema 应补回来。"""
        self.conn.execute("DROP TABLE meta")
        self.conn.commit()
        self.assertEqual(schema_version(self.conn), 0)
        applied = ensure_schema(self.conn)
        self.assertIn(f"schema_version 0→{SCHEMA_VERSION}", applied)
        self.assertEqual(schema_version(self.conn), SCHEMA_VERSION)

    def test_archived_column_exists(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(metrics)").fetchall()}
        self.assertIn("archived", cols)

    def test_migrate_is_idempotent(self):
        first = ensure_schema(self.conn)
        second = ensure_schema(self.conn)
        self.assertEqual(second, [], f"重复执行不应再有变更：{first}")


class TestCalibrationHelpers(unittest.TestCase):
    def test_suggested_p_hit_range(self):
        self.assertGreater(suggested_p_hit(0.0, 10), 0.0)
        self.assertLess(suggested_p_hit(0.0, 10), 1.0)
        self.assertGreaterEqual(suggested_p_hit(1.0, 0), 0.9, "已完成时应给高概率")

    def test_overdue_days(self):
        from lifeplan.calibration import Prediction

        p = Prediction(id=1, goal_id=1, goal_title="g", domain_name="d",
                       horizon_date="2026-10-01", p_hit=0.5, statement="",
                       created_at="2026-09-01")
        self.assertEqual(overdue_days(p, "2026-10-11"), 10)
        self.assertEqual(overdue_days(p, "2026-10-01"), 0)


if __name__ == "__main__":
    unittest.main()

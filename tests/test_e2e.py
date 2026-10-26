"""端到端测试：建库 → 写入 → 评分 → 报告 → 看板。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

from lifeplan.db import connect, init_db
from lifeplan.calibration import compute_calibration, load_predictions
from lifeplan.dashboard import render_dashboard
from lifeplan.metrics import build_snapshot
from lifeplan.report import render_report
from lifeplan.seed import seed_all


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        self.path = init_db(seed=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_produces_full_snapshot(self):
        conn = connect(self.path)
        snap = build_snapshot(conn)
        self.assertGreater(len(snap.domains), 0)
        self.assertGreater(snap.metric_count, 0)
        self.assertGreater(snap.coverage, 0.0)
        self.assertLessEqual(snap.value, 1.5)
        self.assertLessEqual(snap.confidence, 1.0)
        conn.close()

    def test_unquantifiable_register_populated(self):
        conn = connect(self.path)
        snap = build_snapshot(conn)
        self.assertGreater(len(snap.unquantifiable), 0)
        conn.close()

    def test_calibration_has_resolved_samples(self):
        conn = connect(self.path)
        rep = compute_calibration(load_predictions(conn))
        self.assertGreater(rep.n_resolved, 0)
        self.assertIsNotNone(rep.brier)
        conn.close()

    def test_report_renders(self):
        conn = connect(self.path)
        text = render_report(conn)
        self.assertIn("人生规划系统", text)
        self.assertIn("证据置信度", text)
        self.assertIn("不可量化登记", text)
        conn.close()

    def test_dashboard_renders_offline(self):
        import re

        conn = connect(self.path)
        html = render_dashboard(conn)
        self.assertIn("<svg", html)
        self.assertIn("示例数据 · 虚构画像 · 只读", html)
        self.assertFalse(
            re.search(r'(src|href)="https?://', html),
            "看板不得引用任何外部资源",
        )
        conn.close()

    def test_dashboard_svgs_shrink_on_mobile(self):
        conn = connect(self.path)
        html = render_dashboard(conn)
        self.assertIn("svg { max-width:100%; height:auto; }", html)
        self.assertIn("@media (max-width:720px)", html)
        conn.close()

    def test_landing_demo_snapshot_is_present(self):
        from pathlib import Path

        demo = Path(__file__).resolve().parents[1] / "landing" / "demo" / "dashboard.html"
        self.assertTrue(demo.exists(), "landing 的示例看板快照缺失")
        self.assertIn("示例数据", demo.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

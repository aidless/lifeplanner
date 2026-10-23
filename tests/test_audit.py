"""自我审计模块测试。"""

from __future__ import annotations

import os
import tempfile
import unittest

from lifeplan.audit import run_audit
from lifeplan.db import connect, init_db


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        self.path = init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def _mk(self, conn, baseline_at="2026-01-01", is_proxy=0, baseline=0, target=100,
            direction="increase"):
        conn.execute(
            "INSERT INTO domains(name, weight, color, sort_order, created_at) VALUES ('d',1,'#000',1,'2026-01-01')"
        )
        did = conn.execute("SELECT id FROM domains LIMIT 1").fetchone()[0]
        conn.execute(
            """INSERT INTO goals(domain_id, title, description, start_date, end_date, weight, status, created_at)
               VALUES (?,?,?,?,?,1,'active','2026-01-01')""",
            (did, "g", "", "2026-01-01", "2026-12-31"),
        )
        gid = conn.execute("SELECT id FROM goals LIMIT 1").fetchone()[0]
        conn.execute(
            """INSERT INTO metrics(goal_id, name, unit, direction, baseline, target, tolerance, weight,
                   cadence, evidence_tier, is_proxy, proxy_for, baseline_at, created_at)
               VALUES (?,?,?,?,?,?,0,1,'weekly',2,?,'',?,'2026-01-01')""",
            (gid, "m", "", direction, baseline, target, is_proxy, baseline_at),
        )
        mid = conn.execute("SELECT id FROM metrics LIMIT 1").fetchone()[0]
        conn.commit()
        return did, gid, mid

    def _obs(self, conn, mid, value, date, tier=2, ref=""):
        conn.execute(
            """INSERT INTO observations(metric_id, value, observed_at, note, evidence_ref, tier, created_at)
               VALUES (?,?,?,'',?,?,'2026-01-01')""",
            (mid, value, date, ref, tier),
        )
        conn.commit()

    def test_baseline_drift_detected(self):
        conn = connect(self.path)
        _, _, mid = self._mk(conn, baseline_at="2026-06-01")
        self._obs(conn, mid, 10, "2026-03-01")
        rep = run_audit(conn)
        kinds = {f.kind for f in rep.findings}
        self.assertIn("baseline_drift", kinds)
        conn.close()

    def test_no_drift_when_baseline_precedes_observations(self):
        conn = connect(self.path)
        _, _, mid = self._mk(conn, baseline_at="2026-01-01")
        self._obs(conn, mid, 10, "2026-03-01")
        rep = run_audit(conn)
        self.assertEqual(len(rep.by_kind("baseline_drift")), 0)
        conn.close()

    def test_evidence_inflation_detected(self):
        conn = connect(self.path)
        _, _, mid = self._mk(conn)
        self._obs(conn, mid, 10, "2026-03-01", tier=1, ref="same-ref")
        self._obs(conn, mid, 20, "2026-04-01", tier=3, ref="same-ref")
        rep = run_audit(conn)
        self.assertEqual(len(rep.by_kind("evidence_inflation")), 1)
        conn.close()

    def test_unreachable_evidence_detected(self):
        conn = connect(self.path)
        _, _, mid = self._mk(conn)
        self._obs(conn, mid, 10, "2026-03-01", tier=2, ref="no/such/path.csv")
        rep = run_audit(conn)
        self.assertTrue(
            any(f.kind == "stale_evidence" for f in rep.findings)
        )
        conn.close()

    def test_proxy_capture_detected(self):
        conn = connect(self.path)
        _, gid, mid = self._mk(conn, is_proxy=1, baseline=0, target=100)
        self._obs(conn, mid, 90, "2026-03-01", tier=3)  # 代理指标达成
        # 追加一个主指标，进展不足
        conn.execute(
            """INSERT INTO metrics(goal_id, name, unit, direction, baseline, target, tolerance, weight,
                   cadence, evidence_tier, is_proxy, proxy_for, baseline_at, created_at)
               VALUES (?,?,?,?,?,?,0,1,'weekly',3,0,'','2026-01-01','2026-01-01')""",
            (gid, "primary", "", "increase", 0, 100),
        )
        pid = conn.execute("SELECT id FROM metrics WHERE name='primary'").fetchone()[0]
        self._obs(conn, pid, 5, "2026-03-01", tier=3)
        rep = run_audit(conn)
        self.assertTrue(any(f.kind == "proxy_capture" for f in rep.findings))
        conn.close()


if __name__ == "__main__":
    unittest.main()

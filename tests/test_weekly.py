"""每周复盘测试。

重点：
    1. 清单只包含真正需要处理的事项
    2. 归档后指标不再进入评分与提醒
    3. dry-run 绝不写库
    4. 输入中断不丢已收集的答案
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from lifeplan.db import connect, init_db
from lifeplan.metrics import build_snapshot
from lifeplan.weekly import (
    apply_answers,
    auto_suggest,
    build_week,
    run_week,
)


class WeeklyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        self.db = init_db(seed=True)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _add_due_prediction(self, days_ago: int = 2, p: float = 0.7) -> int:
        horizon = (date.today() - timedelta(days=days_ago)).isoformat()
        cur = self.conn.execute(
            "INSERT INTO predictions(goal_id,horizon_date,p_hit,statement,created_at) "
            "VALUES (1,?,?,'测试预测','2026-10-01')",
            (horizon, p),
        )
        self.conn.commit()
        return int(cur.lastrowid)


class TestBuildWeek(WeeklyTestCase):
    def test_due_prediction_appears(self):
        pid = self._add_due_prediction()
        plan = build_week(self.conn, with_suggestions=False)
        resolves = [q for q in plan.questions if q.kind == "resolve"]
        self.assertTrue(any(q.ref_id == pid for q in resolves))

    def test_future_prediction_absent(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        self.conn.execute(
            "INSERT INTO predictions(goal_id,horizon_date,p_hit,statement,created_at) "
            "VALUES (1,?,0.5,'未来预测','2026-10-01')", (future,))
        self.conn.commit()
        plan = build_week(self.conn, with_suggestions=False)
        self.assertFalse(any(q.kind == "resolve" for q in plan.questions))

    def test_limit_and_overflow(self):
        plan = build_week(self.conn, limit=3, with_suggestions=False)
        self.assertLessEqual(len(plan.questions), 3)
        self.assertGreater(plan.overflow, 0)

    def test_empty_db_has_nothing_to_do(self):
        os.environ["LIFEPLAN_HOME"] = tempfile.mkdtemp()
        empty = init_db()
        conn = connect(empty)
        try:
            plan = build_week(conn, with_suggestions=False)
            self.assertTrue(plan.empty)
        finally:
            conn.close()

    def test_archived_metric_not_listed(self):
        self.conn.execute("UPDATE metrics SET archived=1 WHERE id=1")
        self.conn.commit()
        plan = build_week(self.conn, limit=99, with_suggestions=False)
        self.assertFalse(any(q.ref_id == 1 and q.kind == "observe" for q in plan.questions))


class TestAutoSuggest(WeeklyTestCase):
    def test_commit_metric_gets_suggestion(self):
        # 找到「月度有效提交数」的 id
        row = self.conn.execute(
            "SELECT id FROM metrics WHERE name LIKE '%提交%'").fetchone()
        self.assertIsNotNone(row)
        with patch("lifeplan.weekly.scan_projects",
                   return_value=[{"path": "/tmp/x", "is_git": True}]), \
             patch("lifeplan.weekly._git_commit_count", return_value=42):
            suggestions, notes = auto_suggest(self.conn, root=Path("/tmp"))
        self.assertEqual(suggestions.get(row[0]), 42.0)
        self.assertTrue(notes)

    def test_no_repos_no_suggestion(self):
        with patch("lifeplan.weekly.scan_projects", return_value=[]):
            suggestions, _ = auto_suggest(self.conn, root=Path("/tmp"))
        self.assertEqual(suggestions, {})


class TestRunWeek(WeeklyTestCase):
    def _answers(self, seq):
        it = iter(seq)

        def ask(_prompt=""):
            try:
                return next(it)
            except StopIteration:
                raise EOFError

        return ask

    def test_resolve_flow(self):
        pid = self._add_due_prediction()
        echo = lambda *a, **k: None
        run_week(self.conn, ask=self._answers(["y", "s", "s", "s", "s", "s", "s"]),
                 echo=echo, limit=2, with_suggestions=False)
        row = self.conn.execute("SELECT outcome FROM predictions WHERE id=?", (pid,)).fetchone()
        self.assertEqual(row[0], 1)

    def test_observe_flow_writes_tier1(self):
        echo = lambda *a, **k: None
        plan = build_week(self.conn, limit=99, with_suggestions=False)
        obs_q = [q for q in plan.questions if q.kind == "observe"]
        if not obs_q:
            self.skipTest("种子数据没有待补记指标")
        mid = obs_q[0].ref_id
        answers = ["s"] * len([q for q in plan.questions if q.kind == "resolve"])
        # 第一个 observe 填 7，其余跳过
        answers.append("7")
        answers += ["s"] * (len(plan.questions) - len(answers))
        run_week(self.conn, ask=self._answers(answers), echo=echo, limit=99, with_suggestions=False)
        row = self.conn.execute(
            "SELECT value, tier, evidence_ref FROM observations "
            "WHERE metric_id=? AND evidence_ref='week-review'", (mid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["value"], 7.0)
        self.assertEqual(row["tier"], 1, "补录属于自述级证据，不应冒充可核验")

    def test_archive_then_excluded_from_snapshot(self):
        echo = lambda *a, **k: None
        plan = build_week(self.conn, limit=99, with_suggestions=False)
        obs_q = [q for q in plan.questions if q.kind == "observe"]
        if not obs_q:
            self.skipTest("无待补记指标")
        mid = obs_q[0].ref_id
        answers = ["s"] * len([q for q in plan.questions if q.kind == "resolve"])
        answers.append("a")
        answers += ["s"] * (len(plan.questions) - len(answers))
        run_week(self.conn, ask=self._answers(answers), echo=echo, limit=99, with_suggestions=False)
        snap = build_snapshot(self.conn)
        ids = [m.metric.id for d in snap.domains for g in d.goals for m in g.metrics]
        self.assertNotIn(mid, ids, "归档指标不应再出现在快照里")

    def test_dry_run_writes_nothing(self):
        self._add_due_prediction()
        before_obs = self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        before_res = self.conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE outcome IS NOT NULL").fetchone()[0]
        echo = lambda *a, **k: None
        out = run_week(self.conn, ask=self._answers(["y"] * 20), echo=echo,
                       limit=99, dry_run=True, with_suggestions=False)
        self.assertTrue(out["dry_run"])
        after_obs = self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        after_res = self.conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE outcome IS NOT NULL").fetchone()[0]
        self.assertEqual(before_obs, after_obs)
        self.assertEqual(before_res, after_res)

    def test_eof_keeps_collected_answers(self):
        pid = self._add_due_prediction()
        echo = lambda *a, **k: None
        run_week(self.conn, ask=self._answers(["y"]), echo=echo, limit=2, with_suggestions=False)
        row = self.conn.execute("SELECT outcome FROM predictions WHERE id=?", (pid,)).fetchone()
        self.assertEqual(row[0], 1, "中断前已给的答案必须落库")

    def test_invalid_number_skipped(self):
        echo = lambda *a, **k: None
        plan = build_week(self.conn, limit=99, with_suggestions=False)
        obs_q = [q for q in plan.questions if q.kind == "observe"]
        if not obs_q:
            self.skipTest("无待补记指标")
        answers = ["s"] * len([q for q in plan.questions if q.kind == "resolve"])
        answers.append("不是数字")
        answers += ["s"] * (len(plan.questions) - len(answers))
        out = run_week(self.conn, ask=self._answers(answers), echo=echo, limit=99, with_suggestions=False)
        self.assertEqual(out["observed"], 0, "非法输入不应写入")


class TestApplyAnswers(WeeklyTestCase):
    def test_counts(self):
        counts = apply_answers(self.conn, [
            ("observe", 1, 3.0),
            ("observe", 2, 4.0),
            ("archive", 3),
        ])
        self.assertEqual(counts["observed"], 2)
        self.assertEqual(counts["archived"], 1)


if __name__ == "__main__":
    unittest.main()

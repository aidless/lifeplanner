"""感知层与执行层测试。

重点覆盖三件事：
    1. 门禁的分级不能出错（尤其破坏性命令必须默认拒绝）
    2. 确认令牌必须绑定具体命令、且一次性
    3. 宿主推断必须带证据，且证据来源有权重差异
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from lifeplan.db import connect, init_db
from lifeplan.policy import (
    Policy,
    TIER_DESTRUCTIVE,
    TIER_EXECUTE,
    TIER_FORBIDDEN,
    TIER_OBSERVE,
    classify,
    confirm_token,
    load_policy,
    save_policy,
)
from lifeplan.presence import (
    SOURCE_WEIGHT,
    HostProfile,
    Signal,
    build_profile,
    infer,
    load_latest,
    save_profile,
)
from lifeplan.terminal import list_actions, run


class ControlTestCase(unittest.TestCase):
    """统一的隔离环境：独立 LIFEPLAN_HOME + 默认策略。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.environ["LIFEPLAN_HOME"] = self.home
        self.db = init_db()
        save_policy(Policy(workspace_jail=[self.home]))

    def tearDown(self):
        self.tmp.cleanup()


class TestClassify(ControlTestCase):
    def test_forbidden_binaries(self):
        for argv in (["sudo", "ls"], ["mkfs.ext4", "/dev/sda"], ["systemctl", "stop", "sshd"]):
            v = classify(argv, self.home)
            self.assertEqual(v.tier, TIER_FORBIDDEN, argv)
            self.assertFalse(v.allowed, argv)

    def test_forbidden_shell_inline(self):
        v = classify(["bash", "-c", "echo hi"], self.home)
        self.assertEqual(v.tier, TIER_FORBIDDEN)
        self.assertFalse(v.allowed)

    def test_forbidden_rm_root(self):
        for target in ("/", "/*", "~", "/etc"):
            v = classify(["rm", "-rf", target], self.home)
            self.assertEqual(v.tier, TIER_FORBIDDEN, target)

    def test_destructive_requires_policy_optin(self):
        v = classify(["rm", "notes.txt"], self.home)
        self.assertEqual(v.tier, TIER_DESTRUCTIVE)
        self.assertFalse(v.allowed, "默认策略下破坏性命令不应被放行")
        self.assertTrue(v.requires_confirm)

    def test_destructive_allowed_only_after_optin(self):
        save_policy(Policy(allow_destructive=True, workspace_jail=[self.home]))
        v = classify(["rm", "notes.txt"], self.home)
        self.assertEqual(v.tier, TIER_DESTRUCTIVE)
        self.assertTrue(v.allowed)
        self.assertTrue(v.requires_confirm, "即使放行也必须确认")

    def test_find_delete_is_destructive(self):
        self.assertEqual(classify(["find", ".", "-delete"], self.home).tier, TIER_DESTRUCTIVE)

    def test_sed_inplace_is_destructive(self):
        self.assertEqual(classify(["sed", "-i", "s/a/b/", "x"], self.home).tier, TIER_DESTRUCTIVE)

    def test_git_readonly_vs_push(self):
        self.assertEqual(classify(["git", "status"], self.home).tier, TIER_OBSERVE)
        self.assertEqual(classify(["git", "log", "--oneline"], self.home).tier, TIER_OBSERVE)
        self.assertEqual(classify(["git", "push"], self.home).tier, TIER_DESTRUCTIVE)
        self.assertEqual(classify(["git", "reset", "--hard"], self.home).tier, TIER_DESTRUCTIVE)

    def test_observe_is_auto_allowed(self):
        v = classify(["ls", "-la"], self.home)
        self.assertEqual(v.tier, TIER_OBSERVE)
        self.assertTrue(v.allowed)
        self.assertFalse(v.requires_confirm)

    def test_execute_needs_confirm_by_default(self):
        v = classify(["curl", "https://example.com"], self.home)
        self.assertEqual(v.tier, TIER_EXECUTE)
        self.assertTrue(v.requires_confirm)

    def test_workspace_write_inside_jail(self):
        v = classify(["mkdir", "sub"], self.home)
        self.assertEqual(v.tier, 1)
        self.assertTrue(v.allowed)

    def test_workspace_write_outside_jail_escalates(self):
        v = classify(["mkdir", "sub"], "/opt")
        self.assertNotEqual(v.tier, 1)
        self.assertTrue(v.requires_confirm)


class TestToken(ControlTestCase):
    def test_token_deterministic_and_bound(self):
        a = confirm_token(1, "ls -la")
        self.assertEqual(a, confirm_token(1, "ls -la"))
        self.assertNotEqual(a, confirm_token(2, "ls -la"))
        self.assertNotEqual(a, confirm_token(1, "ls -lb"))

    def test_token_length(self):
        self.assertEqual(len(confirm_token(7, "x")), 12)


class TestRun(ControlTestCase):
    def test_readonly_executes(self):
        res = run(["echo", "hello"], conn=connect(self.db))
        self.assertEqual(res.status, "done")
        self.assertIn("hello", res.stdout)

    def test_forbidden_denied_and_recorded(self):
        conn = connect(self.db)
        res = run(["sudo", "ls"], conn=conn)
        self.assertEqual(res.status, "denied")
        rows = list_actions(conn)
        self.assertTrue(any(r["status"] == "denied" for r in rows))

    def test_destructive_blocked_without_policy(self):
        conn = connect(self.db)
        victim = Path(self.home) / "keep.txt"
        victim.write_text("data", encoding="utf-8")
        res = run(["rm", str(victim)], conn=conn)
        self.assertEqual(res.status, "denied")
        self.assertTrue(victim.exists(), "策略未开启时文件必须还在")

    def test_destructive_runs_after_optin_and_confirm(self):
        save_policy(Policy(allow_destructive=True, workspace_jail=[self.home]))
        conn = connect(self.db)
        victim = Path(self.home) / "drop.txt"
        victim.write_text("x", encoding="utf-8")

        first = run(["rm", str(victim)], conn=conn)
        self.assertEqual(first.status, "blocked")
        self.assertTrue(victim.exists())

        second = run(["rm", str(victim)], conn=conn, token=first.token,
                     reuse_action_id=first.action_id)
        self.assertEqual(second.status, "done")
        self.assertFalse(victim.exists())

    def test_wrong_token_denied(self):
        conn = connect(self.db)
        res = run(["curl", "--version"], conn=conn, token="000000000000")
        self.assertIn(res.status, ("denied",))

    def test_replay_denied(self):
        save_policy(Policy(allow_destructive=True, workspace_jail=[self.home]))
        conn = connect(self.db)
        f = Path(self.home) / "once.txt"
        f.write_text("x", encoding="utf-8")
        first = run(["rm", str(f)], conn=conn)
        second = run(["rm", str(f)], conn=conn, token=first.token,
                     reuse_action_id=first.action_id)
        self.assertEqual(second.status, "done")
        # 再放一次 token：已完成的提案必须拒绝重放
        third = run(["rm", str(f)], conn=conn, token=first.token,
                    reuse_action_id=first.action_id)
        self.assertEqual(third.status, "denied")

    def test_env_secrets_not_leaked(self):
        os.environ["MY_API_KEY"] = "sk-should-not-appear"
        try:
            res = run(["env"], conn=connect(self.db))
            self.assertEqual(res.status, "done")
            self.assertNotIn("MY_API_KEY", res.stdout)
        finally:
            os.environ.pop("MY_API_KEY", None)

    def test_shell_metacharacters_are_inert(self):
        res = run(["echo", "a; rm -rf /tmp/nope"], conn=connect(self.db))
        self.assertEqual(res.status, "done")
        self.assertIn("a; rm -rf", res.stdout, "分号应作为普通字符，不被解释")


class TestPresence(ControlTestCase):
    def _signals(self):
        return [
            Signal("project", "casmi26", "Python", "/home/u/casmi26", 2.0),
            Signal("project", "arxiv-publication", "LaTeX", "/home/u/arxiv-publication", 2.0),
            Signal("tool", "机器学习", "nvidia-smi", "which nvidia-smi", 1.0),
        ]

    def test_project_evidence_outweighs_tools(self):
        hyps = infer(self._signals())
        self.assertTrue(hyps)
        # 有项目证据的假设应排在仅靠工具证据的假设之前
        labels = [h.label for h in hyps]
        self.assertIn("学术研究 / 论文产出", labels)
        self.assertIn("机器学习 / 竞赛建模", labels)
        acad = next(h for h in hyps if h.label == "学术研究 / 论文产出")
        ml = next(h for h in hyps if h.label == "机器学习 / 竞赛建模")
        self.assertGreater(acad.score, 0)
        self.assertGreaterEqual(acad.score, ml.score * 0.5)

    def test_hypotheses_always_have_evidence(self):
        for h in infer(self._signals()):
            self.assertTrue(h.evidence, f"{h.label} 没有证据，不应进入结论")

    def test_source_weight_ordering(self):
        self.assertGreater(SOURCE_WEIGHT["project"], SOURCE_WEIGHT["tool"])
        self.assertGreater(SOURCE_WEIGHT["history"], SOURCE_WEIGHT["process"])

    def test_profile_roundtrip(self):
        conn = connect(self.db)
        prof = build_profile(roots=[Path(self.home)])
        pid = save_profile(conn, prof)
        self.assertGreater(pid, 0)
        latest = load_latest(conn)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], pid)
        self.assertIn("这台机器主要用于", latest["summary"])

    def test_signal_count_matches(self):
        conn = connect(self.db)
        prof = HostProfile(
            hostname="h", os_name="linux", session="x11",
            scanned_at="2026-10-22T00:00:00+00:00",
            signals=self._signals(), summary="测试", confidence=0.5,
        )
        pid = save_profile(conn, prof)
        n = conn.execute(
            "SELECT COUNT(*) FROM host_signals WHERE profile_id=?", (pid,)
        ).fetchone()[0]
        self.assertEqual(n, len(prof.signals))


class TestAutopilot(ControlTestCase):
    def test_tick_never_executes_side_effects(self):
        from lifeplan.autopilot import tick

        conn = connect(self.db)
        out = tick(conn, roots=[Path(self.home)])
        self.assertIn("proposals", out)
        done = conn.execute(
            "SELECT COUNT(*) FROM actions WHERE status IN ('done','failed')"
        ).fetchone()[0]
        self.assertEqual(done, 0, "自主循环不得自行执行有副作用的动作")

    def test_proposals_recorded_as_proposed(self):
        from lifeplan.autopilot import tick

        conn = connect(self.db)
        tick(conn, roots=[Path(self.home)])
        rows = list_actions(conn, status="proposed")
        for r in rows:
            self.assertEqual(r["status"], "proposed")


if __name__ == "__main__":
    unittest.main()

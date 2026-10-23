"""专属 agent 测试。

三件事必须被钉死：
    1. 编造的数字会被核验抓出来
    2. tier2 工具只生成提案，绝不执行
    3. 解析器对畸形模型输出不会崩，而是降级
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path

from lifeplan.agent.adapters import Decision, ScriptedAdapter, JevLocalAdapter, pick_adapter
from lifeplan.agent.loop import answer
from lifeplan.agent.prompts import parse_decision
from lifeplan.agent.tools import Context, call
from lifeplan.agent.verify import unsupported_numbers, verify
from lifeplan.db import connect, init_db


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        self.db = init_db(seed=True)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


# ── 核验 ────────────────────────────────────────────────────
class TestVerify(unittest.TestCase):
    def _obs(self, text="价值分 0.292，覆盖率 56%，指标 16 个"):
        return [{"text": text, "data": {"value": 0.292}}]

    def test_supported_numbers_pass(self):
        obs = self._obs()
        self.assertEqual(unsupported_numbers("价值分 0.292，覆盖率 56%", obs), [])

    def test_rounding_tolerated(self):
        obs = self._obs()
        # 0.292 写成 0.29 属于合理四舍五入
        self.assertEqual(unsupported_numbers("价值分约 0.29", obs), [])

    def test_percent_conversion_tolerated(self):
        obs = [{"text": "value 0.292", "data": {}}]
        self.assertEqual(unsupported_numbers("约 29.2%", obs), [])

    def test_fabricated_number_flagged(self):
        obs = self._obs()
        bad = unsupported_numbers("价值分 0.292，名次提升到 1824", obs)
        self.assertIn("1824", bad)

    def test_numbers_without_observations_all_flagged(self):
        bad = unsupported_numbers("你的 Brier 是 0.12", [])
        self.assertTrue(bad, "没有任何工具结果时，数字必须全部判为无依据")

    def test_evidence_out_of_range_flagged(self):
        obs = self._obs()
        ok, issues = verify("价值分 0.292", [1, 5], obs)
        self.assertFalse(ok)
        self.assertTrue(any("不存在" in i for i in issues))

    def test_missing_evidence_flagged(self):
        ok, issues = verify("价值分 0.292", [], self._obs())
        self.assertFalse(ok)
        self.assertTrue(any("未声明 evidence" in i for i in issues))


# ── 解析 ────────────────────────────────────────────────────
class TestParse(unittest.TestCase):
    def test_plain_json_tool_call(self):
        d = parse_decision('{"tool": "plan_score", "args": {}}')
        self.assertEqual(d.tool, "plan_score")

    def test_fenced_json(self):
        d = parse_decision('```json\n{"tool":"plan_audit","args":{}}\n```')
        self.assertEqual(d.tool, "plan_audit")

    def test_json_with_leading_prose(self):
        d = parse_decision('好的，我来查：{"tool":"host_profile","args":{}}')
        self.assertEqual(d.tool, "host_profile")

    def test_final_with_evidence(self):
        d = parse_decision('{"final":"价值分 0.292","evidence":[1,2]}')
        self.assertEqual(d.final, "价值分 0.292")
        self.assertEqual(d.evidence, [1, 2])

    def test_garbage_returns_note_not_raise(self):
        d = parse_decision("我觉得还行吧")
        self.assertIsNone(d.tool)
        self.assertIsNone(d.final)
        self.assertIn("不是合法 JSON", d.note)

    def test_empty_object(self):
        d = parse_decision("{}")
        self.assertIsNone(d.tool)
        self.assertIn("既没有", d.note)


# ── 工具 ────────────────────────────────────────────────────
class TestTools(AgentTestCase):
    def test_readonly_tool_works(self):
        res = call("plan_score", Context(conn=self.conn), {})
        self.assertTrue(res.ok)
        self.assertIn("价值分", res.text)
        self.assertEqual(res.tier, 0)

    def test_write_denied_without_flag(self):
        res = call("log_observation", Context(conn=self.conn, allow_write=False),
                   {"metric_id": 1, "value": 5})
        self.assertFalse(res.ok)
        self.assertIn("只读模式", res.text)

    def test_write_allowed_with_flag(self):
        res = call("log_observation", Context(conn=self.conn, allow_write=True),
                   {"metric_id": 1, "value": 5, "evidence": "https://example.com/x"})
        self.assertTrue(res.ok)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE metric_id=1").fetchone()[0]
        self.assertGreater(n, 0)

    def test_term_exec_only_proposes(self):
        res = call("term_exec", Context(conn=self.conn, cwd=tempfile.gettempdir()),
                   {"argv": ["ls", "-la"], "reason": "看看目录"})
        self.assertTrue(res.has_proposal)
        # 关键断言：actions 表里不能出现 done
        done = self.conn.execute(
            "SELECT COUNT(*) FROM actions WHERE status='done'").fetchone()[0]
        self.assertEqual(done, 0, "term_exec 绝不能真的执行命令")
        prop = self.conn.execute(
            "SELECT status, kind FROM actions WHERE id=?", (res.data["action_id"],)).fetchone()
        self.assertEqual(prop["status"], "proposed")

    def test_term_exec_forbidden_not_proposed(self):
        res = call("term_exec", Context(conn=self.conn), {"argv": ["sudo", "ls"]})
        self.assertFalse(res.ok)
        self.assertIn("禁止", res.text)
        n = self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        self.assertEqual(n, 0, "禁止级命令不应留下可执行提案")

    def test_app_launch_proposes(self):
        res = call("app_launch", Context(conn=self.conn), {"name": "不存在的应用xyz"})
        self.assertFalse(res.ok)

    def test_unknown_tool(self):
        res = call("nope", Context(conn=self.conn), {})
        self.assertFalse(res.ok)
        self.assertIn("未知工具", res.text)

    def test_missing_param_reported(self):
        res = call("log_observation", Context(conn=self.conn, allow_write=True), {})
        self.assertFalse(res.ok)
        self.assertIn("缺少参数", res.text)


# ── 循环 ────────────────────────────────────────────────────
class _FakeAdapter:
    """可控的假适配器：用来测试编造数字是否会被抓。"""

    name = "fake"
    kind = "model"

    def __init__(self, final: str, evidence=None, tool=None, args=None):
        self._final = final
        self._evidence = evidence or []
        self._tool = tool
        self._args = args or {}

    def status(self):
        return True, "fake"

    def decide(self, question, observations, step):
        if self._tool and step == 0:
            return Decision(tool=self._tool, args=self._args)
        return Decision(final=self._final, evidence=self._evidence)


class TestLoop(AgentTestCase):
    def test_scripted_routes_status(self):
        ans = answer("现在状态怎么样", self.conn,
                     adapter=ScriptedAdapter())
        self.assertEqual(ans.adapter, "scripted")
        self.assertTrue(ans.observations)
        self.assertEqual(ans.observations[0].tool, "plan_score")

    def test_scripted_routes_calibration(self):
        ans = answer("我对自己判断准不准", self.conn,
                     adapter=ScriptedAdapter())
        self.assertEqual(ans.observations[0].tool, "plan_calibration")

    def test_scripted_refuses_to_guess_command(self):
        ans = answer("帮我跑一下 git status", self.conn,
                     adapter=ScriptedAdapter())
        self.assertEqual(ans.observations, [])
        self.assertIn("不会从自然语言里猜命令参数", ans.text)

    def test_fabricated_number_caught(self):
        fake = _FakeAdapter(
            final="你的价值分是 0.777，名次提升到 1824",
            evidence=[1],
            tool="plan_score",
        )
        ans = answer("状态如何", self.conn, adapter=fake)
        self.assertFalse(ans.citations_ok)
        self.assertTrue(any("找不到出处" in i for i in ans.issues))

    def test_honest_answer_passes(self):
        real = answer("现在状态怎么样", self.conn, adapter=ScriptedAdapter())
        # 用真实工具文本作为答复，应当通过核验
        fake = _FakeAdapter(final=real.observations[0].text, evidence=[1],
                            tool="plan_score")
        ans = answer("状态如何", self.conn, adapter=fake)
        self.assertTrue(ans.citations_ok, ans.issues)

    def test_adapter_exception_does_not_crash(self):
        class Boom:
            name = "boom"
            kind = "model"

            def status(self):
                return True, "boom"

            def decide(self, *a, **k):
                raise RuntimeError("炸了")

        ans = answer("状态如何", self.conn, adapter=Boom())
        self.assertIn("炸了", " ".join(ans.notes))
        self.assertTrue(ans.text), "适配器崩了也要给出可读答复"


class TestAdapters(AgentTestCase):
    def test_scripted_always_available(self):
        ok, why = ScriptedAdapter().status()
        self.assertTrue(ok)

    def test_jev_without_ckpt_unavailable(self):
        os.environ["LIFEPLAN_JEV_CKPT"] = str(Path(self.tmp.name) / "nope.pt")
        try:
            ok, why = JevLocalAdapter().status()
            self.assertFalse(ok)
            self.assertIn("未找到检查点", why)
        finally:
            os.environ.pop("LIFEPLAN_JEV_CKPT", None)

    def test_pick_falls_back_to_scripted(self):
        """机器上可能真有 Ollama 在跑，所以"没有端点"必须主动模拟：
        把 base 指到一个死端口，才能确定性地走到 scripted。"""
        os.environ["LIFEPLAN_JEV_CKPT"] = str(Path(self.tmp.name) / "nope.pt")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        os.environ["LIFEPLAN_LLM_BASE_URL"] = f"http://127.0.0.1:{dead_port}/v1"
        os.environ.pop("LIFEPLAN_LLM_MODEL", None)
        try:
            ad, skipped = pick_adapter()
            self.assertEqual(ad.name, "scripted")
            self.assertTrue(skipped, "应记录被跳过的适配器原因")
        finally:
            os.environ.pop("LIFEPLAN_JEV_CKPT", None)
            os.environ.pop("LIFEPLAN_LLM_BASE_URL", None)


if __name__ == "__main__":
    unittest.main()


class TestUngroundedAnswer(AgentTestCase):
    """真模型暴露的行为：不给工具结果直接作答。必须被核验标记，不能放行。"""

    def test_final_without_tools_is_flagged(self):
        fake = _FakeAdapter(final="我觉得你状态不错，继续保持", evidence=[])
        ans = answer("状态如何", self.conn, adapter=fake)
        self.assertEqual(ans.observations, [])
        self.assertFalse(ans.citations_ok)
        self.assertTrue(any("没有数据支撑" in i for i in ans.issues))

    def test_final_with_numbers_without_tools_flagged(self):
        fake = _FakeAdapter(final="你的 Brier 是 0.12，很不错", evidence=[])
        ans = answer("状态如何", self.conn, adapter=fake)
        self.assertFalse(ans.citations_ok)
        self.assertTrue(any("没有数据支撑" in i for i in ans.issues))


class TestSystemOne(unittest.TestCase):
    """System 1（Laya）适配器：只路由零参数工具，带置信门控与升级链。

    mock 掉 _predict —— 权重 650MB，单元测试必须封闭可重复。
    真模型的端到端行为在 README 里有实测记录。
    """

    def _adapter(self, result: dict, escalate_to=None):
        from lifeplan.agent.adapters import SystemOneAdapter

        ad = SystemOneAdapter(escalate_to=escalate_to)
        ad.status = lambda: (True, "mock")
        ad._predict = lambda state, questions: result
        return ad

    def _result(self, tool: str, conf: float, enough: float | None = None) -> dict:
        out = {"answers": {"next_tool": {"choice": tool, "confidence": conf}}}
        if enough is not None:
            out["answers"]["enough"] = {"noul": enough}
        return out

    def test_zero_arg_tool_routed(self):
        ad = self._adapter(self._result("plan_score", 0.95))
        d = ad.decide("状态如何", [], 0)
        self.assertEqual(d.tool, "plan_score")
        self.assertIn("0.95", d.note)

    def test_low_confidence_escalates(self):
        from lifeplan.agent.adapters import ScriptedAdapter

        fallback = ScriptedAdapter()
        ad = self._adapter(self._result("plan_score", 0.3), escalate_to=fallback)
        d = ad.decide("状态如何", [], 0)
        # 升级后由 fallback 决策（scripted 会路由 plan_score）
        self.assertEqual(d.tool, "plan_score")
        self.assertIn("升级", d.note)

    def test_args_tool_escalates(self):
        """tier1/2 工具需要参数，System 1 给不了 —— 必须升级而不是瞎填。"""
        from lifeplan.agent.adapters import ScriptedAdapter

        ad = self._adapter(self._result("log_observation", 0.99),
                           escalate_to=ScriptedAdapter())
        d = ad.decide("记录一下", [], 0)
        self.assertIn("需要参数", d.note)

    def test_enough_gate_stops_collection(self):
        ad = self._adapter(self._result("none", 0.9, enough=0.93))
        obs = [{"tool": "plan_score", "text": "价值分 0.292"}]
        d = ad.decide("状态如何", obs, 1)
        self.assertIsNone(d.tool)
        self.assertIsNone(d.final)
        self.assertIn("信息已足够", d.note)

    def test_enough_gate_requires_observations(self):
        """还没有任何数据时，noul 说"够了"也必须先调工具（升级给 fallback）。"""
        from lifeplan.agent.adapters import ScriptedAdapter

        ad = self._adapter(self._result("none", 0.9, enough=0.93),
                           escalate_to=ScriptedAdapter())
        d = ad.decide("状态如何", [], 0)
        # 无观测且选 none → 升级（脚本路由会给出 plan_score）
        self.assertIn("升级", d.note)

    def test_pick_adapter_builds_chain_when_available(self):
        """systemone 可用时（mock status），pick_adapter 应挂上升级链。"""
        import lifeplan.agent.adapters as A

        orig = A.SystemOneAdapter.status
        try:
            A.SystemOneAdapter.status = lambda self: (True, "mock")
            ad, skipped = A.pick_adapter()
            self.assertEqual(ad.name, "systemone")
            self.assertIsNotNone(getattr(ad, "escalate_to", None),
                                 "systemone 必须带升级链，否则低置信会卡死")
        finally:
            A.SystemOneAdapter.status = orig

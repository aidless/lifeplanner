"""Q-One 测试：计算安全、台账完整、协议契约。

钉死四件事：
    1. 计算引擎永不执行任意代码（AST 白名单）
    2. 台账哈希可复算、可验伪（改一个字节就能发现）
    3. 协议契约字段齐全（value/interval/confidence/action/evidence/assumptions/unknowns）
    4. 证据不足时裁决必须是 need_data，而不是硬给一个数
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from lifeplan import calc
from lifeplan.ledger import (
    DecisionRecord,
    canonical_json,
    record,
    sha256_of,
    verify_integrity,
)
from lifeplan.qone import QOneAnswer, ask, qcompile


class TestCalcSafety(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(calc.evaluate("1 + 2 * 3"), 7)
        self.assertEqual(calc.evaluate("(a + b) / 2", {"a": 4, "b": 6}), 5)

    def test_no_code_execution(self):
        for evil in ("__import__('os')", "().__class__", "open('/etc/passwd')",
                     "os.system('x')", "lambda: 1", "[1,2]", "'abc'"):
            with self.assertRaises(calc.CalcError, msg=evil):
                calc.evaluate(evil)

    def test_division_by_zero_is_error_not_crash(self):
        with self.assertRaises(calc.CalcError):
            calc.evaluate("1 / 0")

    def test_ratio_and_mean(self):
        self.assertIsNone(calc.ratio(1, 0))
        self.assertIsNone(calc.mean([]))
        self.assertEqual(calc.ratio(1, 4), 0.25)

    def test_bootstrap_ci_deterministic_and_contains_stat(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ci1 = calc.bootstrap_ci(xs, seed=7)
        ci2 = calc.bootstrap_ci(xs, seed=7)
        self.assertEqual(ci1, ci2, "同种子必须同区间（可复算）")
        self.assertLessEqual(ci1[0], calc.mean(xs))
        self.assertGreaterEqual(ci1[1], calc.mean(xs))
        self.assertIsNone(calc.bootstrap_ci([1.0]), "单点无区间")


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        os.environ["LIFEPLAN_QCOMPILER"] = "rules"  # 测试不打网络
        from lifeplan.db import init_db

        self.db = init_db()
        from lifeplan.db import connect

        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_hash_reproducible(self):
        obj = {"b": 2, "a": 1}
        self.assertEqual(sha256_of(obj), sha256_of({"a": 1, "b": 2}),
                         "规范化 JSON 必须键序无关")

    def test_record_and_verify(self):
        record(self.conn, DecisionRecord(
            state="s", questions=[{"qid": "x"}],
            answer={"action": "continue", "value": 1},
        ))
        rep = verify_integrity(self.conn)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["checked"], 1)

    def test_tamper_detected(self):
        record(self.conn, DecisionRecord(
            state="s", questions=[], answer={"action": "continue"},
        ))
        # 直接改库里的 answer，哈希对不上 → 必须被发现
        self.conn.execute(
            "UPDATE decisions SET answer=? WHERE id=1",
            (canonical_json({"action": "stop"}),),
        )
        self.conn.commit()
        rep = verify_integrity(self.conn)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["tampered"], [1])


class TestCompiler(unittest.TestCase):
    def test_vertical_matched(self):
        vertical, questions, unknowns = qcompile("这个项目是否值得继续投入")
        self.assertEqual(vertical, "project_investment")
        self.assertTrue(questions)
        self.assertEqual(unknowns, [])
        types = {q.type for q in questions}
        self.assertLessEqual({"measure", "score", "choice", "noul"}, types,
                             "协议四类问题都要出现")

    def test_out_of_scope_refused_honestly(self):
        vertical, _, unknowns = qcompile("今天晚饭吃什么")
        self.assertEqual(vertical, "unsupported")
        self.assertTrue(unknowns, "覆盖不到的域必须显式拒绝并给 unknown")


class TestQOneContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        from lifeplan.db import connect, init_db

        self.conn = connect(init_db())

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_contract_fields(self):
        ans = ask("这个项目是否值得继续投入", self.conn, root="/tmp")
        c = ans.to_contract()
        for key in ("action", "confidence", "evidence", "assumptions",
                    "unknowns", "next_measurement"):
            self.assertIn(key, c)
        for e in c["evidence"]:
            self.assertIn("sha256", e, "每条证据都必须带内容哈希")
        self.assertIn(c["action"], ("continue", "pivot", "pause", "stop",
                                    "collect_more_evidence"))
        self.assertIn("evidence_sufficiency", c)
        self.assertIn(c["evidence_sufficiency"],
                      ("sufficient", "medium", "insufficient", "unknown"))
        # 三置信度分离（蓝图 §7）
        for k in ("model", "evidence", "forecast"):
            self.assertIn(k, c["confidence"])

    def test_empty_db_asks_for_data_not_a_number(self):
        """空库（无观测/无校准样本）时不许硬给分数——这是协议的底线。"""
        ans = ask("这个项目是否值得继续投入", self.conn, root="/tmp")
        self.assertEqual(ans.action, "collect_more_evidence")
        self.assertEqual(ans.evidence_sufficiency, "insufficient")
        self.assertTrue(ans.unknowns)

    def test_persisted_to_ledger(self):
        ask("这个项目是否值得继续投入", self.conn, root="/tmp")
        rep = verify_integrity(self.conn)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["checked"], 1)

    def test_out_of_scope_also_ledgered(self):
        ask("今天晚饭吃什么", self.conn, root="/tmp")
        rep = verify_integrity(self.conn)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["checked"], 1, "被拒绝的量化也要留痕")

    def test_interval_within_range(self):
        ans = ask("这个项目是否值得继续投入", self.conn, root="/tmp")
        it = ans.values.get("价值总评", {}).get("interval")
        if it:
            self.assertGreaterEqual(it[0], 0.0)
            self.assertLessEqual(it[1], 100.0)


class TestDecideReturnShape(unittest.TestCase):
    """`_decide` 的返回元组长度契约。

    事故记录（2026-10-23）：真实调用 `qone ask` 时抛
    `ValueError: not enough values to unpack (expected 5, got 4)`。
    根因是三处 `return` 漏掉了 `probs` 元素——拒答分支写 5 个，
    Laya 不可用分支只写 4 个。测试从未直接调用过 `_decide`，
    所以 182 项测试全绿也没发现。

    这个测试的作用是把「所有 return 分支长度一致」钉死，
    防止再加分支时又漏一个。
    """

    def test_all_branches_return_five_elements(self):
        from lifeplan.qone import _decide

        # 分支 1：拒答（无可核验证据）
        abstain = _decide(
            "随便一个问题",
            {"verified_obs_30d": 0, "calibration_n": 0, "goals_with_metrics": 0},
            {},
        )
        self.assertEqual(len(abstain), 5)
        self.assertEqual(abstain[0], "collect_more_evidence")
        self.assertEqual(abstain[2], "abstain")
        self.assertIsInstance(abstain[4], dict)

        # 分支 2：有证据但结构不完整 → 规则兜底
        rule = _decide(
            "项目投入决策",
            {"verified_obs_30d": 3, "calibration_n": 10, "goals_with_metrics": 2},
            {"价值总评": {"value": 50.0}, "结构完整度": {"value": 0.2}},
        )
        self.assertEqual(len(rule), 5)
        self.assertEqual(rule[0], "collect_more_evidence")
        self.assertEqual(rule[2], "rules")
        self.assertIsInstance(rule[3], list)
        self.assertIsInstance(rule[4], dict)

    def test_return_type_annotation_matches_arity(self):
        """签名声明 5 元组，就必须真的返回 5 个。"""
        import inspect

        from lifeplan.qone import _decide

        sig = inspect.signature(_decide)
        ret = sig.return_annotation
        # tuple[str, float, str, list, dict] → 5
        self.assertIn("dict", str(ret))
        self.assertEqual(str(ret).count(","), 4, f"返回类型应声明 5 元组，实际：{ret}")


class TestRootTypeContract(unittest.TestCase):
    """root 参数必须是 Path，传字符串会让证据采集整块失效。

    事故记录（2026-10-23）：`qone ask --root <路径>` 传入 str，
    `scan_projects` 内部执行 `Path / str` 抛 TypeError，
    被 `_evidence` 的 except 吞掉 → git 信号变 None、证据显示
    「扫描失败：TypeError」。表面看是「你没提交代码」，
    实际是类型错误 —— 这类静默降级最危险。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        from lifeplan.db import connect, init_db

        self.conn = connect(init_db())

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_str_root_is_accepted_and_converted(self):
        from pathlib import Path

        ans = ask("这个项目是否值得继续投入", self.conn, root=str(self.tmp.name))
        git_items = [e for e in ans.evidence if e.get("kind") == "git"]
        self.assertTrue(git_items, "应至少产出一条 git 证据记录")
        detail = git_items[0].get("detail", "")
        self.assertNotIn("扫描失败", detail,
                         f"传 str 的 root 不应导致扫描失败，实际：{detail}")

    def test_path_root_also_works(self):
        from pathlib import Path

        ans = ask("这个项目是否值得继续投入", self.conn, root=Path(self.tmp.name))
        git_items = [e for e in ans.evidence if e.get("kind") == "git"]
        self.assertTrue(git_items)
        self.assertNotIn("扫描失败", git_items[0].get("detail", ""))


if __name__ == "__main__":
    unittest.main()

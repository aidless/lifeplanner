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


if __name__ == "__main__":
    unittest.main()

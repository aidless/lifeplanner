"""量化编译器 v2 测试：LLM 只能在注册表内选，且任何异常都静默回退。

三条必须要钉死的：
    1. 注册表外的信号一律拒绝（防 LLM 编造测不出的问题）
    2. measure 类型不允许出现在 extra_questions（数值必须走计算引擎）
    3. LLM 崩溃/输出不合法的 JSON → 回退 v1 规则，而不是报错或硬编
"""

from __future__ import annotations

import os
import tempfile
import unittest

from lifeplan.qcompiler import (
    ALLOWED_EXTRA_TYPES,
    SIGNAL_REGISTRY,
    _extract_json,
    compile_with_llm,
    validate_plan,
)


class TestJsonExtraction(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_code_fence(self):
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_preamble_noise(self):
        self.assertEqual(_extract_json('好的，输出如下：\n{"a": 1}\n以上。'), {"a": 1})

    def test_garbage_returns_none(self):
        for bad in ("", "没有 JSON", "{未闭合", "[1,2,3]"):
            self.assertIsNone(_extract_json(bad))


class TestPlanValidation(unittest.TestCase):
    def _ok(self):
        return {"supported": True, "domain": "d",
                "measures": ["commit_30d"],
                "extra_questions": [{"qid": "risk", "type": "score",
                                     "criteria": ["低", "中", "高"]}],
                "unknowns": []}

    def test_valid_plan_passes(self):
        self.assertEqual(validate_plan(self._ok()), [])

    def test_unknown_signal_rejected(self):
        doc = self._ok()
        doc["measures"] = ["commit_30d", "用户幸福感指数"]
        errors = validate_plan(doc)
        self.assertTrue(any("未知信号" in e for e in errors), errors)

    def test_measure_type_in_extras_rejected(self):
        """数值问题必须由计算引擎产生——LLM 不许提议 measure 题。"""
        doc = self._ok()
        doc["extra_questions"] = [{"qid": "v", "type": "measure", "criteria": None}]
        errors = validate_plan(doc)
        self.assertTrue(any("type" in e for e in errors), errors)

    def test_too_many_extras_rejected(self):
        doc = self._ok()
        doc["extra_questions"] = [
            {"qid": f"q{i}", "type": "noul"} for i in range(4)
        ]
        errors = validate_plan(doc)
        self.assertTrue(any("最多 3" in e for e in errors), errors)

    def test_choice_needs_dict_criteria(self):
        doc = self._ok()
        doc["extra_questions"] = [{"qid": "c", "type": "choice", "criteria": ["a", "b"]}]
        errors = validate_plan(doc)
        self.assertTrue(any("criteria" in e for e in errors), errors)

    def test_all_allowed_types_are_implemented(self):
        for t in ALLOWED_EXTRA_TYPES:
            self.assertIn(t, ("choice", "score", "noul"))


class TestCompileWithLlm(unittest.TestCase):
    def test_returns_plan_with_marker(self):
        def fake(prompt):
            self.assertIn("commit_30d", prompt, "提示词必须包含注册表")
            return '{"supported": true, "domain": "d", "measures": ["commit_30d"], "unknowns": []}'

        plan = compile_with_llm("问题", fake)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["_compiled_by"], "llm")

    def test_refusal_is_passed_through(self):
        def fake(prompt):
            return '{"supported": false, "domain": "", "measures": [], "unknowns": ["测不了"]}'

        plan = compile_with_llm("今天晚饭吃什么", fake)
        self.assertIsNotNone(plan)
        self.assertFalse(plan["supported"])

    def test_invalid_json_falls_back_to_none(self):
        self.assertIsNone(compile_with_llm("问题", lambda p: "我不会输出 JSON"))
        self.assertIsNone(compile_with_llm("问题", lambda p: None))

    def test_llm_crash_falls_back_to_none(self):
        def boom(prompt):
            raise RuntimeError("模型挂了")

        self.assertIsNone(compile_with_llm("问题", boom))

    def test_supported_but_no_measures_is_invalid(self):
        """说"支持"却一个信号都不选 → 视为无效（防止空计划）。"""
        def fake(prompt):
            return '{"supported": true, "domain": "d", "measures": [], "unknowns": []}'

        self.assertIsNone(compile_with_llm("问题", fake))


class TestQCompileAutoFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("LIFEPLAN_QCOMPILER", None)
        self.tmp.cleanup()

    def test_rules_mode_never_calls_llm(self):
        os.environ["LIFEPLAN_QCOMPILER"] = "rules"
        from lifeplan.qone import qcompile_auto

        vertical, questions, unknowns, compiled_by = qcompile_auto("这个项目是否值得继续投入")
        self.assertEqual(compiled_by, "rules")
        self.assertEqual(vertical, "project_investment")
        self.assertTrue(questions)

    def test_out_of_scope_in_rules_mode(self):
        os.environ["LIFEPLAN_QCOMPILER"] = "rules"
        from lifeplan.qone import qcompile_auto

        vertical, _, unknowns, compiled_by = qcompile_auto("今天晚饭吃什么")
        self.assertEqual(vertical, "unsupported")
        self.assertTrue(unknowns)
        self.assertEqual(compiled_by, "rules")


if __name__ == "__main__":
    unittest.main()

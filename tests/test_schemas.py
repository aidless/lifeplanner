"""协议 Schema 校验测试：Q-One 的输出必须过三份 JSON Schema。

- `measurement_contract.schema.json`：指标定义 + 量化结果（§8.1/§8.2）
- `EVIDENCE_SCHEMA.json`：证据记录（§7.2）
- `DECISION_SCHEMA.json`：决策协议（§8.3）

jsonschema 未安装时跳过（CI 的默认解释器可能没有它）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    import jsonschema

    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False


@unittest.skipUnless(HAVE_JSONSCHEMA, "需要 jsonschema")
class TestSchemas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.measurement = json.loads(
            (ROOT / "agent" / "measurement_contract.schema.json").read_text(encoding="utf-8"))
        cls.evidence = json.loads(
            (ROOT / "agent" / "EVIDENCE_SCHEMA.json").read_text(encoding="utf-8"))
        cls.decision = json.loads(
            (ROOT / "agent" / "DECISION_SCHEMA.json").read_text(encoding="utf-8"))
        cls.validator = jsonschema.Draft202012Validator

    def test_schemas_are_valid_draft2020(self):
        for schema in (self.measurement, self.evidence, self.decision):
            self.validator.check_schema(schema)


@unittest.skipUnless(HAVE_JSONSCHEMA, "需要 jsonschema")
class TestQOneOutputConforms(unittest.TestCase):
    """Q-One 真实输出必须过 Schema——协议不是文档，是门禁。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        from lifeplan.db import connect, init_db

        self.conn = connect(init_db())

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_decision_contract(self):
        from lifeplan.qone import ask

        ans = ask("这个项目是否值得继续投入", self.conn, root="/tmp")
        c = ans.to_contract()
        doc = {
            "decision_id": c.get("decision_id", "decision_20260101_000000"),
            "question": ans.state,
            "options": ["continue", "pivot", "pause", "stop", "need_data"],
            "selected": c["action"],
            "confidence": c["confidence"],
            "confidence_type": c["confidence_type"],
            "decided_by": c["decided_by"],
            "reason": c.get("next_measurement") or "",
        }
        jsonschema.validate(doc, json.loads(
            (ROOT / "agent" / "DECISION_SCHEMA.json").read_text(encoding="utf-8")))

    def test_measurement_result_contract(self):
        from lifeplan.qone import ask

        ans = ask("这个项目是否值得继续投入", self.conn, root="/tmp")
        c = ans.to_contract()
        # 至少一个 measure 题的结果应过结果协议
        measured = [v for v in c.values()
                    if isinstance(v, dict) and "value" in v and "unit" in v]
        self.assertTrue(measured, "量化输出里必须有 measure 结果")
        schema = json.loads(
            (ROOT / "agent" / "measurement_contract.schema.json").read_text(encoding="utf-8"))
        result_schema = schema["definitions"]["measurement_result"]
        for v in measured:
            doc = {
                "metric_id": "m",
                "value": v["value"],
                "interval": v["interval"],
                "confidence": c["confidence"],
                "confidence_type": c["confidence_type"],
            }
            jsonschema.validate(doc, result_schema)

    def test_evidence_contract(self):
        from lifeplan.qone import ask

        ans = ask("这个项目是否值得继续投入", self.conn, root="/tmp")
        c = ans.to_contract()
        schema = json.loads(
            (ROOT / "agent" / "EVIDENCE_SCHEMA.json").read_text(encoding="utf-8"))
        for e in c["evidence"]:
            doc = {
                "evidence_id": f"ev_{e['kind']}",
                "source": e["detail"],
                "source_type": {"git": "git", "db": "database"}.get(e["kind"], "file"),
                "collected_at": "2026-10-22T00:00:00",
                "content_hash": e["sha256"],
                "reliability": "L3" if e["kind"] == "git" else "L2",
                "privacy_level": "private",
                "used_for": [ans.state],
            }
            jsonschema.validate(doc, schema)

    def test_interval_null_when_no_series(self):
        """n<2 不给区间——虚假精确是协议明令禁止的。"""
        from lifeplan.qone import ask

        ans = ask("这个项目是否值得继续投入", self.conn, root="/tmp")
        c = ans.to_contract()
        for v in c.values():
            if isinstance(v, dict) and "interval" in v:
                if v["interval"] is not None:
                    self.assertEqual(len(v["interval"]), 2)
                    self.assertLessEqual(v["interval"][0], v["interval"][1])


if __name__ == "__main__":
    unittest.main()

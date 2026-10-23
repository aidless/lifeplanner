"""Q-One 溯源账本：每次量化决策的完整留痕。

记录什么：state 原文、问题集、模型/规则各自输出、证据清单（含内容哈希）、
假设、未知项、下一步测量、人工修正、结果哈希。

为什么用哈希链式：台账的价值不在"存了"，在"可证明没被改过"。
sha256 覆盖规范化 JSON（排序键、无空格），同样输入永远同哈希。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    state           TEXT NOT NULL,
    questions       TEXT NOT NULL,          -- 问题集 JSON
    answer          TEXT NOT NULL,          -- 答复 JSON（含 value/interval/confidence/action…）
    evidence        TEXT NOT NULL DEFAULT '[]',
    assumptions     TEXT NOT NULL DEFAULT '[]',
    unknowns        TEXT NOT NULL DEFAULT '[]',
    next_measurement TEXT,
    answer_sha256   TEXT NOT NULL,
    human_override  TEXT,                   -- 人工修正后的 action（若有）
    override_note   TEXT,
    created_at      TEXT NOT NULL
);
"""


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_of(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


@dataclass
class DecisionRecord:
    state: str
    questions: list
    answer: dict
    evidence: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    unknowns: list = field(default_factory=list)
    next_measurement: str | None = None
    human_override: str | None = None
    override_note: str | None = None
    ts: str = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.now().isoformat(timespec="seconds")

    @property
    def answer_sha256(self) -> str:
        return sha256_of(self.answer)

    def to_row(self) -> tuple:
        return (
            self.ts, self.state, canonical_json(self.questions), canonical_json(self.answer),
            canonical_json(self.evidence), canonical_json(self.assumptions),
            canonical_json(self.unknowns), self.next_measurement, self.answer_sha256,
            self.human_override, self.override_note, self.ts,
        )


def ensure_ledger(conn: sqlite3.Connection) -> None:
    conn.executescript(LEDGER_SCHEMA)
    conn.commit()


def record(conn: sqlite3.Connection, rec: DecisionRecord) -> int:
    ensure_ledger(conn)
    cur = conn.execute(
        """INSERT INTO decisions(ts, state, questions, answer, evidence, assumptions,
                                 unknowns, next_measurement, answer_sha256,
                                 human_override, override_note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rec.to_row(),
    )
    conn.commit()
    return int(cur.lastrowid)


def verify_integrity(conn: sqlite3.Connection) -> dict:
    """重算每行哈希，验证台账未被篡改。"""
    ensure_ledger(conn)
    bad = []
    total = 0
    for row in conn.execute(
        "SELECT id, answer, answer_sha256 FROM decisions"
    ).fetchall():
        total += 1
        if sha256_of(json.loads(row["answer"])) != row["answer_sha256"]:
            bad.append(row["id"])
    return {"checked": total, "tampered": bad, "ok": not bad}


def list_recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    ensure_ledger(conn)
    out = []
    for r in conn.execute(
        "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall():
        d = dict(r)
        for k in ("questions", "answer", "evidence", "assumptions", "unknowns"):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
        out.append(d)
    return out

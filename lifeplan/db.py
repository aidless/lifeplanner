"""数据库连接与通用访问helper。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_HOME = Path.home() / ".lifeplan"


def home_dir() -> Path:
    """数据根目录，可用 LIFEPLAN_HOME 覆盖。"""
    raw = os.environ.get("LIFEPLAN_HOME")
    path = Path(raw).expanduser() if raw else DEFAULT_HOME
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return home_dir() / "lifeplan.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # journal 模式可配。默认 delete（最耐久）；测试环境用 memory ——
    # 沙箱/CI 有时会拒绝 -journal 文件的创建与删除，让整个测试基建瘫痪；
    # 测试不需要跨进程崩溃恢复，没必要为此牺牲可重复性。
    mode = os.environ.get("LIFEPLAN_JOURNAL_MODE", "delete").lower()
    if mode and mode != "delete":
        try:
            conn.execute(f"PRAGMA journal_mode = {mode}")
        except sqlite3.Error:
            pass  # 个别场景改不了就算了，不阻塞
    return conn


def init_db(path: Path | None = None, seed: bool = False) -> Path:
    target = Path(path) if path else db_path()
    conn = connect(target)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate(conn)
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()
    if seed:
        from .seed import seed_all

        seed_all(conn)
    conn.close()
    return target


# 老库升级：只做"加列"这种非破坏性变更，不动已有数据
COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "actions": {"argv_json": "TEXT NOT NULL DEFAULT ''"},
    "unquantifiable": {"review_at": "TEXT"},
    "metrics": {"archived": "INTEGER NOT NULL DEFAULT 0"},
    "predictions": {"evidence_ref": "TEXT", "outcome_note": "TEXT"},
}

#: 当前 schema 版本。每次结构性变更（加表 / 加列）都要 +1。
#: 版本号本身不改变行为，它的价值在于：出问题时能一眼判断
#: "这个库是什么时候的、需不需要迁移"，而不是靠猜。
SCHEMA_VERSION = 3


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row else default


def migrate(conn: sqlite3.Connection) -> list[str]:
    """给已存在的表补列。返回实际执行的变更列表（便于审计输出）。"""
    applied: list[str] = []
    for table, columns in COLUMN_MIGRATIONS.items():
        exists = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        if not exists:
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, decl in columns.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                applied.append(f"{table}.{col}")
    if applied:
        conn.commit()
    return applied


def ensure_schema(conn: sqlite3.Connection) -> list[str]:
    """确保新版本引入的表存在。

    migrate() 只补列，不能补表；老库（在新增感知层之前建的）打开时会缺
    host_profile / host_signals / actions。这里按需重跑 schema（全部 IF NOT EXISTS）。
    """
    required = {"domains", "host_profile", "host_signals", "actions", "meta"}
    have = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    applied: list[str] = []
    if not required <= have:
        before = have
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        after = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        applied = sorted(after - before)
        conn.commit()
    applied.extend(migrate(conn))

    current = schema_version(conn)
    if current < SCHEMA_VERSION:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        applied.append(f"schema_version {current}→{SCHEMA_VERSION}")
    return applied


def is_initialized(path: Path | None = None) -> bool:
    target = Path(path) if path else db_path()
    if not target.exists():
        return False
    try:
        conn = connect(target)
        row = conn.execute(
            "SELECT count(*) AS n FROM sqlite_master WHERE type='table' AND name='domains'"
        ).fetchone()
        conn.close()
        return bool(row and row["n"] > 0)
    except sqlite3.Error:
        return False


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    cur = conn.execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    cur = conn.execute(sql, tuple(params))
    r = cur.fetchone()
    return dict(r) if r else None



def log_event(conn: sqlite3.Connection, kind: str, payload: dict | None = None) -> None:
    """写入审计事件流：系统自身的操作也要留痕。"""
    conn.execute(
        "INSERT INTO events(ts, kind, payload) VALUES (?,?,?)",
        (utc_now(), kind, json.dumps(payload or {}, ensure_ascii=False)),
    )


def recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return rows(conn, "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

"""终端执行层：所有命令都经过门禁，并且先记账再执行。

三条实现上的硬规矩：

1. **绝不用 shell**。命令以 argv 列表传入，`subprocess.run(..., shell=False)`。
   这样 `;`、`|`、`&&`、`$()` 都只是普通字符，不会被意外解释成一个大命令。
2. **先记账再执行**。动作在执行前就写入 actions 表（status=proposed），
   执行后更新结果。即使进程中途崩溃，也留下"曾试图做什么"的痕迹。
3. **环境变量脱敏**。传给子进程的环境变量会剔除 token / key / secret 一类，
   避免一条 `env` 或一个把环境写进日志的子进程把凭据泄漏出去。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass

from .db import connect, utc_now
from .policy import (
    TIER_FORBIDDEN,
    TIER_LABEL,
    classify,
    confirm_token,
    load_policy,
    quote,
)

SENSITIVE_ENV = re.compile(r"(TOKEN|SECRET|PASSWD|PASSWORD|API[_-]?KEY|CREDENTIAL|PRIVATE)", re.I)


@dataclass
class RunResult:
    action_id: int | None
    tier: int
    status: str            # done | failed | blocked | denied
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: int
    reason: str
    token: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "done" and self.returncode == 0


def _sanitized_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not SENSITIVE_ENV.search(k)}
    if extra:
        env.update({k: v for k, v in extra.items() if not SENSITIVE_ENV.search(k)})
    return env


def _record(
    conn, *, kind, tier, target, command, status, requires_confirm,
    rationale, output="", token="", argv: list[str] | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO actions(ts, kind, tier, target, command, status, requires_confirm,
                               rationale, output, confirm_token, argv_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (utc_now(), kind, tier, target, command, status, 1 if requires_confirm else 0,
         rationale, output, token,
         json.dumps(argv or [], ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _update(conn, action_id: int, *, status: str, output: str) -> None:
    conn.execute(
        "UPDATE actions SET status=?, output=? WHERE id=?",
        (status, output, action_id),
    )
    conn.commit()


def run(
    argv: list[str],
    cwd: str | None = None,
    timeout: int | None = None,
    token: str | None = None,
    rationale: str = "",
    env_extra: dict[str, str] | None = None,
    conn=None,
    reuse_action_id: int | None = None,
) -> RunResult:
    """执行一条命令；是否执行由门禁决定。

    reuse_action_id 用于"确认此前记录的提案"：令牌绑定的是那条提案的 id 与命令原文，
    因此确认动作与提案一一对应，重放别的命令令牌即失效。
    """
    pol = load_policy()
    own_conn = conn is None
    conn = conn or connect()
    try:
        verdict = classify(argv, cwd or os.getcwd(), pol)
        command = quote(argv)
        target = cwd or os.getcwd()

        # 策略层面不放行：永久禁止，或该级别未启用
        if not verdict.allowed:
            if verdict.tier == TIER_FORBIDDEN:
                reason = verdict.reason
            else:
                reason = (
                    f"{verdict.reason}｜当前策略未启用 tier{verdict.tier}，"
                    f"如需执行请改 policy.json（如 allow_destructive=true）后重试"
                )
            if reuse_action_id:
                _update(conn, reuse_action_id, status="denied", output=reason)
                aid = reuse_action_id
            else:
                aid = _record(
                    conn, kind="terminal", tier=verdict.tier, target=target,
                    command=command, status="denied", requires_confirm=False,
                    rationale=f"{reason}｜{rationale}".strip("｜"), argv=argv,
                )
            return RunResult(aid, verdict.tier, "denied", None, "", "", 0, reason)

        need_confirm = verdict.requires_confirm
        limit = timeout or pol.timeout_seconds
        expected = ""

        if reuse_action_id:
            aid = reuse_action_id
            row = conn.execute(
                "SELECT command, status FROM actions WHERE id=?", (aid,)
            ).fetchone()
            if row is None:
                return RunResult(None, verdict.tier, "failed", None, "", "", 0,
                                 f"动作 #{aid} 不存在")
            if row["status"] in ("done", "failed"):
                return RunResult(aid, verdict.tier, "denied", None, "", "", 0,
                                 f"提案 #{aid} 已执行过（{row['status']}）。"
                                 f"确认令牌一次性有效，如需再次执行请重新发起")
            if row["command"] != command:
                _update(conn, aid, status="denied", output="命令与提案不一致")
                return RunResult(aid, verdict.tier, "denied", None, "", "", 0,
                                 "命令与提案不一致，拒绝执行")
            expected = confirm_token(aid, command)
            if need_confirm and token != expected:
                if token:
                    _update(conn, aid, status="denied", output="确认令牌不匹配")
                    return RunResult(aid, verdict.tier, "denied", None, "", "", 0,
                                     "确认令牌不匹配（命令可能被改动，或令牌已过期）",
                                     token=expected)
                return RunResult(
                    aid, verdict.tier, "blocked", None, "", "", 0,
                    f"{TIER_LABEL[verdict.tier]}｜{verdict.reason}｜需确认："
                    f"lifeplan act run {aid} --confirm-token {expected}",
                    token=expected,
                )
            _update(conn, aid, status="proposed", output="")
        elif need_confirm:
            aid = _record(
                conn, kind="terminal", tier=verdict.tier, target=target,
                command=command, status="proposed", requires_confirm=True,
                rationale=f"{verdict.reason}｜{rationale}".strip("｜"), argv=argv,
            )
            expected = confirm_token(aid, command)
            if not token:
                return RunResult(
                    aid, verdict.tier, "blocked", None, "", "", 0,
                    f"{TIER_LABEL[verdict.tier]}｜{verdict.reason}｜需确认："
                    f"lifeplan act run {aid} --confirm-token {expected}",
                    token=expected,
                )
            if token != expected:
                _update(conn, aid, status="denied", output="确认令牌不匹配")
                return RunResult(aid, verdict.tier, "denied", None, "", "", 0,
                                 "确认令牌不匹配（命令可能被改动，或令牌已过期）",
                                 token=expected)
        else:
            aid = _record(
                conn, kind="terminal", tier=verdict.tier, target=target,
                command=command, status="proposed", requires_confirm=False,
                rationale=f"{verdict.reason}｜{rationale}".strip("｜"), argv=argv,
            )

        started = time.time()
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd or None,
                timeout=limit,
                capture_output=True,
                text=True,
                shell=False,               # ← 硬规矩 1
                env=_sanitized_env(env_extra),
            )
            elapsed = int((time.time() - started) * 1000)
            out = (proc.stdout or "")[: pol.max_output_chars]
            err = (proc.stderr or "")[: pol.max_output_chars]
            status = "done" if proc.returncode == 0 else "failed"
            _update(conn, aid, status=status,
                    output=(out + ("\n" + err if err else ""))[: pol.max_output_chars])
            return RunResult(aid, verdict.tier, status, proc.returncode, out, err, elapsed, verdict.reason)

        except subprocess.TimeoutExpired:
            elapsed = int((time.time() - started) * 1000)
            _update(conn, aid, status="failed", output=f"超时（{limit}s）")
            return RunResult(aid, verdict.tier, "failed", None, "", f"超时（{limit}s）", elapsed, verdict.reason)
        except FileNotFoundError:
            _update(conn, aid, status="failed", output="命令不存在")
            return RunResult(aid, verdict.tier, "failed", None, "", "命令不存在", 0, verdict.reason)
        except OSError as exc:
            _update(conn, aid, status="failed", output=f"OSError: {exc}")
            return RunResult(aid, verdict.tier, "failed", None, "", f"OSError: {exc}", 0, verdict.reason)
    finally:
        if own_conn:
            conn.close()


def list_actions(conn, limit: int = 30, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM actions"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    cur = conn.execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def stats(conn) -> dict[str, int]:
    cur = conn.execute("SELECT status, COUNT(*) AS n FROM actions GROUP BY status")
    return {r["status"]: r["n"] for r in cur.fetchall()}

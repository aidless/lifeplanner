"""Agent 的工具层。

设计约束（与项目其余部分一致）：

1. **每个工具都要声明权限等级**。tier0 只读、tier1 本地可逆写入、tier2 有外部影响。
2. **agent 不能执行 tier>=2 的动作**。它只能生成一份待确认提案，
   真正执行必须由人回填令牌。这条由工具实现强制保证，不依赖 prompt 约束。
3. **返回值同时给机器（data）与人（text）**，便于模型引用与人类复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..audit import run_audit
from ..calibration import compute_calibration, load_predictions
from ..metrics import build_snapshot
from ..policy import (
    TIER_FORBIDDEN,
    TIER_LABEL,
    classify,
    confirm_token,
    load_policy,
    quote,
)
from ..presence import load_latest
from ..terminal import _record


@dataclass
class Context:
    conn: Any
    as_of: str | None = None
    allow_write: bool = True
    cwd: str | None = None


@dataclass
class ToolResult:
    ok: bool
    data: dict
    text: str
    tier: int = 0
    has_proposal: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    summary: str
    tier: int
    params: dict[str, str]
    handler: Callable[[Context, dict], ToolResult]
    examples: list[str] = field(default_factory=list)

    def signature(self) -> str:
        if not self.params:
            return f"{self.name}()"
        inner = ", ".join(f"{k}: {v}" for k, v in self.params.items())
        return f"{self.name}({inner})"


# ── tier0：只读 ─────────────────────────────────────────────
def _h_plan_score(ctx: Context, args: dict) -> ToolResult:
    snap = build_snapshot(ctx.conn, ctx.as_of)
    data = {
        "value": round(snap.value, 3),
        "confidence": round(snap.confidence, 3),
        "coverage": round(snap.coverage, 3),
        "metric_count": snap.metric_count,
        "stale_count": snap.stale_count,
        "no_data_count": snap.no_data_count,
        "domains": [
            {"name": d.domain["name"], "value": round(d.value, 3),
             "confidence": round(d.confidence, 3)}
            for d in snap.domains
        ],
    }
    lines = [
        f"价值分 {data['value']:.3f}，证据置信度 {data['confidence']:.3f}，"
        f"数据覆盖率 {data['coverage']*100:.0f}%",
        f"指标 {data['metric_count']} 个，其中过期 {data['stale_count']}、无观测 {data['no_data_count']}",
    ]
    for d in data["domains"]:
        lines.append(f"  · {d['name']}：价值 {d['value']:.2f} / 置信 {d['confidence']:.2f}")
    return ToolResult(True, data, "\n".join(lines), tier=0)


def _h_plan_goals(ctx: Context, args: dict) -> ToolResult:
    snap = build_snapshot(ctx.conn, ctx.as_of)
    goals = []
    for d in snap.domains:
        for g in d.goals:
            goals.append({
                "goal_id": g.goal["id"],
                "title": g.goal["title"],
                "domain": d.domain["name"],
                "value": round(g.value, 3),
                "confidence": round(g.confidence, 3),
                "metrics": [
                    {"metric_id": m.metric.id, "name": m.metric.name,
                     "value": m.value, "status": m.status}
                    for m in g.metrics
                ],
            })
    lines = [f"共 {len(goals)} 个目标："]
    for g in goals:
        lines.append(f"  · #{g['goal_id']} {g['title']}（{g['domain']}，价值 {g['value']:.2f}）")
        for m in g["metrics"]:
            val = "—" if m["value"] is None else f"{m['value']:g}"
            lines.append(f"      - metric#{m['metric_id']} {m['name']}：{val}（{m['status']}）")
    return ToolResult(True, {"goals": goals}, "\n".join(lines), tier=0)


def _h_plan_calibration(ctx: Context, args: dict) -> ToolResult:
    cal = compute_calibration(load_predictions(ctx.conn))
    data = {
        "n_resolved": cal.n_resolved,
        "n_open": cal.n_open,
        "brier": None if cal.brier is None else round(cal.brier, 4),
        "ece": None if cal.ece is None else round(cal.ece, 4),
        "overconfidence": None if cal.overconfidence is None else round(cal.overconfidence, 4),
        "verdict": cal.verdict,
    }
    if cal.brier is None:
        text = "尚无已结算预测，无法校准。" + cal.verdict
    else:
        text = (
            f"{cal.verdict}\nBrier {cal.brier:.4f}，ECE {cal.ece:.4f}，"
            f"过度自信度 {cal.overconfidence:+.4f}，样本 n={cal.n_resolved}"
        )
    return ToolResult(True, data, text, tier=0)


def _h_plan_audit(ctx: Context, args: dict) -> ToolResult:
    rep = run_audit(ctx.conn)
    data = {
        "verdict": rep.verdict,
        "findings": [
            {"severity": f.severity, "kind": f.kind, "title": f.title, "detail": f.detail}
            for f in rep.findings
        ],
    }
    lines = [rep.verdict]
    for f in rep.findings[:5]:
        lines.append(f"  · [{f.severity}] {f.title}：{f.detail[:100]}")
    return ToolResult(True, data, "\n".join(lines), tier=0)


def _h_host_profile(ctx: Context, args: dict) -> ToolResult:
    latest = load_latest(ctx.conn)
    if not latest:
        return ToolResult(False, {}, "尚无宿主画像，需先运行 lifeplan host scan。", tier=0)
    parsed = latest.get("parsed", {})
    data = {
        "summary": latest["summary"],
        "confidence": latest["confidence"],
        "scanned_at": latest["scanned_at"],
        "hypotheses": parsed.get("hypotheses", []),
    }
    lines = [latest["summary"], f"置信度 {latest['confidence']:.2f}（启发式，不是概率）"]
    for h in data["hypotheses"][:3]:
        ev = (h.get("evidence") or ["—"])[0]
        lines.append(f"  · {h['label']}（得分 {h['score']}）← {ev}")
    return ToolResult(True, data, "\n".join(lines), tier=0)


def _h_predictions_due(ctx: Context, args: dict) -> ToolResult:
    from datetime import date

    as_of = ctx.as_of or date.today().isoformat()
    rows = [
        {"id": p.id, "statement": p.statement, "p_hit": p.p_hit,
         "horizon": p.horizon_date, "goal": p.goal_title}
        for p in load_predictions(ctx.conn)
        if p.outcome is None and p.horizon_date <= as_of
    ]
    lines = [f"{len(rows)} 条预测已到期未结算"] if rows else ["没有到期未结算的预测"]
    for r in rows[:8]:
        lines.append(f"  · #{r['id']} p={r['p_hit']:.2f} {r['statement'][:40]}（{r['horizon']}）")
    return ToolResult(True, {"due": rows}, "\n".join(lines), tier=0)


def _h_proposals(ctx: Context, args: dict) -> ToolResult:
    cur = ctx.conn.execute(
        "SELECT id, kind, tier, command, rationale FROM actions "
        "WHERE status='proposed' ORDER BY id DESC LIMIT 20"
    )
    items = [dict(r) for r in cur.fetchall()]
    lines = [f"{len(items)} 条待处理提议"] if items else ["没有待处理提议"]
    for it in items:
        lines.append(f"  · #{it['id']} [{it['kind']}] tier{it['tier']} {it['rationale'][:70]}")
    return ToolResult(True, {"proposals": items}, "\n".join(lines), tier=0)


# ── tier1：本地可逆写入 ──────────────────────────────────────
def _h_log_observation(ctx: Context, args: dict) -> ToolResult:
    from datetime import date

    from ..db import utc_now
    from ..evidence import infer_tier

    if not ctx.allow_write:
        return ToolResult(False, {}, "当前会话为只读模式，禁止写入。加 --write 开启。", tier=1)

    metric_id = int(args["metric_id"])
    value = float(args["value"])
    evidence = str(args.get("evidence", "") or "")
    note = str(args.get("note", "") or "")
    row = ctx.conn.execute("SELECT name FROM metrics WHERE id=?", (metric_id,)).fetchone()
    if not row:
        return ToolResult(False, {}, f"metric#{metric_id} 不存在", tier=1)

    tier = infer_tier(evidence, note)
    ctx.conn.execute(
        """INSERT INTO observations(metric_id, value, observed_at, note, evidence_ref, tier, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (metric_id, value, str(args.get("date") or date.today().isoformat()),
         note, evidence, tier, utc_now()),
    )
    ctx.conn.commit()
    return ToolResult(
        True,
        {"metric_id": metric_id, "value": value, "evidence_tier": tier},
        f"已记录：{row[0]} = {value:g}，证据等级 {tier}",
        tier=1,
    )


def _h_register_prediction(ctx: Context, args: dict) -> ToolResult:
    from ..db import utc_now

    if not ctx.allow_write:
        return ToolResult(False, {}, "当前会话为只读模式，禁止写入。加 --write 开启。", tier=1)
    goal_id = int(args["goal_id"])
    p = float(args["p"])
    if not 0.0 <= p <= 1.0:
        return ToolResult(False, {}, "p 必须在 0..1 之间", tier=1)
    ctx.conn.execute(
        """INSERT INTO predictions(goal_id, horizon_date, p_hit, statement, created_at)
           VALUES (?,?,?,?,?)""",
        (goal_id, str(args["horizon"]), p, str(args.get("statement", "")), utc_now()),
    )
    ctx.conn.commit()
    return ToolResult(True, {"goal_id": goal_id, "p": p},
                      f"已登记预测：goal#{goal_id}，p={p:.2f}，到期 {args['horizon']}", tier=1)


def _h_resolve_prediction(ctx: Context, args: dict) -> ToolResult:
    from ..db import utc_now

    if not ctx.allow_write:
        return ToolResult(False, {}, "当前会话为只读模式，禁止写入。加 --write 开启。", tier=1)
    pid = int(args["prediction_id"])
    hit = int(args["hit"])
    row = ctx.conn.execute("SELECT outcome FROM predictions WHERE id=?", (pid,)).fetchone()
    if not row:
        return ToolResult(False, {}, f"prediction#{pid} 不存在", tier=1)
    ctx.conn.execute(
        "UPDATE predictions SET outcome=?, resolved_at=? WHERE id=?", (hit, utc_now(), pid)
    )
    ctx.conn.commit()
    return ToolResult(True, {"prediction_id": pid, "outcome": hit},
                      f"已结算 prediction#{pid}：{'命中' if hit else '未中'}", tier=1)


# ── tier2：只能提议，不能执行 ────────────────────────────────
def _proposal(conn, *, kind: str, argv: list[str], cwd: str, rationale: str) -> tuple[int, int, str]:
    pol = load_policy(create=False)
    verdict = classify(argv, cwd, pol)
    command = quote(argv)
    aid = _record(
        conn, kind=kind, tier=verdict.tier, target=cwd, command=command,
        status="proposed", requires_confirm=True, rationale=rationale, argv=argv,
    )
    token = confirm_token(aid, command)
    return aid, verdict.tier, token


def _h_term_exec(ctx: Context, args: dict) -> ToolResult:
    argv = args.get("argv") or []
    if isinstance(argv, str):
        import shlex

        argv = shlex.split(argv)
    if not argv:
        return ToolResult(False, {}, "argv 不能为空", tier=2)

    cwd = ctx.cwd or "."
    pol = load_policy(create=False)
    verdict = classify(argv, cwd, pol)
    if verdict.tier == TIER_FORBIDDEN:
        return ToolResult(
            False, {"tier": verdict.tier},
            f"该命令属于禁止级，不可能执行：{verdict.reason}", tier=verdict.tier,
        )

    aid, tier, token = _proposal(
        ctx.conn, kind="terminal", argv=argv, cwd=cwd,
        rationale=f"agent 提议：{args.get('reason', '')}".strip("："),
    )
    return ToolResult(
        True,
        {"action_id": aid, "tier": tier, "token": token, "note": "仅生成提案，未执行"},
        f"已生成待确认提案 #{aid}（{TIER_LABEL.get(tier, tier)}）：{quote(argv)}\n"
        f"  执行需你确认：lifeplan act run {aid} --confirm-token {token}",
        tier=tier,
        has_proposal=True,
    )


def _h_app_launch(ctx: Context, args: dict) -> ToolResult:
    from .. import desktop

    name = str(args.get("name", "")).strip()
    app = desktop.find_app(name)
    if app is None:
        return ToolResult(False, {}, f"未找到应用「{name}」，可用 lifeplan apps list 查看", tier=2)
    argv = ["gio", "launch", app.desktop_file]
    aid, tier, token = _proposal(
        ctx.conn, kind="app", argv=argv, cwd=ctx.cwd or ".",
        rationale=f"agent 提议启动应用 {app.name}",
    )
    return ToolResult(
        True,
        {"action_id": aid, "tier": tier, "token": token, "app": app.name},
        f"已生成待确认提案 #{aid}：启动 {app.name}\n"
        f"  执行需你确认：lifeplan act run {aid} --confirm-token {token}",
        tier=tier,
        has_proposal=True,
    )


# ── 注册表 ──────────────────────────────────────────────────
TOOLS: dict[str, ToolSpec] = {
    "plan_score": ToolSpec(
        "plan_score", "查看整体状态：价值分、证据置信度、覆盖率、分领域得分", 0, {},
        _h_plan_score, ["现在状态怎么样", "我进展如何"],
    ),
    "plan_goals": ToolSpec(
        "plan_goals", "列出全部目标与指标（含 metric_id / goal_id，供其他工具引用）", 0, {},
        _h_plan_goals, ["我有哪些目标"],
    ),
    "plan_calibration": ToolSpec(
        "plan_calibration", "自我预测校准诊断：Brier、ECE、过度自信度", 0, {},
        _h_plan_calibration, ["我对自己判断准不准"],
    ),
    "plan_audit": ToolSpec(
        "plan_audit", "自我审计：基线追认、代理反噬、证据通胀", 0, {},
        _h_plan_audit, ["有没有自欺"],
    ),
    "host_profile": ToolSpec(
        "host_profile", "宿主画像：这台机器的主人在做什么（含证据）", 0, {},
        _h_host_profile, ["这台机器是干什么的"],
    ),
    "predictions_due": ToolSpec(
        "predictions_due", "到期未结算的预测", 0, {},
        _h_predictions_due, ["有什么预测该结算了"],
    ),
    "proposals_list": ToolSpec(
        "proposals_list", "待处理的提议（含需要确认的动作）", 0, {},
        _h_proposals, ["有什么待办"],
    ),
    "log_observation": ToolSpec(
        "log_observation", "记录一次指标观测（写入本地库，可逆）", 1,
        {"metric_id": "int, 必填", "value": "float, 必填",
         "evidence": "str, 可选：URL / 文件路径", "note": "str, 可选",
         "date": "str, 可选 YYYY-MM-DD"},
        _h_log_observation, ['记录 metric#1 = 12'],
    ),
    "register_prediction": ToolSpec(
        "register_prediction", "登记一条事前概率预测（写入本地库）", 1,
        {"goal_id": "int, 必填", "p": "float 0..1, 必填",
         "horizon": "str YYYY-MM-DD, 必填", "statement": "str, 可选"},
        _h_register_prediction, ["预测 goal#1 两周内拿到面试，p=0.6"],
    ),
    "resolve_prediction": ToolSpec(
        "resolve_prediction", "结算一条预测（写入本地库）", 1,
        {"prediction_id": "int, 必填", "hit": "int 0|1, 必填"},
        _h_resolve_prediction, ["结算 prediction#3 未中"],
    ),
    "term_exec": ToolSpec(
        "term_exec", "提议执行一条终端命令。注意：只生成待确认提案，不会执行", 2,
        {"argv": "list[str], 必填", "reason": "str, 可选"},
        _h_term_exec, ["跑一下 git status"],
    ),
    "app_launch": ToolSpec(
        "app_launch", "提议启动一个桌面应用。注意：只生成待确认提案，不会执行", 2,
        {"name": "str, 必填"},
        _h_app_launch, ["打开 Chrome"],
    ),
}


def tool_catalog() -> list[dict]:
    return [
        {"name": t.name, "summary": t.summary, "tier": t.tier,
         "tier_label": TIER_LABEL.get(t.tier, str(t.tier)), "params": t.params}
        for t in TOOLS.values()
    ]


def tool_catalog_lines() -> list[str]:
    """给模型看的工具清单（紧凑一行一个）。"""
    return [
        f"- {t.signature()}｜tier{t.tier}｜{t.summary}"
        for t in TOOLS.values()
    ]


def call(name: str, ctx: Context, args: dict) -> ToolResult:
    spec = TOOLS.get(name)
    if spec is None:
        return ToolResult(False, {}, f"未知工具「{name}」。可用：{', '.join(TOOLS)}")
    try:
        return spec.handler(ctx, args or {})
    except KeyError as exc:
        return ToolResult(False, {}, f"缺少参数 {exc}", tier=spec.tier)
    except (ValueError, TypeError) as exc:
        return ToolResult(False, {}, f"参数错误：{exc}", tier=spec.tier)

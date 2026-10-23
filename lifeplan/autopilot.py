"""自主循环：感知 → 推断 → 提议 →（经门禁）执行 → 记账。

分工是刻意的：

    感知与推断   全自动、无限制（只读，不产生任何副作用）
    有副作用的动作 只"提议"，等确认，绝不自主执行

理由：一个会自己动手的系统，其错误率等于"模型判断错误率 × 无人复核次数"。
把提案与执行分开，等于给每一次副作用留一个人工确认点，而不是靠模型自觉。

目前支持的提议来源：
    1. 指标过期 / 无观测        → 建议补记观测
    2. 预测已到期未结算          → 建议结算（这会直接改善校准模块）
    3. 新出现的项目未被目标覆盖  → 建议新建目标（来自宿主感知的 drift）
    4. 不可量化项到达复核期      → 建议复核
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .db import connect, rows, utc_now
from .metrics import build_snapshot
from .presence import HostProfile, build_profile, load_latest, save_profile
from .terminal import list_actions, _record


@dataclass
class Proposal:
    kind: str
    target: str
    command: str
    rationale: str
    tier: int = 0



def perceive(conn, include_history: bool = False, roots=None) -> tuple[HostProfile, int]:
    """扫描宿主并落库。纯读操作。"""
    profile = build_profile(roots=roots, include_history=include_history)
    pid = save_profile(conn, profile)
    return profile, pid


def _parse(d: str) -> date | None:
    try:
        return datetime.fromisoformat(d).date()
    except (ValueError, TypeError):
        return None


def propose_from_plan(conn, as_of: str | None = None) -> list[Proposal]:
    """从计划状态生成提议。"""
    as_of = as_of or date.today().isoformat()
    snap = build_snapshot(conn, as_of)
    out: list[Proposal] = []
    today = date.fromisoformat(as_of)

    # 0) 目标临近截止却无指标/无观测 —— 真实库首日就暴露的盲区：
    #    （实测：某目标 60 天内截止、无指标无预测时，tick 曾说「无需处理」）
    #    截止前 60 天内没有任何可结算事实的目标，必须提醒。
    for d in snap.domains:
        for g in d.goals:
            end = g.goal.get("end_date")
            if not end:
                continue
            try:
                days_left = (date.fromisoformat(end) - today).days
            except ValueError:
                continue
            if days_left < 0 or days_left > 60:
                continue
            has_metric = any(m.metric.id for m in g.metrics)
            has_data = any(m.has_data for m in g.metrics)
            if has_data:
                continue
            why = ("该目标下没有任何指标" if not has_metric
                   else "指标存在但从未观测")
            out.append(
                Proposal(
                    "goal", g.goal["title"],
                    f"lifeplan metric add --goal {g.goal['id']} --name <指标名> --unit <单位> "
                    f"--baseline <基线> --target <目标值> --cadence <周期>",
                    f"目标「{g.goal['title'][:30]}」{days_left} 天后截止（{end}），"
                    f"但{why}——到期时它将无法被结算",
                    tier=0,
                )
            )

    # 1) 无观测 / 过期指标
    for d in snap.domains:
        for g in d.goals:
            for m in g.metrics:
                if not m.has_data:
                    out.append(
                        Proposal(
                            "observe", m.metric.name,
                            f"lifeplan observe --metric {m.metric.id} --value <数值>",
                            f"指标「{m.metric.name}」从未记录，{g.goal['title']} 无法评分",
                            tier=0,
                        )
                    )
                elif m.status == "stale":
                    out.append(
                        Proposal(
                            "observe", m.metric.name,
                            f"lifeplan observe --metric {m.metric.id} --value <数值>",
                            f"指标「{m.metric.name}」已 {m.staleness_days} 天未更新（周期 {m.metric.cadence}）",
                            tier=0,
                        )
                    )

    for r in rows(
        conn,
        "SELECT id, statement, horizon_date, p_hit FROM predictions "
        "WHERE outcome IS NULL AND horizon_date <= ? ORDER BY horizon_date",
        (as_of,),
    ):
        out.append(
            Proposal(
                "resolve", f"prediction#{r['id']}",
                f"lifeplan resolve {r['id']} --hit 1|0",
                f"预测 #{r['id']}「{r['statement'][:40]}」已于 {r['horizon_date']} 到期，"
                f"事前 p={r['p_hit']:.2f}，未结算则校准样本缺失",
                tier=0,
            )
        )

    for u in rows(conn, "SELECT * FROM unquantifiable WHERE status='open' AND review_at IS NOT NULL"):
        due = _parse(u["review_at"])
        if due and due <= date.today():
            out.append(
                Proposal(
                    "review", u["name"],
                    "lifeplan unquantifiable list",
                    f"不可量化项「{u['name']}」已到复核期 {u['review_at']}",
                    tier=0,
                )
            )
    return out


def propose_from_drift(conn, profile: HostProfile) -> list[Proposal]:
    """宿主画像的变化 → 建议补目标。

    只在"新项目出现且计划里完全没有对应目标"时提议，避免每次扫描都刷一堆噪音。
    """
    latest = load_latest(conn)
    prev = {}
    if latest:
        prev = latest.get("parsed", {})
    prev_names = {h["label"] for h in prev.get("hypotheses", [])}

    out: list[Proposal] = []
    for h in profile.hypotheses[:3]:
        if h.label in prev_names:
            continue
        if h.score < 3.0:
            continue
        out.append(
            Proposal(
                "goal", h.label,
                f'lifeplan goal add --domain 1 --title "{h.label}相关目标"',
                f"宿主画像新识别出活动方向「{h.label}」（得分 {h.score}），"
                f"但计划中尚无对应目标。证据：{'；'.join(h.evidence[:2]) or '见扫描详情'}",
                tier=1,
            )
        )
    return out


def record_proposals(conn, proposals: list[Proposal]) -> list[int]:
    ids = []
    for p in proposals:
        aid = _record(
            conn, kind=p.kind, tier=p.tier, target=p.target, command=p.command,
            status="proposed", requires_confirm=p.tier >= 2, rationale=p.rationale,
        )
        ids.append(aid)
    return ids


def tick(
    conn=None,
    include_history: bool = False,
    roots=None,
    as_of: str | None = None,
) -> dict:
    """跑一轮自主循环：感知 → 推断 → 提议 → 落库。不执行任何有副作用的动作。"""
    own = conn is None
    conn = conn or connect()
    try:
        profile, pid = perceive(conn, include_history=include_history, roots=roots)
        plan_props = propose_from_plan(conn, as_of)
        drift_props = propose_from_drift(conn, profile)
        all_props = plan_props + drift_props
        ids = record_proposals(conn, all_props)
        return {
            "profile_id": pid,
            "summary": profile.summary,
            "confidence": profile.confidence,
            "hypotheses": [(h.label, h.score) for h in profile.hypotheses[:3]],
            "proposals": [
                {"id": aid, "kind": p.kind, "rationale": p.rationale, "tier": p.tier}
                for aid, p in zip(ids, all_props)
            ],
            "n_proposals": len(all_props),
        }
    finally:
        if own:
            conn.close()


"""自我审计：检测量化系统中最隐蔽的三种自欺。

1. 事后移动门柱（baseline drift）
   目标没达成，于是把基线改到"我其实已经做到了的水平"。
   检测方式：比较 baseline_at（基线冻结时间）与最早观测时间。
   若最早观测早于基线冻结，说明基线是在有了数据之后才定的 —— 数字被追认。

2. 代理反噬（proxy capture）
   用代理指标替代真实目标后，优化代理逐渐侵蚀真实目标。
   典型：把"面试复盘完成率"当"面试能力"，结果只刷复盘不真面试。
   检测方式：同一目标下，代理指标达成而主指标不达成。

3. 证据通胀（evidence inflation）
   证据等级不断上调，但引用内容没变 —— 换汤不换药的自证。
   检测方式：同一证据引用在时间线上等级发生跃升。

这三类问题都无法靠"多打点卡"解决，只能靠对数据本身做一致性检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .db import rows
from .metrics import goal_progress


@dataclass
class Finding:
    severity: str          # high | medium | low
    kind: str              # baseline_drift | proxy_capture | evidence_inflation | stale_evidence
    title: str
    detail: str
    entity: str = ""
    suggestion: str = ""


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "high")

    @property
    def verdict(self) -> str:
        if not self.findings:
            return "未发现结构性自欺迹象"
        if self.high_count:
            return f"发现 {self.high_count} 项高危自欺迹象，需立即处理"
        return f"发现 {len(self.findings)} 项需留意的问题"

    def by_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


def _parse(s: str):
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def audit_baseline_drift(conn) -> list[Finding]:
    """基线冻结时间晚于最早观测时间 → 基线是追认的。"""
    out: list[Finding] = []
    data = rows(conn, """
        SELECT m.id, m.name, m.baseline, m.baseline_at, m.target, m.direction,
               g.title AS goal_title,
               MIN(o.observed_at) AS first_obs, COUNT(o.id) AS n_obs
        FROM metrics m
        JOIN goals g ON g.id = m.goal_id
        LEFT JOIN observations o ON o.metric_id = m.id
        GROUP BY m.id
    """)
    for d in data:
        if not d["first_obs"]:
            continue
        base_at = _parse(d["baseline_at"])
        first_obs = _parse(d["first_obs"])
        if not base_at or not first_obs:
            continue
        if first_obs < base_at:
            gap_days = (base_at - first_obs).days
            out.append(Finding(
                severity="high" if gap_days > 7 else "medium",
                kind="baseline_drift",
                title=f"基线追认：{d['name']}",
                detail=(
                    f"最早观测于 {d['first_obs']}，但基线冻结于 {d['baseline_at']}"
                    f"（晚 {gap_days} 天）。基线是在已有观测之后才确定的，"
                    f"基线值 {d['baseline']:g} → 目标 {d['target']:g} 的差距可能被事后缩小。"
                ),
                entity=f"metric#{d['id']}",
                suggestion="重述该指标时明确标注基线冻结时间，并说明变更理由；不要静默修改。",
            ))
    return out


def audit_proxy_capture(conn) -> list[Finding]:
    """代理指标达成但主指标未达成 → 代理反噬。"""
    out: list[Finding] = []
    goals = rows(conn, "SELECT id, title FROM goals WHERE status='active'")
    for g in goals:
        metrics = rows(conn, """
            SELECT m.*,
                   (SELECT o.value FROM observations o WHERE o.metric_id=m.id
                    ORDER BY o.observed_at DESC LIMIT 1) AS last_value,
                   (SELECT COUNT(*) FROM observations o WHERE o.metric_id=m.id) AS n_obs
            FROM metrics m WHERE m.goal_id=?
        """, (g["id"],))
        proxies = [m for m in metrics if m["is_proxy"] and m["n_obs"]]
        primaries = [m for m in metrics if not m["is_proxy"] and m["n_obs"]]
        if not proxies or not primaries:
            continue

        def progress(m) -> float:
            # 统一走 metrics.goal_progress：这里曾经有一份内联副本，
            # 而且漏了 maintain 分支（把"维持型"当成"提升型"算，得负分）
            if m["last_value"] is None:
                return 0.0
            raw = goal_progress(
                m["last_value"], m["baseline"], m["target"],
                m["direction"], m["tolerance"] or 0.0,
            )
            return max(0.0, min(1.0, raw))

        proxy_prog = max(progress(m) for m in proxies)
        primary_prog = max(progress(m) for m in primaries)
        if proxy_prog >= 0.75 and primary_prog < 0.4:
            proxy_names = "、".join(m["name"] for m in proxies)
            primary_names = "、".join(m["name"] for m in primaries)
            out.append(Finding(
                severity="high",
                kind="proxy_capture",
                title=f"代理反噬：{g['title']}",
                detail=(
                    f"代理指标「{proxy_names}」进展达 {proxy_prog*100:.0f}%，"
                    f"而主指标「{primary_names}」仅 {primary_prog*100:.0f}%。"
                    f"代理正在替代真实目标发挥作用。"
                ),
                entity=f"goal#{g['id']}",
                suggestion=(
                    f"把代理指标降级为参考项（--proxy 保留但权重调低），"
                    f"并在主指标上加一条观测频率约束，强制回到结果本身。"
                ),
            ))
    return out


def audit_evidence_inflation(conn) -> list[Finding]:
    """同一证据引用出现等级跃升 → 证据通胀。"""
    out: list[Finding] = []
    data = rows(conn, """
        SELECT o.metric_id, o.evidence_ref, m.name AS metric_name, m.evidence_tier AS required_tier,
               COUNT(DISTINCT o.tier) AS n_tiers, MIN(o.tier) AS min_tier, MAX(o.tier) AS max_tier,
               GROUP_CONCAT(DISTINCT o.tier) AS tiers, COUNT(*) AS n
        FROM observations o
        JOIN metrics m ON m.id = o.metric_id
        WHERE o.evidence_ref != ''
        GROUP BY o.metric_id, o.evidence_ref
        HAVING n_tiers > 1
    """)
    for d in data:
        if d["max_tier"] > d["min_tier"]:
            out.append(Finding(
                severity="medium",
                kind="evidence_inflation",
                title=f"证据通胀：{d['metric_name']}",
                detail=(
                    f"同一份证据引用「{d['evidence_ref']}」在 {d['n']} 次观测中被赋予了不同等级"
                    f"（{d['tiers']}）。证据内容没变，可信度却在变——这说明评分依据的是当下感受而非证据本身。"
                ),
                entity=f"metric#{d['metric_id']}",
                suggestion="为同一 evidence_ref 固定一个等级；证据升级应指向新的证据，而非重新解读旧的。",
            ))

    # 引用了不存在的文件：典型编造痕迹
    import os

    refs = rows(conn, """
        SELECT DISTINCT o.evidence_ref, o.metric_id, m.name AS metric_name
        FROM observations o JOIN metrics m ON m.id=o.metric_id
        WHERE o.evidence_ref != '' AND o.evidence_ref NOT LIKE 'http%'
    """)
    for r in refs:
        ref = r["evidence_ref"]
        if "/" in ref and not os.path.exists(ref):
            out.append(Finding(
                severity="medium",
                kind="stale_evidence",
                title=f"证据不可达：{r['metric_name']}",
                detail=f"引用的本地证据「{ref}」在当前路径下不存在。",
                entity=f"metric#{r['metric_id']}",
                suggestion="使用绝对路径或可追溯的仓库内相对路径；证据不可达等同于无证据。",
            ))
    return out


def audit_value_confidence_gap(conn) -> list[Finding]:
    """高分低置信盯梢：「留」的代价可视化。

    用户决策（2026-10-22）：得分最高的领域即使证据置信低也保留在看板上，
    但条件是「清楚标出证据置信度」。这条审计把「最高分 × 低置信」的组合
    每次都翻出来给人看——留，但不许被遗忘它弱在哪。
    """
    from .metrics import build_snapshot

    snap = build_snapshot(conn)
    if not snap.domains:
        return []
    top = max(snap.domains, key=lambda d: d.value)
    # 门：真实库首日发现空库上 max() 挑中 value=0 的领域，产出
    # 「价值 0.00 是全部领域最高」的可笑误报。只有分够高才值得盯。
    if top.confidence >= 0.6 or top.value < 0.5 or snap.metric_count == 0:
        return []
    return [Finding(
        severity="medium",
        kind="value_confidence_gap",
        title=f"高分低置信：{top.domain['name']}",
        detail=(f"价值 {top.value:.2f} 是全部领域最高，但证据置信度只有 "
                f"{top.confidence:.2f}。按主人的决定它留在看板上（置信度已标注），"
                f"但这个分不应作为对外陈述的依据。"),
        entity=top.domain["name"],
        suggestion="给该领域补可核验证据（tier≥2 的观测），或接受它只是自评。",
    )]


def run_audit(conn) -> AuditReport:
    drift = audit_baseline_drift(conn)
    proxy = audit_proxy_capture(conn)
    inflation = audit_evidence_inflation(conn)
    gap = audit_value_confidence_gap(conn)

    order = {"high": 0, "medium": 1, "low": 2}
    findings = sorted(
        drift + proxy + inflation + gap,
        key=lambda f: (order.get(f.severity, 3), f.kind),
    )
    return AuditReport(
        findings=findings,
        checked={
            "baseline_drift": len(drift),
            "proxy_capture": len(proxy),
            "evidence_inflation": len(inflation),
            "value_confidence_gap": len(gap),
        },
    )

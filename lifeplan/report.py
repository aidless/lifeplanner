"""周报生成：Markdown 文本报告。

**结构原则（2026-10-22 调整）**：
校准放在首页，价值分降为参考。

理由是实测出来的：填完一周数据后价值分从 0.292 跳到 0.725，
唯一原因是实际提交量把 `target=12` 那条指标打满——而 target 是人拍的。
也就是说价值分的绝对水平与变化幅度都由"当初谁写了 target"决定，
它不可解读。

而校准（p=0.7 的预测是否真的 70% 兑现）的判据在系统外部，改不了。
所以它才是这个系统唯一能自证的指标。

报告结构：
    一、校准摘要（主指标）
    二、计划状态（价值分为参考，明确标注局限）
    三、分领域
    四、目标明细
    五、承诺与产能
    六、不可量化登记
    七、自我审计
    附、宿主画像
"""

from __future__ import annotations

from datetime import date

from .audit import run_audit
from .calibration import compute_calibration, load_predictions
from .db import rows
from .evidence import tier_label
from .metrics import Snapshot, build_snapshot

#: 校准结论有统计意义所需的最低已结算样本量
MIN_SAMPLES_FOR_SIGNAL = 20


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _bar(value: float, width: int = 20) -> str:
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "█" * filled + "░" * (width - filled)


def _status_label(status: str) -> str:
    return {
        "done": "已达成",
        "active": "推进中",
        "stale": "数据过期",
        "no_data": "无观测",
    }.get(status, status)


# ── 一、校准摘要 ────────────────────────────────────────────
def _calibration_section(add, cal) -> None:
    add("## 一、校准摘要（主指标）")
    add("")
    add("这个系统里唯一**外部可检验**的量：你说「能做到」时的把握，和实际做到的频率是否一致。")
    add("它不依赖任何自我评价，也不受目标值怎么设的影响——所以它是主指标。")
    add("")

    if cal.n_resolved == 0:
        add("尚无已结算预测，无法给出校准诊断。")
        add("")
        add("最小启动方式：每周押 3 条「下周会不会发生」的判断并写下概率，到期结算。")
        add(f"累计 {MIN_SAMPLES_FOR_SIGNAL} 条后才谈得上统计意义。")
        add("")
        return

    add(f"**{cal.verdict}**")
    add("")
    add("| 指标 | 数值 | 说明 |")
    add("|---|---|---|")
    add(f"| 已结算 / 待结算 | {cal.n_resolved} / {cal.n_open} | 样本量 |")
    if cal.brier is not None:
        add(f"| Brier 分数 | {cal.brier:.4f} | 越低越好：0 完美，0.25 相当于永远猜 0.5 |")
    if cal.skill_score is not None:
        tag = "优于基线" if cal.skill_score > 0 else "劣于基线"
        add(f"| Skill Score | {cal.skill_score:+.4f} | 相对基线率的技巧分，{tag} |")
    if cal.ece is not None:
        add(f"| ECE 期望校准误差 | {cal.ece:.4f} | 分箱后预测与实际的平均偏离 |")
    if cal.overconfidence is not None:
        direction = "过度自信" if cal.overconfidence > 0 else "过度保守"
        add(f"| 过度自信度 | {cal.overconfidence:+.4f} | 平均预测概率 − 实际命中率；正值={direction} |")
    if cal.mean_p is not None:
        add(f"| 平均预测概率 | {cal.mean_p:.3f} | 「我能做到」时的平均把握 |")
    if cal.accuracy is not None:
        add(f"| 实际命中率 | {cal.accuracy:.3f} | 说到做到的频率 |")
    add("")

    # 可靠曲线（分箱）
    filled = [b for b in cal.bins if b.count > 0 and b.mean_p is not None]
    if filled:
        add("**可靠性分箱**（预测区间 → 实际命中率；偏差为正说明偏保守，为负说明偏乐观）：")
        add("")
        add("| 预测区间 | n | 平均预测 | 实际命中 | 偏差 |")
        add("|---|---|---|---|---|")
        for b in filled:
            gap = b.accuracy - b.mean_p
            add(f"| {b.lower:.1f}–{b.upper:.1f} | {b.count} | {b.mean_p:.2f} | "
                f"{b.accuracy:.2f} | {gap:+.2f} |")
        add("")

    if cal.by_domain:
        add("**分领域过度自信度**（正值越高越容易高估自己）：")
        add("")
        add("| 领域 | n | 平均预测 | 实际命中 | 过度自信 |")
        add("|---|---|---|---|---|")
        for name, st in sorted(cal.by_domain.items(), key=lambda kv: -kv[1]["overconfidence"]):
            add(f"| {name} | {st['n']} | {st['mean_p']:.2f} | {st['accuracy']:.2f} | "
                f"{st['overconfidence']:+.2f} |")
        add("")

    for note in cal.notes:
        add(f"- {note}")

    if cal.n_resolved < MIN_SAMPLES_FOR_SIGNAL:
        need = MIN_SAMPLES_FOR_SIGNAL - cal.n_resolved
        add(f"- 距离统计意义还差 **{need}** 条已结算预测——在此之前，以上数字只能当方向参考")
    add("")


# ── 二、计划状态 ────────────────────────────────────────────
def _plan_section(add, snap: Snapshot) -> None:
    add("## 二、计划状态（参考指标）")
    add("")
    add("> **这是自造标量，不可当作「过得好不好」的读数。**")
    add("> 它的 baseline / target 由人填写，因此分值水平与变化幅度都取决于当初怎么写目标——")
    add("> 同一周同样的生活，改一下 target 就能让分数从 0.3 跳到 0.7（实测发生过）。")
    add("> 只用于**前后纵向比较自己**，不做横向比较，也不作为主指标。")
    add("")
    add("| 维度 | 数值 | 含义 |")
    add("|---|---|---|")
    add(f"| 价值分 value | {snap.value:.3f} | 指标按方向归一化后的加权均值 |")
    add(f"| 证据置信度 confidence | {snap.confidence:.3f} | 这些进度有多少能被独立核验 |")
    add(f"| 折减后投影 composite | {snap.composite:.3f} | 附带参考值，勿单独作为目标 |")
    add(f"| 数据覆盖率 | {_pct(snap.coverage)} | 有实际观测的指标权重占比 |")
    add(f"| 过期指标 | {snap.stale_count} 个 | 超过 2 个采集周期未更新 |")
    add(f"| 无观测指标 | {snap.no_data_count} 个 | 既无进展也无数据 |")
    add("")
    add(f"> 置信度 {_pct(snap.confidence)} 说明当前判断有约 {_pct(snap.confidence)} 建立在可核验证据上，"
        f"其余依赖自述。价值分与置信度差距大，说明**进展真实但未被证明**。")
    add("")


def render_report(conn, as_of: str | None = None) -> str:
    as_of = as_of or date.today().isoformat()
    snap: Snapshot = build_snapshot(conn, as_of)
    preds = load_predictions(conn)
    cal = compute_calibration(preds)
    budget_rows = rows(conn, """
        SELECT b.*, d.name AS domain_name, d.weight AS domain_weight
        FROM budget b JOIN domains d ON d.id = b.domain_id
        WHERE b.week_start = (SELECT MAX(week_start) FROM budget)
        ORDER BY d.sort_order
    """)

    lines: list[str] = []
    add = lines.append

    add("# 人生规划系统 · 周报")
    add("")
    add(f"**统计截止**：{as_of}　|　**目标 {sum(len(d.goals) for d in snap.domains)} 个**　"
        f"**指标 {snap.metric_count} 个**　|　**数据覆盖率 {_pct(snap.coverage)}**　|　"
        f"**已结算预测 {cal.n_resolved} 条**")
    add("")

    # ── 一、校准摘要（主指标） ──────────────────────────────
    _calibration_section(add, cal)

    # ── 二、计划状态（参考） ────────────────────────────────
    _plan_section(add, snap)

    # ── 三、分领域 ──────────────────────────────────────────
    add("## 三、分领域")
    add("")
    add("| 领域 | 价值分 | 证据置信度 | 覆盖率 | 进展条 | 短板 |")
    add("|---|---|---|---|---|---|")
    for d in snap.domains:
        goals_cov = (
            sum(g.coverage * g.goal.get("weight", 1.0) for g in d.goals)
            / (sum(g.goal.get("weight", 1.0) for g in d.goals) or 1.0)
        )
        weakest = ""
        worst = None
        for g in d.goals:
            for m in g.metrics:
                if m.has_data and m.progress is not None:
                    if worst is None or m.progress < worst[1]:
                        worst = (m.metric.name, m.progress)
                elif not m.has_data:
                    worst = (m.metric.name, -1.0)
                    break
            if worst and worst[1] < 0:
                break
        if worst:
            weakest = f"{worst[0]}（无数据）" if worst[1] < 0 else f"{worst[0]}（{_pct(worst[1])}）"
        add(f"| {d.domain['name']} | {d.value:.3f} | {d.confidence:.3f} | {_pct(goals_cov)} | "
            f"`{_bar(d.value)}` | {weakest} |")
    add("")

    # ── 四、目标明细 ────────────────────────────────────────
    add("## 四、目标明细")
    add("")
    for d in snap.domains:
        if not d.goals:
            continue
        add(f"### {d.domain['name']}")
        add("")
        for g in d.goals:
            add(f"**{g.goal['title']}**　—　价值 {g.value:.2f} / 置信 {g.confidence:.2f} / 覆盖 {_pct(g.coverage)}")
            add("")
            add("| 指标 | 当前值 | 基线→目标 | 进展 | 证据 | 状态 | 采集周期 |")
            add("|---|---|---|---|---|---|---|")
            for m in g.metrics:
                cur = f"{m.value:g} {m.metric.unit}" if m.value is not None else "—"
                bt = f"{m.metric.baseline:g}→{m.metric.target:g}"
                prog = f"{_pct(m.progress)}" if m.progress is not None and m.has_data else "—"
                ev = f"{tier_label(m.evidence_tier)}" + (" · 代理" if m.metric.is_proxy else "")
                proxy_note = f"（代理：{m.metric.proxy_for}）" if m.metric.is_proxy else ""
                add(f"| {m.metric.name}{proxy_note} | {cur} | {bt} | {prog} | {ev} | "
                    f"{_status_label(m.status)} | {m.metric.cadence} |")
            add("")

    # ── 五、承诺 vs 产能 ────────────────────────────────────
    if budget_rows:
        add("## 五、承诺与产能")
        add("")
        add("| 领域 | 计划小时 | 实际小时 | 差额 | 判断 |")
        add("|---|---|---|---|---|")
        for b in budget_rows:
            planned = b["planned_hours"]
            actual = b["actual_hours"]
            diff = actual - planned
            if diff > planned * 0.15:
                verdict = "超载"
            elif diff < -planned * 0.3:
                verdict = "投入不足"
            else:
                verdict = "符合计划"
            add(f"| {b['domain_name']} | {planned:g} | {actual:g} | {diff:+g} | {verdict} |")
        add("")
        over = [b for b in budget_rows if b["actual_hours"] > b["planned_hours"] * 1.15]
        if over:
            names = "、".join(b["domain_name"] for b in over)
            add(f"> **产能超载**：{names} 领域超出计划。量化系统最常见的失败模式不是"
                f"指标没达成，而是同时承诺太多、每项都推进不足。")
        add("")

    # ── 六、不可量化登记 ────────────────────────────────────
    add("## 六、不可量化登记")
    add("")
    if snap.unquantifiable:
        add("以下事项**无法被可靠量化**。列出它们不是缺陷，而是系统的诚实边界——"
            "假装能测，反而会让系统给出错误的优化方向。")
        add("")
        for u in snap.unquantifiable:
            add(f"- **{u['name']}**：{u['reason']}")
        add("")
    else:
        add("暂无登记项。")
        add("")

    # ── 七、自我审计 ────────────────────────────────────────
    add("## 七、自我审计")
    add("")
    audit = run_audit(conn)
    if audit.findings:
        add(f"**{audit.verdict}**")
        add("")
        for f in audit.findings:
            flag = {"high": "高危", "medium": "中等", "low": "轻微"}[f.severity]
            add(f"- **[{flag}] {f.title}**：{f.detail}")
            if f.suggestion:
                add(f"  - 建议：{f.suggestion}")
        add("")
    else:
        add("基线追认、代理反噬、证据通胀三项检查全部通过。")
        add("")

    # ── 附：宿主画像 ────────────────────────────────────────
    from .presence import load_latest

    latest = load_latest(conn)
    add("## 附：宿主画像（本机推断）")
    add("")
    if not latest:
        add("尚未扫描。运行 `lifeplan host scan` 生成。")
        add("")
    else:
        parsed = latest.get("parsed", {})
        add(f"{latest['summary']}")
        add("")
        add(f"- 扫描时间：{latest['scanned_at']}")
        add(f"- 推断置信度：{latest['confidence']:.2f}（启发式比例，不是概率）")
        hyps = parsed.get("hypotheses", [])
        if hyps:
            add("")
            add("| 假设 | 得分 | 主要证据 |")
            add("|---|---|---|")
            for h in hyps[:4]:
                ev = (h.get("evidence") or ["—"])[0]
                add(f"| {h['label']} | {h['score']} | {ev} |")
        notes = parsed.get("notes", [])
        if notes:
            add("")
            for n in notes:
                add(f"- {n}")
        add("")
        add("> 这是**启发式推断**，依据目录名、git 远端、已装工具与运行进程；"
            "不是事实陈述。真实身份以你自己的判断为准。")
        add("")

    add("---")
    add("")
    add(f"*本报告由 lifeplan 自动生成于 {as_of}。所有数字来自本地 SQLite 数据库，无任何外部上传。*")

    return "\n".join(lines)

"""每周复盘：把「15 次打断」压成「1 次」。

这是针对实测问题做的改动——库里 25 条观测全部来自种子数据、真实观测为 0，
说明「按指标逐个记录」的交互方式在实践中不成立（见 docs/OPTIMIZATION.md）。

改动只做一件事：**换交互单位**。
不再要求"想起来时去记一个指标"，而是每周一次，把到期项一次过完：

    1. 到期未结算的预测      → 逐个 y/n
    2. 过期 / 无观测的指标    → 给建议值，回车即接受
    3. 反复记不上的指标      → 允许当场归档，不再提醒

三条设计约束：
    · 一次最多处理 12 项（`--limit`），保证 3 分钟内能结束
    · 每条都可跳过，不强迫补数据——强迫会让人整个放弃
    · 归档是"降级"而非删除：数据留着，随时可恢复
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .db import utc_now
from .metrics import build_snapshot
from .presence import scan_projects

DEFAULT_LIMIT = 12


@dataclass
class Question:
    kind: str                     # resolve | observe | archive
    ref_id: int
    label: str
    hint: str = ""
    suggestion: float | None = None
    unit: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class WeekPlan:
    as_of: str
    questions: list[Question] = field(default_factory=list)
    auto_notes: list[str] = field(default_factory=list)
    overflow: int = 0

    @property
    def empty(self) -> bool:
        return not self.questions


# ── 自动建议值 ──────────────────────────────────────────────
def _git_commit_count(repos: list[Path], days: int = 7) -> int:
    """统计若干仓库近 N 天的提交数。

    只用于给「有效提交数」这类指标一个起点建议值，读操作、固定 argv、
    不接收任何外部输入，因此不经过终端门禁也不构成注入面。
    """
    total = 0
    for repo in repos:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "log", f"--since={days} days",
                 "--oneline", "--no-merges"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                total += len(proc.stdout.strip().splitlines())
        except (subprocess.SubprocessError, OSError):
            continue
    return total


def auto_suggest(conn, as_of: str | None = None, root: Path | None = None) -> tuple[dict[int, float], list[str]]:
    """为「可以从本地痕迹推断」的指标给出建议值。

    刻意只覆盖确实能推断的那一小部分。睡眠、面试、关系这类推不出来的，
    不做假推断——那会污染台账，比没有数据更糟。
    """
    root = root or Path.home()
    suggestions: dict[int, float] = {}
    notes: list[str] = []

    cur = conn.execute("SELECT id, name, unit FROM metrics WHERE archived = 0")
    metrics = [dict(r) for r in cur.fetchall()]
    wants_commits = [m for m in metrics if "提交" in m["name"] or "commit" in m["name"].lower()]
    if not wants_commits:
        return suggestions, notes

    projects = scan_projects(root)
    repos = [Path(p["path"]) for p in projects if p.get("is_git")]
    if not repos:
        return suggestions, notes

    n = _git_commit_count(repos[:30])
    if n > 0:
        for m in wants_commits:
            suggestions[m["id"]] = float(n)
        notes.append(f"近 7 天在 {min(len(repos), 30)} 个 git 仓库中共 {n} 次提交，"
                     f"已作为「提交类指标」的建议值")

    if len(metrics) > len(wants_commits):
        notes.append("其余指标无法从本地痕迹推断，需要你直接填写（或不填）")
    return suggestions, notes


# ── 生成一周清单 ────────────────────────────────────────────
def build_week(conn, as_of: str | None = None, limit: int = DEFAULT_LIMIT,
               root: Path | None = None, with_suggestions: bool = True) -> WeekPlan:
    as_of = as_of or date.today().isoformat()
    plan = WeekPlan(as_of=as_of)
    if with_suggestions:
        suggestions, notes = auto_suggest(conn, as_of, root)
        plan.auto_notes = notes
    else:
        suggestions = {}

    # 空库/半空库引导：第一次真实使用时最需要的是"下一步做什么"，而不是空清单。
    # 真实库首日踩过两个坑：无目标时说"无需处理"；有目标无指标时也说"无需处理"。
    n_goals = conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    n_metrics = conn.execute("SELECT COUNT(*) FROM metrics WHERE archived=0").fetchone()[0]
    if n_goals == 0:
        plan.auto_notes.insert(0, "库还没有任何目标。定义第一个："
                                  "lifeplan goal add --domain 1 --title '…'，"
                                  "然后 lifeplan metric add 挂指标（基线/目标值你自己定，"
                                  "但记住它们会决定分数——拍之前想清楚）")
        return plan
    if n_metrics == 0:
        plan.auto_notes.insert(
            0, f"有 {n_goals} 个目标，但一个指标都没有——目标没有被测量，无法评分也无法结算。"
               f"下一步：lifeplan metric add --goal <id> --name <指标名> --unit <单位> "
               f"--baseline <基线> --target <目标值> --cadence <周期>"
               f"（基线/目标值必须你自己定，系统不预填）")
        return plan

    # 1) 到期未结算的预测
    cur = conn.execute(
        """SELECT id, statement, horizon_date, p_hit FROM predictions
           WHERE outcome IS NULL AND horizon_date <= ? ORDER BY horizon_date""",
        (as_of,),
    )
    for r in cur.fetchall():
        plan.questions.append(
            Question(
                kind="resolve", ref_id=r["id"],
                label=r["statement"] or f"预测 #{r['id']}",
                hint=f"到期 {r['horizon_date']}，事前 p={r['p_hit']:.2f}",
                extra={"p_hit": r["p_hit"]},
            )
        )

    # 2) 过期 / 无观测的指标
    snap = build_snapshot(conn, as_of)
    for d in snap.domains:
        for g in d.goals:
            for m in g.metrics:
                if m.has_data and m.status != "stale":
                    continue
                last = f"上次 {m.value:g}{m.metric.unit}" if m.has_data else "从未记录"
                plan.questions.append(
                    Question(
                        kind="observe", ref_id=m.metric.id,
                        label=m.metric.name,
                        hint=f"{g.goal['title'][:20]}｜{last}",
                        suggestion=suggestions.get(m.metric.id),
                        unit=m.metric.unit,
                    )
                )

    if len(plan.questions) > limit:
        plan.overflow = len(plan.questions) - limit
        plan.questions = plan.questions[:limit]
    return plan


def render_plan(plan: WeekPlan) -> str:
    if plan.empty:
        lines = ["本周没有需要处理的事项。"]
    else:
        lines = [f"本周复盘清单（{plan.as_of}）：{len(plan.questions)} 项"
                 + (f"，另有 {plan.overflow} 项顺延到下周" if plan.overflow else "")]
        for i, q in enumerate(plan.questions, 1):
            tag = "结算预测" if q.kind == "resolve" else "补记指标"
            lines.append(f"  {i:>2}. [{tag}] {q.label}")
            if q.hint:
                lines.append(f"      {q.hint}")
    for n in plan.auto_notes:
        lines.append(f"  · {n}")
    return "\n".join(lines)


# ── 应用答案 ────────────────────────────────────────────────
def apply_answers(conn, answers: list[tuple]) -> dict:
    """answers: [("resolve", pid, hit) | ("observe", mid, value) | ("archive", mid)]"""
    counts = {"resolved": 0, "observed": 0, "archived": 0}
    now = utc_now()
    for item in answers:
        kind = item[0]
        if kind == "resolve":
            _, pid, hit = item
            conn.execute(
                "UPDATE predictions SET outcome=?, resolved_at=? WHERE id=?",
                (int(hit), now, int(pid)),
            )
            counts["resolved"] += 1
        elif kind == "observe":
            _, mid, value = item
            conn.execute(
                """INSERT INTO observations(metric_id, value, observed_at, note, evidence_ref, tier, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (int(mid), float(value), date.today().isoformat(),
                 "每周复盘补录", "week-review", 1, now),
            )
            counts["observed"] += 1
        elif kind == "archive":
            _, mid = item
            conn.execute("UPDATE metrics SET archived = 1 WHERE id = ?", (int(mid),))
            counts["archived"] += 1
    conn.commit()
    return counts


# ── 交互式跑完一周 ──────────────────────────────────────────
def run_week(
    conn,
    ask=input,
    echo=print,
    as_of: str | None = None,
    limit: int = DEFAULT_LIMIT,
    root: Path | None = None,
    dry_run: bool = False,
    with_suggestions: bool = True,
) -> dict:
    plan = build_week(conn, as_of, limit, root, with_suggestions=with_suggestions)
    if plan.empty:
        echo("本周没有需要处理的事项。")
        return {"questions": 0, "resolved": 0, "observed": 0, "archived": 0, "dry_run": dry_run}

    total = len(plan.questions)
    answers: list[tuple] = []
    skipped = 0

    for i, q in enumerate(plan.questions, 1):
        try:
            if q.kind == "resolve":
                echo(f"\n[{i}/{total}] 预测到期：{q.label}")
                echo(f"        {q.hint}")
                raw = ask("        兑现了吗？[y=兑现 / n=没兑现 / s=跳过] ").strip().lower()
                if raw in ("y", "yes", "1", "是"):
                    answers.append(("resolve", q.ref_id, 1))
                elif raw in ("n", "no", "0", "否"):
                    answers.append(("resolve", q.ref_id, 0))
                else:
                    skipped += 1
            else:
                echo(f"\n[{i}/{total}] 补记指标：{q.label}（{q.unit}）")
                echo(f"        {q.hint}")
                if q.suggestion is not None:
                    prompt = (f"        新值 [回车={q.suggestion:g} / 数字 / "
                              f"s=跳过 / a=归档不再提醒] ")
                else:
                    prompt = "        新值 [数字 / s=跳过 / a=归档不再提醒] "
                raw = ask(prompt).strip()

                if raw == "" and q.suggestion is not None:
                    answers.append(("observe", q.ref_id, q.suggestion))
                elif raw.lower() in ("s", "skip"):
                    skipped += 1
                elif raw.lower() in ("a", "archive"):
                    answers.append(("archive", q.ref_id))
                else:
                    try:
                        answers.append(("observe", q.ref_id, float(raw)))
                    except ValueError:
                        skipped += 1
        except EOFError:
            echo("\n输入中断（非交互环境？）。已收集的答案仍会保存。")
            break

    if dry_run:
        echo(f"\n[dry-run] 未写入。本次将处理 {len(answers)} 项、跳过 {skipped} 项。")
        return {"questions": total, "resolved": 0, "observed": 0,
                "archived": 0, "dry_run": True, "planned": len(answers)}

    counts = apply_answers(conn, answers)
    echo(f"\n✓ 已结算预测 {counts['resolved']} 条、补记观测 {counts['observed']} 条、"
         f"归档指标 {counts['archived']} 个、跳过 {skipped} 项")

    # 立刻给一次校准回读——让「这次输入换来了什么」当场可见
    try:
        from .calibration import compute_calibration, load_predictions

        cal = compute_calibration(load_predictions(conn))
        if cal.brier is not None:
            echo(f"  当前校准：Brier {cal.brier:.4f}，ECE {cal.ece:.4f}，"
                 f"过度自信 {cal.overconfidence:+.4f}（n={cal.n_resolved}）")
            echo(f"  {cal.verdict}")
    except Exception:  # 校准失败不应让复盘本身失败
        pass

    return {"questions": total, **counts, "skipped": skipped, "dry_run": False}

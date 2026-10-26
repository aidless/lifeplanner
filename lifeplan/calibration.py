"""预测校准模块。

借用了评测方法论里成熟的一套：把"我能不能做到"当成一个二分类预测任务，
登记在案、到期结算、用 Brier 分数与期望校准误差（ECE）反过来校准自己。

    p_hit 是事前写下的主观概率，outcome 是事后结算的 0/1。

    Brier = mean((p_hit - outcome)^2)          越低越好
    ECE   = Σ (n_b/N) * |mean_p_b - acc_b|    分箱后校准误差
    可靠曲线：预测概率分箱，横轴预测、纵轴实际发生率，贴近对角线即校准良好

超过自信度（overconfidence）= mean(p_hit) - mean(outcome)，
系统性为正说明"我说能做到的时候，实际做不到的次数更多"。
这个数字往往比总分更能改变行为。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

N_BINS = 10


@dataclass
class Prediction:
    id: int
    goal_id: int
    goal_title: str
    domain_name: str
    horizon_date: str
    p_hit: float
    statement: str
    created_at: str
    resolved_at: str | None = None
    outcome: int | None = None

    @property
    def is_open(self) -> bool:
        return self.outcome is None

    @property
    def brier(self) -> float | None:
        if self.outcome is None:
            return None
        return (self.p_hit - self.outcome) ** 2


@dataclass
class Bin:
    index: int
    lower: float
    upper: float
    count: int
    mean_p: float
    accuracy: float
    gap: float  # mean_p - accuracy, 正值代表过度自信


@dataclass
class CalibrationReport:
    n_resolved: int = 0
    n_open: int = 0
    brier: float | None = None
    baseline_brier: float | None = None   # 同基线率下的参照分
    skill_score: float | None = None      # 1 - brier/baseline，>0 表示优于基线
    ece: float | None = None
    overconfidence: float | None = None
    mean_p: float | None = None
    accuracy: float | None = None
    bins: list[Bin] = field(default_factory=list)
    by_domain: dict[str, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.brier is None:
            return "无已结算预测，无法校准"
        if self.ece is not None and self.ece <= 0.08 and abs(self.overconfidence or 0) <= 0.10:
            return "校准良好：概率与实际基本对齐"
        if (self.overconfidence or 0) > 0.15:
            return "系统性过度自信：说'能做到'时更容易做不到"
        if (self.overconfidence or 0) < -0.15:
            return "系统性过度保守：低估了自己的兑现能力"
        return "校准一般：存在方向性偏差但幅度有限"


def load_predictions(conn, include_open: bool = True) -> list[Prediction]:
    from .db import rows

    sql = """
        SELECT p.*, g.title AS goal_title, d.name AS domain_name
        FROM predictions p
        JOIN goals g ON g.id = p.goal_id
        JOIN domains d ON d.id = g.domain_id
    """
    if not include_open:
        sql += " WHERE p.outcome IS NOT NULL"
    sql += " ORDER BY p.horizon_date, p.id"

    return [
        Prediction(
            id=r["id"],
            goal_id=r["goal_id"],
            goal_title=r["goal_title"],
            domain_name=r["domain_name"],
            horizon_date=r["horizon_date"],
            p_hit=r["p_hit"],
            statement=r["statement"],
            created_at=r["created_at"],
            resolved_at=r["resolved_at"],
            outcome=r["outcome"],
        )
        for r in rows(conn, sql)
    ]


def _bin_index(p: float, n_bins: int = N_BINS) -> int:
    idx = int(p * n_bins)
    return max(0, min(n_bins - 1, idx))


def compute_calibration(predictions: list[Prediction]) -> CalibrationReport:
    resolved = [p for p in predictions if p.outcome is not None]
    open_preds = [p for p in predictions if p.outcome is None]

    report = CalibrationReport(n_resolved=len(resolved), n_open=len(open_preds))

    if not resolved:
        report.notes.append("尚无已结算预测：校准需要至少若干条事前概率与事后结果。")
        return report

    brier = sum((p.p_hit - p.outcome) ** 2 for p in resolved) / len(resolved)
    report.brier = brier

    base_rate = sum(p.outcome for p in resolved) / len(resolved)
    baseline_brier = base_rate * (1 - base_rate)
    report.baseline_brier = baseline_brier
    if baseline_brier > 1e-9:
        report.skill_score = 1.0 - brier / baseline_brier

    mean_p = sum(p.p_hit for p in resolved) / len(resolved)
    acc = base_rate
    report.mean_p = mean_p
    report.accuracy = acc
    report.overconfidence = mean_p - acc

    buckets: dict[int, list[Prediction]] = {}
    for p in resolved:
        buckets.setdefault(_bin_index(p.p_hit), []).append(p)

    bins: list[Bin] = []
    ece = 0.0
    mce = 0.0
    for i in range(N_BINS):
        group = buckets.get(i, [])
        if group:
            mp = sum(p.p_hit for p in group) / len(group)
            ac = sum(p.outcome for p in group) / len(group)
            gap = mp - ac
            ece += (len(group) / len(resolved)) * abs(gap)
            mce = max(mce, abs(gap))
        else:
            mp, ac, gap = None, None, None  # type: ignore[assignment]
        bins.append(
            Bin(
                index=i,
                lower=i / N_BINS,
                upper=(i + 1) / N_BINS,
                count=len(group),
                mean_p=mp,  # type: ignore[arg-type]
                accuracy=ac,  # type: ignore[arg-type]
                gap=gap,  # type: ignore[arg-type]
            )
        )
    report.bins = bins
    report.ece = ece
    report.notes.append(f"最大分箱偏差 (MCE) = {mce:.3f}")

    # 分领域校准：找出我尤其不擅长的领域
    dom_groups: dict[str, list[Prediction]] = {}
    for p in resolved:
        dom_groups.setdefault(p.domain_name, []).append(p)
    for name, group in dom_groups.items():
        b = sum((p.p_hit - p.outcome) ** 2 for p in group) / len(group)
        mp = sum(p.p_hit for p in group) / len(group)
        ac = sum(p.outcome for p in group) / len(group)
        report.by_domain[name] = {
            "n": len(group),
            "brier": b,
            "mean_p": mp,
            "accuracy": ac,
            "overconfidence": mp - ac,
        }

    if len(resolved) < 10:
        report.notes.append(
            f"样本量偏小 (n={len(resolved)})，校准结论只能作为初步信号，不足以支撑强判断。"
        )
    return report


def due_predictions(conn, as_of: str | None = None) -> list[Prediction]:
    """已到期但未结算的预测。"""
    as_of = as_of or date.today().isoformat()
    return [
        p
        for p in load_predictions(conn)
        if p.is_open and p.horizon_date <= as_of
    ]


def overdue_days(pred: Prediction, as_of: str | None = None) -> int:
    as_of = as_of or date.today().isoformat()
    try:
        h = datetime.fromisoformat(pred.horizon_date).date()
        a = datetime.fromisoformat(as_of).date()
    except ValueError:
        return 0
    return (a - h).days


def suggested_p_hit(current_progress: float, days_left: int) -> float:
    """基于当前进展与剩余天数给出的朴素概率锚点（**不是模型，只是参考值**）。

    思路：剩余进展比例 × 时间压力，压出一个大致概率。它的唯一用途是在
    `lifeplan predict` 时给人一个起点，避免每次都从 0.5 开始拍。
    真正有价值的不是这个数，而是事后结算出来的偏差。
    """
    remaining = 1.0 - max(0.0, min(1.0, current_progress))
    if remaining <= 0:
        return 0.97
    if days_left <= 0:
        return 0.05
    time_pressure = 1.0 - min(1.0, days_left / 90.0) * 0.5
    raw = 1.0 - remaining * (1.5 - time_pressure)
    return max(0.02, min(0.97, raw))

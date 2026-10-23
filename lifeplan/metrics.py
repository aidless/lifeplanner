"""指标归一化与评分引擎。

核心取舍：**不输出单一总分**。

把价值分与证据置信度压成一个标量，等于让系统自己的排名变成优化目标，
随后所有人都会开始优化那个标量（Goodhart 定律）。
因此系统输出一对坐标 (value, confidence)，综合分只是附带的投影值。

各方向指标的归一化：
    increase  p = (v - baseline) / (target - baseline)
    decrease  p = (baseline - v) / (baseline - target)
    maintain  落在 [baseline ± tolerance] 内记 1，超出后线性衰减

未观测的指标计 0 分且不参与置信度加权 —— 没有数据不是"表现差"，
但也不是"有进展"，这在报告里会单列为数据覆盖率，避免误读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .evidence import apply_evidence_gap, normalize_tier, tier_weight

# 置信度与价值的融合系数：confidence=0 时只保留 50% 价值分
CONFIDENCE_BLEND = 0.5

CADENCE_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "yearly": 365,
}

DEFAULT_OBSERVATION_WINDOW_DAYS = 90


@dataclass
class Metric:
    id: int
    goal_id: int
    name: str
    unit: str
    direction: str
    baseline: float
    target: float
    tolerance: float
    weight: float
    cadence: str
    evidence_tier: int
    is_proxy: bool
    proxy_for: str
    baseline_at: str


@dataclass
class Observation:
    id: int
    metric_id: int
    value: float
    observed_at: str
    note: str
    evidence_ref: str
    tier: int


@dataclass
class MetricState:
    """单个指标的当前状态快照。"""

    metric: Metric
    value: float | None
    progress: float | None          # 归一化进展 [0,1]
    confidence: float               # 证据置信度 [0,1]
    observation_count: int
    last_observed_at: str | None
    staleness_days: int
    evidence_tier: int
    has_data: bool

    @property
    def status(self) -> str:
        if not self.has_data:
            return "no_data"
        if self.progress is not None and self.progress >= 0.999:
            return "done"
        if self.staleness_days > CADENCE_DAYS.get(self.metric.cadence, 7) * 2:
            return "stale"
        return "active"


@dataclass
class GoalState:
    goal: dict
    metrics: list[MetricState] = field(default_factory=list)

    @property
    def value(self) -> float:
        weights = sum(m.metric.weight for m in self.metrics) or 1.0
        total = 0.0
        for m in self.metrics:
            p = m.progress if (m.progress is not None and m.has_data) else 0.0
            total += m.metric.weight * p
        return total / weights

    @property
    def confidence(self) -> float:
        weights = sum(m.metric.weight for m in self.metrics) or 1.0
        total = 0.0
        for m in self.metrics:
            total += m.metric.weight * m.confidence
        return total / weights

    @property
    def composite(self) -> float:
        """价值分按置信度折减后的投影值。"""
        return self.value * (CONFIDENCE_BLEND + (1 - CONFIDENCE_BLEND) * self.confidence)

    @property
    def coverage(self) -> float:
        """有数据的指标权重占比。"""
        weights = sum(m.metric.weight for m in self.metrics) or 1.0
        covered = sum(m.metric.weight for m in self.metrics if m.has_data)
        return covered / weights


@dataclass
class DomainState:
    domain: dict
    goals: list[GoalState] = field(default_factory=list)

    @property
    def value(self) -> float:
        weights = sum(g.goal.get("weight", 1.0) for g in self.goals) or 1.0
        total = sum(g.goal.get("weight", 1.0) * g.value for g in self.goals)
        return total / weights

    @property
    def confidence(self) -> float:
        weights = sum(g.goal.get("weight", 1.0) for g in self.goals) or 1.0
        total = sum(g.goal.get("weight", 1.0) * g.confidence for g in self.goals)
        return total / weights

    @property
    def composite(self) -> float:
        return self.value * (CONFIDENCE_BLEND + (1 - CONFIDENCE_BLEND) * self.confidence)


@dataclass
class Snapshot:
    as_of: str
    domains: list[DomainState] = field(default_factory=list)
    unquantifiable: list[dict] = field(default_factory=list)

    @property
    def value(self) -> float:
        weights = sum(d.domain.get("weight", 1.0) for d in self.domains) or 1.0
        total = sum(d.domain.get("weight", 1.0) * d.value for d in self.domains)
        return total / weights

    @property
    def confidence(self) -> float:
        weights = sum(d.domain.get("weight", 1.0) for d in self.domains) or 1.0
        total = sum(d.domain.get("weight", 1.0) * d.confidence for d in self.domains)
        return total / weights

    @property
    def composite(self) -> float:
        return self.value * (CONFIDENCE_BLEND + (1 - CONFIDENCE_BLEND) * self.confidence)

    @property
    def coverage(self) -> float:
        """全局数据覆盖率：有权重的指标中，有实际观测的比例。"""
        all_metrics = [m for d in self.domains for g in d.goals for m in g.metrics]
        if not all_metrics:
            return 0.0
        total_w = sum(m.metric.weight for m in all_metrics)
        covered_w = sum(m.metric.weight for m in all_metrics if m.has_data)
        return covered_w / total_w

    @property
    def metric_count(self) -> int:
        return sum(len(g.metrics) for d in self.domains for g in d.goals)

    @property
    def stale_count(self) -> int:
        return sum(
            1 for d in self.domains for g in d.goals for m in g.metrics if m.status == "stale"
        )

    @property
    def no_data_count(self) -> int:
        return sum(
            1 for d in self.domains for g in d.goals for m in g.metrics if not m.has_data
        )


def goal_progress(
    value: float,
    baseline: float,
    target: float,
    direction: str = "increase",
    tolerance: float = 0.0,
) -> float:
    """把原始观测值映射为进展分。**全项目唯一的进度公式**。

    三种方向：
        increase   (v − b) / (t − b)
        decrease   (b − v) / (b − t)
        maintain   落在 [b ± tolerance] 内记 1，超出后按 max(tol, |b|/2) 线性衰减

    返回值**不裁剪上界**：> 1 表示超额完成，调用方需要时自己 min(1.0, ·)。
    这一点很重要——审计模块要看到"超额"，评分模块则要裁到 1。

    历史上这个公式在 audit.py 里有一份内联副本，且漏了 maintain 分支
    （maintain 目标被当成 increase 计算，结果是负分）。现在统一到此处。
    """
    if direction == "maintain":
        tol = tolerance if tolerance > 0 else max(abs(baseline) * 0.1, 1e-9)
        deviation = abs(value - baseline)
        if deviation <= tol:
            return 1.0
        span = max(tol, abs(baseline) * 0.5, 1e-9)
        return max(0.0, 1.0 - (deviation - tol) / span)

    if direction == "decrease":
        denom = baseline - target
        if abs(denom) < 1e-12:
            return 1.0 if value <= target else 0.0
        return (baseline - value) / denom

    denom = target - baseline
    if abs(denom) < 1e-12:
        return 1.0 if value >= target else 0.0
    return (value - baseline) / denom


def normalize_value(value: float, metric: Metric) -> float:
    """把原始观测值映射到 [0,1] 的进展分（goal_progress 的便捷封装）。"""
    return goal_progress(
        value, metric.baseline, metric.target, metric.direction, metric.tolerance
    )


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value).date()


def compute_metric_state(
    metric: Metric,
    observations: list[Observation],
    as_of: str,
) -> MetricState:
    today = _parse_date(as_of)
    window_days = max(
        CADENCE_DAYS.get(metric.cadence, 7) * 2,
        DEFAULT_OBSERVATION_WINDOW_DAYS,
    )
    cutoff = today - timedelta(days=window_days)

    recent = []
    for obs in observations:
        try:
            if _parse_date(obs.observed_at) >= cutoff:
                recent.append(obs)
        except ValueError:
            continue

    if not recent:
        # 窗口内无观测，但历史上有观测：不谎报"无数据"，
        # 而是沿用最后已知值并标记为过期 + 重罚置信度。
        # 对累计型指标（论文数、投递数）尤其重要——没人记录不等于归零。
        if not observations:
            return MetricState(
                metric=metric,
                value=None,
                progress=None,
                confidence=0.0,
                observation_count=0,
                last_observed_at=None,
                staleness_days=9999,
                evidence_tier=normalize_tier(metric.evidence_tier),
                has_data=False,
            )
        latest_any = max(observations, key=lambda o: o.observed_at)
        stale_state = compute_metric_state(
            metric,
            [latest_any],
            latest_any.observed_at,
        )
        return MetricState(
            metric=metric,
            value=stale_state.value,
            progress=stale_state.progress,
            confidence=stale_state.confidence * 0.4,
            observation_count=1,
            last_observed_at=latest_any.observed_at,
            staleness_days=(today - _parse_date(latest_any.observed_at)).days,
            evidence_tier=stale_state.evidence_tier,
            has_data=True,
        )

    recent.sort(key=lambda o: o.observed_at)
    latest = recent[-1]
    # 裁剪到 [0,1]：目标价值是"完成比例"，不能超过 100%。
    # 超额信息保留在原始观测里（实际值 vs target 一眼可见），
    # 但聚合分若不裁剪，一次超额就会把整个领域价值顶到 5.5 —— 实测发生过。
    progress = min(1.0, max(0.0, normalize_value(latest.value, metric)))

    tier_values = [normalize_tier(o.tier) for o in recent]
    achieved_tier = max(tier_values)
    mean_tier_weight = sum(tier_weight(t) for t in tier_values) / len(tier_values)

    required_tier = normalize_tier(metric.evidence_tier)
    confidence = apply_evidence_gap(mean_tier_weight, achieved_tier, required_tier)

    staleness = (today - _parse_date(latest.observed_at)).days
    cadence_days = CADENCE_DAYS.get(metric.cadence, 7)
    if staleness > cadence_days * 2:
        confidence *= 0.4
    elif staleness > cadence_days:
        confidence *= 0.7

    return MetricState(
        metric=metric,
        value=latest.value,
        progress=progress,
        confidence=min(1.0, max(0.0, confidence)),
        observation_count=len(recent),
        last_observed_at=latest.observed_at,
        staleness_days=staleness,
        evidence_tier=achieved_tier,
        has_data=True,
    )


def build_snapshot(
    conn,
    as_of: str | None = None,
) -> Snapshot:
    """从数据库构建整体状态快照。"""
    from .db import rows

    as_of = as_of or date.today().isoformat()

    domains = rows(conn, "SELECT * FROM domains ORDER BY sort_order, id")
    goals = rows(
        conn,
        "SELECT * FROM goals WHERE status='active' ORDER BY id",
    )
    # 归档指标不参与评分，也不进入提醒；数据保留，可恢复
    metrics_rows = rows(conn, "SELECT * FROM metrics WHERE archived = 0 ORDER BY id")

    obs_rows = rows(conn, "SELECT * FROM observations ORDER BY observed_at")

    obs_by_metric: dict[int, list[Observation]] = {}
    for o in obs_rows:
        obs_by_metric.setdefault(o["metric_id"], []).append(
            Observation(
                id=o["id"],
                metric_id=o["metric_id"],
                value=o["value"],
                observed_at=o["observed_at"],
                note=o["note"],
                evidence_ref=o["evidence_ref"],
                tier=o["tier"],
            )
        )

    metrics_by_goal: dict[int, list[Metric]] = {}
    for m in metrics_rows:
        metrics_by_goal.setdefault(m["goal_id"], []).append(
            Metric(
                id=m["id"],
                goal_id=m["goal_id"],
                name=m["name"],
                unit=m["unit"],
                direction=m["direction"],
                baseline=m["baseline"],
                target=m["target"],
                tolerance=m["tolerance"],
                weight=m["weight"],
                cadence=m["cadence"],
                evidence_tier=m["evidence_tier"],
                is_proxy=bool(m["is_proxy"]),
                proxy_for=m["proxy_for"],
                baseline_at=m["baseline_at"],
            )
        )

    goals_by_domain: dict[int, list[dict]] = {}
    for g in goals:
        goals_by_domain.setdefault(g["domain_id"], []).append(g)

    snapshot = Snapshot(as_of=as_of)
    for d in domains:
        ds = DomainState(domain=d)
        for g in goals_by_domain.get(d["id"], []):
            gs = GoalState(goal=g)
            for m in metrics_by_goal.get(g["id"], []):
                gs.metrics.append(compute_metric_state(m, obs_by_metric.get(m.id, []), as_of))
            ds.goals.append(gs)
        snapshot.domains.append(ds)

    snapshot.unquantifiable = rows(
        conn, "SELECT * FROM unquantifiable WHERE status != 'retired' ORDER BY id"
    )
    return snapshot

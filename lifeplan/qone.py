"""Q-One：把「万事万物皆可量化」落成可复算的测量协议。

六层架构（与设计约定一一对应）：

    量化编译器 qcompile()      自然语言问题 → 测量计划（v1 为垂直域规则，
                               LLM 拆解放到 v2——先用规则保证可复算）
    证据 Agent   _evidence()   本地证据：git 统计 / 数据库 / 文件（每项带 sha256）
    计算引擎     calc.py       所有数值在此产生，模型永不碰算术
    决策模型     _decide()     Laya/Jev 风格 typed questions（choice/noul），
                               不可用时降级为文档化的阈值规则
    不确定性层   _uncertainty() 区间（自助法）、置信度（证据×校准）、拒答策略
    溯源账本     ledger.py     每次决策完整留痕 + 哈希校验

v1 垂直域：**项目投入决策**（「这个项目是否值得继续投入？」）。
其他域先显式拒绝——诚实的Scope 好过虚假的通用。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import calc
from .ledger import DecisionRecord, record, sha256_of

# ── 问题类型（与 Laya/Jev 的 choice/score/noul 对齐，measure 为本系统扩展）──
QTYPES = ("measure", "score", "choice", "noul")

VERTICAL = "project_investment"      # v1 唯一覆盖的垂直域
VERTICAL_HINTS = ("项目", "投入", "继续", "值得", "project", "invest", "continue")


@dataclass
class Question:
    qid: str
    type: str                          # measure | score | choice | noul
    unit: str = ""
    criteria: dict | list | None = None  # score 的有序量表 / choice 的选项与判据
    instructions: str = ""


@dataclass
class QOneAnswer:
    state: str
    questions: list
    values: dict                        # measure 题 → {value, interval}
    scores: dict                        # score 题 → {level, rule}
    action: str                         # choice 题的裁决
    confidence: float
    evidence: list
    assumptions: list
    unknowns: list
    next_measurement: str | None
    decided_by: str                     # "laya" | "rules"

    def to_contract(self) -> dict:
        """对外 JSON 契约：value/interval/confidence/action/evidence/assumptions/unknowns。"""
        out: dict = {}
        for qid, v in self.values.items():
            out[qid] = {"value": v["value"], "interval": v["interval"],
                        "unit": v["unit"]}
        for qid, v in self.scores.items():
            out[qid] = {"level": v["level"], "rule": v["rule"]}
        out["action"] = self.action
        out["confidence"] = round(self.confidence, 3)
        out["evidence"] = self.evidence
        out["assumptions"] = self.assumptions
        out["unknowns"] = self.unknowns
        out["next_measurement"] = self.next_measurement
        out["decided_by"] = self.decided_by
        return out


# ── 量化编译器 ──────────────────────────────────────────────
def qcompile(state: str) -> tuple[str, list[Question], list[str]]:
    """问题 → (垂直域, 问题集, 未知项)。

    v1 规则版：命中垂直域关键词才展开测量计划；否则显式拒绝并给出
    unknown——不假装能量化一切，这本身就是协议的一部分。
    """
    lowered = state.lower()
    if not any(h in lowered for h in VERTICAL_HINTS):
        return "unsupported", [], [
            f"垂直域 v1 只覆盖「{VERTICAL}」；这个问题需要 v2 的 LLM 编译器"
        ]

    questions = [
        Question("投入强度", "measure", "commits_per_30d",
                 instructions="近 30 天 git 提交数（可核验的活动量代理）"),
        Question("证据密度", "measure", "verified_obs_per_30d",
                 instructions="近 30 天 tier≥2 的观测数（有据可查的进展）"),
        Question("自评准确度", "measure", "score_0_1",
                 instructions="1 − |过度自信度|，来自校准台账"),
        Question("结构完整度", "measure", "ratio_0_1",
                 instructions="有指标的目标占比（没有指标的目标无法结算）"),
        Question("risk", "score", criteria=["低", "中", "高", "极高"],
                 instructions="继续投入的风险等级"),
        Question("action", "choice",
                 criteria={"continue": "继续投入", "pivot": "调整方向",
                           "stop": "停止", "need_data": "先补充证据"},
                 instructions="应该对这个项目做什么"),
        Question("证据充分性", "noul",
                 instructions="现有证据是否足以支撑上面的裁决"),
    ]
    return VERTICAL, questions, []


# ── 证据 Agent ──────────────────────────────────────────────
def _evidence(conn, as_of: str | None = None, root=None) -> tuple[dict, list]:
    """收集本地证据。返回 (信号值, 证据清单[含 sha256])。"""
    as_of = as_of or date.today().isoformat()
    since = (date.fromisoformat(as_of) - timedelta(days=30)).isoformat()
    signals: dict = {}
    evidence: list = []

    # 1) git 活动（只读、固定 argv）
    try:
        from .presence import scan_projects

        repos = [p for p in scan_projects(root) if p.get("is_git")]
        n = 0
        for p in repos[:30]:
            try:
                import subprocess

                r = subprocess.run(
                    ["git", "-C", p["path"], "log", "--since=30 days",
                     "--oneline", "--no-merges"],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    n += len(r.stdout.strip().splitlines())
            except Exception:
                continue
        signals["commit_30d"] = float(n)
        evidence.append({"kind": "git", "detail": f"30 天内 {min(len(repos),30)} 个仓库提交",
                          "value": n, "sha256": sha256_of({"git_commits_30d": n, "repos": min(len(repos),30)})})
    except Exception as exc:
        signals["commit_30d"] = None
        evidence.append({"kind": "git", "detail": f"扫描失败：{type(exc).__name__}",
                          "value": None, "sha256": sha256_of({"git_error": str(exc)})})

    # 2) 观测密度（tier≥2）
    row = conn.execute(
        "SELECT COUNT(*) n FROM observations WHERE observed_at >= ? AND tier >= 2",
        (since,),
    ).fetchone()
    signals["verified_obs_30d"] = float(row["n"])
    evidence.append({"kind": "db", "detail": "observations WHERE observed_at>=? AND tier>=2",
                      "value": row["n"], "sha256": sha256_of({"verified_obs_30d": row["n"], "since": since})})

    # 3) 校准质量（自评准确度）
    overconfidence = None
    n_resolved = 0
    try:
        from .calibration import compute_calibration, load_predictions

        cal = compute_calibration(load_predictions(conn))
        overconfidence = cal.overconfidence
        n_resolved = cal.n_resolved
    except Exception:
        pass
    signals["calibration_overconfidence"] = overconfidence
    signals["calibration_n"] = float(n_resolved)
    evidence.append({"kind": "db", "detail": "calibration.overconfidence",
                      "value": overconfidence,
                      "sha256": sha256_of({"overconfidence": overconfidence, "n": n_resolved})})

    # 4) 结构完整度（有指标的目标占比）
    goals = conn.execute("SELECT COUNT(*) n FROM goals").fetchone()["n"]
    goals_with_metrics = conn.execute(
        "SELECT COUNT(DISTINCT goal_id) n FROM metrics WHERE archived=0"
    ).fetchone()["n"]
    signals["goals"] = float(goals)
    signals["goals_with_metrics"] = float(goals_with_metrics)
    evidence.append({"kind": "db", "detail": "goals vs metrics 覆盖",
                      "value": f"{goals_with_metrics}/{goals}",
                      "sha256": sha256_of({"goals": goals, "goals_with_metrics": goals_with_metrics})})

    return signals, evidence


# ── 不确定性层 ──────────────────────────────────────────────
def _uncertainty(signals: dict, evidence: list) -> tuple[dict, list]:
    """数值 + 区间；证据缺口 → unknown。"""
    values: dict = {}
    unknowns: list = []

    def measure(qid: str, unit: str, value, series=None):
        interval = calc.bootstrap_ci(series) if series else None
        values[qid] = {"value": value, "interval": interval, "unit": unit}

    measure("投入强度", "commits_per_30d", signals.get("commit_30d"))
    measure("证据密度", "verified_obs_per_30d", signals.get("verified_obs_30d"))

    oc = signals.get("calibration_overconfidence")
    acc = None if oc is None else calc.clamp(1.0 - abs(oc))
    measure("自评准确度", "score_0_1", acc)
    if signals.get("calibration_n", 0) < 5:
        unknowns.append(f"校准样本不足（n={int(signals.get('calibration_n', 0))}），"
                        f"自评准确度只能当方向参考")

    goals = signals.get("goals") or 0
    gwm = signals.get("goals_with_metrics") or 0
    structural = calc.ratio(gwm, goals)
    measure("结构完整度", "ratio_0_1", structural)
    if structural is not None and structural < 1.0:
        unknowns.append(f"{int(goals - gwm)} 个目标没有指标，无法被结算")

    if signals.get("commit_30d") is None:
        unknowns.append("git 活动不可读（无仓库或无权限）")

    # 综合价值分：三个可核验维度的加权（权重写进假设，不写进模型）
    parts = [
        (calc.clamp((signals.get("commit_30d") or 0) / 40.0), 0.4, "投入强度：40 次提交/30 天记满分"),
        (calc.clamp((signals.get("verified_obs_30d") or 0) / 10.0), 0.3, "证据密度：10 条 tier≥2 观测/30 天记满分"),
        (acc if acc is not None else 0.5, 0.3, "自评准确度：1−|过度自信|"),
    ]
    value = sum(v * w for v, w, _ in parts) * 100
    dims = [v for v, _, _ in parts]
    interval = calc.bootstrap_ci(dims)
    # 区间传播：以加权均值为中心，用维度间离散度做粗略扩张（不是统计严格的预测区间）
    values["价值总评"] = {
        "value": round(value, 1),
        "interval": ([round(max(0.0, min(100.0, value - (0.5 - interval[0]) * 100)), 1),
                      round(max(0.0, min(100.0, value + (interval[1] - 0.5) * 100)), 1)]
                     if interval else None),
        "unit": "score_0_100",
    }
    return values, unknowns


# ── 决策模型（choice/noul） ─────────────────────────────────
def _decide(state: str, signals: dict, values: dict) -> tuple[str, float, str, list]:
    """choice 题交给决策模型（Laya），降级为文档化规则。

    拒答策略（测试钉死）：没有任何可核验证据时——无 tier≥2 观测、
    无校准样本、无指标目标——**只允许 need_data**。
    实测教训：空库上 731 次 git 提交曾把价值分撑到 55、裁决 pivot——
    活动量冒充了证据。git 提交是"在动"，不是"在对"。
    """
    no_real_evidence = (
        (signals.get("verified_obs_30d") or 0) == 0
        and (signals.get("calibration_n") or 0) < 5
        and (signals.get("goals_with_metrics") or 0) == 0
    )
    if no_real_evidence:
        return "need_data", 0.8, "abstain", [
            "无可核验观测、无校准样本、无指标目标——按拒答策略只裁决 need_data"
        ]

    value = values.get("价值总评", {}).get("value", 0)
    structural = values.get("结构完整度", {}).get("value")
    unknowns_structural = structural is not None and structural < 0.5

    # 规则基线（透明、可复算；也是模型不可用时的兜底）
    if unknowns_structural:
        rule_action, rule_conf = "need_data", 0.7
    elif value >= 60:
        rule_action, rule_conf = "continue", 0.6
    elif value >= 35:
        rule_action, rule_conf = "pivot", 0.55
    else:
        rule_action, rule_conf = "stop", 0.6

    # 尝试 Laya（System 1）做 choice 判断
    try:
        from .agent.adapters import SystemOneAdapter

        s1 = SystemOneAdapter()
        ok, _ = s1.status()
        if not ok:
            return rule_action, rule_conf, "rules", []

        state_text = (
            f"项目投入决策。state：{state}。"
            f"价值总评 {value}/100，投入强度 {values.get('投入强度', {}).get('value')} 次/30天，"
            f"证据密度 {values.get('证据密度', {}).get('value')} 条/30天，"
            f"自评准确度 {values.get('自评准确度', {}).get('value')}，"
            f"结构完整度 {structural}"
        )
        questions = {
            "action": {"type": "choice",
                       "instructions": "这个项目应该继续投入、调整方向、停止，还是先补充证据？",
                       "criteria": {"continue": "价值与证据都支持继续",
                                    "pivot": "方向需调整",
                                    "stop": "投入产出不成立",
                                    "need_data": "关键证据缺失，无法判断"}},
        }
        res = s1._get_agent().predict(state_text, questions)
        ans = res.get("answers", {}).get("action", {})
        action = ans.get("choice")
        conf = ans.get("confidence", 0.0) or 0.0
        if action in ("continue", "pivot", "stop", "need_data") and conf >= 0.5:
            return action, conf, "laya", []
        return rule_action, rule_conf, "rules", ["Laya 置信不足，降级为规则"]
    except Exception as exc:
        return rule_action, rule_conf, "rules", [f"Laya 不可用（{type(exc).__name__}），降级为规则"]


# ── 主编排 ──────────────────────────────────────────────────
def ask(state: str, conn, as_of: str | None = None, root=None,
        persist: bool = True) -> QOneAnswer:
    vertical, questions, unknowns = qcompile(state)
    if vertical == "unsupported":
        ans = QOneAnswer(
            state=state, questions=[], values={}, scores={}, action="need_data",
            confidence=0.0, evidence=[], assumptions=["垂直域 v1 只覆盖项目投入决策"],
            unknowns=unknowns, next_measurement="用 v2 的 LLM 编译器拆解这个问题",
            decided_by="rules",
        )
        if persist:
            record(conn, DecisionRecord(
                state=state, questions=[], answer=ans.to_contract(),
                assumptions=ans.assumptions, unknowns=ans.unknowns,
                next_measurement=ans.next_measurement,
            ))
        return ans

    signals, evidence = _evidence(conn, as_of, root)
    values, unknowns2 = _uncertainty(signals, evidence)
    unknowns = unknowns + unknowns2
    action, conf, decided_by, notes = _decide(state, signals, values)

    # 总置信度 = 裁决置信度 × 证据覆盖度（证据缺一半，置信度就打对折）
    coverage = sum(1 for e in evidence if e.get("value") is not None) / max(len(evidence), 1)
    confidence = calc.clamp(conf * (0.5 + 0.5 * coverage))

    ans = QOneAnswer(
        state=state,
        questions=[{"qid": q.qid, "type": q.type, "unit": q.unit,
                    "criteria": q.criteria, "instructions": q.instructions}
                   for q in questions],
        values=values,
        scores={"risk": {"level": _risk_level(values), "rule": "价值总评<35→极高；35-60→高；60-80→中；≥80→低"}},
        action=action,
        confidence=confidence,
        evidence=evidence,
        assumptions=[
            "投入强度以 git 提交为代理，不等于价值创造",
            "证据密度只计 tier≥2 观测；自述不计入",
            "权重 0.4/0.3/0.3 为第一版先验，未经拟合",
        ] + notes,
        unknowns=unknowns,
        next_measurement=_next_step(action, unknowns),
        decided_by=decided_by,
    )
    if persist:
        record(conn, DecisionRecord(
            state=state, questions=ans.questions, answer=ans.to_contract(),
            evidence=evidence, assumptions=ans.assumptions, unknowns=unknowns,
            next_measurement=ans.next_measurement,
        ))
    return ans


def _risk_level(values: dict) -> str:
    v = values.get("价值总评", {}).get("value", 0)
    if v >= 80:
        return "低"
    if v >= 60:
        return "中"
    if v >= 35:
        return "高"
    return "极高"


def _next_step(action: str, unknowns: list) -> str:
    if unknowns:
        return unknowns[0]
    return {"continue": "下周复盘时复核投入强度是否维持",
            "pivot": "定义新的方向假设并登记一条预测",
            "stop": "归档项目，把资源转移到结构完整度更高的目标",
            "need_data": "先补证据再裁决"}.get(action, "补充证据")

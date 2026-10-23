"""首次引导：把「定义指标 + 登记预测」压缩成一次模板编辑。

设计（对应 FAILURE_LOG F-01/F-07 的纪律）：
    · 系统**只提案，绝不预填数值**——baseline/target/p 全部留空
    · 提案来自本体的草案定义（指标名/单位/方向），可逐条改
    · 应用前校验（缺值/方向矛盾/p 不在 (0,1) 一律拒绝），应用后立即可见效果

格式选择 TOML 而非 YAML：stdlib `tomllib` 即可解析（3.11+，3.10 用 tomli），
支持注释，不为一纸模板引入第三方依赖。

用法：
    lifeplan setup init     # 生成 ~/.lifeplan/setup.toml（提案，数值留空）
    lifeplan setup check    # 只校验不写入
    lifeplan setup apply    # 校验并写入
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None


# 目标 → 本体草案指标（只提案指标名/单位/方向/频率；数值一律由主人填）
GOAL_METRIC_HINTS = [
    (("offer", "岗位", "求职"), [
        ("有效投递数", "份", "increase", "weekly", 3),
        ("面试转化率", "%", "increase", "monthly", 3),
    ]),
    (("奖牌", "竞赛", "CASMI", "最佳分"), [
        ("公榜最佳分", "分", "decrease", "weekly", 3),
    ]),
    (("TMLR", "投稿", "论文", "首投"), [
        ("成稿提交数", "篇", "increase", "monthly", 3),
    ]),
    (("写作", "产出", "文章"), [
        ("新稿发布数", "篇", "increase", "monthly", 3),
    ]),
    # 开源：以「持续活跃」为主，不以「被合并 PR 数」为主。
    # 理由：外部 PR 合并受维护者响应速度支配，不是自己能控的变量；
    # 把不可控变量当主指标 = 代理反噬。活跃度是自己可控、且可核验的。
    (("开源", "社区贡献", "PR"), [
        ("周活跃天数", "天", "increase", "weekly", 3),
        ("发布次数", "次", "increase", "monthly", 3),
        ("合并的外部 PR 数", "个", "increase", "quarterly", 3),
    ]),
]

# 外部锚点：只收录可公开查证、且与目标直接相关的参考值。
# 用途仅是给 baseline/target 一个参照，**不是系统建议的答案**。
# 每条都带来源与日期；过期的锚点必须删除或更新。
EXTERNAL_ANCHORS = {
    "公榜最佳分": ("铜牌线约 0.451（Kaggle 公榜公开阈值，2026-10 快照）", ),
    "有效投递数": ("参考类：你自己历史周投递量（本库无观测时留空，不猜）", ),
    "面试转化率": ("参考类：面试数 ÷ 定制投递数；行业基准不可直接搬用", ),
    "成稿提交数": ("参考类：你的既有成稿储备（库存文件数，可数）", ),
    "合并的外部 PR 数": ("参考类：过去 12 个月你向他人项目提过几次 PR、合并几次"
                        "（受维护者响应速度支配，不是自己可控——所以只作副指标）", ),
    "周活跃天数": ("口径：git 提交 / issue / review / release 任一发生记为活跃；"
                "target 建议取「能长期维持」而非「拼一周」", ),
    "发布次数": ("参考类：过去 12 个月自己项目的 release / 版本 tag 数（可数）", ),
}

# (statement, horizon, 结算标准, 目标匹配关键词)
# ⚠ horizon 必须是**未来日期**——2027-03-31 是当前规划期，不是 2026（今天已 2026-10-23）
BIG_DECISIONS = [
    ("CASMI26 公榜最佳分进入铜牌区（≥0.451）", "2026-12-14",
     "结算标准：Kaggle 公榜页面（tier3）", ("奖牌", "竞赛", "CASMI", "最佳分")),
    ("完成 TMLR 首投（≥1 篇，approval-gates 或 agent-effort）", "2027-03-31",
     "结算标准：投稿系统确认邮件（tier3）", ("TMLR", "投稿", "首投")),
    ("开源项目保持对外可见的持续更新（≥1 次/月）至 2027-03-31", "2027-03-31",
     "结算标准：自己的仓库在 2027-01~03 每月至少有 1 次公开提交/release（tier3）",
     ("开源", "社区贡献", "社区")),
    ("向他人项目提交的 PR 至少 1 个被合并（非自己项目）", "2027-06-30",
     "结算标准：目标仓库的 PR 页面显示 merged（tier3）；受他方响应影响，故放宽期限",
     ("开源", "社区贡献", "社区")),
]


def setup_path() -> Path:
    from .db import db_path

    return db_path().parent / "setup.toml"


def generate_template(conn) -> str:
    goals = conn.execute(
        "SELECT id, title FROM goals WHERE status='active' ORDER BY id"
    ).fetchall()

    lines = [
        "# lifeplan 首次引导（setup.toml）",
        "# 规则：系统只提案指标名与单位；baseline / target / p 全部由你填。",
        "# 你填的每个数都会进评分与校准曲线——我填的数已被记录在案地错过两次。",
        "#",
        "# baseline = 现在的值（第一次观测前冻结；事后改 = 基线追认，审计标红）",
        "# target   = 到期想达到的值（你拍）",
        "# p_hit    = 你认为该预测会发生的概率，开区间 (0,1)；到期结算进校准曲线",
        "#",
        "# TOML 没有 null：未填的项是注释掉的，去掉行首 # 即生效。",
        "# 填完：lifeplan setup check  →  lifeplan setup apply",
        "",
    ]

    for g in goals:
        title = g["title"]
        hints = next(
            (ms for keys, ms in GOAL_METRIC_HINTS
             if any(k.lower() in title.lower() for k in keys)),
            [("自定义指标", "单位", "increase", "weekly", 2)],
        )
        lines.append(f"# ── 目标 #{g['id']}：{title}")
        for name, unit, direction, cadence, tier in hints:
            if name in EXTERNAL_ANCHORS:
                for anchor in EXTERNAL_ANCHORS[name]:
                    lines.append(f"# 外部锚点：{anchor}")
            lines += [
                "[[metrics]]",
                f"goal_id = {g['id']}",
                f'name = "{name}"',
                f'unit = "{unit}"',
                f'direction = "{direction}"',
                "# baseline = 0.0     # ← 去掉行首 # 并填当前值",
                "# target = 0.0       # ← 去掉行首 # 并填目标值",
                f'cadence = "{cadence}"',
                f"tier = {tier}",
                "",
            ]

    lines.append("# ── 三条大决策（你的概率，系统不猜）")
    lines.append("#")
    lines.append("# 两步法（把直觉变成可复算的估计）：")
    lines.append("#   1) 参考类：过去 12 个月里，同类事件发生了多少次 / 给了多少次机会？")
    lines.append("#      例如「提交后 14 天内进面试」：若 20 次投递里 3 次 → 基准 0.15")
    lines.append("#   2) 收缩：你有实测的过度自信倾向（演示数据 +0.17）。")
    lines.append("#      p_final ≈ clamp(基准 + 0.05~0.10 的上调, 0.05, 0.95)")
    lines.append("#      注意：是「先按参考类定基准，再小幅上调」，不是「凭感觉拍再砍一半」")
    lines.append("#   3) 写进 note 里：你用的是哪个参考类、哪个数字")
    lines.append("#      —— 到期结算时才知道是「估计错」还是「参考类选错」")
    for stmt, horizon, note, keys in BIG_DECISIONS:
        matched = next(
            (g["id"] for g in goals
             if any(k.lower() in g["title"].lower() for k in keys)),
            None,
        )
        if matched is None:
            # 目标不存在（如该目标已暂停）→ 不写这条预测，并留下说明
            lines.append(f"# ⚠ 跳过预测「{stmt[:20]}…」：没有匹配的 active 目标")
            lines.append("")
            continue
        goal_id = matched
        lines += [
            "[[predictions]]",
            f"goal_id = {goal_id}",
            f'statement = "{stmt}"',
            f'horizon_date = "{horizon}"',
            "# p_hit = 0.5         # ← 去掉行首 # 并填你的概率（0-1 开区间）",
            f'note = "{note}"',
            "",
        ]
    return "\n".join(lines)


def load(path: Path) -> dict:
    if tomllib is None:
        raise RuntimeError("需要 tomllib（Python 3.11+）或 tomli（pip install tomli）")
    with open(path, "rb") as f:
        return tomllib.load(f)


def validate(doc: dict, only: int | None = None) -> list[str]:
    """校验模板。only=goal_id 时只校验该目标下的条目。

    为什么需要 only：一次只想推进一个目标时，其余目标没填的数值不该挡住它。
    但「只校验一部分」的代价是可能漏掉整体冲突，所以调用方必须显式声明 only，
    且 apply 时也只写这部分——不允许「部分校验、全部写入」。
    """
    errors: list[str] = []

    def _keep(item) -> bool:
        return only is None or item.get("goal_id") == only

    metrics = [m for m in (doc.get("metrics") or []) if _keep(m)]
    if not metrics and only is None:
        errors.append("metrics 为空——至少要定义一个指标")
    for i, m in enumerate(metrics, 1):
        for field in ("goal_id", "name", "unit", "direction", "baseline", "target"):
            if m.get(field) is None:
                hint = "（去掉行首 # 后填数）" if field in ("baseline", "target") else ""
                errors.append(f"metrics[{i}] 缺 {field}{hint}")
        if m.get("baseline") is not None and m.get("target") is not None:
            d = m.get("direction")
            if d == "increase" and m["target"] <= m["baseline"]:
                errors.append(f"metrics[{i}] increase 型要求 target > baseline")
            if d == "decrease" and m["target"] >= m["baseline"]:
                errors.append(f"metrics[{i}] decrease 型要求 target < baseline")
    preds = [p for p in (doc.get("predictions") or []) if _keep(p)]
    if not preds and only is None:
        errors.append("predictions 为空——至少登记一条预测，否则校准无从开始")
    for i, p in enumerate(preds, 1):
        if p.get("p_hit") is None:
            errors.append(f"predictions[{i}] 缺 p_hit（去掉行首 # 后填你的概率）")
        elif not (0 < float(p["p_hit"]) < 1):
            errors.append(f"predictions[{i}] p_hit 必须在 (0,1) 开区间——"
                          f"0 和 1 不是概率，是断言")
        for field in ("goal_id", "statement", "horizon_date"):
            if not p.get(field):
                errors.append(f"predictions[{i}] 缺 {field}")
    return errors


def apply_setup(conn, doc: dict, only: int | None = None) -> dict:
    """写入指标与预测。only=goal_id 时只写该目标（与 validate 的 only 必须一致）。"""
    now = datetime.now().isoformat(timespec="seconds")
    n_metrics = n_preds = 0
    for m in doc.get("metrics") or []:
        if only is not None and m.get("goal_id") != only:
            continue
        g = conn.execute("SELECT start_date FROM goals WHERE id=?", (m["goal_id"],)).fetchone()
        baseline_at = g[0] if g else date.today().isoformat()
        conn.execute(
            """INSERT INTO metrics(goal_id, name, unit, direction, baseline, target, tolerance,
                                   weight, cadence, evidence_tier, is_proxy, proxy_for,
                                   baseline_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (m["goal_id"], m["name"], m["unit"], m["direction"], m["baseline"], m["target"],
             m.get("tolerance", 0.0), m.get("weight", 1.0), m.get("cadence", "weekly"),
             m.get("tier", 2), 0, "", baseline_at, now),
        )
        n_metrics += 1
    for p in doc.get("predictions") or []:
        if only is not None and p.get("goal_id") != only:
            continue
        conn.execute(
            """INSERT INTO predictions(goal_id, horizon_date, p_hit, statement, created_at,
                                       outcome, evidence_ref, outcome_note)
               VALUES (?,?,?,?,?,NULL,?,?)""",
            (p["goal_id"], p["horizon_date"], float(p["p_hit"]), p["statement"], now,
             "", p.get("note", "")),
        )
        n_preds += 1
    conn.commit()
    return {"metrics": n_metrics, "predictions": n_preds}

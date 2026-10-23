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
]

BIG_DECISIONS = [
    ("CASMI26 公榜最佳分进入铜牌区（≥0.451）", "2026-12-14",
     "结算标准：Kaggle 公榜页面（tier3）"),
    ("拿到上海或杭州的 LLM/Agent 评测岗 offer（≥1 个）", "2026-06-30",
     "结算标准：offer 邮件或合同文本（tier3）"),
    ("完成 TMLR 首投（≥1 篇，approval-gates 或 agent-effort）", "2026-03-31",
     "结算标准：投稿系统确认邮件（tier3）"),
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
    lines.append("# 已实测：过度自信是倾向（演示数据 +0.17）——拍时往低调半档。")
    for i, (stmt, horizon, note) in enumerate(BIG_DECISIONS, 1):
        goal_id = goals[0]["id"] if goals else 1
        for g in goals:
            t = g["title"].lower()
            if i == 1 and ("奖牌" in t or "竞赛" in t):
                goal_id = g["id"]
            elif i == 2 and ("offer" in t or "岗位" in t):
                goal_id = g["id"]
            elif i == 3 and ("tmlr" in t or "投稿" in t):
                goal_id = g["id"]
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


def validate(doc: dict) -> list[str]:
    errors: list[str] = []
    metrics = doc.get("metrics") or []
    if not metrics:
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
    preds = doc.get("predictions") or []
    if not preds:
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


def apply_setup(conn, doc: dict) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    n_metrics = n_preds = 0
    for m in doc.get("metrics") or []:
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

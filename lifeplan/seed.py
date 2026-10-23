"""种子数据：按"真实人生"的结构预置的**虚构演示数据**。

领域划分刻意采用四象限：
    事业深耕 / 长期资产 / 健康底盘 / 关系与意义

最后一项（关系与意义）在多数量化系统里被删掉，因为难量化。
这里保留它，并且用"不可量化登记表"显式承认它的测量困难，
而不是假装它不存在。

注意：本文件是演示/测试用的**虚构数据集**（一个虚构的开发者画像），
不是任何真实人物的记录。真实使用请 `lifeplan init`（不带 --seed）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .db import utc_now


def seed_all(conn) -> None:
    now = utc_now()
    today = date.today()
    week_start = (today - timedelta(days=today.weekday())).isoformat()

    domains = [
        ("事业深耕", 1.5, "#4c6ef5", 1),
        ("长期资产", 1.2, "#7048e8", 2),
        ("健康底盘", 1.0, "#12b886", 3),
        ("关系与意义", 0.8, "#f76707", 4),
    ]
    domain_ids: dict[str, int] = {}
    for name, weight, color, order in domains:
        cur = conn.execute(
            "INSERT INTO domains(name, weight, color, sort_order, created_at) VALUES (?,?,?,?,?)",
            (name, weight, color, order, now),
        )
        domain_ids[name] = cur.lastrowid

    def add_goal(domain: str, title: str, desc: str, start: str, end: str, weight: float = 1.0) -> int:
        cur = conn.execute(
            """INSERT INTO goals(domain_id, title, description, start_date, end_date, weight, status, created_at)
               VALUES (?,?,?,?,?,?,'active',?)""",
            (domain_ids[domain], title, desc, start, end, weight, now),
        )
        return cur.lastrowid

    def add_metric(
        goal_id: int,
        name: str,
        unit: str,
        direction: str,
        baseline: float,
        target: float,
        cadence: str,
        tier: int,
        weight: float = 1.0,
        tolerance: float = 0.0,
        is_proxy: bool = False,
        proxy_for: str = "",
        baseline_at: str | None = None,
    ) -> int:
        # 基线冻结时间：默认取所属目标的开始日期 —— 基线必须先于观测存在，
        # 否则就是"事后追认"，审计模块会把它标为高危。
        if baseline_at is None:
            g = conn.execute("SELECT start_date FROM goals WHERE id=?", (goal_id,)).fetchone()
            baseline_at = g[0] if g else today.isoformat()
        cur = conn.execute(
            """INSERT INTO metrics(goal_id, name, unit, direction, baseline, target, tolerance,
                                   weight, cadence, evidence_tier, is_proxy, proxy_for, baseline_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                goal_id, name, unit, direction, baseline, target, tolerance, weight,
                cadence, tier, 1 if is_proxy else 0, proxy_for, baseline_at, now,
            ),
        )
        return cur.lastrowid

    # ── 事业深耕 ────────────────────────────────────────────────
    g1 = add_goal(
        "事业深耕", "完成一次职业方向转换",
        "从投递到拿到 offer；优先目标城市，远程可谈。",
        (today - timedelta(days=90)).isoformat(),
        (today + timedelta(days=150)).isoformat(),
        weight=2.0,
    )
    m1 = add_metric(g1, "有效投递数", "份", "increase", 0, 40, "weekly", 3, weight=1.2)
    m2 = add_metric(g1, "面试转化率", "%", "increase", 0, 15, "monthly", 3, weight=1.0)
    m3 = add_metric(g1, "公开竞赛最佳分", "分", "decrease", 0.5, 0.4, "weekly", 3, weight=1.5)
    m4 = add_metric(
        g1, "面试复盘完成率", "%", "increase", 0, 100, "monthly", 2,
        weight=0.8, is_proxy=True, proxy_for="真实面试能力（以过程质量代理结果）",
    )

    g2 = add_goal(
        "事业深耕", "技术写作稳定产出",
        "每月一篇长文；限额每月 2 篇新稿。",
        (today - timedelta(days=60)).isoformat(),
        (today + timedelta(days=300)).isoformat(),
        weight=1.0,
    )
    add_metric(g2, "新稿发布数", "篇", "increase", 0, 2, "monthly", 3, weight=1.5)
    add_metric(g2, "外部审稿轮次", "轮", "increase", 0, 26, "monthly", 2, weight=0.8)

    g3 = add_goal(
        "事业深耕", "开源项目活跃度维持",
        "保持有意义的更新，而非空转提交。",
        (today - timedelta(days=60)).isoformat(),
        (today + timedelta(days=180)).isoformat(),
        weight=0.6,
    )
    add_metric(g3, "月度有效提交数", "次", "increase", 0, 12, "monthly", 3, weight=1.0)
    add_metric(g3, "新增外部用户/Star", "个", "increase", 0, 30, "monthly", 2, weight=0.8)

    # ── 长期资产 ────────────────────────────────────────────────
    g4 = add_goal(
        "长期资产", "知识资产沉淀",
        "公开文章与工具文档的存量积累；全部如实标注状态，不虚报。",
        (today - timedelta(days=120)).isoformat(),
        (today + timedelta(days=300)).isoformat(),
        weight=1.0,
    )
    add_metric(g4, "公开文章数", "篇", "increase", 10, 16, "quarterly", 3, weight=1.2)
    add_metric(g4, "成稿储备", "份", "increase", 10, 20, "quarterly", 2, weight=0.8)

    g5 = add_goal(
        "长期资产", "资金缓冲垫",
        "职业过渡期的现金流安全垫，避免被迫接受不合适岗位。",
        (today - timedelta(days=90)).isoformat(),
        (today + timedelta(days=150)).isoformat(),
        weight=1.0,
    )
    add_metric(g5, "储蓄月数覆盖", "月", "increase", 0, 6, "monthly", 3, weight=1.2)

    # ── 健康底盘 ────────────────────────────────────────────────
    g6 = add_goal(
        "健康底盘", "睡眠与体力不塌方",
        "高强度并行期最容易被牺牲的部分，但它是其余一切的前置条件。",
        (today - timedelta(days=30)).isoformat(),
        (today + timedelta(days=180)).isoformat(),
        weight=1.2,
    )
    add_metric(
        g6, "日均睡眠", "小时", "maintain", 7.0, 7.0, "daily", 2,
        weight=1.0, tolerance=0.8,
    )
    add_metric(g6, "每周运动次数", "次", "increase", 2, 4, "weekly", 2, weight=1.0)
    add_metric(g6, "连续熬夜天数", "天", "decrease", 2, 0, "weekly", 1, weight=0.8)

    # ── 关系与意义 ──────────────────────────────────────────────
    g7 = add_goal(
        "关系与意义", "维持重要关系与心流体验",
        "这一维度的量化最容易失真，故以过程频次作代理，并承认其局限。",
        (today - timedelta(days=30)).isoformat(),
        (today + timedelta(days=365)).isoformat(),
        weight=0.8,
    )
    add_metric(
        g7, "深度对话次数", "次", "increase", 0, 4, "weekly", 1,
        weight=1.0, is_proxy=True, proxy_for="关系亲密度（以主动深度交流频次代理）",
    )
    add_metric(
        g7, "心流状态小时", "小时", "increase", 0, 10, "weekly", 1,
        weight=1.0, is_proxy=True, proxy_for="工作意义感（以心流时长代理）",
    )

    # ── 不可量化登记表：系统的诚实性边界 ────────────────────────
    unquant = [
        (
            "职业长期意义感",
            "意义感高度依赖未来的自我叙事与外部反馈，无法用当前频次/时长类指标可靠代表；"
            "用'心流小时'代理会系统性高估可量化工作、忽视维护性工作。",
            None,
        ),
        (
            "亲密关系的质量",
            "互动次数与关系质量之间不存在稳定单调关系，高频低质互动反而可能造成负向代理偏差。",
            None,
        ),
        (
            "认知能力的真实增长",
            "评测分数、产出数等活动量指标无法直接代表能力存量是否真正提升。",
            None,
        ),
    ]
    for name, reason, proxy in unquant:
        conn.execute(
            "INSERT INTO unquantifiable(name, reason, proxy_metric_id, status, created_at) VALUES (?,?,?,?,?)",
            (name, reason, proxy, "open", now),
        )

    # ── 少量历史观测：让系统一上来就有非零状态 ──────────────────
    def add_obs(metric_id: int, value: float, days_ago: int, tier: int, ref: str = "", note: str = "") -> None:
        obs_date = (today - timedelta(days=days_ago)).isoformat()
        conn.execute(
            """INSERT INTO observations(metric_id, value, observed_at, note, evidence_ref, tier, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (metric_id, value, obs_date, note, ref, tier, now),
        )

    add_obs(m1, 13, 35, 2, "applications.csv", "第一周")
    add_obs(m1, 17, 28, 2, "applications.csv", "第二周")
    add_obs(m1, 14, 21, 2, "applications.csv", "第三周，节奏回落")
    add_obs(m1, 22, 14, 3, "https://example.com/tracker", "持续投递中")
    add_obs(m1, 19, 7, 3, "https://example.com/tracker", "近一周")
    add_obs(m2, 9, 14, 2, "interview-log.md", "有 9 次进入面试环节")
    add_obs(m3, 0.53, 21, 3, "contest.csv", "早期提交")
    add_obs(m3, 0.51, 7, 3, "contest.csv", "当前最佳")
    add_obs(m4, 40, 14, 2, "interview-log.md", "复盘覆盖 40%")

    # 睡眠 / 运动 / 熬夜
    g6_metrics = [
        r[0] for r in conn.execute("SELECT id FROM metrics WHERE goal_id = ? ORDER BY id", (g6,)).fetchall()
    ]
    if len(g6_metrics) >= 3:
        sleep_id, exercise_id, late_id = g6_metrics[0], g6_metrics[1], g6_metrics[2]
        for d in range(21, -1, -3):
            add_obs(sleep_id, 6.4 + (d % 3) * 0.4, d, 2, "sleep-tracker.csv")
        add_obs(exercise_id, 3, 14, 2, "gym-log.md")
        add_obs(exercise_id, 2, 7, 2, "gym-log.md")
        add_obs(late_id, 3, 14, 1, "", "一周内三次熬夜")
        add_obs(late_id, 1, 7, 1, "", "改善中")

    g7_metrics = [
        r[0] for r in conn.execute("SELECT id FROM metrics WHERE goal_id = ? ORDER BY id", (g7,)).fetchall()
    ]
    if len(g7_metrics) >= 2:
        conv_id, flow_id = g7_metrics[0], g7_metrics[1]
        add_obs(conv_id, 2, 14, 1, "", "本周两次较深的交流")
        add_obs(conv_id, 3, 7, 1, "", "略有上升")
        add_obs(flow_id, 7.5, 14, 1, "", "心流时长")
        add_obs(flow_id, 9.0, 7, 1, "", "心流时长")

    # ── 预测台账：带历史结算样本，让校准模块有内容可算 ───────────
    preds = [
        (g1, (today - timedelta(days=40)).isoformat(), 0.60, "40 天内拿到 3 次以上面试", 1,
         (today - timedelta(days=40)).isoformat()),
        (g1, (today - timedelta(days=40)).isoformat(), 0.50, "40 天内拿到 offer", 0,
         (today - timedelta(days=40)).isoformat()),
        (g2, (today - timedelta(days=30)).isoformat(), 0.80, "本月发布 2 篇新稿", 1,
         (today - timedelta(days=30)).isoformat()),
        (g3, (today - timedelta(days=25)).isoformat(), 0.70, "本月完成 12 次有效提交", 0,
         (today - timedelta(days=25)).isoformat()),
        (g6, (today - timedelta(days=21)).isoformat(), 0.85, "未来一周日均睡眠不低于 7 小时", 1,
         (today - timedelta(days=21)).isoformat()),
        (g6, (today - timedelta(days=21)).isoformat(), 0.70, "未来一周运动 4 次", 0,
         (today - timedelta(days=21)).isoformat()),
        (g1, (today - timedelta(days=14)).isoformat(), 0.65, "14 天内投递 8 份", 1,
         (today - timedelta(days=14)).isoformat()),
        (g1, (today - timedelta(days=14)).isoformat(), 0.55, "14 天内进入面试", 0,
         (today - timedelta(days=14)).isoformat()),
        (g2, (today + timedelta(days=20)).isoformat(), 0.75, "20 天内发布 2 篇新稿", None, None),
        (g1, (today + timedelta(days=45)).isoformat(), 0.45, "45 天内拿到 offer", None, None),
    ]
    for goal_id, horizon, p_hit, statement, outcome, resolved in preds:
        created = (
            datetime.fromisoformat(horizon) - timedelta(days=14)
        ).isoformat()
        conn.execute(
            """INSERT INTO predictions(goal_id, horizon_date, p_hit, statement, created_at, resolved_at, outcome)
               VALUES (?,?,?,?,?,?,?)""",
            (goal_id, horizon, p_hit, statement, created, resolved, outcome),
        )

    # ── 时间预算：暴露"承诺多于产能"的问题 ──────────────────────
    budgets = [
        ("事业深耕", 30, 26),
        ("长期资产", 12, 14),
        ("健康底盘", 8, 5),
        ("关系与意义", 6, 3),
    ]
    for name, planned, actual in budgets:
        conn.execute(
            "INSERT INTO budget(domain_id, week_start, planned_hours, actual_hours) VALUES (?,?,?,?)",
            (domain_ids[name], week_start, planned, actual),
        )

    conn.execute(
        "INSERT INTO events(ts, kind, payload) VALUES (?,?,?)",
        (now, "seed", '{"source":"seed_all"}'),
    )
    # 打上种子标记：否则 doctor / 报告无法区分"演示数据"和"真实记录"
    try:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('seeded_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now,),
        )
    except Exception:
        pass
    conn.commit()

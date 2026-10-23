"""命令行界面。

设计取向：像 git 一样可组合、可重复。子命令动词化，数据全部留在本地 SQLite。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .calibration import compute_calibration, load_predictions, due_predictions
from .db import connect, db_path, init_db, is_initialized, log_event, recent_events, rows, utc_now
from .evidence import infer_tier, tier_label
from .metrics import build_snapshot


def _err(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    raise SystemExit(1)


def _ensure_db() -> None:
    if not is_initialized():
        _err("数据库尚未初始化。先运行：lifeplan init")


def _open():
    conn = connect()
    # 老库升级：补缺失的表，再补缺失的列（都不动已有数据）
    from .db import ensure_schema

    ensure_schema(conn)
    return conn


# ── init ───────────────────────────────────────────────────
def cmd_init(args) -> None:
    path = init_db(seed=args.seed)
    print(f"✓ 数据库已初始化：{path}")
    if args.seed:
        print("✓ 已写入种子数据（4 领域 / 7 目标 / 13 指标 / 10 条预测）")
    else:
        print("  提示：加 --seed 可写入一份贴近真实的示例数据")


# ── goal ───────────────────────────────────────────────────
def cmd_goal_add(args) -> None:
    conn = _open()
    end = args.end or (date.today() + timedelta(days=90)).isoformat()
    cur = conn.execute(
        """INSERT INTO goals(domain_id, title, description, start_date, end_date, weight, status, created_at)
           VALUES (?,?,?,?,?,?,'active',?)""",
        (args.domain, args.title, args.desc or "", args.start, end, args.weight, utc_now()),
    )
    conn.commit()
    log_event(conn, "goal.add", {"id": cur.lastrowid, "title": args.title})
    conn.commit()
    print(f"✓ 目标 #{cur.lastrowid} 已创建：{args.title}")
    print(f"  接下来：lifeplan metric add --goal {cur.lastrowid} --name \"...\" "
          f"--baseline 0 --target 10 --unit 次 --tier 2")


def cmd_goal_list(args) -> None:
    conn = _open()
    snap = build_snapshot(conn, args.as_of)
    if not snap.domains:
        print("还没有任何目标。")
        return
    for d in snap.domains:
        print(f"\n▌{d.domain['name']}　价值 {d.value:.2f} / 置信 {d.confidence:.2f} / 覆盖 {d.coverage*100:.0f}%")
        for g in d.goals:
            print(f"  ○ #{g.goal['id']} {g.goal['title']}")
            for m in g.metrics:
                val = f"{m.value:g}{m.metric.unit}" if m.value is not None else "—"
                prog = f"{m.progress*100:.0f}%" if m.has_data and m.progress is not None else "无数据"
                proxy = " [代理]" if m.metric.is_proxy else ""
                print(f"      · {m.metric.name}: {val} ({prog}) "
                      f"[{tier_label(m.evidence_tier)}]{proxy}")


def cmd_metric_add(args) -> None:
    conn = _open()
    cur = conn.execute(
        """INSERT INTO metrics(goal_id, name, unit, direction, baseline, target, tolerance,
                               weight, cadence, evidence_tier, is_proxy, proxy_for, baseline_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (args.goal, args.name, args.unit or "", args.direction, args.baseline, args.target,
         args.tolerance, args.weight, args.cadence, args.tier, 1 if args.proxy else 0,
         args.proxy_for or "", date.today().isoformat(), utc_now()),
    )
    conn.commit()
    log_event(conn, "metric.add", {"id": cur.lastrowid, "goal": args.goal})
    conn.commit()
    print(f"✓ 指标 #{cur.lastrowid} 已创建：{args.name}")
    print(f"  基线 {args.baseline:g} → 目标 {args.target:g}，要求证据等级 {args.tier}（{tier_label(args.tier)}）")


# ── observe ────────────────────────────────────────────────
def cmd_observe(args) -> None:
    conn = _open()
    tier = args.tier if args.tier is not None else infer_tier(args.evidence or "", args.note or "")
    cur = conn.execute(
        """INSERT INTO observations(metric_id, value, observed_at, note, evidence_ref, tier, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (args.metric, args.value, args.date, args.note or "", args.evidence or "", tier, utc_now()),
    )
    conn.commit()
    log_event(conn, "observe", {"id": cur.lastrowid, "metric": args.metric, "value": args.value})
    conn.commit()
    print(f"✓ 已记录：指标 #{args.metric} = {args.value:g}　证据等级 {tier}（{tier_label(tier)}）")


# ── predict / resolve ──────────────────────────────────────
def cmd_predict(args) -> None:
    from .calibration import suggested_p_hit

    conn = _open()
    cur = conn.execute(
        """INSERT INTO predictions(goal_id, horizon_date, p_hit, statement, created_at)
           VALUES (?,?,?,?,?)""",
        (args.goal, args.horizon, args.p, args.statement or "", utc_now()),
    )
    conn.commit()
    log_event(conn, "predict", {"id": cur.lastrowid, "goal": args.goal, "p": args.p})
    conn.commit()
    print(f"✓ 预测 #{cur.lastrowid} 已登记：p={args.p:.2f}　到期 {args.horizon}")
    print(f"  「{args.statement or '(未写陈述)'}」")

    # 参考锚点：把「当前进展 + 剩余天数」折算成一个朴素概率，
    # 用来对照人填的值。它不是模型，价值在于事后看偏差方向。
    try:
        snap = build_snapshot(conn)
        goal = next(
            (g for d in snap.domains for g in d.goals if g.goal["id"] == args.goal), None
        )
        if goal is not None:
            try:
                days_left = (date.fromisoformat(args.horizon) - date.today()).days
            except ValueError:
                days_left = 0
            anchor = suggested_p_hit(goal.value, days_left)
            gap = args.p - anchor
            mood = "偏乐观" if gap > 0.1 else ("偏保守" if gap < -0.1 else "接近锚点")
            print(f"  参考锚点 p≈{anchor:.2f}（按当前进展 {goal.value*100:.0f}% 与"
                  f"剩余 {days_left} 天推算，非模型）")
            print(f"  你填的是 {args.p:.2f}（{gap:+.2f}，{mood}）")
    except Exception:  # 锚点计算失败不影响登记本身
        pass

    print(f"  到期后结算：lifeplan resolve {cur.lastrowid} --hit 1|0")


def cmd_resolve(args) -> None:
    from .evidence import infer_tier

    conn = _open()
    row = rows(conn, "SELECT * FROM predictions WHERE id=?", (args.id,))
    if not row:
        _err(f"预测 #{args.id} 不存在")
    pred = dict(row[0])
    if pred["outcome"] is not None:
        _err(f"预测 #{args.id} 已结算过（outcome={pred['outcome']}），"
             f"令牌一次性有效——重新结算请撤销后重新登记")

    evidence = args.evidence or ""
    tier = infer_tier(evidence, args.note or "")
    conn.execute(
        "UPDATE predictions SET outcome=?, resolved_at=?, evidence_ref=?, outcome_note=? "
        "WHERE id=?",
        (args.hit, utc_now(), evidence, args.note or "", args.id),
    )
    conn.commit()
    log_event(conn, "resolve", {"id": args.id, "outcome": args.hit,
                                "evidence_tier": tier})
    conn.commit()
    cal = compute_calibration(load_predictions(conn))
    print(f"✓ 预测 #{args.id} 已结算：{'命中' if args.hit else '未中'}")
    if evidence:
        print(f"  结算依据：{evidence}（证据等级 {tier}）")
    else:
        print("  ⚠ 未提供结算依据——此条结算按「自述」计，"
              "对外陈述校准结论时样本量要扣除它")
    if cal.brier is not None:
        print(f"  当前 Brier = {cal.brier:.4f}　ECE = {cal.ece:.4f}　"
              f"过度自信 {cal.overconfidence:+.4f}（n={cal.n_resolved}）")


def cmd_due(args) -> None:
    from .calibration import overdue_days

    conn = _open()
    due = due_predictions(conn)
    if not due:
        print("没有到期未结算的预测。")
        return
    print(f"有 {len(due)} 条预测已到期未结算：\n")
    for p in due:
        late = overdue_days(p)
        late_txt = f"　逾期 {late} 天" if late > 0 else ""
        print(f"  #{p.id}  p={p.p_hit:.2f}  {p.goal_title}{late_txt}")
        print(f"       「{p.statement}」　到期 {p.horizon_date}")
        print(f"       结算：lifeplan resolve {p.id} --hit 1   或   --hit 0")
    print("\n  提示：lifeplan week 可以一次把到期项全过完。")


# ── score ──────────────────────────────────────────────────
def cmd_score(args) -> None:
    conn = _open()
    snap = build_snapshot(conn, args.as_of)
    if args.json:
        print(json.dumps({
            "as_of": snap.as_of,
            "value": snap.value,
            "confidence": snap.confidence,
            "composite": snap.composite,
            "coverage": snap.coverage,
            "metric_count": snap.metric_count,
            "stale_count": snap.stale_count,
            "no_data_count": snap.no_data_count,
            "domains": [
                {
                    "name": d.domain["name"],
                    "value": d.value,
                    "confidence": d.confidence,
                    "composite": d.composite,
                    "goals": [
                        {
                            "id": g.goal["id"],
                            "title": g.goal["title"],
                            "value": g.value,
                            "confidence": g.confidence,
                            "coverage": g.coverage,
                            "metrics": [
                                {
                                    "name": m.metric.name,
                                    "value": m.value,
                                    "progress": m.progress,
                                    "confidence": m.confidence,
                                    "status": m.status,
                                    "is_proxy": m.metric.is_proxy,
                                }
                                for m in g.metrics
                            ],
                        }
                        for g in d.goals
                    ],
                }
                for d in snap.domains
            ],
        }, ensure_ascii=False, indent=2))
        return

    print(f"\n生命状态　截止 {snap.as_of}")
    print("=" * 56)
    print(f"  价值分 value          {snap.value:.3f}")
    print(f"  证据置信度 confidence {snap.confidence:.3f}")
    print(f"  折减后投影 composite  {snap.composite:.3f}")
    print(f"  数据覆盖率            {snap.coverage*100:.0f}%")
    print(f"  过期 / 无数据指标      {snap.stale_count} / {snap.no_data_count}")
    print()
    for d in snap.domains:
        cov = sum(g.coverage for g in d.goals) / (len(d.goals) or 1) * 100
        print(f"  {d.domain['name']:<10} 价值 {d.value:.2f}　置信 {d.confidence:.2f}　"
              f"覆盖 {cov:.0f}%")
    if snap.metric_count == 0:
        print("\n  （还没有指标。下一步：lifeplan goal add 补目标，"
              "lifeplan metric add 挂指标，或 lifeplan week 按引导走）")
    print()


def cmd_calibrate(args) -> None:
    conn = _open()
    cal = compute_calibration(load_predictions(conn))
    if args.json:
        print(json.dumps({
            "n_resolved": cal.n_resolved,
            "n_open": cal.n_open,
            "brier": cal.brier,
            "skill_score": cal.skill_score,
            "ece": cal.ece,
            "overconfidence": cal.overconfidence,
            "mean_p": cal.mean_p,
            "accuracy": cal.accuracy,
            "verdict": cal.verdict,
            "by_domain": cal.by_domain,
        }, ensure_ascii=False, indent=2))
        return
    print(f"\n{cal.verdict}\n")
    print(f"  已结算 {cal.n_resolved} 条　待结算 {cal.n_open} 条")
    if cal.brier is not None:
        print(f"  Brier           {cal.brier:.4f}")
        print(f"  Skill Score     {cal.skill_score:+.4f}" if cal.skill_score is not None else "")
        print(f"  ECE             {cal.ece:.4f}")
        print(f"  过度自信度      {cal.overconfidence:+.4f}")
        print(f"  平均预测 {cal.mean_p:.3f}　实际命中 {cal.accuracy:.3f}")
        print()
        print("  分领域过度自信：")
        for name, st in sorted(cal.by_domain.items(), key=lambda kv: -kv[1]["overconfidence"]):
            print(f"    {name:<12} n={st['n']:<3} 偏差 {st['overconfidence']:+.2f}")
    else:
        print("  尚无已结算预测。用 `lifeplan predict` 登记预测，`lifeplan resolve` 结算。")
    print()


# ── report / dashboard / export ────────────────────────────
def cmd_report(args) -> None:
    from .report import render_report

    conn = _open()
    text = render_report(conn, args.as_of)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"✓ 报告已写入 {args.out}")
    else:
        print(text)


def cmd_dashboard(args) -> None:
    from .dashboard import render_dashboard

    conn = _open()
    html = render_dashboard(conn, args.as_of)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ 看板已生成：{args.out}")


def cmd_export(args) -> None:
    conn = _open()
    data = {
        "domains": rows(conn, "SELECT * FROM domains ORDER BY sort_order"),
        "goals": rows(conn, "SELECT * FROM goals ORDER BY id"),
        "metrics": rows(conn, "SELECT * FROM metrics ORDER BY id"),
        "observations": rows(conn, "SELECT * FROM observations ORDER BY observed_at"),
        "predictions": rows(conn, "SELECT * FROM predictions ORDER BY id"),
        "unquantifiable": rows(conn, "SELECT * FROM unquantifiable ORDER BY id"),
        "budget": rows(conn, "SELECT * FROM budget ORDER BY week_start, id"),
    }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"✓ 已导出 {args.out}")
    else:
        print(text)


def cmd_unquantifiable(args) -> None:
    conn = _open()
    if args.action == "add":
        conn.execute(
            "INSERT INTO unquantifiable(name, reason, proxy_metric_id, status, created_at) VALUES (?,?,?,?,?)",
            (args.name, args.reason, args.proxy_metric, args.status, utc_now()),
        )
        conn.commit()
        print(f"✓ 已登记不可量化项：{args.name}")
        return
    items = rows(conn, "SELECT * FROM unquantifiable ORDER BY id")
    if not items:
        print("暂无登记项。")
        return
    print()
    for u in items:
        print(f"  ● {u['name']}　[{u['status']}]")
        print(f"    {u['reason']}")
        if u["proxy_metric_id"]:
            print(f"    代理指标：#{u['proxy_metric_id']}")
        print()


def cmd_audit(args) -> None:
    from .audit import run_audit

    conn = _open()
    rep = run_audit(conn)
    if args.json:
        print(json.dumps({
            "verdict": rep.verdict,
            "checked": rep.checked,
            "findings": [
                {
                    "severity": f.severity,
                    "kind": f.kind,
                    "title": f.title,
                    "detail": f.detail,
                    "entity": f.entity,
                    "suggestion": f.suggestion,
                }
                for f in rep.findings
            ],
        }, ensure_ascii=False, indent=2))
        return
    print(f"\n{rep.verdict}\n")
    if not rep.findings:
        print("  基线冻结、代理一致性、证据等级三项检查全部通过。")
        return
    icon = {"high": "●", "medium": "○", "low": "·"}
    for f in rep.findings:
        color = {"high": "高危", "medium": "中等", "low": "轻微"}[f.severity]
        print(f"  {icon[f.severity]} [{color}] {f.title}")
        print(f"      {f.detail}")
        if f.suggestion:
            print(f"      → {f.suggestion}")
        print()


def cmd_events(args) -> None:
    conn = _open()
    for e in recent_events(conn, args.limit):
        print(f"  {e['ts']}  {e['kind']:<14} {e['payload']}")


# ── 宿主感知 ────────────────────────────────────────────────
def cmd_host(args) -> None:
    from .presence import build_profile, load_latest, save_profile

    if args.host_action == "caps":
        from .desktop import capability_report, session_info

        info = session_info()
        print(f"\n会话：{info['session_type']}　DISPLAY={info['display'] or '—'}　"
              f"WAYLAND={info['wayland'] or '—'}　桌面={info['desktop'] or '—'}")
        print("控制能力：")
        print(capability_report())
        print()
        return

    conn = _open()

    if args.host_action == "show":
        latest = load_latest(conn)
        if not latest:
            print("还没有宿主画像。运行：lifeplan host scan")
            return
        parsed = latest.get("parsed", {})
        print(f"\n宿主画像（{latest['scanned_at']}）")
        print("=" * 60)
        print(f"  {latest['summary']}")
        print(f"  推断置信度 {latest['confidence']:.2f}（启发式比例，不是概率）")
        print("\n  假设排序：")
        for h in parsed.get("hypotheses", []):
            print(f"    · {h['label']}　得分 {h['score']}")
            for ev in h.get("evidence", [])[:3]:
                print(f"        ← {ev}")
        notes = parsed.get("notes", [])
        if notes:
            print("\n  说明：")
            for n in notes:
                print(f"    - {n}")
        print()
        return

    # scan
    roots = [Path(args.root).expanduser()] if args.root else None
    profile = build_profile(roots=roots, include_history=args.include_history)
    pid = save_profile(conn, profile)

    if args.json:
        print(profile.to_json())
        return

    print(f"\n✓ 扫描完成（画像 #{pid}，{len(profile.signals)} 条信号）\n")
    print(f"  {profile.summary}")
    print(f"  推断置信度 {profile.confidence:.2f}\n")
    print("  假设排序（每条都带证据）：")
    for h in profile.hypotheses[:4]:
        print(f"    · {h.label}　得分 {h.score}")
        for ev in h.evidence[:3]:
            print(f"        ← {ev}")
    if profile.notes:
        print("\n  说明：")
        for n in profile.notes:
            print(f"    - {n}")
    print()


# ── 执行层 ──────────────────────────────────────────────────
def cmd_term(args) -> None:
    from .terminal import list_actions, run, stats

    conn = _open()

    if args.term_action == "log":
        items = list_actions(conn, limit=args.limit, status=args.status)
        if not items:
            print("暂无执行记录。")
            return
        print()
        for a in items:
            mark = {"done": "✓", "failed": "✗", "blocked": "⏸", "denied": "🚫", "proposed": "·"}.get(
                a["status"], "?"
            )
            print(f"  {mark} #{a['id']:<4} tier{a['tier']}  {a['status']:<8} {a['command'][:70]}")
            if a["rationale"]:
                print(f"        {a['rationale'][:100]}")
        s = stats(conn)
        print(f"\n  统计：{'　'.join(f'{k}={v}' for k, v in sorted(s.items()))}\n")
        return

    # run
    argv = [a for a in args.argv if a != "--"]
    if not argv:
        _err("缺少命令。用法：lifeplan term run -- ls -la")

    res = run(
        argv,
        cwd=args.cwd,
        timeout=args.timeout,
        token=args.confirm_token,
        rationale=args.rationale or "",
        conn=conn,
    )

    if res.status == "blocked":
        print(f"\n⏸ 已暂缓（{res.reason}）\n")
        print(f"  确认执行请回填令牌：")
        print(f"    lifeplan act run {res.action_id} --confirm-token {res.token}")
        print(f"  或直接：lifeplan term run --confirm-token {res.token} -- {argv[0]} …")
        print()
        return
    if res.status == "denied":
        print(f"\n🚫 已拒绝：{res.reason}\n")
        return

    head = "✓" if res.ok else "✗"
    print(f"\n{head} tier{res.tier}｜{res.reason}｜{res.duration_ms} ms｜退出码 {res.returncode}")
    if res.stdout:
        print(res.stdout.rstrip())
    if res.stderr:
        print("--- stderr ---")
        print(res.stderr.rstrip())
    print()


def cmd_apps(args) -> None:
    from . import desktop

    conn = _open()

    if args.apps_action == "list":
        apps = desktop.list_apps()
        if args.filter:
            q = args.filter.lower()
            apps = [a for a in apps if q in a.name.lower()]
        print(f"\n可用应用 {len(apps)} 个：")
        for a in apps[: args.limit]:
            print(f"  · {a.name:<28} {a.argv[0] if a.argv else a.exec_line[:40]}")
        if len(apps) > args.limit:
            print(f"  … 其余 {len(apps) - args.limit} 个用 --limit 调整")
        print()
        return

    if args.apps_action == "windows":
        wins, note = desktop.list_windows()
        print(f"\n{note}")
        for w in wins:
            print(f"  {w['id']}  {w['title'][:70]}")
        print()
        return

    if args.apps_action == "launch":
        res, note = desktop.launch(args.name, token=args.confirm_token, conn=conn)
        print(f"\n{note}")
        if res is None:
            print()
            return
        if res.status == "blocked":
            print(f"⏸ 启动应用属于 tier2（有外部影响），需确认：")
            print(f"   lifeplan act run {res.action_id} --confirm-token {res.token}")
        elif res.ok:
            print("✓ 已启动")
        else:
            print(f"✗ {res.stderr.strip()[:200] or res.status}")
        print()
        return

    # type / key / focus
    if args.apps_action == "type":
        res, note = desktop.type_text(args.text, token=args.confirm_token, conn=conn)
    elif args.apps_action == "key":
        res, note = desktop.send_key(args.combo, token=args.confirm_token, conn=conn)
    else:
        res, note = desktop.focus_window(args.window_id, token=args.confirm_token, conn=conn)

    print(f"\n{note}")
    if res is not None and res.status == "blocked":
        print(f"需确认：lifeplan act run {res.action_id} --confirm-token {res.token}")
    print()


# ── 自主循环 ────────────────────────────────────────────────
def cmd_act(args) -> None:
    from . import autopilot
    from .terminal import list_actions, run

    conn = _open()

    if args.act_action == "tick":
        roots = [Path(args.root).expanduser()] if args.root else None
        out = autopilot.tick(conn, include_history=args.include_history, roots=roots)
        print(f"\n✓ 自主循环完成（画像 #{out['profile_id']}）\n")
        print(f"  {out['summary']}")
        print(f"  置信度 {out['confidence']:.2f}\n")
        if out["n_proposals"]:
            print(f"  生成 {out['n_proposals']} 条提议（均未执行）：")
            for p in out["proposals"]:
                print(f"    #{p['id']:<4} [tier{p['tier']}] {p['rationale'][:90]}")
        else:
            print("  没有需要处理的事项。")
        print()
        return

    if args.act_action == "list":
        items = list_actions(conn, limit=args.limit, status="proposed")
        if not items:
            print("没有待处理提议。运行 lifeplan act tick 生成。")
            return
        print()
        for a in items:
            print(f"  #{a['id']:<4} [{a['kind']}] tier{a['tier']}  {a['command'][:64]}")
            print(f"        {a['rationale'][:110]}")
        print()
        return

    # run <id>
    row = conn.execute("SELECT * FROM actions WHERE id=?", (args.id,)).fetchone()
    if not row:
        _err(f"提议 #{args.id} 不存在")
    item = dict(row)

    if item["kind"] in ("observe", "resolve", "review", "goal"):
        # 这类提议需要人来填数值/做判断，系统不代为决定
        print(f"\n这类提议需要你补数据，系统不代为决定：\n")
        print(f"  {item['rationale']}\n")
        print(f"  建议命令：{item['command']}\n")
        return

    argv = json.loads(item["argv_json"] or "[]")
    if not argv:
        # 兼容旧记录：用 shlex 还原（仅当能安全拆分时）
        import shlex

        try:
            argv = shlex.split(item["command"])
        except ValueError:
            _err("无法还原该提案的命令，请从其 rationale 手工处理")

    res = run(
        argv,
        cwd=item["target"] or None,
        token=args.confirm_token,
        rationale=item["rationale"],
        conn=conn,
        reuse_action_id=args.id,
    )
    if res.status == "blocked":
        print(f"\n⏸ 需确认：lifeplan act run {res.action_id} --confirm-token {res.token}\n")
        return
    if res.status == "denied":
        print(f"\n🚫 已拒绝：{res.reason}\n")
        return
    head = "✓" if res.ok else "✗"
    print(f"\n{head} #{res.action_id}｜tier{res.tier}｜{res.status}｜退出码 {res.returncode}")
    if res.stdout:
        print(res.stdout.rstrip()[:4000])
    if res.stderr:
        print("--- stderr ---")
        print(res.stderr.rstrip()[:2000])
    print()


# ── 每周复盘 / 备份 ─────────────────────────────────────────
def cmd_week(args) -> None:
    from .weekly import build_week, render_plan, run_week

    conn = _open()

    if args.plan_only:
        plan = build_week(conn, args.as_of, args.limit)
        print()
        print(render_plan(plan))
        print()
        return

    print(f"\n每周复盘　（可随时 Ctrl-C 或输入 s 跳过；共最多 {args.limit} 项）")
    print("=" * 58)
    result = run_week(
        conn,
        as_of=args.as_of,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print()


def cmd_backup(args) -> None:
    """用 SQLite 在线备份 API，避免直接拷文件遇到 WAL 不一致。"""
    from .db import db_path

    src = _open()
    out = Path(args.out).expanduser() if args.out else (
        Path.home() / f"lifeplan-backup-{date.today().isoformat()}.db"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    dst = sqlite3.connect(out)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()

    # 立即校验：表数量 + 关键表行数
    check = sqlite3.connect(out)
    try:
        tables = [r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        counts = {t: check.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("observations", "predictions", "goals", "metrics", "actions")
                  if t in tables}
    finally:
        check.close()

    print(f"\n✓ 已备份到 {out}（{out.stat().st_size / 1024:.0f} KB）")
    print(f"  表 {len(tables)} 个；关键行数：{counts}")

    # 记一笔，doctor 会读它提醒你是否该备份了
    from .db import set_meta, utc_now

    set_meta(src, "last_backup_at", utc_now())
    set_meta(src, "last_backup_path", str(out))
    print(f"  恢复命令：lifeplan restore --from {out} --force\n")


def cmd_restore(args) -> None:
    from .db import db_path

    src = Path(args.src).expanduser()
    if not src.exists():
        _err(f"备份文件不存在：{src}")
    if not args.force:
        _err("恢复会覆盖当前数据库。确认无误后加 --force 重试。")

    # 先校验备份文件本身是否可用
    probe = sqlite3.connect(src)
    try:
        tables = {r[0] for r in probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        need = {"domains", "goals", "metrics", "observations"}
        if not need <= tables:
            _err(f"备份文件缺少必要表：{sorted(need - tables)}")
        n_obs = probe.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    finally:
        probe.close()

    target = db_path()
    safety = target.with_suffix(f".pre-restore-{datetime.now():%Y%m%d-%H%M%S}.db")
    if target.exists():
        import shutil

        shutil.copy2(target, safety)

    import shutil

    shutil.copy2(src, target)
    print(f"\n✓ 已从 {src} 恢复（观测 {n_obs} 条）")
    if safety.exists():
        print(f"  恢复前的库已自动另存：{safety}")
    print()


def cmd_doctor(args) -> None:
    """一次性把系统健康状况摊开：库、版本、数据量、能力、备份。"""
    from . import desktop
    from .db import SCHEMA_VERSION, db_path, get_meta, schema_version
    from .policy import load_policy, policy_path

    conn = _open()
    print(f"\n数据库　{db_path()}")
    size = db_path().stat().st_size / 1024 if db_path().exists() else 0
    print(f"  大小 {size:.0f} KB　|　schema 版本 {schema_version(conn)}"
          f"（当前 {SCHEMA_VERSION}）{' ✓' if schema_version(conn) == SCHEMA_VERSION else ' ⚠ 需迁移'}")

    print("\n数据量：")
    for t in ("domains", "goals", "metrics", "observations", "predictions",
              "unquantifiable", "budget", "actions", "host_profile"):
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            n = -1
        print(f"  {t:<16} {n if n >= 0 else '缺失'}")

    real_obs = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE evidence_ref != 'week-review'"
    ).fetchone()[0]
    week_obs = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE evidence_ref = 'week-review'"
    ).fetchone()[0]
    seeded = get_meta(conn, "seeded_at", "")
    print(f"  观测来源：非周复盘 {real_obs} 条，周复盘补录 {week_obs} 条")
    if seeded:
        print(f"  ⚠ 本库含种子数据（{seeded} 导入），上述计数包含演示记录，不是真实使用量")

    print("\n校准：")
    try:
        from .calibration import compute_calibration, load_predictions

        cal = compute_calibration(load_predictions(conn))
        print(f"  已结算 {cal.n_resolved} / 待结算 {cal.n_open}"
              f"{'　' + cal.verdict if cal.n_resolved else ''}")
    except Exception as exc:
        print(f"  计算失败：{exc}")

    print("\n权限策略：")
    pol = load_policy()
    print(f"  {policy_path()}")
    print(f"  allow_execute={pol.allow_execute}　allow_destructive={pol.allow_destructive}"
          f"　allow_shell_wrapper={pol.allow_shell_wrapper}")

    print("\n桌面控制能力：")
    for line in desktop.capability_report().splitlines():
        print(line if line.startswith("  ") else f"  {line}")

    print("\n备份：")
    print(f"  最后备份时间：{get_meta(conn, 'last_backup_at', '从未备份 ⚠')}")
    print(f"  建议：lifeplan backup --out ~/lifeplan-backup-{date.today().isoformat()}.db")

    print("\nAgent：")
    try:
        from .agent import pick_adapter

        chosen, skipped = pick_adapter()
        print(f"  当前驱动 {chosen.name}（跳过 {len(skipped)} 个）")
    except Exception as exc:
        print(f"  检查失败：{exc}")
    print()


# ── Q-One：万物量化协议 ─────────────────────────────────────
def cmd_qone(args) -> None:
    from .ledger import list_recent, verify_integrity
    from .qone import ask

    conn = _open()

    if args.qone_action == "verify":
        rep = verify_integrity(conn)
        print(f"\n台账校验：{rep['checked']} 条记录，"
              f"{'全部通过' if rep['ok'] else '发现篡改：' + str(rep['tampered'])}\n")
        return

    if args.qone_action == "list":
        items = list_recent(conn, limit=args.limit)
        if not items:
            print("\n台账为空。\n")
            return
        print(f"\n最近 {len(items)} 条量化决策：\n")
        for d in items:
            a = d.get("answer", {})
            print(f"  #{d['id']} {d['ts'][:16]}　action={a.get('action')}　"
                  f"confidence={a.get('confidence')}　by={a.get('decided_by')}")
            print(f"      state：{d['state'][:60]}")
            if a.get("unknowns"):
                print(f"      未知项：{a['unknowns'][0][:50]}")
        print()
        return

    # ask
    state = " ".join(args.state).strip()
    if not state:
        _err("请提供待量化的问题，例如：lifeplan qone ask 这个项目是否值得继续投入")
    ans = ask(state, conn, root=args.root)
    contract = ans.to_contract()

    if args.json:
        print(json.dumps(contract, ensure_ascii=False, indent=2))
        return

    print(f"\n量化决策　{state}")
    print("=" * 60)
    for qid, v in contract.items():
        if isinstance(v, dict) and "value" in v:
            interval = v.get("interval")
            it = f"　区间 [{interval[0]}, {interval[1]}]" if interval else ""
            print(f"  {qid:<8} {v['value']} {v['unit']}{it}")
        elif isinstance(v, dict) and "level" in v:
            print(f"  {qid:<8} {v['level']}（{v['rule'][:40]}）")
    print(f"\n  裁决 action      {contract['action']}")
    print(f"  置信度           {contract['confidence']}（裁决者 {contract['decided_by']}）")
    print(f"\n  证据（{len(contract['evidence'])} 项，均含 sha256）：")
    for e in contract["evidence"]:
        print(f"    · [{e['kind']}] {e['detail']} = {e['value']}")
    if contract["assumptions"]:
        print("\n  假设：")
        for a in contract["assumptions"]:
            print(f"    · {a}")
    if contract["unknowns"]:
        print("\n  未知项：")
        for u in contract["unknowns"]:
            print(f"    · {u}")
    print(f"\n  下一步测量：{contract['next_measurement']}")
    print("\n  （已写入溯源账本：lifeplan qone list / verify）\n")


# ── 首次引导 ────────────────────────────────────────────────
def cmd_setup(args) -> None:
    from .setup import apply_setup, generate_template, load, setup_path, validate

    conn = _open()
    path = setup_path()

    if args.setup_action == "init":
        if path.exists() and not args.force:
            _err(f"{path} 已存在。加 --force 重新生成（会覆盖你填的内容）")
        text = generate_template(conn)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"\n✓ 已生成引导模板：{path}")
        print("  编辑它（baseline/target/p 必须你填），然后：lifeplan setup check")
        print("  规则：系统只提案；数值全部归你——这是 0.725 教训的制度化。\n")
        return

    if not path.exists():
        _err(f"{path} 不存在。先 lifeplan setup --init")

    doc = load(path)
    only = getattr(args, "only", None)
    scope = f"（只看目标 #{only}）" if only is not None else ""

    if args.setup_action == "check":
        errors = validate(doc, only=only)
        n_m = len([m for m in (doc.get("metrics") or [])
                   if only is None or m.get("goal_id") == only])
        n_p = len([p for p in (doc.get("predictions") or [])
                   if only is None or p.get("goal_id") == only])
        print(f"\n校验 {path}{scope}：{n_m} 个指标，{n_p} 条预测")
        if errors:
            print("  ✗ 还不能应用：")
            for e in errors:
                print(f"    · {e}")
            print()
            raise SystemExit(1)
        print("  ✓ 可以应用：lifeplan setup apply\n")
        return

    # apply
    errors = validate(doc, only=only)
    if errors:
        print("\n✗ 拒绝应用（校验未过）：")
        for e in errors:
            print(f"    · {e}")
        print()
        raise SystemExit(1)

    stat = apply_setup(conn, doc, only=only)
    print(f"\n✓ 已写入：{stat['metrics']} 个指标，{stat['predictions']} 条预测")
    print("  基线已按目标开始日期冻结——从现在起改它们就是「基线追认」，审计会标红。")

    from .calibration import compute_calibration, load_predictions

    cal = compute_calibration(load_predictions(conn))
    print(f"\n  当前预测台账：{cal.n_open} 条未结算（到期后 lifeplan resolve 结算）")
    print("  下一步：lifeplan week（周一见）｜lifeplan qone ask \"…\"（随时问）\n")


def cmd_policy(args) -> None:
    from .policy import TIER_LABEL, load_policy, policy_path

    pol = load_policy()
    print(f"\n策略文件：{policy_path()}\n")
    print(json.dumps(pol.to_dict(), ensure_ascii=False, indent=2))
    print("\n分级含义：")
    for tier, label in TIER_LABEL.items():
        print(f"  tier{tier}  {label}")
    print("\n改策略请直接编辑上面的 JSON 文件；改完再跑一次本命令确认。\n")


# ── 专属 agent ──────────────────────────────────────────────
def cmd_agent(args) -> None:
    from .agent import answer, available_adapters, tool_catalog
    from .policy import TIER_LABEL

    if args.agent_action == "tools":
        items = tool_catalog()
        print(f"\n可用工具 {len(items)} 个：\n")
        for t in items:
            print(f"  {t['name']:<20} tier{t['tier']}  {t['tier_label']}")
            print(f"      {t['summary']}")
            if t["params"]:
                for k, v in t["params"].items():
                    print(f"        - {k}: {v}")
        print()
        return

    if args.agent_action == "doctor":
        print("\n适配器状态（按优先级）：\n")
        for ad in available_adapters():
            print("  " + ad.describe())
        from .agent.adapters import JevLocalAdapter

        jev = JevLocalAdapter()
        print(f"\nJev 检查点查找路径：{jev.ckpt}")
        print("\n说明：")
        print("  - jev-local 需要训练好的检查点；未训练时探针损失会超阈值，自动跳过")
        print("  - openai-compat 需要环境变量 LIFEPLAN_LLM_BASE_URL / LIFEPLAN_LLM_MODEL"
              "（可选 LIFEPLAN_LLM_API_KEY）")
        print("  - scripted 为兜底，永远可用，但只做关键词路由，不做推理")
        print("\n当前会使用：", end="")
        from .agent import pick_adapter

        chosen, skipped = pick_adapter()
        print(chosen.name + (f"（跳过 {len(skipped)} 个）" if skipped else ""))
        print()
        return

    # ask
    question = " ".join(args.question).strip()
    if not question:
        _err("请提供问题，例如：lifeplan agent ask 现在状态怎么样")

    conn = _open()
    ans = answer(
        question,
        conn,
        allow_write=args.write,
        as_of=args.as_of,
        max_steps=args.max_steps,
        preferred=args.adapter,
        cwd=str(Path.cwd()),
    )
    print()
    print(ans.render(verbose=args.verbose))
    if ans.notes and args.verbose:
        print("\n过程备注：")
        for n in ans.notes:
            print(f"  · {n}")
    print()



def cmd_budget(args) -> None:
    conn = _open()
    week = args.week or _current_week()
    if args.planned is not None or args.actual is not None:
        conn.execute(
            """INSERT INTO budget(domain_id, week_start, planned_hours, actual_hours)
               VALUES (?,?,?,?)
               ON CONFLICT(domain_id, week_start) DO UPDATE SET
                 planned_hours=excluded.planned_hours,
                 actual_hours=excluded.actual_hours""",
            (args.domain, week, args.planned or 0, args.actual or 0),
        )
        conn.commit()
        print(f"✓ 已记录 {args.domain} 领域 {week} 周预算")
        return
    items = rows(conn, """
        SELECT b.*, d.name AS domain_name FROM budget b
        JOIN domains d ON d.id=b.domain_id
        WHERE b.week_start=(SELECT MAX(week_start) FROM budget)
        ORDER BY d.sort_order""")
    print()
    for b in items:
        diff = b["actual_hours"] - b["planned_hours"]
        flag = "超载" if diff > b["planned_hours"] * 0.15 else ("不足" if diff < -b["planned_hours"] * 0.3 else "正常")
        print(f"  {b['domain_name']:<10} 计划 {b['planned_hours']:g}h　实际 {b['actual_hours']:g}h　"
              f"差 {diff:+g}h　{flag}")
    print()


def _current_week() -> str:
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


# ── 参数解析 ───────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lifeplan",
        description="人生规划系统：把目标变成可核验指标，把判断变成可校准预测。",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="初始化数据库")
    sp.add_argument("--seed", action="store_true", help="写入贴近真实的种子数据")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("goal", help="目标管理")
    gsub = sp.add_subparsers(dest="goal_command", required=True)
    g1 = gsub.add_parser("add", help="新增目标")
    g1.add_argument("--domain", type=int, required=True)
    g1.add_argument("--title", required=True)
    g1.add_argument("--desc", default="")
    g1.add_argument("--start", default=date.today().isoformat())
    g1.add_argument("--end", default=None)
    g1.add_argument("--weight", type=float, default=1.0)
    g1.add_argument("--as-of", dest="as_of", default=None)
    g1.set_defaults(func=cmd_goal_add)
    g2 = gsub.add_parser("list", help="列出目标与指标状态")
    g2.add_argument("--as-of", dest="as_of", default=None)
    g2.set_defaults(func=cmd_goal_list)

    sp = sub.add_parser("metric", help="指标管理")
    msub = sp.add_subparsers(dest="metric_command", required=True)
    m1 = msub.add_parser("add", help="新增指标")
    m1.add_argument("--goal", type=int, required=True)
    m1.add_argument("--name", required=True)
    m1.add_argument("--unit", default="")
    m1.add_argument("--direction", choices=["increase", "decrease", "maintain"], default="increase")
    m1.add_argument("--baseline", type=float, required=True)
    m1.add_argument("--target", type=float, required=True)
    m1.add_argument("--tolerance", type=float, default=0.0)
    m1.add_argument("--weight", type=float, default=1.0)
    m1.add_argument("--cadence", choices=["daily", "weekly", "monthly", "quarterly", "yearly"], default="weekly")
    m1.add_argument("--tier", type=int, choices=[0, 1, 2, 3], default=2)
    m1.add_argument("--proxy", action="store_true", help="标记为代理指标")
    m1.add_argument("--proxy-for", default="")
    m1.set_defaults(func=cmd_metric_add)

    sp = sub.add_parser("observe", help="记录一次观测")
    sp.add_argument("--metric", type=int, required=True)
    sp.add_argument("--value", type=float, required=True)
    sp.add_argument("--date", default=date.today().isoformat())
    sp.add_argument("--note", default="")
    sp.add_argument("--evidence", default="", help="证据引用：URL / 文件路径 / 提交号")
    sp.add_argument("--tier", type=int, choices=[0, 1, 2, 3], default=None)
    sp.set_defaults(func=cmd_observe)

    sp = sub.add_parser("predict", help="登记一条事前概率预测")
    sp.add_argument("--goal", type=int, required=True)
    sp.add_argument("--horizon", required=True, help="到期日 YYYY-MM-DD")
    sp.add_argument("--p", type=float, required=True, help="成功概率 0..1")
    sp.add_argument("--statement", default="")
    sp.set_defaults(func=cmd_predict)

    sp = sub.add_parser("resolve", help="结算一条预测")
    sp.add_argument("id", type=int)
    sp.add_argument("--hit", type=int, required=True, choices=[0, 1])
    sp.add_argument("--evidence", default=None, help="结算依据：URL/文件路径/可查证来源")
    sp.add_argument("--note", default=None, help="结算备注：为什么判这个结果")
    sp.set_defaults(func=cmd_resolve)

    sp = sub.add_parser("due", help="列出到期未结算的预测")
    sp.set_defaults(func=cmd_due)

    sp = sub.add_parser("score", help="查看当前状态")
    sp.add_argument("--as-of", dest="as_of", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_score)

    sp = sub.add_parser("calibrate", help="自我预测校准诊断")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("report", help="生成周报")
    sp.add_argument("--as-of", dest="as_of", default=None)
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("dashboard", help="生成单文件 HTML 看板")
    sp.add_argument("--as-of", dest="as_of", default=None)
    sp.add_argument("--out", default="lifeplan.html")
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("export", help="导出全部数据为 JSON")
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("unquantifiable", help="不可量化事项登记")
    usub = sp.add_subparsers(dest="action", required=True)
    u1 = usub.add_parser("add", help="登记一项")
    u1.add_argument("--name", required=True)
    u1.add_argument("--reason", required=True)
    u1.add_argument("--proxy-metric", type=int, default=None)
    u1.add_argument("--status", choices=["open", "proxied", "retired"], default="open")
    u2 = usub.add_parser("list", help="列出登记项")
    u2.set_defaults(func=cmd_unquantifiable)

    sp = sub.add_parser("budget", help="时间预算（承诺 vs 产能）")
    bsub = sp.add_subparsers(dest="action", required=True)
    b1 = bsub.add_parser("set", help="设置某领域本周工时")
    b1.add_argument("--domain", type=int, required=True)
    b1.add_argument("--week", default=None)
    b1.add_argument("--planned", type=float, default=None)
    b1.add_argument("--actual", type=float, default=None)
    b2 = bsub.add_parser("list", help="查看本周预算")
    b2.set_defaults(func=cmd_budget)

    sp = sub.add_parser("audit", help="自我审计：基线追认 / 代理反噬 / 证据通胀")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("events", help="审计事件流")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_events)

    # ── 宿主感知 ──
    sp = sub.add_parser("host", help="宿主感知：这台机器的主人在干什么")
    hsub = sp.add_subparsers(dest="host_action", required=True)
    h1 = hsub.add_parser("scan", help="扫描并生成宿主画像")
    h1.add_argument("--root", default=None, help="扫描根目录，默认家目录")
    h1.add_argument("--include-history", action="store_true", help="纳入 shell 历史（敏感行自动打码）")
    h1.add_argument("--json", action="store_true")
    h1.set_defaults(func=cmd_host)
    h2 = hsub.add_parser("show", help="查看最近一次画像")
    h2.set_defaults(func=cmd_host)
    h3 = hsub.add_parser("caps", help="本机实际可用的控制能力")
    h3.set_defaults(func=cmd_host)

    # ── 终端执行 ──
    sp = sub.add_parser("term", help="终端执行（全部分级 + 审计）")
    tsub = sp.add_subparsers(dest="term_action", required=True)
    t1 = tsub.add_parser("run", help="执行命令，用法：term run -- ls -la")
    t1.add_argument("--cwd", default=None)
    t1.add_argument("--timeout", type=int, default=None)
    t1.add_argument("--confirm-token", default=None)
    t1.add_argument("--rationale", default="")
    t1.add_argument("argv", nargs=argparse.REMAINDER)
    t1.set_defaults(func=cmd_term)
    t2 = tsub.add_parser("log", help="查看执行记录")
    t2.add_argument("--limit", type=int, default=20)
    t2.add_argument("--status", default=None)
    t2.set_defaults(func=cmd_term)

    # ── 应用控制 ──
    sp = sub.add_parser("apps", help="应用控制：启动应用、枚举窗口、模拟输入")
    asub = sp.add_subparsers(dest="apps_action", required=True)
    a1 = asub.add_parser("list", help="枚举已安装应用")
    a1.add_argument("--filter", default=None)
    a1.add_argument("--limit", type=int, default=60)
    a1.set_defaults(func=cmd_apps)
    a2 = asub.add_parser("launch", help="启动应用（tier2，需确认）")
    a2.add_argument("name")
    a2.add_argument("--confirm-token", default=None)
    a2.set_defaults(func=cmd_apps)
    a3 = asub.add_parser("windows", help="枚举窗口")
    a3.set_defaults(func=cmd_apps)
    a4 = asub.add_parser("type", help="模拟键盘输入")
    a4.add_argument("text")
    a4.add_argument("--confirm-token", default=None)
    a4.set_defaults(func=cmd_apps)
    a5 = asub.add_parser("key", help="发送组合键，如 ctrl+s")
    a5.add_argument("combo")
    a5.add_argument("--confirm-token", default=None)
    a5.set_defaults(func=cmd_apps)
    a6 = asub.add_parser("focus", help="聚焦窗口")
    a6.add_argument("window_id")
    a6.add_argument("--confirm-token", default=None)
    a6.set_defaults(func=cmd_apps)

    # ── 自主循环 ──
    sp = sub.add_parser("act", help="自主循环：感知 → 提议 → 确认后执行")
    csub = sp.add_subparsers(dest="act_action", required=True)
    c1 = csub.add_parser("tick", help="跑一轮感知与提议（不执行副作用）")
    c1.add_argument("--include-history", action="store_true")
    c1.add_argument("--root", default=None)
    c1.set_defaults(func=cmd_act)
    c2 = csub.add_parser("list", help="查看待处理提议")
    c2.add_argument("--limit", type=int, default=20)
    c2.set_defaults(func=cmd_act)
    c3 = csub.add_parser("run", help="确认并执行某条提议")
    c3.add_argument("id", type=int)
    c3.add_argument("--confirm-token", default=None)
    c3.set_defaults(func=cmd_act)

    # ── 首次引导 ──
    sp = sub.add_parser("setup", help="首次引导：生成提案模板 → 你填数值 → 一条命令应用")
    gs = sp.add_subparsers(dest="setup_action", required=True)
    s1 = gs.add_parser("init", help="生成 ~/.lifeplan/setup.yaml（提案，数值留空）")
    s1.add_argument("--force", action="store_true", help="覆盖已有文件")
    s1.set_defaults(func=cmd_setup)
    s2 = gs.add_parser("check", help="只校验，不写入")
    s2.add_argument("--only", type=int, default=None,
                    help="只校验某个目标（goal id）；其余目标没填的数值不阻塞")
    s2.set_defaults(func=cmd_setup)
    s3 = gs.add_parser("apply", help="校验并写入指标与预测")
    s3.add_argument("--only", type=int, default=None,
                    help="只写入某个目标（goal id）；与 check --only 配合，逐目标推进")
    s3.set_defaults(func=cmd_setup)

    # ── Q-One ──
    sp = sub.add_parser("qone", help="Q-One：万物量化协议（测量→证据→计算→裁决→台账）")
    gq = sp.add_subparsers(dest="qone_action", required=True)
    q1 = gq.add_parser("ask", help="对一个 state 做完整量化决策")
    q1.add_argument("state", nargs="+")
    q1.add_argument("--root", default=None, help="证据扫描根目录（默认家目录）")
    q1.add_argument("--json", action="store_true")
    q1.set_defaults(func=cmd_qone)
    q2 = gq.add_parser("list", help="查看量化决策台账")
    q2.add_argument("--limit", type=int, default=10)
    q2.set_defaults(func=cmd_qone)
    q3 = gq.add_parser("verify", help="校验台账完整性（重算哈希）")
    q3.set_defaults(func=cmd_qone)

    # ── 策略 ──
    sp = sub.add_parser("policy", help="查看权限策略与分级含义")
    sp.set_defaults(func=cmd_policy)

    sp = sub.add_parser("doctor", help="系统体检：库/版本/数据量/能力/备份")
    sp.set_defaults(func=cmd_doctor)

    # ── 每周复盘 ──
    sp = sub.add_parser("week", help="每周复盘：一次处理完到期项（推荐入口）")
    sp.add_argument("--as-of", dest="as_of", default=None)
    sp.add_argument("--limit", type=int, default=12, help="本次最多处理几项（默认 12）")
    sp.add_argument("--plan-only", action="store_true", help="只列清单，不交互")
    sp.add_argument("--dry-run", action="store_true", help="演练：不写入")
    sp.set_defaults(func=cmd_week)

    # ── 备份与恢复 ──
    sp = sub.add_parser("backup", help="备份数据库（在线备份 + 立即校验）")
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore", help="从备份恢复（会覆盖当前库）")
    sp.add_argument("--from", dest="src", required=True)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_restore)

    # ── 专属 agent ──
    sp = sub.add_parser("agent", help="专属 agent：用工具回答问题，不编数字")
    gsub = sp.add_subparsers(dest="agent_action", required=True)
    g1 = gsub.add_parser("ask", help="向 agent 提问")
    g1.add_argument("question", nargs="+")
    g1.add_argument("--write", action="store_true", help="允许 tier1 写入（记录观测/登记预测）")
    g1.add_argument("--as-of", dest="as_of", default=None)
    g1.add_argument("--max-steps", dest="max_steps", type=int, default=4)
    g1.add_argument("--adapter", default=None,
                    choices=["jev-local", "openai-compat", "scripted"])
    g1.add_argument("--verbose", action="store_true")
    g1.set_defaults(func=cmd_agent)
    g2 = gsub.add_parser("tools", help="列出可用工具与参数")
    g2.set_defaults(func=cmd_agent)
    g3 = gsub.add_parser("doctor", help="检查适配器与检查点状态")
    g3.set_defaults(func=cmd_agent)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "init":
        _ensure_db()
    args.func(args)


if __name__ == "__main__":
    main()

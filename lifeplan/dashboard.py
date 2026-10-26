"""单文件 HTML 看板生成器。

约束：零外部依赖（无 CDN、无 JS 框架），离线双击可开，图表用原生 SVG 手绘。
这样看板可以随报告一起进版本库、随邮件附件发出去，不会因为某个 CDN 挂了就失效。
"""

from __future__ import annotations

import html
import json
from datetime import date

from .calibration import compute_calibration, load_predictions
from .db import get_meta, rows
from .evidence import tier_label
from .metrics import build_snapshot

# 配色：浅色为主，同时用 prefers-color-scheme 支持深色
PALETTE = {
    "bg": "#f8f9fb",
    "card": "#ffffff",
    "border": "#e4e7ec",
    "text": "#1a1d23",
    "muted": "#6b7280",
    "accent": "#4c6ef5",
    "accent2": "#7048e8",
    "good": "#12b886",
    "warn": "#f59f00",
    "bad": "#e03131",
    "proxy": "#f76707",
}


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _num(x, fmt: str = "{:.4f}", dash: str = "—") -> str:
    """数值格式化；None 显示为破折号。

    没有已结算预测时 brier/ece 都是 None，直接格式化会抛异常，
    整个看板就白屏了。这个坑在无预测的新库上一定会踩到。
    """
    return dash if x is None else fmt.format(x)


def _signed(x, fmt: str = "{:+.3f}", dash: str = "—") -> str:
    return dash if x is None else fmt.format(x)


def _svg_donut(value: float, confidence: float, size: int = 132) -> str:
    """双环：外环=价值分，内环=证据置信度。"""
    r_out = size / 2 - 10
    r_in = r_out - 14
    c = size / 2
    circ_out = 2 * 3.141592653589793 * r_out
    circ_in = 2 * 3.141592653589793 * r_in
    v = max(0.0, min(1.0, value))
    cf = max(0.0, min(1.0, confidence))
    return f"""
    <svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" role="img" aria-label="价值分与置信度">
      <circle cx="{c}" cy="{c}" r="{r_out}" fill="none" stroke="{PALETTE['border']}" stroke-width="12"/>
      <circle cx="{c}" cy="{c}" r="{r_out}" fill="none" stroke="{PALETTE['accent']}" stroke-width="12"
              stroke-dasharray="{circ_out * v:.2f} {circ_out:.2f}" stroke-linecap="round"
              transform="rotate(-90 {c} {c})"/>
      <circle cx="{c}" cy="{c}" r="{r_in}" fill="none" stroke="{PALETTE['border']}" stroke-width="9"/>
      <circle cx="{c}" cy="{c}" r="{r_in}" fill="none" stroke="{PALETTE['good']}" stroke-width="9"
              stroke-dasharray="{circ_in * cf:.2f} {circ_in:.2f}" stroke-linecap="round"
              transform="rotate(-90 {c} {c})"/>
      <text x="{c}" y="{c - 4}" text-anchor="middle" font-size="24" font-weight="700"
            fill="{PALETTE['text']}">{value:.2f}</text>
      <text x="{c}" y="{c + 16}" text-anchor="middle" font-size="11" fill="{PALETTE['muted']}">价值分</text>
      <text x="{c}" y="{c + 32}" text-anchor="middle" font-size="11" fill="{PALETTE['good']}">置信 {confidence:.2f}</text>
    </svg>"""


def _svg_reliability(bins, width: int = 320, height: int = 240) -> str:
    """可靠曲线：预测概率 vs 实际命中率，贴近对角线即校准良好。"""
    pad_l, pad_b, pad_t, pad_r = 34, 34, 12, 12
    w = width - pad_l - pad_r
    h = height - pad_t - pad_b

    def px(p):
        return pad_l + p * w

    def py(a):
        return pad_t + h - a * h

    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="可靠性曲线">'
    ]
    # 网格
    for i in range(5):
        val = i / 4
        parts.append(
            f'<line x1="{px(val):.1f}" y1="{pad_t}" x2="{px(val):.1f}" y2="{pad_t+h}" '
            f'stroke="{PALETTE["border"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<line x1="{pad_l}" y1="{py(val):.1f}" x2="{pad_l+w}" y2="{py(val):.1f}" '
            f'stroke="{PALETTE["border"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{px(val):.1f}" y="{height-pad_b+16}" font-size="10" text-anchor="middle" '
            f'fill="{PALETTE["muted"]}">{val:.2f}</text>'
        )
        parts.append(
            f'<text x="{pad_l-6}" y="{py(val)+4:.1f}" font-size="10" text-anchor="end" '
            f'fill="{PALETTE["muted"]}">{val:.2f}</text>'
        )
    # 对角线
    parts.append(
        f'<line x1="{px(0):.1f}" y1="{py(0):.1f}" x2="{px(1):.1f}" y2="{py(1):.1f}" '
        f'stroke="{PALETTE["muted"]}" stroke-width="1.5" stroke-dasharray="4 3"/>'
    )
    # 数据点
    for b in bins:
        if b.count == 0 or b.mean_p is None:
            continue
        r = min(7, 3 + b.count * 1.1)
        parts.append(
            f'<circle cx="{px(b.mean_p):.1f}" cy="{py(b.accuracy):.1f}" r="{r:.1f}" '
            f'fill="{PALETTE["accent"]}" fill-opacity="0.85" stroke="#fff" stroke-width="1.5">'
            f'<title>预测 {b.mean_p:.2f} → 实际 {b.accuracy:.2f}（n={b.count}）</title></circle>'
        )
    parts.append(
        f'<text x="{pad_l+w/2}" y="{height-4}" font-size="10" text-anchor="middle" '
        f'fill="{PALETTE["muted"]}">事前预测概率</text>'
    )
    parts.append(
        f'<text x="10" y="{pad_t+h/2}" font-size="10" text-anchor="middle" fill="{PALETTE["muted"]}" '
        f'transform="rotate(-90 10 {pad_t+h/2})">实际命中率</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _svg_bars(items, width: int = 560) -> str:
    """分组条形图：价值分 vs 置信度。"""
    if not items:
        return '<p class="muted">无领域数据</p>'
    row_h = 40
    pad_l = 92
    height = len(items) * row_h + 16
    w = width - pad_l - 52
    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="领域对比">']
    for i, (name, value, conf) in enumerate(items):
        y = i * row_h + 8
        parts.append(
            f'<text x="{pad_l-8}" y="{y+13}" font-size="12" text-anchor="end" '
            f'fill="{PALETTE["text"]}">{_esc(name)}</text>'
        )
        vw = max(0.0, min(1.0, value)) * w
        cw = max(0.0, min(1.0, conf)) * w
        parts.append(
            f'<rect x="{pad_l}" y="{y+2}" width="{w:.1f}" height="10" rx="5" fill="{PALETTE["border"]}"/>'
        )
        parts.append(
            f'<rect x="{pad_l}" y="{y+2}" width="{vw:.1f}" height="10" rx="5" fill="{PALETTE["accent"]}"/>'
        )
        parts.append(
            f'<rect x="{pad_l}" y="{y+16}" width="{w:.1f}" height="6" rx="3" fill="{PALETTE["border"]}"/>'
        )
        parts.append(
            f'<rect x="{pad_l}" y="{y+16}" width="{cw:.1f}" height="6" rx="3" fill="{PALETTE["good"]}"/>'
        )
        parts.append(
            f'<text x="{pad_l+w+8}" y="{y+11}" font-size="11" fill="{PALETTE["muted"]}">{value:.2f}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def render_dashboard(conn, as_of: str | None = None) -> str:
    as_of = as_of or date.today().isoformat()
    snap = build_snapshot(conn, as_of)
    preds = load_predictions(conn)
    cal = compute_calibration(preds)
    seeded_at = get_meta(conn, "seeded_at", "")
    banner_html = ""
    if seeded_at:
        seed_label = seeded_at.replace("T", " ")[:16]
        banner_html = (
            '<div class="demo-banner" role="status">'
            "<strong>示例数据 · 虚构画像 · 只读</strong>"
            f"　本库带种子标记（{_esc(seed_label)} UTC），不是真实用户记录。"
            "</div>"
        )
    budget_rows = rows(conn, """
        SELECT b.*, d.name AS domain_name FROM budget b JOIN domains d ON d.id=b.domain_id
        WHERE b.week_start=(SELECT MAX(week_start) FROM budget) ORDER BY d.sort_order""")

    domain_bars = [(d.domain["name"], d.value, d.confidence) for d in snap.domains]

    # 目标卡片
    goal_cards = []
    for d in snap.domains:
        for g in d.goals:
            metric_rows = []
            for m in g.metrics:
                prog = m.progress if (m.has_data and m.progress is not None) else 0.0
                pct = prog * 100
                status = {
                    "done": ("已达成", PALETTE["good"]),
                    "active": ("推进中", PALETTE["accent"]),
                    "stale": ("数据过期", PALETTE["warn"]),
                    "no_data": ("无观测", PALETTE["muted"]),
                }.get(m.status, ("—", PALETTE["muted"]))
                proxy_tag = (
                    f'<span class="tag proxy">代理</span>' if m.metric.is_proxy else ""
                )
                cur = f"{m.value:g}{m.metric.unit}" if m.value is not None else "—"
                metric_rows.append(f"""
      <tr>
        <td>{_esc(m.metric.name)} {proxy_tag}</td>
        <td class="num">{_esc(cur)}</td>
        <td class="num muted">{m.metric.baseline:g} → {m.metric.target:g}</td>
        <td>
          <div class="bar"><span style="width:{pct:.1f}%;background:{status[1]}"></span></div>
          <span class="muted small">{'—' if not m.has_data else f'{pct:.0f}%'}</span>
        </td>
        <td><span class="tag">{_esc(tier_label(m.evidence_tier))}</span></td>
        <td><span style="color:{status[1]}">{status[0]}</span></td>
      </tr>""")
            goal_cards.append(f"""
    <section class="card goal" data-domain="{_esc(d.domain['name'])}">
      <div class="goal-head">
        <div>
          <h3>{_esc(g.goal['title'])}</h3>
          <p class="muted small">{_esc(g.goal['description'][:80])}</p>
        </div>
        <div class="goal-stats">
          <div><span class="big">{g.value:.2f}</span><span class="muted small">价值</span></div>
          <div><span class="big">{g.confidence:.2f}</span><span class="muted small">置信</span></div>
          <div><span class="big">{g.coverage*100:.0f}%</span><span class="muted small">覆盖</span></div>
        </div>
      </div>
      <table>
        <thead><tr><th>指标</th><th>当前</th><th>基线→目标</th><th>进展</th><th>证据</th><th>状态</th></tr></thead>
        <tbody>{''.join(metric_rows)}</tbody>
      </table>
    </section>""")

    # 校准块
    if cal.n_resolved:
        cal_rows = []
        for b in cal.bins:
            if b.count == 0 or b.mean_p is None:
                continue
            gap = b.accuracy - b.mean_p
            color = PALETTE["good"] if abs(gap) < 0.1 else (PALETTE["warn"] if abs(gap) < 0.25 else PALETTE["bad"])
            cal_rows.append(
                f'<tr><td class="num">{b.mean_p:.2f}</td><td class="num">{b.accuracy:.2f}</td>'
                f'<td class="num" style="color:{color}">{gap:+.2f}</td><td class="num">{b.count}</td></tr>'
            )
        cal_html = f"""
      <div class="cal-grid">
        <div>{_svg_reliability(cal.bins)}</div>
        <div>
          <div class="verdict">{_esc(cal.verdict)}</div>
          <table class="mini">
            <thead><tr><th>预测</th><th>实际</th><th>偏差</th><th>n</th></tr></thead>
            <tbody>{''.join(cal_rows) or '<tr><td colspan="4" class="muted">无数据</td></tr>'}</tbody>
          </table>
        </div>
      </div>"""
    else:
        cal_html = '<p class="muted">尚无已结算预测。登记预测并在到期后结算，即可得到可靠性曲线。</p>'

    # 预算块
    budget_html = ""
    if budget_rows:
        rows_html = []
        for b in budget_rows:
            diff = b["actual_hours"] - b["planned_hours"]
            flag = "超载" if diff > b["planned_hours"] * 0.15 else ("投入不足" if diff < -b["planned_hours"] * 0.3 else "正常")
            color = PALETTE["bad"] if flag == "超载" else (PALETTE["warn"] if flag == "投入不足" else PALETTE["good"])
            rows_html.append(
                f'<tr><td>{_esc(b["domain_name"])}</td><td class="num">{b["planned_hours"]:g}h</td>'
                f'<td class="num">{b["actual_hours"]:g}h</td>'
                f'<td class="num" style="color:{color}">{diff:+g}h · {flag}</td></tr>'
            )
        budget_html = f"""
    <section class="card">
      <h3>承诺与产能</h3>
      <p class="muted small">量化系统最常见的失败模式不是指标没达成，而是同时承诺太多、每项都推进不足。</p>
      <table class="mini"><thead><tr><th>领域</th><th>计划</th><th>实际</th><th>差额</th></tr></thead>
      <tbody>{''.join(rows_html)}</tbody></table>
    </section>"""

    # 不可量化块
    if snap.unquantifiable:
        uq = "".join(
            f'<li><strong>{_esc(u["name"])}</strong><span class="muted">{_esc(u["reason"])}</span></li>'
            for u in snap.unquantifiable
        )
        uq_html = f"""
    <section class="card uq">
      <h3>不可量化登记</h3>
      <p class="muted small">列出这些不是缺陷，而是系统的诚实边界——假装能测，反而会让系统给出错误的优化方向。</p>
      <ul class="uq-list">{uq}</ul>
    </section>"""
    else:
        uq_html = ""

    # 待结算
    open_preds = [p for p in preds if p.is_open]
    if open_preds:
        op = "".join(
            f'<tr><td class="num">#{p.id}</td><td>{_esc(p.statement or p.goal_title)}</td>'
            f'<td class="num">{p.p_hit:.2f}</td><td class="muted">{_esc(p.horizon_date)}</td></tr>'
            for p in open_preds
        )
        open_html = f"""
    <section class="card">
      <h3>待结算预测（{len(open_preds)}）</h3>
      <table class="mini"><thead><tr><th>ID</th><th>陈述</th><th>p</th><th>到期</th></tr></thead>
      <tbody>{op}</tbody></table>
    </section>"""
    else:
        open_html = ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>人生规划系统 · {as_of}</title>
<style>
  :root {{
    --bg:{PALETTE['bg']}; --card:{PALETTE['card']}; --border:{PALETTE['border']};
    --text:{PALETTE['text']}; --muted:{PALETTE['muted']}; --accent:{PALETTE['accent']};
    --good:{PALETTE['good']}; --warn:{PALETTE['warn']}; --bad:{PALETTE['bad']};
    --proxy:{PALETTE['proxy']};
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#15171c; --card:#1e2128; --border:#2c313a; --text:#e8eaed; --muted:#9aa2ae;
    }}
  }}
  * {{ box-sizing:border-box; }}
  svg {{ max-width:100%; height:auto; }}
  body {{ margin:0; padding:32px 20px 60px; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
    line-height:1.55; }}
  .wrap {{ max-width:1080px; margin:0 auto; }}
  header {{ display:flex; flex-wrap:wrap; gap:20px 24px; align-items:flex-end;
    justify-content:space-between; margin-bottom:24px; }}
  .brand {{ flex:1 1 320px; min-width:260px; }}
  h1 {{ font-size:22px; margin:0 0 4px; letter-spacing:-.2px; }}
  h2 {{ font-size:15px; margin:0 0 14px; text-transform:none; color:var(--muted); font-weight:600; }}
  h3 {{ font-size:15px; margin:0 0 6px; }}
  .muted {{ color:var(--muted); }}
  .small {{ font-size:12px; }}
  .num {{ font-variant-numeric:tabular-nums; text-align:right; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:18px 20px; margin-bottom:16px; }}
  .demo-banner {{ margin-bottom:16px; padding:10px 14px; border-radius:8px;
    background:rgba(245,159,0,.12); border:1px solid rgba(245,159,0,.38);
    color:#8a5a00; font-size:13px; line-height:1.5; }}
  .demo-banner strong {{ letter-spacing:.02em; }}
  .top {{ display:grid; grid-template-columns:auto 1fr; gap:28px; align-items:center; }}
  .kpis {{ flex:2 1 560px; display:grid; grid-template-columns:repeat(auto-fit,minmax(92px,1fr));
    gap:10px; }}
  .kpi {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:10px 12px; }}
  .kpi .v {{ font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; line-height:1.25; }}
  .kpi .l {{ font-size:11px; color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; font-weight:600; color:var(--muted); font-size:11px;
    padding:6px 8px; border-bottom:1px solid var(--border); }}
  td {{ padding:7px 8px; border-bottom:1px solid var(--border); vertical-align:middle; }}
  tbody tr:last-child td {{ border-bottom:none; }}
  .mini td,.mini th {{ padding:5px 7px; font-size:12px; }}
  .bar {{ display:inline-block; width:110px; height:7px; background:var(--border);
    border-radius:4px; overflow:hidden; vertical-align:middle; }}
  .bar span {{ display:block; height:100%; border-radius:4px; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:20px; font-size:11px;
    background:var(--border); color:var(--muted); }}
  .tag.proxy {{ background:rgba(247,103,7,.14); color:var(--proxy); }}
  .goal-head {{ display:flex; justify-content:space-between; align-items:flex-start;
    gap:16px; margin-bottom:12px; }}
  .goal-stats {{ display:flex; gap:18px; text-align:right; }}
  .goal-stats .big {{ display:block; font-size:18px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .cal-grid {{ display:grid; grid-template-columns:auto 1fr; gap:24px; align-items:start; }}
  .verdict {{ padding:10px 14px; border-radius:8px; background:rgba(76,110,245,.09);
    color:var(--accent); font-size:13px; font-weight:600; margin-bottom:12px; }}
  .uq-list {{ list-style:none; padding:0; margin:0; }}
  .uq-list li {{ padding:8px 0; border-bottom:1px solid var(--border); }}
  .uq-list li:last-child {{ border-bottom:none; }}
  .uq-list span {{ display:block; font-size:12px; margin-top:2px; }}
  .foot {{ text-align:center; color:var(--muted); font-size:11px; margin-top:28px; }}
  @media (max-width:720px) {{
    body {{ padding:20px 12px 40px; }}
    .card {{ padding:14px; }}
    .top,.cal-grid {{ grid-template-columns:1fr; }}
    .goal-stats {{ text-align:left; gap:12px; }}
    .bar {{ width:72px; }}
    table {{ font-size:12px; }}
    th, td {{ padding:6px 5px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  {banner_html}
  <header>
    <div class="brand">
      <h1>人生规划系统</h1>
      <div class="muted small">万事万物皆可量化 —— 但不可核验的量化，只值一半分。　统计截止 {as_of}</div>
    </div>
    <div class="kpis">
      <div class="kpi"><div class="v">{_num(cal.brier)}</div><div class="l">Brier 分数</div></div>
      <div class="kpi"><div class="v">{_num(cal.ece)}</div><div class="l">ECE 校准误差</div></div>
      <div class="kpi"><div class="v">{_signed(cal.overconfidence)}</div><div class="l">过度自信度</div></div>
      <div class="kpi"><div class="v">{cal.n_resolved}</div><div class="l">已结算预测</div></div>
      <div class="kpi"><div class="v">{snap.coverage*100:.0f}%</div><div class="l">数据覆盖率</div></div>
      <div class="kpi"><div class="v">{snap.value:.3f}</div><div class="l">价值分（参考）</div></div>
    </div>
  </header>

  <div class="card">
    <div class="top">
      <div>{_svg_donut(snap.value, snap.confidence)}</div>
      <div>
        <h2>为什么校准在前、价值分在后</h2>
        <p class="muted small" style="max-width:56ch">
          价值分的 baseline / target 由人填写，因此<strong>同一周同样的生活，
          改一下 target 就能让分数从 0.3 跳到 0.7</strong>（实测发生过：超额提交打满了
          target=12 的指标）。它只能自比，不能当"过得好不好"的读数。
          而校准的判据在系统外部 —— p=0.7 是否真的 70% 兑现，改不了。
          所以校准是主指标，价值分降为参考。左图外环为价值分、内环为证据置信度。
        </p>
      </div>
    </div>
  </div>

  <section class="card">
    <h2>校准：本系统唯一外部可检验的指标</h2>
    <p class="muted small">
      把「我能不能做到」当作二分类预测任务事前登记、到期结算，用 Brier 与 ECE 校准自己。
      这套方法来自评测方法论：<strong>裁判的可信度不来自权威，而来自其判断与实际结果的一致性</strong>。
    </p>
    {cal_html}
  </section>

  <div class="card">
    <h2>分领域：价值分（上）/ 证据置信度（下）<span class="muted small">　—— 参考指标</span></h2>
    <p class="muted small" style="margin-top:-6px">
      只做纵向自比，不做横向比较；分值水平取决于当初怎么写目标。
    </p>
    {_svg_bars(domain_bars)}
  </div>

  {''.join(goal_cards)}

  {open_html}
  {budget_html}
  {uq_html}

  <div class="foot">
    lifeplan · 单文件离线看板 · 所有数据来自本地 SQLite，无外部上传
  </div>
</div>
</body>
</html>"""

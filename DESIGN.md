---
name: lifeplan landing
description: 个人校准对账 · 本地优先的人生目标结算工具
colors:
  paper: "#eef0ea"
  surface: "#fdfdfb"
  ink: "#14171a"
  ink-hover: "#292f29"
  body-text: "#3d443e"
  muted: "#5f6660"
  rule: "#c9cec6"
  rule-strong: "#98a198"
  blue: "#1f5eff"
  blue-ink: "#1237a8"
  red: "#b42318"
  green: "#087f5b"
  yellow: "#f2d24b"
  terminal-bg: "#171a17"
  terminal-rule: "#2c302c"
  terminal-text: "#e7eae4"
  terminal-muted: "#a7aea5"
  terminal-control: "#444a44"
  terminal-copy: "#d7dbd4"
  terminal-control-hover: "#8a938a"
  terminal-prompt: "#7bd88f"
typography:
  display:
    fontFamily: "Noto Serif CJK SC, Source Han Serif SC, Songti SC, STSong, serif"
    fontSize: "clamp(48px, 6.3vw, 92px)"
    fontWeight: 700
    lineHeight: 1.04
    letterSpacing: "-0.035em"
  display-mobile:
    fontFamily: "Noto Serif CJK SC, Source Han Serif SC, Songti SC, STSong, serif"
    fontSize: "clamp(42px, 13vw, 64px)"
    fontWeight: 700
    lineHeight: 1.04
  section:
    fontFamily: "Noto Serif CJK SC, Source Han Serif SC, Songti SC, STSong, serif"
    fontSize: "clamp(30px, 3.4vw, 44px)"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  section-mobile:
    fontFamily: "Noto Serif CJK SC, Source Han Serif SC, Songti SC, STSong, serif"
    fontSize: "32px"
    fontWeight: 700
    lineHeight: 1.2
  sheet-title:
    fontFamily: "Noto Serif CJK SC, Source Han Serif SC, Songti SC, STSong, serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.3
  sheet-title-mobile:
    fontFamily: "Noto Serif CJK SC, Source Han Serif SC, Songti SC, STSong, serif"
    fontSize: "21px"
    fontWeight: 700
    lineHeight: 1.3
  step-char:
    fontFamily: "Noto Serif CJK SC, Source Han Serif SC, Songti SC, STSong, serif"
    fontSize: "56px"
    fontWeight: 700
    lineHeight: 1
  step-char-mobile:
    fontFamily: "Noto Serif CJK SC, Source Han Serif SC, Songti SC, STSong, serif"
    fontSize: "48px"
    fontWeight: 700
    lineHeight: 1
  faq-sign:
    fontFamily: "Noto Sans Mono CJK SC, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "20px"
    fontWeight: 400
    lineHeight: 1
  lede:
    fontFamily: "Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "19px"
    fontWeight: 400
    lineHeight: 1.75
  h3:
    fontFamily: "Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontFamily: "Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "17px"
    fontWeight: 400
    lineHeight: 1.7
    letterSpacing: "normal"
  body-mobile:
    fontFamily: "Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.7
  body-sm:
    fontFamily: "Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.6
  nav:
    fontFamily: "Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  small:
    fontFamily: "Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  caption:
    fontFamily: "Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
  mono:
    fontFamily: "Noto Sans Mono CJK SC, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "0.04em"
rounded:
  flat: "0px"
  control: "2px"
spacing:
  xs: "8px"
  sm: "16px"
  md: "24px"
  lg: "32px"
  xl: "64px"
  section: "88px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "0 22px"
    height: "50px"
  button-primary-hover:
    backgroundColor: "#292f29"
    textColor: "{colors.surface}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 22px"
    height: "50px"
  sheet:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.flat}"
    padding: "24px"
---

# lifeplan landing

## Overview

The landing is a **personal calibration statement**, not a SaaS marketing page. It inherits the dashboard's audit language and raises it into a ledger: paper ground, hairline rules, ink-black actions, blue data marks, a red overconfidence stamp, and a yellow highlighter on the verdict. The settlement sheet is the hero; it demonstrates the product by showing one prediction before and after settlement.

## Colors

- `paper` / `surface`: the only two grounds. `surface` is reserved for the settlement sheet, screenshots, and terminal contrast; never use white as a generic card fill on the page.
- `ink`: display type, primary actions, rules that carry structure.
- `muted`: secondary prose, labels, captions. Never use gray on a colored surface; tint from the surface hue instead.
- `blue`: data marks, links, the before-settlement stamp.
- `red`: only overconfidence / negative deviation. Do not use it for ordinary emphasis.
- `green`: reserved for terminal prompts and positive settlement states.
- `yellow`: the highlighter behind the H1 verdict and text selection. One use per viewport.

## Typography

- Display: Noto Serif CJK SC 700, tight tracking, line-height near 1.04. The headline is the only serif display; section headings use the same face at 30–44px.
- Body: Noto Sans CJK SC at 17px, 1.7 line-height, 65–75ch measure.
- Mono: Noto Sans Mono CJK SC for numbers, dates, document metadata, commands, and terminal output. Not for decorative labels that carry no measurement.
- Functional text never goes below 11px; captions and notes are 12px.

## Layout

- Content max-width 1180px; page padding 32px desktop, 20px mobile.
- First viewport: asymmetric 1.12 / 0.88 split. Left carries the verdict and actions; right carries the settlement sheet. At 980px the sheet stacks below; at 760px the nav collapses to the primary CTA.
- Section rhythm: 88px vertical padding desktop, 64px mobile. More space above a heading than below it.
- Tables are the default comparison structure. Avoid icon-card grids; the only repeated cards are the settlement rows and step columns, both carrying different content.

## Elevation & Depth

Flat by default. Borders and rules carry structure. The only shadow token is a 1px contact line (`0 1px 0 rgba(20,23,26,.04)`) on paper surfaces; there is no large soft shadow, glass, or gradient text.

## Shapes

Sharp corners: 0px on the sheet, screenshots, terminal, and document surfaces; 2px on buttons and the toggle. The stamp is the one rotated element (-6deg after settlement, 0deg before). Diagonal dashed lines belong to the reliability chart only.

## Components

- **Settlement sheet**: 1px border, 4px ink top rule, document metadata, 事前/结算后 toggle, dashed row rules, reliability chart, footer data, verdict stamp. The toggle is the signature interaction; it must flip the settlement row, deviation, chart points, footer text, and stamp together.
- **Primary button**: ink fill, paper text, 2px radius, 50px height. Ghost button: transparent, 1px rule-strong border. Hover raises 1px, never changes text to a low-contrast color.
- **Comparison table**: two columns, mono uppercase headers, 1px rules; stacks into labeled blocks below 760px.
- **Steps**: three columns separated by rules, a large blue Chinese character as the anchor. No cards, no icons.
- **Terminal**: ink ground, mono output, copy control at 11px minimum. It wraps on mobile rather than forcing horizontal scroll.
- **FAQ**: native `details`/`summary`, plus/minus indicator, no modal.

## Do's and Don'ts

- Do show the mechanism with real demo output; label synthetic data everywhere it appears.
- Do keep one authored motion (the sheet entrance) and one interaction (settlement toggle).
- Do keep all claims traceable to `PRODUCT.md`; no users, retention, stars, or benchmarks.
- Don't use gradient text, glass, colored left borders, hard offset shadows, or nested cards.
- Don't put an eyebrow/kicker above a heading; the heading carries itself.
- Don't use monospace for decoration; only for data, measurement, code, and terminal.
- Don't invent an AI feature or a graphical entry UI; the current product is CLI + offline dashboard.

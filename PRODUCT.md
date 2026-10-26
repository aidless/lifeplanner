# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

主用户 `[inferred]`：25–40 岁、同时背着多个长期目标（职业、健康、关系、学习）、已经试过待办清单/打卡但觉得“完成了也不代表过得好”的人。愿意每周花约 10 分钟做一次复盘，能接受命令行或一次性配置。

次用户 `[inferred]`：对量化自我、预测校准、评测方法论感兴趣的开发者和研究者。

## Product Purpose

把抽象的人生目标变成可核验的指标，把“我能不能做到”变成事前登记、到期结算的预测，让用户看见自己的判断偏差。数据默认只留在本地。

成功标准 `[inferred]`：用户能在一周后回来看一次结算，并据此调整下一次预测；不是日活、下载量或连续打卡天数。

## Positioning

不是待办清单，也不是打卡应用。唯一外部可检验的指标是**校准**：事前写的概率是否真的按比例兑现（Brier / ECE）。价值分由用户自己填的 baseline/target 决定，因此被明确降为参考指标。相邻产品可以复制看板，但复制不了“到期结算、不可事后改判”的机制。

## Operating Context

- Python 3.10+，零第三方依赖（`tomli` 仅在 3.10 需要），SQLite 单文件数据库。
- 命令行完成初始化、记录观测、登记预测、结算、周报与看板生成。
- 看板是单文件 HTML，无 CDN、无 JS 框架，离线双击可开；可随邮件或版本库传递。
- 演示数据为虚构画像，真实使用从空库开始。
- 没有云端同步、账号体系或多人协作。

## Capabilities and Constraints

已确认：

- `lifeplan init --seed` 初始化并写入演示数据；`score`、`calibrate`、`audit`、`observe`、`predict`、`resolve`、`report`、`dashboard`、`export` 可用。
- 188 个测试通过（2026-10-25，本机 `make test`，2 skipped）。
- `make demo` 约 3 秒产出 `dashboard.html`、`report.md`、`data.json`、`lifeplan.db`。
- 移动端已修复横向溢出（390px 视口下 scrollWidth = clientWidth = 390）。

约束与未决：

- 不是心理治疗、医疗建议或财务建议。
- 价值分只可纵向自比，不可横向比较，也不可作为对外陈述的依据。
- `[open]` 是否发布到 PyPI、是否提供图形化录入界面、是否支持多设备同步，均未决定。
- `[open]` 真实用户数、留存、NPS 均无数据，未来页面不得编造。

## Brand Commitments

- 现有主文案：`万事万物皆可量化 —— 但不可核验的量化，只值一半分。`
- 语气 `[inferred]`：克制、诚实、不评判；承认测量困难，不用“自律”“逆袭”“人生开挂”一类话术。
- 不使用“AI 驱动”作为主卖点；项目本身不依赖模型也能运行。

## Evidence on Hand

- `examples/dashboard.html` 与 `examples/report.md`：真实生成的演示产物。
- `lifeplan/seed.py`：虚构演示数据，文件内已声明不是真实人物记录。
- `tests/`：188 个测试；`make demo` 可复跑。
- 无真实用户、无推荐语、无媒体报道、无 GitHub star 可引用。

## Product Principles

1. **校准优先**：能由外部结算的预测，优先于自评分数。
2. **证据分级**：弱证据拿不到高分；置信度与价值分同时展示。
3. **不移动门柱**：基线冻结，事后追认会被审计检出。
4. **本地优先**：默认不上传，单文件可带走。
5. **承认不可量化**：难量化的领域保留登记，而不是假装它不存在。

## Accessibility & Inclusion

- 移动端 390px 无横向滚动；键盘可读的语义化 HTML；深浅色跟随系统。
- 不依赖 CDN 或远程字体，弱网/离线可用。

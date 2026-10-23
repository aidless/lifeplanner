# AGENT_PROTOCOL.md · Agent 工作协议

> 对应文档 §10 七步流程。每一步的输入输出与失败处理。

版本：v1（2026-10-22）

---

## 七步流程

### 第一步：识别目标

判断用户在做什么：取事实 / 预测 / 比较方案 / 排序 / 设计行动 /
复盘 / 重新定义目标。

实现：`qcompile()` 关键词匹配（v1）；v2 由 LLM 分类。
失败处理：域外 → 显式 unknown + `need_data`，不猜。

### 第二步：生成量化计划

确定：测什么、哪些数据已有、哪些缺失、哪些变量不可直接观测、
哪些假设必须公开。

实现：`Question` 列表（measure/score/choice/noul 四类）。
失败处理：计划无法被现有工具取证 → 在 assumptions 里写明并降级。

### 第三步：获取证据

优先级（从高到低）：

1. 用户提供的原始数据
2. 可追溯的本地记录（git/文件/DB）
3. 一手论文与技术报告
4. 官方 API
5. 经验证的二手资料
6. 模型推断（必须标 L1）

实现：`_evidence()`；每条证据 sha256 + 分级。
失败处理：取不到 → 该信号记 unknown，不用模型幻觉填补。

### 第四步：计算与预测

全部由 `calc.py`/SQL 完成：指标、趋势、差值、区间、成本、风险、机会成本。
失败处理：除零/空输入 → None；n<2 → 不给区间。

### 第五步：结构化决策

Laya/Jev 风格问题：

```
哪个方案优先？（choice）
风险属于哪一级？（score）
是否需要更多证据？（noul）
是否应升级到更强模型？（noul，act-or-escalate）
是否应停止自动行动？（noul → human_review_required）
```

实现：`_decide()`；置信 < 0.60 升级 LLM；LLM 不可用 → 文档化规则；
证据不足 → 拒答（优先于以上全部）。

### 第六步：输出结果

必须同时包含：结论、数值/等级、证据、置信度、未知项、假设、下一步行动。
实现：`QOneAnswer.to_contract()`，过 `DECISION_SCHEMA.json` 校验。

### 第七步：记录实际结果

行动后记录：预期、实际、偏差、错误来源、指标是否需改。
实现：`decisions` 台账 + `week.py` 周复盘 + `calibration.py` 结算。

---

## 协议铁律

1. **LLM 只提候选，人批准**：指标定义进协议前必须人确认
2. **数字必须过 Schema**：任何对外数字先过 `measurement_contract.schema.json`
3. **证据不齐就拒答**：`need_data` 是合法且受保护的输出
4. **决策必须可复算**：同输入同 seed → 同输出（bootstrap 固定种子）
5. **行动必须分级**：L0-L2 自动，L3 人工，L4 禁止

## 工具清单（v1，12 个）

| 工具 | tier | 说明 |
|---|---|---|
| plan_score / plan_goals / plan_calibration / plan_audit / host_profile / predictions_due / proposals_list | 0 | 只读取证 |
| log_observation / register_prediction / resolve_prediction | 1 | 本地可逆写入（需 --write） |
| term_exec / app_launch | 2 | **只生成提案，永不执行** |

tier2 执行必须 `lifeplan act run <id> --confirm-token <token>`。

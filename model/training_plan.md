# training_plan.md · 专用决策模型训练计划（阶段六）

> 蓝图 §12 阶段六 + §14。**只有当决策台账有足够带★字段的记录后才启动。**
> 当前状态：未启动（记录不足）。

版本：v0.1（2026-10-22）

---

## 1. 启动门槛（缺一不可）

1. `decisions` 台账 ≥ 50 条，其中 `human_decision` 非空 ≥ 30 条
2. `actual_outcome` 已回填 ≥ 20 条
3. `trainable: true` 显式标记 ≥ 30 条（隐私政策 §5）
4. 基线 A（纯人工）与 F（LLM+证据+计算+决策层）已实测

## 2. 训练顺序（蓝图 §12）

```
choice → score → noul → measure → forecast → rank → abstain
```

每题型独立模型头 + 独立温标（蓝图 §13：不同题型校准不同）。

## 3. 架构（jev-s1，已备）

- 双向 encoder（ModernBERT 规模）+ 决策头（选项末 token 打分）
- **必须先 MLM 预训练**（CL-04：从零训练存在内容绑定平台期）
- 阶段一路线：nano 验证机制 → 预训练实验 → 421M 档（Kaggle T4×2）

## 4. 训练数据构成

| 来源 | 用途 |
|---|---|
| decisions 台账（trainable=true） | 监督（human_decision 为标签） |
| predictions 结算历史 | 校准训练（RLCD：Brier 为 reward） |
| 合成任务（jevs1/data.py） | 机制验证与预训练补充 |

## 5. 评测门（每题型）

- choice：Accuracy、Macro-F1、**选项顺序不变性**
- score/noul：Brier、ECE（分题型）、Risk-Coverage
- abstain：拒答后人工修正率 vs 硬答错误率
- 全部：延迟 < 100ms（CPU）、可复算（固定 seed）

## 6. 反作弊

- 训练/验证/测试按**时间**划分（不用随机划分——防未来信息泄漏）
- 测试集只在最终评测用；温标在验证集拟合
- 模型卡必须记录：数据版本、阈值、已知失败模式

## 7. 明确不做

- 不用未授权数据（隐私政策）
- 不为刷指标改测试集
- 不合并题型训练（数值预测与决策判断分开，蓝图 §12）

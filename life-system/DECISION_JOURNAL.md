# DECISION_JOURNAL.md · 决策日志规范

> 蓝图 §12 阶段五 + §18 产出。这是阶段八训练专用模型的**唯一数据来源**——
> 记录质量直接决定模型上限。

版本：v1（2026-10-22）

---

## 每条记录必填（蓝图 §12 阶段五）

```yaml
decision_id: decision_20261022_234753   # 自动生成
ts: 2026-10-22T23:47:53
state: "这个项目是否值得继续投入"
question_type: choice                    # 七类之一
options: [continue, pivot, pause, stop, collect_more_evidence]
model_decision: collect_more_evidence    # 系统裁决
model_probabilities: {...}               # 各选项概率
confidence: {model: 0.8, evidence: 0.75, forecast: null}
human_decision: null                     # ★ 人工裁决（训练标签）
human_override_note: null                # ★ 为什么改
action_taken: null                       # 实际采取的行动
actual_outcome: null                     # ★ 实际结果（到期回填）
outcome_evidence: null                   # 结果证据（sha256）
error_source: null                       # 误差来源（复盘填）
review_conclusion: null                  # 复盘结论
trainable: false                         # ★ 主人显式标记可否用于训练
```

## 三个★字段是训练数据的命门

1. **human_decision**：没有人工裁决的记录只能当预训练/对比样本，
   不能当监督标签——否则模型学的是自己的偏见。
2. **actual_outcome**：没有实际结果的记录无法用于校准训练
   （RLCD 的 reward 需要结果）。
3. **trainable**：默认 false。隐私政策 §5 规定——
   **未经主人显式标记，任何个人决策不得进入训练集。**

## 实现映射

| 字段 | 存储 |
|---|---|
| 决策主体 | `decisions` 表（`ledger.py`） |
| 人工修正 | `decisions.human_override` / `override_note` |
| 预测与实际 | `predictions.outcome` + `evidence_ref` |
| 复盘结论 | `observations.note` / `events` |

## 记录纪律

1. 决策当时记录，不事后补（补的记录标注 `reconstructed: true`）
2. 人工修正必须写理由——「我觉得」不是理由，「证据 X 显示…」才是
3. 到期必须回填 actual_outcome；无法回填的标 `unresolvable` 并说明
4. `qone verify` 定期校验台账完整性

## 当前状态

台账已有记录（含 1 条域外拒绝样本），但三条★字段全空——
它们在等主人：裁决、回填、授权。

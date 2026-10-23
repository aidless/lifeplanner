# ARCHITECTURE-OPTIONS.md · 架构备选登记

> 只登记「看过、评估过、暂不实施」的外部设计。每条给出：来源、机制拆解、与本系统的映射、成本、拒绝或推迟的理由。
> 与 ARCHITECTURE.md 的关系：本文件是候选池，不是现状描述；采纳后才写入 ARCHITECTURE.md。

---

## OPT-01 · 上下文冻结交接（来源：TabTin `SessionContinuation`）

**登记日期**：2026-10-23
**来源**：TabTin 开源仓 `apps/tabtin_django/apps/chat/conversation/models.py:1501-1565`、`session_continuation_service.py`、`api/fork.py`

### 机制拆解

TabTin 把「任务交接」做成一个**发送时冻结的一次性续接包**，而不是转发聊天记录：

| 字段 | 作用 |
|---|---|
| `frozen_context_json` | 冻结对话上下文（发送那一刻的切片，之后不再变化） |
| `resources_json` | 快照引用文档/表格/云端/本地文件，且只包含**有权共享**的资源 |
| `context_status` | 标注完整度：complete / truncated / empty——接手人知道这份上下文缺没缺 |
| `SessionShareResourceGrant` | 资源级授权（不是会话级），逐个资源判权 |
| `api/fork.py` | 接手方 fork 时做消息 ID 重映射，形成独立任务线 |
| 事件表 | `SessionContinuationEvent` / `SessionShareEvent`，交接全程可追溯 |

### 映射到 lifeplan

lifeplan 的证据层已经比 TabTin 严格（sha256 + tier 分级 + 拒答门控），所以**不能照搬「冻结文本」**，应改成「冻结证据包」：

1. 交接包 = 任务状态（当前 question/计划）+ 证据引用（sha256 + tier）+ 未完成的测量协议
2. 交接包自身要有哈希与版本号——否则「冻结」只是口头承诺，无法核验
3. 接收方 fork 后必须**重新验证证据哈希**，验证失败则拒绝接续（对应 ARCHITECTURE.md 的「没有箭头的地方没有信任」）
4. 交接包只允许携带证据与上下文，**不允许携带结论**——LLM 仍然不能碰数值

### 成本

- 最小实现：一个 JSON schema + 校验器 + 一张事件表（SQLite），不需要服务端
- 与现有 schema 的冲突：需要给证据表加 `bundle_id` 外键；v2 迁移
- 风险：上下文体积 vs 完整度；跨版本 schema 兼容；授权粒度在单机场景下退化为「文件路径白名单」

### 决定

**推迟到 v2**。当前 lifeplan 是单用户本地系统，交接需求尚未出现；先把「证据包哈希 + 重验证」做成通用工具函数（`presence.py` 旁），等出现跨会话/跨 agent 续接的真实场景再接模型层。届时优先复用本条目的字段设计，而不是重新发明。

---

<!-- 新条目按 OPT-02 递增；每条必须写明「决定」（采纳/推迟/拒绝）与理由 -->

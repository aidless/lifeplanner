-- 人生规划系统 · 数据库结构
-- 设计原则：一切目标必须落到可观测指标；每一次观测必须带证据等级；每一次预测必须可结算。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS domains (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    weight      REAL    NOT NULL DEFAULT 1.0,
    color       TEXT    NOT NULL DEFAULT '#4c6ef5',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY,
    domain_id   INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    start_date  TEXT    NOT NULL,
    end_date    TEXT    NOT NULL,
    weight      REAL    NOT NULL DEFAULT 1.0,
    status      TEXT    NOT NULL DEFAULT 'active',   -- active | done | dropped
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id            INTEGER PRIMARY KEY,
    goal_id       INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    unit          TEXT    NOT NULL DEFAULT '',
    direction     TEXT    NOT NULL DEFAULT 'increase',  -- increase | decrease | maintain
    baseline      REAL    NOT NULL,
    target        REAL    NOT NULL,
    tolerance     REAL    NOT NULL DEFAULT 0,           -- 仅 maintain 方向生效：容差带
    weight        REAL    NOT NULL DEFAULT 1.0,
    cadence       TEXT    NOT NULL DEFAULT 'weekly',    -- daily | weekly | monthly | quarterly | yearly
    evidence_tier INTEGER NOT NULL DEFAULT 2,           -- 该指标要求的证据等级 0..3
    is_proxy      INTEGER NOT NULL DEFAULT 0,           -- 是否为代理指标
    proxy_for     TEXT    NOT NULL DEFAULT '',          -- 代理所指向的真实属性（不可量化时使用）
    baseline_at   TEXT    NOT NULL,                     -- 基线冻结时间：用于检测事后移动门柱
    archived      INTEGER NOT NULL DEFAULT 0,           -- 归档：不再参与评分与提醒，但不删数据
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id           INTEGER PRIMARY KEY,
    metric_id    INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    value        REAL    NOT NULL,
    observed_at  TEXT    NOT NULL,
    note         TEXT    NOT NULL DEFAULT '',
    evidence_ref TEXT    NOT NULL DEFAULT '',           -- URL / commit / 文件路径 / 截图
    tier         INTEGER NOT NULL DEFAULT 1,            -- 0 断言 · 1 自述 · 2 有记录 · 3 可核验
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    id           INTEGER PRIMARY KEY,
    goal_id      INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    horizon_date TEXT    NOT NULL,
    p_hit        REAL    NOT NULL,
    statement    TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    resolved_at  TEXT,
    outcome      INTEGER,                             -- 1 命中 · 0 未中 · NULL 未结算
    evidence_ref TEXT,                                -- 结算依据：URL / 文件 / 说明
    outcome_note TEXT                                 -- 结算备注（可写为什么判这个结果）
);

CREATE TABLE IF NOT EXISTS unquantifiable (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL,
    reason           TEXT    NOT NULL,
    proxy_metric_id  INTEGER REFERENCES metrics(id) ON DELETE SET NULL,
    status           TEXT    NOT NULL DEFAULT 'open',   -- open | proxied | retired
    review_at        TEXT,                              -- 复核期：到期后自主循环会提醒
    created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS budget (
    id           INTEGER PRIMARY KEY,
    domain_id    INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    week_start   TEXT    NOT NULL,
    planned_hours REAL   NOT NULL DEFAULT 0,
    actual_hours  REAL   NOT NULL DEFAULT 0,
    UNIQUE(domain_id, week_start)
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);

-- 元信息：schema 版本、最后备份时间等
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_metric ON observations(metric_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_metric_goal ON metrics(goal_id);
CREATE INDEX IF NOT EXISTS idx_goal_domain ON goals(domain_id);
CREATE INDEX IF NOT EXISTS idx_pred_goal ON predictions(goal_id);
CREATE INDEX IF NOT EXISTS idx_pred_open ON predictions(outcome, horizon_date);

-- ── 宿主感知层 ──────────────────────────────────────────────
-- 每次扫描产出一条画像；所有推断都必须挂在 host_signals 的证据上，
-- 不接受"模型说是就是"的结论。
CREATE TABLE IF NOT EXISTS host_profile (
    id         INTEGER PRIMARY KEY,
    scanned_at TEXT    NOT NULL,
    hostname   TEXT    NOT NULL DEFAULT '',
    os_name    TEXT    NOT NULL DEFAULT '',
    session    TEXT    NOT NULL DEFAULT '',
    summary    TEXT    NOT NULL,
    confidence REAL    NOT NULL DEFAULT 0,
    raw_json   TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS host_signals (
    id         INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES host_profile(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,   -- project | tool | process | history | workspace | session
    name       TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    evidence   TEXT    NOT NULL,   -- 该信号的来源（路径 / 命令 / 进程）
    weight     REAL    NOT NULL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_signal_profile ON host_signals(profile_id);

-- ── 执行层审计 ──────────────────────────────────────────────
-- 每一个有副作用的动作都要留痕：谁提出、放行到哪一级、执行了什么、结果如何。
CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY,
    ts           TEXT    NOT NULL,
    kind         TEXT    NOT NULL,   -- terminal | app | write | observe
    tier         INTEGER NOT NULL,   -- 0 观察 1 工作区写 2 执行 3 破坏性
    target       TEXT    NOT NULL DEFAULT '',
    command      TEXT    NOT NULL,
    status       TEXT    NOT NULL,   -- proposed | done | failed | blocked | denied
    requires_confirm INTEGER NOT NULL DEFAULT 0,
    rationale    TEXT    NOT NULL DEFAULT '',
    output       TEXT    NOT NULL DEFAULT '',
    confirm_token TEXT   NOT NULL DEFAULT '',
    argv_json    TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status, ts);


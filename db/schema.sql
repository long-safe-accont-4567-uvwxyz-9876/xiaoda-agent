-- ============================================================
-- AI Agent 数据库完整初始化脚本
-- 来源: database.py (_create_tables + _run_migrations)
-- 说明: 已合并所有迁移(v1, v2)的列到建表语句中
-- 用法: sqlite3 agent.db < schema.sql
-- ============================================================

-- 迁移版本追踪
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

-- 对话日志（v2: 已合并 session_id 列）
CREATE TABLE IF NOT EXISTS conversation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    user_id TEXT DEFAULT '',
    source TEXT DEFAULT 'qq',
    user_message TEXT DEFAULT '',
    assistant_reply TEXT DEFAULT '',
    emotion_label TEXT DEFAULT '',
    model_used TEXT DEFAULT '',
    session_id TEXT DEFAULT ''
);

-- 审计日志
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    user_id TEXT DEFAULT '',
    detail TEXT DEFAULT ''
);

-- 情景记忆
CREATE TABLE IF NOT EXISTS episodic_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    summary TEXT NOT NULL,
    importance REAL DEFAULT 0.5,
    emotion_label TEXT DEFAULT '',
    session_id TEXT DEFAULT 'user',
    embedding_id INTEGER DEFAULT -1,
    rag_status TEXT DEFAULT 'pending',         -- RAG 同步状态: pending(待索引)/indexed(已索引)/failed(索引失败)/excluded(排除索引)
    rag_synced_at REAL DEFAULT 0,              -- 最后 RAG 同步时间戳
    doc_id TEXT DEFAULT '',                    -- 关联外部向量数据库文档 ID
    source TEXT DEFAULT 'user',
    access_count INTEGER DEFAULT 0,            -- 检索命中次数
    distilled INTEGER DEFAULT 0,               -- 是否已被蒸馏提取
    entities TEXT DEFAULT '',
    event_type TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    content_hash TEXT DEFAULT '',
    version INTEGER DEFAULT 1
);

-- 情景记忆全文索引（FTS5）
CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory_fts USING fts5(
    id UNINDEXED,
    summary_index
);

-- 子chunk表（父子Chunk RAG优化）
CREATE TABLE IF NOT EXISTS memory_child_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    embed_content TEXT DEFAULT '',
    chunk_type TEXT NOT NULL DEFAULT 'segment',
    importance REAL DEFAULT 0.5,
    overlap_hash TEXT DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES episodic_memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_child_parent ON memory_child_chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_child_type ON memory_child_chunks(chunk_type);

-- 子chunk全文索引
CREATE VIRTUAL TABLE IF NOT EXISTS memory_child_chunks_fts
    USING fts5(content, tokenize='unicode61');

-- 记忆合并候选（审计）
CREATE TABLE IF NOT EXISTS consolidation_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'rule',
    kind TEXT NOT NULL DEFAULT 'fact',
    summary TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    importance REAL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'pending',
    target_memory_id INTEGER DEFAULT -1,
    metadata_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL
);

-- 定时任务上次运行时间
CREATE TABLE IF NOT EXISTS cron_last_run (
    task_name TEXT PRIMARY KEY,
    last_run REAL NOT NULL
);

-- 用户画像
CREATE TABLE IF NOT EXISTS user_portrait (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    version INTEGER DEFAULT 1,
    source_ids TEXT DEFAULT '',
    change_log TEXT DEFAULT '',
    created_at REAL NOT NULL
);

-- 笔记本条目
CREATE TABLE IF NOT EXISTS notebook_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'note',
    content TEXT NOT NULL,
    tags TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    due_date REAL DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- 主动消息
CREATE TABLE IF NOT EXISTS proactive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    content TEXT NOT NULL,
    sent_at REAL NOT NULL
);

-- API 用量
CREATE TABLE IF NOT EXISTS api_usage (
    id TEXT PRIMARY KEY,
    user_openid TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    model TEXT DEFAULT '',
    task_type TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cache_hit_tokens INTEGER DEFAULT 0,
    cache_miss_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    created_at REAL NOT NULL
);

-- 问候调度配置
CREATE TABLE IF NOT EXISTS greeting_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK(type IN ('fixed','random')),
    time TEXT DEFAULT '',
    window_start TEXT DEFAULT '',
    window_end TEXT DEFAULT '',
    count_per_day INTEGER DEFAULT 1,
    days TEXT NOT NULL DEFAULT '[1,2,3,4,5,6,7]',
    prompt_hint TEXT DEFAULT '',
    channels TEXT NOT NULL DEFAULT '["web"]',
    enabled INTEGER NOT NULL DEFAULT 1,
    next_fire_times TEXT DEFAULT '[]',
    drawn_date TEXT DEFAULT '',
    created_at REAL NOT NULL
);

-- 问候执行日志
CREATE TABLE IF NOT EXISTS greeting_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER DEFAULT 0,
    fired_at REAL NOT NULL,
    content TEXT DEFAULT '',
    channel TEXT DEFAULT 'web',
    reason TEXT DEFAULT ''
);

-- 媒体任务
CREATE TABLE IF NOT EXISTS media_tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    prompt TEXT DEFAULT '',
    params TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL DEFAULT 0,
    result_path TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at REAL NOT NULL,
    finished_at REAL
);

-- 健康检查报告
CREATE TABLE IF NOT EXISTS health_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at REAL NOT NULL,
    passed INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '[]'
);

-- 会话
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_openid TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    turn_count INTEGER DEFAULT 0,
    total_cost_usd REAL DEFAULT 0,
    cache_hit_tokens INTEGER DEFAULT 0,
    cache_miss_tokens INTEGER DEFAULT 0,
    started_at REAL NOT NULL,
    ended_at REAL DEFAULT 0,
    status TEXT DEFAULT 'active'
);

-- Agent 事件
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    user_openid TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    created_at REAL NOT NULL
);

-- 知识图谱 - 实体
CREATE TABLE IF NOT EXISTS knowledge_entities (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE,
    kind TEXT DEFAULT '',
    observations TEXT DEFAULT '[]',
    updated_at REAL NOT NULL
);

-- 知识实体全文索引（FTS5）
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_entities_fts USING fts5(
    id UNINDEXED,
    name_index
);

CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_ai AFTER INSERT ON knowledge_entities BEGIN
    INSERT INTO knowledge_entities_fts(id, name_index)
    VALUES (new.id, new.name);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_ad AFTER DELETE ON knowledge_entities BEGIN
    INSERT INTO knowledge_entities_fts(knowledge_entities_fts, id, name_index)
    VALUES ('delete', old.id, old.name);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_au AFTER UPDATE ON knowledge_entities BEGIN
    INSERT INTO knowledge_entities_fts(knowledge_entities_fts, id, name_index)
    VALUES ('delete', old.id, old.name);
    INSERT INTO knowledge_entities_fts(id, name_index)
    VALUES (new.id, new.name);
END;

-- 知识图谱 - 关系（v1: 已合并 valid_from/valid_to/confidence）
CREATE TABLE IF NOT EXISTS knowledge_relations (
    id TEXT PRIMARY KEY,
    from_entity TEXT,
    relation_type TEXT,
    to_entity TEXT,
    created_at REAL DEFAULT 0,
    updated_at REAL NOT NULL,
    valid_from REAL DEFAULT 0,
    valid_to REAL DEFAULT 0,
    confidence REAL DEFAULT 1.0
);

-- 学习记录
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learning_id TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'insight',
    priority TEXT NOT NULL DEFAULT 'low',
    status TEXT NOT NULL DEFAULT 'pending',
    area TEXT DEFAULT 'backend',
    summary TEXT NOT NULL,
    details TEXT DEFAULT '',
    suggested_action TEXT DEFAULT '',
    source TEXT DEFAULT 'conversation',
    pattern_key TEXT DEFAULT '',
    recurrence_count INTEGER DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    created_at REAL NOT NULL
);

-- 错误记录
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    error_id TEXT NOT NULL UNIQUE,
    priority TEXT NOT NULL DEFAULT 'high',
    status TEXT NOT NULL DEFAULT 'pending',
    area TEXT DEFAULT 'backend',
    summary TEXT NOT NULL,
    error_text TEXT DEFAULT '',
    context TEXT DEFAULT '',
    suggested_fix TEXT DEFAULT '',
    reproducible TEXT DEFAULT 'unknown',
    created_at REAL NOT NULL
);

-- 功能请求
CREATE TABLE IF NOT EXISTS feature_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'pending',
    area TEXT DEFAULT 'backend',
    capability TEXT NOT NULL,
    user_context TEXT DEFAULT '',
    complexity TEXT DEFAULT 'medium',
    suggested_impl TEXT DEFAULT '',
    frequency TEXT DEFAULT 'first_time',
    created_at REAL NOT NULL
);

-- 会话条目
CREATE TABLE IF NOT EXISTS session_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- 会话摘要
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY,
    mtime INTEGER NOT NULL DEFAULT 0,
    summary_data TEXT NOT NULL DEFAULT '{}'
);

-- 数据清理配置
CREATE TABLE IF NOT EXISTS cleanup_config (
    table_name TEXT PRIMARY KEY,
    retention_days INTEGER NOT NULL,
    date_column TEXT NOT NULL DEFAULT 'timestamp',
    enabled INTEGER DEFAULT 1
);

-- ============================================================
-- 索引
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversation_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversation_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_conv_source ON conversation_logs(source);
CREATE INDEX IF NOT EXISTS idx_mem_ts ON episodic_memories(timestamp);
CREATE INDEX IF NOT EXISTS idx_mem_importance ON episodic_memories(importance);
CREATE INDEX IF NOT EXISTS idx_episodic_session ON episodic_memories(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_portrait_created ON user_portrait(created_at);
CREATE INDEX IF NOT EXISTS idx_notebook_kind ON notebook_entries(kind);
CREATE INDEX IF NOT EXISTS idx_notebook_status ON notebook_entries(status);
CREATE INDEX IF NOT EXISTS idx_notebook_due ON notebook_entries(due_date);
CREATE INDEX IF NOT EXISTS idx_proactive_user ON proactive_messages(user_id);
CREATE INDEX IF NOT EXISTS idx_api_usage_ts ON api_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_api_usage_user ON api_usage(user_openid);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_openid);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_events_type ON agent_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_ts ON agent_events(created_at);
CREATE INDEX IF NOT EXISTS idx_kg_entity_name ON knowledge_entities(name);
CREATE INDEX IF NOT EXISTS idx_kg_entity_updated ON knowledge_entities(updated_at);
CREATE INDEX IF NOT EXISTS idx_kg_rel_from ON knowledge_relations(from_entity);
CREATE INDEX IF NOT EXISTS idx_kg_rel_to ON knowledge_relations(to_entity);
CREATE INDEX IF NOT EXISTS idx_learnings_cat ON learnings(category);
CREATE INDEX IF NOT EXISTS idx_learnings_status ON learnings(status);
CREATE INDEX IF NOT EXISTS idx_learnings_pattern ON learnings(pattern_key);
CREATE INDEX IF NOT EXISTS idx_errors_status ON errors(status);
CREATE INDEX IF NOT EXISTS idx_featreq_status ON feature_requests(status);
CREATE INDEX IF NOT EXISTS idx_session_entries_sid ON session_entries(session_id);
CREATE INDEX IF NOT EXISTS idx_session_entries_created ON session_entries(created_at);

-- Task 2 新增索引
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_kg_rel_type ON knowledge_relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_media_status ON media_tasks(status);

-- ============================================================
-- 复合索引优化 (Phase 2: 性能优化)
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_episodic_timestamp_importance
    ON episodic_memories(timestamp DESC, importance DESC);
CREATE INDEX IF NOT EXISTS idx_episodic_session_created
    ON episodic_memories(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_session
    ON conversation_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_api_usage_created
    ON api_usage(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_entries_session
    ON session_entries(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_events_type_created
    ON agent_events(event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status_started
    ON sessions(status, started_at DESC);

-- ============================================================
-- 默认数据
-- ============================================================

INSERT INTO cleanup_config (table_name, retention_days, date_column) VALUES ('audit_logs', 90, 'timestamp');
INSERT INTO cleanup_config (table_name, retention_days, date_column) VALUES ('api_usage', 30, 'created_at');
INSERT INTO cleanup_config (table_name, retention_days, date_column) VALUES ('sessions', 180, 'ended_at');

-- ============================================================
-- 概念图表 (扩散激活记忆系统)
-- ============================================================

CREATE TABLE IF NOT EXISTS concept_nodes (
    id            TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 1.0,
    peak_weight   REAL NOT NULL DEFAULT 1.0,
    confidence    REAL NOT NULL DEFAULT 1.0,
    access_count  INTEGER NOT NULL DEFAULT 0,
    keys          TEXT NOT NULL DEFAULT '[]',
    layer         TEXT NOT NULL DEFAULT 'hippocampus',
    created       TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    superseded_by TEXT,
    history       TEXT NOT NULL DEFAULT '[]',
    origin        TEXT NOT NULL DEFAULT '{}',
    source_mem_id INTEGER,
    embedding     BLOB
);

CREATE TABLE IF NOT EXISTS concept_edges (
    source_id  TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    relation   TEXT NOT NULL DEFAULT 'related',
    weight     REAL NOT NULL DEFAULT 1.0,
    created    TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id)
);

CREATE TABLE IF NOT EXISTS concept_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concept_node_keys ON concept_nodes(keys);
CREATE INDEX IF NOT EXISTS idx_concept_node_layer ON concept_nodes(layer);
CREATE INDEX IF NOT EXISTS idx_concept_node_weight ON concept_nodes(weight);
CREATE INDEX IF NOT EXISTS idx_concept_node_valid ON concept_nodes(valid_to);
CREATE INDEX IF NOT EXISTS idx_concept_edge_source ON concept_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_concept_edge_target ON concept_edges(target_id);

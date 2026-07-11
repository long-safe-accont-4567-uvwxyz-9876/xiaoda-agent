-- v0.6.0 认知架构优化迁移

-- 语义记忆（consolidation后的长期记忆）
CREATE TABLE IF NOT EXISTS semantic_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_memory_id INTEGER,
    content TEXT NOT NULL,
    embedding_id INTEGER DEFAULT -1,
    cluster_id INTEGER DEFAULT -1,
    salience REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0,
    last_accessed REAL DEFAULT 0,
    created_at REAL NOT NULL,
    emotion_label TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_semantic_cluster ON semantic_memories(cluster_id);
CREATE INDEX IF NOT EXISTS idx_semantic_salience ON semantic_memories(salience);

-- 记忆连接图
CREATE TABLE IF NOT EXISTS memory_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    weight REAL DEFAULT 0.5,
    edge_type TEXT NOT NULL DEFAULT 'similar',
    activation_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    last_activated REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conn_source ON memory_connections(source_id);
CREATE INDEX IF NOT EXISTS idx_conn_target ON memory_connections(target_id);
CREATE INDEX IF NOT EXISTS idx_conn_type ON memory_connections(edge_type);

-- 桥接记忆
CREATE TABLE IF NOT EXISTS bridge_memories (
    id TEXT PRIMARY KEY,
    source_memory_id INTEGER NOT NULL,
    target_memory_id INTEGER NOT NULL,
    weight REAL NOT NULL,
    bridge_type TEXT DEFAULT 'semantic',
    source_session_id TEXT DEFAULT '',
    target_session_id TEXT DEFAULT '',
    cross_session INTEGER DEFAULT 0,
    discovered_at REAL NOT NULL,
    discovery_reason TEXT DEFAULT 'rem_bridge'
);
CREATE INDEX IF NOT EXISTS idx_bridge_source ON bridge_memories(source_memory_id);
CREATE INDEX IF NOT EXISTS idx_bridge_target ON bridge_memories(target_memory_id);

-- 冲突修订链
CREATE TABLE IF NOT EXISTS memory_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_memory_id INTEGER NOT NULL,
    new_memory_id INTEGER NOT NULL,
    conflict_type TEXT DEFAULT 'numeric_token',
    revision_chain TEXT DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_old ON memory_revisions(old_memory_id);

-- 偏好模式
CREATE TABLE IF NOT EXISTS preference_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_text TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    source_sessions TEXT DEFAULT '[]',
    salience REAL DEFAULT 2.0,
    created_at REAL NOT NULL,
    last_matched REAL DEFAULT 0,
    match_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_preference_salience ON preference_patterns(salience);

-- episodic_memories 新增字段 (使用安全的 ALTER TABLE)
ALTER TABLE episodic_memories ADD COLUMN salience REAL DEFAULT 0.5;
ALTER TABLE episodic_memories ADD COLUMN last_accessed REAL DEFAULT 0;
ALTER TABLE episodic_memories ADD COLUMN status TEXT DEFAULT 'active';

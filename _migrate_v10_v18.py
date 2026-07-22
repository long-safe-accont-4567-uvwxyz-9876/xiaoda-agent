"""手动执行 v10-v18 数据库迁移（幂等）。"""
import sqlite3
import time

DB = "/home/orangepi/ai-agent/data/agent.db"
conn = sqlite3.connect(DB)

def cols(table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

def tables():
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

def add_col(table, name, typ, default):
    """幂等添加列。"""
    existing = cols(table)
    if name not in existing:
        if typ == "TEXT":
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ} DEFAULT '{default}'")
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ} DEFAULT {default}")
        print(f"  +{name}")

epi = cols("episodic_memories")
tbls = tables()

print("=== v10: entities, event_type, metadata_json ===")
add_col("episodic_memories", "entities", "TEXT", "")
add_col("episodic_memories", "event_type", "TEXT", "")
add_col("episodic_memories", "metadata_json", "TEXT", "")

print("=== v11: memory_recall_notes ===")
if "memory_recall_notes" not in tbls:
    conn.execute("CREATE TABLE IF NOT EXISTS memory_recall_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL, note TEXT NOT NULL, created_at REAL NOT NULL)")
    print("  +memory_recall_notes")

print("=== v12: content_hash, version, memory_versions, context_audit_log ===")
add_col("episodic_memories", "content_hash", "TEXT", "")
add_col("episodic_memories", "version", "INTEGER", 1)
for tn, ddl in [
    ("memory_versions", "id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL, version INTEGER NOT NULL, summary TEXT, created_at REAL NOT NULL"),
    ("context_audit_log", "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, event_type TEXT NOT NULL, details TEXT, created_at REAL NOT NULL"),
]:
    if tn not in tbls:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {tn} ({ddl})")
        print(f"  +{tn}")

print("=== v15: fsrs_dsr_columns ===")
add_col("episodic_memories", "difficulty", "REAL", 5.0)
add_col("episodic_memories", "stability", "REAL", 3.0)
add_col("episodic_memories", "phase", "TEXT", "buffer")
add_col("episodic_memories", "last_review", "REAL", 0)
add_col("episodic_memories", "reinforcement_count", "INTEGER", 0)
conn.execute("UPDATE episodic_memories SET phase = 'permanent' WHERE access_count >= 5 AND phase = 'buffer'")
print("  updated permanent phases")

print("=== v16: created_at ===")
add_col("episodic_memories", "created_at", "REAL", 0)
conn.execute("UPDATE episodic_memories SET created_at = timestamp WHERE created_at = 0")
print("  backfilled created_at")

print("=== v17: greeting_schedules reminder_type ===")
if "greeting_schedules" in tbls:
    add_col("greeting_schedules", "reminder_type", "TEXT", "greeting")

print("=== v18: distill_status ===")
add_col("episodic_memories", "distill_status", "TEXT", "")
conn.execute("UPDATE episodic_memories SET distill_status = 'failed' WHERE emotion_label = 'distill_failed'")
print("  backfilled distill_status")

# Update schema_version
now = time.time()
for v in range(10, 19):
    conn.execute("INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)", (v, now))

conn.commit()

# Verify
epi2 = cols("episodic_memories")
sv = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
print("\n=== Result ===")
print(f"Schema version: {sv}")
print(f"Has phase: {'phase' in epi2}")
print(f"Has distill_status: {'distill_status' in epi2}")
print(f"All cols: {epi2}")
conn.close()
print("DONE")

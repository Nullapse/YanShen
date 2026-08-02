import re
import shutil
import sqlite3
import time
from pathlib import Path

CURRENT_SCHEMA_VERSION = 6
BUILT_IN_QUESTION_PREFIX = "GKS-"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL,
    errors_json TEXT NOT NULL DEFAULT '[]',
    question_count INTEGER NOT NULL DEFAULT 0,
    answer_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_code TEXT NOT NULL UNIQUE,
    paper_name TEXT NOT NULL,
    paper_category TEXT NOT NULL DEFAULT '',
    exam_type TEXT NOT NULL,
    year INTEGER NOT NULL,
    region TEXT NOT NULL,
    source_province TEXT NOT NULL DEFAULT '',
    target_group TEXT NOT NULL DEFAULT '',
    zhejiang_relevance INTEGER NOT NULL DEFAULT 3,
    source_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_code TEXT NOT NULL UNIQUE,
    paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    paper_name TEXT NOT NULL DEFAULT '',
    paper_category TEXT NOT NULL DEFAULT '',
    question_number INTEGER NOT NULL DEFAULT 0,
    exam_type TEXT NOT NULL,
    year INTEGER NOT NULL,
    region TEXT NOT NULL,
    source_province TEXT NOT NULL DEFAULT '',
    zhejiang_relevance INTEGER NOT NULL DEFAULT 3,
    question_type TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    original_text TEXT NOT NULL DEFAULT '',
    materials TEXT NOT NULL,
    requirements TEXT NOT NULL,
    word_limit TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    is_full_original INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    source_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS paper_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    material_number INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(paper_id, material_number)
);

CREATE TABLE IF NOT EXISTS question_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '',
    extracted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(question_id, provider, source_name, source_path, source_url)
);

CREATE TABLE IF NOT EXISTS reference_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    organization TEXT NOT NULL,
    canonical_organization TEXT NOT NULL DEFAULT '',
    answer_text TEXT NOT NULL,
    scoring_points TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    import_id INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    is_reviewed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(question_id, organization)
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    answer_text TEXT NOT NULL,
    answer_format_json TEXT NOT NULL DEFAULT '[]',
    word_count INTEGER NOT NULL DEFAULT 0,
    grading_result TEXT NOT NULL DEFAULT '',
    grading_references_configured INTEGER NOT NULL DEFAULT 0,
    grading_reference_ids TEXT NOT NULL DEFAULT '[]',
    custom_reference_answer TEXT NOT NULL DEFAULT '',
    personal_note TEXT NOT NULL DEFAULT '',
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    paper_elapsed_seconds INTEGER NOT NULL DEFAULT 0,
    paper_time_excluded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS grading_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    report_text TEXT NOT NULL,
    prompt_text TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS grading_rubrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    reference_set_hash TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    rubric_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('building', 'ready', 'failed', 'stale')),
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(question_id, reference_set_hash, source_hash, rubric_version)
);

CREATE INDEX IF NOT EXISTS idx_grading_rubrics_question
ON grading_rubrics(question_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS grading_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'preparing', 'building_rubric', 'reusing_rubric', 'retrieving',
                   'grading', 'validating', 'repairing_answer', 'completed', 'failed', 'interrupted')
    ),
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    report_id INTEGER REFERENCES grading_reports(id) ON DELETE SET NULL,
    retryable INTEGER NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_grading_jobs_attempt
ON grading_jobs(attempt_id, created_at DESC);

CREATE TABLE IF NOT EXISTS grading_report_contexts (
    report_id INTEGER PRIMARY KEY REFERENCES grading_reports(id) ON DELETE CASCADE,
    rubric_id INTEGER REFERENCES grading_rubrics(id) ON DELETE SET NULL,
    pipeline_version TEXT NOT NULL,
    retrieval_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT NOT NULL DEFAULT '{}',
    validation_json TEXT NOT NULL DEFAULT '{}',
    rubric_snapshot_json TEXT NOT NULL DEFAULT '{}',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS grading_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES grading_reports(id) ON DELETE CASCADE,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    point_key TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'report' CHECK (scope IN ('report', 'question')),
    corrected_status TEXT NOT NULL DEFAULT '' CHECK (corrected_status IN ('', 'hit', 'partial', 'miss', 'invalid')),
    corrected_quote TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(report_id, point_key, scope)
);

CREATE INDEX IF NOT EXISTS idx_grading_feedback_question
ON grading_feedback(question_id, point_key, scope, updated_at DESC);

CREATE TABLE IF NOT EXISTS question_favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL UNIQUE REFERENCES questions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS paper_favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS text_annotations (
    annotation_key TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK (target_type IN ('material', 'answer', 'note')),
    question_id INTEGER REFERENCES questions(id) ON DELETE CASCADE,
    material_number INTEGER,
    attempt_id INTEGER REFERENCES attempts(id) ON DELETE CASCADE,
    text_hash TEXT NOT NULL DEFAULT '',
    annotations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (target_type = 'material' AND question_id IS NOT NULL AND material_number IS NOT NULL AND attempt_id IS NULL)
        OR
        (target_type IN ('answer', 'note') AND question_id IS NULL AND material_number IS NULL AND attempt_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS ai_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'api',
    provider_name TEXT NOT NULL DEFAULT 'DeepSeek',
    api_base_url TEXT NOT NULL DEFAULT 'https://api.deepseek.com',
    api_key TEXT NOT NULL DEFAULT '',
    api_key_env TEXT NOT NULL DEFAULT 'DEEPSEEK_API_KEY',
    model TEXT NOT NULL DEFAULT 'deepseek-v4-pro',
    temperature REAL NOT NULL DEFAULT 0.2,
    prompt_template TEXT NOT NULL DEFAULT '',
    grading_mode TEXT NOT NULL DEFAULT 'enhanced',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_ai_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    use_grading_api INTEGER NOT NULL DEFAULT 1 CHECK (use_grading_api IN (0, 1)),
    provider_name TEXT NOT NULL DEFAULT 'DeepSeek',
    api_base_url TEXT NOT NULL DEFAULT 'https://api.deepseek.com',
    api_key TEXT NOT NULL DEFAULT '',
    api_key_env TEXT NOT NULL DEFAULT 'DEEPSEEK_API_KEY',
    model TEXT NOT NULL DEFAULT 'deepseek-v4-pro',
    temperature REAL NOT NULL DEFAULT 0.2,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    subject_type TEXT NOT NULL DEFAULT '',
    subject_id INTEGER,
    status TEXT NOT NULL DEFAULT 'created',
    user_goal TEXT NOT NULL DEFAULT '',
    input_summary TEXT NOT NULL DEFAULT '',
    final_text TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    tool_name TEXT NOT NULL DEFAULT '',
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS training_plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL,
    question_id INTEGER REFERENCES questions(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    target_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    entrypoint TEXT NOT NULL DEFAULT 'chat',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_type TEXT NOT NULL DEFAULT 'text',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type TEXT NOT NULL,
    memory_key TEXT NOT NULL,
    content TEXT NOT NULL,
    source_conversation_id INTEGER REFERENCES agent_conversations(id) ON DELETE SET NULL,
    source_message_id INTEGER REFERENCES agent_messages(id) ON DELETE SET NULL,
    confidence REAL NOT NULL DEFAULT 0.8,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(memory_type, memory_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_memories_status_type
ON agent_memories(status, memory_type, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_eval_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suite_name TEXT NOT NULL,
    case_id TEXT NOT NULL,
    case_title TEXT NOT NULL,
    task_type TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_context_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    attempt_id INTEGER,
    question_id INTEGER,
    question_type TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    year INTEGER,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    score REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS agent_context_fts
USING fts5(title, body, content='agent_context_chunks', content_rowid='id');

CREATE TABLE IF NOT EXISTS agent_context_vectors (
    chunk_id INTEGER PRIMARY KEY REFERENCES agent_context_chunks(id) ON DELETE CASCADE,
    vector_json TEXT NOT NULL,
    norm REAL NOT NULL DEFAULT 0,
    embedding_model TEXT NOT NULL DEFAULT 'feature-hash-v1',
    dimensions INTEGER NOT NULL DEFAULT 128,
    content_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_context_dense_vectors (
    chunk_id INTEGER NOT NULL REFERENCES agent_context_chunks(id) ON DELETE CASCADE,
    embedding_model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chunk_id, embedding_model)
);

CREATE INDEX IF NOT EXISTS idx_agent_context_dense_model
ON agent_context_dense_vectors(embedding_model, dimensions, chunk_id);

CREATE TABLE IF NOT EXISTS agent_skill_nodes (
    skill_key TEXT PRIMARY KEY,
    module TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_skill_edges (
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    skill_key TEXT NOT NULL REFERENCES agent_skill_nodes(skill_key) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_type, source_key, skill_key, relation)
);

CREATE INDEX IF NOT EXISTS idx_agent_skill_edges_skill
ON agent_skill_edges(skill_key, relation, source_type);

CREATE TABLE IF NOT EXISTS agent_context_index_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    dirty INTEGER NOT NULL DEFAULT 1,
    full_rebuild INTEGER NOT NULL DEFAULT 1,
    knowledge_signature TEXT NOT NULL DEFAULT '',
    embedding_model TEXT NOT NULL DEFAULT 'feature-hash-v1',
    embedding_dimensions INTEGER NOT NULL DEFAULT 128,
    rebuilt_at TEXT
);

INSERT OR IGNORE INTO agent_context_index_state (id, dirty, full_rebuild) VALUES (1, 1, 1);

CREATE TABLE IF NOT EXISTS agent_context_pending (
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
    queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    next_retry_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (source_type, source_id)
);

CREATE TABLE IF NOT EXISTS agent_context_worker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'idle',
    current_type TEXT NOT NULL DEFAULT '',
    processed_count INTEGER NOT NULL DEFAULT 0,
    total_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO agent_context_worker_state (id) VALUES (1);

DROP TRIGGER IF EXISTS trg_agent_context_attempts_insert;
DROP TRIGGER IF EXISTS trg_agent_context_attempts_update;
DROP TRIGGER IF EXISTS trg_agent_context_attempts_delete;
DROP TRIGGER IF EXISTS trg_agent_context_reports_insert;
DROP TRIGGER IF EXISTS trg_agent_context_reports_update;
DROP TRIGGER IF EXISTS trg_agent_context_reports_delete;
DROP TRIGGER IF EXISTS trg_agent_context_questions_insert;
DROP TRIGGER IF EXISTS trg_agent_context_questions_update;
DROP TRIGGER IF EXISTS trg_agent_context_questions_delete;
DROP TRIGGER IF EXISTS trg_agent_context_materials_insert;
DROP TRIGGER IF EXISTS trg_agent_context_materials_update;
DROP TRIGGER IF EXISTS trg_agent_context_materials_delete;
DROP TRIGGER IF EXISTS trg_agent_context_references_insert;
DROP TRIGGER IF EXISTS trg_agent_context_references_update;
DROP TRIGGER IF EXISTS trg_agent_context_references_delete;

CREATE TRIGGER trg_agent_context_attempts_insert AFTER INSERT ON attempts BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('attempt', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_attempts_update AFTER UPDATE ON attempts BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('attempt', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_attempts_delete AFTER DELETE ON attempts BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('attempt', OLD.id, 'delete')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'delete', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_reports_insert AFTER INSERT ON grading_reports BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('grading_report', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_reports_update AFTER UPDATE ON grading_reports BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('grading_report', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_reports_delete AFTER DELETE ON grading_reports BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('grading_report', OLD.id, 'delete')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'delete', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_questions_insert AFTER INSERT ON questions BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('question', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_questions_update AFTER UPDATE ON questions BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('question', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_questions_delete AFTER DELETE ON questions BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('question', OLD.id, 'delete')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'delete', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_materials_insert AFTER INSERT ON paper_materials BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('material', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_materials_update AFTER UPDATE ON paper_materials BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('material', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_materials_delete AFTER DELETE ON paper_materials BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('material', OLD.id, 'delete')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'delete', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_references_insert AFTER INSERT ON reference_answers BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('reference_answer', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_references_update AFTER UPDATE ON reference_answers BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('reference_answer', NEW.id, 'upsert')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'upsert', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;
CREATE TRIGGER trg_agent_context_references_delete AFTER DELETE ON reference_answers BEGIN
    INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('reference_answer', OLD.id, 'delete')
    ON CONFLICT(source_type, source_id) DO UPDATE SET operation = 'delete', queued_at = CURRENT_TIMESTAMP;
    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
END;

CREATE TABLE IF NOT EXISTS agent_weakness_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module TEXT NOT NULL,
    question_type TEXT NOT NULL DEFAULT '',
    problem_type TEXT NOT NULL,
    frequency INTEGER NOT NULL DEFAULT 0,
    severity REAL NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(module, question_type, problem_type)
);

CREATE TABLE IF NOT EXISTS coverage_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    exam_type TEXT NOT NULL,
    region TEXT NOT NULL,
    source_province TEXT NOT NULL DEFAULT '',
    paper_name TEXT NOT NULL,
    target_group TEXT NOT NULL,
    zhejiang_relevance INTEGER NOT NULL DEFAULT 3,
    priority INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT '待录入原文',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(year, exam_type, region, paper_name)
);

CREATE TABLE IF NOT EXISTS release_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

REQUIRED_SCHEMA_TABLES = frozenset(
    re.findall(
        r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
        SCHEMA,
        flags=re.IGNORECASE,
    )
)

# These three index/settings structures were added while the public database still used schema v4.
# Keep the migration explicit: known additive changes are created in place;
# any other missing current-version table is treated as corruption.
SCHEMA_V4_ADDITIONS = """
CREATE TABLE IF NOT EXISTS agent_ai_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    use_grading_api INTEGER NOT NULL DEFAULT 1 CHECK (use_grading_api IN (0, 1)),
    provider_name TEXT NOT NULL DEFAULT 'DeepSeek',
    api_base_url TEXT NOT NULL DEFAULT 'https://api.deepseek.com',
    api_key TEXT NOT NULL DEFAULT '',
    api_key_env TEXT NOT NULL DEFAULT 'DEEPSEEK_API_KEY',
    model TEXT NOT NULL DEFAULT 'deepseek-v4-pro',
    temperature REAL NOT NULL DEFAULT 0.2,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_context_vectors (
    chunk_id INTEGER PRIMARY KEY REFERENCES agent_context_chunks(id) ON DELETE CASCADE,
    vector_json TEXT NOT NULL,
    norm REAL NOT NULL DEFAULT 0,
    embedding_model TEXT NOT NULL DEFAULT 'feature-hash-v1',
    dimensions INTEGER NOT NULL DEFAULT 128,
    content_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS agent_context_fts
USING fts5(title, body, content='agent_context_chunks', content_rowid='id');

CREATE TABLE IF NOT EXISTS agent_context_worker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'idle',
    current_type TEXT NOT NULL DEFAULT '',
    processed_count INTEGER NOT NULL DEFAULT 0,
    total_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO agent_context_worker_state (id) VALUES (1);
"""
SCHEMA_V4_ADDITIVE_TABLES = frozenset(
    {
        "agent_ai_settings",
        "agent_context_vectors",
        "agent_context_fts",
        "agent_context_worker_state",
    }
)


TARGET_SEED = [
    ("国考", "全国", "全国", "国考申论卷组", "国考", 3, 2, "待核对卷种后录入原文"),
    ("浙江省考", "浙江", "浙江", "浙江省考申论卷组", "核心题库", 5, 1, "已录入完整题目原文和机构答案"),
    ("浙江选调", "浙江", "浙江", "浙江选调申论/综合写作", "核心题库", 5, 1, "已录入完整题目原文和机构答案"),
    ("江苏省考", "江苏", "江苏", "江苏省考申论卷组", "拓展题库", 4, 3, "作为跨省训练材料"),
    ("上海市考", "上海", "上海", "上海市考申论卷组", "拓展题库", 4, 3, "作为跨省训练材料"),
    ("山东省考", "山东", "山东", "山东省考申论卷组", "拓展题库", 4, 3, "作为跨省训练材料"),
    ("广东省考", "广东", "广东", "广东省考申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
    ("安徽省考", "安徽", "安徽", "安徽省考申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
    ("河北省考", "河北", "河北", "河北省考申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
    ("河南省考", "河南", "河南", "河南省考申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
    ("河南选调", "河南", "河南", "河南选调申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
    ("湖北省考", "湖北", "湖北", "湖北省考申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
    ("湖北选调", "湖北", "湖北", "湖北选调申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
    ("湖南省考", "湖南", "湖南", "湖南省考申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
    ("湖南选调", "湖南", "湖南", "湖南选调申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
    ("四川省考", "四川", "四川", "四川省考申论卷组", "拓展题库", 3, 4, "作为跨省训练材料"),
]


def get_db_path(app):
    return Path(app.instance_path) / "gongkao.sqlite3"


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=60, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _table_names(conn):
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _ensure_index_queue_triggers(conn):
    sources = (
        ("attempts", "attempt", "attempts"),
        ("grading_reports", "grading_report", "reports"),
        ("questions", "question", "questions"),
        ("paper_materials", "material", "materials"),
        ("reference_answers", "reference_answer", "references"),
    )
    for table, source_type, trigger_prefix in sources:
        for event, row_ref, operation in (
            ("insert", "NEW", "upsert"),
            ("update", "NEW", "upsert"),
            ("delete", "OLD", "delete"),
        ):
            trigger = f"trg_agent_context_{trigger_prefix}_{event}"
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute(
                f"""
                CREATE TRIGGER {trigger} AFTER {event.upper()} ON {table} BEGIN
                    INSERT INTO agent_context_pending (
                        source_type, source_id, operation, queued_at,
                        retry_count, last_error, next_retry_at, status
                    ) VALUES (
                        '{source_type}', {row_ref}.id, '{operation}', CURRENT_TIMESTAMP,
                        0, '', NULL, 'pending'
                    )
                    ON CONFLICT(source_type, source_id) DO UPDATE SET
                        operation = excluded.operation,
                        queued_at = CURRENT_TIMESTAMP,
                        retry_count = 0,
                        last_error = '',
                        next_retry_at = NULL,
                        status = 'pending';
                    UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1;
                END
                """
            )


def _ensure_index_worker_schema(conn):
    pending_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(agent_context_pending)")
    }
    additions = (
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_error", "TEXT NOT NULL DEFAULT ''"),
        ("next_retry_at", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'pending'"),
    )
    for name, declaration in additions:
        if name not in pending_columns:
            conn.execute(
                f"ALTER TABLE agent_context_pending ADD COLUMN {name} {declaration}"
            )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_context_worker_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL DEFAULT 'idle',
            current_type TEXT NOT NULL DEFAULT '',
            processed_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO agent_context_worker_state (id) VALUES (1)")
    _ensure_index_queue_triggers(conn)


def init_db(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(5):
        try:
            with connect(db_path) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                existing_tables = _table_names(conn)
                if version == 0 and existing_tables:
                    raise RuntimeError(
                        f"unsupported database schema {version}; expected {CURRENT_SCHEMA_VERSION}"
                    )
                if version not in {0, 4, 5, CURRENT_SCHEMA_VERSION}:
                    raise RuntimeError(
                        f"unsupported database schema {version}; expected {CURRENT_SCHEMA_VERSION}"
                    )
                missing_tables = REQUIRED_SCHEMA_TABLES - existing_tables
                if version == 0 or not existing_tables:
                    conn.executescript(SCHEMA)
                elif missing_tables:
                    unknown_missing = missing_tables - SCHEMA_V4_ADDITIVE_TABLES
                    if unknown_missing:
                        names = ", ".join(sorted(unknown_missing))
                        raise RuntimeError(f"current database schema is incomplete; missing tables: {names}")
                    conn.executescript(SCHEMA_V4_ADDITIONS)
                if version == 4:
                    attempt_columns = {
                        row["name"] for row in conn.execute("PRAGMA table_info(attempts)")
                    }
                    if "answer_format_json" not in attempt_columns:
                        conn.execute(
                            "ALTER TABLE attempts ADD COLUMN answer_format_json TEXT NOT NULL DEFAULT '[]'"
                        )
                attempt_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(attempts)")
                }
                if "answer_format_json" not in attempt_columns:
                    raise RuntimeError("current database schema is incomplete; missing attempts.answer_format_json")
                _ensure_index_worker_schema(conn)
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                except Exception:
                    pass
                seed_ai_settings(conn)
                seed_agent_ai_settings(conn)
                seed_coverage_targets(conn)
                conn.execute(
                    """
                    UPDATE grading_jobs
                       SET status = 'interrupted',
                           message = '应用关闭导致任务中断，可重新发起批改。',
                           retryable = 1,
                           finished_at = CURRENT_TIMESTAMP
                     WHERE status IN ('queued', 'preparing', 'building_rubric', 'reusing_rubric',
                                      'retrieving', 'grading', 'validating', 'repairing_answer')
                    """
                )
                conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            break
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < 4:
                time.sleep(0.5 * (attempt + 1))
            else:
                raise


def prepare_user_database(db_path, seed_path=None):
    db_path = Path(db_path)
    seed_path = Path(seed_path) if seed_path else None
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists() and seed_path and seed_path.exists():
        shutil.copy2(seed_path, db_path)
    init_db(db_path)
    if seed_path and seed_path.exists() and seed_path.resolve() != db_path.resolve():
        sync_seed_content(db_path, seed_path)
        # Recreate triggers dropped during sync
        init_db(db_path)
    return db_path


def _upsert(conn, table, values, conflict_columns, excluded_columns=("id",)):
    columns = [column for column in values.keys() if column not in excluded_columns]
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE "
        + " AND ".join(f"{column} = ?" for column in conflict_columns)
        + " LIMIT 1",
        [values[column] for column in conflict_columns],
    ).fetchone()
    if existing is not None and all(existing[column] == values[column] for column in columns):
        return False
    placeholders = ", ".join("?" for _ in columns)
    update_columns = [column for column in columns if column not in conflict_columns]
    update_sql = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    if update_sql:
        sql += f" ON CONFLICT ({', '.join(conflict_columns)}) DO UPDATE SET {update_sql}"
    else:
        sql += f" ON CONFLICT ({', '.join(conflict_columns)}) DO NOTHING"
    conn.execute(sql, [values[column] for column in columns])
    return True


def _remove_obsolete_seed_questions(conn, current_codes):
    candidate_paper_ids = set()
    built_in_prefixes = (BUILT_IN_QUESTION_PREFIX,)
    prefix_filter = " OR ".join("question_code LIKE ?" for _ in built_in_prefixes)
    for row in conn.execute(
        f"""
        SELECT id, paper_id, question_code
          FROM questions
         WHERE {prefix_filter}
        """,
        tuple(f"{prefix}%" for prefix in built_in_prefixes),
    ):
        if row["question_code"] in current_codes:
            continue
        question_id = row["id"]
        has_personal_data = any(
            conn.execute(sql, (question_id,)).fetchone()
            for sql in (
                "SELECT 1 FROM attempts WHERE question_id = ? LIMIT 1",
                "SELECT 1 FROM question_favorites WHERE question_id = ? LIMIT 1",
                "SELECT 1 FROM training_plan_items WHERE question_id = ? LIMIT 1",
                "SELECT 1 FROM agent_runs WHERE subject_type = 'question' AND subject_id = ? LIMIT 1",
            )
        )
        if has_personal_data:
            continue
        chunk_ids = [
            chunk["id"]
            for chunk in conn.execute(
                "SELECT id FROM agent_context_chunks WHERE question_id = ?", (question_id,)
            )
        ]
        conn.executemany("DELETE FROM agent_context_fts WHERE rowid = ?", [(value,) for value in chunk_ids])
        conn.executemany("DELETE FROM agent_context_chunks WHERE id = ?", [(value,) for value in chunk_ids])
        conn.execute(
            "DELETE FROM agent_context_pending WHERE source_type = 'question' AND source_id = ?",
            (question_id,),
        )
        conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))
        if row["paper_id"] is not None:
            candidate_paper_ids.add(row["paper_id"])

    for paper_id in candidate_paper_ids:
        if conn.execute("SELECT 1 FROM questions WHERE paper_id = ? LIMIT 1", (paper_id,)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM paper_favorites WHERE paper_id = ? LIMIT 1", (paper_id,)).fetchone():
            continue
        conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))


def sync_seed_content(db_path, seed_path):
    source = sqlite3.connect(seed_path)
    source.row_factory = sqlite3.Row
    try:
        with connect(db_path) as dest:
            # Temporarily drop index triggers to avoid populating huge queue
            for trigger in [
                "trg_agent_context_questions_insert", "trg_agent_context_questions_update", "trg_agent_context_questions_delete",
                "trg_agent_context_materials_insert", "trg_agent_context_materials_update", "trg_agent_context_materials_delete",
                "trg_agent_context_references_insert", "trg_agent_context_references_update", "trg_agent_context_references_delete"
            ]:
                dest.execute(f"DROP TRIGGER IF EXISTS {trigger}")

            paper_map = {}
            changed_material_ids = set()
            changed_question_ids = set()
            changed_reference_ids = set()
            for row in source.execute("SELECT * FROM papers ORDER BY id"):
                values = dict(row)
                _upsert(dest, "papers", values, ("paper_code",))
                paper_map[row["id"]] = dest.execute(
                    "SELECT id FROM papers WHERE paper_code = ?", (row["paper_code"],)
                ).fetchone()["id"]

            for row in source.execute("SELECT * FROM paper_materials ORDER BY id"):
                values = dict(row)
                values["paper_id"] = paper_map[row["paper_id"]]
                changed = _upsert(dest, "paper_materials", values, ("paper_id", "material_number"))
                material_id = dest.execute(
                    "SELECT id FROM paper_materials WHERE paper_id = ? AND material_number = ?",
                    (values["paper_id"], values["material_number"]),
                ).fetchone()["id"]
                if changed:
                    changed_material_ids.add(material_id)

            question_map = {}
            current_question_codes = set()
            for row in source.execute("SELECT * FROM questions ORDER BY id"):
                values = dict(row)
                values["paper_id"] = paper_map.get(row["paper_id"])
                changed = _upsert(dest, "questions", values, ("question_code",))
                current_question_codes.add(row["question_code"])
                question_map[row["id"]] = dest.execute(
                    "SELECT id FROM questions WHERE question_code = ?", (row["question_code"],)
                ).fetchone()["id"]
                if changed:
                    changed_question_ids.add(question_map[row["id"]])

            for row in source.execute("SELECT * FROM reference_answers ORDER BY id"):
                values = dict(row)
                values["question_id"] = question_map[row["question_id"]]
                values["import_id"] = None
                changed = _upsert(dest, "reference_answers", values, ("question_id", "organization"))
                reference_id = dest.execute(
                    "SELECT id FROM reference_answers WHERE question_id = ? AND organization = ?",
                    (values["question_id"], values["organization"]),
                ).fetchone()["id"]
                if changed:
                    changed_reference_ids.add(reference_id)

            for row in source.execute("SELECT * FROM question_sources ORDER BY id"):
                values = dict(row)
                values["question_id"] = question_map[row["question_id"]]
                _upsert(
                    dest,
                    "question_sources",
                    values,
                    ("question_id", "provider", "source_name", "source_path", "source_url"),
                )

            for row in source.execute("SELECT * FROM coverage_targets ORDER BY id"):
                _upsert(
                    dest,
                    "coverage_targets",
                    dict(row),
                    ("year", "exam_type", "region", "paper_name"),
                )

            for row in source.execute("SELECT * FROM release_metadata"):
                _upsert(dest, "release_metadata", dict(row), ("key",), excluded_columns=())

            _remove_obsolete_seed_questions(dest, current_question_codes)

            # Manually clean up chunks for deleted questions, materials, answers
            dest.execute("DELETE FROM agent_context_chunks WHERE source_type = 'question' AND source_id NOT IN (SELECT id FROM questions)")
            dest.execute("DELETE FROM agent_context_chunks WHERE source_type = 'reference_answer' AND source_id NOT IN (SELECT id FROM reference_answers)")
            dest.execute("DELETE FROM agent_context_chunks WHERE source_type = 'material' AND source_id NOT IN (SELECT id FROM paper_materials)")
            dest.execute("DELETE FROM agent_context_pending WHERE source_type = 'question' AND source_id NOT IN (SELECT id FROM questions)")
            dest.execute("DELETE FROM agent_context_pending WHERE source_type = 'reference_answer' AND source_id NOT IN (SELECT id FROM reference_answers)")
            dest.execute("DELETE FROM agent_context_pending WHERE source_type = 'material' AND source_id NOT IN (SELECT id FROM paper_materials)")

            indexed_count = dest.execute(
                "SELECT COUNT(*) FROM agent_context_chunks"
            ).fetchone()[0]
            if indexed_count == 0:
                # A fresh database needs one background rebuild, not one queue
                # record for every seed question, material, and reference.
                dest.execute(
                    "DELETE FROM agent_context_pending WHERE source_type IN "
                    "('question', 'material', 'reference_answer')"
                )
                dest.execute(
                    "UPDATE agent_context_index_state "
                    "SET dirty = 1, full_rebuild = 1 WHERE id = 1"
                )
            else:
                pending_rows = [
                    ("question", source_id, "upsert")
                    for source_id in changed_question_ids
                ]
                pending_rows.extend(
                    ("material", source_id, "upsert")
                    for source_id in changed_material_ids
                )
                pending_rows.extend(
                    ("reference_answer", source_id, "upsert")
                    for source_id in changed_reference_ids
                )
                dest.executemany(
                    """
                    INSERT INTO agent_context_pending (source_type, source_id, operation)
                    VALUES (?, ?, ?)
                    ON CONFLICT(source_type, source_id) DO UPDATE SET
                        operation = excluded.operation,
                        queued_at = CURRENT_TIMESTAMP,
                        retry_count = 0,
                        last_error = '',
                        next_retry_at = NULL,
                        status = 'pending'
                    """,
                    pending_rows,
                )
                for source_type, table in (
                    ("question", "questions"),
                    ("reference_answer", "reference_answers"),
                    ("material", "paper_materials"),
                ):
                    dest.execute(
                        f"""
                        INSERT OR IGNORE INTO agent_context_pending
                            (source_type, source_id, operation)
                        SELECT ?, id, 'upsert' FROM {table}
                         WHERE id NOT IN (
                             SELECT source_id FROM agent_context_chunks
                              WHERE source_type = ?
                         )
                        """,
                        (source_type, source_type),
                    )
                queued_count = dest.execute(
                    "SELECT COUNT(*) FROM agent_context_pending"
                ).fetchone()[0]
                if pending_rows or queued_count:
                    dest.execute(
                        "UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1"
                    )
    finally:
        source.close()


def seed_ai_settings(conn):
    conn.execute(
        """
        INSERT OR IGNORE INTO ai_settings (
            id, mode, provider_name, api_base_url, api_key_env, model, temperature
        ) VALUES (1, 'api', 'DeepSeek', 'https://api.deepseek.com', 'DEEPSEEK_API_KEY', 'deepseek-v4-pro', 0.2)
        """
    )


def seed_agent_ai_settings(conn):
    conn.execute(
        """
        INSERT OR IGNORE INTO agent_ai_settings (
            id, use_grading_api, provider_name, api_base_url, api_key_env, model, temperature
        ) VALUES (1, 1, 'DeepSeek', 'https://api.deepseek.com', 'DEEPSEEK_API_KEY', 'deepseek-v4-pro', 0.2)
        """
    )


def seed_coverage_targets(conn):
    for year in range(2020, 2027):
        for exam_type, region, province, paper_name, target_group, relevance, priority, notes in TARGET_SEED:
            conn.execute(
                """
                INSERT OR IGNORE INTO coverage_targets (
                    year, exam_type, region, source_province, paper_name,
                    target_group, zhejiang_relevance, priority, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (year, exam_type, region, province, paper_name, target_group, relevance, priority, notes),
            )

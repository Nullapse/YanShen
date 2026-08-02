import json
import logging
import os
from hashlib import blake2b, sha256

from ..db import connect
from ..knowledge_cards import load_knowledge_cards
from ..paths import resource_root, user_data_dir
from ..skill_graph import rebuild_skill_graph
from ..statistics import parse_report_score
from .catalog import module_definition, valid_module_id
from .embeddings import FEATURE_HASH_MODEL, VECTOR_DIM
from .embeddings import embed_text as _embed_text
from .query import where_for_scope as _where_for_scope

BGE_MODEL = "BAAI/bge-small-zh-v1.5"
BGE_DIMENSIONS = 512
_DENSE_MODELS = {}
STATIC_REBUILD_THRESHOLD = 256
MAX_INDEX_RETRIES = 3


def _clean(value, limit=1800):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."

def _dense_model_cache_dir():
    path = user_data_dir() / "embedding_models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_dense_model(model_name=BGE_MODEL, allow_download=False):
    cache_key = (model_name, bool(allow_download))
    if cache_key in _DENSE_MODELS:
        return _DENSE_MODELS[cache_key]
    try:
        from fastembed import TextEmbedding

        model = TextEmbedding(
            model_name=model_name,
            cache_dir=str(_dense_model_cache_dir()),
            local_files_only=not allow_download,
        )
    except Exception:
        return None
    _DENSE_MODELS[cache_key] = model
    return model


def configure_dense_embedding(conn, enabled=True, allow_download=True):
    if not enabled:
        conn.execute(
            "UPDATE agent_context_index_state SET embedding_model = ?, embedding_dimensions = ? WHERE id = 1",
            (FEATURE_HASH_MODEL, VECTOR_DIM),
        )
        return {"enabled": False, "model": FEATURE_HASH_MODEL, "dimensions": VECTOR_DIM}
    model = _load_dense_model(BGE_MODEL, allow_download=allow_download)
    if model is None:
        raise RuntimeError("无法加载中文 embedding 模型 BAAI/bge-small-zh-v1.5")
    conn.execute(
        "UPDATE agent_context_index_state SET embedding_model = ?, embedding_dimensions = ? WHERE id = 1",
        (BGE_MODEL, BGE_DIMENSIONS),
    )
    return {"enabled": True, "model": BGE_MODEL, "dimensions": BGE_DIMENSIONS}


def _active_embedding(conn):
    try:
        row = conn.execute(
            "SELECT embedding_model, embedding_dimensions FROM agent_context_index_state WHERE id = 1"
        ).fetchone()
    except Exception:
        return FEATURE_HASH_MODEL, VECTOR_DIM
    return (
        (row["embedding_model"] or FEATURE_HASH_MODEL) if row else FEATURE_HASH_MODEL,
        int(row["embedding_dimensions"] or VECTOR_DIM) if row else VECTOR_DIM,
    )


def _dense_query_embedding(conn, text):
    model_name, dimensions = _active_embedding(conn)
    if model_name == FEATURE_HASH_MODEL:
        vector, _ = _embed_text(text)
        return vector, FEATURE_HASH_MODEL
    model = _load_dense_model(model_name, allow_download=False)
    if model is None:
        vector, _ = _embed_text(text)
        return vector, FEATURE_HASH_MODEL
    vector = next(iter(model.query_embed([text]))).tolist()
    if len(vector) != dimensions:
        raise RuntimeError(f"embedding dimension mismatch: expected {dimensions}, got {len(vector)}")
    return [round(float(value), 8) for value in vector], model_name


def sync_dense_embeddings(conn, batch_size=32, limit=None, sync_vector_index=True):
    model_name, dimensions = _active_embedding(conn)
    if model_name == FEATURE_HASH_MODEL:
        return {"available": False, "model": FEATURE_HASH_MODEL, "updated": 0, "remaining": 0}
    model = _load_dense_model(model_name, allow_download=False)
    if model is None:
        return {"available": False, "model": model_name, "updated": 0, "remaining": -1}
    sql = """
        SELECT c.id, c.title, c.body, c.content_hash
          FROM agent_context_chunks c
     LEFT JOIN agent_context_dense_vectors v
            ON v.chunk_id = c.id AND v.embedding_model = ?
         WHERE v.chunk_id IS NULL OR v.content_hash <> c.content_hash
      ORDER BY c.id
    """
    params = [model_name]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    rows = conn.execute(sql, params).fetchall()
    updated = 0
    for start in range(0, len(rows), max(1, int(batch_size))):
        batch = rows[start : start + max(1, int(batch_size))]
        texts = [f"{row['title']}\n{row['body']}" for row in batch]
        vectors = list(model.passage_embed(texts))
        for row, vector in zip(batch, vectors):
            values = [round(float(value), 8) for value in vector.tolist()]
            if len(values) != dimensions:
                raise RuntimeError(f"embedding dimension mismatch: expected {dimensions}, got {len(values)}")
            conn.execute(
                """
                INSERT INTO agent_context_dense_vectors (
                    chunk_id, embedding_model, dimensions, vector_json, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chunk_id, embedding_model) DO UPDATE SET
                    dimensions = excluded.dimensions,
                    vector_json = excluded.vector_json,
                    content_hash = excluded.content_hash,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (row["id"], model_name, dimensions, json.dumps(values), row["content_hash"]),
            )
            updated += 1
        # Embedding inference can be slow on first use. Keep each database
        # write transaction bounded to one completed batch so normal answer
        # submissions are never blocked while the next batch is computed.
        conn.commit()
    remaining = conn.execute(
        """
        SELECT COUNT(*)
          FROM agent_context_chunks c
     LEFT JOIN agent_context_dense_vectors v
            ON v.chunk_id = c.id AND v.embedding_model = ?
         WHERE v.chunk_id IS NULL OR v.content_hash <> c.content_hash
        """,
        (model_name,),
    ).fetchone()[0]
    if updated and sync_vector_index:
        sync_sqlite_vec_index(conn, model_name, dimensions)
        conn.commit()
    return {"available": True, "model": model_name, "updated": updated, "remaining": remaining}


def _load_sqlite_vec(conn):
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass
        return False


def _vec_table_name(dimensions):
    dimensions = int(dimensions)
    if dimensions not in {128, 384, 512, 768, 1024, 1536}:
        raise ValueError(f"unsupported embedding dimension: {dimensions}")
    return f"agent_context_vec_{dimensions}_v2"


def sync_sqlite_vec_index(
    conn,
    embedding_model=FEATURE_HASH_MODEL,
    dimensions=VECTOR_DIM,
    commit_batch_size=None,
):
    if not _load_sqlite_vec(conn):
        return {"available": False, "inserted": 0, "deleted": 0, "table": ""}
    table = _vec_table_name(dimensions)
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(
            embedding float[{int(dimensions)}],
            source_type TEXT partition key,
            question_type TEXT,
            +content_hash TEXT
        )
        """
    )
    current = {
        int(row["rowid"]): row["content_hash"] or ""
        for row in conn.execute(f"SELECT rowid, content_hash FROM {table}")
    }
    vector_table = (
        "agent_context_vectors"
        if embedding_model == FEATURE_HASH_MODEL
        else "agent_context_dense_vectors"
    )
    expected_rows = conn.execute(
        f"""
        SELECT v.chunk_id, v.vector_json, v.content_hash, c.source_type, c.question_type
          FROM {vector_table} v
          JOIN agent_context_chunks c ON c.id = v.chunk_id
         WHERE embedding_model = ? AND dimensions = ?
        """,
        (embedding_model, int(dimensions)),
    ).fetchall()
    expected_ids = {int(row["chunk_id"]) for row in expected_rows}
    stale = sorted(set(current) - expected_ids)
    changed = [
        row
        for row in expected_rows
        if current.get(int(row["chunk_id"])) != (row["content_hash"] or "")
    ]
    writes_since_commit = 0
    for chunk_id in stale:
        conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (chunk_id,))
        writes_since_commit += 1
        if commit_batch_size and writes_since_commit >= int(commit_batch_size):
            conn.commit()
            writes_since_commit = 0
    for row in changed:
        chunk_id = int(row["chunk_id"])
        if chunk_id in current:
            conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (chunk_id,))
        conn.execute(
            f"INSERT INTO {table} (rowid, embedding, source_type, question_type, content_hash) VALUES (?, ?, ?, ?, ?)",
            (
                chunk_id,
                row["vector_json"],
                row["source_type"] or "unknown",
                row["question_type"] or "",
                row["content_hash"] or "",
            ),
        )
        writes_since_commit += 1
        if commit_batch_size and writes_since_commit >= int(commit_batch_size):
            conn.commit()
            writes_since_commit = 0
    if commit_batch_size and writes_since_commit:
        conn.commit()
    return {
        "available": True,
        "inserted": len(changed),
        "deleted": len(stale),
        "table": table,
        "count": len(expected_ids),
    }


def _sqlite_vec_search(conn, query_vector, scope, limit=120, embedding_model=FEATURE_HASH_MODEL):
    dimensions = len(query_vector)
    if not _load_sqlite_vec(conn):
        return []
    table = _vec_table_name(dimensions)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if not exists:
        status = sync_sqlite_vec_index(conn, embedding_model, dimensions)
        if not status["available"]:
            return []
    clauses, params = _where_for_scope(scope)
    where = " AND ".join(f"c.{clause}" for clause in clauses) if clauses else "1 = 1"
    vec_clauses = []
    vec_params = []
    source_types = [str(value) for value in (scope.get("source_types") or []) if str(value)]
    if len(source_types) == 1:
        vec_clauses.append("knn.source_type = ?")
        vec_params.append(source_types[0])
    question_type = (scope.get("filters") or {}).get("question_type")
    if question_type:
        vec_clauses.append("knn.question_type = ?")
        vec_params.append(question_type)
    vec_where = " AND ".join(vec_clauses) if vec_clauses else "1 = 1"
    rows = conn.execute(
        f"""
        SELECT c.*, knn.distance
          FROM {table} AS knn
          JOIN agent_context_chunks c ON c.id = knn.rowid
         WHERE knn.embedding MATCH ?
           AND k = ?
           AND {vec_where}
           AND {where}
      ORDER BY knn.distance
        """,
        (
            json.dumps(query_vector, ensure_ascii=False),
            max(int(limit), 40),
            *vec_params,
            *params,
        ),
    ).fetchall()
    output = []
    for row in rows[:limit]:
        item = dict(row)
        distance = max(0.0, float(item.pop("distance", 2.0)))
        item["_vector_score"] = max(0.0, min(1.0, 1.0 - distance / 2.0))
        item["_vector_backend"] = f"sqlite-vec:{embedding_model}"
        output.append(item)
    return output


def _content_hash(source_type, source_id, title, body, metadata=None):
    payload = json.dumps(
        {
            "source_type": source_type,
            "source_id": source_id,
            "title": title or "",
            "body": body or "",
            "metadata": metadata or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def knowledge_signature(items=None):
    items = items if items is not None else load_knowledge_items()
    payload = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _evidence_ref(row):
    source = row.get("source_type") or "context"
    source_id = row.get("source_id")
    if source == "knowledge":
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if metadata.get("knowledge_id"):
            return metadata["knowledge_id"]
    if row.get("attempt_id"):
        return f"{source}:attempt-{row['attempt_id']}"
    if row.get("question_id"):
        return f"{source}:question-{row['question_id']}"
    return f"{source}:{source_id}"


def _knowledge_source_id(knowledge_id):
    digest = blake2b(str(knowledge_id or "").encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") % 2_000_000_000


def load_knowledge_items():
    root = resource_root() / "knowledge"
    if not root.exists():
        return []
    include_private = os.environ.get("GONGKAO_PUBLIC_KNOWLEDGE_ONLY", "").strip() != "1"
    return load_knowledge_cards(root, include_private=include_private)


def _knowledge_body(item):
    parts = [
        item.get("content") or "",
        "示例：" + "；".join(item.get("examples") or []),
        "反例：" + "；".join(item.get("counterexamples") or []),
        "避坑：" + "；".join(item.get("pitfalls") or []),
        "适用：" + "；".join(item.get("applicable_when") or []),
        "不适用：" + "；".join(item.get("not_applicable_when") or []),
        "标签：" + " ".join(item.get("tags") or []),
    ]
    return "\n".join(part for part in parts if part.strip())


def _insert_knowledge_chunks(conn):
    for item in load_knowledge_items():
        module_id = valid_module_id(item.get("module") or "overview")
        definition = module_definition(module_id)
        _insert_chunk(
            conn,
            "knowledge",
            _knowledge_source_id(item["id"]),
            item["title"],
            _knowledge_body(item),
            question_type=definition.get("question_type") or "",
            metadata={
                "knowledge_id": item["id"],
                "module": module_id,
                "module_label": definition.get("label") or module_id,
                "tags": item.get("tags") or [],
                "kind": item.get("kind") or "",
                "source_version": item.get("version") or 1,
                "source_file": item.get("_source_file") or "",
                "skill": item.get("skill") or "",
                "difficulty": item.get("difficulty"),
                "review_status": (item.get("review") or {}).get("status") or "draft",
                "visibility": (item.get("source") or {}).get("visibility") or "public",
            },
        )


def _source_rank(source_type):
    return {
        "grading_report": 1.0,
        "knowledge": 0.96,
        "personal_note": 0.92,
        "attempt": 0.84,
        "reference_answer": 0.66,
        "material": 0.6,
        "question": 0.54,
    }.get(source_type, 0.45)


def _insert_chunk(conn, source_type, source_id, title, body, **metadata):
    body = _clean(body, 2200)
    if not body:
        return
    stored_metadata = metadata.get("metadata") or {}
    content_hash = _content_hash(
        source_type,
        source_id,
        title,
        body,
        {
            "attempt_id": metadata.get("attempt_id"),
            "question_id": metadata.get("question_id"),
            "question_type": metadata.get("question_type") or "",
            "region": metadata.get("region") or "",
            "year": metadata.get("year"),
            "score": metadata.get("score"),
            "created_at": metadata.get("created_at"),
            "metadata": stored_metadata,
        },
    )
    cursor = conn.execute(
        """
        INSERT INTO agent_context_chunks (
            source_type, source_id, attempt_id, question_id, question_type, region,
            year, title, body, score, created_at, metadata_json, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?)
        """,
        (
            source_type,
            source_id,
            metadata.get("attempt_id"),
            metadata.get("question_id"),
            metadata.get("question_type") or "",
            metadata.get("region") or "",
            metadata.get("year"),
            title or "",
            body,
            metadata.get("score"),
            metadata.get("created_at"),
            json.dumps(stored_metadata, ensure_ascii=False),
            content_hash,
        ),
    )
    conn.execute(
        "INSERT INTO agent_context_fts(rowid, title, body) VALUES (?, ?, ?)",
        (cursor.lastrowid, title or "", body),
    )
    vector, norm = _embed_text(" ".join([title or "", body or ""]))
    conn.execute(
        """
        INSERT OR REPLACE INTO agent_context_vectors (
            chunk_id, vector_json, norm, embedding_model, dimensions, content_hash, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            cursor.lastrowid,
            json.dumps(vector, ensure_ascii=False),
            norm,
            FEATURE_HASH_MODEL,
            len(vector),
            content_hash,
        ),
    )


def rebuild_agent_context_index(conn):
    """Rebuild the local retrieval index without holding a lock during inference."""
    conn.execute("DELETE FROM agent_context_fts")
    conn.execute("DELETE FROM agent_context_chunks")
    _insert_knowledge_chunks(conn)
    attempts = conn.execute(
        """
        SELECT a.*, q.title, q.question_type, q.region, q.year, q.question_code
          FROM attempts a
          JOIN questions q ON q.id = a.question_id
      ORDER BY a.created_at DESC, a.id DESC
        """
    ).fetchall()
    for row in attempts:
        title = f"{row['question_code']} {row['title']}"
        _insert_chunk(
            conn,
            "attempt",
            row["id"],
            title,
            row["answer_text"],
            attempt_id=row["id"],
            question_id=row["question_id"],
            question_type=row["question_type"],
            region=row["region"],
            year=row["year"],
            created_at=row["created_at"],
            metadata={"word_count": row["word_count"]},
        )
        if (row["personal_note"] or "").strip():
            _insert_chunk(
                conn,
                "personal_note",
                row["id"],
                f"{title} 复盘笔记",
                row["personal_note"],
                attempt_id=row["id"],
                question_id=row["question_id"],
                question_type=row["question_type"],
                region=row["region"],
                year=row["year"],
                created_at=row["created_at"],
            )
    reports = conn.execute(
        """
        SELECT gr.*, a.question_id, q.title, q.question_type, q.region, q.year, q.question_code
          FROM grading_reports gr
          JOIN attempts a ON a.id = gr.attempt_id
          JOIN questions q ON q.id = a.question_id
      ORDER BY gr.created_at DESC, gr.id DESC
        """
    ).fetchall()
    for row in reports:
        score = parse_report_score(row["report_text"])
        _insert_chunk(
            conn,
            "grading_report",
            row["id"],
            f"{row['question_code']} {row['title']} 批改报告",
            row["report_text"],
            attempt_id=row["attempt_id"],
            question_id=row["question_id"],
            question_type=row["question_type"],
            region=row["region"],
            year=row["year"],
            score=score,
            created_at=row["created_at"],
            metadata={"provider": row["provider"], "model": row["model"]},
        )
    questions = conn.execute("SELECT * FROM questions ORDER BY year DESC, id DESC").fetchall()
    for row in questions:
        _insert_chunk(
            conn,
            "question",
            row["id"],
            f"{row['question_code']} {row['title']}",
            "\n".join([row["prompt"], row["requirements"], row["materials"]]),
            question_id=row["id"],
            question_type=row["question_type"],
            region=row["region"],
            year=row["year"],
            created_at=row["created_at"],
            metadata={"question_code": row["question_code"]},
        )
    for row in conn.execute(
        """
        SELECT m.*, q.id AS question_id, q.question_type, q.region, q.year, q.title AS question_title
          FROM paper_materials m
          JOIN questions q ON q.paper_id = m.paper_id
      ORDER BY q.year DESC, m.material_number
        """
    ):
        _insert_chunk(
            conn,
            "material",
            row["id"],
            f"{row['question_title']} 材料{row['material_number']}",
            row["content"],
            question_id=row["question_id"],
            question_type=row["question_type"],
            region=row["region"],
            year=row["year"],
            created_at=row["created_at"],
        )
    for row in conn.execute(
        """
        SELECT r.*, q.title, q.question_type, q.region, q.year
          FROM reference_answers r
          JOIN questions q ON q.id = r.question_id
      ORDER BY r.id DESC
        """
    ):
        _insert_chunk(
            conn,
            "reference_answer",
            row["id"],
            f"{row['title']} 参考答案",
            "\n".join([row["answer_text"], row["scoring_points"]]),
            question_id=row["question_id"],
            question_type=row["question_type"],
            region=row["region"],
            year=row["year"],
            created_at=row["created_at"],
        )
    rebuild_skill_graph(conn, load_knowledge_items())
    sync_sqlite_vec_index(conn)
    conn.execute("DELETE FROM agent_context_pending")
    conn.execute(
        """
        UPDATE agent_context_index_state
           SET dirty = 0,
               full_rebuild = 0,
               knowledge_signature = ?,
               rebuilt_at = CURRENT_TIMESTAMP
         WHERE id = 1
        """,
        (knowledge_signature(),),
    )
    # The searchable feature-hash index is complete at this point. Publish it
    # before dense-vector inference, which may take longer but must still report
    # a real failure instead of being silently swallowed.
    conn.commit()
    sync_dense_embeddings(conn)


def _delete_context_chunks(conn, where_sql, params=()):
    chunk_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM agent_context_chunks WHERE {where_sql}",
            params,
        )
    ]
    if not chunk_ids:
        return 0
    conn.executemany("DELETE FROM agent_context_fts WHERE rowid = ?", [(chunk_id,) for chunk_id in chunk_ids])
    conn.executemany("DELETE FROM agent_context_chunks WHERE id = ?", [(chunk_id,) for chunk_id in chunk_ids])
    return len(chunk_ids)


def _refresh_attempt_chunks(conn, attempt_id):
    _delete_context_chunks(
        conn,
        "source_type IN ('attempt', 'personal_note') AND source_id = ?",
        (attempt_id,),
    )
    row = conn.execute(
        """
        SELECT a.*, q.title, q.question_type, q.region, q.year, q.question_code
          FROM attempts a JOIN questions q ON q.id = a.question_id
         WHERE a.id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if not row:
        return
    title = f"{row['question_code']} {row['title']}"
    _insert_chunk(
        conn,
        "attempt",
        row["id"],
        title,
        row["answer_text"],
        attempt_id=row["id"],
        question_id=row["question_id"],
        question_type=row["question_type"],
        region=row["region"],
        year=row["year"],
        created_at=row["created_at"],
        metadata={"word_count": row["word_count"]},
    )
    if (row["personal_note"] or "").strip():
        _insert_chunk(
            conn,
            "personal_note",
            row["id"],
            f"{title} 复盘笔记",
            row["personal_note"],
            attempt_id=row["id"],
            question_id=row["question_id"],
            question_type=row["question_type"],
            region=row["region"],
            year=row["year"],
            created_at=row["created_at"],
        )


def _refresh_report_chunk(conn, report_id):
    _delete_context_chunks(conn, "source_type = 'grading_report' AND source_id = ?", (report_id,))
    row = conn.execute(
        """
        SELECT gr.*, a.question_id, q.title, q.question_type, q.region, q.year, q.question_code
          FROM grading_reports gr
          JOIN attempts a ON a.id = gr.attempt_id
          JOIN questions q ON q.id = a.question_id
         WHERE gr.id = ?
        """,
        (report_id,),
    ).fetchone()
    if not row:
        return
    _insert_chunk(
        conn,
        "grading_report",
        row["id"],
        f"{row['question_code']} {row['title']} 批改报告",
        row["report_text"],
        attempt_id=row["attempt_id"],
        question_id=row["question_id"],
        question_type=row["question_type"],
        region=row["region"],
        year=row["year"],
        score=parse_report_score(row["report_text"]),
        created_at=row["created_at"],
        metadata={"provider": row["provider"], "model": row["model"]},
    )


def _insert_question_chunk(conn, row):
    _insert_chunk(
        conn,
        "question",
        row["id"],
        f"{row['question_code']} {row['title']}",
        "\n".join([row["prompt"], row["requirements"], row["materials"]]),
        question_id=row["id"],
        question_type=row["question_type"],
        region=row["region"],
        year=row["year"],
        created_at=row["created_at"],
        metadata={"question_code": row["question_code"]},
    )


def _insert_material_chunk(conn, material, question):
    _insert_chunk(
        conn,
        "material",
        material["id"],
        f"{question['title']} 材料{material['material_number']}",
        material["content"],
        question_id=question["id"],
        question_type=question["question_type"],
        region=question["region"],
        year=question["year"],
        created_at=material["created_at"],
    )


def _insert_reference_chunk(conn, row):
    _insert_chunk(
        conn,
        "reference_answer",
        row["id"],
        f"{row['title']} 参考答案",
        "\n".join([row["answer_text"], row["scoring_points"]]),
        question_id=row["question_id"],
        question_type=row["question_type"],
        region=row["region"],
        year=row["year"],
        created_at=row["created_at"],
    )


def _refresh_material_chunks(conn, material_id):
    _delete_context_chunks(conn, "source_type = 'material' AND source_id = ?", (material_id,))
    material = conn.execute("SELECT * FROM paper_materials WHERE id = ?", (material_id,)).fetchone()
    if not material:
        return
    for question in conn.execute("SELECT * FROM questions WHERE paper_id = ?", (material["paper_id"],)):
        _insert_material_chunk(conn, material, question)


def _refresh_reference_chunk(conn, reference_id):
    _delete_context_chunks(conn, "source_type = 'reference_answer' AND source_id = ?", (reference_id,))
    row = conn.execute(
        """
        SELECT r.*, q.title, q.question_type, q.region, q.year
          FROM reference_answers r JOIN questions q ON q.id = r.question_id
         WHERE r.id = ?
        """,
        (reference_id,),
    ).fetchone()
    if row:
        _insert_reference_chunk(conn, row)


def _queue_index_task(conn, source_type, source_id, operation="upsert"):
    conn.execute(
        """
        INSERT INTO agent_context_pending (
            source_type, source_id, operation, queued_at,
            retry_count, last_error, next_retry_at, status
        ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, 0, '', NULL, 'pending')
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            operation = excluded.operation,
            queued_at = CURRENT_TIMESTAMP,
            retry_count = 0,
            last_error = '',
            next_retry_at = NULL,
            status = 'pending'
        """,
        (source_type, source_id, operation),
    )


def _refresh_question_context(conn, question_id):
    _delete_context_chunks(
        conn,
        "question_id = ? AND source_type IN ('question', 'material', 'reference_answer')",
        (question_id,),
    )
    question = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if not question:
        return
    _insert_question_chunk(conn, question)
    if question["paper_id"]:
        for material in conn.execute(
            "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
            (question["paper_id"],),
        ):
            _insert_material_chunk(conn, material, question)
    for row in conn.execute(
        """
        SELECT r.*, q.title, q.question_type, q.region, q.year
          FROM reference_answers r JOIN questions q ON q.id = r.question_id
         WHERE r.question_id = ?
        """,
        (question_id,),
    ):
        _insert_reference_chunk(conn, row)
    for row in conn.execute("SELECT id FROM attempts WHERE question_id = ?", (question_id,)):
        _queue_index_task(conn, "attempt", row["id"])
    for row in conn.execute(
        "SELECT gr.id FROM grading_reports gr "
        "JOIN attempts a ON a.id = gr.attempt_id WHERE a.question_id = ?",
        (question_id,),
    ):
        _queue_index_task(conn, "grading_report", row["id"])


def _refresh_knowledge_chunks(conn, items=None):
    items = items if items is not None else load_knowledge_items()
    _delete_context_chunks(conn, "source_type = 'knowledge'")
    for item in items:
        module_id = valid_module_id(item.get("module") or "overview")
        definition = module_definition(module_id)
        _insert_chunk(
            conn,
            "knowledge",
            _knowledge_source_id(item["id"]),
            item["title"],
            _knowledge_body(item),
            question_type=definition.get("question_type") or "",
            metadata={
                "knowledge_id": item["id"],
                "module": module_id,
                "module_label": definition.get("label") or module_id,
                "tags": item.get("tags") or [],
                "kind": item.get("kind") or "",
                "source_version": item.get("version") or 1,
                "source_file": item.get("_source_file") or "",
                "skill": item.get("skill") or "",
                "difficulty": item.get("difficulty"),
                "review_status": (item.get("review") or {}).get("status") or "draft",
                "visibility": (item.get("source") or {}).get("visibility") or "public",
            },
        )
    rebuild_skill_graph(conn, items)
    return knowledge_signature(items)


def coalesce_agent_context_pending(db_path, rebuild_threshold=STATIC_REBUILD_THRESHOLD):
    """Collapse covered static tasks or promote a large backlog to one rebuild."""
    with connect(db_path) as reader:
        has_static = reader.execute(
            "SELECT 1 FROM agent_context_pending WHERE status = 'pending' "
            "AND source_type IN ('question', 'material', 'reference_answer') LIMIT 1"
        ).fetchone()
    if not has_static:
        return "none"
    with connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 1000")
        conn.execute("BEGIN IMMEDIATE")
        static_count = conn.execute(
            "SELECT COUNT(*) FROM agent_context_pending "
            "WHERE status = 'pending' AND source_type IN "
            "('question', 'material', 'reference_answer')"
        ).fetchone()[0]
        effective_question_count = conn.execute(
            """
            SELECT COUNT(DISTINCT question_id)
              FROM (
                    SELECT source_id AS question_id
                      FROM agent_context_pending
                     WHERE status = 'pending' AND source_type = 'question'
                    UNION ALL
                    SELECT q.id
                      FROM agent_context_pending p
                      JOIN paper_materials m ON m.id = p.source_id
                      JOIN questions q ON q.paper_id = m.paper_id
                     WHERE p.status = 'pending' AND p.source_type = 'material'
                    UNION ALL
                    SELECT r.question_id
                      FROM agent_context_pending p
                      JOIN reference_answers r ON r.id = p.source_id
                     WHERE p.status = 'pending' AND p.source_type = 'reference_answer'
                   )
            """
        ).fetchone()[0]
        estimated_work = max(int(static_count), int(effective_question_count))
        if estimated_work >= max(1, int(rebuild_threshold)):
            conn.execute(
                "DELETE FROM agent_context_pending WHERE status = 'pending' "
                "AND source_type IN ('question', 'material', 'reference_answer')"
            )
            conn.execute(
                "UPDATE agent_context_index_state SET dirty = 1, full_rebuild = 1 WHERE id = 1"
            )
            conn.execute(
                """
                UPDATE agent_context_worker_state
                   SET status = 'queued_rebuild', current_type = 'full_rebuild',
                       processed_count = 0, total_count = ?, last_error = '',
                       started_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                 WHERE id = 1
                """,
                (estimated_work,),
            )
            return "rebuild"

        material_questions = conn.execute(
            """
            SELECT DISTINCT q.id
              FROM agent_context_pending p
              JOIN paper_materials m ON m.id = p.source_id
              JOIN questions q ON q.paper_id = m.paper_id
             WHERE p.status = 'pending' AND p.source_type = 'material'
            """
        ).fetchall()
        for row in material_questions:
            _queue_index_task(conn, "question", row["id"])
        if material_questions:
            conn.execute(
                "DELETE FROM agent_context_pending "
                "WHERE status = 'pending' AND source_type = 'material'"
            )

        conn.execute(
            """
            DELETE FROM agent_context_pending
             WHERE status = 'pending' AND source_type = 'reference_answer'
               AND EXISTS (
                   SELECT 1
                     FROM reference_answers r
                     JOIN agent_context_pending q
                       ON q.source_type = 'question'
                      AND q.source_id = r.question_id
                      AND q.status = 'pending'
                    WHERE r.id = agent_context_pending.source_id
               )
            """
        )
        return "coalesced"


def _record_index_failure(db_path, source_type, source_id, error):
    message = str(error)[:600]
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT retry_count FROM agent_context_pending "
            "WHERE source_type = ? AND source_id = ?",
            (source_type, source_id),
        ).fetchone()
        if not row:
            return
        retry_count = int(row["retry_count"] or 0) + 1
        failed = retry_count >= MAX_INDEX_RETRIES
        delay_seconds = min(60, 2 ** retry_count)
        conn.execute(
            """
            UPDATE agent_context_pending
               SET retry_count = ?, last_error = ?,
                   next_retry_at = CASE WHEN ? THEN NULL ELSE datetime('now', ?) END,
                   status = CASE WHEN ? THEN 'failed' ELSE 'pending' END
             WHERE source_type = ? AND source_id = ?
            """,
            (
                retry_count,
                message,
                failed,
                f"+{delay_seconds} seconds",
                failed,
                source_type,
                source_id,
            ),
        )
        failed_count = conn.execute(
            "SELECT COUNT(*) FROM agent_context_pending WHERE status = 'failed'"
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE agent_context_worker_state
               SET status = CASE WHEN ? THEN 'completed_with_errors' ELSE 'retrying' END,
                   failed_count = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
             WHERE id = 1
            """,
            (failed, failed_count, message),
        )


def process_agent_context_pending_batch(db_path, batch_size=8):
    """Process queued source changes in independent one-item transactions."""
    with connect(db_path) as reader:
        rows = reader.execute(
            """
            SELECT source_type, source_id
              FROM agent_context_pending
             WHERE status = 'pending'
               AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
          ORDER BY CASE source_type
                   WHEN 'attempt' THEN 0
                   WHEN 'grading_report' THEN 1
                   ELSE 2 END,
                   queued_at,
                   source_type,
                   source_id
             LIMIT ?
            """,
            (max(1, int(batch_size)),),
        ).fetchall()
        pending_total = reader.execute(
            "SELECT COUNT(*) FROM agent_context_pending WHERE status = 'pending'"
        ).fetchone()[0]
    if rows:
        with connect(db_path) as conn:
            worker = conn.execute(
                "SELECT status, processed_count FROM agent_context_worker_state WHERE id = 1"
            ).fetchone()
            starting = not worker or worker["status"] not in {"running", "retrying"}
            processed_before = 0 if starting else int(worker["processed_count"] or 0)
            conn.execute(
                """
                UPDATE agent_context_worker_state
                   SET status = 'running',
                       processed_count = ?, total_count = ?,
                       started_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE started_at END,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = 1
                """,
                (processed_before, processed_before + pending_total, starting),
            )
    handlers = {
        "attempt": _refresh_attempt_chunks,
        "grading_report": _refresh_report_chunk,
        "question": _refresh_question_context,
        "material": _refresh_material_chunks,
        "reference_answer": _refresh_reference_chunk,
    }
    processed = 0
    for queued in rows:
        try:
            with connect(db_path) as conn:
                conn.execute("PRAGMA busy_timeout = 1000")
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT operation FROM agent_context_pending "
                    "WHERE source_type = ? AND source_id = ? AND status = 'pending' "
                    "AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)",
                    (queued["source_type"], queued["source_id"]),
                ).fetchone()
                if current is None:
                    continue
                handler = handlers.get(queued["source_type"])
                if handler:
                    handler(conn, queued["source_id"])
                conn.execute(
                    "DELETE FROM agent_context_pending "
                    "WHERE source_type = ? AND source_id = ?",
                    (queued["source_type"], queued["source_id"]),
                )
                conn.execute(
                    "UPDATE agent_context_index_state SET dirty = 1 WHERE id = 1"
                )
                conn.execute(
                    """
                    UPDATE agent_context_worker_state
                       SET status = 'running', current_type = ?,
                           processed_count = processed_count + 1,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = 1
                    """,
                    (queued["source_type"],),
                )
            processed += 1
        except Exception as exc:
            if "locked" in str(exc).lower():
                break
            logging.exception(
                "Agent index batch failed for %s:%s",
                queued["source_type"],
                queued["source_id"],
            )
            _record_index_failure(
                db_path,
                queued["source_type"],
                queued["source_id"],
                exc,
            )
            continue
    return processed


def finalize_agent_context_index(db_path):
    """Synchronize derived vector tables after the source queue is drained."""
    with connect(db_path) as conn:
        pending = conn.execute(
            "SELECT 1 FROM agent_context_pending WHERE status = 'pending' LIMIT 1"
        ).fetchone()
        if pending:
            return False
        state = conn.execute(
            "SELECT dirty, full_rebuild FROM agent_context_index_state WHERE id = 1"
        ).fetchone()
        if not state or not state["dirty"] or state["full_rebuild"]:
            return False
        sync_sqlite_vec_index(conn, commit_batch_size=128)
        conn.commit()
        sync_dense_embeddings(conn)
        failed_count = conn.execute(
            "SELECT COUNT(*) FROM agent_context_pending WHERE status = 'failed'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE agent_context_index_state "
            "SET dirty = ?, rebuilt_at = CURRENT_TIMESTAMP WHERE id = 1",
            (1 if failed_count else 0,),
        )
        conn.execute(
            """
            UPDATE agent_context_worker_state
               SET status = CASE WHEN ? > 0 THEN 'completed_with_errors' ELSE 'idle' END,
                   current_type = '', failed_count = ?, updated_at = CURRENT_TIMESTAMP
             WHERE id = 1
            """,
            (failed_count, failed_count),
        )
    return True


def rebuild_agent_context_index_if_needed(db_path):
    with connect(db_path) as conn:
        state = conn.execute(
            "SELECT full_rebuild FROM agent_context_index_state WHERE id = 1"
        ).fetchone()
        indexed_count = conn.execute(
            "SELECT COUNT(*) FROM agent_context_chunks"
        ).fetchone()[0]
        if indexed_count and state and not state["full_rebuild"]:
            return False
    with connect(db_path) as conn:
        pending_total = conn.execute(
            "SELECT COUNT(*) FROM agent_context_pending WHERE status = 'pending'"
        ).fetchone()[0]
        worker_total = conn.execute(
            "SELECT total_count FROM agent_context_worker_state WHERE id = 1"
        ).fetchone()[0]
        total = max(int(pending_total), int(worker_total or 0))
        conn.execute(
            """
            UPDATE agent_context_worker_state
               SET status = 'rebuilding', current_type = 'full_rebuild',
                   processed_count = 0, total_count = ?, last_error = '',
                   started_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
             WHERE id = 1
            """,
            (total,),
        )
    try:
        with connect(db_path) as conn:
            rebuild_agent_context_index(conn)
        with connect(db_path) as conn:
            conn.execute(
                """
                UPDATE agent_context_worker_state
                   SET status = 'idle', current_type = '',
                       processed_count = total_count, failed_count = 0,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = 1
                """
            )
    except Exception as exc:
        with connect(db_path) as conn:
            conn.execute(
                "UPDATE agent_context_worker_state "
                "SET status = 'failed', last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (str(exc)[:600],),
            )
        raise
    return True


def refresh_agent_knowledge_if_needed(db_path):
    items = load_knowledge_items()
    signature = knowledge_signature(items)
    with connect(db_path) as conn:
        state = conn.execute(
            "SELECT knowledge_signature, full_rebuild "
            "FROM agent_context_index_state WHERE id = 1"
        ).fetchone()
        if not state or state["full_rebuild"] or state["knowledge_signature"] == signature:
            return False
        signature = _refresh_knowledge_chunks(conn, items)
        conn.execute(
            "UPDATE agent_context_index_state "
            "SET knowledge_signature = ?, dirty = 1 WHERE id = 1",
            (signature,),
        )
    return True


def ensure_agent_context_index(conn):
    try:
        state = conn.execute("SELECT * FROM agent_context_index_state WHERE id = 1").fetchone()
        indexed_count = conn.execute("SELECT COUNT(*) FROM agent_context_chunks").fetchone()[0]
    except Exception as exc:
        logging.error("Agent index state is unavailable during interactive retrieval: %s", exc)
        return False
    if not state or state["full_rebuild"] or indexed_count == 0:
        return False
    # Incremental maintenance belongs to the independent background indexer.
    # Interactive retrieval only reads the last committed complete index.
    return False



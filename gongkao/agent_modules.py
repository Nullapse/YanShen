import json
import re

from . import agent_retrieval as _retrieval
from .agent_retrieval import (
    FEATURE_HASH_MODEL,
    MODULES,
    module_definition,
    valid_module_id,
)
from .agent_retrieval import (
    cosine_similarity as _cosine,
)
from .agent_retrieval import (
    embed_text as _embed_text,
)
from .agent_retrieval import indexing as _indexing
from .agent_retrieval import (
    problem_categories as _problem_categories,
)
from .agent_retrieval import (
    profile_snapshot as _profile_snapshot,
)
from .agent_retrieval import (
    tokenize as _tokens,
)
from .agent_retrieval import (
    update_weakness_profile as _update_weakness_profile,
)
from .agent_retrieval.indexing import (
    _dense_query_embedding,
    _evidence_ref,
    _source_rank,
    _sqlite_vec_search,
    ensure_agent_context_index,
)
from .agent_retrieval.query import where_for_scope as _where_for_scope

VECTOR_DIM = _retrieval.VECTOR_DIM
PROBLEM_PATTERNS = _retrieval.PROBLEM_PATTERNS
BGE_MODEL = _indexing.BGE_MODEL
BGE_DIMENSIONS = _indexing.BGE_DIMENSIONS
_load_dense_model = _indexing._load_dense_model
configure_dense_embedding = _indexing.configure_dense_embedding
sync_dense_embeddings = _indexing.sync_dense_embeddings
sync_sqlite_vec_index = _indexing.sync_sqlite_vec_index
knowledge_signature = _indexing.knowledge_signature
load_knowledge_items = _indexing.load_knowledge_items
rebuild_agent_context_index = _indexing.rebuild_agent_context_index


def classify_module_heuristic(text="", module_hint=""):
    if module_hint in MODULES:
        return module_hint
    text = text or ""
    if any(key in text for key in ("归纳概括", "概括题")):
        return "summary"
    if "综合分析" in text:
        return "analysis"
    if any(key in text for key in ("提出对策", "对策题")):
        return "countermeasure"
    if any(key in text for key in ("公文写作", "公文", "应用文")):
        return "document"
    if "综合写作" in text:
        return "essay"
    if any(key in text for key in ("最大失分", "失分点", "最丢分")):
        return "top_loss"
    if any(key in text for key in ("怎么改", "怎么提高", "怎么改进", "提升", "改进")):
        return "improvement"
    return "overview"


def requested_recent_limit(text=""):
    text = text or ""
    match = re.search(r"最近\s*(\d+)\s*(?:道|题|次|篇|个)", text)
    if match:
        return max(1, min(int(match.group(1)), 20))
    numbers = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    for word, value in numbers.items():
        if re.search(rf"最近[^，。！？\s]*{word}\s*(?:道|题|次|篇|个)", text):
            return value
    return None


def build_analysis_scope(user_goal="", module_id="overview", filters=None):
    definition = module_definition(module_id)
    filters = dict(filters or {})
    if definition["question_type"] and not filters.get("question_type"):
        filters["question_type"] = definition["question_type"]
    recent_limit = requested_recent_limit(user_goal)
    return {
        "module": module_id,
        "module_label": definition["label"],
        "focus": definition["focus"],
        "filters": filters,
        "scope": "recent" if recent_limit else "all",
        "recent_limit": recent_limit,
        "required_sources": ["stats", "attempts", "reports", "notes", "questions", "references", "materials"],
    }


def _clean(value, limit=1800):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _coverage(conn, scope):
    clauses, params = _where_for_scope(scope)
    where = " AND ".join(clauses) if clauses else "1 = 1"
    attempt_count = conn.execute(
        f"SELECT COUNT(*) FROM agent_context_chunks WHERE source_type = 'attempt' AND {where}",
        params,
    ).fetchone()[0]
    report_count = conn.execute(
        f"SELECT COUNT(*) FROM agent_context_chunks WHERE source_type = 'grading_report' AND {where}",
        params,
    ).fetchone()[0]
    note_count = conn.execute(
        f"SELECT COUNT(*) FROM agent_context_chunks WHERE source_type = 'personal_note' AND {where}",
        params,
    ).fetchone()[0]
    chunk_count = conn.execute(
        f"SELECT COUNT(*) FROM agent_context_chunks WHERE {where}",
        params,
    ).fetchone()[0]
    return {
        "attempt_count": attempt_count,
        "report_count": report_count,
        "note_count": note_count,
        "chunk_count": chunk_count,
    }


def _module_query(module_id, user_goal):
    base = module_definition(module_id)["focus"]
    return " ".join([user_goal or "", base, "失分 材料 采分 结构 审题 改进"])


def _keyword_score(row, terms):
    text = f"{row.get('title', '')} {row.get('body', '')}"
    if not terms:
        return 0.0
    hits = sum(1 for term in terms if term and term in text)
    return min(1.0, hits / max(1, min(len(terms), 8)))


def _source_counts(conn, scope):
    clauses, params = _where_for_scope(scope)
    where = " AND ".join(clauses) if clauses else "1 = 1"
    rows = conn.execute(
        f"""
        SELECT source_type, COUNT(*) AS count
          FROM agent_context_chunks
         WHERE {where}
      GROUP BY source_type
      ORDER BY count DESC
        """,
        params,
    ).fetchall()
    return {row["source_type"]: row["count"] for row in rows}


def _all_scope_problem_categories(conn, scope):
    clauses, params = _where_for_scope(scope)
    where = " AND ".join(clauses) if clauses else "1 = 1"
    rows = conn.execute(
        f"""
        SELECT title, body
          FROM agent_context_chunks
         WHERE {where}
           AND source_type IN ('grading_report', 'personal_note', 'attempt')
        """,
        params,
    ).fetchall()
    return _problem_categories([dict(row) for row in rows])


def _search_chunks(conn, scope, user_goal, limit=40, prefer_dense=True):
    clauses, params = _where_for_scope(scope)
    query = user_goal if scope.get("query_mode") == "knowledge" else _module_query(scope["module"], user_goal)
    words = [word for word in re.split(r"\s+", query) if word]
    semantic_terms = _tokens(query)
    like_terms = words[:8] or semantic_terms[:8] or ["失分"]
    where = " AND ".join(clauses) if clauses else "1 = 1"
    like_sql = " OR ".join("body LIKE ?" for _ in like_terms)
    keyword_rows = conn.execute(
        f"""
        SELECT *
          FROM agent_context_chunks
         WHERE {where}
           AND ({like_sql} OR source_type IN ('grading_report', 'personal_note', 'attempt'))
      ORDER BY CASE source_type
               WHEN 'grading_report' THEN 0
               WHEN 'personal_note' THEN 1
               WHEN 'attempt' THEN 2
               ELSE 3 END,
               created_at DESC,
               id DESC
         LIMIT ?
        """,
        (*params, *(f"%{term}%" for term in like_terms), limit),
    ).fetchall()
    fts_terms = []
    for raw_term in words + [scope.get("module_label") or ""]:
        term = raw_term.strip("，。！？；：、（）()[]【】\"'")
        if 2 <= len(term) <= 24 and term not in fts_terms:
            fts_terms.append(term)
    fts_rows = []
    if fts_terms:
        match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in fts_terms[:10])
        try:
            fts_rows = conn.execute(
                f"""
                SELECT c.*, bm25(agent_context_fts) AS fts_rank
                  FROM agent_context_fts
                  JOIN agent_context_chunks c ON c.id = agent_context_fts.rowid
                 WHERE agent_context_fts MATCH ?
                   AND {where}
              ORDER BY fts_rank
                 LIMIT ?
                """,
                (match_query, *params, limit),
            ).fetchall()
        except Exception:
            fts_rows = []
    if prefer_dense:
        query_vector, embedding_model = _dense_query_embedding(conn, query)
        try:
            vector_rows = _sqlite_vec_search(
                conn,
                query_vector,
                scope,
                limit=max(120, limit * 3),
                embedding_model=embedding_model,
            )
        except Exception:
            vector_rows = []
    else:
        query_vector, _ = _embed_text(query)
        embedding_model = FEATURE_HASH_MODEL
        vector_rows = []
    vector_backend = f"sqlite-vec:{embedding_model}" if vector_rows else "python-recent-scan:feature-hash-v1"
    if not vector_rows:
        query_vector, _ = _embed_text(query)
        vector_rows = conn.execute(
            f"""
            SELECT c.*, v.vector_json
              FROM agent_context_chunks c
              JOIN agent_context_vectors v ON v.chunk_id = c.id
             WHERE {where}
               AND v.embedding_model = ?
          ORDER BY c.created_at DESC, c.id DESC
             LIMIT 900
            """,
            (*params, FEATURE_HASH_MODEL),
        ).fetchall()
    scored = {}
    for rank, row in enumerate(keyword_rows, start=1):
        item = dict(row)
        item["_keyword_score"] = max(item.get("_keyword_score", 0), _keyword_score(item, like_terms + semantic_terms[:12]))
        item["_vector_score"] = 0.0
        item["_bm25_score"] = 0.0
        item["_keyword_rank"] = rank
        scored[item["id"]] = item
    for rank, row in enumerate(fts_rows, start=1):
        item = dict(row)
        item.pop("fts_rank", None)
        existing = scored.get(item["id"], item)
        existing["_bm25_score"] = max(existing.get("_bm25_score", 0.0), 1.0 / (1.0 + rank / 8.0))
        existing["_bm25_rank"] = min(existing.get("_bm25_rank", rank), rank)
        existing["_keyword_score"] = max(existing.get("_keyword_score", 0.0), _keyword_score(existing, like_terms + semantic_terms[:12]))
        existing["_vector_score"] = existing.get("_vector_score", 0.0)
        scored[item["id"]] = existing
    for rank, row in enumerate(vector_rows, start=1):
        item = dict(row)
        if "_vector_score" in item:
            vector_score = float(item.get("_vector_score") or 0)
        else:
            try:
                vector = json.loads(item.pop("vector_json") or "[]")
            except json.JSONDecodeError:
                vector = []
            vector_score = _cosine(query_vector, vector)
        if vector_score < 0.08 and item["id"] not in scored:
            continue
        existing = scored.get(item["id"], item)
        existing["_vector_score"] = max(existing.get("_vector_score", 0.0), vector_score)
        existing["_vector_rank"] = min(existing.get("_vector_rank", rank), rank)
        existing["_vector_backend"] = item.get("_vector_backend") or vector_backend
        existing["_keyword_score"] = max(existing.get("_keyword_score", 0.0), _keyword_score(existing, like_terms + semantic_terms[:12]))
        existing["_bm25_score"] = existing.get("_bm25_score", 0.0)
        scored[item["id"]] = existing
    for item in scored.values():
        source_score = _source_rank(item.get("source_type"))
        score_penalty = 0.0
        if item.get("score") is not None:
            try:
                score_penalty = max(0.0, (70.0 - float(item["score"])) / 100.0)
            except (TypeError, ValueError):
                score_penalty = 0.0
        rrf_raw = sum(
            1.0 / (60.0 + float(item[key]))
            for key in ("_keyword_rank", "_bm25_rank", "_vector_rank")
            if item.get(key)
        )
        item["_rrf_score"] = round(rrf_raw / (3.0 / 61.0), 4)
        item["_rerank_score"] = round(
            item.get("_rrf_score", 0) * 0.5
            + item.get("_keyword_score", 0) * 0.1
            + item.get("_vector_score", 0) * 0.17
            + source_score * 0.15
            + score_penalty * 0.08,
            4,
        )
        if scope.get("query_mode") == "knowledge" and item.get("source_type") == "knowledge":
            try:
                metadata = json.loads(item.get("metadata_json") or "{}")
            except json.JSONDecodeError:
                metadata = {}
            if metadata.get("kind") in {"question_type_method", "training_checklist"}:
                item["_rerank_score"] = round(item["_rerank_score"] - 0.18, 4)
    return sorted(
        scored.values(),
        key=lambda item: (item.get("_rerank_score", 0), item.get("created_at") or "", item.get("id") or 0),
        reverse=True,
    )[:limit]


def retrieve_module_evidence(conn, module_id, user_goal="", filters=None):
    module_id = valid_module_id(module_id)
    ensure_agent_context_index(conn)
    # Retrieval is read-only from here; publish any index maintenance first.
    conn.commit()
    scope = build_analysis_scope(user_goal, module_id, filters)
    coverage = _coverage(conn, scope)
    chunks = _search_chunks(conn, scope, user_goal)
    recent_limit = scope.get("recent_limit")
    if recent_limit:
        chunks = sorted(chunks, key=lambda item: (item.get("created_at") or "", item.get("id") or 0), reverse=True)[:recent_limit * 4]
    categories = _all_scope_problem_categories(conn, scope) if scope.get("scope") == "all" else _problem_categories(chunks)
    question_type = scope.get("filters", {}).get("question_type") or ""
    _update_weakness_profile(conn, module_id, question_type, categories, chunks)
    representative = [
        {
            "evidence_ref": _evidence_ref(row),
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "attempt_id": row["attempt_id"],
            "question_id": row["question_id"],
            "question_type": row["question_type"],
            "region": row["region"],
            "year": row["year"],
            "title": row["title"],
            "body": _clean(row["body"], 650),
            "score": row["score"],
            "created_at": row["created_at"],
            "retrieval": {
                "keyword_score": row.get("_keyword_score", 0),
                "bm25_score": round(row.get("_bm25_score", 0), 4),
                "vector_score": round(row.get("_vector_score", 0), 4),
                "vector_backend": row.get("_vector_backend") or "none",
                "rrf_score": row.get("_rrf_score", 0),
                "rerank_score": row.get("_rerank_score", 0),
            },
        }
        for row in chunks[:28]
    ]
    source_counts = _source_counts(conn, scope)
    return {
        "module": module_id,
        "module_label": module_definition(module_id)["label"],
        "scope": scope,
        "coverage": coverage,
        "source_counts": source_counts,
        "analysis_basis": {
            "coverage_mode": "all_matching_history" if scope.get("scope") == "all" else "recent_matching_history",
            "retrieval_strategy": "full local scan for coverage and categories; keyword/vector/rerank selects representative evidence for the prompt",
            "representative_evidence_count": len(representative),
            "matched_chunk_count": coverage.get("chunk_count", 0),
        },
        "problem_categories": categories,
        "weakness_profile": _profile_snapshot(conn, module_id, question_type),
        "evidence_chunks": representative,
    }


def ensure_knowledge_index(conn):
    return ensure_agent_context_index(conn)


def retrieve_knowledge_evidence(
    conn,
    module_id="overview",
    user_goal="",
    limit=8,
    *,
    ensure_index=True,
    prefer_dense=True,
):
    if ensure_index:
        ensure_agent_context_index(conn)
        # Never carry an index-maintenance write lock into query embedding.
        conn.commit()
    module_id = valid_module_id(module_id or "overview")
    scope = {
        "module": module_id,
        "module_label": module_definition(module_id)["label"],
        "filters": {
            "question_type": module_definition(module_id)["question_type"]
        } if module_definition(module_id)["question_type"] else {},
        "source_types": ["knowledge"],
        "query_mode": "knowledge",
    }
    scored = _search_chunks(
        conn,
        scope,
        user_goal,
        limit=max(24, int(limit) * 4),
        prefer_dense=prefer_dense,
    )
    for item in scored:
        try:
            metadata = json.loads(item.get("metadata_json") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        item["_metadata"] = metadata
    module_scored = [
        item for item in scored
        if (item.get("_metadata") or {}).get("module") == module_id
    ]
    if module_scored:
        scored = module_scored
    return [
        {
            "evidence_ref": _evidence_ref(row),
            "source_type": "knowledge",
            "source_id": row["source_id"],
            "title": row["title"],
            "body": _clean(row["body"], 800),
            "metadata": row.get("_metadata") or {},
            "retrieval": {
                "keyword_score": row.get("_keyword_score", 0),
                "bm25_score": round(row.get("_bm25_score", 0), 4),
                "vector_score": round(row.get("_vector_score", 0), 4),
                "vector_backend": row.get("_vector_backend") or "none",
                "rrf_score": row.get("_rrf_score", 0),
                "rerank_score": row.get("_rerank_score", 0),
            },
        }
        for row in scored[:limit]
    ]

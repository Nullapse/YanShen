"""Scoped filters, compact evidence discovery and paged source reading."""

import hashlib
import json

from .agent_modules import module_definition
from .grading import select_relevant_materials


def effective_filters(state, args):
    filters = dict(state.get("filters") or {})
    question_type = module_definition(state.get("module") or "overview").get("question_type")
    if question_type and not filters.get("question_type"):
        filters["question_type"] = question_type
    for key in ("question_type", "region", "year", "work_status"):
        value = args.get(key)
        if value is None:
            continue
        if filters.get(key) not in (None, "", value):
            raise ValueError(f"{key} 与本轮已确定范围冲突，请保持原筛选或由用户开启新任务")
        filters[key] = value
    return filters


def compact_search(conn, state, context, args, filters):
    cards = list(context.get("evidence_cards") or [])
    allowed = set((context.get("query_plan") or {}).get("sources") or [])
    sources = args.get("sources")
    if sources and not set(sources) <= allowed:
        raise ValueError("sources 超出本轮允许的证据来源")
    if sources:
        cards = [c for c in cards if c.get("source_type") in sources]
    selected = []
    seen = set()
    for card in cards:
        identifier = card.get("evidence_id")
        if not identifier or identifier in seen:
            continue
        qid = card.get("question_id")
        # Recheck entity filters for routes that construct cards without SQL retrieval.
        if qid:
            question = conn.execute("SELECT question_type, region, year FROM questions WHERE id = ?", (qid,)).fetchone()
            if not question or any(
                filters.get(k) not in (None, "") and question[k] != filters[k]
                for k in ("question_type", "region", "year")
            ):
                continue
        seen.add(identifier)
        selected.append(card)
    # Keep actual source evidence ahead of overview/profile cards in the short list.
    selected.sort(key=lambda c: c.get("source_type") in {"aggregate", "weakness_profile"})
    selected = selected[: args.get("top_k", 8)]
    catalog = dict(state.get("evidence_catalog") or {})
    brief = []
    for card in selected:
        catalog[card["evidence_id"]] = card
        brief.append({k: card.get(k) for k in ("evidence_id", "source_type", "question_id", "attempt_id", "title")})
        brief[-1].update(
            title=str(card.get("title") or "")[:120],
            content=str(card.get("content") or "")[:240],
            can_read_source=card.get("source_type") not in {"aggregate", "weakness_profile"},
        )
    result = dict(context)
    contract = dict(result.get("grounding_contract") or {})
    contract["allowed_evidence_ids"] = [c["evidence_id"] for c in selected]
    result.update(evidence_cards=selected, grounding_contract=contract)
    module = context.get("module_context") or {}
    return {
        "rag_context": result,
        "module_context": module,
        "evidence_catalog": catalog,
        "search_observation": {
            "rag_route": context.get("rag_route"),
            "filters": filters,
            "sources": sources or sorted(allowed),
            "evidence_cards": brief,
            "grounding_contract": {"allowed_evidence_ids": [c["evidence_id"] for c in selected]},
            "coverage": module.get("coverage") or {},
            "returned_count": len(brief),
            "has_more_candidates": len(seen) > len(selected),
            "next_step": "需要原文时用 read_source，传 evidence_id 或已出现的 attempt_id。",
        },
    }


def read_source(conn, state, args):
    catalog = state.get("evidence_catalog") or {}
    evidence_id = args.get("evidence_id")
    attempt_id = args.get("attempt_id")
    if bool(evidence_id) == bool(attempt_id):
        raise ValueError("evidence_id 与 attempt_id 必须且只能提供一个")
    card = catalog.get(evidence_id) if evidence_id else None
    if evidence_id and card is None:
        raise ValueError("证据编号尚未在本轮召回，请先 search_evidence")
    known_attempts = set(state.get("subject_ids") or [])
    known_attempts.update(c.get("attempt_id") for c in catalog.values() if c.get("attempt_id"))
    if attempt_id and attempt_id not in known_attempts:
        raise ValueError("作答编号未在本轮选定或召回，拒绝读取")
    card = card or {"source_type": "attempt", "attempt_id": attempt_id, "evidence_id": f"attempt:{attempt_id}"}
    attempt_id = card.get("attempt_id")
    kind = card.get("source_type")
    section = args.get("section", "source")
    plan = (state.get("context_plan") or {}).get("rag_query_plan") or {}
    if plan.get("scope") == "notes_only" and (
        kind not in {"personal_note", "knowledge"} or section not in {"source", "note"}
    ):
        raise ValueError("本轮仅允许读取笔记与已召回知识")
    if plan.get("scope") == "current_attempt" and attempt_id not in (state.get("subject_ids") or []):
        # Method knowledge has no attempt association and is safe within the discovered catalog.
        if kind != "knowledge":
            raise ValueError("作答超出本轮选定范围")
    metadata = card.get("metadata") or {}
    source_id = metadata.get("source_id")
    attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone() if attempt_id else None
    qid = card.get("question_id") or (attempt["question_id"] if attempt else None)
    question = conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone() if qid else None
    filters = effective_filters(state, {})
    if question and any(
        filters.get(k) not in (None, "") and question[k] != filters[k] for k in ("question_type", "region", "year")
    ):
        raise ValueError("原文已不符合本轮题目筛选，请重新检索")
    if section == "source":
        section = {
            "attempt": "answer",
            "personal_note": "note",
            "question": "question",
            "candidate_question": "question",
            "material": "materials",
            "grading_report": "reports",
            "reference_answer": "references",
            "knowledge": "knowledge",
        }.get(kind)
    chunks = []

    def add(label, text):
        if text:
            chunks.append(f"[{label}]\n{text}")

    if section in {"answer", "note"} and attempt:
        add(f"attempt:{attempt_id}/{section}", attempt["answer_text" if section == "answer" else "personal_note"])
    elif section == "question" and question:
        for field in ("title", "prompt", "requirements"):
            add(f"question:{qid}/{field}", question[field])
    elif section == "reports" and attempt:
        rows = conn.execute(
            "SELECT id, report_text FROM grading_reports WHERE attempt_id = ? ORDER BY created_at DESC, id DESC",
            (attempt_id,),
        ).fetchall()
        for row in rows:
            if args.get("section", "source") == "source" and source_id and row["id"] != source_id:
                continue
            add(f"grading_report:{row['id']}", row["report_text"])
    elif section == "materials" and question:
        rows = conn.execute(
            "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number", (question["paper_id"],)
        ).fetchall()
        for row in select_relevant_materials(dict(question), rows):
            if args.get("section", "source") == "source" and source_id and row["id"] != source_id:
                continue
            add(f"material:{row['id']}", row["content"])
    elif section == "references" and question:
        rows = conn.execute(
            "SELECT id, answer_text, scoring_points FROM reference_answers WHERE question_id = ? ORDER BY id", (qid,)
        ).fetchall()
        for row in rows:
            if args.get("section", "source") == "source" and source_id and row["id"] != source_id:
                continue
            add(f"reference_answer:{row['id']}", f"{row['answer_text']}\n{row['scoring_points']}")
    elif section == "knowledge" and kind == "knowledge":
        from .agent_modules import load_knowledge_items

        for item in load_knowledge_items():
            if item.get("id") == card.get("evidence_id"):
                add(card["evidence_id"], json.dumps(item, ensure_ascii=False))
                break
    if not chunks:
        return {
            "source_detail": {
                "evidence_id": card.get("evidence_id"),
                "available": False,
                "reason": "当前来源没有该部分原文，或实体已删除。",
            }
        }
    text = "\n\n".join(chunks)
    revision = hashlib.sha256(text.encode()).hexdigest()[:24]
    if args.get("revision") and args["revision"] != revision:
        raise ValueError("原文在分页期间发生变化，请从 offset=0 重新读取")
    offset, length = args.get("offset", 0), args.get("length", 1600)
    if offset and not args.get("revision"):
        raise ValueError("继续分页必须提供上一页的 revision")
    end = min(len(text), offset + length)
    return {
        "source_detail": {
            "evidence_id": card.get("evidence_id"),
            "attempt_id": attempt_id,
            "question_id": qid,
            "section": args.get("section", "source"),
            "resolved_section": section,
            "available": True,
            "revision": revision,
            "offset": offset,
            "total_chars": len(text),
            "text": text[offset:end],
            "next_offset": end if end < len(text) else None,
        }
    }

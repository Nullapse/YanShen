import json
import re

from .agent_modules import retrieve_knowledge_evidence, retrieve_module_evidence, valid_module_id

STRUCTURE_KEYS = ("这样写", "结构", "过去现在", "能不能", "是否可以", "行不行", "框架", "分几段")
REWRITE_KEYS = ("改写", "示范", "帮我写", "重写", "润色", "下一版", "修改成")
LOSS_KEYS = ("失分", "哪里弱", "问题在哪", "为什么扣分", "最大问题", "短板")
RECOMMEND_KEYS = ("练什么", "推荐", "下一题", "安排")
NOTE_KEYS = ("笔记", "复盘笔记", "注意事项", "避坑", "清单", "检查单")
NOTE_ORGANIZE_KEYS = ("整理", "汇总", "总结", "归纳", "所有", "全部", "注意事项", "清单", "检查单")
WRITING_GUIDE_KEYS = ("是什么", "怎么写", "几种写法", "写法", "格式", "模板", "编者按", "短评", "倡议书", "讲话稿", "宣传稿")
GUIDANCE_INTENT_RE = re.compile(
    r"(\u662f\u4ec0\u4e48|\u600e\u4e48|\u5982\u4f55|\u6709\u54ea\u4e9b|\u54ea\u4e9b|"
    r"\u51e0\u79cd|\u533a\u522b|\u683c\u5f0f|\u6a21\u677f|\u5199\u6cd5|\u6b65\u9aa4|"
    r"\u793a\u4f8b|\u4f8b\u5b50|\u5957\u8def|\u65b9\u6cd5|\u8981\u70b9|"
    r"\u6ce8\u610f\u4ec0\u4e48|\u600e\u4e48\u5224\u65ad)"
)


VALID_ACTIONS = {
    "diagnose",
    "review",
    "judge_structure",
    "rewrite",
    "recommend",
    "organize",
    "explain",
    "guide",
    "compare",
}
VALID_SCOPES = {
    "current_attempt",
    "notes_only",
    "candidate_questions",
    "module_history",
    "overall_history",
}
VALID_SOURCES = {
    "question",
    "attempt",
    "material",
    "reference_answer",
    "grading_report",
    "personal_note",
    "knowledge",
    "candidate_question",
    "weakness_profile",
    "aggregate",
}


def clip_text(value, limit=700):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _has_any(text, keys):
    return any(key in (text or "") for key in keys)


def _wants_note_organization(text):
    return _has_any(text, NOTE_KEYS) and _has_any(text, NOTE_ORGANIZE_KEYS)


def _wants_guidance(text):
    text = text or ""
    return bool(GUIDANCE_INTENT_RE.search(text)) or _has_any(text, WRITING_GUIDE_KEYS)


def fallback_query_plan(user_goal="", task_type="diagnosis", subject_ids=None, module=""):
    subject_ids = subject_ids or []
    if task_type == "review" and subject_ids:
        action = "review"
        if _has_any(user_goal, STRUCTURE_KEYS):
            action = "judge_structure"
        elif _has_any(user_goal, REWRITE_KEYS):
            action = "rewrite"
        return {
            "action": action,
            "scope": "current_attempt",
            "sources": ["question", "attempt", "material", "reference_answer", "grading_report", "personal_note"],
            "module": valid_module_id(module or "") or "overview",
            "reason": "fallback_current_attempt",
        }
    if _wants_note_organization(user_goal):
        return {
            "action": "organize",
            "scope": "notes_only",
            "sources": ["personal_note", "aggregate"],
            "module": "overview",
            "reason": "fallback_notes_only",
        }
    if _wants_guidance(user_goal):
        return {
            "action": "guide",
            "scope": "overall_history",
            "sources": ["aggregate"],
            "module": valid_module_id(module or "") or "overview",
            "reason": "fallback_writing_guidance",
        }
    if task_type == "recommend" or _has_any(user_goal, RECOMMEND_KEYS):
        return {
            "action": "recommend",
            "scope": "candidate_questions",
            "sources": ["candidate_question", "weakness_profile", "aggregate"],
            "module": valid_module_id(module or "") or "overview",
            "reason": "fallback_recommend",
        }
    if _has_any(user_goal, REWRITE_KEYS) and subject_ids:
        return {
            "action": "rewrite",
            "scope": "current_attempt",
            "sources": ["question", "attempt", "material", "reference_answer", "grading_report"],
            "module": valid_module_id(module or "") or "overview",
            "reason": "fallback_rewrite",
        }
    scope = "module_history" if valid_module_id(module or "") and valid_module_id(module or "") != "overview" else "overall_history"
    return {
        "action": "diagnose" if _has_any(user_goal, LOSS_KEYS) else "explain",
        "scope": scope,
        "sources": ["aggregate", "weakness_profile", "attempt", "grading_report", "personal_note"],
        "module": valid_module_id(module or "") or "overview",
        "reason": "fallback_history",
    }


def normalize_query_plan(plan=None, user_goal="", task_type="diagnosis", subject_ids=None, module=""):
    fallback = fallback_query_plan(user_goal, task_type, subject_ids, module)
    plan = plan if isinstance(plan, dict) else {}
    action = plan.get("action") if plan.get("action") in VALID_ACTIONS else fallback["action"]
    scope = plan.get("scope") if plan.get("scope") in VALID_SCOPES else fallback["scope"]
    raw_sources = plan.get("sources") if isinstance(plan.get("sources"), list) else fallback["sources"]
    sources = [source for source in raw_sources if source in VALID_SOURCES]
    if not sources:
        sources = fallback["sources"]
    normalized = {
        "action": action,
        "scope": scope,
        "sources": list(dict.fromkeys(sources)),
        "module": valid_module_id(plan.get("module") or module or fallback.get("module") or "") or "overview",
        "reason": plan.get("reason") or fallback.get("reason") or "fallback",
    }
    referential_followup = any(
        key in (user_goal or "")
        for key in ("这道", "第二道", "这个问题", "刚才", "那具体", "按你说的", "和上一次")
    )
    explicit_module_change = any(
        key in (user_goal or "")
        for key in ("归纳概括", "综合分析", "提出对策", "公文写作", "综合写作")
    )
    if referential_followup and module in {"summary", "analysis", "countermeasure", "document", "essay", "top_loss", "improvement"} and not explicit_module_change:
        normalized["module"] = module
    writing_guidance_requested = _wants_guidance(user_goal)
    explicit_note_organization = _wants_note_organization(user_goal)
    history_expansion_requested = any(
        key in (user_goal or "")
        for key in ("全部历史", "所有历史", "长期问题", "整体历史", "全量历史")
    )
    if task_type == "recommend":
        normalized["action"] = "recommend"
        normalized["scope"] = "candidate_questions"
        normalized["sources"] = ["candidate_question", "weakness_profile", "aggregate"]
        normalized["reason"] = "recommend_task_constraint"
    elif normalized["scope"] == "notes_only" and not explicit_note_organization:
        normalized = dict(fallback)
        normalized["reason"] = "reject_unrequested_notes_scope"
    if writing_guidance_requested and not explicit_note_organization and task_type != "recommend":
        was_already_guidance = normalized["action"] == "guide" and normalized["scope"] == "overall_history"
        normalized["action"] = "guide"
        normalized["scope"] = "overall_history"
        normalized["sources"] = ["aggregate"]
        if not was_already_guidance:
            normalized["reason"] = "writing_guidance_override"
    if writing_guidance_requested and task_type != "recommend" and "knowledge" not in normalized["sources"]:
        normalized["sources"].insert(0, "knowledge")
    if normalized["action"] in {"judge_structure", "rewrite"} and "knowledge" not in normalized["sources"]:
        normalized["sources"].append("knowledge")
    if history_expansion_requested and task_type != "recommend":
        normalized["action"] = "compare" if "比较" in (user_goal or "") else "diagnose"
        normalized["scope"] = "overall_history"
        normalized["sources"] = ["aggregate", "weakness_profile", "attempt", "grading_report", "personal_note"]
        normalized["reason"] = "explicit_history_expansion"
    if normalized["scope"] == "current_attempt" and not subject_ids:
        normalized["scope"] = "module_history" if normalized["module"] != "overview" else "overall_history"
        normalized["sources"] = ["aggregate", "weakness_profile", "attempt", "grading_report", "personal_note"]
        normalized["reason"] = "reject_missing_attempt_scope"
    if normalized["action"] in {"diagnose", "explain", "compare"} and normalized["scope"] in {"module_history", "overall_history"}:
        for source in ("attempt", "grading_report", "personal_note", "aggregate", "weakness_profile"):
            if source not in normalized["sources"]:
                normalized["sources"].append(source)
    notes_only_requested = explicit_note_organization and (
        normalized["scope"] == "notes_only"
        or (normalized["action"] == "organize" and "personal_note" in normalized["sources"])
    )
    if task_type == "review" and subject_ids and not history_expansion_requested and not notes_only_requested and normalized["action"] not in {"guide"}:
        normalized["scope"] = "current_attempt"
        for source in ["question", "attempt"]:
            if source not in normalized["sources"]:
                normalized["sources"].insert(0, source)
    if notes_only_requested:
        normalized["scope"] = "notes_only"
        normalized["sources"] = [
            source
            for source in ["knowledge", "personal_note", "aggregate"]
            if source in set(normalized["sources"]) or source == "personal_note"
        ]
        if "aggregate" not in normalized["sources"]:
            normalized["sources"].append("aggregate")
    return normalized


def route_from_plan(plan):
    action = (plan or {}).get("action")
    scope = (plan or {}).get("scope")
    if scope == "current_attempt":
        if action == "judge_structure":
            return "structure_judgement"
        if action == "rewrite":
            return "rewrite_attempt"
        return "current_attempt_review"
    if scope == "notes_only":
        return "note_organization"
    if action == "guide":
        return "writing_guidance"
    if action == "recommend" or scope == "candidate_questions":
        return "recommend_questions"
    if scope == "module_history":
        return "module_diagnosis"
    return "overview_diagnosis"


def route_rag(user_goal="", task_type="diagnosis", subject_ids=None, module=""):
    plan = normalize_query_plan(None, user_goal, task_type, subject_ids, module)
    return route_from_plan(plan)


def make_card(
    evidence_id,
    source_type,
    title,
    content,
    claim="",
    question_id=None,
    attempt_id=None,
    supports=None,
    url="",
    confidence=0.75,
    metadata=None,
):
    return {
        "evidence_id": evidence_id,
        "source_type": source_type,
        "title": title or "",
        "claim": claim or "",
        "content": clip_text(content),
        "question_id": question_id,
        "attempt_id": attempt_id,
        "supports": supports or [],
        "url": url or _url_for(question_id, attempt_id),
        "confidence": confidence,
        "metadata": metadata or {},
    }


def summarize_evidence_cards(cards, limit=10, content_limit=180):
    output = []
    for card in (cards or [])[:limit]:
        output.append(
            {
                "evidence_id": card.get("evidence_id"),
                "source_type": card.get("source_type"),
                "title": card.get("title"),
                "claim": card.get("claim"),
                "content": clip_text(card.get("content"), content_limit),
                "question_id": card.get("question_id"),
                "attempt_id": card.get("attempt_id"),
                "url": card.get("url"),
                "confidence": card.get("confidence"),
            }
        )
    return output


def evidence_sufficiency(rag_route, cards, module_context=None):
    cards = cards or []
    module_context = module_context or {}
    source_types = {card.get("source_type") for card in cards}
    coverage = module_context.get("coverage") or {}
    if not cards:
        return {
            "level": "insufficient",
            "label": "证据不足",
            "note": "本轮没有检索到可用证据，不能做确定性诊断。",
        }
    if rag_route in {"current_attempt_review", "structure_judgement", "rewrite_attempt"}:
        has_attempt = "attempt" in source_types
        has_question = "question" in source_types
        has_support = bool(source_types & {"grading_report", "reference_answer", "material"})
        if has_attempt and has_question and has_support:
            return {
                "level": "sufficient",
                "label": "本题证据充分",
                "note": "已包含题干、作答和至少一种材料/批改/参考答案证据，可用于本题判断。",
            }
        return {
            "level": "limited",
            "label": "本题证据有限",
            "note": "只能基于当前题目与作答做谨慎判断，不应扩展为长期能力结论。",
        }
    attempt_count = int(coverage.get("attempt_count") or 0)
    report_count = int(coverage.get("report_count") or 0)
    if rag_route in {"module_diagnosis", "overview_diagnosis"}:
        if attempt_count >= 3 and (report_count >= 1 or len(cards) >= 6):
            return {
                "level": "sufficient",
                "label": "历史证据充分",
                "note": "已覆盖多次作答及代表证据，可用于模块/总体趋势判断。",
            }
        return {
            "level": "limited",
            "label": "历史证据有限",
            "note": "历史样本偏少或批改报告不足，结论应表达为阶段性观察。",
        }
    if rag_route == "writing_guidance":
        return {
            "level": "sufficient",
            "label": "写法说明可用",
            "note": "本轮是概念或写法讲解，依据通用申论写法知识回答，不整理个人笔记，除非用户明确要求。",
        }
    if len(cards) >= 3:
        return {
            "level": "sufficient",
            "label": "推荐证据可用",
            "note": "已有候选题或画像证据，可用于择题建议。",
        }
    return {
        "level": "limited",
        "label": "推荐证据有限",
        "note": "候选题或画像证据较少，推荐理由需要保持谨慎。",
    }


def _url_for(question_id=None, attempt_id=None):
    if attempt_id:
        return f"/attempts/{attempt_id}"
    if question_id:
        return f"/questions/{question_id}"
    return ""


def _review_items(review_context):
    if not review_context:
        return []
    if review_context.get("attempt_reviews"):
        return review_context.get("attempt_reviews") or []
    return [review_context]


def cards_from_review_context(review_context, rag_route):
    cards = []
    supports = [rag_route, "current_attempt_review"]
    for item in _review_items(review_context):
        attempt = item.get("attempt") or {}
        question = item.get("question") or {}
        attempt_id = attempt.get("id")
        question_id = question.get("id") or attempt.get("question_id")
        if question:
            cards.append(
                make_card(
                    f"question:{question_id}",
                    "question",
                    f"{question.get('question_code', '')} {question.get('title', '')}".strip(),
                    "\n".join([question.get("prompt", ""), question.get("requirements", "")]),
                    claim="题干、作答任务和要求，用于判断回答方向与结构是否合题。",
                    question_id=question_id,
                    attempt_id=attempt_id,
                    supports=supports,
                    confidence=0.9,
                )
            )
        if attempt:
            cards.append(
                make_card(
                    f"attempt:{attempt_id}",
                    "attempt",
                    f"作答 {attempt_id}",
                    "\n".join([attempt.get("answer_text", ""), attempt.get("personal_note", "")]),
                    claim="用户本次作答文本和复盘笔记，是本题复盘与改写判断的主证据。",
                    question_id=question_id,
                    attempt_id=attempt_id,
                    supports=supports + ["rewrite_attempt"],
                    confidence=0.95,
                )
            )
        for material in item.get("materials") or []:
            number = material.get("material_number")
            cards.append(
                make_card(
                    f"material:{question_id}:{number}",
                    "material",
                    f"材料{number} {material.get('title', '')}".strip(),
                    material.get("content", ""),
                    claim="题目材料中的事实、变化、问题或对策线索。",
                    question_id=question_id,
                    attempt_id=attempt_id,
                    supports=supports + ["structure_judgement", "rewrite_attempt"],
                    confidence=0.82,
                    metadata={"material_number": number},
                )
            )
        for index, report in enumerate(item.get("reports") or [], start=1):
            cards.append(
                make_card(
                    f"report:{attempt_id}:{index}",
                    "grading_report",
                    f"批改报告 {index}",
                    report.get("report_text", ""),
                    claim="批改报告中的命中点、失分点和修改建议。",
                    question_id=question_id,
                    attempt_id=attempt_id,
                    supports=supports + ["loss_analysis", "rewrite_attempt"],
                    confidence=0.88,
                    metadata={"provider": report.get("provider"), "model": report.get("model")},
                )
            )
        for index, reference in enumerate(item.get("references") or [], start=1):
            cards.append(
                make_card(
                    f"reference:{question_id}:{index}",
                    "reference_answer",
                    f"参考答案 {reference.get('organization', index)}",
                    "\n".join([reference.get("answer_text", ""), reference.get("scoring_points", "")]),
                    claim="参考答案和采分点，用于判断缺漏、结构和改写方向。",
                    question_id=question_id,
                    attempt_id=attempt_id,
                    supports=supports + ["rewrite_attempt"],
                    confidence=0.78,
                )
            )
    return cards


def filter_cards_by_sources(cards, sources):
    sources = set(sources or [])
    if not sources:
        return cards
    source_aliases = {
        "personal_note": {"personal_note"},
        "knowledge": {"knowledge"},
        "grading_report": {"grading_report", "report"},
        "reference_answer": {"reference_answer"},
        "candidate_question": {"candidate_question"},
        "weakness_profile": {"weakness_profile"},
        "aggregate": {"aggregate"},
        "attempt": {"attempt"},
        "question": {"question"},
        "material": {"material"},
    }
    allowed = set()
    for source in sources:
        allowed.update(source_aliases.get(source, {source}))
    return [card for card in cards if card.get("source_type") in allowed]


def cards_from_module_context(module_context):
    cards = []
    module = module_context.get("module") or "overview"
    supports = ["module_diagnosis", module]
    coverage = module_context.get("coverage") or {}
    if coverage:
        cards.append(
            make_card(
                f"profile:{module}:coverage",
                "aggregate",
                f"{module_context.get('module_label', module)} 覆盖统计",
                json.dumps(
                    {
                        "coverage": coverage,
                        "source_counts": module_context.get("source_counts") or {},
                        "analysis_basis": module_context.get("analysis_basis") or {},
                        "problem_categories": module_context.get("problem_categories") or [],
                    },
                    ensure_ascii=False,
                ),
                claim="选定模块全量匹配历史的覆盖、来源和问题分布统计。",
                supports=supports,
                confidence=0.9,
            )
        )
    for index, item in enumerate(module_context.get("weakness_profile") or [], start=1):
        cards.append(
            make_card(
                f"profile:{module}:weakness:{index}",
                "weakness_profile",
                item.get("problem_type") or "能力画像",
                json.dumps(item, ensure_ascii=False),
                claim="长期能力画像中的高频弱项。",
                supports=supports,
                confidence=0.84,
            )
        )
    for chunk in module_context.get("evidence_chunks") or []:
        evidence_id = _normalize_evidence_id(chunk.get("evidence_ref"), chunk.get("source_type"), chunk.get("source_id"))
        cards.append(
            make_card(
                evidence_id,
                chunk.get("source_type") or "context",
                chunk.get("title") or evidence_id,
                chunk.get("body") or "",
                claim=f"模块代表证据：{chunk.get('source_type')}",
                question_id=chunk.get("question_id"),
                attempt_id=chunk.get("attempt_id"),
                supports=supports,
                confidence=(chunk.get("retrieval") or {}).get("rerank_score") or 0.76,
                metadata={"score": chunk.get("score"), "retrieval": chunk.get("retrieval") or {}},
            )
        )
    return cards


def _normalize_evidence_id(value, source_type="", source_id=None):
    value = value or ""
    match = re.search(r"attempt-(\d+)", value)
    if match:
        return f"attempt:{match.group(1)}"
    match = re.search(r"question-(\d+)", value)
    if match:
        return f"question:{match.group(1)}"
    if value:
        return value.replace(":", "-") if value.count(":") > 1 else value
    return f"{source_type}:{source_id}"


def cards_from_candidates(candidates, user_context=None):
    cards = []
    for question in candidates or []:
        question_id = question.get("id")
        cards.append(
            make_card(
                f"question:{question_id}",
                "candidate_question",
                f"{question.get('question_code', '')} {question.get('title', '')}".strip(),
                json.dumps(question, ensure_ascii=False),
                claim="候选训练题，可用于择题推荐。",
                question_id=question_id,
                supports=["recommend_questions"],
                confidence=0.78,
            )
        )
    for index, item in enumerate((user_context or {}).get("weakness_profile") or [], start=1):
        cards.append(
            make_card(
                f"profile:global:{index}",
                "weakness_profile",
                item.get("problem_type") or "训练弱项",
                json.dumps(item, ensure_ascii=False),
                claim="用户长期训练画像中的弱项，用于解释为什么推荐这些题。",
                supports=["recommend_questions", "overview_diagnosis"],
                confidence=0.75,
            )
        )
    return cards


def cards_from_notes(conn, limit=80):
    rows = conn.execute(
        """
        SELECT a.id AS attempt_id,
               a.question_id,
               a.personal_note,
               a.created_at,
               q.title,
               q.question_code,
               q.question_type,
               q.year,
               q.region
          FROM attempts a
          JOIN questions q ON q.id = a.question_id
         WHERE TRIM(COALESCE(a.personal_note, '')) <> ''
      ORDER BY a.created_at DESC, a.id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    cards = [
        make_card(
            "notes:all",
            "aggregate",
            "全部复盘笔记索引",
            json.dumps(
                [
                    {
                        "attempt_id": row["attempt_id"],
                        "question_id": row["question_id"],
                        "title": row["title"],
                        "question_type": row["question_type"],
                        "created_at": row["created_at"],
                        "note": clip_text(row["personal_note"], 220),
                    }
                    for row in rows
                ],
                ensure_ascii=False,
            ),
            claim=f"当前共读取 {len(rows)} 条复盘笔记，用于整理注意事项清单。",
            supports=["note_organization"],
            confidence=0.9,
            metadata={"note_count": len(rows)},
        )
    ]
    for row in rows:
        title = " ".join(
            str(part)
            for part in [row["question_code"], row["title"]]
            if part
        ).strip() or f"作答 {row['attempt_id']} 复盘笔记"
        cards.append(
            make_card(
                f"note:{row['attempt_id']}",
                "personal_note",
                title,
                row["personal_note"],
                claim="用户在该次作答后记录的复盘笔记，可归纳为后续作答注意事项。",
                question_id=row["question_id"],
                attempt_id=row["attempt_id"],
                supports=["note_organization"],
                confidence=0.86,
                metadata={
                    "question_type": row["question_type"],
                    "year": row["year"],
                    "region": row["region"],
                    "created_at": row["created_at"],
                },
            )
        )
    return cards


def cards_from_knowledge(conn, user_goal="", module="overview", limit=8):
    cards = []
    for item in retrieve_knowledge_evidence(conn, module, user_goal, limit=limit):
        metadata = item.get("metadata") or {}
        knowledge_id = metadata.get("knowledge_id") or item.get("evidence_ref")
        tags = metadata.get("tags") or []
        claim_parts = [
            "申论教材库条目",
            metadata.get("module_label") or metadata.get("module") or "",
            "、".join(str(tag) for tag in tags[:5]),
        ]
        cards.append(
            make_card(
                knowledge_id,
                "knowledge",
                item.get("title") or knowledge_id,
                item.get("body") or "",
                claim="；".join(part for part in claim_parts if part),
                supports=["writing_guidance", metadata.get("module") or "overview"],
                confidence=(item.get("retrieval") or {}).get("rerank_score") or 0.86,
                metadata={**metadata, "retrieval": item.get("retrieval") or {}},
            )
        )
    return cards


def build_rag_context(conn, task_type, user_goal, subject_ids=None, module="", filters=None, user_context=None, candidates=None, review_context=None, query_plan=None):
    subject_ids = subject_ids or []
    filters = filters or {}
    module = valid_module_id(module or "")
    query_plan = normalize_query_plan(query_plan, user_goal, task_type, subject_ids, module)
    module = query_plan.get("module") or module
    rag_route = route_from_plan(query_plan)
    module_context = {}
    cards = []
    if rag_route in {"current_attempt_review", "structure_judgement", "rewrite_attempt"}:
        cards = cards_from_review_context(review_context or {}, rag_route)
        if "knowledge" in query_plan.get("sources", []) or rag_route in {"structure_judgement", "rewrite_attempt"}:
            cards.extend(cards_from_knowledge(conn, user_goal, module, limit=6))
    elif rag_route == "note_organization":
        cards = cards_from_notes(conn)
        if "knowledge" in query_plan.get("sources", []):
            cards.extend(cards_from_knowledge(conn, user_goal, module, limit=6))
    elif rag_route == "writing_guidance":
        cards = cards_from_knowledge(conn, user_goal, module, limit=10)
    elif rag_route == "recommend_questions":
        cards = cards_from_candidates(candidates or [], user_context)
    else:
        module_context = retrieve_module_evidence(conn, module or "overview", user_goal, filters)
        module_context["candidate_questions"] = candidates or []
        cards = cards_from_module_context(module_context)
        if candidates:
            cards.extend(cards_from_candidates(candidates, user_context)[:5])
    cards = filter_cards_by_sources(cards, query_plan.get("sources"))
    cards = cards[:36]
    sufficiency = evidence_sufficiency(rag_route, cards, module_context)
    return {
        "query_plan": query_plan,
        "rag_route": rag_route,
        "retrieval_policy": policy_for_route(rag_route),
        "evidence_cards": cards,
        "evidence_sufficiency": sufficiency,
        "module_context": module_context,
        "grounding_contract": {
            "must_cite_evidence_id": True,
            "allowed_evidence_ids": [card["evidence_id"] for card in cards],
            "current_attempt_only": rag_route in {"current_attempt_review", "structure_judgement", "rewrite_attempt"},
            "evidence_sufficiency": sufficiency,
        },
    }


def policy_for_route(rag_route):
    return {
        "current_attempt_review": "Use only the selected attempt, its question, materials, references, and grading reports.",
        "structure_judgement": "Judge structure using the selected attempt, prompt requirements, materials, and report evidence.",
        "rewrite_attempt": "Rewrite using the selected answer, references, scoring points, materials, and report feedback.",
        "recommend_questions": "Recommend from candidate questions using user profile and filters.",
        "note_organization": "Organize the user's personal review notes into an actionable checklist; do not diagnose from unrelated attempts or reports.",
        "writing_guidance": "Answer the user's writing-method question directly with concept, structures, types, examples, and pitfalls; do not organize personal notes unless explicitly requested.",
        "module_diagnosis": "Analyze module-wide history with aggregate coverage plus representative evidence cards.",
        "overview_diagnosis": "Analyze overall history with aggregate coverage plus representative evidence cards.",
    }.get(rag_route, "Use retrieved evidence cards and do not invent unsupported facts.")

import json

from ..agent_modules import _search_chunks, retrieve_knowledge_evidence
from ..db import connect
from ..grading import limited_reference_guidance, word_limit_budget
from .common import (
    ANSWER_GRID_RULES,
    QUESTION_TYPE_MODULES,
    RUBRIC_VERSION,
    _clean,
    _full_reference_context,
    _row_dict,
    _word_budget_guidance,
    question_display_max_score,
    reference_set_hash,
    rubric_source_hash,
)
from .rubric import _default_criteria, _material_text, validate_rubric


def _dimension_score_template(dimensions):
    return [
        {
            "dimension": item.get("dimension"),
            "max_score": float(item.get("weight") or 0),
            "score": float(item.get("weight") or 0),
            "reason": "先确认已满足之处，再说明有证据支持的扣分项",
        }
        for item in dimensions
        if isinstance(item, dict) and item.get("dimension")
    ]


DIMENSION_SCORING_GUIDANCE = """维度分采用“得分制”，不是扣分值、错误数、概率或0—1置信度：先以该维度 max_score 为起点，再按有证据的缺陷扣分。
- 完全或基本符合：给该维度满分的80%—100%；单个措辞、标题语气、编号或轻微重复问题通常仍在此区间。
- 存在明确的一般问题，但主体功能仍然成立：给60%—80%。
- 多处严重问题，已经明显损害任务完成：给30%—60%。
- 低于30%只用于该维度几乎缺失、文种/结构根本错误或表达大面积不可理解。
- 低于50%时，reason 必须指出至少两个严重且具体的缺陷及其实际影响；理由若只描述轻微问题，分数不得低于70%。
- 称谓、落款等仅在题干或相应文种确实要求时评分；题目不要求的要素缺失不得扣分。"""


def _evidence_card(row, role, confidence=None):
    source_type = row.get("source_type") or "context"
    question_id = row.get("question_id")
    attempt_id = row.get("attempt_id")
    if source_type == "knowledge":
        metadata = row.get("metadata") or {}
        evidence_id = metadata.get("knowledge_id") or row.get("evidence_ref") or f"knowledge:{row.get('source_id')}"
    else:
        evidence_id = f"{source_type}:{row.get('source_id') or row.get('id')}"
    return {
        "evidence_id": evidence_id,
        "source_type": source_type,
        "role": role,
        "title": _clean(row.get("title"), 100),
        "snippet": _clean(row.get("body") or row.get("content"), 220),
        "question_id": question_id,
        "attempt_id": attempt_id,
        "url": f"/attempts/{attempt_id}" if attempt_id else (f"/questions/{question_id}" if question_id else ""),
        "confidence": round(float(confidence if confidence is not None else row.get("_rerank_score") or 0.72), 4),
    }


def retrieve_grading_evidence(conn, question, attempt, rubric, options):
    try:
        index_state = conn.execute("SELECT dirty, full_rebuild FROM agent_context_index_state WHERE id = 1").fetchone()
        index_dirty = bool(index_state and (index_state["dirty"] or index_state["full_rebuild"]))
    except Exception:
        index_dirty = True
    module_id = QUESTION_TYPE_MODULES.get(question.get("question_type"), "overview")
    query = " ".join(
        [
            question.get("question_type") or "",
            _clean(question.get("prompt"), 240),
            _clean(question.get("requirements"), 160),
            " ".join(point.get("label") or "" for point in rubric.get("points", [])[:12]),
        ]
    )
    current_cards = [
        {
            "evidence_id": f"question:{question.get('id')}",
            "source_type": "question",
            "role": "current_scoring",
            "title": _clean(question.get("title") or question.get("question_code"), 100),
            "snippet": _clean(f"{question.get('prompt') or ''} {question.get('requirements') or ''}", 220),
            "question_id": question.get("id"),
            "attempt_id": None,
            "url": f"/questions/{question.get('id')}",
            "confidence": 1.0,
        }
    ]
    for reference in rubric.get("selected_references") or []:
        reference_id = reference.get("reference_id")
        current_cards.append(
            {
                "evidence_id": f"reference_answer:{reference_id}",
                "source_type": "reference_answer",
                "role": "current_scoring",
                "title": f"本题参考答案 · {reference.get('organization') or reference_id}",
                "snippet": "本题已选择的机构答案全文，直接参与共识评分基准与本次批改。",
                "question_id": question.get("id"),
                "attempt_id": None,
                "url": f"/questions/{question.get('id')}",
                "confidence": 1.0,
            }
        )
    material_cards = {}
    for point in rubric.get("points", []):
        for item in point.get("material_evidence", []):
            number = item.get("material_number")
            if number in material_cards:
                continue
            material_cards[number] = {
                "evidence_id": f"material:{question.get('id')}:{number}",
                "source_type": "material",
                "role": "current_scoring",
                "title": f"本题材料{number}",
                "snippet": _clean(item.get("quote"), 220),
                "question_id": question.get("id"),
                "attempt_id": None,
                "url": f"/questions/{question.get('id')}#material-{number}",
                "confidence": 1.0,
            }
    current_cards.extend(material_cards.values())
    cards = []
    if options.get("analogies", True):
        scope = {
            "module": module_id,
            "module_label": question.get("question_type") or "",
            "filters": {"question_type": question.get("question_type") or ""},
            "source_types": ["question"],
        }
        rows = _search_chunks(conn, scope, query, limit=30, prefer_dense=False)
        seen_questions = set()
        for row in rows:
            row = dict(row)
            question_id = row.get("question_id")
            relevance = float(row.get("_rerank_score") or 0)
            if (
                not question_id
                or int(question_id) == int(question.get("id") or 0)
                or question_id in seen_questions
                or relevance < 0.58
            ):
                continue
            seen_questions.add(question_id)
            cards.append(_evidence_card(row, "method_calibration"))
            if len([card for card in cards if card["role"] == "method_calibration"]) >= 3:
                break
    if options.get("knowledge", True):
        for item in retrieve_knowledge_evidence(
            conn,
            module_id,
            query,
            limit=3,
            ensure_index=False,
            prefer_dense=False,
        ):
            row = {
                "source_type": "knowledge",
                "source_id": item.get("source_id"),
                "title": item.get("title"),
                "body": item.get("body"),
                "metadata": item.get("metadata") or {},
                "evidence_ref": item.get("evidence_ref"),
            }
            cards.append(
                _evidence_card(row, "method_calibration", (item.get("retrieval") or {}).get("rerank_score") or 0.82)
            )
    history_cards = []
    if options.get("history", True):
        scope = {
            "module": module_id,
            "module_label": question.get("question_type") or "",
            "filters": {"question_type": question.get("question_type") or ""},
            "source_types": ["grading_report", "personal_note", "attempt"],
        }
        rows = _search_chunks(conn, scope, query, limit=30, prefer_dense=False)
        seen = set()
        for row in rows:
            row = dict(row)
            attempt_id = row.get("attempt_id")
            if not attempt_id or int(attempt_id) == int(attempt.get("id") or 0):
                continue
            key = (row.get("source_type"), attempt_id)
            if key in seen:
                continue
            seen.add(key)
            history_cards.append(_evidence_card(row, "personalization"))
            if len(history_cards) >= 4:
                break
    cards.extend(history_cards)
    total = 0
    bounded = []
    for card in cards:
        remaining = 3500 - total
        if remaining <= 0:
            break
        card["snippet"] = card["snippet"][:remaining]
        total += len(card["snippet"])
        bounded.append(card)
    distinct_history_attempts = {card.get("attempt_id") for card in history_cards if card.get("attempt_id")}
    return current_cards + bounded, {
        "history_attempt_count": len(distinct_history_attempts),
        "history_stable": len(distinct_history_attempts) >= 2
        and any(card["source_type"] in {"grading_report", "personal_note"} for card in history_cards),
        "index_dirty": index_dirty,
        "retrieval_mode": "lightweight_snapshot",
    }


def build_combined_grading_prompt(
    question,
    materials,
    references,
    attempt,
    consensus,
    evidence,
    custom_answer="",
    history_meta=None,
    question_feedback=None,
):
    history_meta = history_meta or {}
    reference_context = _full_reference_context(references or [])
    word_budget = word_limit_budget(question.get("word_limit") or "")
    budget_guidance = _word_budget_guidance(word_budget)
    question_context = {
        key: question.get(key)
        for key in (
            "id",
            "question_code",
            "paper_name",
            "exam_type",
            "year",
            "region",
            "question_type",
            "title",
            "prompt",
            "requirements",
            "word_limit",
            "zhejiang_relevance",
            "is_full_original",
        )
    }
    question_context["word_budget"] = word_budget
    question_context["display_max_score"] = question_display_max_score(question)
    attempt_context = {
        "id": attempt.get("id"),
        "created_at": attempt.get("created_at"),
        "saved_word_count": attempt.get("word_count"),
        "answer_text": attempt.get("answer_text") or "",
    }
    feedback_calibration = [
        {
            "point_key": row.get("point_key"),
            "corrected_status": row.get("corrected_status"),
            "confirmed_expression": _clean(row.get("corrected_quote"), 180),
            "note": _clean(row.get("note"), 220),
        }
        for row in (question_feedback or [])
        if row.get("corrected_status") in {"hit", "partial", "miss"}
    ][:20]

    dimension_profile = _default_criteria(question.get("question_type"))
    dimension_score_template = _dimension_score_template(dimension_profile)
    return f"""你正在执行申论单次智能联合批改。请在一次响应中先建立独立评分基准，再分析全部采分点，最后按题型维度综合评分。

最高事实来源是本题题干与材料。机构答案只是候选解释。没有本题材料依据的内容不得成为主要扣分点。
建立 rubric 时不得根据本次作答增删采分点或改变权重；本次作答只能用于 evaluation。

本题完整信息：
{json.dumps(question_context, ensure_ascii=False)}

本题占格要求：{budget_guidance}
{ANSWER_GRID_RULES}

本题材料：
{_material_text(materials)}

本题已选择的机构参考答案全文（共 {len(reference_context)} 份，属于本题主证据）：
{json.dumps(reference_context, ensure_ascii=False)}
{limited_reference_guidance(len(reference_context))}

本地对全部机构答案分句、向量聚类后的候选共识：
{json.dumps(consensus, ensure_ascii=False)}

用户补充参考答案：
{custom_answer or "无"}

本次作答：
{json.dumps(attempt_context, ensure_ascii=False)}

跨题、知识和历史最小证据：
{json.dumps(evidence, ensure_ascii=False)}

历史证据状态：{json.dumps(history_meta, ensure_ascii=False)}

同题人工纠错校准：
{json.dumps(feedback_calibration, ensure_ascii=False)}

本题固定维度框架（满分合计100）：
{json.dumps(dimension_profile, ensure_ascii=False)}

{DIMENSION_SCORING_GUIDANCE}

输出保持干练：每个 reason、weight_reason 最多 60 字；aliases 每点最多 3 个；
annotations 最多 6 条；material_reading 最多 6 条；optimization_suggestions 最多 5 条；
personalized_findings 最多 3 条。不要重复题干、材料或参考答案全文。
personalized_findings 必须做深层归因，而不是复述症状：每条都要写清“反复出现的现象 →
导致它的具体作答机制/原因 → 下一步练什么”。禁止出现“多次遗漏要点”“多次失分”“需要加强”
这类只有结论没有机制的句子；root_cause 要指出具体环节（如审题时未先圈定任务动词、
提取材料时按自然段逐段摘抄而没有先做主题归并、要点堆叠后未回读题干核对对象）。

只输出一个由 <smart_grading_json> 与 </smart_grading_json> 包裹的合法 JSON：
<smart_grading_json>
{{
  "rubric": {{
    "question_id": {int(question.get("id") or 0)},
    "task_constraints": {{"object": "", "required_structure": [], "format_rules": []}},
    "points": [{{
      "point_key": "point-1",
      "label": "简短采分点名",
      "canonical_expression": "规范表达",
      "aliases": ["同义表达"],
      "tier": "core|material_core|supporting|disputed",
      "importance": "critical|major|supporting",
      "suggested_weight": 0.0,
      "weight_reason": "为什么该点权重高或低",
      "required_for_full_score": true,
      "required_elements": ["不可缺少的语义"],
      "optional_details": ["可省略的例子或修饰"],
      "minimum_expression": "最短完整写法",
      "material_evidence": [{{"material_number": 1, "quote": "材料连续原文"}}],
      "reference_ids": [1],
      "confidence": 0.9
    }}],
    "equal_weight_reason": "",
    "conflicts": []
  }},
  "evaluation": {{
    "point_matches": [{{"point_key": "", "status": "hit|partial|miss", "coverage_ratio": 0.0, "answer_quote": "用户答案连续原文", "reason": "覆盖或缺失说明"}}],
    "dimension_scores": {json.dumps(dimension_score_template, ensure_ascii=False)},
    "holistic_adjustment_reason": "",
    "annotations": [{{"kind": "good|polish|change|delete|add|critical", "severity": "positive|low|medium|high|critical", "quote": "非补充类必须为用户答案连续原文", "anchor": "补充类必须为用户答案连续原文，表示插入在此句之后", "replacement": "", "reason": "", "point_key": ""}}],
    "reference_fusion": "共性核心点和差异补充点",
    "material_reading": ["材料信息 -> 可转化要点 -> 答案表达"],
    "optimization_suggestions": ["具体建议"],
    "personalized_findings": [{{"finding": "跨题共性现象", "root_cause": "导致该现象的具体作答机制/原因", "next_step": "下一步针对这个原因练什么", "evidence_ids": [""], "confidence": "stage|recurring"}}],
    "overall_summary": "说明整体完成情况、最大得分处和最大失分处",
    "revised_answer": "可直接替换的修改版答案正文"
  }}
}}
</smart_grading_json>

规则：
1. rubric 的采分点权重总和必须等于 content 维度满分。明确评分标准中的数值优先；否则依据任务必要性、材料层级和机构共识动态分配，禁止无理由平均分配。
2. rubric.point_key 只使用 point-1、point-2 这类 ASCII 标识；evaluation.point_matches 必须逐字复制对应的 rubric.point_key，不得翻译、改写或另起编号。先逐点分析，再给 dimension_scores。内容分是结合重点覆盖、准确性、完整性和材料转化质量的综合判断，不得机械等于逐点覆盖加总。
3. dimension_scores 必须逐项覆盖固定维度；JSON 中 max_score 只用于明确尺度，score 必须遵守上述得分制标尺且在0到 max_score之间。各维度问题只扣一次，不再输出整体质量乘数。
4. hit/partial 的 answer_quote 必须是用户答案中的连续原文；找不到只能判 miss。annotations 中除 add 外 quote 必须是连续原文；add 必须填写可在原文精确定位的 anchor，并把拟补文字写入 replacement。
5. coaching context（跨题、知识、历史证据）只用于建议和 personalized_findings，绝不能影响 point_matches 或 dimension_scores。
6. personalized_findings 的 finding 必须指出跨题共性（至少 2 条证据支撑才算 recurring）；root_cause 分析具体环节而不是复述症状；next_step 给出可执行的下一道题训练动作。宁可少写一条，也不要写空话。
7. 修改版答案以 suggested_min—suggested_max 为目标，低于硬上限并预留至少8格。
8. 只输出上述单个 JSON 块，不输出 Markdown 或额外解释。
"""


def _save_rubric_to_db(db_path, question, references, materials, settings, feedback, parsed, consensus=None):
    consensus = consensus or {}
    ref_hash = reference_set_hash(references)
    source_hash = rubric_source_hash(question, materials, references)
    rubric = validate_rubric(parsed, question, materials, references, feedback)
    rubric["source_hash"] = source_hash
    rubric["reference_set_hash"] = ref_hash
    rubric["consensus_summary"] = {
        "embedding_model": consensus.get("embedding_model"),
        "organization_count": consensus.get("organization_count"),
        "source_clause_count": consensus.get("source_clause_count"),
    }
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO grading_rubrics (
                question_id, reference_set_hash, source_hash, rubric_version,
                provider, model, rubric_json, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', CURRENT_TIMESTAMP)
            ON CONFLICT(question_id, reference_set_hash, source_hash, rubric_version) DO UPDATE SET
                provider = excluded.provider,
                model = excluded.model,
                rubric_json = excluded.rubric_json,
                status = 'ready',
                error_text = '',
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                question["id"],
                ref_hash,
                source_hash,
                RUBRIC_VERSION,
                settings.get("provider_name") or "",
                settings.get("model") or "",
                json.dumps(rubric, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM grading_rubrics
             WHERE question_id = ? AND reference_set_hash = ? AND source_hash = ? AND rubric_version = ?
            """,
            (question["id"], ref_hash, source_hash, RUBRIC_VERSION),
        ).fetchone()
    return _row_dict(row), rubric


def build_grading_prompt(
    question,
    materials,
    attempt,
    rubric,
    evidence,
    custom_answer="",
    history_meta=None,
    question_feedback=None,
    references=None,
):
    history_meta = history_meta or {}
    reference_context = _full_reference_context(references or [])
    question_context = {
        key: question.get(key)
        for key in (
            "id",
            "question_code",
            "paper_name",
            "exam_type",
            "year",
            "region",
            "question_type",
            "title",
            "prompt",
            "requirements",
            "word_limit",
            "zhejiang_relevance",
            "is_full_original",
        )
    }
    question_context["word_budget"] = word_limit_budget(question.get("word_limit") or "")
    question_context["display_max_score"] = question_display_max_score(question)
    attempt_context = {
        "id": attempt.get("id"),
        "created_at": attempt.get("created_at"),
        "saved_word_count": attempt.get("word_count"),
        "answer_text": attempt.get("answer_text") or "",
    }
    feedback_calibration = [
        {
            "point_key": row.get("point_key"),
            "corrected_status": row.get("corrected_status"),
            "confirmed_expression": _clean(row.get("corrected_quote"), 180),
            "note": _clean(row.get("note"), 220),
        }
        for row in (question_feedback or [])
        if row.get("corrected_status") in {"hit", "partial", "miss"}
    ][:20]
    dimension_profile = rubric.get("dimensions") or rubric.get("criteria") or _default_criteria(
        question.get("question_type")
    )
    dimension_score_template = _dimension_score_template(dimension_profile)
    budget_guidance = _word_budget_guidance(question_context["word_budget"])
    return f"""你正在使用已校验的不等权评分基准执行申论综合批改。先分析全部采分点，再按评分基准中的固定维度综合评分。
评分只能依据 current_scoring：本题信息、材料、参考答案和评分基准。coaching_context 只用于点评建议，不得影响任何得分。

与旧批改包一致的本题完整信息：
{json.dumps(question_context, ensure_ascii=False)}

本题材料：
{_material_text(materials)}

系统校验后的评分基准：
{json.dumps(rubric, ensure_ascii=False)}

本题已选择的机构参考答案全文（共 {len(reference_context)} 份，属于 current_scoring 主证据，旧批改模式中的答案、采分点和备注均完整保留）：
{json.dumps(reference_context, ensure_ascii=False)}
{limited_reference_guidance(len(reference_context))}

用户补充参考答案（只能作为本题补充候选，必须服从材料）：
{custom_answer or "无"}

本次作答：
{json.dumps(attempt_context, ensure_ascii=False)}

coaching_context（跨题、知识和历史最小证据）：
{json.dumps(evidence, ensure_ascii=False)}

历史证据状态：{json.dumps(history_meta, ensure_ascii=False)}

本题修改版答案占格要求：{budget_guidance}

同题人工纠错校准：
{json.dumps(feedback_calibration, ensure_ascii=False)}

本题各维度的明确满分与输出模板：
{json.dumps(dimension_score_template, ensure_ascii=False)}

{DIMENSION_SCORING_GUIDANCE}

输出保持干练：每个 reason 最多 60 字；annotations 最多 6 条；
material_reading 最多 6 条；optimization_suggestions 最多 5 条；
personalized_findings 最多 3 条。不要重复题干、材料或参考答案全文。
personalized_findings 必须做深层归因，而不是复述症状：每条都要写清“反复出现的现象 →
导致它的具体作答机制/原因 → 下一步练什么”。禁止出现“多次遗漏要点”“多次失分”“需要加强”
这类只有结论没有机制的句子；root_cause 要指出具体环节（如审题时未先圈定任务动词、
提取材料时按自然段逐段摘抄而没有先做主题归并、要点堆叠后未回读题干核对对象）。

只输出一个由 <smart_grading_json> 与 </smart_grading_json> 包裹的合法 JSON：
<smart_grading_json>
{{
  "evaluation": {{
    "point_matches": [{{"point_key": "", "status": "hit|partial|miss", "coverage_ratio": 0.0, "answer_quote": "用户答案连续原文", "reason": "覆盖或缺失说明"}}],
    "dimension_scores": {json.dumps(dimension_score_template, ensure_ascii=False)},
    "holistic_adjustment_reason": "",
    "annotations": [{{"kind": "good|polish|change|delete|add|critical", "severity": "positive|low|medium|high|critical", "quote": "非补充类必须为用户答案连续原文", "anchor": "补充类必须为用户答案连续原文，表示插入在此句之后", "replacement": "", "reason": "", "point_key": ""}}],
    "reference_fusion": "共性核心点和差异补充点",
    "material_reading": ["材料信息 -> 可转化要点 -> 答案表达"],
    "optimization_suggestions": ["具体建议"],
    "personalized_findings": [{{"finding": "跨题共性现象", "root_cause": "导致该现象的具体作答机制/原因", "next_step": "下一步针对这个原因练什么", "evidence_ids": [""], "confidence": "stage|recurring"}}],
    "overall_summary": "说明整体完成情况、最大得分处和最大失分处",
    "revised_answer": "可直接替换的修改版答案正文"
  }}
}}
</smart_grading_json>

规则：
1. 每个可计分 point_key 必须且只能出现一次，并逐字复制评分基准中的 point_key，不得翻译、改写或另起编号；先在原答案中查找同义表达，避免误判漏点。
2. point_matches 的每项还必须输出 coverage_ratio（0—1）。hit 固定为1，miss固定为0；partial 根据 required_elements 中实际覆盖的核心语义给出0.1—0.9，不得把所有 partial 机械写成0.5。简洁同义表达完整覆盖核心语义时应判 hit，不能因没写 optional_details 而降分。
3. hit/partial 的 answer_quote 必须是用户答案中的连续原文；找不到原句只能判 miss。annotations 中除 add 外 quote 必须是连续原文；add 必须填写可在原文精确定位的 anchor，并把拟补文字写入 replacement。
4. dimension_scores 必须逐项覆盖评分基准 dimensions；max_score 只用于明确尺度，score 必须遵守上述得分制标尺且在0到 max_score之间。内容分需综合全部不等权采分点、准确性、完整性和材料转化质量，不得机械逐点相加。
5. 同一个问题只能在最相关维度扣一次，不得再使用 overall_quality_ratio、总分系数或统一封顶。
6. recurring 只在 history_stable=true 时使用，否则写 stage。
7. personalized_findings 只能引用上面存在且 role=personalization 的 evidence_id。
8. personalized_findings 的 finding 必须指出跨题共性（至少 2 条证据支撑才算 recurring）；root_cause 分析具体环节而不是复述症状；next_step 给出可执行的下一道题训练动作。宁可少写一条，也不要写空话。
9. 修改版答案必须按结构化 word_budget 生成，以 suggested_min—suggested_max 为目标，并至少预留8格安全余量。{ANSWER_GRID_RULES}
10. 输出修改版答案前先在内部按上述规则估算占格；除文种或结构确有需要外避免手动换行，因为换行会结算当前行剩余格。不得用空话凑字数，也不得为了写全 optional_details 挤占 required 点。
11. 不直接输出总分；系统将各维度 score 相加，并缩放到原题满分。
12. 同题人工纠错优先用于识别同义表达，但本次 hit/partial 仍必须给出当前用户答案中的连续原句。
13. 上面的机构参考答案数量大于 0 时，reference_fusion 必须说明实际纳入的机构答案及其共性/差异，严禁写“无参考答案”“无额外参考答案”或“未提供参考答案”。
"""

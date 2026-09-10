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
    question_word_limit_text,
    reference_set_hash,
    rubric_source_hash,
)
from .rubric import _default_criteria, _material_text, validate_rubric


def _dimension_score_template(dimensions):
    return [
        {
            "dimension": item.get("dimension"),
            "max_score": float(item.get("weight") or 0),
            "score": 0,
            "reason": "依据考场标准，从实际完成质量出发给分并说明依据",
        }
        for item in dimensions
        if isinstance(item, dict) and item.get("dimension")
    ]


DIMENSION_SCORING_GUIDANCE = """维度分采用符合真实考场阅卷强度的“得分制”，不是从满分起步的扣分制，也不是概率或0—1置信度。必须先看实际完成质量，再从0分向上给分。
- 90%—100%仅用于几乎可直接作为考场高分范文的表现：任务完整、材料转化准确、重点突出且基本没有可见缺陷；普通“写到了”不能进入此档。
- 75%—89%用于完成度较高但仍有明确提升空间的表现。
- 60%—74%用于主体任务基本成立、同时存在常见遗漏或一般质量问题的考场中档答案。
- 40%—59%用于完成不充分、遗漏较多或结构表达明显影响阅卷识别的答案。
- 低于40%用于核心任务大面积缺失、严重偏题、文种/结构根本错误或表达大面积不可理解。
- 不得因答案语句通顺就默认给结构、表达、格式高分；每个高于80%的维度都必须指出达到高分档的具体证据。
- 称谓、落款等仅在题干或相应文种确实要求时评分；题目不要求的要素缺失不得扣分。
- 【错别字免扣分铁律】：考生在电脑端作答采用键盘输入，拼音同音字、联想误差等非原则性错别字一律免予扣分，重在采分点语义识别与材料提取能力。"""


ESSAY_SCORING_GUIDANCE = """综合写作必须先判断整篇文章所处的整体档位，再把该档位总分合理分配到各维度；不能先给每个维度“看起来不错”的高比例后相加。
- 80—100：罕见的考场优秀范文。立意深刻准确，主要论证线均充分展开，材料转化准确且几乎没有事实、逻辑或表达硬伤。
- 70—79：明显高于一般水平。立意、结构和主要论证均较强，材料名称与事实基本准确，不得存在主要段落空泛或明显材料误读。
- 60—69：主体任务成立的中上档。立意与结构较好，但存在一个主要部分论证偏薄、若干材料事实不准确，或论据转化较普通等常见问题。
- 50—59：基本切题但完成质量一般，论证、材料转化或结构存在多处明显不足。
- 50以下：偏题、任务完成不充分，或论证结构存在严重缺陷。
若诊断中已经指出“事实偏差/材料误读”且某一主要论证部分空泛、缺少展开，通常不得给到80分以上；语言流畅、标题完整和分段清楚本身不足以进入优秀档。"""


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
    single_reference_policy = (
        "本题只有一份粉笔标准答案：它是内容采分点的唯一边界。只能拆分该答案已经写出的语义；材料不得新增采分点或追加必需细节。"
        if len(reference_context) == 1 and question.get("question_type") != "综合写作"
        else "按题干、材料和所选参考答案共同核验评分边界。"
    )
    word_budget = word_limit_budget(question_word_limit_text(question))
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

你是申论阅卷与诊断老师，不是参考答案作者。{single_reference_policy} 白鹭和小马哥只用于拆点、归并与同义表达识别，绝不用于生成完整答案。
【错别字免扣分铁律】：考生采用电脑键盘拼音输入法打字作答，同音错别字、输入法联想失误等非原则性错字属正常录入现象，一律免予扣分，严禁以此作为扣分依据！评分核心在于采分点语义识别与材料提取能力；只要语义表达能识别出采分点（无论是否存在同音错别字），均须判定为命中给分！
建立 rubric 时不得根据本次作答增删采分点或改变权重；本次作答只能用于 evaluation。

本题完整信息：
{json.dumps(question_context, ensure_ascii=False)}

本题占格要求：{budget_guidance}
{ANSWER_GRID_RULES}

本题材料：
{_material_text(materials)}

本题已选择的机构参考答案全文（共 {len(reference_context)} 份；只有一份时只能从该答案切分计分点，不得依据材料自主补点）：
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
      "group_key": "group-1",
      "group_label": "粉笔答案中的一级要点",
      "group_order": 1,
      "point_order": 1,
      "label": "完整给分点名称",
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
      "reference_quote": "粉笔答案中仅对应当前小点的连续原文",
      "material_evidence": [{{"material_number": 1, "quote": "材料连续原文"}}],
      "reference_ids": [1],
      "confidence": 0.9
    }}],
    "equal_weight_reason": "",
    "conflicts": []
  }},
  "evaluation": {{
    "point_matches": [{{"point_key": "", "status": "hit|partial|miss", "score_level": "full|mostly|half|slight|none", "coverage_ratio": 0.0, "answer_quote": "尽量使用用户答案短且连续的原文", "reason": "覆盖或缺失说明", "confidence": 0.0, "missing_elements": []}}],
    "dimension_scores": {json.dumps(dimension_score_template, ensure_ascii=False)},
    "holistic_adjustment_reason": "",
    "annotations": [{{"kind": "good|polish|change|delete|add|critical", "severity": "positive|low|medium|high|critical", "quote": "非补充类必须为用户答案连续原文", "anchor": "补充类必须为用户答案连续原文，表示插入在此句之后", "replacement": "", "reason": "", "point_key": ""}}],
    "redundancies": [{{"quote": "用户答案中完全未采分的自创套话、超纲展开或多余修饰", "wasted_chars": 20, "reason": "为何未采分且冗余", "suggestion": "建议精简或删除"}}],
    "reference_fusion": "共性核心点和差异补充点",
    "material_reading": ["材料信息 -> 可转化要点 -> 答案表达"],
    "optimization_suggestions": ["具体建议"],
    "personalized_findings": [{{"finding": "跨题共性现象", "root_cause": "导致该现象的具体作答机制/原因", "next_step": "下一步针对这个原因练什么", "evidence_ids": [""], "confidence": "stage|recurring"}}],
    "summary": {{"verdict": "不含分数的整体判断", "strengths": ["主要优点"], "weaknesses": ["主要问题"]}}
  }}
}}
</smart_grading_json>

规则：
1. rubric 的采分点权重总和必须等于 content 维度满分。明确评分标准中的数值优先；否则依据任务必要性、材料层级和机构共识动态分配，禁止无理由平均分配。
2. rubric.point_key 只使用 point-1、point-2 这类 ASCII 标识；evaluation.point_matches 必须逐字复制对应的 rubric.point_key，不得翻译、改写或另起编号。先逐点分析，再给 dimension_scores。
3. dimension_scores 必须逐项覆盖固定维度；JSON 中 max_score 只用于明确尺度，score 必须遵守上述得分制标尺且在0到 max_score之间。
4. 【核心要义对齐原则】：申论参考答案不唯一。只要用户答案表达的动作机制、工作手段、目标成效与采分小点的核心要义一致（近义概括、同义替换），必须判定为 hit 命中，绝不因未出现参考答案字面原词而误判！
5. 【真实阅卷式划点】：先按粉笔答案识别一级要点组，再把每组合理归并为1—3个完整给分点；20分题通常共5—10个给分点，每点一般1—3分。不得把每个词、地点、案例拆成0.5分碎片，也不得把整组措施合成一个笼统大点。
6. 【同一状态源】：每个 rubric 点必须提供粉笔答案中的逐字 reference_quote；evaluation 只判该 point_key 的 hit/partial/miss。参考答案着色、用户答案着色和得分都将直接使用这个状态，禁止另设一套判断。
7. 【合理分档】：同义核心完整必须 hit/full。partial 只用于给分点自身缺少实质语义，并选择 mostly/half/slight；不得因为材料额外细节降分。
5. 【结构体例刚性扣分】：若题目材料按地区、主体或案例分设（如J县、K县、M县；或总分结构），用户答案若抹去主体、案例归属不清或缺失总述，必须在 structure/结构维度及主体采分点上进行硬扣分（扣1~2分），并在点评中严厉指出，绝不可放水！
6. 【冗余废话深度排查】：在 redundancies 列表中，列出用户答案中与所有采分要义均无关联的文字（无信息增量套话、超纲细微展开、主观脑补），指出具体占用字数与删减理由。
7. hit/partial 应提供用户答案中的短连续原文；若同一要点散落在多处，可用“……”连接多个按原文顺序出现的短片段，不得因此改判 miss。annotations 中除 add 外 quote 必须是连续原文。
8. coaching context（跨题、知识、历史证据）只用于建议和 personalized_findings，绝不能影响 point_matches 或 dimension_scores。
9. 只判断用户作答是否符合字数与格式要求；参考答案和批改诊断不计入用户作答字数。不得输出任何完整替代答案。
10. 只输出上述单个 JSON 块，不输出 Markdown 或额外解释。
"""


def _save_rubric_to_db(
    db_path,
    question,
    references,
    materials,
    settings,
    feedback,
    parsed,
    consensus=None,
    prevalidated=False,
):
    consensus = consensus or {}
    ref_hash = reference_set_hash(references)
    source_hash = rubric_source_hash(question, materials, references)
    rubric = parsed if prevalidated else validate_rubric(parsed, question, materials, references, feedback)
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
    fenbi_tree = rubric.get("scoring_mode") == "fenbi_tree"
    if fenbi_tree:
        single_reference_policy = (
            "本题评分标准已由粉笔踩分树固化。只能逐点判断 hit/partial/miss，"
            "禁止新增、删除、合并、拆散或重算任何采分点，禁止依据材料补充必写细节。"
        )
    else:
        single_reference_policy = (
            "本题只有一份粉笔标准答案：它是内容采分点的唯一边界。不得从材料新增采分点或追加标答未写的必需细节。"
            if len(reference_context) == 1 and question.get("question_type") != "综合写作"
            else "按已校验评分基准逐点判定。"
        )
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
    question_context["word_budget"] = word_limit_budget(question_word_limit_text(question))
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
    essay_guidance = ESSAY_SCORING_GUIDANCE if question.get("question_type") == "综合写作" else ""
    scoring_mode_label = "粉笔固定踩分树" if fenbi_tree else "已校验的不等权评分基准"
    fenbi_tree_instruction = (
        "【固定踩分树】：下面的 points 是唯一可判分点，weight/display_weight/full_mark 已经固化；"
        "禁止重新划点、合点、拆点或重算权重，point_key 必须原样使用。\n"
        if fenbi_tree
        else ""
    )
    return f"""你正在使用{scoring_mode_label}执行申论综合批改。你是阅卷与诊断老师，不是参考答案作者；只分析采分点、材料证据、用户作答覆盖和固定维度得分。
评分只能依据 current_scoring：{single_reference_policy} 用户不要求逐字一致，核心动作、对象或效果与 reference_quote 同义就必须判 hit。partial 只适用于 reference_quote 自身包含的核心语义确实只写了一半；严禁因为材料里另有地点、案例、政策名、技术名而降分。白鹭和小马哥仅用于拆点和同义识别。禁止生成、改写、压缩或润色任何完整答案。coaching_context 只用于点评建议，不得影响任何得分。

与旧批改包一致的本题完整信息：
{json.dumps(question_context, ensure_ascii=False)}

本题材料：
{_material_text(materials)}

{fenbi_tree_instruction}系统校验后的评分基准：
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

本次用户作答占格要求：{budget_guidance}
{ANSWER_GRID_RULES}

同题人工纠错校准：
{json.dumps(feedback_calibration, ensure_ascii=False)}

本题各维度的明确满分与输出模板：
{json.dumps(dimension_score_template, ensure_ascii=False)}

{DIMENSION_SCORING_GUIDANCE}

{essay_guidance}

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
    "point_matches": [{{"point_key": "", "status": "hit|partial|miss", "score_level": "full|mostly|half|slight|none", "coverage_ratio": 0.0, "answer_quote": "尽量使用用户答案短且连续的原文", "reason": "覆盖或缺失说明", "confidence": 0.0, "missing_elements": []}}],
    "dimension_scores": {json.dumps(dimension_score_template, ensure_ascii=False)},
    "holistic_adjustment_reason": "",
    "annotations": [{{"kind": "good|polish|change|delete|add|critical", "severity": "positive|low|medium|high|critical", "quote": "非补充类必须为用户答案连续原文", "anchor": "补充类必须为用户答案连续原文，表示插入在此句之后", "replacement": "", "reason": "", "point_key": ""}}],
    "redundancies": [{{"quote": "用户答案中完全未采分的自创套话、超纲展开或多余修饰", "wasted_chars": 20, "reason": "为何未采分且冗余", "suggestion": "建议精简或删除"}}],
    "reference_fusion": "共性核心点和差异补充点",
    "material_reading": ["材料信息 -> 可转化要点 -> 答案表达"],
    "optimization_suggestions": ["具体建议"],
    "personalized_findings": [{{"finding": "跨题共性现象", "root_cause": "导致该现象的具体作答机制/原因", "next_step": "下一步针对这个原因练什么", "evidence_ids": [""], "confidence": "stage|recurring"}}],
    "summary": {{"verdict": "不含分数的整体判断", "strengths": ["主要优点"], "weaknesses": ["主要问题"]}}
  }}
}}
</smart_grading_json>

规则：
1. 每个可计分 point_key 必须且只能出现一次，并逐字复制评分基准中的 point_key，不得翻译、改写或另起编号。
2. 【核心同义即全分】：只要用户答案的核心动作、对象或效果与 reference_quote 同义，必须判 hit，不要求逐字一致。“瞄准细分领域”对应“以细分领域作为切入口”、“打造产业先行区”对应“划定产业先行区”，都必须全分。不得用 reference_quote 没写出的材料细节降为 partial。
3. 【结构体例刚性扣分】：若题目材料按地区、主体或案例分设（如J县、K县、M县；或总分结构），用户答案若抹去主体、案例归属不清或缺失总述，必须在 structure/结构维度及主体采分点上进行硬扣分（扣1~2分），并在点评中严厉指出，绝不可放水！
4. 【冗余废话深度排查】：在 redundancies 列表中，列出用户答案中与所有采分要义均无关联的文字（无信息增量套话、超纲细微展开、主观脑补），指出具体占用字数与删减理由。
5. point_matches 按完整给分点判定。核心意思完整为 hit/full=1；部分命中必须选择 mostly=0.75、half=0.5 或 slight=0.25，并明确指出 reference_quote 自身缺少的核心语义；miss/none=0。0.5只是分档刻度，禁止把参考答案每个词机械切成0.5分。
6. hit/partial 应提供用户答案中的短连续原文；若语义散落在多处，可用“……”连接按顺序出现的多个短片段。
7. dimension_scores 必须逐项覆盖评分基准 dimensions；max_score 只用于明确尺度，score 必须遵守上述得分制标尺且在0到 max_score之间。
8. 你是阅卷与诊断老师，不是答案作者。禁止输出、改写、压缩或润色任何完整答案；禁止生成“修改版答案”“名师答案”“小马哥版”或“白鹭版”。白鹭和小马哥只用于拆点、归并、识别材料原词和同义表达。
9. 不直接输出总分、分数算式、折算分或等级；系统将各维度 score 相加、校准并缩放到原题满分。
10. 只有一份粉笔答案时，reference_fusion 必须明确“仅按唯一粉笔答案切分采分点”。材料明确但粉笔没写的内容不得设为 material_core 内容分，也不得在 reason、missing_elements、annotations 或优化建议中伪装成必写答案。
"""

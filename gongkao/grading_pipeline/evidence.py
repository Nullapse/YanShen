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


ESSAY_SCORING_GUIDANCE = """【袁东大作文（综合写作）专业评审方法论】：
一、审题与主题类型判断（核心立意与论证框架）：
袁东大作文体系核心：不是先想“怎么写”，而是先判断“是什么类型”。从题干判断主题类型 → 按类型确定分论点框架 → 从题干和材料填充关键词。
1. 单主题：题干围绕单一核心概念（如“以‘创新驱动发展’为主题”）。
   - 框架：围绕单一主题的多个维度展开（为什么重要、怎么做、从哪些方面发力）。
2. 双主题：
   - AB型：题干为两个并列/对立/互补概念（如“守正与创新”“变与不变”“有为与不为”“效率与公平”）。
     * 分论点一：A 对 B 的影响/作用/前提（如：守正是创新的前提，守好根本创新才不跑偏）；
     * 分论点二：B 对 A 的影响/作用/保障（如：创新是守正的保障，没有创新守正会僵化）；
     * 分论点三：A 和 B 双向奔赴、互相作用产生更大价值，推动事业行稳致远。
   - ABC型：题干出现三个概念，A和B共同服务于C（如“以法治和德治推动社会治理现代化”）。
     * 分论点一：A 对 C 的作用（刚性约束与制度保障）；
     * 分论点二：B 对 C 的作用（柔性引导与价值支撑）；
     * 分论点三：A + B 刚柔并济、协同发力对 C 的整体推动。
3. 多主题：出现三个及以上并列概念（如“改革、发展、稳定”）。
   - 框架：强调“组合拳”式整体效应（分论点一：改革是动力；分论点二：发展是目的；分论点三：稳定是前提）。
* 评分依据：严查考生中心论点与分论点是否严格符合所属主题类型的推导逻辑，立意是否切合题意，有无偏题、跑题或核心概念割裂。

二、分论点寻找法与材料结合度（三步走原则，严禁脱离材料空发议论）：
1. Step 1 从题干圈定核心词（标题与分论点必须直接包含题干关键词）；
2. Step 2 从给定材料寻找观点句、典型做法与成效，作为分论点与论据的直接依据；
3. Step 3 关联前序小题材料提取论据与案例，实现全卷材料融会贯通。
* 核心动作：分论点核心术语与论据必须来自题目或材料，严禁通篇空洞套话或脱离材料的主观发挥。

三、五段三分规范结构：
1. 标题（1行）：观点式（直接亮明中心论点）、对仗式（两短语对仗）或比喻式，必须直接关联题干核心词。
2. 首段（150-200字）：破题入题 + 亮明中心论点。
3. 分论点一、二、三（各250-300字）：每段严格遵循“明确论点句前置 + 政策理论阐释 + 典型案例分析”的论证链条。
4. 尾段（100-150字）：回扣中心论点 + 结合国家发展大局总结升华。

四、整篇综合档位定级：
综合写作必须先判断整篇文章所处的整体档位，再把该档位总分合理分配到各维度；不能先给每个维度高比例后相加。
- 80—100分（一类文/优秀范文）：罕见的考场优秀范文。审题类型判断精准，立意深刻高远，五段三分严密，主要论证线均充分展开，紧扣材料关键词且几乎没有事实、逻辑或表达硬伤。
- 70—79分（二类文/良好）：明显高于一般水平。切题准确，中心论点鲜明，符合五段三分，立意、结构和主要论证均较强，材料名称与事实基本准确，不得存在主要段落空泛或明显材料误读。
- 60—69分（三类文/主体任务成立中上档）：立意与结构较好，但存在一个主要部分论证偏薄、若干材料事实不准确，或论据转化较普通等常见问题。
- 50—59分：基本切题但完成质量一般，论证、材料转化或结构存在多处明显不足。
- 50分以下：偏题、任务完成不充分，或论证结构存在严重缺陷。
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

    is_essay = question.get("question_type") == "综合写作"
    if is_essay:
        role_instruction = (
            "你正在评审申论大作文（综合写作）。请严格依据【袁东大作文方法论】进行专业审题判断与定级赋分。"
            "你是阅卷与诊断老师，不是参考答案作者；只分析立意、分论点框架、材料论据转化与固定维度得分。"
            "禁止生成、改写、压缩或润色任何完整答案。coaching_context 只用于点评建议，不得影响任何得分。"
        )
        essay_guidance = ESSAY_SCORING_GUIDANCE
    else:
        role_instruction = (
            f"你正在执行申论小题联合批改。你是阅卷与诊断老师，不是参考答案作者；"
            "系统已提供明确的采分点和分值，AI 绝不生成任何答案，只逐点判断用户在这些得分点的得分情况。\n"
            f"评分只能依据 current_scoring：{single_reference_policy} "
            "【全面性与准确性阶梯评分铁律】：真实申论阅卷与粉笔评分均以“全面、准确”为核心尺度。\n"
            "1. 既全面又准确才可判 hit/full（100%全分）：核心要素完整（举措、对象、成效无缺失），专业规范提炼精准，不苛求机械逐字一致，但绝不可放水；\n"
            "2. 概括不全面或不够准确必须判 partial 阶梯赋分：若表述过于宽泛、笼统、口语化、提炼偏弱（不够准确），或遗漏重要宾语/成效/并列要点（概括不全），严禁判 full，必须判定为 partial，并严格根据缺损程度梯次定档（mostly=0.75、half=0.5 或 slight=0.25），同时在 missing_elements 和 reason 中明确指出具体是不全面（漏了什么）还是不够准确（何处泛化/口语化）；\n"
            "3. 严禁因为材料中未作要求的细枝末节（如额外地名、人名）苛扣，但参考答案本身的核心要素与精准提炼必须严格考核；\n"
            "4. 完全未答或理解错误判 miss/none=0。\n"
            "禁止生成、改写、压缩或润色任何完整答案。coaching_context 只用于点评建议，不得影响任何得分。"
        )
        essay_guidance = DIMENSION_SCORING_GUIDANCE

    dimension_profile = _default_criteria(question.get("question_type"))
    dimension_score_template = _dimension_score_template(dimension_profile)
    return f"""{role_instruction}
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

{essay_guidance}

输出保持干练：每个 reason、weight_reason 最多 60 字；aliases 每点最多 3 个；
annotations 最多 6 条。不要重复题干、材料或参考答案全文。

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
    "summary": {{"verdict": "不含分数的整体判断", "strengths": ["主要优点"], "weaknesses": ["主要问题"]}}
  }}
}}
</smart_grading_json>

规则：
1. rubric 的采分点权重总和必须等于 content 维度满分。明确评分标准中的数值优先；否则依据任务必要性、材料层级和机构共识动态分配，禁止无理由平均分配。
2. rubric.point_key 只使用 point-1、point-2 这类 ASCII 标识；evaluation.point_matches 必须逐字复制对应的 rubric.point_key，不得翻译、改写或另起编号。先逐点分析，再给 dimension_scores。
3. dimension_scores 必须逐项覆盖固定维度；JSON 中 max_score 只用于明确尺度，score 必须遵守上述得分制标尺且在0到 max_score之间。
4. 【全面性与准确性审查】：申论答案不苛求机械逐字一致，但必须以“全面”与“准确”为硬性审查指标。既全面又准确才判命中满分；概括不全面或不够准确严禁给满分。
5. 【真实阅卷式划点】：先按粉笔答案识别一级要点组，再把每组合理归并为1—3个完整给分点；20分题通常共5—10个给分点，每点一般1—3分。不得把每个词、地点、案例拆成0.5分碎片，也不得把整组措施合成一个笼统大点。
6. 【同一状态源】：每个 rubric 点必须提供粉笔答案中的逐字 reference_quote；evaluation 只判该 point_key 的 hit/partial/miss。参考答案着色、用户答案着色和得分都将直接使用这个状态，禁止另设一套判断。
7. 【梯次分档给分】：核心意思完整为 hit/full=1；部分命中必须选择 mostly=0.75、half=0.5 或 slight=0.25，0.5只是分档刻度，禁止把参考答案每个词机械切成0.5分；miss/none=0。
   - hit/full=1.0：既全面又准确，核心举措、对象与成效要素完整，规范提炼精准。
   - partial mostly=0.75：表达准确，主体框架完整，仅有极细微要素或规范修饰语轻微欠缺。
   - partial half=0.5：①概括不全面（如写出举措但遗漏关键对象/成效，或并列项只答出一半）；②不够准确（表述过于宽泛、笼统、口语化，未精准提炼材料要义）。
   - partial slight=0.25：零星沾边或仅提及个别词汇，缺乏完整准确逻辑。
   - miss/none=0：完全未答或答非所问。
   判定 partial 必须在 missing_elements 和 reason 中明确说明是不全面还是不够准确。不得因同义沾边就无原则判满分。
8. hit/partial 应提供用户答案中的短连续原文；若同一要点散落在多处，可用“……”连接多个按原文顺序出现的短片段，不得因此改判 miss。annotations 中除 add 外 quote 必须是连续原文。
9. 你是阅卷与诊断老师，不是答案作者。禁止输出、改写、压缩或润色任何完整答案；禁止自创任何替代答案。
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
    is_essay = question.get("question_type") == "综合写作"
    if is_essay:
        role_instruction = (
            "你正在评审申论大作文（综合写作）。请严格依据【袁东大作文方法论】进行专业审题判断与定级赋分。"
            "你是阅卷与诊断老师，不是参考答案作者；只分析立意、分论点框架、材料论据转化与固定维度得分。"
            "禁止生成、改写、压缩或润色任何完整答案。coaching_context 只用于点评建议，不得影响任何得分。"
        )
    else:
        role_instruction = (
            f"你正在使用{scoring_mode_label}执行申论小题批改。你是阅卷与诊断老师，不是参考答案作者；"
            "系统已提供明确的采分点和分值，AI 绝不生成任何答案，只逐点判断用户在这些得分点的得分情况。\n"
            f"评分只能依据 current_scoring：{single_reference_policy} "
            "【全面性与准确性阶梯评分铁律】：真实申论阅卷与粉笔评分均以“全面、准确”为核心尺度。\n"
            "1. 既全面又准确才可判 hit/full（100%全分）：核心要素完整（举措、对象、成效无缺失），专业规范提炼精准，不苛求机械逐字一致，但绝不可放水；\n"
            "2. 概括不全面或不够准确必须判 partial 阶梯赋分：若表述过于宽泛、笼统、口语化、提炼偏弱（不够准确），或遗漏重要宾语/成效/并列要点（概括不全），严禁判 full，必须判定为 partial，并严格根据缺损程度梯次定档（mostly=0.75、half=0.5 或 slight=0.25），同时在 missing_elements 和 reason 中明确指出具体是不全面（漏了什么）还是不够准确（何处泛化/口语化）；\n"
            "3. 严禁因为材料中未作要求的细枝末节（如额外地名、人名）苛扣，但参考答案本身的核心要素与精准提炼必须严格考核；\n"
            "4. 完全未答或理解错误判 miss/none=0。\n"
            "禁止生成、改写、压缩或润色任何完整答案。coaching_context 只用于点评建议，不得影响任何得分。"
        )

    return f"""{role_instruction}

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

输出保持干练：每个 reason 最多 60 字；annotations 最多 6 条。不要重复题干、材料或参考答案全文。

只输出一个由 <smart_grading_json> 与 </smart_grading_json> 包裹的合法 JSON：
<smart_grading_json>
{{
  "evaluation": {{
    "point_matches": [{{"point_key": "", "status": "hit|partial|miss", "score_level": "full|mostly|half|slight|none", "coverage_ratio": 0.0, "answer_quote": "尽量使用用户答案短且连续的原文", "reason": "覆盖或缺失说明", "confidence": 0.0, "missing_elements": []}}],
    "dimension_scores": {json.dumps(dimension_score_template, ensure_ascii=False)},
    "holistic_adjustment_reason": "",
    "annotations": [{{"kind": "good|polish|change|delete|add|critical", "severity": "positive|low|medium|high|critical", "quote": "非补充类必须为用户答案连续原文", "anchor": "补充类必须为用户答案连续原文，表示插入在此句之后", "replacement": "", "reason": "", "point_key": ""}}],
    "summary": {{"verdict": "不含分数的整体判断", "strengths": ["主要优点"], "weaknesses": ["主要问题"]}}
  }}
}}
</smart_grading_json>

规则：
1. 每个可计分 point_key 必须且只能出现一次，并逐字复制评分基准中的 point_key，不得翻译、改写或另起编号。
2. 【全面性与准确性审查】：既全面又准确才判 hit/full（1.0）。若核心意思虽然沾边，但概括不够全面（缺少关键对象、举措或成效）或不够准确（用词泛化、笼统、口语化、未能提炼出专业规范词），严禁判 full，必须判定为 partial！不得因个别词同义就放水给全分。
3. 【梯次分档赋分】：point_matches 按真实阅卷尺度梯次定档。核心意思完整为 hit/full=1；部分命中必须选择 mostly=0.75、half=0.5 或 slight=0.25，0.5只是分档刻度，禁止把参考答案每个词机械切成0.5分；miss/none=0。
   - hit/full=1.0：全面且准确，核心要素完整，规范提炼精准。
   - partial mostly=0.75：表达准确，主体框架完整，仅有个别细微要素或规范修饰语轻微欠缺。
   - partial half=0.5：①概括不全面（如写出举措但漏掉对象/成效，或并列项只答一半）；②不够准确（表述过于宽泛、大而化之、口语化，未精准提炼材料专业规范要义）。
   - partial slight=0.25：零星沾边或仅写出个别词，缺乏完整准确逻辑。
   - miss/none=0：完全未答或答非所问。
   判定 partial 必须在 missing_elements 和 reason 中明确说明是不全面（漏了什么）还是不够准确（何处宽泛/口语化）。禁止把参考答案机械切成碎片。
4. hit/partial 应提供用户答案中的短连续原文；若语义散落在多处，可用“……”连接按顺序出现的多个短片段。
5. dimension_scores 必须逐项覆盖评分基准 dimensions；max_score 只用于明确尺度，score 必须遵守上述得分制标尺且在0到 max_score之间。
6. 你是阅卷与诊断老师，不是答案作者。禁止输出、改写、压缩或润色任何完整答案；禁止自创任何替代答案。
7. 不直接输出总分、分数算式、折算分或等级；系统将各维度 score 相加、校准并缩放到原题满分。
"""

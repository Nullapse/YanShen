import json
import re

from ..agent_modules import FEATURE_HASH_MODEL, _cosine, _embed_text
from ..grading import limited_reference_guidance, normalize_reference_answer_text, word_limit_budget
from .common import (
    ANSWER_GRID_RULES,
    CONSENSUS_MAX_MATERIAL_CLAUSES,
    CRITERION_LABELS,
    QUESTION_TYPE_PROFILES,
    RUBRIC_VERSION,
    _canonical_organization,
    _clean,
    _extract_matching_quote,
    _full_reference_context,
    _hash,
    _row_dict,
    _word_budget_guidance,
    dedupe_references,
    question_display_max_score,
    question_score_is_estimated,
    question_word_limit_text,
    reference_set_hash,
    rubric_source_hash,
    split_semantic_clauses,
)


def _embed_texts(_conn, texts):
    """Embed grading-consensus hints without starting the heavyweight dense model.

    These vectors only cluster reference-answer clauses before the formal model
    call.  They are not scoring evidence, so the deterministic feature hash is
    both sufficient and dramatically faster for long exam materials.
    """
    return [_embed_text(text)[0] for text in texts], FEATURE_HASH_MODEL


def compact_reference_consensus(conn, references, materials, similarity_threshold=0.82):
    references = dedupe_references(references)
    clauses = []
    for reference in references:
        for clause in split_semantic_clauses(reference.get("answer_text"))[:24]:
            clauses.append(
                {
                    "text": clause,
                    "reference_id": int(reference["id"]),
                    "organization": _canonical_organization(reference),
                }
            )
    if not clauses:
        return {"embedding_model": FEATURE_HASH_MODEL, "organization_count": len(references), "clusters": []}

    vectors, embedding_model = _embed_texts(conn, [item["text"] for item in clauses])
    clusters = []
    for clause, vector in zip(clauses, vectors):
        best_index = -1
        best_score = -1.0
        for index, cluster in enumerate(clusters):
            score = _cosine(vector, cluster["vector"])
            if score > best_score:
                best_index, best_score = index, score
        if best_index >= 0 and best_score >= similarity_threshold:
            cluster = clusters[best_index]
            cluster["items"].append(clause)
            if len(clause["text"]) < len(cluster["representative"]):
                cluster["representative"] = clause["text"]
                cluster["vector"] = vector
        else:
            clusters.append({"representative": clause["text"], "vector": vector, "items": [clause]})

    organization_count = len(references)
    single_reference_scoring = organization_count == 1 and question.get("question_type") != "综合写作"
    core_threshold = max(2, (organization_count + 1) // 2)
    cluster_candidates = []
    for index, cluster in enumerate(clusters, start=1):
        organizations = sorted({item["organization"] for item in cluster["items"]})
        reference_ids = sorted({item["reference_id"] for item in cluster["items"]})
        cluster_candidates.append(
            {
                "cluster_id": f"cluster-{index}",
                "representative": cluster["representative"],
                "vector": cluster["vector"],
                "reference_ids": reference_ids,
                "organizations": organizations,
                "support_org_count": len(organizations),
                "consensus_candidate": len(organizations) >= core_threshold,
            }
        )
    cluster_candidates.sort(key=lambda item: item["support_org_count"], reverse=True)
    common = [item for item in cluster_candidates if item["support_org_count"] >= 2]
    rare = [item for item in cluster_candidates if item["support_org_count"] == 1][:12]
    selected_clusters = (common + rare)[:36]

    material_clauses = []
    per_material_limit = max(
        12,
        CONSENSUS_MAX_MATERIAL_CLAUSES // max(1, len(materials)),
    )
    for material in materials:
        clauses_for_material = split_semantic_clauses(
            material.get("content"),
            minimum=8,
            maximum=120,
        )[:per_material_limit]
        for clause in clauses_for_material:
            material_clauses.append(
                {
                    "material_number": material.get("material_number"),
                    "quote": clause,
                }
            )
    material_clauses = material_clauses[:CONSENSUS_MAX_MATERIAL_CLAUSES]
    material_vectors, _ = (
        _embed_texts(conn, [item["quote"] for item in material_clauses]) if material_clauses else ([], embedding_model)
    )

    output = []
    for cluster in selected_clusters:
        best_material = None
        best_material_score = 0.0
        for material, vector in zip(material_clauses, material_vectors):
            score = _cosine(cluster["vector"], vector)
            if score > best_material_score:
                best_material, best_material_score = material, score
        output.append(
            {
                "cluster_id": cluster["cluster_id"],
                "representative": cluster["representative"],
                "reference_ids": cluster["reference_ids"],
                "organizations": cluster["organizations"],
                "support_org_count": cluster["support_org_count"],
                "consensus_candidate": cluster["consensus_candidate"],
                "material_candidate": best_material,
                "material_similarity": round(best_material_score, 4),
            }
        )
    output.sort(key=lambda item: (item["support_org_count"], item["material_similarity"]), reverse=True)
    return {
        "embedding_model": embedding_model,
        "preprocessing_mode": "lightweight",
        "degraded": False,
        "organization_count": organization_count,
        "core_threshold": core_threshold,
        "source_clause_count": len(clauses),
        "material_clause_count": len(material_clauses),
        "clusters": output,
    }


def _score_tree_from_reference(reference):
    reference = _row_dict(reference)
    raw = reference.get("score_tree_json") or reference.get("score_tree")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def fenbi_tree_available(references):
    return any(_score_tree_from_reference(reference) for reference in dedupe_references(references))


def _tree_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _fenbi_tree_points(tree):
    points = []

    def visit(node, ancestors):
        name = _clean(node.get("name"), 220)
        full_mark = _tree_number(node.get("full_mark") or node.get("fullMark"))
        children = node.get("children") or []
        if not children:
            if full_mark:
                points.append(
                    {
                        "node": node,
                        "name": name or "未命名采分点",
                        "full_mark": full_mark,
                        "ancestors": list(ancestors),
                    }
                )
            return full_mark

        child_total = 0.0
        for child in children:
            child_total += visit(child, ancestors + [name])
        residual = round(full_mark - child_total, 4)
        if residual > 0.0001:
            if not ancestors and name in {"得分分析", ""}:
                label = "发展等级/综合表达"
            else:
                label = f"{name or '其他'}（其他）"
            points.append(
                {
                    "node": node,
                    "name": label,
                    "full_mark": residual,
                    "ancestors": list(ancestors),
                }
            )
            return full_mark
        return child_total

    visit(tree, [])
    return points


def build_fenbi_tree_rubric(question, references):
    """Build a fixed rubric directly from the Fenbi score-analysis tree."""

    question = _row_dict(question)
    references = dedupe_references(references)
    reference = next(
        (item for item in references if _score_tree_from_reference(item)),
        None,
    )
    if reference is None:
        raise ValueError("本题没有可用的粉笔踩分树")
    tree = _score_tree_from_reference(reference)
    display_max = float(question_display_max_score(question) or 100)
    points = []
    for index, item in enumerate(_fenbi_tree_points(tree), start=1):
        node = item["node"]
        full_mark = item["full_mark"]
        node_id = node.get("id")
        point_key = f"fenbi-{node_id}" if node_id is not None else f"fenbi-point-{index}"
        group_label = item["ancestors"][-1] if item["ancestors"] else "得分分析"
        comment = _clean(node.get("comment"), 600)
        importance = "critical" if full_mark >= 3 else ("major" if full_mark >= 1 else "supporting")
        weight = round(full_mark / max(display_max, 1) * 100, 4)
        points.append(
            {
                "point_key": point_key,
                "group_key": f"fenbi-group-{max(1, len(item['ancestors']))}",
                "group_label": group_label,
                "group_order": len(item["ancestors"]),
                "point_order": index,
                "label": item["name"],
                "canonical_expression": item["name"],
                "aliases": [],
                "tier": "core",
                "importance": importance,
                "suggested_weight": weight,
                "weight": weight,
                "display_weight": round(full_mark, 2),
                "weight_reason": comment[:240] or "粉笔踩分树原分值。",
                "required_for_full_score": True,
                "required_elements": [],
                "optional_details": [],
                "minimum_expression": item["name"],
                "reference_quote": item["name"],
                "material_evidence": [],
                "reference_ids": [int(reference["id"])],
                "confidence": 1.0,
                "score_role": "required",
                "coverage_role": "required",
                "alternative_group": "",
                "is_structural": False,
                "source_comment": comment,
            }
        )

    if not points:
        raise ValueError("粉笔踩分树没有可用的计分点")

    dimensions = [
        {
            "criterion_id": "content-1",
            "dimension": "content",
            "description": "粉笔踩分树逐点判分",
            "weight": 100,
        }
    ]
    return {
        "schema_version": RUBRIC_VERSION,
        "question_id": int(question.get("id") or 0),
        "max_score": 100,
        "display_max_score": display_max,
        "score_is_estimated": question_score_is_estimated(question),
        "question_type": question.get("question_type") or "",
        "word_limit": question_word_limit_text(question),
        "scoring_mode": "fenbi_tree",
        "selected_reference_count": len(references),
        "selected_references": [
            {
                "reference_id": int(item["id"]),
                "id": int(item["id"]),
                "organization": _canonical_organization(item),
                "answer_text": str(item.get("answer_text") or "").strip(),
            }
            for item in references
        ],
        "mapped_reference_ids": [int(reference["id"])],
        "reference_mapping_status": "mapped",
        "task_constraints": {},
        "word_budget": word_limit_budget(question.get("word_limit") or ""),
        "points": points,
        "equal_weight_reason": "",
        "dimensions": dimensions,
        "criteria": dimensions,
        "conflicts": [],
        "source": "fenbi_score_tree",
    }


def manual_grading_basis(conn, question, materials, references):
    question = _row_dict(question)
    materials = [_row_dict(row) for row in materials]
    references = [_row_dict(row) for row in references]
    ref_hash = reference_set_hash(references)
    source_hash = rubric_source_hash(question, materials, references)
    row = conn.execute(
        """
        SELECT rubric_json FROM grading_rubrics
         WHERE question_id = ? AND reference_set_hash = ? AND source_hash = ?
           AND rubric_version = ? AND status = 'ready'
      ORDER BY updated_at DESC LIMIT 1
        """,
        (question.get("id"), ref_hash, source_hash, RUBRIC_VERSION),
    ).fetchone()
    if row:
        return {"kind": "cached_rubric", "rubric": json.loads(row["rubric_json"])}
    if fenbi_tree_available(references):
        return {
            "kind": "fenbi_tree",
            "rubric": build_fenbi_tree_rubric(question, references),
        }
    # The local sentence clusters are retrieval hints for the AI rubric builder,
    # not verified scoring points.  Never expose them as a manual grading basis.
    return {"kind": "uncached"}


def _material_text(materials):
    return "\n\n".join(
        f"材料{material.get('material_number')} {material.get('title') or ''}\n{material.get('content') or ''}"
        for material in materials
    )


def build_rubric_prompt(question, materials, references, consensus):
    reference_context = _full_reference_context(references)
    effective_word_limit = question_word_limit_text(question)
    word_budget = word_limit_budget(effective_word_limit)
    budget_guidance = _word_budget_guidance(word_budget)
    profile = QUESTION_TYPE_PROFILES.get(question.get("question_type")) or QUESTION_TYPE_PROFILES["归纳概括"]
    content_display_max = round(profile["content"] * question_display_max_score(question) / 100, 1)
    single_reference_policy = (
        "本题只有一份粉笔参考答案。该答案是内容采分点的唯一边界：只能切分其中已经写出的语义，"
        "材料只用于排除明显无依据内容，绝对不得从材料新增采分点，也不得给参考答案小点追加地点、案例、政策名称或技术名称。"
        if len(reference_context) == 1 and question.get("question_type") != "综合写作"
        else "多份参考答案仅作候选解释，题干与材料用于核验评分边界。"
    )
    return f"""你正在为一道申论题建立可缓存、可审计的评分基准。只建立本题评分基准，不批改用户答案。

{single_reference_policy}

题目ID：{question.get("id")}
题型：{question.get("question_type")}
题干：{question.get("prompt")}
要求：{question.get("requirements")}
字数：{effective_word_limit}
结构化字数预算：{json.dumps(word_budget, ensure_ascii=False)}
本题占格要求：{budget_guidance}

{ANSWER_GRID_RULES}

本题材料：
{_material_text(materials)}

本题已有的参考答案全文（共 {len(reference_context)} 份；只有一份时，它就是内容采分点的唯一切分来源，不得自行扩写）：
{json.dumps(reference_context, ensure_ascii=False)}
{limited_reference_guidance(len(reference_context))}

本地对全部机构答案分句、向量聚类后的候选共识：
{json.dumps(consensus, ensure_ascii=False)}

请输出严格 JSON，并放在 <rubric_json> 与 </rubric_json> 之间：
{{
  "question_id": {int(question.get("id") or 0)},
  "task_constraints": {{"object": "", "required_structure": [], "format_rules": []}},
  "equal_weight_reason": "仅当三个以上计分点确实应等权时填写具体理由，否则留空",
  "points": [
    {{
      "group_key": "group-1",
      "group_label": "参考答案中的一级要点，如科学决策选准赛道",
      "group_order": 1,
      "point_order": 1,
      "label": "完整、可快速识别的给分点名称",
      "canonical_expression": "规范表达",
      "core_mechanism": "该小点的核心动作机制与动宾要义（考生表述机制一致即可得分，不拘泥于特定字面）",
      "aliases": ["同义表达"],
      "tier": "core|material_core|supporting|disputed",
      "importance": "critical|major|supporting",
      "suggested_weight": 0.0,
      "weight_reason": "该给分点为何值1—3分；所有内容点建议分合计应接近本题内容分{content_display_max}分",
      "coverage_role": "required|alternative|bonus",
      "is_structural": false,
      "alternative_group": "同组替代论据标识；无则为空",
      "required_for_full_score": true,
      "required_elements": ["该点不可缺少的语义成分"],
      "optional_details": ["受字数限制可省略的例子、修饰或效果"],
      "minimum_expression": "在答题纸上表达该点的最短完整写法",
      "reference_quote": "该小点在粉笔参考答案中的最小连续原文；材料新增点可留空",
      "material_evidence": [{{"material_number": 1, "quote": "必须逐字来自上面的本题材料"}}],
      "reference_ids": [1],
      "confidence": 0.0
    }}
  ],
  "conflicts": []
}}

规则：
1. 只使用本题参考答案中存在的 reference_id。只有一份参考答案时，每一个内容计分点都必须来自该答案的 reference_quote；材料提到但参考答案没写的内容不得设为计分点。
2. material_evidence.quote 只用于核验粉笔给分点是否明显有材料依据。只有一份参考答案时，材料不能扩写该点，也不能提供新的必写细节。
3. 多份参考答案时，core 至少由两个不同机构支持且达到机构总数的一半；只有一份时，以该答案的完整语义结构合理划点。
4. 只有一份参考答案时，禁止创建 material_core 内容分，禁止依据材料自主补题或扩写标答。材料只用于发现并剔除参考答案中明显无依据的内容；材料新增信息最多写入非计分说明。
5. 评分基准必须能在题目字数预算内完成。先提炼“为完成题目任务不可缺少的语义”，再把例子、修饰、展开说明和非任务要求的泛化成效放入 optional_details；不得要求考生机械写全所有机构答案细节。
6. required_for_full_score 只用于在建议字数内仍应覆盖的 core/material_core。supporting、disputed 以及仅属补充说明的内容必须为 false，遗漏时不扣主要分。
7. 评分基准遵循【真实阅卷老师合理划点原则】：非综合写作通常划分为5—10个完整给分点，每点一般值1—3分。一个给分点应是一项完整措施、原因、问题或成效，可以合理归并紧密关联的动作；禁止把每个词、地点、案例拆成0.5分碎片，也禁止把整组措施打包成一个笼统大点。
8. 按粉笔答案原有顺序填写 group_key、group_label、group_order 和 point_order。一级标题用于组织展示，不必机械单独给内容分；其下必须有对应的完整给分点。评分表不得按权重重新排序。
9. disputed 不计分。20分非作文题通常5—10个给分点，10分题通常3—6个，允许根据答案结构小幅浮动。任一给分点不得占内容分的35%以上；标题、背景、结尾不能替代主体内容。suggested_weight 使用0.5分刻度表达相对分值，不得给每个词机械分配0.5分。
10. 只要上面机构参考答案数量大于 0，就不得声称“无参考答案”或“未提供参考答案”；本地候选聚类只是辅助信息，不得替代对答案全文的核对。
11. 综合写作不得把每一则具体材料案例都设为必答点。中心立意可以是 required；不同材料案例应作为同一 alternative_group 下的可替代论据；一般升华、科技手段等只能是 bonus。
12. 每个计分点必须填写 suggested_weight 和 weight_reason；不要机械等权。若三个以上计分点确实等权，必须在顶层 equal_weight_reason 说明它们为何对完成题目任务同等重要。
13. 不输出 Markdown、解释或用户答案。
"""


def extract_tagged_json(text, tag):
    match = re.search(rf"<{re.escape(tag)}>\s*(.*?)\s*</{re.escape(tag)}>", str(text or ""), flags=re.I | re.S)
    candidate = match.group(1) if match else str(text or "").strip()
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"\s*```$", "", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            return json.loads(candidate[start : end + 1])
        raise


def normalize_essay_coverage_roles(rubric):
    """Keep essay thesis mandatory while treating material lines as alternatives."""
    if not isinstance(rubric, dict) or rubric.get("question_type") != "综合写作":
        return rubric
    points = [point for point in rubric.get("points", []) if isinstance(point, dict)]
    scoreable = [
        point for point in points
        if float(point.get("suggested_weight", point.get("weight")) or 0) > 0
        and point.get("coverage_role") != "bonus"
        and point.get("tier") != "disputed"
    ]
    thesis_pattern = re.compile(r"中心立意|中心论点|总论点|核心观点|文章主旨|主题")
    thesis = next(
        (
            point for point in scoreable
            if thesis_pattern.search(
                f"{point.get('label') or ''} {point.get('canonical_expression') or ''}"
            )
        ),
        scoreable[0] if scoreable else None,
    )
    for point in scoreable:
        if point is thesis:
            point["coverage_role"] = "required"
            point["required_for_full_score"] = True
            point["score_role"] = "required"
            point["alternative_group"] = ""
        else:
            point["coverage_role"] = "alternative"
            point["required_for_full_score"] = False
            point["score_role"] = "alternative"
            point["alternative_group"] = point.get("alternative_group") or "essay-evidence"
    return rubric


def _quote_in_materials(quote, materials, label=""):
    quote_raw = str(quote or "").strip()
    if not quote_raw:
        return False, ""
    quote_compact = re.sub(r"\s+", "", quote_raw)
    for material in materials:
        content = str(material.get("content") or "")
        content_compact = re.sub(r"\s+", "", content)
        if quote_compact and quote_compact in content_compact:
            return True, quote_raw

    quote_chars = re.sub(r"[^\w\u4e00-\u9fa5]", "", quote_raw)
    if len(quote_chars) >= 4:
        for material in materials:
            content = str(material.get("content") or "")
            content_chars = re.sub(r"[^\w\u4e00-\u9fa5]", "", content)
            if quote_chars in content_chars:
                return True, quote_raw

    for material in materials:
        content = str(material.get("content") or "")
        matched = _extract_matching_quote(quote_raw, label, content)
        if matched:
            return True, matched

    return False, ""


def _stable_point_key(point):
    evidence = point.get("material_evidence") or []
    payload = {
        "expression": _clean(point.get("canonical_expression") or point.get("label")),
        "quotes": sorted(_clean(item.get("quote")) for item in evidence if item.get("quote")),
    }
    return "point-" + _hash(payload)[:16]


def _character_bigrams(value):
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(value or "").lower())
    return {text[index : index + 2] for index in range(max(0, len(text) - 1))}


def _infer_reference_support_ids(candidate, evidence, references_by_id):
    cues = [
        candidate.get("label"),
        candidate.get("canonical_expression"),
        *(candidate.get("aliases") or []),
        *(item.get("quote") for item in evidence),
    ]
    cue_sets = [_character_bigrams(value) for value in cues if len(_clean(value)) >= 4]
    if not cue_sets:
        return []
    supported = []
    for reference_id, reference in references_by_id.items():
        reference_text = " ".join(str(reference.get(key) or "") for key in ("answer_text", "scoring_points", "notes"))
        reference_bigrams = _character_bigrams(reference_text)
        if not reference_bigrams:
            continue
        best = max(
            (len(cue_set & reference_bigrams) / len(cue_set) for cue_set in cue_sets if cue_set),
            default=0,
        )
        if best >= 0.48:
            supported.append(reference_id)
    return sorted(supported)


def _default_criteria(question_type):
    profile = QUESTION_TYPE_PROFILES.get(question_type) or QUESTION_TYPE_PROFILES["归纳概括"]
    return [
        {
            "criterion_id": f"{dimension}-1",
            "dimension": dimension,
            "description": CRITERION_LABELS[dimension],
            "weight": weight,
        }
        for dimension, weight in profile.items()
    ]


def _is_generic_optional_effect(candidate, question):
    point_text = " ".join(str(candidate.get(key) or "") for key in ("label", "canonical_expression"))
    task_text = f"{question.get('prompt') or ''} {question.get('requirements') or ''}"
    effect_terms = ("整体成效", "总体成效", "综合成效", "示范意义", "总体意义", "整体作用")
    task_asks_effect = bool(re.search(r"(意义|作用|成效|效果|影响)", task_text))
    return any(term in point_text for term in effect_terms) and not task_asks_effect


def _positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _point_importance(candidate, tier):
    value = str(candidate.get("importance") or "").strip().lower()
    aliases = {
        "critical": "critical",
        "core": "critical",
        "核心": "critical",
        "major": "major",
        "important": "major",
        "重要": "major",
        "supporting": "supporting",
        "supplementary": "supporting",
        "补充": "supporting",
    }
    if value in aliases:
        return aliases[value]
    if tier in {"core", "material_core"}:
        return "critical"
    return "supporting"


def _weight_reason(candidate, importance):
    return _clean(
        candidate.get("weight_reason")
        or candidate.get("importance_reason")
        or {
            "critical": "完成题目任务不可缺少的核心信息。",
            "major": "影响答案完整性和质量的重要信息。",
            "supporting": "用于完善答案但影响相对较小的补充信息。",
        }[importance],
        220,
    )



def _validated_reference_quote(candidate, reference_ids, references_by_id):
    """Bind one rubric point to the exact Fenbi/reference clause it colors."""
    references = [references_by_id[value] for value in reference_ids if value in references_by_id]
    if not references:
        references = list(references_by_id.values())
    bodies = [normalize_reference_answer_text(reference.get("answer_text")) for reference in references]

    raw_candidates = []
    direct = candidate.get("reference_quote")
    if direct:
        raw_candidates.append(str(direct).strip())
    raw_candidates.extend(
        str(value).strip()
        for value in (candidate.get("reference_quotes") or [])
        if str(value or "").strip()
    )
    raw_candidates.extend(
        str(value or "").strip()
        for value in (
            candidate.get("minimum_expression"),
            candidate.get("canonical_expression"),
            *(candidate.get("required_elements") or []),
        )
        if str(value or "").strip()
    )
    for quote in raw_candidates:
        normalized_quote = normalize_reference_answer_text(quote)
        if normalized_quote and any(normalized_quote in body for body in bodies):
            return _clean(normalized_quote, 220)
    return ""

def validate_rubric(raw, question, materials, references, question_feedback=None):
    if not isinstance(raw, dict) or not isinstance(raw.get("points"), list):
        raise ValueError("评分基准缺少 points 数组")
    references = dedupe_references(references)
    references_by_id = {int(reference["id"]): reference for reference in references}
    organization_count = len(references)
    single_reference_scoring = organization_count == 1 and question.get("question_type") != "综合写作"
    core_threshold = max(2, (organization_count + 1) // 2)
    invalid_keys = {
        row.get("point_key")
        for row in (question_feedback or [])
        if row.get("scope") == "question" and row.get("corrected_status") == "invalid"
    }
    points = []
    for candidate in raw.get("points")[:30]:
        if not isinstance(candidate, dict):
            continue
        label_expr = candidate.get("canonical_expression") or candidate.get("label") or ""
        evidence = []
        for item in candidate.get("material_evidence") or []:
            if isinstance(item, dict):
                is_valid, verified_quote = _quote_in_materials(item.get("quote"), materials, label_expr)
                if is_valid:
                    evidence.append(
                        {
                            "material_number": item.get("material_number"),
                            "quote": _clean(verified_quote, 160),
                        }
                    )

        if not evidence and label_expr:
            for material in materials:
                matched = _extract_matching_quote(label_expr, "", str(material.get("content") or ""))
                if matched:
                    evidence.append(
                        {
                            "material_number": material.get("material_number"),
                            "quote": _clean(matched, 160),
                        }
                    )
                    break

        reference_ids = sorted(
            {
                int(value)
                for value in (candidate.get("reference_ids") or [])
                if str(value).isdigit() and int(value) in references_by_id
            }
        )
        if not reference_ids and references_by_id:
            reference_ids = _infer_reference_support_ids(candidate, evidence, references_by_id)
        organizations = {_canonical_organization(references_by_id[value]) for value in reference_ids}
        tier = (
            candidate.get("tier")
            if candidate.get("tier") in {"core", "material_core", "supporting", "disputed"}
            else "supporting"
        )
        if (materials and not evidence) or (not evidence and not reference_ids):
            tier = "disputed"
        if tier == "core" and len(organizations) < core_threshold and organization_count > 1:
            tier = "supporting"
        if tier == "material_core" and organization_count > 1 and len(organizations) >= core_threshold:
            tier = "core"
        required_for_full_score = (
            tier in {"core", "material_core"}
            and candidate.get("required_for_full_score") is not False
            and not _is_generic_optional_effect(candidate, question)
        )
        importance = _point_importance(candidate, tier)
        suggested_weight = _positive_number(candidate.get("suggested_weight", candidate.get("weight")))
        coverage_role = "required" if required_for_full_score else "bonus"
        alternative_group = _clean(candidate.get("alternative_group"), 80)
        if question.get("question_type") == "综合写作" and tier == "material_core":
            coverage_role = "alternative"
            alternative_group = alternative_group or "essay-evidence"
        if tier == "disputed" or coverage_role == "bonus" or (
            not required_for_full_score and _is_generic_optional_effect(candidate, question)
        ):
            suggested_weight = 0
        elif not suggested_weight:
            suggested_weight = {
                "critical": 4.0,
                "major": 2.0,
                "supporting": 1.0,
            }[importance]
        point = {
            "group_key": _clean(candidate.get("group_key"), 80),
            "group_label": _clean(candidate.get("group_label"), 80),
            "group_order": int(candidate.get("group_order") or 0),
            "point_order": int(candidate.get("point_order") or 0),
            "label": _clean(candidate.get("label") or candidate.get("canonical_expression"), 60),
            "canonical_expression": _clean(candidate.get("canonical_expression") or candidate.get("label"), 180),
            "aliases": [_clean(value, 80) for value in (candidate.get("aliases") or []) if _clean(value)][:8],
            "tier": tier,
            "material_evidence": evidence[:3],
            "reference_ids": reference_ids,
            "support_org_count": len(organizations),
            "confidence": max(0.0, min(1.0, float(candidate.get("confidence") or 0.5))),
            "required_for_full_score": required_for_full_score,
            "required_elements": [
                _clean(value, 80) for value in (candidate.get("required_elements") or []) if _clean(value)
            ][:6],
            "optional_details": [
                _clean(value, 100) for value in (candidate.get("optional_details") or []) if _clean(value)
            ][:6],
            "minimum_expression": _clean(
                candidate.get("minimum_expression") or candidate.get("canonical_expression") or candidate.get("label"),
                120,
            ),
            "reference_quote": _validated_reference_quote(candidate, reference_ids, references_by_id),
            "is_structural": bool(candidate.get("is_structural")),
            "importance": importance,
            "weight_reason": _weight_reason(candidate, importance),
            "score_role": "required"
            if required_for_full_score
            else ("disputed" if tier == "disputed" else "supplementary"),
            "coverage_role": coverage_role,
            "alternative_group": alternative_group,
            "suggested_weight": suggested_weight,
        }
        if single_reference_scoring:
            # The sole Fenbi answer defines the complete scoring boundary.  Do
            # not let material-derived examples silently become requirements.
            if not point["reference_quote"]:
                point["tier"] = "disputed"
                point["required_for_full_score"] = False
                point["score_role"] = "disputed"
                point["coverage_role"] = "bonus"
                point["suggested_weight"] = 0
            else:
                point["canonical_expression"] = point["reference_quote"]
                point["minimum_expression"] = point["reference_quote"]
                point["required_elements"] = []
                point["optional_details"] = []
                if not point["is_structural"]:
                    point["tier"] = "core"
                    point["required_for_full_score"] = True
                    point["score_role"] = "required"
                    point["coverage_role"] = "required"
                    if not point["suggested_weight"]:
                        point["suggested_weight"] = 1.0
        if not point["label"] or not point["canonical_expression"]:
            continue
        supplied_key = _clean(candidate.get("point_key"), 80)
        point["point_key"] = (
            supplied_key
            if supplied_key and re.fullmatch(r"[A-Za-z0-9_.:-]+", supplied_key)
            else _stable_point_key(point)
        )
        if supplied_key and supplied_key != point["point_key"]:
            point["source_point_key"] = supplied_key
        if point["point_key"] not in invalid_keys:
            points.append(point)
    if question.get("question_type") == "综合写作":
        normalize_essay_coverage_roles({"question_type": "综合写作", "points": points})
    allowed_roles = (
        {"required", "alternative"}
        if question.get("question_type") == "综合写作"
        else {"required"}
    )
    scoreable = [
        point
        for point in points
        if point["suggested_weight"] > 0 and point.get("coverage_role") in allowed_roles
    ]
    if not scoreable:
        raise ValueError("评分基准没有通过材料校验的有效采分点")
    if question.get("question_type") != "综合写作":
        longest_reference = max(
            (len(re.sub(r"\s+", "", normalize_reference_answer_text(ref.get("answer_text")))) for ref in references),
            default=0,
        )
        display_max = question_display_max_score(question)
        if longest_reference >= 100:
            minimum_points = 5 if display_max >= 15 else 3
            maximum_points = 12 if display_max >= 15 else 8
            if not (minimum_points <= len(scoreable) <= maximum_points):
                raise ValueError(
                    f"非作文题共有 {len(scoreable)} 个计分点，应合理归并为 {minimum_points}—{maximum_points} 个完整给分点"
                )
        if longest_reference >= 60:
            raw_total = sum(point["suggested_weight"] for point in scoreable) or 1
            largest_share = max(point["suggested_weight"] for point in scoreable) / raw_total
            if largest_share > 0.35:
                raise ValueError("存在权重超过内容分35%的笼统大点，必须拆成更合理的完整给分点")
            reference_bound = [point for point in scoreable if point.get("reference_quote")]
            if references and len(reference_bound) < min(4, len(scoreable)):
                raise ValueError("粉笔参考答案与给分点绑定不足，必须为主体给分点提供逐字 reference_quote")
            if single_reference_scoring:
                bound_quotes = [point["reference_quote"] for point in reference_bound]
                if len(bound_quotes) != len(set(bound_quotes)):
                    raise ValueError("多个给分点绑定了同一段粉笔原文，必须划分为互不重复的评分依据")
                grouped = {point.get("group_key") for point in scoreable if point.get("group_key")}
                numbered_sections = re.findall(r"(?:^|[。；])\s*[一二三四五六七八九十]+、", normalize_reference_answer_text(references[0].get("answer_text")))
                if numbered_sections and len(grouped) < min(len(numbered_sections), 3):
                    raise ValueError("评分基准没有保留粉笔答案的主要要点组结构")
    if (
        len(scoreable) >= 3
        and len({round(point["suggested_weight"], 4) for point in scoreable}) == 1
        and not _clean(raw.get("equal_weight_reason"), 240)
    ):
        raise ValueError("三个及以上采分点被平均分配，但未说明等权理由")
    profile = QUESTION_TYPE_PROFILES.get(question.get("question_type")) or QUESTION_TYPE_PROFILES["归纳概括"]
    base_total = sum(point["suggested_weight"] for point in scoreable) or 1
    display_scale = question_display_max_score(question) / 100
    if question.get("question_type") != "综合写作" and display_scale > 0:
        total_half_units = max(len(scoreable), round(profile["content"] * display_scale * 2))
        exact_units = [total_half_units * point["suggested_weight"] / base_total for point in scoreable]
        units = [max(1, int(value)) for value in exact_units]
        while sum(units) < total_half_units:
            index = max(range(len(units)), key=lambda i: exact_units[i] - units[i])
            units[index] += 1
        while sum(units) > total_half_units:
            candidates = [i for i, value in enumerate(units) if value > 1]
            if not candidates:
                break
            index = max(candidates, key=lambda i: units[i] - exact_units[i])
            units[index] -= 1
        for point in points:
            point["weight"] = 0
            point["display_weight"] = 0
        for point, half_units in zip(scoreable, units):
            point["display_weight"] = half_units / 2
            point["weight"] = round(point["display_weight"] / display_scale, 3)
    else:
        for point in points:
            point["weight"] = (
                round(profile["content"] * point["suggested_weight"] / base_total, 3)
                if point["suggested_weight"] else 0
            )
            point["display_weight"] = round(float(point.get("weight") or 0) * display_scale, 1)
    weighted_total = round(sum(point["weight"] for point in points), 3)
    if scoreable and abs(weighted_total - profile["content"]) > 0.001:
        scoreable[-1]["weight"] = round(
            scoreable[-1]["weight"] + profile["content"] - weighted_total,
            3,
        )
        scoreable[-1]["display_weight"] = round(scoreable[-1]["weight"] * display_scale, 1)
    mapped_reference_ids = sorted({value for point in points for value in point.get("reference_ids", [])})
    return {
        "schema_version": RUBRIC_VERSION,
        "question_id": int(question.get("id") or 0),
        "max_score": 100,
        "display_max_score": question_display_max_score(question),
        "score_is_estimated": question_score_is_estimated(question),
        "question_type": question.get("question_type") or "",
        "word_limit": question_word_limit_text(question),
        "scoring_mode": (
            "holistic_essay"
            if question.get("question_type") == "综合写作"
            else "point_based"
        ),
        "selected_reference_count": len(references),
        "selected_references": [
            {
                "reference_id": int(reference["id"]),
                "id": int(reference["id"]),
                "organization": _canonical_organization(reference),
                "answer_text": str(reference.get("answer_text") or "").strip(),
            }
            for reference in references
        ],
        "mapped_reference_ids": mapped_reference_ids,
        "reference_mapping_status": "mapped"
        if mapped_reference_ids
        else ("selected_unmapped" if references else "none"),
        "task_constraints": raw.get("task_constraints") if isinstance(raw.get("task_constraints"), dict) else {},
        "word_budget": word_limit_budget(question.get("word_limit") or ""),
        "points": points,
        "equal_weight_reason": _clean(raw.get("equal_weight_reason"), 240),
        "dimensions": _default_criteria(question.get("question_type")),
        "criteria": _default_criteria(question.get("question_type")),
        "conflicts": [str(value)[:240] for value in (raw.get("conflicts") or [])[:8]],
    }

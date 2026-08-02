import json
import re

from ..agent_modules import FEATURE_HASH_MODEL, _cosine, _embed_text
from ..grading import limited_reference_guidance, word_limit_budget
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
    word_budget = word_limit_budget(question.get("word_limit") or "")
    budget_guidance = _word_budget_guidance(word_budget)
    return f"""你正在为一道申论题建立可缓存、可审计的评分基准。只建立本题评分基准，不批改用户答案。

最高事实来源是本题题干与材料。机构答案只是候选解释。没有本题材料依据的内容不得成为主要扣分点。

题目ID：{question.get("id")}
题型：{question.get("question_type")}
题干：{question.get("prompt")}
要求：{question.get("requirements")}
字数：{question.get("word_limit")}
结构化字数预算：{json.dumps(word_budget, ensure_ascii=False)}
本题占格要求：{budget_guidance}

{ANSWER_GRID_RULES}

本题材料：
{_material_text(materials)}

本题已选择的机构参考答案全文（共 {len(reference_context)} 份，属于本题评分主证据；answer_text、scoring_points、notes 均来自旧批改模式原始上下文）：
{json.dumps(reference_context, ensure_ascii=False)}
{limited_reference_guidance(len(reference_context))}

本地对全部机构答案分句、向量聚类后的候选共识：
{json.dumps(consensus, ensure_ascii=False)}

请输出严格 JSON，并放在 <rubric_json> 与 </rubric_json> 之间：
{{
  "question_id": {int(question.get("id") or 0)},
  "task_constraints": {{"object": "", "required_structure": [], "format_rules": []}},
  "points": [
    {{
      "label": "简短采分点名",
      "canonical_expression": "规范表达",
      "aliases": ["同义表达"],
      "tier": "core|material_core|supporting|disputed",
      "required_for_full_score": true,
      "required_elements": ["该点不可缺少的语义成分"],
      "optional_details": ["受字数限制可省略的例子、修饰或效果"],
      "minimum_expression": "在答题纸上表达该点的最短完整写法",
      "material_evidence": [{{"material_number": 1, "quote": "必须逐字来自上面的本题材料"}}],
      "reference_ids": [1],
      "confidence": 0.0
    }}
  ],
  "conflicts": []
}}

规则：
1. 只使用本题机构参考答案中存在的 reference_id。逐份检查全文并把支持当前采分点的 ID 写入 reference_ids；不得仅因本地聚类未聚合就把所有 reference_ids 留空。
2. material_evidence.quote 必须是本题材料中的连续原文短句。
3. core 至少由两个不同机构支持，且支持机构数达到机构总数的一半；否则降为 supporting。
4. 只有一份机构答案时，可把材料直接确认的点标为 material_core，不得声称形成机构共识。
5. 评分基准必须能在题目字数预算内完成。先提炼“为完成题目任务不可缺少的语义”，再把例子、修饰、展开说明和非任务要求的泛化成效放入 optional_details；不得要求考生机械写全所有机构答案细节。
6. required_for_full_score 只用于在建议字数内仍应覆盖的 core/material_core。supporting、disputed 以及仅属补充说明的内容必须为 false，遗漏时不扣主要分。
7. 合并同一措施或同一机制下的相近细节。所有 required 点的 minimum_expression 加上必要序号和标点，按上述占格规则估算后必须能放入 suggested_max，并保留至少8格安全余量。
8. 题干未明确要求“意义、作用、成效、影响”时，不得把泛化的“整体成效/示范意义”单列为必答扣分点；它只能是可选补充。
9. disputed 不计分。控制在4—12个有效采分点，避免把一条答案拆成大量细碎扣分项。
10. 只要上面机构参考答案数量大于 0，就不得声称“无参考答案”或“未提供参考答案”；本地候选聚类只是辅助信息，不得替代对答案全文的核对。
11. 不输出 Markdown、解释或用户答案。
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


def validate_rubric(raw, question, materials, references, question_feedback=None):
    if not isinstance(raw, dict) or not isinstance(raw.get("points"), list):
        raise ValueError("评分基准缺少 points 数组")
    references = dedupe_references(references)
    references_by_id = {int(reference["id"]): reference for reference in references}
    organization_count = len(references)
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
        if tier == "disputed" or (not required_for_full_score and _is_generic_optional_effect(candidate, question)):
            suggested_weight = 0
        elif not suggested_weight:
            suggested_weight = {
                "critical": 4.0,
                "major": 2.0,
                "supporting": 1.0,
            }[importance]
        point = {
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
            "importance": importance,
            "weight_reason": _weight_reason(candidate, importance),
            "score_role": "required"
            if required_for_full_score
            else ("disputed" if tier == "disputed" else "supplementary"),
            "suggested_weight": suggested_weight,
        }
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
    scoreable = [point for point in points if point["suggested_weight"] > 0]
    if not scoreable:
        raise ValueError("评分基准没有通过材料校验的有效采分点")
    if (
        len(scoreable) >= 3
        and len({round(point["suggested_weight"], 4) for point in scoreable}) == 1
        and not _clean(raw.get("equal_weight_reason"), 240)
    ):
        raise ValueError("三个及以上采分点被平均分配，但未说明等权理由")
    profile = QUESTION_TYPE_PROFILES.get(question.get("question_type")) or QUESTION_TYPE_PROFILES["归纳概括"]
    base_total = sum(point["suggested_weight"] for point in scoreable) or 1
    for point in points:
        point["weight"] = (
            round(profile["content"] * point["suggested_weight"] / base_total, 3) if point["suggested_weight"] else 0
        )
    weighted_total = round(sum(point["weight"] for point in points), 3)
    if scoreable and abs(weighted_total - profile["content"]) > 0.001:
        scoreable[-1]["weight"] = round(
            scoreable[-1]["weight"] + profile["content"] - weighted_total,
            3,
        )
    mapped_reference_ids = sorted({value for point in points for value in point.get("reference_ids", [])})
    return {
        "schema_version": RUBRIC_VERSION,
        "question_id": int(question.get("id") or 0),
        "max_score": 100,
        "display_max_score": question_display_max_score(question),
        "score_is_estimated": question_score_is_estimated(question),
        "question_type": question.get("question_type") or "",
        "selected_reference_count": len(references),
        "selected_references": [
            {
                "reference_id": int(reference["id"]),
                "organization": _canonical_organization(reference),
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

from .common import (
    _NO_REFERENCE_CLAIMS,
    CRITERION_LABELS,
    QUESTION_TYPE_PROFILES,
    RESULT_VERSION,
    _clean,
    _round_half,
)
from .contracts import GradingResult
from .rubric import _default_criteria


def _coverage_factor(candidate, status):
    if status == "hit":
        return 1.0
    if status == "miss":
        return 0.0
    raw = candidate.get("coverage_ratio", candidate.get("coverage", 0.5))
    try:
        if isinstance(raw, str) and raw.strip().endswith("%"):
            value = float(raw.strip()[:-1]) / 100
        else:
            value = float(raw)
            if value > 1:
                value /= 100
    except (TypeError, ValueError):
        value = 0.5
    value = max(0.1, min(0.9, value))
    return round(value * 20) / 20


def _exam_score_ceiling(score):
    """Compress unusually high AI totals to a realistic closed-book exam scale."""
    score = float(score or 0)
    if score <= 70:
        return round(score, 1)
    # A raw model total above 70 still distinguishes strong answers, but the
    # top range is deliberately harder to enter: even a raw 100 maps to 88.
    return round(70 + (score - 70) * 0.6, 1)


def _consistent_point_reason(status, reason, point):
    """Keep the displayed judgement about the user's answer aligned with status."""
    reason = _clean(reason, 300)
    label = _clean(point.get("label") or point.get("canonical_expression"), 80) or "该采分点"
    positive_claims = (
        "完整覆盖",
        "充分覆盖",
        "完全覆盖",
        "已经覆盖",
        "已覆盖",
        "明确体现",
        "准确体现",
        "完整体现",
        "已命中",
        "完全命中",
        "符合采分点",
        "与采分点一致",
    )
    if status == "miss" and any(claim in reason for claim in positive_claims):
        return f"原答案未提供能够证明“{label}”的明确表述；判断仅依据本次作答原文。"
    if status == "miss" and not reason:
        return f"原答案未出现“{label}”对应的明确表述。"
    return reason


def _validated_reference_fusion(value, rubric):
    text = _clean(value, 600)
    references = rubric.get("selected_references") or []
    count = int(rubric.get("selected_reference_count") or len(references))
    if count <= 0:
        return text
    organizations = [str(item.get("organization") or "").strip() for item in references if item.get("organization")]
    organization_text = "、".join(organizations)
    if count == 1:
        source = f"（{organization_text}）" if organization_text else ""
        return (
            f"本题仅有 1 份机构参考答案{source}，用于辅助核对采分点；"
            "评分同时依据题干任务与材料原文，不把单份答案视为唯一标准。"
        )
    prefix = f"本题已纳入 {count} 份机构参考答案"
    if organization_text:
        prefix += f"（{organization_text}）"
    prefix += "进行融合。"
    if any(claim in text for claim in _NO_REFERENCE_CLAIMS):
        text = "系统已结合机构答案全文与本题材料核验共性核心点和差异补充点。"
    if text.startswith(("本题已纳入", "本题纳入")):
        _, separator, remainder = text.partition("。")
        text = remainder.strip() if separator else ""
    return prefix + (text or "系统已结合机构答案全文与本题材料核验采分点。")


def validate_grading_result(
    raw,
    rubric,
    answer_text,
    evidence,
    report_feedback=None,
) -> GradingResult:
    """Validate model evidence while preserving its holistic dimension scores."""
    if isinstance(raw, dict) and isinstance(raw.get("evaluation"), dict):
        raw = raw["evaluation"]
    if not isinstance(raw, dict):
        raise ValueError("批改结果不是 JSON 对象")

    points = sorted(
        [point for point in rubric.get("points", []) if float(point.get("weight") or 0) > 0],
        key=lambda point: (-float(point.get("weight") or 0), point.get("point_key") or ""),
    )
    point_by_key = {point["point_key"]: point for point in points}
    candidates = {
        str(item.get("point_key") or ""): item
        for item in (raw.get("point_matches") or [])
        if isinstance(item, dict) and item.get("point_key")
    }
    matches = []
    for point in points:
        candidate = candidates.get(point["point_key"])
        if candidate is None and point.get("source_point_key"):
            candidate = candidates.get(point["source_point_key"])
        candidate = candidate or {}
        status = candidate.get("status")
        if status not in {"hit", "partial", "miss"}:
            status = "miss"
        quote = _clean(candidate.get("answer_quote"), 180)
        if status in {"hit", "partial"} and (not quote or quote not in answer_text):
            status = "miss"
            quote = ""
        coverage = _coverage_factor(candidate, status)
        matches.append(
            {
                "point_key": point["point_key"],
                "status": status,
                "coverage_ratio": coverage,
                "answer_quote": quote,
                "reason": _consistent_point_reason(status, candidate.get("reason"), point),
                "weight": round(float(point.get("weight") or 0), 3),
                "importance": point.get("importance") or "supporting",
            }
        )

    content_weight = float(
        (QUESTION_TYPE_PROFILES.get(rubric.get("question_type")) or QUESTION_TYPE_PROFILES["归纳概括"])["content"]
    )
    weighted_coverage = round(
        sum(match["weight"] * match["coverage_ratio"] for match in matches),
        1,
    )
    dimensions = rubric.get("dimensions") or rubric.get("criteria") or _default_criteria(rubric.get("question_type"))
    dimension_by_name = {
        item["dimension"]: {
            "dimension": item["dimension"],
            "label": CRITERION_LABELS.get(item["dimension"], item["dimension"]),
            "max_score": float(item.get("weight") or 0),
        }
        for item in dimensions
        if isinstance(item, dict) and item.get("dimension")
    }
    if not dimension_by_name or round(sum(item["max_score"] for item in dimension_by_name.values()), 4) != 100:
        raise ValueError("评分维度满分未闭合到100分")

    raw_dimensions = {
        str(item.get("dimension") or ""): item
        for item in (raw.get("dimension_scores") or [])
        if isinstance(item, dict) and item.get("dimension")
    }
    normalized_dimensions = []
    blank_answer = not str(answer_text or "").strip()
    for dimension, definition in dimension_by_name.items():
        candidate = raw_dimensions.get(dimension)
        if candidate is None and not blank_answer:
            raise ValueError(f"批改结果缺少 {dimension} 维度得分")
        try:
            score = 0.0 if blank_answer else float(candidate.get("score"))
        except (TypeError, ValueError):
            raise ValueError(f"{dimension} 维度得分不是有效数字")
        if score != score or score < 0 or score > definition["max_score"]:
            raise ValueError(f"{dimension} 维度得分超出有效范围")
        normalized_dimensions.append(
            {
                **definition,
                "score": round(score, 1),
                "reason": "空白答案。" if blank_answer else _clean(candidate.get("reason"), 300),
            }
        )

    content_score = next(
        (item["score"] for item in normalized_dimensions if item["dimension"] == "content"),
        0.0,
    )
    adjustment_reason = _clean(raw.get("holistic_adjustment_reason"), 360)
    calibration_note = ""
    if not blank_answer:
        # Holistic quality may move the content score, but it must remain
        # anchored to evidence-backed point coverage. Keeping this local also
        # avoids an extra API retry solely because the model over-adjusted.
        max_adjustment = content_weight * 0.08
        lower_bound = max(0.0, weighted_coverage - max_adjustment)
        upper_bound = min(content_weight, weighted_coverage + max_adjustment)
        calibrated_content_score = round(
            min(max(content_score, lower_bound), upper_bound),
            1,
        )
        if calibrated_content_score != content_score:
            calibration_note = f"综合内容分已按可核验采分点覆盖校准：{content_score:g}→{calibrated_content_score:g}。"
            content_score = calibrated_content_score
            for dimension in normalized_dimensions:
                if dimension["dimension"] != "content":
                    continue
                dimension["score"] = content_score
                dimension["reason"] = " ".join(value for value in (dimension.get("reason"), calibration_note) if value)
                break
            adjustment_reason = " ".join(value for value in (adjustment_reason, calibration_note) if value)

    valid_evidence_ids = {card.get("evidence_id") for card in evidence if card.get("role") == "personalization"}
    history_stable = (
        len(
            {
                card.get("attempt_id")
                for card in evidence
                if card.get("role") == "personalization" and card.get("attempt_id")
            }
        )
        >= 2
    )
    personalized = []
    for finding in raw.get("personalized_findings") or []:
        if not isinstance(finding, dict):
            continue
        evidence_ids = [value for value in (finding.get("evidence_ids") or []) if value in valid_evidence_ids]
        if not evidence_ids:
            continue
        personalized.append(
            {
                "finding": _clean(finding.get("finding"), 220),
                "root_cause": _clean(finding.get("root_cause"), 220),
                "next_step": _clean(finding.get("next_step"), 220),
                "evidence_ids": evidence_ids,
                "confidence": ("recurring" if finding.get("confidence") == "recurring" and history_stable else "stage"),
            }
        )

    annotation_severities = {
        "good": "positive",
        "polish": "low",
        "change": "medium",
        "delete": "high",
        "add": "high",
        "critical": "critical",
    }
    valid_severities = {"positive", "low", "medium", "high", "critical"}
    annotations = []
    for item in raw.get("annotations") or []:
        if not isinstance(item, dict):
            continue
        kind = (
            item.get("kind")
            if item.get("kind") in {"good", "polish", "change", "delete", "add", "critical"}
            else "change"
        )
        quote = _clean(item.get("quote"), 180)
        anchor = _clean(item.get("anchor"), 180)
        if kind != "add" and (not quote or quote not in answer_text):
            continue
        if kind == "add" and (not anchor or anchor not in answer_text):
            continue
        severity = item.get("severity")
        if severity not in valid_severities:
            severity = annotation_severities[kind]
        annotations.append(
            {
                "kind": kind,
                "quote": quote,
                "replacement": _clean(item.get("replacement"), 200),
                "reason": _clean(item.get("reason"), 240),
                "point_key": item.get("point_key") if item.get("point_key") in point_by_key else "",
                "severity": severity,
                "anchor": anchor,
            }
        )

    raw_score = round(sum(item["score"] for item in normalized_dimensions), 1)
    score = _exam_score_ceiling(raw_score)
    if blank_answer:
        score = 0.0
    exam_calibration_note = ""
    if raw_score > 0 and score != raw_score:
        factor = score / raw_score
        for dimension in normalized_dimensions:
            dimension["score"] = round(dimension["score"] * factor, 1)
        # Rounding individual dimensions can move the displayed sum by 0.1.
        difference = round(score - sum(item["score"] for item in normalized_dimensions), 1)
        if difference:
            normalized_dimensions[0]["score"] = round(normalized_dimensions[0]["score"] + difference, 1)
        content_score = next(
            (item["score"] for item in normalized_dimensions if item["dimension"] == "content"),
            0.0,
        )
        exam_calibration_note = f"已按真实考场高分稀缺度校准总分：{raw_score:g}→{score:g}。"
    display_max_score = float(rubric.get("display_max_score") or 100)
    display_score = _round_half(score * display_max_score / 100)
    display_scale = display_max_score / 100
    for dimension in normalized_dimensions:
        dimension["display_max_score"] = round(dimension["max_score"] * display_scale, 2)
        dimension["display_score"] = round(dimension["score"] * display_scale, 2)

    return {
        "schema_version": RESULT_VERSION,
        "score_status": "valid",
        "point_matches": matches,
        "dimension_scores": normalized_dimensions,
        # Kept for readers of older result payloads.
        "criteria_matches": [
            {
                "criterion_id": f"{item['dimension']}-1",
                "dimension": item["dimension"],
                "weight": item["max_score"],
                "awarded": item["score"],
                "reason": item["reason"],
            }
            for item in normalized_dimensions
        ],
        "weighted_coverage_score": weighted_coverage,
        "holistic_adjustment_reason": adjustment_reason,
        "annotations": annotations[:12],
        "reference_fusion": _validated_reference_fusion(raw.get("reference_fusion"), rubric),
        "material_reading": [_clean(value, 360) for value in (raw.get("material_reading") or []) if _clean(value)][:12],
        "optimization_suggestions": [
            _clean(value, 300) for value in (raw.get("optimization_suggestions") or []) if _clean(value)
        ][:10],
        "personalized_findings": personalized[:6],
        "overall_summary": _clean(raw.get("overall_summary"), 360)
        or ("空白答案，未完成作答。" if blank_answer else "请结合维度得分与采分点分析查看。"),
        "revised_answer": str(raw.get("revised_answer") or "").strip(),
        "score": score,
        "display_score": display_score,
        "display_max_score": int(display_max_score) if display_max_score.is_integer() else display_max_score,
        "score_is_estimated": bool(rubric.get("score_is_estimated")),
        "content_score": round(content_score, 1),
        "validation_errors": [value for value in (calibration_note, exam_calibration_note) if value],
    }

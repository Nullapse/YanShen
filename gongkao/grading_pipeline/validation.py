import re

from .calibration import apply_score_calibration
from .common import (
    _NO_REFERENCE_CLAIMS,
    CRITERION_LABELS,
    QUESTION_TYPE_PROFILES,
    RESULT_VERSION,
    _clean,
    _round_half,
)
from .contracts import GradingResult
from .evidence_resolution import resolve_answer_evidence
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
    calibration_policy=None,
    reviewed=False,
) -> GradingResult:
    """Validate model evidence while preserving its holistic dimension scores."""
    if isinstance(raw, dict) and isinstance(raw.get("evaluation"), dict):
        raw = raw["evaluation"]
    if not isinstance(raw, dict):
        raise ValueError("批改结果不是 JSON 对象")

    points = sorted(
        [point for point in rubric.get("points", []) if isinstance(point, dict) and point.get("point_key")],
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
        quote = _clean(candidate.get("answer_quote"), 240)
        resolution = (
            resolve_answer_evidence(quote, answer_text)
            if status in {"hit", "partial"}
            else {"status": "not_required", "quote": "", "spans": []}
        )
        if resolution["status"] == "resolved":
            quote = _clean(resolution.get("quote"), 240)
        coverage = _coverage_factor(candidate, status)
        coverage_role = point.get("coverage_role") or (
            "required" if point.get("required_for_full_score", True) else "bonus"
        )
        matches.append(
            {
                "point_key": point["point_key"],
                "status": status,
                "coverage_ratio": coverage,
                "answer_quote": quote,
                "reason": _consistent_point_reason(status, candidate.get("reason"), point),
                "weight": round(float(point.get("weight") or 0), 3),
                "importance": point.get("importance") or "supporting",
                "coverage_role": coverage_role,
                "evidence_status": resolution["status"],
                "evidence_spans": resolution.get("spans") or [],
                "confidence": max(0.0, min(1.0, float(candidate.get("confidence") or 0.7))),
                "missing_elements": [
                    _clean(value, 100)
                    for value in (candidate.get("missing_elements") or [])
                    if _clean(value)
                ][:6],
            }
        )

    content_weight = float(
        (QUESTION_TYPE_PROFILES.get(rubric.get("question_type")) or QUESTION_TYPE_PROFILES["归纳概括"])["content"]
    )
    scoring_mode = rubric.get("scoring_mode") or (
        "holistic_essay" if rubric.get("question_type") == "综合写作" else "point_based"
    )
    weighted_coverage = round(
        sum(
            match["weight"] * match["coverage_ratio"]
            for match in matches
            if match.get("coverage_role") in {"required", "alternative"}
            and not (
                scoring_mode == "point_based"
                and match.get("status") in {"hit", "partial"}
                and match.get("evidence_status") == "unresolved"
            )
        ),
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
    if not blank_answer and scoring_mode == "point_based":
        content_score = min(content_weight, weighted_coverage)
        for dimension in normalized_dimensions:
            if dimension["dimension"] == "content":
                dimension["score"] = round(content_score, 1)
                dimension["reason"] = _clean(
                    dimension.get("reason") or "内容分由必答采分点覆盖确定。",
                    300,
                )
                break

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

    redundancies = []
    for item in raw.get("redundancies") or []:
        if not isinstance(item, dict):
            continue
        quote = _clean(item.get("quote"), 200)
        if not quote:
            continue
        quote_clean = re.sub(r"\s+", "", quote)
        answer_clean = re.sub(r"\s+", "", str(answer_text or ""))
        if quote_clean and quote_clean in answer_clean:
            wasted = int(item.get("wasted_chars") or len(quote))
            redundancies.append(
                {
                    "quote": quote,
                    "wasted_chars": wasted,
                    "reason": _clean(item.get("reason"), 240) or "脱离采分要义的冗余修饰",
                    "suggestion": _clean(item.get("suggestion"), 200) or "建议精简或删除",
                }
            )

    raw_score = round(sum(item["score"] for item in normalized_dimensions), 1)
    score, score_calibration = apply_score_calibration(raw_score, calibration_policy)
    if blank_answer:
        score = 0.0
        score_calibration["adjustment"] = 0.0
    if raw_score > 0 and score != raw_score:
        remaining = round(score - raw_score, 1)
        if remaining < 0:
            total = sum(item["score"] for item in normalized_dimensions) or 1
            for dimension in normalized_dimensions:
                share = remaining * dimension["score"] / total
                dimension["score"] = round(max(0.0, dimension["score"] + share), 1)
        else:
            headroom = sum(item["max_score"] - item["score"] for item in normalized_dimensions) or 1
            for dimension in normalized_dimensions:
                share = remaining * (dimension["max_score"] - dimension["score"]) / headroom
                dimension["score"] = round(min(dimension["max_score"], dimension["score"] + share), 1)
        difference = round(score - sum(item["score"] for item in normalized_dimensions), 1)
        if difference:
            for dimension in normalized_dimensions:
                candidate = round(dimension["score"] + difference, 1)
                if 0 <= candidate <= dimension["max_score"]:
                    dimension["score"] = candidate
                    break
        content_score = next(
            (item["score"] for item in normalized_dimensions if item["dimension"] == "content"),
            0.0,
        )

    review_reasons = []
    for match in matches:
        if (
            match.get("status") in {"hit", "partial"}
            and match.get("evidence_status") == "unresolved"
            and match.get("coverage_role") == "required"
        ):
            review_reasons.append(f"unresolved_required_evidence:{match['point_key']}")
    if scoring_mode == "holistic_essay" and abs(content_score - weighted_coverage) > content_weight * 0.35:
        review_reasons.append("essay_content_diagnostic_divergence")
    essay_diagnostic = " ".join(item.get("reason") or "" for item in normalized_dimensions)
    if (
        scoring_mode == "holistic_essay"
        and raw_score >= 70
        and re.search(
            r"(?:关键)?事实(?:偏差|错误)|材料误读|论证(?:空泛|薄弱|不足)|未能结合.{0,12}(?:材料|案例)|主要(?:部分|段落).{0,8}(?:缺少|不足)",
            essay_diagnostic,
        )
    ):
        review_reasons.append("essay_high_band_diagnostic_conflict")
    score_status = "provisional" if review_reasons else "valid"
    review = {
        "triggered": bool(review_reasons),
        "reasons": review_reasons,
        "decision": "confirmed" if reviewed and not review_reasons else ("required" if review_reasons else "not_needed"),
    }
    display_max_score = float(rubric.get("display_max_score") or 100)
    display_score = _round_half(score * display_max_score / 100)
    display_scale = display_max_score / 100
    for dimension in normalized_dimensions:
        dimension["display_max_score"] = round(dimension["max_score"] * display_scale, 2)
        dimension["display_score"] = round(dimension["score"] * display_scale, 2)

    return {
        "schema_version": RESULT_VERSION,
        "score_status": score_status,
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
        "redundancies": redundancies[:10],
        "reference_fusion": _validated_reference_fusion(raw.get("reference_fusion"), rubric),
        "material_reading": [_clean(value, 360) for value in (raw.get("material_reading") or []) if _clean(value)][:12],
        "optimization_suggestions": [
            _clean(value, 300) for value in (raw.get("optimization_suggestions") or []) if _clean(value)
        ][:10],
        "personalized_findings": personalized[:6],
        "overall_summary": _clean(raw.get("overall_summary"), 360)
        or ("空白答案，未完成作答。" if blank_answer else "请结合维度得分与采分点分析查看。"),
        "summary": raw.get("summary") if isinstance(raw.get("summary"), dict) else {},
        "revised_answer": str(raw.get("revised_answer") or "").strip(),
        "score": score,
        "display_score": display_score,
        "display_max_score": int(display_max_score) if display_max_score.is_integer() else display_max_score,
        "score_is_estimated": bool(rubric.get("score_is_estimated")),
        "content_score": round(content_score, 1),
        "score_calibration": score_calibration,
        "review": review,
        "validation_errors": review_reasons,
    }

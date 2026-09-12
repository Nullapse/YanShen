import re

from ..grading import normalize_reference_answer_text
from .contracts import GradingEvidence, GradingResult

CONTENT_WEIGHTS = {
    "归纳概括": 70,
    "综合分析": 55,
    "提出对策": 60,
    "公文写作": 50,
    "综合写作": 40,
}


def _format_score(value) -> str:
    return f"{round(float(value or 0), 1):g}"


def _essay_grade_label(score, result):
    """Use Yuan Dong's five essay classes instead of the generic score labels."""
    return result.get("essay_band_label") or (
        "一类文（优秀）" if score >= 80 else
        "二类文（良好）" if score >= 70 else
        "三类文（中上）" if score >= 60 else
        "四类文（一般）" if score >= 50 else
        "五类文（较弱）"
    )


def _format_point_data(point_def, match, display_scale):
    status = str(match.get("status") or "miss").lower()
    if status in ("hit", "full", "完全得分", "完全命中", "满分"):
        status_clean = "hit"
    elif status in ("partial", "part", "部分得分", "部分命中"):
        status_clean = "partial"
    else:
        status_clean = "miss"

    max_pts = float(point_def.get("display_weight") or 0)
    if not max_pts:
        max_pts = float(point_def.get("weight") or point_def.get("suggested_weight") or 2.0) * display_scale
    ratio = float(match.get("coverage_ratio") if match.get("coverage_ratio") is not None else (1.0 if status_clean == "hit" else (0.5 if status_clean == "partial" else 0.0)))
    earned = float(match.get("awarded_score") or 0) * display_scale
    if match.get("awarded_score") is None:
        earned = max_pts * ratio
    score_str = f"+{_format_score(earned)}分 / 满分{_format_score(max_pts)}分"

    user_quote = str(match.get("answer_quote") or match.get("matched_quote") or "").strip()
    reason = str(match.get("reason") or match.get("analysis") or "").strip()
    missing = match.get("missing_elements") or []

    if status_clean == "hit":
        eval_body = f"你的作答准确命中：『{user_quote}』。" if user_quote else "你的作答完整覆盖该得分点。"
        if reason:
            eval_body += f" {reason}"
        else:
            eval_body += " 核心语义完整准确，完全得分。"
    elif status_clean == "partial":
        level_text = {0.75: "大部分得分", 0.5: "一半得分", 0.25: "少量得分"}.get(ratio, "部分得分")
        miss_body = f"缺失要义：{'、'.join(str(m) for m in missing)}。" if missing else ""
        quote_body = f"你的作答{level_text}：『{user_quote}』。" if user_quote else f"你的作答{level_text}。"
        eval_body = f"{quote_body} {miss_body} {reason or '采分点表述不够完整或有遗漏，部分得分。'}".strip()
    else:
        eval_body = f"你的作答未体现该得分点。{reason or '未能从给定材料中提取出此项关键得分信息，未得分。'}".strip()

    material_evidence = point_def.get("material_evidence") or []
    ev_list = []
    if isinstance(material_evidence, list):
        for ev in material_evidence:
            if isinstance(ev, dict):
                num = ev.get("material_number")
                q = ev.get("quote") or ev.get("text") or ""
                p = f"材料{num}：" if num else ""
                if q:
                    ev_list.append(f"{p}『{q}』")
            elif isinstance(ev, str) and ev.strip():
                ev_list.append(f"『{ev.strip()}』")
    elif isinstance(material_evidence, str) and material_evidence.strip():
        ev_list.append(f"『{material_evidence.strip()}』")

    if ev_list:
        source_str = "；".join(ev_list)
    else:
        label = point_def.get("label") or point_def.get("canonical_expression") or "材料论述"
        source_str = f"根据给定材料中关于“{label}”的相关论述提取。"

    def sanitize(val):
        return str(val or "").replace("|", "／").replace("]", "）").replace("\n", " ").strip()

    pkey = str(point_def.get("point_key") or match.get("point_key") or "").strip()

    return {
        "status": status_clean,
        "score_str": sanitize(score_str),
        "my_eval": sanitize(eval_body),
        "material_source": sanitize(source_str),
        "point_key": sanitize(pkey),
    }


def _valid_reference_answers(rubric):
    references = []
    for reference in rubric.get("selected_references") or []:
        answer_text = normalize_reference_answer_text(reference.get("answer_text"))
        organization = str(
            reference.get("canonical_organization") or reference.get("organization") or ""
        ).strip()
        if not answer_text or answer_text in {"未提供文本", "无", "暂无", "（未提供文本）"}:
            continue
        references.append({**reference, "organization": organization or "参考答案", "answer_text": answer_text})
    return references


def _primary_reference_answer(rubric):
    references = _valid_reference_answers(rubric)
    if not references:
        return None
    return next(
        (reference for reference in references if "粉笔" in reference.get("organization", "")),
        references[0],
    )


def _build_master_annotated_answer(result, rubric, display_scale) -> str:
    """Color the Fenbi answer from the exact same point decisions used for scoring.

    No renderer-side semantic judgement is allowed here.  The rubric binds an
    exact reference clause to a point key; the validated match for that key is
    the single source of truth for status and awarded score.
    """
    rubric_points = [p for p in (rubric.get("points") or []) if p.get("point_key")]
    point_matches = result.get("point_matches") or result.get("points") or []
    match_by_key = {m.get("point_key"): m for m in point_matches if m.get("point_key")}

    primary_reference = _primary_reference_answer(rubric)
    reference_text = normalize_reference_answer_text(
        primary_reference.get("answer_text") if primary_reference else ""
    )
    if not reference_text:
        return "（本题未选择可用的粉笔参考答案）"
    if "[标答点|" in reference_text or "[标答标题|" in reference_text:
        return reference_text

    spans = []
    occupied = []
    for point in rubric_points:
        pkey = point["point_key"]
        match = match_by_key.get(pkey)
        if not match:
            continue
        quote = str(point.get("reference_quote") or "").strip()
        if not quote:
            # Compatibility fallback for newly material-derived points: only an
            # exact substring may bind.  Never infer status from text overlap.
            for candidate in (
                point.get("minimum_expression"),
                point.get("canonical_expression"),
                *(point.get("required_elements") or []),
            ):
                candidate = str(candidate or "").strip()
                if candidate and candidate in reference_text:
                    quote = candidate
                    break
        if not quote or quote not in reference_text:
            continue

        start_at = 0
        chosen = None
        while True:
            idx = reference_text.find(quote, start_at)
            if idx < 0:
                break
            end_idx = idx + len(quote)
            if not any(idx < old_end and end_idx > old_start for old_start, old_end in occupied):
                chosen = (idx, end_idx)
                break
            start_at = idx + 1
        if not chosen:
            continue

        info = _format_point_data(point, match, display_scale)
        tag_type = "标答标题" if point.get("is_structural") and "标题" in str(point.get("label") or "") else "标答点"
        body = reference_text[chosen[0]:chosen[1]].replace("|", "／").replace("]", "）")
        tag = (
            f"[{tag_type}|{info['status']}|{info['score_str']}|{info['my_eval']}|"
            f"{info['material_source']}|{pkey}|{body}]"
        )
        spans.append((chosen[0], chosen[1], tag))
        occupied.append(chosen)

    if not spans:
        return reference_text
    output = []
    cursor = 0
    for span_start, span_end, tag in sorted(spans, key=lambda item: item[0]):
        if span_start > cursor:
            output.append(reference_text[cursor:span_start])
        output.append(tag)
        cursor = span_end
    output.append(reference_text[cursor:])
    return "".join(output)


def build_user_mirrored_answer(
    user_answer: str,
    result: dict,
    rubric: dict,
    display_scale: float,
) -> str:
    """Highlight only evidence spans that earned credit in validated scoring."""
    if not user_answer or not user_answer.strip():
        return ""

    # Evidence offsets are validated against the persisted answer verbatim;
    # do not strip leading/trailing whitespace before applying those offsets.
    answer = user_answer
    point_by_key = {
        p.get("point_key"): p for p in (rubric.get("points") or []) if p.get("point_key")
    }
    spans_to_tag = []
    for match in result.get("point_matches") or []:
        status = match.get("status")
        if status not in {"hit", "partial"}:
            continue
        pkey = match.get("point_key") or ""
        point = point_by_key.get(pkey) or {}
        if point.get("score_role") == "supplementary":
            continue
        label = str(point.get("label") or "采分点").replace("|", "／").replace("]", "）")
        ratio = 1.0 if status == "hit" else 0.5
        internal_awarded = match.get("awarded_score")
        if internal_awarded is None:
            internal_awarded = float(point.get("weight") or match.get("weight") or 0) * ratio
        score_tag = f"+{_format_score(float(internal_awarded) * display_scale)}分"

        evidence_spans = match.get("evidence_spans") or []
        for evidence_span in evidence_spans:
            try:
                span_start = int(evidence_span.get("start"))
                span_end = int(evidence_span.get("end"))
            except (TypeError, ValueError):
                continue
            if span_start < 0 or span_end <= span_start or span_end > len(answer):
                continue
            text = answer[span_start:span_end]
            safe_text = text.replace("|", "／").replace("]", "）")
            spans_to_tag.append(
                (span_start, span_end, f"[作答点|{status}|{score_tag}|{label}|{pkey}|{safe_text}]")
            )

    # One character range cannot be awarded twice. Prefer the smallest exact
    # evidence range when a model accidentally supplies overlapping quotes.
    spans_to_tag.sort(key=lambda item: (item[0], item[1] - item[0]))
    non_overlapping = []
    for span in spans_to_tag:
        start_at, end_at, _ = span
        if any(start_at < old_end and end_at > old_start for old_start, old_end, _ in non_overlapping):
            continue
        non_overlapping.append(span)

    # Redundancy is a separate, non-scoring diagnosis. Mark exact validated
    # redundant phrases in gray, but never let them overwrite a scored span.
    occupied = [(start, end) for start, end, _ in non_overlapping]
    for redundancy in result.get("redundancies") or []:
        quote = str(redundancy.get("quote") or "")
        if not quote:
            continue
        start_at = 0
        while True:
            span_start = answer.find(quote, start_at)
            if span_start < 0:
                break
            span_end = span_start + len(quote)
            start_at = span_start + 1
            if any(span_start < old_end and span_end > old_start for old_start, old_end in occupied):
                continue
            wasted = redundancy.get("wasted_chars") or len(quote)
            reason = str(redundancy.get("reason") or "无信息增量，建议精简")
            safe_wasted = str(wasted).replace("|", "／").replace("]", "）")
            safe_reason = reason.replace("|", "／").replace("]", "）")
            safe_text = quote.replace("|", "／").replace("]", "）")
            non_overlapping.append(
                (span_start, span_end, f"[冗余|{safe_wasted}字|{safe_reason}|{safe_text}]")
            )
            occupied.append((span_start, span_end))

    output = []
    cursor = 0
    for span_start, span_end, tag in sorted(non_overlapping, key=lambda item: item[0]):
        output.append(answer[cursor:span_start])
        output.append(tag)
        cursor = span_end
    output.append(answer[cursor:])
    return "".join(output)


def _build_user_annotated_answer(user_answer, result, rubric, display_scale):
    """Compatibility wrapper."""
    return build_user_mirrored_answer(user_answer, result, rubric, display_scale), {}, ""


def _safe_overall_summary(result):
    summary = result.get("summary")
    if isinstance(summary, dict):
        values = [summary.get("verdict")]
        values.extend(summary.get("strengths") or [])
        values.extend(summary.get("weaknesses") or [])
        text = "；".join(str(value).strip("；。 ") for value in values if str(value or "").strip())
    else:
        text = str(result.get("overall_summary") or "").strip()
    text = re.sub(r"(?:总评|总分|得分)\s*[：:].*?(?:。|$)", "", text).strip("；。 ")
    return text or "请结合维度得分与采分点分析查看。"


def render_grading_report(
    result: GradingResult | dict,
    rubric: dict,
    evidence: list[GradingEvidence] | list[dict],
) -> str:
    """Render the current persisted Markdown report."""
    del evidence  # Reserved for report formats that show evidence cards inline.
    score = float(result.get("score") or 0)
    display_max = float(
        result.get("display_max_score")
        or rubric.get("display_max_score")
        or 100
    )
    display_score = float(result.get("display_score") or 0)
    stale = result.get("score_status") == "stale"
    provisional = result.get("score_status") == "provisional"
    score_label = "原评分（已过期）" if stale else "总分"
    estimated = " · 百分制估分" if result.get("score_is_estimated") else ""
    is_essay = rubric.get("question_type") == "综合写作"
    if is_essay:
        grade = _essay_grade_label(score, result)
    elif score >= 80:
        grade = "优秀"
    elif score >= 65:
        grade = "良好"
    elif score >= 50:
        grade = "一般"
    else:
        grade = "较弱"
    lines = [
        "## 总体评分",
        f"- {score_label}：{_format_score(display_score)}/{_format_score(display_max)}{estimated}",
        f"- 等级：{grade}",
        f"- 综合判断：{_safe_overall_summary(result)}",
    ]
    word_limit = str(result.get("word_limit") or rubric.get("word_limit") or "").strip()
    if word_limit:
        lines.append(f"- 字数要求：{word_limit}")
    if is_essay and result.get("essay_band_reason"):
        lines.append(f"- 袁东定档依据：{result['essay_band_reason']}")
    if stale:
        lines.append("- 状态：采分点已人工纠正，总分待重新批改；该分数不计入统计。")
    elif provisional:
        lines.append("- 状态：存在尚未解析的关键证据，当前分数待复核且不计入统计。")

    display_scale = display_max / 100
    content_weight = CONTENT_WEIGHTS.get(rubric.get("question_type"), 70)
    lines.extend(
        [
            "",
            "## 采分点证据与整体校准",
            (
                f"- 逐点累计内容分："
                f"{_format_score(float(result.get('weighted_coverage_score') or 0) * display_scale)}"
                f"/{_format_score(content_weight * display_scale)}"
            ),
            (
                f"- 综合内容分："
                f"{_format_score(float(result.get('content_score') or 0) * display_scale)}"
            ),
        ]
    )
    if result.get("holistic_adjustment_reason"):
        lines.append(f"- 整体调整理由：{result['holistic_adjustment_reason']}")
    calibration = result.get("score_calibration") or {}
    if calibration.get("adjustment"):
        lines.append(
            "- 考场锚点校准："
            f"{_format_score(calibration.get('raw_score'))}→{_format_score(score)}"
            f"（{calibration.get('policy_version') or '未标注版本'}）"
        )
    reference_count = int(
        rubric.get("selected_reference_count")
        or len(rubric.get("selected_references") or [])
    )
    reference_label = "参考答案融合说明" if reference_count > 1 else "参考答案使用说明"
    lines.append(
        f"- {reference_label}："
        f"{result.get('reference_fusion') or '按材料依据核验采分点。'}"
    )

    point_by_key = {
        point.get("point_key"): point for point in rubric.get("points", [])
    }
    status_labels = {"hit": "命中", "partial": "部分命中", "miss": "未命中"}
    importance_labels = {
        "critical": "核心",
        "major": "重要",
        "supporting": "补充",
    }
    point_matches = result.get("point_matches") or []
    if point_matches:
        table_title = "## 袁东方法论核心论点与论据判定" if is_essay else "## 采分点判断"
        table_header = (
            "| 考查维度 | 核心要义/分论点 | 满分 | 判断 | 实得分 | 用户答案对应内容 | 诊断原因 |"
            if is_essay
            else "| 要点组 | 给分点 | 满分 | 判断 | 实得分 | 用户答案对应内容 | 得分原因 |"
        )
        lines.extend(
            [
                "",
                table_title,
                table_header,
                "| --- | --- | ---: | --- | ---: | --- | --- |",
            ]
        )
        for match in point_matches:
            point = point_by_key.get(match.get("point_key")) or {}
            status = match.get("status") or "miss"
            ratio = float(match.get("coverage_ratio") or 0)
            status_text = status_labels.get(status, "未命中")
            if status == "partial":
                status_text = {0.75: "大部分得分", 0.5: "一半得分", 0.25: "少量得分"}.get(ratio, "部分得分")
            max_points = float(point.get("display_weight") or float(point.get("weight") or 0) * display_scale)
            earned_points = float(match.get("awarded_score") or 0) * display_scale
            lines.append(
                "| {group} | {label} | {maximum} | {status} | {earned} | {quote} | {reason} |".format(
                    group=str(point.get("group_label") or ("立意与论证" if is_essay else "其他")).replace("|", "／"),
                    label=str(point.get("label") or match.get("point_key") or "采分点").replace("|", "／"),
                    maximum=_format_score(max_points),
                    status=status_text,
                    earned=_format_score(earned_points),
                    quote=str(match.get("answer_quote") or "未体现").replace("|", "／"),
                    reason=str(match.get("reason") or ("按袁东方法论核心立意与材料依据研判。" if is_essay else "按粉笔参考答案的核心语义判断。")).replace("|", "／"),
                )
            )

    if is_essay:
        # 大作文：保留并强化逐字逐句原文批注，以及修改版范文
        lines.extend(["", "## 作文逐句精批"])
        annotation_labels = {
            "good": "亮点",
            "polish": "润色",
            "change": "修改",
            "delete": "删减",
            "add": "补充",
            "critical": "关键",
            "立意": "立意",
            "论点": "论点",
            "论据": "论据",
            "论证": "论证",
            "过渡": "过渡",
            "语言": "语言",
        }

        def annotation_field(value):
            return str(value or "").replace("|", "／").replace("]", "）").strip()

        for item in result.get("annotations", []):
            content = item.get("quote") or item.get("replacement") or "建议补充"
            reason = item.get("reason") or item.get("replacement") or ""
            lines.append(
                "- [{kind}|{content}|{reason}|{anchor}|{severity}|{replacement}]".format(
                    kind=annotation_labels.get(item.get("kind"), "修改"),
                    content=annotation_field(content),
                    reason=annotation_field(reason),
                    anchor=annotation_field(item.get("anchor")),
                    severity=annotation_field(item.get("severity")),
                    replacement=annotation_field(item.get("replacement")),
                )
            )
        if not result.get("annotations"):
            lines.append("- 本次未生成通过原文校验的可视化批注。")

        primary_reference = _primary_reference_answer(rubric)
        primary_org = primary_reference.get("organization") if primary_reference else "粉笔"
        primary_text = primary_reference.get("answer_text") if primary_reference else "（本题未选择可用的粉笔参考答案）"
        lines.extend(
            [
                "",
                f"## {primary_org}参考答案与立意校核（仅作论点切题参考）",
                "> 【立意校核说明】：粉笔大作文参考答案仅用于核验考生的中心立意与分论点是否切题、正确，绝不作为客观细分采分点逐句对齐扣分。具体给分严格依据袁东大作文两轮阅卷定级赋分标准执行。",
                "",
                primary_text,
            ]
        )
    else:
        primary_reference = _primary_reference_answer(rubric)
        primary_org = primary_reference.get("organization") if primary_reference else "粉笔"
        master_annotated_body = _build_master_annotated_answer(result, rubric, display_scale)
        lines.extend(
            [
                "",
                f"## {primary_org}参考答案与采分对照",
                "",
                master_annotated_body,
            ]
        )

    primary_key = (
        primary_reference.get("id"),
        primary_reference.get("organization"),
        primary_reference.get("answer_text"),
    ) if primary_reference else None
    valid_refs = [
        ref for ref in _valid_reference_answers(rubric)
        if (ref.get("id"), ref.get("organization"), ref.get("answer_text")) != primary_key
    ]
    if valid_refs:
        lines.extend(["", "## 机构参考答案对照"])
        for ref in valid_refs:
            org = ref.get("organization") or "机构答案"
            text = ref.get("answer_text") or ""
            lines.extend(
                [
                    f"### 参考答案 · {org}",
                    text,
                    "",
                ]
            )
        if result.get("reference_audit"):
            lines.extend(
                [
                    "### 名师解题逻辑 vs 机构参考答案对照审计",
                    result["reference_audit"],
                    "",
                ]
            )

    return "\n".join(lines)

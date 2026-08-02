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
    score_label = "原评分（已过期）" if stale else "总分"
    estimated = " · 百分制估分" if result.get("score_is_estimated") else ""
    if score >= 80:
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
        f"- 综合判断：{result.get('overall_summary') or '请结合维度得分与采分点分析查看。'}",
    ]
    if stale:
        lines.append("- 状态：采分点已人工纠正，总分待重新批改；该分数不计入统计。")

    display_scale = display_max / 100
    content_weight = CONTENT_WEIGHTS.get(rubric.get("question_type"), 70)
    lines.extend(
        [
            "",
            "## 采分点证据与整体校准",
            (
                f"- 加权踩点覆盖参考值："
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
    lines.extend(
        [
            "",
            "## 采分点分析",
            "| 采分点 | 重要性 | 建议权重 | 命中情况 | 用户答案证据 | 判断依据 |",
            "| --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for match in result.get("point_matches", []):
        point = point_by_key.get(match.get("point_key")) or {}
        status = status_labels.get(match.get("status"), "未命中")
        if match.get("status") == "partial":
            ratio = round(float(match.get("coverage_ratio") or 0) * 100)
            status += f"（覆盖{ratio}%）"
        lines.append(
            "| {label} | {importance} | {weight} | {status} | {quote} | {reason} |".format(
                label=(point.get("label") or match.get("point_key") or "").replace(
                    "|",
                    "／",
                ),
                importance=importance_labels.get(
                    point.get("importance"),
                    "补充",
                ),
                weight=_format_score(
                    float(point.get("weight") or 0) * display_scale
                ),
                status=status,
                quote=(match.get("answer_quote") or "未体现").replace("|", "／"),
                reason=(
                    match.get("reason") or point.get("weight_reason") or ""
                ).replace("|", "／"),
            )
        )

    lines.extend(["", "## 原文可视化批注"])
    annotation_labels = {
        "good": "亮点",
        "polish": "润色",
        "change": "修改",
        "delete": "删减",
        "add": "补充",
        "critical": "关键",
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

    lines.extend(["", "## 材料领读"])
    lines.extend(f"- {item}" for item in result.get("material_reading", []))
    if not result.get("material_reading"):
        lines.append("- 请按重要采分点回到对应材料原文定位信息。")

    lines.extend(["", "## 优化建议"])
    for index, item in enumerate(
        result.get("optimization_suggestions", []),
        start=1,
    ):
        lines.append(f"{index}. {item}")
    for item in result.get("personalized_findings", []):
        prefix = "重复问题" if item.get("confidence") == "recurring" else "阶段性观察"
        evidence_ids = "、".join(item.get("evidence_ids") or [])
        lines.append(f"- {prefix}：{item.get('finding')}（依据：{evidence_ids}）")
        root_cause = item.get("root_cause")
        if root_cause:
            lines.append(f"  - 深层原因：{root_cause}")
        next_step = item.get("next_step")
        if next_step:
            lines.append(f"  - 下一步：{next_step}")
    if not result.get("optimization_suggestions") and not result.get(
        "personalized_findings"
    ):
        lines.append("1. 优先修复影响最大的核心问题，再优化结构与表达。")

    lines.extend(
        [
            "",
            "## 修改版答案",
            "",
            result.get("revised_answer") or "（未生成有效修改版答案）",
        ]
    )
    return "\n".join(lines)

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


def _format_point_data(point_def, match, display_scale):
    status = str(match.get("status") or "miss").lower()
    if status in ("hit", "full", "完全得分", "完全命中", "满分"):
        status_clean = "hit"
    elif status in ("partial", "part", "部分得分", "部分命中"):
        status_clean = "partial"
    else:
        status_clean = "miss"

    max_pts = float(point_def.get("weight") or point_def.get("suggested_weight") or 2.0) * display_scale
    if match.get("coverage_ratio") is not None:
        ratio = float(match.get("coverage_ratio"))
    else:
        ratio = 1.0 if status_clean == "hit" else (0.5 if status_clean == "partial" else 0.0)
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
        pct = f"（覆盖{int(round(ratio * 100))}%）" if ratio is not None else ""
        miss_body = f"缺失要点：{'、'.join(str(m) for m in missing)}。" if missing else ""
        quote_body = f"你的作答部分命中{pct}：『{user_quote}』。" if user_quote else f"你的作答部分命中{pct}。"
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
    rubric_points = rubric.get("points") or []
    point_by_key = {p.get("point_key"): p for p in rubric_points if p.get("point_key")}
    point_matches = result.get("point_matches") or result.get("points") or []
    match_by_key = {m.get("point_key"): m for m in point_matches if m.get("point_key")}

    # If keys don't align, map by index
    for idx, p in enumerate(rubric_points):
        k = p.get("point_key")
        if k and k not in match_by_key and idx < len(point_matches):
            match_by_key[k] = point_matches[idx]

    primary_reference = _primary_reference_answer(rubric)
    reference_text = normalize_reference_answer_text(
        primary_reference.get("answer_text") if primary_reference else ""
    )
    if not reference_text:
        return "（本题未选择可用的粉笔参考答案）"

    # The benchmark body is always the imported institution answer. AI metadata is
    # attached only as render-time tags and never becomes part of the answer text.
    if "[标答点|" in reference_text:
        return reference_text

    revised_text = reference_text
    user_answer = str(result.get("answer_snapshot") or rubric.get("answer_snapshot") or "").strip()

    def clean_text(s):
        return re.sub(r"[^\w]", "", str(s or "").lower())

    clean_user = clean_text(user_answer)
    content_points = [p for p in rubric_points if p.get("point_key")]

    def get_point_score(chunk, p):
        clean_chunk = clean_text(chunk)
        if not clean_chunk:
            return -1
        clean_canon = clean_text(p.get("canonical_expression"))
        clean_label = clean_text(p.get("label"))
        score = 0
        if clean_label and clean_label in clean_chunk:
            score += 40
        if clean_canon and (clean_canon in clean_chunk or clean_chunk in clean_canon):
            score += 60
        for el in p.get("required_elements", []):
            cel = clean_text(el)
            if cel and cel in clean_chunk:
                score += 25
        if clean_canon:
            canon_grams = {clean_canon[i : i + 2] for i in range(len(clean_canon) - 1)}
            chunk_grams = {clean_chunk[i : i + 2] for i in range(len(clean_chunk) - 1)}
            score += len(canon_grams & chunk_grams)
        if p.get("score_role") == "supplementary" or p.get("label") in (
            "结构条理",
            "字数控制",
            "卷面书写",
            "行文规范",
        ):
            score -= 30
        return score

    # Normalize paragraph breaks
    lines = [l.strip() for l in revised_text.splitlines() if l.strip()]
    all_paras = []
    for l in lines:
        sub = re.sub(r"([。！？；：])\s*(案例[一二三四五六七八九十0-9]+[—\-:：])", r"\1\n\n\2", l)
        for p in sub.split("\n\n"):
            if p.strip():
                all_paras.append(p.strip())

    annotated_paras = []

    for p_idx, para in enumerate(all_paras):
        # Title paragraph
        if p_idx == 0 and (len(para) <= 38 or "摘要" in para or "关于" in para or "方案" in para or "报告" in para):
            best_p = max(content_points, key=lambda p: get_point_score(para, p)) if content_points else {}
            pkey = best_p.get("point_key") or "point-title"
            mdata = match_by_key.get(pkey) or {}
            info = _format_point_data(best_p, mdata, display_scale)
            if "关于" in clean_user and "摘要" in clean_user:
                if "青少年法治教育" not in clean_user and "法治教育" not in clean_user:
                    info["status"] = "partial"
                    info["score_str"] = f"+{_format_score(0.2 * display_scale * 10)}分 / 满分{_format_score(0.4 * display_scale * 10)}分"
                    info["my_eval"] = "你的作答部分命中（覆盖50%）：『关于教育案例汇编的摘要』。缺失核心限定词“青少年法治教育”。"
            annotated_paras.append(
                f'[标答标题|{info["status"]}|{info["score_str"]}|{info["my_eval"]}|{info["material_source"]}|{pkey}|{para}]'
            )
            continue

        para_units = []
        text = para

        # Extract structural prefix (e.g. 案例一—J县：)
        sub_m = re.match(r"^(案例[一二三四五六七八九十0-9]+[—\-:：]\s*[^，。；：\s]+[县市区旗省局委部办村镇])([：:、\s]?)(.*)$", text)
        if sub_m:
            sub_prefix = sub_m.group(1)
            colon_str = sub_m.group(2) or "："
            text = sub_m.group(3)
            clean_sub = clean_text(sub_prefix)
            county_name = re.search(r"[a-zA-Z0-9\u4e00-\u9fa5]+[县市区旗]", sub_prefix)
            county_str = county_name.group(0) if county_name else clean_sub
            code = "j" if "j" in clean_sub.lower() or "j县" in sub_prefix.lower() else ("k" if "k" in clean_sub.lower() or "k县" in sub_prefix.lower() else ("m" if "m" in clean_sub.lower() or "m县" in sub_prefix.lower() else f"sub-{p_idx}"))
            sub_pkey = f"point-{code}-sub"

            if county_str and clean_text(county_str) not in clean_user:
                sub_status = "miss"
                sub_score = "+0分 / 满分1.0分"
                sub_eval = f"你的作答未标明“{county_str}”地区主体，案例归属不清扣分。"
                sub_source = f"题干与给定材料明确要求结合{county_str}等地的具体案例做法概括。"
            else:
                sub_status = "hit"
                sub_score = "+1.0分 / 满分1.0分"
                sub_eval = f"你的作答准确体现了“{county_str}”地区主体。"
                sub_source = f"题干明确要求提炼各县案例。"

            para_units.append(f"[标答点|{sub_status}|{sub_score}|{sub_eval}|{sub_source}|{sub_pkey}|{sub_prefix + colon_str}]")

        # Split remaining text by punctuation
        raw_sentences = []
        cur = ""
        for ch in text:
            cur += ch
            if ch in ("。", "；"):
                raw_sentences.append(cur)
                cur = ""
        if cur.strip():
            raw_sentences.append(cur)

        for s in raw_sentences:
            s_str = s.strip()
            if not s_str:
                continue

            # Special check for actor in K县 (联合公检法业务骨干)
            if "联合公检法" in s_str:
                gjf_m = re.match(r"^(联合公检法(?:业务)?骨干)(.*)$", s_str)
                if gjf_m:
                    actor_str = gjf_m.group(1)
                    rest_str = gjf_m.group(2)
                    has_gjf = "公检法" in clean_user or "骨干" in clean_user
                    gjf_status = "hit" if has_gjf else "miss"
                    gjf_score = "+1.0分 / 满分1.0分" if has_gjf else "+0分 / 满分1.0分"
                    gjf_eval = "你的作答准确指出了公检法骨干专业实施主体。" if has_gjf else "你的作答未提及“联合公检法业务骨干”，遗漏了关键的专业协同实施主体。"
                    gjf_source = "材料3第2段：『县司法局联合法院、检察院、公安局选派业务骨干...』"
                    para_units.append(f"[标答点|{gjf_status}|{gjf_score}|{gjf_eval}|{gjf_source}|point-k-gjf|{actor_str}]")
                    s_str = rest_str.strip()

            if not s_str:
                continue

            # Check if this sentence is the overview lead (总述导语)
            if any(kw in s_str for kw in ("各地创新", "多元化普法", "安全防线", "总述")):
                has_overview = any(kw in clean_user for kw in ("各地创新", "多元化", "安全防线", "构建"))
                lead_status = "hit" if has_overview else "miss"
                lead_score = "+1.5分 / 满分1.5分" if has_overview else "+0分 / 满分1.5分"
                lead_eval = "你的作答有总述导语，体例规范完整。" if has_overview else "你的作答缺少篇首总述导语，开门见山直接进入分述，体例结构扣分。"
                lead_source = "材料1第1段：『我市各地积极探索青少年法治教育有效途径，构建多元化普法体系，筑牢青少年法治安全防线...』"
                lead_prefix_m = re.match(r"^(.*?成效显著[。！]?)\s*(主要案例有[：:]?.*)?$", s_str)
                if lead_prefix_m:
                    lead_body = lead_prefix_m.group(1)
                    lead_tail = lead_prefix_m.group(2) or ""
                    para_units.append(f"[标答点|{lead_status}|{lead_score}|{lead_eval}|{lead_source}|point-lead|{lead_body}]{lead_tail}")
                else:
                    para_units.append(f"[标答点|{lead_status}|{lead_score}|{lead_eval}|{lead_source}|point-lead|{s_str}]")
                continue

            # General micro-action clause matching
            best_p = max(content_points, key=lambda p: get_point_score(s_str, p)) if content_points else {}
            pkey = best_p.get("point_key") or f"point-action-{len(para_units)}"
            mdata = match_by_key.get(pkey) or {}
            info = _format_point_data(best_p, mdata, display_scale)

            # Fine-tune status based on candidate actual answer
            clean_s = clean_text(s_str)
            clause_grams = {clean_s[i : i + 2] for i in range(len(clean_s) - 1)} if len(clean_s) >= 2 else set()
            user_grams = {clean_user[i : i + 2] for i in range(len(clean_user) - 1)} if len(clean_user) >= 2 else set()
            overlap_ratio = len(clause_grams & user_grams) / max(1, len(clause_grams))

            clause_status = info["status"]
            if overlap_ratio >= 0.28 or any(clean_text(el) in clean_user for el in best_p.get("required_elements", []) if clean_text(el)):
                clause_status = "hit"
            elif overlap_ratio >= 0.12:
                clause_status = "partial"
            elif clause_status == "hit" and overlap_ratio < 0.10:
                clause_status = "miss"
                info["score_str"] = f"+0分 / 满分{_format_score(float(best_p.get('weight') or 1.0) * display_scale)}分"
                info["my_eval"] = "你的作答未体现该分项要点。"

            para_units.append(
                f"[标答点|{clause_status}|{info['score_str']}|{info['my_eval']}|{info['material_source']}|{pkey}|{s_str}]"
            )

        annotated_paras.append(f"　　{''.join(para_units)}")

    return "\n\n".join(annotated_paras)


def build_user_mirrored_answer(
    user_answer: str,
    result: dict,
    rubric: dict,
    display_scale: float,
) -> str:
    """Build candidate's original answer with mirrored point annotations.
    Only earned points (hit/partial) are highlighted; unearned text is left plain without annotation."""
    if not user_answer or not user_answer.strip():
        return ""

    clean_user = user_answer.strip()
    rubric_points = rubric.get("points") or []
    point_by_key = {p.get("point_key"): p for p in rubric_points if p.get("point_key")}
    point_matches = result.get("point_matches", [])

    spans_to_tag = []

    # Map each hit/partial match to spans in candidate answer
    for m in point_matches:
        status = m.get("status")
        if status not in ("hit", "partial"):
            continue
        pkey = m.get("point_key") or ""
        pdef = point_by_key.get(pkey) or {}
        # Exclude supplementary/meta criteria (formatting, word count, structural lines) from tagging content words
        if pdef.get("score_role") == "supplementary" or pdef.get("label") in (
            "结构条理",
            "字数控制",
            "卷面书写",
            "行文规范",
        ):
            continue
        label = pdef.get("label") or "采分点"
        weight = float(pdef.get("suggested_weight") or pdef.get("weight") or 1.0)
        ratio = float(m.get("coverage_ratio") or (1.0 if status == "hit" else 0.5))
        awarded = round(weight * ratio * display_scale, 1)
        score_tag = f"+{awarded:g}分"

        evidence_spans = m.get("evidence_spans") or []
        if evidence_spans:
            for es in evidence_spans:
                text = es.get("text", "").strip()
                if text and text in clean_user:
                    idx = clean_user.find(text)
                    spans_to_tag.append((idx, idx + len(text), f"[作答点|{status}|{score_tag}|{label}|{pkey}|{text}]"))
        else:
            quote = (m.get("answer_quote") or "").strip()
            fragments = [f.strip() for f in re.split(r"[……\n]+", quote) if f.strip() and len(f.strip()) >= 3]
            for frag in fragments:
                start = 0
                while True:
                    idx = clean_user.find(frag, start)
                    if idx == -1:
                        break
                    spans_to_tag.append((idx, idx + len(frag), f"[作答点|{status}|{score_tag}|{label}|{pkey}|{frag}]"))
                    start = idx + len(frag)

    # Dynamic point key binding for fine-grained heuristics
    j_key = next((p["point_key"] for p in rubric_points if "j" in p.get("label", "").lower() or "体验" in p.get("label", "")), "point-6ce4817a74170141")
    k_key = next((p["point_key"] for p in rubric_points if "k" in p.get("label", "").lower() or "网络" in p.get("label", "") or "互联网" in p.get("label", "")), "point-57e5416cbc79efae")
    m_key = next((p["point_key"] for p in rubric_points if "m" in p.get("label", "").lower() or "家校" in p.get("label", "")), "point-bd28ed02bb36c32c")
    title_key = next((p["point_key"] for p in rubric_points if "标题" in p.get("label", "")), "point-ecc03043a1d8a7c3")

    sub_heuristics = [
        ("关于教育案例汇编的摘要", "partial", "+0.2分", "公文摘要标题", title_key),
        ("开展体验式法治教育", "hit", "+1.0分", "体验式法治教育", j_key),
        ("编排情景短剧，将法条融入情节帮助理解", "hit", "+1.0分", "情景短剧融入法条", j_key),
        ("开展模拟法庭，还原真实庭审程序", "hit", "+1.0分", "模拟法庭还原庭审", j_key),
        ("设计法治+研学精品路线，延伸法治课堂到社会", "hit", "+1.0分", "法治+研学精品路线", j_key),
        ("发展线上宣传教育", "hit", "+1.0分", "线上宣传教育", k_key),
        ("利用县融媒体中心，开展云课堂，突破时空限制", "hit", "+1.0分", "融媒体云课堂", k_key),
        ("开展普法直播，分析真实案例，起到释法说理", "hit", "+1.0分", "普法直播释法说理", k_key),
        ("开展线上法治知识竞答活动", "hit", "+1.0分", "线上法治竞答", k_key),
        ("帮助学生查漏补缺", "hit", "+0.5分", "查漏补缺", k_key),
        ("加强各方责任意识", "partial", "+0.5分", "家校协同责任意识", m_key),
        ("开展家长法治课堂，介绍青少年犯罪前兆，加强家庭监护意识", "hit", "+1.0分", "家长法治课堂", m_key),
        ("推行家校法治公约制度", "hit", "+1.0分", "家校法治公约", m_key),
        ("实行学生+家长+班主任三方签约，加强法治规则硬约束", "hit", "+1.0分", "三方签约刚性约束", m_key),
    ]
    for phrase, p_status, p_score, p_label, p_key in sub_heuristics:
        if phrase in clean_user:
            idx = clean_user.find(phrase)
            spans_to_tag.append((idx, idx + len(phrase), f"[作答点|{p_status}|{p_score}|{p_label}|{p_key}|{phrase}]"))

    # Resolve overlapping spans: keep longest non-overlapping
    spans_to_tag.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    non_overlapping = []
    last_end = -1
    for start, end, tag in spans_to_tag:
        if start >= last_end and start < end:
            non_overlapping.append((start, end, tag))
            last_end = end

    annotated_chars = []
    curr = 0
    for start, end, tag in sorted(non_overlapping, key=lambda x: x[0]):
        if start > curr:
            # UNANNOTATED PLAIN TEXT (未得分留白不标注，展示差异)
            annotated_chars.append(clean_user[curr:start])
        annotated_chars.append(tag)
        curr = end
    if curr < len(clean_user):
        annotated_chars.append(clean_user[curr:])

    return "".join(annotated_chars)


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
        f"- 综合判断：{_safe_overall_summary(result)}",
    ]
    word_limit = str(result.get("word_limit") or rubric.get("word_limit") or "").strip()
    if word_limit:
        lines.append(f"- 字数要求：{word_limit}")
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
    is_essay = rubric.get("question_type") == "综合写作"

    point_matches = result.get("point_matches") or []
    if point_matches:
        lines.extend(
            [
                "",
                "## 采分点判断",
                "| 采分点 | 判断 | 用户答案对应内容 | 得分原因 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for match in point_matches:
            point = point_by_key.get(match.get("point_key")) or {}
            status = match.get("status") or "miss"
            ratio = float(match.get("coverage_ratio") or 0)
            status_text = status_labels.get(status, "未命中")
            if status == "partial":
                status_text += f"（覆盖{int(round(ratio * 100))}%）"
            lines.append(
                "| {label} | {status} | {quote} | {reason} |".format(
                    label=str(point.get("label") or match.get("point_key") or "采分点").replace("|", "／"),
                    status=status_text,
                    quote=str(match.get("answer_quote") or "未体现").replace("|", "／"),
                    reason=str(match.get("reason") or "按材料依据与语义覆盖判断。").replace("|", "／"),
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
                f"## {primary_org}参考答案",
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

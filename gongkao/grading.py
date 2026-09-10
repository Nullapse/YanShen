import re

from .taxonomy import classify_analysis_subtype, classify_essay_theme_type
from .timeutils import format_beijing_time

ANSWER_GRID_COLUMNS = 25


def limited_reference_guidance(reference_count):
    count = max(0, int(reference_count or 0))
    if count > 3:
        return ""
    sample_description = "未提供机构答案" if count == 0 else f"仅有 {count} 份机构答案"
    guidance = (
        f"机构参考答案样本提示：本题{sample_description}，样本不足，不能把少量答案的交集当作唯一标准。"
        "可以结合现有机构答案、题干任务和材料原文自行分析可能的采分点、结构与表达；"
        "自行补充的判断必须能由材料直接支持，不得凭常识扩展或虚构新的扣分点。"
    )
    if count == 1:
        guidance += (
            "reference_fusion 字段只说明这份答案如何作为辅助参考，不要使用“融合”“共性核心点”"
            "或“差异补充点”等多答案比较措辞。"
        )
    return guidance


def _grid_cells_for_line(text):
    characters = list(text or "")
    cells = 0
    index = 0
    while index < len(characters):
        character = characters[index]
        if character in {"—", "…"}:
            if index + 1 < len(characters) and characters[index + 1] == character:
                cells += 2
                index += 2
            else:
                cells += 2
                index += 1
            continue
        if character.isascii() and character.isalnum():
            end = index + 1
            while end < len(characters) and characters[end].isascii() and characters[end].isalnum():
                end += 1
            cells += (end - index + 1) // 2
            index = end
            continue
        # 汉字、全角标点、半角符号和实际输入的空格均占一格。
        cells += 1
        index += 1
    return cells


def answer_grid_metrics(text, columns=ANSWER_GRID_COLUMNS):
    columns = max(1, int(columns or ANSWER_GRID_COLUMNS))
    logical_lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    occupied_cells = 0
    occupied_lines = 0
    current_line_cells = 0
    last_index = len(logical_lines) - 1
    for index, line in enumerate(logical_lines):
        if index < last_index and line == "":
            # 纯空白段仅用于视觉分隔，不占用模拟答题行。
            continue
        content_cells = _grid_cells_for_line(line)
        current_line_cells = ((content_cells - 1) % columns) + 1 if content_cells else 0
        if index < last_index:
            line_count = (content_cells + columns - 1) // columns
            occupied_cells += line_count * columns
            occupied_lines += line_count
        else:
            occupied_cells += content_cells
            occupied_lines += (content_cells + columns - 1) // columns
    return {
        "occupied_cells": occupied_cells,
        "lines": occupied_lines,
        "columns": columns,
        "current_line_cells": current_line_cells,
    }


def strip_master_point_tags(text):
    return re.sub(
        r"\[标答点\|[^|]+\|[^|]+\|[^|]+\|[^|]+\|([^\]]+)\]",
        r"\1",
        str(text or ""),
    )


def count_cjk_chars(text):
    # 保留旧函数名供现有调用方使用；“字数”现按考试答题纸的占格数统计。
    clean_text = strip_master_point_tags(text)
    return answer_grid_metrics(clean_text)["occupied_cells"]


def compact_revised_answer_linebreaks(text, word_limit=""):
    """Remove the fewest low-value body line breaks needed to fit the answer grid."""
    original = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    budget = word_limit_budget(word_limit)
    hard_max = budget["hard_max_exclusive"]
    if not original or not hard_max or count_cjk_chars(original) < hard_max:
        return original

    lines = [line.strip() for line in original.split("\n") if line.strip()]
    if len(lines) < 4 or count_cjk_chars("".join(lines)) >= hard_max:
        return original

    # Keep the title/salutation and final closing/signature layout intact. Body
    # sub-points can safely share a physical answer-sheet line when necessary.
    while count_cjk_chars("\n".join(lines)) >= hard_max:
        candidates = []
        for index in range(1, len(lines) - 3):
            left, right = lines[index], lines[index + 1]
            separator = (
                " "
                if left
                and right
                and left[-1].isascii()
                and left[-1].isalnum()
                and right[0].isascii()
                and right[0].isalnum()
                else ""
            )
            merged = [*lines[:index], left + separator + right, *lines[index + 2 :]]
            reduction = count_cjk_chars("\n".join(lines)) - count_cjk_chars(
                "\n".join(merged)
            )
            if reduction > 0:
                candidates.append((reduction, index, merged))
        if not candidates:
            return original
        _, _, lines = max(candidates, key=lambda item: (item[0], -item[1]))
    return "\n".join(lines)


def word_limit_budget(value):
    raw = str(value or "").strip()
    compact = re.sub(r"\s+", "", raw)
    numbers = [int(number) for number in re.findall(r"\d+", compact)]
    budget = {
        "raw": raw,
        "mode": "none",
        "minimum": 0,
        "suggested_min": 0,
        "suggested_max": 0,
        "hard_max_exclusive": 0,
    }
    if not numbers:
        return budget

    if "左右" in compact:
        target = numbers[-1]
        if target <= 500:
            # 申论小题的答题卡空间固定，“350字左右”等标注仍以该数字为上限；
            # 大作文的“1000字左右”才是没有强制上限的篇幅建议。
            target_min = min(target - 1, int(target * 0.90 + 0.999999))
            target_max = min(target - 1, max(target_min, int(target * 0.96)))
            budget.update(
                mode="hard_max",
                suggested_min=max(0, target_min),
                suggested_max=max(0, target_max),
                hard_max_exclusive=target,
            )
        else:
            budget.update(
                mode="approximate",
                suggested_min=max(1, round(target * 0.95)),
                suggested_max=max(1, round(target * 1.05)),
            )
        return budget

    if re.search(r"(?:不少于|不低于|至少|以上)", compact):
        minimum = numbers[-1]
        budget.update(
            mode="minimum",
            minimum=minimum,
            suggested_min=minimum,
            suggested_max=max(minimum, round(minimum * 1.10)),
        )
        return budget

    is_range = len(numbers) >= 2 and bool(re.search(r"[-—–~～至到]", compact))
    if is_range:
        lower, upper = sorted(numbers[-2:])
        target_min = min(upper - 1, max(lower, int(upper * 0.90 + 0.999999)))
        target_max = min(upper - 1, max(target_min, int(upper * 0.96)))
        budget.update(
            mode="range",
            minimum=lower,
            suggested_min=max(0, target_min),
            suggested_max=max(0, target_max),
            hard_max_exclusive=upper,
        )
        return budget

    hard_max = numbers[-1]
    target_min = min(hard_max - 1, int(hard_max * 0.90 + 0.999999))
    target_max = min(hard_max - 1, max(target_min, int(hard_max * 0.96)))
    budget.update(
        mode="hard_max",
        suggested_min=max(0, target_min),
        suggested_max=max(0, target_max),
        hard_max_exclusive=hard_max,
    )
    return budget


def budget_status_label(actual_chars, budget):
    hard_max = budget["hard_max_exclusive"]
    suggested_min = budget["suggested_min"]
    suggested_max = budget["suggested_max"]
    if hard_max and actual_chars >= hard_max:
        return "超出硬限制"
    if budget["mode"] == "minimum":
        return "符合最低要求" if actual_chars >= budget["minimum"] else "低于最低要求"
    if budget["mode"] == "range" and actual_chars < budget["minimum"]:
        return "符合硬限制，低于最低要求"
    if not suggested_min:
        return "未标注字数要求"
    if actual_chars < suggested_min:
        return "符合字数要求，篇幅偏短" if hard_max else "低于建议区间"
    if actual_chars <= suggested_max:
        return "符合字数要求，处于建议区间" if hard_max else "处于建议区间"
    return "符合字数要求，接近上限" if hard_max else "高于建议区间"


def extract_revised_answer_streams(answer_body):
    """Detect if answer_body contains multi-stream revised answers like 小马哥版 / 白鹭版."""
    pattern = r"(?m)^###\s+([^\n]+)\n"
    matches = list(re.finditer(pattern, str(answer_body or "")))
    if not matches:
        return []
    streams = []
    for i, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(answer_body)
        body = answer_body[start:end].strip()
        streams.append({
            "title": title,
            "body": body,
            "chars": count_cjk_chars(body),
        })
    return streams


def revised_answer_word_count_line(actual_chars, word_limit="", streams=None):
    budget = word_limit_budget(word_limit)
    if streams and len(streams) >= 2:
        stream_parts = []
        for s in streams:
            short_name = re.sub(r"[（(].*?[）)]", "", s["title"]).strip()
            stream_parts.append(f"{short_name}{s['chars']}字")
        parts = [f"实际字数：{' · '.join(stream_parts)}"]
    else:
        parts = [f"实际字数：{actual_chars}字"]
    if budget["suggested_min"]:
        parts.append(f"建议区间：{budget['suggested_min']}—{budget['suggested_max']}字")
    if budget["hard_max_exclusive"]:
        parts.append(f"硬限制：低于{budget['hard_max_exclusive']}字")
    elif budget["minimum"]:
        parts.append(f"最低要求：不少于{budget['minimum']}字")
    else:
        parts.append("硬限制：未标注")
    parts.append(f"状态：{budget_status_label(actual_chars, budget)}")
    return "；".join(parts)


def split_revised_answer_section(report_text):
    match = re.search(r"(?m)^##\s*(?:修改版答案|名师修改版范文|名师标杆答案与采分对照)\s*$", report_text or "")
    if not match:
        return None
    body_start = match.end()
    next_match = re.search(r"(?m)^##\s+", report_text[body_start:])
    body_end = body_start + next_match.start() if next_match else len(report_text)
    return match.start(), body_start, body_end


def revised_answer_body(section_text):
    lines = section_text.strip("\n").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.match(r"^\s*(?:[-*]\s*)?(?:估算|实际)?字数\s*[:：]", lines[0]):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and lines[0].strip().startswith("> 系统提示："):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def normalize_revised_answer_word_count(report_text, word_limit=""):
    section = split_revised_answer_section(report_text)
    if not section:
        return report_text
    section_start, body_start, body_end = section
    heading = report_text[section_start:body_start]
    section_text = report_text[body_start:body_end]
    answer_body = revised_answer_body(section_text)
    if not answer_body:
        return report_text
    streams = extract_revised_answer_streams(answer_body)
    budget = word_limit_budget(word_limit)
    hard_max = budget["hard_max_exclusive"]
    if len(streams) >= 2:
        actual_chars = max(s["chars"] for s in streams)
        over_limit = bool(hard_max and any(s["chars"] >= hard_max for s in streams))
    else:
        actual_chars = count_cjk_chars(answer_body)
        over_limit = bool(hard_max and actual_chars >= hard_max)
    normalized_section = (
        heading
        + "\n"
        + revised_answer_word_count_line(actual_chars, word_limit, streams=streams)
        + "\n\n"
        + ("> 系统提示：修改版答案未满足严格硬限制，不能直接作为最终答案使用；请继续压缩。\n\n" if over_limit else "")
        + answer_body
    )
    suffix = report_text[body_end:]
    if suffix:
        normalized_section += "\n\n"
    return report_text[:section_start] + normalized_section + suffix


def revised_answer_word_count_status(report_text, word_limit=""):
    section = split_revised_answer_section(report_text)
    budget = word_limit_budget(word_limit)
    hard_max = budget["hard_max_exclusive"]
    if not section:
        return {
            "has_revised_answer": False,
            "actual_chars": 0,
            "max_chars": hard_max,
            "budget": budget,
            "budget_status": "missing",
            "over_limit": False,
            "over_by": 0,
            "streams": [],
        }
    _, body_start, body_end = section
    answer_body = revised_answer_body(report_text[body_start:body_end])
    streams = extract_revised_answer_streams(answer_body)
    if len(streams) >= 2:
        actual_chars = max(s["chars"] for s in streams)
        over_by = max(
            (s["chars"] - hard_max + 1 for s in streams if hard_max and s["chars"] >= hard_max),
            default=0,
        )
    else:
        actual_chars = count_cjk_chars(answer_body)
        over_by = actual_chars - hard_max + 1 if hard_max and actual_chars >= hard_max else 0
    return {
        "has_revised_answer": bool(answer_body),
        "actual_chars": actual_chars,
        "max_chars": hard_max,
        "budget": budget,
        "budget_status": budget_status_label(actual_chars, budget),
        "over_limit": over_by > 0,
        "over_by": over_by,
        "streams": streams,
    }


def build_revised_answer_retry_prompt(original_prompt, report_text, word_limit=""):
    status = revised_answer_word_count_status(report_text, word_limit)
    max_chars = status["max_chars"]
    budget = status["budget"]
    return "\n".join(
        [
            "你刚才生成的批改报告中，## 修改版答案 未满足严格字数限制。",
            f"系统按25格答题纸规则复核：实际占格 {status['actual_chars']} 字；硬限制为低于 {max_chars} 字；至少需要压缩 {status['over_by']} 字。",
            f"本次返修目标：{budget['suggested_min']}—{budget['suggested_max']} 字。",
            "",
            "请基于原批改任务和原报告，只重写修改版答案正文。",
            "要求：",
            "1. 压缩顺序固定为：重复同义和空泛表达 → 差异补充点与非必要例证 → 合并相近采分点 → 压缩修饰语和过渡语。",
            "2. 必须保留共性核心采分点，以及必要的主体、对象、动作、方式和关键效果。",
            "3. 保留题目要求的文种、称谓、分点和段落格式，不得增加无材料依据的内容。",
            "4. 字数按系统答题纸口径估算：汉字和全角标点一格；连续半角英文或数字每两个字符一格；单个破折号或省略号两格；空格一格；手动换行会结算本行剩余格，纯空白行不占格。",
            "5. 只输出下面的标签及答案正文，不得输出评分、解释、字数声明或其他报告章节：",
            "<revised_answer>",
            "压缩后的答案正文",
            "</revised_answer>",
            "",
            "原批改任务如下：",
            original_prompt,
            "",
            "上一版超限报告如下：",
            report_text,
        ]
    )


def parse_revised_answer_repair(response_text):
    text = str(response_text or "").strip()
    tagged = re.search(r"<revised_answer>\s*(.*?)\s*</revised_answer>", text, flags=re.I | re.S)
    if tagged:
        return tagged.group(1).strip()
    section = split_revised_answer_section(text)
    if section:
        _, body_start, body_end = section
        return revised_answer_body(text[body_start:body_end])
    if re.search(r"(?m)^##\s+(?:总体评分|得分点清单|踩点对比|材料领读|优化建议)\s*$", text):
        return ""
    text = re.sub(r"^```(?:markdown|text)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return revised_answer_body(text)


def replace_revised_answer_body(report_text, answer_body, word_limit=""):
    section = split_revised_answer_section(report_text)
    answer_body = str(answer_body or "").strip()
    if not section or not answer_body:
        return report_text
    _, body_start, body_end = section
    replaced = report_text[:body_start] + "\n\n" + answer_body + "\n\n" + report_text[body_end:]
    return normalize_revised_answer_word_count(replaced, word_limit)


def word_limit_max(value):
    return word_limit_budget(value)["hard_max_exclusive"]


CN_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


COMPREHENSIVE_WRITING_CUES = (
    "写一篇文章",
    "写一篇关于",
    "写一篇议论文",
    "写一篇议论",
    "议论文",
    "议论性文章",
    "讨论性文章",
    "策论文",
    "对策性文章",
    "作文",
    "自拟题目",
    "自选角度",
)


def parse_number(value):
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    if value in CN_NUMBERS:
        return CN_NUMBERS[value]
    if value.startswith("十") and len(value) == 2:
        return 10 + CN_NUMBERS.get(value[1], 0)
    if value.endswith("十") and len(value) == 2:
        return CN_NUMBERS.get(value[0], 0) * 10
    if "十" in value and len(value) == 3:
        return CN_NUMBERS.get(value[0], 0) * 10 + CN_NUMBERS.get(value[2], 0)
    return None


def question_value(question, key, default=""):
    if not question or not hasattr(question, "keys") or key not in question.keys():
        return default
    return question[key] or default


def question_text(question):
    return "\n".join(
        str(question_value(question, key))
        for key in ("prompt", "requirements", "title", "question_type")
    )


def is_comprehensive_writing(question):
    text = re.sub(r"\s+", "", question_text(question))
    return (
        question_value(question, "question_type") == "综合写作"
        or any(cue in text for cue in COMPREHENSIVE_WRITING_CUES)
    )


def should_use_whole_paper_materials(question):
    if not is_comprehensive_writing(question):
        return False
    text = re.sub(r"\s+", "", question_text(question))
    return bool(
        re.search(
            r"(?:参考|结合|依据|根据|围绕)"
            r"(?:全部|所有|上述|以上|整卷|全卷|整篇|全篇)?"
            r"(?:给定)?(?:资料|材料)(?![0-9一二两三四五六七八九十])",
            text,
        )
    )


def referenced_material_numbers(question):
    text = "\n".join(
        str(question_value(question, key))
        for key in ("prompt", "requirements", "title")
    )
    pattern = re.compile(
        r"(?:给定)?(?:资料|材料)\s*([0-9一二两三四五六七八九十]+(?:\s*(?:[-—至到~～、,，和及]\s*)[0-9一二两三四五六七八九十]+)*)"
    )
    number_pattern = re.compile(r"[0-9]+|[一二两三四五六七八九十]+")
    range_pattern = re.compile(
        r"([0-9一二两三四五六七八九十]+)\s*(?:[-—至到~～])\s*([0-9一二两三四五六七八九十]+)"
    )
    numbers = []
    for match in pattern.finditer(text):
        cluster = match.group(1)
        consumed = []
        for range_match in range_pattern.finditer(cluster):
            start = parse_number(range_match.group(1))
            end = parse_number(range_match.group(2))
            if start is not None and end is not None:
                low, high = sorted((start, end))
                numbers.extend(range(low, high + 1))
                consumed.append(range_match.span())
        remainder = list(cluster)
        for start, end in consumed:
            for index in range(start, end):
                remainder[index] = " "
        for number_match in number_pattern.finditer("".join(remainder)):
            number = parse_number(number_match.group(0))
            if number is not None:
                numbers.append(number)
    deduped = []
    for number in numbers:
        if number not in deduped:
            deduped.append(number)
    return deduped


def select_relevant_materials(question, materials):
    if not materials:
        return []
    if should_use_whole_paper_materials(question):
        return list(materials)
    wanted = set(referenced_material_numbers(question))
    if not wanted:
        return list(materials)
    selected = [material for material in materials if material["material_number"] in wanted]
    return selected or list(materials)


REPORT_INSTRUCTIONS = """你是一名严谨的申论批改老师，融合白鹭、小马哥（小题）和袁东（大作文）的解题方法论，以申论命题人和阅卷人的专业视角严格评判。请基于题目、作答要求、整卷材料、用户答案，以及本批改包实际提供的参考答案进行批改。

批改原则：
1. 【错别字免扣分铁律】：考生采用电脑键盘拼音输入法打字作答，同音错别字、输入法联想失误、打字手滑等非原则性错字属正常录入现象，一律免予扣分，严禁以此作为扣分依据！评分的核心在于采分点语义识别与材料提取能力；只要语义表达能够识别出采分点（无论是否存在同音错别字），均须判定为命中给分！
2. 修改版答案必须遵守题目信息中的结构化字数预算。“建议作答区间”只用于指导首轮生成，不是合格下限，也不得为了进入区间而补写套话；“硬限制”才决定能否保存。若标注“低于 N 字”，正文占格必须严格小于 N，等于 N 也算超限。模型填写的字数只作占位，系统会按答题纸规则重新计算并覆盖。
3. 生成修改版答案前，先在内部提炼共性核心采分点并分配表达预算。每个核心点优先保留主体、对象、动作、方式和关键效果；多份参考答案不能简单相加，差异补充点必须服从总预算，不得挤占核心点。
4. 采分始终以给定材料和题目任务为唯一客观真理源。所提供的机构/用户参考答案可能存在主观发挥、套话堆砌或漏点，仅供对比纠错参考，绝非真理依据！AI 必须以自身依据材料原词与 Shenlun.skill 规则独立自主推导的客观得分点为准进行评分。禁止被不准确的参考答案误导，禁止将脱离材料的机构套话立为扣分项。
5. 采分点覆盖法（权威评分规则）：申论踩点加分制——踩到给分，没踩到不给，不存在倒扣分。编造内容该项不得分，不额外倒扣（代价是占用字数与格子空间）。判定命中时：踩到≥80%得满分，40%-80%得一半分，<40%不得分。
6. 判定“漏点”前必须先在用户原答案中查找同义表达、近义表达、主谓宾不同但意思相同的表达。只要用户已经表达了同一材料信息或同一措施方向，应判为“命中/部分命中”，不得把已经写出的原句判为未命中，不得说完全漏写。
7. 对用户答案要严格区分“命中、部分命中、未命中”，并在“用户答案对应内容”列引用原答案中的短句；如果找不到对应短句，才可写“未体现”。
8. 评分要像真实申论批改：重视要点覆盖、材料依据、结构逻辑、表达规范和字数格式。
9. 修改建议要可操作，指出应补、应删、应合并、应规范表达的位置。严格去水，不写“在……过程中”“不断”“进一步”“加大力度”等无采分点废话。合并传统的踩点对比、材料领读与优化建议为整合式名师标杆答案与采分对照。
10. 综合分析题按4子类逻辑组织采分与评判：词句理解类（表层含义→深层内涵→对策实质）、观点评析类（亮明态度→合理性与局限性→结论）、现象分析类（概括现象→深层原因→治理对策）、关系分析类（关系本质→双向互动→每点回扣题干核心词）。
11. 大作文（综合写作）按袁东框架两轮阅卷：第一轮判定主题立意（单主题/双主题AB型/双主题ABC型/多主题，立意是否精准/偏题/跑题）；第二轮五维打分（立意30%+结构20%+论证20%+素材15%+语言15%）与创新加分。
12. 禁止用重复表达、空泛背景、无材料依据的意义、例证和套话凑字数。生成时应保留安全余量，避免答案贴近硬上限后因标点、空格或手动换行超限。
13. 字数统一按考试答题纸占格规则估算：汉字、全角标点每个一格；连续英文、半角数字每两个字符一格；标准“——”“……”整体两格，单独“—”“…”也按两格；空格一格；手动换行立即结算本行剩余格并从下一行开始，纯空白行不占格；不自动添加段首缩进。先写“实际字数：X 字；建议区间：A—B 字；硬限制：低于 N 字；状态：待系统复核”，系统保存报告时会按网格规则重新计算并覆盖状态行。

请按题目类型输出对应的 Markdown 结构：

【对于非大作文小题（归纳概括、综合分析、提出对策、公文写作）】：
## 总体评分
- 总分：X/题目分值或建议分值
- 等级：优秀/良好/一般/较弱
- 综合判断：一句话说明最大得分点和最大失分点

## 采分点证据与整体校准
- 加权踩点覆盖参考值：
- 综合内容分：
- 参考答案使用说明：

## 名师标杆答案与采分对照
AI 依据名师技能（小马哥原词直抄、白鹭近义提炼与轻串联）及给定材料、参考答案，进行名师标答与参考答案对照，先推导出一份高水准修改版答案标杆。系统将把答案中对应各采分点的重点词句以不同颜色呈现（完全得分🟢、部分得分🟡、未得分🔴），并在点击时弹出作答诊断与材料出处。
在答案中使用如下标记语法：
`[标答点|status|score|my_eval|material_source|标答短句]`
- status: hit (完全得分) / partial (部分得分) / miss (未得分)
- score: 得分与分值说明（如：+2分 / 满分2分）
- my_eval: 我是怎么得分的（命中原词依据、扣分原因、缺失要点）
- material_source: 答案来自于材料什么部分（如：材料2第3段：『……』）
- 标答短句: 标杆答案正文中的核心短句

标答正文通常包含以下双流派：
### 小马哥版（极简·原词直抄流）
完全使用材料原词，不自创任何概括词，“材料有什么抄什么”，动宾短语+具体对象+结果成效，单条要点≤40字，严格去水，抗扣分能力最强。

### 白鹭版（近义提炼·轻串联流）
从材料已有词汇中提炼4/6/8字前置概括小标题，逻辑归并3—5条，每条包含“概括词 + 举措展开 -> 成效”，加入少量轻串联使通顺流畅。

【对于大作文（综合写作）】：
## 总体评分
- 总分：X/题目分值
- 等级：优秀/良好/一般/较弱
- 综合判断：

## 采分点证据与整体校准
- 综合内容分：

## 作文逐句精批
对考生作答全文进行逐字逐句的精细化批注（原文可视化批注，支持立意、论点、论据、论证、过渡、语言、亮点、润色、修改、删减、补充、关键）：
- `[亮点|原文短句|为什么有效||positive|]`
- `[类型|原文短句|批注原因|锚点|严重度|建议修改]`

## 名师修改版范文
提供完整高质量的名师修改版大作文范文。

## 机构参考答案对照
客观对比机构参考答案与名师审题逻辑。
"""


def build_grading_package(
    question,
    references,
    attempt=None,
    materials=None,
    custom_reference_answer="",
    grading_basis=None,
):
    attempt_text = attempt["answer_text"] if attempt else ""
    attempt_created = format_beijing_time(attempt["created_at"]) if attempt else "未保存作答"
    budget = word_limit_budget(question["word_limit"] or "")
    suggested_range = (
        f"{budget['suggested_min']}—{budget['suggested_max']}字"
        if budget["suggested_min"]
        else "未标注"
    )
    hard_limit = (
        f"必须低于{budget['hard_max_exclusive']}字"
        if budget["hard_max_exclusive"]
        else "无"
    )
    minimum_requirement = (
        f"不少于{budget['minimum']}字（仅提示，不触发自动补写）"
        if budget["minimum"]
        else "无"
    )
    if materials:
        materials = select_relevant_materials(question, materials)
        material_text = "\n\n".join(
            f"{material['title'] or ('材料' + str(material['material_number']))}\n{material['content']}"
            for material in materials
        )
    else:
        material_text = question["materials"] or "未录入材料原文。"

    parts = [
        "# 申论作答批改包",
        "",
        "## 题目信息",
        f"- 题目编号：{question['question_code']}",
        f"- 考试：{question['year']} {question['region']} {question['exam_type']}",
        f"- 原卷：{question['paper_name'] or '未标注'}",
        f"- 题型：{question['question_type']}",
        f"- 标题：{question['title']}",
        f"- 字数限制：{question['word_limit'] or '未标注'}",
        f"- 建议作答区间：{suggested_range}",
        f"- 硬限制：{hard_limit}",
        f"- 最低要求：{minimum_requirement}",
        f"- 训练优先级：{question['zhejiang_relevance']}/5",
        f"- 原文完整：{'是' if question['is_full_original'] else '否/待校对'}",
    ]

    q_dict = dict(question)
    q_type = q_dict.get("question_type")
    if q_type == "综合分析":
        subtype, guide = classify_analysis_subtype(q_dict.get("prompt"), q_dict.get("requirements"))
        parts.append(f"- 综合分析题型细分：{subtype}（解题逻辑：{guide}）")
    elif q_type == "综合写作":
        theme_type, guide = classify_essay_theme_type(q_dict.get("prompt"), q_dict.get("requirements"))
        parts.append(f"- 综合写作主题分析：{theme_type}（分论点方向：{guide}）")

    parts.extend(
        [
            "",
            "## Shenlun.skill 名师做题法与批改铁律",
            "- 【小马哥极简原词直抄流】：材料有什么抄什么，不自创高级概括词。利用8类信号词锁定材料要点，提取动宾短语+具体对象+成效，单条要点≤40字，严格去水，字数预算85%—95%，高密度踩点。",
            "- 【白鹭规范提炼流】：从材料已有规范词中提炼4/6/8字前置概括小标题，逻辑归并3—5条，结构为“小标题 + 举措展开 -> 成效”，轻串联流畅表达。",
            "- 【错别字免扣分铁律】：考生使用电脑键盘输入法作答，同音错别字、联想误差属正常输入偏差，一律不予扣分，只要语义表达指向采分点即判定命中给分！",
            "- 【名师标答与参考答案对照】：输出时须并列展示【名师标准答案】、【机构参考答案】与【做题逻辑偏差对照审计】，指出机构答案虚浮套话或漏点，说明名师做题逻辑如何规避偏差。",
            "",
            "## 题目",
            question["prompt"],
            "",
            question["requirements"],
            "",
            "## 材料",
            material_text,
            "",
            "## 本次批改参考答案",
            "",
            "参考答案使用规则：多份参考答案只能用于提炼共性核心点与材料依据，不能把不同机构的所有要点机械相加；差异补充点应谨慎作为加分提醒，不能导致修改版答案超出字数上限。",
        ]
    )
    sample_guidance = limited_reference_guidance(len(references))
    if sample_guidance:
        parts.append(sample_guidance)

    if references:
        for index, ref in enumerate(references, start=1):
            parts.extend(
                [
                    "",
                    f"### {index}. {ref['organization']}",
                    ref["answer_text"],
                    "",
                    "采分点：",
                    ref["scoring_points"] or "未提炼",
                ]
            )
    if custom_reference_answer.strip():
        parts.extend(
            [
                "",
                "### 用户补充参考答案",
                custom_reference_answer.strip(),
            ]
        )
    if not references and not custom_reference_answer.strip():
        parts.append("本次未提供参考答案，请仅依据题目、作答要求和材料进行批改。")

    if grading_basis:
        if grading_basis.get("kind") == "cached_rubric":
            parts.extend(["", "## AI 智能评分基准"])
            rubric = grading_basis.get("rubric") or {}
            parts.append("以下为本题在智能批改中生成、已缓存并通过材料引文校验的评分基准；仍须核对用户答案中的同义表达。")
            for index, point in enumerate(rubric.get("points") or [], start=1):
                evidence = "；".join(item.get("quote") or "" for item in point.get("material_evidence") or [])
                parts.append(
                    f"{index}. [{point.get('tier')}] {point.get('label')}：{point.get('canonical_expression')}"
                    f"；材料依据：{evidence or '不足'}；支持机构数：{point.get('support_org_count', 0)}"
                )
        else:
            parts.extend(
                [
                    "",
                    "## 评分依据状态",
                    "本题尚未生成 AI 智能评分基准。本批改包已主动省略未经材料核验的本地分句聚类候选，禁止把相似句或材料片段直接当作采分点。",
                    "请先依据题目任务、作答要求和材料原文，结合所选参考答案，独立提炼 4—12 个有材料依据、能在规定字数内完成的采分点；再逐点核对我的答案。每个扣分点都必须说明对应材料依据，并识别同义或近义表达。",
                ]
            )

    parts.extend(
        [
            "",
            "## 我的答案",
            f"- 作答时间：{attempt_created}",
            f"- 实际占格数：{count_cjk_chars(attempt_text)}",
            "原文命中识别规则：批改踩点前必须先在下方原答案中查找同义或近义表达；若用户已经写出同一意思，请判为命中或部分命中，不要误判为漏点。",
            "",
            attempt_text or "（请在这里粘贴我的答案）",
            "",
            "## 请按以下标准批改",
            REPORT_INSTRUCTIONS,
        ]
    )
    return "\n".join(parts)


def build_ai_prompt(
    question,
    references,
    attempt,
    materials=None,
    custom_reference_answer="",
):
    return build_grading_package(
        question,
        references,
        attempt,
        materials,
        custom_reference_answer,
    )

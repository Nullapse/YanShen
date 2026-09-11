import html as html_lib
import re

from .taxonomy import classify_analysis_subtype, classify_essay_theme_type
from .timeutils import format_beijing_time

ANSWER_GRID_COLUMNS = 25


def limited_reference_guidance(reference_count):
    count = max(0, int(reference_count or 0))
    if count == 1:
        return (
            "唯一参考答案规则：本题只有 1 份粉笔标准答案。非作文题的内容采分点只能从这份答案逐句切分，"
            "不得从材料新增采分点，不得把材料中的地点、案例、政策名或技术名追加为必需条件。"
            "用户表述不要求逐字一致，核心动作、对象或效果同义即应命中全分；概括不全面或不够准确必须按阶梯判定部分得分。"
        )
    if count == 0:
        return "本题未提供参考答案，只能依据题干和材料建立评分基准。"
    if count > 3:
        return ""
    return (
        f"机构参考答案样本提示：本题有 {count} 份机构答案，需结合题干与材料核验共同采分点；"
        "不得把机构答案中无材料依据的发挥设为必得分点。"
    )


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


def normalize_reference_answer_text(text):
    """Decode harmless HTML entities in imported reference answers without rendering HTML."""
    value = html_lib.unescape(str(text or ""))
    return (
        value.replace("\xa0", " ")
        .replace("\u2002", " ")
        .replace("\u2003", " ")
        .replace("\u2007", " ")
        .replace("\u202f", " ")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def strip_master_point_tags(text):
    """Remove report-only scoring metadata while preserving the visible answer clause."""
    if not text:
        return ""
    s = str(text)

    def visible_clause(match):
        payload = match.group(1)
        return payload.rsplit("|", 1)[-1]

    s = re.sub(r"\[(?:标答点|标答标题|作答点)\|([^\]]+)\]", visible_clause, s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    s = s.replace("\u2003", " ").replace("\u2002", " ").replace("\xa0", " ")
    return s


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


def revised_answer_word_count_line(actual_chars, word_limit="", streams=None, is_reference=False):
    budget = word_limit_budget(word_limit)
    if streams and len(streams) >= 2:
        stream_parts = []
        for s in streams:
            short_name = re.sub(r"[（(].*?[）)]", "", s["title"]).strip()
            stream_parts.append(f"{short_name}{s['chars']}字")
        parts = [f"实际字数：{' · '.join(stream_parts)}"]
    else:
        parts = [f"实际字数：{actual_chars}字"]
    if is_reference:
        return "；".join(parts)
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
    match = re.search(
        r"(?m)^##\s*(?:修改版答案|名师修改版范文|名师标杆答案与采分对照|粉笔参考答案与采分对照|.+?参考答案与采分对照)\s*$",
        report_text or "",
    )
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
    is_reference = "参考答案" in heading
    if len(streams) >= 2:
        actual_chars = max(s["chars"] for s in streams)
        over_limit = bool(hard_max and any(s["chars"] >= hard_max for s in streams) and not is_reference)
    else:
        clean_body = strip_master_point_tags(answer_body)
        actual_chars = count_cjk_chars(clean_body)
        over_limit = bool(hard_max and actual_chars >= hard_max and not is_reference)
    normalized_section = (
        heading
        + "\n"
        + revised_answer_word_count_line(actual_chars, word_limit, streams=streams, is_reference=is_reference)
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
    section_start, body_start, body_end = section
    heading = report_text[section_start:body_start]
    answer_body = revised_answer_body(report_text[body_start:body_end])
    streams = extract_revised_answer_streams(answer_body)
    is_reference = "参考答案" in heading
    if len(streams) >= 2:
        actual_chars = max(s["chars"] for s in streams)
        over_by = max(
            (s["chars"] - hard_max + 1 for s in streams if hard_max and s["chars"] >= hard_max),
            default=0,
        ) if not is_reference else 0
    else:
        clean_body = strip_master_point_tags(answer_body)
        actual_chars = count_cjk_chars(clean_body)
        over_by = actual_chars - hard_max + 1 if hard_max and actual_chars >= hard_max and not is_reference else 0
    budget_status = "参考答案" if is_reference else budget_status_label(actual_chars, budget)
    return {
        "has_revised_answer": bool(answer_body),
        "actual_chars": actual_chars,
        "max_chars": hard_max,
        "budget": budget,
        "budget_status": budget_status,
        "over_limit": bool(hard_max and actual_chars >= hard_max and not is_reference),
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


REPORT_INSTRUCTIONS = """你是一名严谨的申论阅卷与诊断老师，不是参考答案作者。请基于题目、作答要求、整卷材料、用户答案和本批改包实际提供的参考答案完成评分与诊断。

批改原则：
1. 对只有一份粉笔答案的非作文题，粉笔答案是内容给分点的唯一来源；题干用于限定任务，材料只用于核验明显事实错误，绝不能新增、扩写或替换粉笔答案中的扣分条件。系统已提供采分点，AI 绝不生成任何答案，只逐点判断用户在这些得分点的得分情况。
2. 禁止生成、改写、压缩、润色或替换参考答案；禁止输出“修改版答案”“名师答案”或任何可直接替换的完整范文。
3. 非作文题采用真实阅卷式划点：以唯一粉笔答案为来源，先识别一级要点组，再合理归并为少量完整给分点；20分题通常5—10点，每点一般1—3分。不得把每个词、地点、案例拆成0.5分碎片，也不得把整组措施捆成一个笼统大点。
4. 判定漏点与得分档位时，严格审查考生作答的“全面性”与“准确性”。不能要求与粉笔答案逐字相同，但必须既全面又准确才判命中满分；若核心意思虽有涉及但概括不全面（遗漏重要要素或成效）或不够准确（表述宽泛、笼统、口语化、提炼偏弱），必须判为部分得分并阶梯赋分，不得直接给满分；完全未提及或答错才判为未命中。
5. 【错别字免扣分铁律】：同音错别字、输入法联想失误和非原则性打字错误不作为扣分依据；语义可识别时正常判分。
6. 【大作文袁东方法论】：大作文（综合写作）严格依据袁东方法论进行审题判断与定级赋分，严禁脱离材料空发议论，审查立意、五段三分结构、材料关键词转化及语言表达。
7. 修改建议必须说明用户答案应补、应删、应合并或应规范的位置，只给局部动作，不输出重写后的完整答案。
8. 字数只统计用户作答纯正文。参考答案、批改标签、得分说明、材料出处和诊断文字均不计入用户答案字数。

请输出 Markdown：
## 总体评分
- 总分：X/题目分值或建议分值
- 等级：优秀/良好/一般/较弱
- 综合判断：一句话说明最大得分点和最大失分点

## 采分点分析
按粉笔答案顺序分组列出：要点组、完整给分点、满分、粉笔依据、用户对应原句、完成档位、实得分和理由。给分点应便于真实阅卷老师快速识别，不机械碎分。

## 用户作答诊断
指出结构、逻辑、表达和字数问题，并引用用户原句。

## 局部修改建议
只列具体修改动作，不提供完整改写答案。

## 参考答案可靠性说明
说明粉笔答案中可能存在的明显事实争议；不得把材料补充内容变成给分点，也不得重复、改写或重新输出参考答案全文。
"""


YUANDONG_ESSAY_METHODOLOGY = """【袁东大作文（综合写作）专业方法论与评审标准】：
核心思想：不是先想“怎么写”，而是先判断“是什么类型”。从题干判断主题类型 → 按类型确定分论点框架 → 从题干和材料填充关键词。分论点的框架是类型决定的，内容是从材料中提取的。

一、审题与主题类型判断（决定分论点推导框架）：
1. 单主题：题干围绕单一核心概念（如“以‘创新驱动发展’为主题”）。
   - 分论点框架：围绕单一主题的多个维度展开——为什么重要（意义）、怎么做（举措）、从哪些方面发力。
2. 双主题：
   - AB型：题干为两个并列/对立/互补概念（如“守正与创新”“变与不变”“有为与不为”“效率与公平”）。
     * 分论点一：A 对 B 的影响/作用/前提（如：守正是创新的前提，守好根本创新才不跑偏）；
     * 分论点二：B 对 A 的影响/作用/保障（如：创新是守正的保障，没有创新守正会僵化失去生命力）；
     * 分论点三：A 和 B 双向奔赴、互相作用产生更大价值，推动事业行稳致远。
   - ABC型：题干出现三个概念，但 A 和 B 共同服务于 C（如“以法治和德治推动社会治理现代化”）。
     * 分论点一：A 对 C 的作用（刚性约束与制度保障）；
     * 分论点二：B 对 C 的作用（柔性引导与价值支撑）；
     * 分论点三：A + B 刚柔并济、协同发力对 C 的整体推动。
3. 多主题：题干出现三个及以上并列概念（如“改革、发展、稳定”）。
   - 分论点框架：强调“组合拳”式整体效应（分论点一：改革是动力；分论点二：发展是目的；分论点三：稳定是前提）。
* 评分依据：严查考生中心论点与分论点是否严格符合所属主题类型的推导逻辑，立意是否切合题意，严禁偏题、跑题或将核心概念割裂。

二、分论点寻找法（三步走原则，严禁脱离材料空发议论）：
1. Step 1 从题干圈定核心词：题干本身已暗示分论点方向，标题与分论点必须直接包含题干关键词；
2. Step 2 从给定材料寻找：找出与题干核心概念相关的段落，提取其中的观点句、典型做法与成效，作为分论点与论据的直接依据；
3. Step 3 从前面小题材料寻找：整套试卷材料内在相通，若大作文给定材料不够支撑，往前面小题材料提取案例与论据，融会贯通。
* 核心动作：始终是“寻找关键词”，不是自己凭空编造。分论点核心术语必须来自题目或材料，严禁通篇脱离材料空发议论。

三、五段三分规范结构与段内四层链条：
1. 标题（1行）：观点式（直接亮明中心论点）、对仗式（两短语对仗）或比喻式，必须直接关联题干核心词。阅卷老师2秒内判断是否偏题。
2. 首段（150-200字，三层递进）：
   - 引出话题（1-2句）：从时代背景或材料现象切入；
   - 点明问题/意义（1-2句）：阐明该话题为什么重要、为何值得探讨；
   - 亮明中心论点（1句）：段尾最后一句话必须旗帜鲜明亮出中心论点，绝不含糊。
3. 分论点一、二、三（各250-300字，字数基本对等）：段内严格遵循四层链条与双线论证：
   - 第①层（1句，段首置顶）：明确提出分论点句，直接亮明本段核心论点；
   - 第②层（2-3句，政策理论线）：转述国家政策精神、时代发展要求，点明与话题关联，不整段抄录，点到为止；
   - 第③层（3-4句，案例分析线）：选取给定材料或前序小题典型案例，有名有姓有细节，结合案例提炼启示，不堆砌琐碎情节；
   - 第④层（1-2句，小结回扣）：总结回扣本段分论点，强化论证闭环。
4. 尾段（100-150字）：
   - 回扣：用新话术重申中心论点（严禁照抄首段原话）；
   - 升华：自然上升到国家发展、人民幸福、社会进步的高度。
   - 禁忌：不得提出新论点，不用“但是”转折，不用“我希望/我相信”等空泛主观口号。

四、双线论述法：
每个分论点段内部坚持“政策理论线 + 案例分析线”双线并行融合：
- 政策理论线：转述政策精神，说明与当前话题的关系；
- 案例分析线：选用典型案例，有名有姓有细节，夹叙夹议，提炼启示；
- 严禁通篇纯理论空谈，亦严禁通篇讲故事无理论高度。

五、模板与创新规则（模板是底线，不是天花板）：
1. 袁东框架是保底工具：按模板写且框架正确，可保底稳在二类文（70分以上）。
2. 超越模板的创新加分情形：
   - 独到的切入角度：如从哲学高度切入（如“守正的本质是对时间的敬畏，创新的本质是对变化的拥抱”），格局更高，予以加分；
   - 新颖的论证结构：若以核心故事贯穿全文层层剥开，只要自洽深刻，不扣结构分，反而加分；
   - 深刻的独立分析：在材料事实基础上独立思考提出深层归因，加分；
   - 辨识度的语言风格：有文采但不浮夸，节奏有力，加分。
3. 评判标准：按模板写结构对=基础分稳；没按模板但逻辑自洽深刻=不扣结构分并额外加分；按模板但内容空洞=指出套路化问题；结构混乱=建议先用模板稳住。

六、粉笔参考答案定位铁律（立意校核）：
1. 【粉笔大作文参考答案仅作立意校核】：大作文不是客观小题！粉笔大作文参考答案仅用于辅助判断考生的中心立意与分论点方向是否正确切题。
2. 【严禁逐句对齐扣分】：严禁把粉笔大作文参考答案当成客观采分点去逐句比对考生作答扣分！
3. 【具体给分以袁东定级为准】：具体评分严格按袁东两轮阅卷五档标准与五维模型执行。

七、两轮阅卷五档定级与赋分标准：
1. 第一轮阅卷：快速浏览标题 + 首段中心论点 + 尾段回扣 + 三个分论点，判定立意档位：
   - 一类文（80—100分）：优秀考场范文。审题精准，立意高远深刻，五段三分严密，双线论证充分展开，紧扣材料关键词，无事实/逻辑硬伤，创新加分。
   - 二类文（70—79分）：良好。明显高于一般水平。切题准确，中心论点鲜明，符合五段三分，立意与论证较强，材料基本准确。
   - 三类文（60—69分）：主体任务成立中上档。立意较好，但存在主要论证偏薄、材料事实不准或套路化空洞。
   - 四类文（50—59分）：基本切题但完成质量一般，论证、材料转化或结构存在多处明显不足。
   - 五类文（50分以下）：偏题、跑题、任务完成严重不充分，或论证结构存在严重缺陷。
2. 第二轮阅卷：五维细评分配分数：
   - 立意准确性（30%）：审题类型精准度、中心论点高度与深度；
   - 结构规范性（20%）：五段三分布局、段内四层链条、首尾呼应；
   - 论证充分性（20%）：双线论述融合度、论证逻辑闭环与创新深度；
   - 素材运用度（15%）：材料关键词转化、典型案例提炼、整卷材料融会；
   - 语言表达力（15%）：政策规范用语、文采与逻辑节奏（键盘错别字免扣分）。

八、修改建议与诊断规范：
1. 修改建议必须指出用户答案应补、应删、应合并或应规范的具体位置与局部动作。
2. 严禁输出重写后的完整答案、修改版范文或替代文章，只给局部批改动作。
3. 字数统计只计算用户作答的纯正文；参考答案、批改标签、材料出处和诊断文字均不计入。
4. 【错别字免扣分铁律】：键盘拼音同音错别字、联想误差一律免扣分，语义可识别即正常判分。"""


REPORT_INSTRUCTIONS_ESSAY = """你是一名严谨的申论大作文阅卷与诊断老师，不是参考答案作者。请基于题目、作答要求、整卷材料、用户作答和参考答案，严格依据【袁东大作文方法论】完成大作文评审与诊断。

核心批改铁律：
1. 【粉笔答案仅作立意校核】：大作文不是客观小题！粉笔参考答案仅用于判断考生的中心立意与分论点是否切题、正确，绝不是客观给分点，严禁把粉笔答案逐句对齐扣分！
2. 【袁东两轮阅卷定级】：必须严格依据袁东方法论（审题三种类型判定、分论点三步走、五段三分四层结构、政策与案例双线论述、创新加分机制），先定整篇档位（一类文80-100/二类文70-79/三类文60-69/四类文50-59/五类文50以下），再分配五维得分。
3. 【禁止生成完整答案】：禁止输出“修改版范文”“名师大作文”或任何替代性完整文章。修改建议只给局部具体动作（应补、应删、应合并、应规范的具体位置），严禁输出重写后的全文。
4. 【素材必须结合材料】：严查分论点与论据是否源自给定材料或前序小题材料，严禁通篇脱离材料空发议论。
5. 【字数与错别字】：字数只统计用户作答纯正文。键盘输入同音错别字、输入法联想失误一律免予扣分，语义可识别时正常评判。

请输出 Markdown：
## 总体评分
- 总分：X/题目分值（如 32/40分）
- 档位等级：一类文（80-100%）/ 二类文（70-79%）/ 三类文（60-69%）/ 四类文（50-59%）/ 五类文（50%以下）
- 综合判断：一句话总结文章最大亮点与最大失分薄弱点

## 袁东审题与立意诊断
- 题型判断：指出题干属于单主题 / 双主题AB型 / 双主题ABC型 / 多主题，并说明判断依据与分论点推导逻辑。
- 中心立意研判：评价文章立意高度与切题深度，是否准确提炼题干核心词，有无偏题或概念割裂。
- 粉笔参考答案立意校核：对照粉笔参考答案的立意方向，校核考生立意与分论点的正确性（粉笔仅作方向参考，绝非客观扣分点）。

## 五段三分与双线论证分析
- 标题分析：评价标题类型（观点式/对仗式/比喻式），是否关联核心词，2秒内能否判断切题。
- 首段分析：审查三层递进链条（引出话题1-2句 -> 点明问题/意义1-2句 -> 亮明中心论点1句）。
- 分论点与段内四层结构诊断（逐段审查）：
  * 分论点一：段首论点句、政策理论线（政策转述）、案例分析线（有名有姓有细节、紧扣材料）、小结回扣。
  * 分论点二：段首论点句、政策理论线、案例分析线、小结回扣。
  * 分论点三：段首论点句、政策理论线、案例分析线、小结回扣。
- 尾段分析：是否用新话术回扣中心论点，是否自然升华到国家发展人民幸福（有无转折/新论点/主观口号）。
- 创新亮点加分判定：审查是否存在独到切入（如哲学高度）、新颖自洽结构、深层独立归因或有辨识度文采（有则明确加分）。

## 维度得分与扣分明细
- 立意准确性（30%）：X分 / 满分X分。具体理由。
- 结构规范性（20%）：X分 / 满分X分。具体理由。
- 论证充分性（20%）：X分 / 满分X分。具体理由。
- 素材运用度（15%）：X分 / 满分X分。具体理由。
- 语言表达力（15%）：X分 / 满分X分。具体理由。

## 局部修改建议
指出用户答案中应补、应删、应合并或应规范的具体位置与局部动作。只给局部修改动作，严禁输出重写后的完整文章。

## 参考答案立意校核说明
说明粉笔参考答案的核心立意与构思方向，对比考生作答的契合度与差异点。
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
    is_essay = q_type == "综合写作"
    if q_type == "综合分析":
        subtype, guide = classify_analysis_subtype(q_dict.get("prompt"), q_dict.get("requirements"))
        parts.append(f"- 综合分析题型细分：{subtype}（解题逻辑：{guide}）")
    elif is_essay:
        theme_type, guide = classify_essay_theme_type(q_dict.get("prompt"), q_dict.get("requirements"))
        parts.append(f"- 综合写作主题分析：{theme_type}（分论点方向：{guide}）")

    if is_essay:
        parts.extend(
            [
                "",
                "## Shenlun.skill 袁东大作文方法论与评审铁律",
                YUANDONG_ESSAY_METHODOLOGY,
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
                "参考答案使用规则：【粉笔大作文参考答案仅作立意校核依据】。大作文不是客观小题，粉笔参考答案仅用于辅助判断考生的中心立意与分论点是否切题、正确，绝不是客观给分点，严禁机械对齐逐句扣分！具体评分严格依据袁东两轮阅卷定级赋分标准执行。AI 绝不生成任何完整范文或修改版答案。",
            ]
        )
    else:
        parts.extend(
            [
                "",
                "## Shenlun.skill 名师做题法与批改铁律",
                "- 【AI 仅评分诊断】：系统或参考答案已提供采分点及分值，AI 绝不生成任何完整答案，只逐点判断用户在各采分点的得分情况。",
                "- 【全面准确阶梯给分】：既全面又准确才判命中满分。若概括不全面或用词不够准确规范，必须判为部分得分并梯度赋分，绝不机械苛求字面原词一致，但也绝不把粗糙沾边放水为满分。",
                "- 【大作文袁东方法论】：综合写作严格依据袁东方法论，审查立意深度、五段三分论点结构、材料关键词转化与四维度定级赋分，严禁脱离材料空发议论。",
                "- 【错别字免扣分铁律】：考生使用电脑键盘输入法作答，同音错别字、联想误差属正常输入偏差，一律不予扣分，只要语义表达指向采分点即判定命中给分！",
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
                "参考答案使用规则：系统已提供明确的采分点和分值，AI 只逐点判断用户在这些得分点的得分情况。AI 绝不生成任何答案或改写完整答案。",
            ]
        )
    sample_guidance = limited_reference_guidance(len(references))
    if sample_guidance:
        parts.append(sample_guidance)

    if references:
        for index, ref in enumerate(references, start=1):
            ref_header = f"### {index}. {ref['organization']}"
            if is_essay and "粉笔" in ref["organization"]:
                ref_header += "（仅用于立意与论点切题度校核，非客观采分点）"
            parts.extend(
                [
                    "",
                    ref_header,
                    ref["answer_text"],
                    "",
                    "采分点/论点提炼：" if is_essay else "采分点：",
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
        if is_essay:
            parts.extend(
                [
                    "",
                    "## 袁东大作文核心立意与论据考查基准",
                    "以下基准为大作文核心立意与材料论据考查参考；评分重点在于立意高度、五段三分结构完整性与双线论证深度，严禁当作客观细分采分点逐句扣分。",
                ]
            )
            rubric = grading_basis.get("rubric") or {}
            for index, point in enumerate(rubric.get("points") or [], start=1):
                label = point.get("label") or point.get("canonical_expression")
                parts.append(f"{index}. 核心要素/论点：{label}")
        elif grading_basis.get("kind") in {"cached_rubric", "fenbi_tree"}:
            rubric = grading_basis.get("rubric") or {}
            if rubric.get("scoring_mode") == "fenbi_tree" or rubric.get("source") == "fenbi_score_tree":
                parts.extend(["", "## 粉笔踩分树（固定评分标准）"])
                parts.append("以下踩分树来自粉笔得分详情，已作为唯一评分标准写入系统；AI 只逐点判断，不重新划点或重算权重。")
                for index, point in enumerate(rubric.get("points") or [], start=1):
                    score = point.get("display_weight", point.get("weight"))
                    parts.append(
                        f"{index}. {point.get('label')}（{score}分）"
                    )
                    if point.get("reference_quote"):
                        parts.append(f"   粉笔依据：{point['reference_quote']}")
                    if point.get("source_comment"):
                        parts.append(f"   判分说明：{point['source_comment']}")
            else:
                parts.extend(["", "## AI 智能评分基准"])
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
                    "请以唯一粉笔参考答案为答案来源，按照其一级结构合理归并为少量完整给分点；20分题通常5—10点，每点一般1—3分。用户核心动作、对象或效果同义即可得分，不要求逐字一致；粉笔答案没有写出的材料细节不得成为扣分条件。",
                ]
            )

    parts.extend(
        [
            "",
            "## 我的答案",
            f"- 作答时间：{attempt_created}",
            f"- 实际占格数：{count_cjk_chars(attempt_text)}",
            "原文命中识别规则：批改踩点前必须先在下方原答案中查找同义或近义表达；若用户已经写出同一意思，请判为命中或部分命中，不要误判为漏点。" if not is_essay else "正文字数规则：仅统计用户作答纯正文字数。键盘输入同音错别字免扣分，语义可识别即正常判分。",
            "",
            attempt_text or "（请在这里粘贴我的答案）",
            "",
            "## 请按以下标准批改",
            REPORT_INSTRUCTIONS_ESSAY if is_essay else REPORT_INSTRUCTIONS,
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

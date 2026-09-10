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
            "用户表述不要求逐字一致，核心动作、对象或效果同义即应命中全分。"
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
    def visible_clause(match):
        payload = match.group(1)
        return payload.rsplit("|", 1)[-1]

    return re.sub(r"\[(?:标答点|标答标题)\|([^\]]+)\]", visible_clause, str(text or ""))


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


REPORT_INSTRUCTIONS = """你是一名严谨的申论阅卷与诊断老师，不是参考答案作者。请基于题目、作答要求、整卷材料、用户答案和本批改包实际提供的参考答案完成评分与诊断。

批改原则：
1. 对只有一份粉笔答案的非作文题，粉笔答案是内容给分点的唯一来源；题干用于限定任务，材料只用于核验明显事实错误，绝不能新增、扩写或替换粉笔答案中的扣分条件。
2. 白鹭和小马哥的方法只用于拆分采分点、归并同类信息、识别材料原词、动宾要义、主体对象及同义表达，严禁据此生成任何完整答案。
3. 禁止生成、改写、压缩、润色或替换参考答案；禁止输出“修改版答案”“名师答案”“小马哥版”“白鹭版”或任何可直接替换的完整范文。
4. 非作文题采用真实阅卷式划点：以唯一粉笔答案为来源，先识别一级要点组，再合理归并为少量完整给分点；20分题通常5—10点，每点一般1—3分。不得把每个词、地点、案例拆成0.5分碎片，也不得把整组措施捆成一个笼统大点。
5. 判定漏点前必须在用户原答案中寻找同义、近义和句式不同但机制一致的表达。核心动作、对象、方式或效果一致时，应判为命中或部分命中，不能要求与粉笔答案逐字相同。
6. 【错别字免扣分铁律】：同音错别字、输入法联想失误和非原则性打字错误不作为扣分依据；语义可识别时正常判分。
7. 粉笔答案可能存在局限，可在“参考答案可靠性说明”中提示争议，但不得把粉笔未写的材料内容计分或扣分；若发现粉笔与材料存在明显事实冲突，应拒绝给出数值分并说明原因。
8. 修改建议必须说明用户答案应补、应删、应合并或应规范的位置，只给局部动作，不输出重写后的完整答案。
9. 综合分析题按题型逻辑判断；大作文按立意、结构、论证、素材和语言综合评分。
10. 字数只统计用户作答纯正文。参考答案、批改标签、得分说明、材料出处和诊断文字均不计入用户答案字数。

请输出 Markdown：
## 总体评分
- 总分：X/题目分值或建议分值
- 等级：优秀/良好/一般/较弱
- 综合判断：一句话说明最大得分点和最大失分点

## 采分点分析
按粉笔答案顺序分组列出：要点组、完整给分点、满分、粉笔依据、用户对应原句、完成档位、实得分和理由。给分点应便于真实阅卷老师快速识别，不机械碎分。

## 用户作答诊断
指出结构、逻辑、表达、冗余和字数问题，并引用用户原句。

## 局部修改建议
只列具体修改动作，不提供完整改写答案。

## 参考答案可靠性说明
说明粉笔答案中可能存在的明显事实争议；不得把材料补充内容变成给分点，也不得重复、改写或重新输出参考答案全文。
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
            "- 【AI 仅评分诊断】：唯一粉笔答案是内容给分点来源；题干限定任务，材料只核验明显事实错误。白鹭和小马哥只用于拆点、归并和同义识别，AI 禁止生成、改写或压缩完整答案。",
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
            "参考答案使用规则：只有一份粉笔答案时，内容给分点只能从该答案合理划分；题干限定任务，材料仅核验明显事实错误，不新增扣分条件。AI 只做采分点判断和局部诊断，不得生成或改写完整答案。",
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
                    "请以唯一粉笔参考答案为答案来源，按照其一级结构合理归并为少量完整给分点；20分题通常5—10点，每点一般1—3分。用户核心动作、对象或效果同义即可得分，不要求逐字一致；粉笔答案没有写出的材料细节不得成为扣分条件。",
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

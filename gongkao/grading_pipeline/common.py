import json
import re
from hashlib import sha256


def _extract_matching_quote(quote, reason, answer_text):
    if not answer_text:
        return ""
    clean_answer = str(answer_text).strip()
    clean_quote = str(quote or "").strip()
    if clean_quote and clean_quote in clean_answer:
        return clean_quote

    clauses = [c.strip() for c in re.split(r"[。；;！!\n]", clean_answer) if c.strip()]
    if not clauses:
        clauses = [clean_answer]

    target = clean_quote or str(reason or "").strip()
    target_chars = re.sub(r"[^\w\u4e00-\u9fa5]", "", target)
    if not target_chars:
        return ""

    best_clause = ""
    best_score = 0
    for clause in clauses:
        clause_chars = re.sub(r"[^\w\u4e00-\u9fa5]", "", clause)
        if not clause_chars:
            continue
        common = sum(min(target_chars.count(ch), clause_chars.count(ch)) for ch in set(target_chars))
        score = common / max(len(target_chars), 1)
        if score > best_score:
            best_score = score
            best_clause = clause

    if best_score >= 0.35:
        return best_clause
    return ""


RUBRIC_VERSION = "rubric-v4"

RESULT_VERSION = "grading-result-v5"

PIPELINE_VERSION = "smart-grading-v4"

CONSENSUS_MAX_MATERIAL_CLAUSES = 240

QUESTION_TYPE_PROFILES = {
    "归纳概括": {"content": 70, "structure": 15, "expression": 10, "format": 5},
    "综合分析": {"content": 55, "reasoning": 25, "structure": 10, "expression": 10},
    "提出对策": {"content": 60, "feasibility": 20, "structure": 10, "expression": 10},
    "公文写作": {"content": 50, "format": 20, "structure": 20, "expression": 10},
    "综合写作": {"content": 40, "reasoning": 25, "structure": 20, "expression": 10, "format": 5},
}

CRITERION_LABELS = {
    "content": "围绕题目任务准确、完整地使用材料信息，重点突出且无明显事实偏差",
    "structure": "结构层次符合题目任务，分点或段落组织清楚",
    "expression": "表达准确、规范、简洁，避免歧义和重复",
    "format": "文种、身份、称谓、落款和字数格式符合要求",
    "reasoning": "论点、论据与论证关系完整，材料转化合理",
    "feasibility": "对策回应问题，主体、对象和措施明确，具有针对性与可执行性",
}

QUESTION_TYPE_MODULES = {
    "归纳概括": "summary",
    "综合分析": "analysis",
    "提出对策": "countermeasure",
    "公文写作": "document",
    "综合写作": "essay",
}

_NO_REFERENCE_CLAIMS = (
    "无参考答案",
    "无额外参考答案",
    "没有参考答案",
    "没有额外参考答案",
    "未提供参考答案",
    "未提供额外参考答案",
    "本题无参考答案",
    "本题无额外参考答案",
)

ANSWER_GRID_RULES = """考试答题纸占格规则：
- 汉字、全角标点每个占1格。
- 连续英文、半角数字每2个字符占1格，奇数个向上取整。
- 标准破折号“——”、省略号“……”整体占2格；单独“—”或“…”也占2格。
- 空格占1格。
- 手动换行会立即结算当前行，未用完的格子也会占用版面，下一段从新行开始；纯空白行不计行数。
- 不自动增加段首两格，只有实际输入的空格才计格。"""


def _word_budget_guidance(budget):
    suggested_min = int(budget.get("suggested_min") or 0)
    suggested_max = int(budget.get("suggested_max") or 0)
    hard_max = int(budget.get("hard_max_exclusive") or 0)
    minimum = int(budget.get("minimum") or 0)
    parts = []
    if suggested_min and suggested_max:
        parts.append(f"修改版答案目标为 {suggested_min}—{suggested_max} 格")
    if hard_max:
        parts.append(f"最终结果必须严格低于 {hard_max} 格，等于 {hard_max} 也超限")
    elif minimum:
        parts.append(f"题目要求不少于 {minimum} 格")
    if not parts:
        parts.append("题目没有可执行的硬性占格限制")
    return "；".join(parts) + "。"


def _row_dict(row):
    return dict(row) if row is not None else {}


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value):
    return sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _clean(value, limit=0):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _canonical_organization(reference):
    return _clean(reference.get("canonical_organization") or reference.get("organization"))


def _full_reference_context(references):
    return [
        {
            "reference_id": int(reference["id"]),
            "organization": _canonical_organization(reference),
            "answer_text": str(reference.get("answer_text") or "").strip(),
            "scoring_points": str(reference.get("scoring_points") or "").strip(),
            "notes": str(reference.get("notes") or "").strip(),
        }
        for reference in dedupe_references(references)
    ]


def question_display_max_score(question):
    """Return the question's original point scale, keeping 100 as a safe fallback."""
    texts = [
        str(question.get("prompt") or ""),
        str(question.get("requirements") or ""),
        str(question.get("title") or ""),
        str(question.get("original_text") or ""),
    ]
    patterns = (
        re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]"),
        re.compile(r"(?:本题|该题|满分|分值)\s*(?:为|是|[:：])?\s*(\d+(?:\.\d+)?)\s*分"),
    )
    for pattern in patterns:
        for text in texts:
            match = pattern.search(text)
            if not match:
                continue
            value = float(match.group(1))
            if 0 < value <= 100:
                return int(value) if value.is_integer() else value
    return 100


def question_score_is_estimated(question):
    return question_display_max_score(question) == 100 and not any(
        re.search(r"[（(]\s*100(?:\.0+)?\s*分\s*[）)]", str(question.get(key) or ""))
        for key in ("prompt", "requirements", "title", "original_text")
    )


def _round_half(value):
    return round(float(value or 0) * 2) / 2


def _format_score(value):
    return f"{round(float(value or 0), 1):g}"


def dedupe_references(references):
    output = []
    seen = set()
    for raw in references or []:
        reference = _row_dict(raw)
        organization = _canonical_organization(reference)
        if not organization or organization in seen:
            continue
        seen.add(organization)
        reference["canonical_organization"] = organization
        output.append(reference)
    return output


def reference_set_hash(references):
    return _hash(
        [
            {
                "id": reference.get("id"),
                "organization": _canonical_organization(reference),
                "answer_text": reference.get("answer_text") or "",
            }
            for reference in dedupe_references(references)
        ]
    )


def rubric_source_hash(question, materials, references):
    return _hash(
        {
            "rubric_version": RUBRIC_VERSION,
            "question": {
                "id": question.get("id"),
                "type": question.get("question_type"),
                "prompt": question.get("prompt"),
                "requirements": question.get("requirements"),
                "word_limit": question.get("word_limit"),
            },
            "materials": [
                {
                    "number": material.get("material_number"),
                    "title": material.get("title"),
                    "content": material.get("content"),
                }
                for material in materials
            ],
            "references": [
                {
                    "id": reference.get("id"),
                    "organization": _canonical_organization(reference),
                    "answer_text": reference.get("answer_text"),
                }
                for reference in dedupe_references(references)
            ],
        }
    )


def grading_input_hash(attempt, reference_ids, custom_answer, options, model=""):
    return _hash(
        {
            "attempt_id": attempt.get("id"),
            "answer": attempt.get("answer_text"),
            "reference_ids": sorted(int(value) for value in reference_ids),
            "custom_answer": custom_answer or "",
            "options": options,
            "model": model,
            "pipeline": PIPELINE_VERSION,
        }
    )


def split_semantic_clauses(text, minimum=12, maximum=100):
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw_parts = re.split(r"(?:\n+|(?<=[。！？；]))|(?:^|\s)[一二三四五六七八九十0-9]+[、.．]", normalized)
    output = []
    for part in raw_parts:
        part = _clean(part)
        if not part:
            continue
        if len(part) <= maximum:
            if len(part) >= minimum:
                output.append(part)
            continue
        for start in range(0, len(part), maximum):
            item = part[start : start + maximum].strip()
            if len(item) >= minimum:
                output.append(item)
    return output

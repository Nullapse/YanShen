import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gongkao.db import connect, init_db  # noqa: E402


DB_PATH = ROOT / "instance" / "gongkao.sqlite3"
MANIFEST = ROOT / "source_docs" / "manifest.json"

SKIP_NAME_WORDS = ["笔记", "板书", "解析", "参考答案", "范文", "规范词", "高分范文"]
QUESTION_NAME_WORDS = ["用题", "题本", "作业", "真题实战", "刷题", "公文写作", "分析题", "提出对策", "概括题", "大作文"]
CATEGORY_PATTERNS = ["行政执法", "地市级", "副省级", "省级", "市县级", "乡镇", "基层", "A卷", "B卷", "C卷", "综合写作"]
QUESTION_PATTERNS = [
    r"根据给定资料[^。\n]{0,120}[。？?]",
    r"请(?:根据|结合|围绕|谈谈|分析|概括|归纳|提出|写|拟写|撰写)[^。\n]{4,180}[。？?]",
    r"假如你是[^。\n]{4,180}[。？?]",
]

NOISE_LINE_PATTERNS = [
    r"袁东老师微信号[:：]?\s*Yuandong2238",
    r"Yuandong2238",
    r"微信号",
    r"加微信",
    r"扫码",
    r"领取资料",
    r"课程咨询",
    r"公众号",
    r"超大杯",
]

MOJIBAKE_CHARS = set("粣憔斶韙鬞鞟炪鋢刞趇璖骀鮉惽鐧楣鏉愭枡")
TYPE_HINTS = [
    ("概括", "概括归纳"),
    ("归纳", "概括归纳"),
    ("启示", "启示分析"),
    ("理解", "词句理解"),
    ("分析", "综合分析"),
    ("对策", "提出对策"),
    ("建议", "提出对策"),
    ("公开信", "公文写作"),
    ("发言", "公文写作"),
    ("宣传", "公文写作"),
    ("简报", "公文写作"),
    ("报告", "公文写作"),
    ("编者按", "公文写作"),
    ("作文", "文章写作"),
]


def normalize(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def unwrap_pdf_lines(text):
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    output = []
    hard_start = re.compile(r"^(?:材料|资料)\s*[一二三四五六七八九十\d]+[：:、.\s]|^要求[:：]|^[●◆◇■]|^\d+[、.．]")
    sentence_end = tuple("。！？；：”’）】》)")
    for line in lines:
        if not line:
            if output and output[-1]:
                output.append("")
            continue
        if not output or output[-1] == "" or hard_start.search(line):
            output.append(line)
            continue
        previous = output[-1]
        if previous.endswith(sentence_end):
            output.append(line)
        else:
            glue = "" if re.search(r"[\u4e00-\u9fff]$", previous) and re.match(r"^[\u4e00-\u9fff“”‘’]", line) else " "
            output[-1] = previous + glue + line
    return normalize("\n".join(output))


def strip_noise(text):
    lines = []
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned:
            lines.append(line)
            continue
        if any(re.search(pattern, cleaned, re.I) for pattern in NOISE_LINE_PATTERNS):
            continue
        lines.append(line)
    return normalize("\n".join(lines))


def is_mojibake(text):
    sample = text[:3000]
    if not sample:
        return True
    chinese = len(re.findall(r"[\u4e00-\u9fff]", sample))
    mojibake = sum(1 for char in sample if char in MOJIBAKE_CHARS)
    weird = len(re.findall(r"[�\ue000-\uf8ff]", sample))
    if weird >= 3:
        return True
    return chinese > 0 and mojibake / max(chinese, 1) > 0.18


def extract_docx(path):
    from docx import Document

    doc = Document(path)
    return normalize("\n".join(p.text for p in doc.paragraphs if p.text.strip()))


def extract_pdf(path):
    import pdfplumber

    chunks = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            chunks.append(page.extract_text() or "")
    return unwrap_pdf_lines("\n".join(chunks))


def extract_doc_rough(path):
    return ""


def extract_text(path):
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".doc":
        return extract_doc_rough(path)
    return ""


def infer_category(text, name):
    merged = name + "\n" + text[:2000]
    for item in CATEGORY_PATTERNS:
        if item in merged:
            return item
    return ""


def infer_exam(text, name):
    merged = name + "\n" + text[:3000]
    if "浙江选调" in merged or "选调" in merged:
        return "浙江选调", "浙江", "浙江", 5
    if "浙江" in merged:
        return "浙江省考", "浙江", "浙江", 5
    if "国考" in merged or "国家公务员" in merged or "国家公考" in merged:
        return "国考", "全国", "全国", 3
    if "江苏" in merged:
        return "江苏省考", "江苏", "江苏", 4
    if "上海" in merged:
        return "上海市考", "上海", "上海", 4
    if "山东" in merged:
        return "山东省考", "山东", "山东", 4
    return "真题待核对", "待核对", "待核对", 3


def infer_year(text, name):
    merged = name + "\n" + text[:2000]
    matches = re.findall(r"20(?:2[0-6]|1[0-9])", merged)
    return int(matches[0]) if matches else 2026


def infer_type(text, name):
    merged = name + "\n" + text[:2000]
    for key, value in TYPE_HINTS:
        if key in merged:
            return value
    return "申论题"


def is_question_file(item, text):
    marker = item["name"] + item["section"]
    if any(word in marker for word in SKIP_NAME_WORDS):
        return False
    if any(word in marker for word in QUESTION_NAME_WORDS):
        return True
    return find_prompt(text) != ""


def trim_before_answer(text):
    stops = []
    for pattern in [
        r"\n\s*(?:参考答案|答案解析|作答思路|解题思路|课堂笔记|老师解析|范文)[：:\s]",
        r"\n\s*(?:【参考答案】|【答案解析】|【作答思路】)",
    ]:
        stops.extend(m.start() for m in re.finditer(pattern, text))
    return normalize(text[: min(stops)] if stops else text)


def find_prompt(text):
    for pattern in QUESTION_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(0).strip()
    for line in [line.strip() for line in text.splitlines() if line.strip()]:
        if 18 <= len(line) <= 220 and ("要求" not in line) and any(token in line for token in ["请", "根据", "谈谈", "概括", "分析", "提出", "拟写"]):
            return line
    return ""


def find_requirements(text):
    match = re.search(r"要求[:：]?\s*([^\n]{4,160})", text)
    if match:
        return "要求：" + match.group(1).strip()
    match = re.search(r"不超过\s*\d+\s*字", text)
    return match.group(0) if match else "要求待校对"


def chinese_number(value):
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return int(value) if value.isdigit() else digits.get(value, 1)


def split_materials(text):
    markers = list(re.finditer(r"(?:材料|资料)\s*([一二三四五六七八九十\d]+)[：:、.\s]", text))
    result = []
    for index, marker in enumerate(markers):
        start = marker.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        content = text[start:end].strip()
        if content:
            result.append((chinese_number(marker.group(1)), content))
    return result


def content_hash(prompt, materials, requirements):
    raw = re.sub(r"\s+", "", "\n".join([prompt, materials, requirements]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def paper_code(year, exam_type, region, paper_name, category):
    raw = f"{year}-{exam_type}-{region}-{paper_name}-{category}"
    return "P-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def question_code(provider, source_name, index, h):
    prefix = "BL" if "白鹭" in provider else "YD" if "袁东" in provider else "SRC"
    return f"{prefix}-{h[:12]}-{index}"


def title_from_item(item, prompt):
    cleaned = re.sub(r"\.(docx?|pdf)$", "", item["name"], flags=re.I).strip()
    return cleaned or prompt[:32]


def upsert_paper(conn, year, exam_type, region, province, paper_name, category, relevance, note):
    code = paper_code(year, exam_type, region, paper_name, category)
    conn.execute(
        """
        INSERT INTO papers (
            paper_code, paper_name, paper_category, exam_type, year,
            region, source_province, zhejiang_relevance, source_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_code) DO UPDATE SET
            paper_name = excluded.paper_name,
            paper_category = excluded.paper_category,
            updated_at = CURRENT_TIMESTAMP
        """,
        (code, paper_name, category, exam_type, year, region, province, relevance, note),
    )
    return conn.execute("SELECT id FROM papers WHERE paper_code = ?", (code,)).fetchone()["id"]


def upsert_question(conn, item, raw_text, index=1):
    text = strip_noise(trim_before_answer(raw_text))
    if is_mojibake(text):
        return None, False, "skipped_mojibake"
    if not is_question_file(item, text):
        return None, False, "skipped_non_question"

    provider = item["provider"]
    name = item["name"]
    year = infer_year(text, name)
    exam_type, region, province, relevance = infer_exam(text, name)
    category = infer_category(text, name)
    qtype = infer_type(text, name)
    prompt = find_prompt(text) or title_from_item(item, "")
    requirements = find_requirements(text)
    materials_list = split_materials(text)
    materials = "\n\n".join(f"材料{num}：{content}" for num, content in materials_list)
    h = content_hash(prompt, materials, requirements)
    title = title_from_item(item, prompt)

    if exam_type == "真题待核对":
        paper_name = f"{year}真题出处待核对{('（' + category + '）') if category else ''}"
        note = "待联网核对真实考试出处；来源机构只作标签"
    else:
        paper_name = f"{year}{exam_type}申论{('（' + category + '）') if category else ''}真题"
        note = "来源机构只作标签"
    paper_id = upsert_paper(conn, year, exam_type, region, province, paper_name, category, relevance, note)

    existing = conn.execute("SELECT id FROM questions WHERE content_hash = ?", (h,)).fetchone()
    if existing:
        question_id = existing["id"]
    else:
        code = question_code(provider, name, index, h)
        conn.execute(
            """
            INSERT INTO questions (
                question_code, paper_id, paper_name, paper_category, question_number,
                exam_type, year, region, source_province, zhejiang_relevance,
                question_type, title, prompt, original_text, materials, requirements,
                word_limit, source_kind, is_full_original, content_hash, source_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                paper_id,
                paper_name,
                category,
                index,
                exam_type,
                year,
                region,
                province,
                relevance,
                qtype,
                title,
                prompt,
                "",
                materials,
                requirements,
                "",
                "课程讲义",
                1 if materials_list else 0,
                h,
                note,
            ),
        )
        question_id = conn.execute("SELECT id FROM questions WHERE question_code = ?", (code,)).fetchone()["id"]

    for number, content in materials_list:
        conn.execute(
            """
            INSERT INTO paper_materials (paper_id, material_number, title, content)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(paper_id, material_number) DO UPDATE SET
                content = excluded.content,
                updated_at = CURRENT_TIMESTAMP
            """,
            (paper_id, number, f"材料{number}", content),
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO question_sources (
            question_id, provider, source_name, source_path, section
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (question_id, provider, name, item["original"], item["section"]),
    )
    return question_id, bool(existing), "ok"


def main():
    init_db(DB_PATH)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    imported = 0
    deduped = 0
    skipped = 0
    skipped_files = []
    skipped_mojibake = 0
    skipped_mojibake_files = []
    failed = []
    pending_origin = 0
    with connect(DB_PATH) as conn:
        for item in manifest:
            path = ROOT / item["local"]
            if path.suffix.lower() not in {".doc", ".docx", ".pdf"}:
                continue
            try:
                text = extract_text(path)
                if len(text) < 40:
                    failed.append({"file": item["original"], "reason": "文本过短或无法解析"})
                    continue
                result = upsert_question(conn, item, text)
                if result[2] == "skipped_non_question":
                    skipped += 1
                    skipped_files.append(item["original"])
                    continue
                if result[2] == "skipped_mojibake":
                    skipped_mojibake += 1
                    skipped_mojibake_files.append(item["original"])
                    continue
                question_id, existed, _ = result
                question = conn.execute("SELECT exam_type FROM questions WHERE id = ?", (question_id,)).fetchone()
                if question and question["exam_type"] == "真题待核对":
                    pending_origin += 1
                if existed:
                    deduped += 1
                else:
                    imported += 1
            except Exception as exc:
                failed.append({"file": item["original"], "reason": repr(exc)})
    report = {
        "imported": imported,
        "deduped_sources": deduped,
        "pending_origin_questions": pending_origin,
        "skipped_non_questions": skipped,
        "skipped_mojibake": skipped_mojibake,
        "skipped_non_question_files": skipped_files,
        "skipped_mojibake_files": skipped_mojibake_files,
        "failed": failed,
    }
    (ROOT / "source_docs" / "extract_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

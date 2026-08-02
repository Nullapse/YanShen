import csv
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .organizations import canonicalize_organization
from .taxonomy import classify_question_type

QUESTION_ALIASES = {
    "question_code": ["题目编号", "编号", "question_code", "code"],
    "paper_name": ["原卷名称", "试卷名称", "卷名", "paper_name"],
    "paper_category": ["卷种", "卷子类别", "试卷类别", "paper_category"],
    "question_number": ["题号", "第几题", "question_number"],
    "exam_type": ["考试类型", "exam_type"],
    "year": ["年份", "year"],
    "region": ["地区", "region"],
    "source_province": ["来源省份", "source_province", "province"],
    "zhejiang_relevance": ["训练优先级", "适用权重", "zhejiang_relevance"],
    "question_type": ["题型", "question_type"],
    "title": ["标题", "题目标题", "title"],
    "prompt": ["题干", "设问", "prompt"],
    "original_text": ["题目原文", "完整原文", "原文", "original_text"],
    "materials": ["材料", "materials"],
    "requirements": ["作答要求", "要求", "requirements"],
    "word_limit": ["字数限制", "字数", "word_limit"],
    "source_url": ["来源链接", "来源URL", "source_url", "url"],
    "source_kind": ["来源类型", "source_kind"],
    "is_full_original": ["是否完整原文", "原文完整", "is_full_original"],
    "source_note": ["来源备注", "备注", "source_note"],
}

ANSWER_ALIASES = {
    "question_code": ["题目编号", "编号", "question_code", "code"],
    "organization": ["机构名", "机构", "organization"],
    "answer_text": ["参考答案原文", "参考答案", "answer_text"],
    "scoring_points": ["采分点", "scoring_points"],
    "notes": ["备注", "notes"],
    "is_reviewed": ["是否已校对", "is_reviewed"],
}

QUESTION_REQUIRED = ["question_code", "exam_type", "year", "region", "question_type", "title", "prompt", "materials", "requirements"]
ANSWER_REQUIRED = ["question_code", "organization", "answer_text"]


def _read_upload(file_storage):
    if hasattr(file_storage, "file"):
        return file_storage.file.read()
    if hasattr(file_storage, "read"):
        return file_storage.read()
    raise TypeError("Unsupported upload object")


def _cell_ref_to_index(ref):
    letters = re.sub(r"[^A-Z]", "", ref.upper())
    value = 0
    for letter in letters:
        value = value * 26 + (ord(letter) - ord("A") + 1)
    return max(0, value - 1)


def _read_xlsx(raw):
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("x:si", ns):
                text = "".join(node.text or "" for node in item.findall(".//x:t", ns))
                shared.append(text)

        sheet_name = "xl/worksheets/sheet1.xml"
        root = ET.fromstring(zf.read(sheet_name))
        matrix = []
        for row in root.findall(".//x:sheetData/x:row", ns):
            values = []
            for cell in row.findall("x:c", ns):
                index = _cell_ref_to_index(cell.attrib.get("r", "A1"))
                while len(values) <= index:
                    values.append("")
                value_node = cell.find("x:v", ns)
                inline_node = cell.find("x:is/x:t", ns)
                value = ""
                if inline_node is not None and inline_node.text:
                    value = inline_node.text
                elif value_node is not None and value_node.text is not None:
                    value = value_node.text
                    if cell.attrib.get("t") == "s":
                        value = shared[int(value)] if value.isdigit() and int(value) < len(shared) else ""
                values[index] = str(value).strip()
            matrix.append(values)

    if not matrix:
        return []
    headers = [str(cell or "").strip() for cell in matrix[0]]
    rows = []
    for values in matrix[1:]:
        if not any(values):
            continue
        rows.append({headers[i]: values[i] if i < len(values) else "" for i in range(len(headers))})
    return rows


def _read_rows(file_storage):
    filename = getattr(file_storage, "filename", "") or ""
    suffix = Path(filename).suffix.lower()
    raw = _read_upload(file_storage)
    if suffix == ".xlsx":
        return _read_xlsx(raw)

    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def _normalize_row(row, aliases):
    normalized = {}
    for target, names in aliases.items():
        value = ""
        for name in names:
            if name in row and str(row[name]).strip():
                value = str(row[name]).strip()
                break
        normalized[target] = value
    return normalized


def _as_int(value, default=0):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_bool(value):
    return 1 if str(value).strip().lower() in {"1", "true", "yes", "y", "是", "已校对"} else 0


def _paper_code(row):
    raw = f"{row['year']}-{row['exam_type']}-{row['region']}-{row.get('paper_name','')}-{row.get('paper_category','')}"
    return "P-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _content_hash(row):
    raw = "\n".join([row.get("prompt", ""), row.get("materials", ""), row.get("requirements", "")])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _ensure_paper(conn, row, year, relevance):
    paper_name = row.get("paper_name") or f"{year}{row['region']}{row['exam_type']}未分卷"
    normalized = dict(row)
    normalized["year"] = year
    normalized["paper_name"] = paper_name
    code = _paper_code(normalized)
    conn.execute(
        """
        INSERT INTO papers (
            paper_code, paper_name, paper_category, exam_type, year, region,
            source_province, zhejiang_relevance, source_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_code) DO UPDATE SET
            paper_name = excluded.paper_name,
            paper_category = excluded.paper_category,
            exam_type = excluded.exam_type,
            year = excluded.year,
            region = excluded.region,
            source_province = excluded.source_province,
            zhejiang_relevance = excluded.zhejiang_relevance,
            source_note = excluded.source_note,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            code,
            paper_name,
            row.get("paper_category", ""),
            row["exam_type"],
            year,
            row["region"],
            row.get("source_province", ""),
            relevance,
            row.get("source_note", ""),
        ),
    )
    return conn.execute("SELECT id FROM papers WHERE paper_code = ?", (code,)).fetchone()["id"]


def _upsert_materials(conn, paper_id, row):
    materials = []
    for key, value in row.items():
        if not value:
            continue
        text_key = str(key).strip()
        match = re.fullmatch(r"(?:材料|material)[\s_]*(\d+)", text_key, re.I)
        if match:
            materials.append((int(match.group(1)), value))
    if not materials and row.get("materials"):
        materials.append((1, row["materials"]))
    for number, content in sorted(materials):
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


def import_questions(conn, file_storage, import_id):
    rows = _read_rows(file_storage)
    errors = []
    imported = 0
    updated = 0

    for index, raw in enumerate(rows, start=2):
        row = _normalize_row(raw, QUESTION_ALIASES)
        missing = [name for name in QUESTION_REQUIRED if not row.get(name)]
        if missing:
            errors.append(f"题目表第 {index} 行缺少必填字段: {', '.join(missing)}")
            continue

        year = _as_int(row["year"])
        if year <= 0:
            errors.append(f"题目表第 {index} 行年份无效: {row['year']}")
            continue

        relevance = max(1, min(5, _as_int(row.get("zhejiang_relevance"), 3)))
        row["question_type"] = classify_question_type(
            row["prompt"],
            row["requirements"],
            fallback=row["question_type"],
        )[0]
        paper_id = _ensure_paper(conn, row, year, relevance)
        _upsert_materials(conn, paper_id, {**raw, **row})
        existing = conn.execute(
            "SELECT id FROM questions WHERE question_code = ?",
            (row["question_code"],),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE questions
                   SET paper_name = ?, exam_type = ?, year = ?, region = ?, source_province = ?,
                       paper_id = ?, paper_category = ?, question_number = ?,
                       zhejiang_relevance = ?, question_type = ?, title = ?,
                       prompt = ?, original_text = ?, materials = ?, requirements = ?,
                       word_limit = ?, source_url = ?, source_kind = ?,
                       is_full_original = ?, content_hash = ?, source_note = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE question_code = ?
                """,
                (
                    row.get("paper_name", ""),
                    row["exam_type"],
                    year,
                    row["region"],
                    row.get("source_province", ""),
                    paper_id,
                    row.get("paper_category", ""),
                    _as_int(row.get("question_number", ""), 0),
                    relevance,
                    row["question_type"],
                    row["title"],
                    row["prompt"],
                    row.get("original_text", ""),
                    row["materials"],
                    row["requirements"],
                    row.get("word_limit", ""),
                    row.get("source_url", ""),
                    row.get("source_kind", ""),
                    _as_bool(row.get("is_full_original", "")),
                    _content_hash(row),
                    row.get("source_note", ""),
                    row["question_code"],
                ),
            )
            updated += 1
        else:
            conn.execute(
                """
                INSERT INTO questions (
                    question_code, paper_id, paper_name, paper_category, question_number,
                    exam_type, year, region, source_province,
                    zhejiang_relevance, question_type, title, prompt, materials,
                    original_text, requirements, word_limit, source_url, source_kind,
                    is_full_original, content_hash, source_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["question_code"],
                    paper_id,
                    row.get("paper_name", ""),
                    row.get("paper_category", ""),
                    _as_int(row.get("question_number", ""), 0),
                    row["exam_type"],
                    year,
                    row["region"],
                    row.get("source_province", ""),
                    relevance,
                    row["question_type"],
                    row["title"],
                    row["prompt"],
                    row["materials"],
                    row.get("original_text", ""),
                    row["requirements"],
                    row.get("word_limit", ""),
                    row.get("source_url", ""),
                    row.get("source_kind", ""),
                    _as_bool(row.get("is_full_original", "")),
                    _content_hash(row),
                    row.get("source_note", ""),
                ),
            )
            imported += 1

    return {"imported": imported, "updated": updated, "errors": errors}


def import_answers(conn, file_storage, import_id):
    rows = _read_rows(file_storage)
    errors = []
    imported = 0
    updated = 0

    for index, raw in enumerate(rows, start=2):
        row = _normalize_row(raw, ANSWER_ALIASES)
        missing = [name for name in ANSWER_REQUIRED if not row.get(name)]
        if missing:
            errors.append(f"答案表第 {index} 行缺少必填字段: {', '.join(missing)}")
            continue

        question = conn.execute(
            "SELECT id FROM questions WHERE question_code = ?",
            (row["question_code"],),
        ).fetchone()
        if not question:
            errors.append(f"答案表第 {index} 行找不到题目编号: {row['question_code']}")
            continue

        existing = conn.execute(
            "SELECT id FROM reference_answers WHERE question_id = ? AND organization = ?",
            (question["id"], row["organization"]),
        ).fetchone()
        payload = (
            canonicalize_organization(row["organization"]),
            row["answer_text"],
            row.get("scoring_points", ""),
            row.get("notes", ""),
            import_id,
            _as_bool(row.get("is_reviewed", "")),
        )
        if existing:
            conn.execute(
                """
                UPDATE reference_answers
                   SET canonical_organization = ?, answer_text = ?, scoring_points = ?, notes = ?,
                       import_id = ?, is_reviewed = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (*payload, existing["id"]),
            )
            updated += 1
        else:
            conn.execute(
                """
                INSERT INTO reference_answers (
                    question_id, organization, canonical_organization, answer_text, scoring_points,
                    notes, import_id, is_reviewed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (question["id"], row["organization"], *payload),
            )
            imported += 1

    return {"imported": imported, "updated": updated, "errors": errors}


def create_import_record(conn, filename):
    cur = conn.execute(
        "INSERT INTO imports (filename, status) VALUES (?, ?)",
        (filename, "running"),
    )
    return cur.lastrowid


def finish_import_record(conn, import_id, status, errors, question_count, answer_count):
    conn.execute(
        """
        UPDATE imports
           SET status = ?, errors_json = ?, question_count = ?, answer_count = ?
         WHERE id = ?
        """,
        (status, json.dumps(errors, ensure_ascii=False), question_count, answer_count, import_id),
    )

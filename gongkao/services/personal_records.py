import html
import json


def _escape(value):
    return html.escape("" if value is None else str(value), quote=True)


def _nonnegative_int(value, default=0):
    try:
        parsed = int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return default
    return max(0, parsed)

def row_dict(row):
    return dict(row) if row is not None else {}


PERSONAL_BACKUP_VERSION = 3
QUESTION_BACKUP_COLUMNS = [
    "id",
    "question_code",
    "content_hash",
    "prompt",
    "title",
    "year",
    "region",
    "exam_type",
    "paper_name",
    "question_number",
]
PAPER_BACKUP_COLUMNS = [
    "id",
    "paper_code",
    "paper_name",
    "paper_category",
    "exam_type",
    "year",
    "region",
]
ANNOTATION_TARGETS = {"material", "answer", "note"}
ANNOTATION_COLORS = {"yellow", "green", "blue", "pink", "orange", "purple"}
ANNOTATION_STYLES = {"strike", "underline"}


def _int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_text_annotations(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value[:1000]:
        if not isinstance(item, dict):
            continue
        start = item.get("start")
        end = item.get("end")
        if isinstance(start, bool) or isinstance(end, bool):
            continue
        try:
            start = int(start)
            end = int(end)
        except (TypeError, ValueError):
            continue
        color = str(item.get("color") or "")
        style = str(item.get("style") or "")
        if start < 0 or end <= start or end > 10_000_000:
            continue
        if color not in ANNOTATION_COLORS:
            color = ""
        if style not in ANNOTATION_STYLES:
            style = ""
        if color or style:
            annotation = {"start": start, "end": end, "color": color, "style": style}
            note = str(item.get("note") or "")[:2000]
            if note:
                annotation["note"] = note
            quote = str(item.get("quote") or "")[:2000]
            if quote:
                annotation.update(
                    {
                        "quote": quote,
                        "prefix": str(item.get("prefix") or "")[-80:],
                        "suffix": str(item.get("suffix") or "")[:80],
                        "anchor_version": 1,
                    }
                )
            normalized.append(annotation)
    return normalized


def text_annotation_key(target_type, question_id=None, material_number=None, attempt_id=None):
    if target_type == "material":
        return f"material:{int(question_id)}:{int(material_number)}"
    if target_type in {"answer", "note"}:
        return f"{target_type}:{int(attempt_id)}"
    raise ValueError("不支持的标注类型。")


def save_text_annotations(
    conn,
    target_type,
    annotations,
    text_hash="",
    question_id=None,
    material_number=None,
    attempt_id=None,
):
    if target_type not in ANNOTATION_TARGETS:
        raise ValueError("不支持的标注类型。")
    question_id = _int_or_none(question_id)
    material_number = _int_or_none(material_number)
    attempt_id = _int_or_none(attempt_id)
    if target_type == "material":
        if question_id is None or material_number is None:
            raise ValueError("材料标注缺少题目或材料编号。")
        exists = conn.execute(
            """
            SELECT 1
              FROM questions q
              JOIN paper_materials m ON m.paper_id = q.paper_id
             WHERE q.id = ? AND m.material_number = ?
             LIMIT 1
            """,
            (question_id, material_number),
        ).fetchone()
        if not exists:
            raise ValueError("找不到对应材料。")
        attempt_id = None
    else:
        if attempt_id is None or not conn.execute(
            "SELECT 1 FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone():
            raise ValueError("找不到对应作答。")
        question_id = None
        material_number = None
    key = text_annotation_key(target_type, question_id, material_number, attempt_id)
    normalized = normalize_text_annotations(annotations)
    if not normalized:
        conn.execute("DELETE FROM text_annotations WHERE annotation_key = ?", (key,))
        return key
    conn.execute(
        """
        INSERT INTO text_annotations (
            annotation_key, target_type, question_id, material_number,
            attempt_id, text_hash, annotations_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(annotation_key) DO UPDATE SET
            text_hash = excluded.text_hash,
            annotations_json = excluded.annotations_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            key,
            target_type,
            question_id,
            material_number,
            attempt_id,
            str(text_hash or "")[:100],
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    return key


def annotation_data_attributes(row):
    annotations = row["annotations_json"] if row else "[]"
    text_hash = row["text_hash"] if row else ""
    return (
        f'data-saved-annotations="{_escape(annotations)}" '
        f'data-saved-text-hash="{_escape(text_hash)}" '
        'data-annotation-save-url="/annotations"'
    )


def _question_snapshot(conn, question_id):
    question_id = _int_or_none(question_id)
    if question_id is None:
        return None
    row = conn.execute(
        f"SELECT {', '.join(QUESTION_BACKUP_COLUMNS)} FROM questions WHERE id = ?",
        (question_id,),
    ).fetchone()
    return row_dict(row) or None


def _paper_snapshot(conn, paper_id):
    paper_id = _int_or_none(paper_id)
    if paper_id is None:
        return None
    row = conn.execute(
        f"SELECT {', '.join(PAPER_BACKUP_COLUMNS)} FROM papers WHERE id = ?",
        (paper_id,),
    ).fetchone()
    return row_dict(row) or None


def _backup_attempts(conn):
    attempts = []
    for row in conn.execute("SELECT * FROM attempts ORDER BY id"):
        item = dict(row)
        snapshot = _question_snapshot(conn, item.get("question_id"))
        if snapshot:
            item["question"] = snapshot
        attempts.append(item)
    return attempts


def _backup_question_favorites(conn):
    favorites = []
    for row in conn.execute("SELECT * FROM question_favorites ORDER BY id"):
        item = dict(row)
        snapshot = _question_snapshot(conn, item.get("question_id"))
        if snapshot:
            item["question"] = snapshot
        favorites.append(item)
    return favorites


def _backup_paper_favorites(conn):
    favorites = []
    for row in conn.execute("SELECT * FROM paper_favorites ORDER BY id"):
        item = dict(row)
        snapshot = _paper_snapshot(conn, item.get("paper_id"))
        if snapshot:
            item["paper"] = snapshot
        favorites.append(item)
    return favorites


def _backup_text_annotations(conn):
    annotations = []
    for row in conn.execute("SELECT * FROM text_annotations ORDER BY annotation_key"):
        item = dict(row)
        if item["target_type"] == "material":
            snapshot = _question_snapshot(conn, item.get("question_id"))
            if snapshot:
                item["question"] = snapshot
        annotations.append(item)
    return annotations


def export_personal_data(conn, include_api_key=False):
    settings = row_dict(conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone())
    agent_settings = row_dict(conn.execute("SELECT * FROM agent_ai_settings WHERE id = 1").fetchone())
    if not include_api_key:
        settings.pop("api_key", None)
        agent_settings.pop("api_key", None)
    payload = {
        "format": "gongkao-personal-backup",
        "version": PERSONAL_BACKUP_VERSION,
        "exported_at": conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0],
        "ai_settings": settings,
        "agent_ai_settings": agent_settings,
        "attempts": _backup_attempts(conn),
        "grading_reports": [dict(row) for row in conn.execute("SELECT * FROM grading_reports ORDER BY id")],
        "grading_report_contexts": [dict(row) for row in conn.execute("SELECT * FROM grading_report_contexts ORDER BY report_id")],
        "grading_feedback": [dict(row) for row in conn.execute("SELECT * FROM grading_feedback ORDER BY id")],
        "question_favorites": _backup_question_favorites(conn),
        "paper_favorites": _backup_paper_favorites(conn),
        "text_annotations": _backup_text_annotations(conn),
        "agent_memories": [
            dict(row)
            for row in conn.execute(
                """
                SELECT memory_type, memory_key, content, confidence, status, created_at, updated_at
                  FROM agent_memories
              ORDER BY id
                """
            )
        ],
        "agent_runs": [dict(row) for row in conn.execute("SELECT * FROM agent_runs ORDER BY id")],
        "agent_conversations": [dict(row) for row in conn.execute("SELECT * FROM agent_conversations ORDER BY id")],
        "agent_messages": [dict(row) for row in conn.execute("SELECT * FROM agent_messages ORDER BY id")],
        "agent_feedback": [dict(row) for row in conn.execute("SELECT * FROM agent_feedback ORDER BY id")],
        "training_plan_items": [dict(row) for row in conn.execute("SELECT * FROM training_plan_items ORDER BY id")],
    }
    return payload


def _row_id(row):
    return row["id"] if row else None


def _question_exists(conn, question_id):
    question_id = _int_or_none(question_id)
    if question_id is None:
        return None
    return _row_id(conn.execute("SELECT id FROM questions WHERE id = ?", (question_id,)).fetchone())


def _paper_exists(conn, paper_id):
    paper_id = _int_or_none(paper_id)
    if paper_id is None:
        return None
    return _row_id(conn.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone())


def _resolve_question_id(conn, item):
    direct_id = _question_exists(conn, item.get("question_id"))
    snapshot = item.get("question") if isinstance(item.get("question"), dict) else {}
    code = (snapshot.get("question_code") or item.get("question_code") or "").strip()
    if code:
        found = _row_id(conn.execute("SELECT id FROM questions WHERE question_code = ?", (code,)).fetchone())
        if found:
            return found
    return direct_id


def _resolve_paper_id(conn, item):
    direct_id = _paper_exists(conn, item.get("paper_id"))
    snapshot = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    code = (snapshot.get("paper_code") or item.get("paper_code") or "").strip()
    if code:
        found = _row_id(conn.execute("SELECT id FROM papers WHERE paper_code = ?", (code,)).fetchone())
        if found:
            return found
    return direct_id


def _normalized_reference_ids(conn, question_id, raw_ids):
    try:
        values = [int(value) for value in json.loads(raw_ids or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    if not values:
        return "[]"
    valid_ids = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM reference_answers WHERE question_id = ?",
            (question_id,),
        )
    }
    return json.dumps([value for value in values if value in valid_ids], ensure_ascii=False)


def import_personal_data(conn, payload):
    """Merge a current-format backup while remapping source foreign keys.

    This orchestration is intentionally sequential: attempts, reports, runs,
    conversations, and messages carry IDs from the source database, so each
    mapping must be available before the dependent collection is imported.
    The ordered transaction is kept together to make partial merges impossible.
    """
    if not isinstance(payload, dict) or payload.get("format") != "gongkao-personal-backup":
        raise ValueError("备份文件格式不正确。")
    if payload.get("version") != PERSONAL_BACKUP_VERSION:
        raise ValueError(f"仅支持版本 {PERSONAL_BACKUP_VERSION} 的个人数据备份。")
    counts = {
        "attempts": 0,
        "reports": 0,
        "grading_contexts": 0,
        "grading_feedback": 0,
        "question_favorites": 0,
        "paper_favorites": 0,
        "settings": 0,
        "annotations": 0,
        "memories": 0,
        "conversations": 0,
        "messages": 0,
        "feedback": 0,
        "training_plan_items": 0,
        "runs": 0,
        "skipped_attempts": 0,
        "skipped_reports": 0,
        "skipped_grading_contexts": 0,
        "skipped_grading_feedback": 0,
        "skipped_question_favorites": 0,
        "skipped_paper_favorites": 0,
        "skipped_annotations": 0,
        "skipped_memories": 0,
        "skipped_conversations": 0,
        "skipped_messages": 0,
        "skipped_feedback": 0,
        "skipped_training_plan_items": 0,
        "skipped_runs": 0,
    }
    settings = payload.get("ai_settings") or {}
    if isinstance(settings, dict):
        allowed = [
            "mode",
            "provider_name",
            "api_base_url",
            "api_key_env",
            "model",
            "temperature",
            "prompt_template",
            "grading_mode",
        ]
        if "api_key" in settings:
            allowed.append("api_key")
        values = {key: settings[key] for key in allowed if key in settings}
        if values:
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE ai_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                list(values.values()),
            )
            counts["settings"] = 1

    agent_settings = payload.get("agent_ai_settings") or {}
    if isinstance(agent_settings, dict):
        allowed = [
            "use_grading_api",
            "provider_name",
            "api_base_url",
            "api_key_env",
            "model",
            "temperature",
        ]
        if "api_key" in agent_settings:
            allowed.append("api_key")
        values = {key: agent_settings[key] for key in allowed if key in agent_settings}
        if values:
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE agent_ai_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                list(values.values()),
            )
            counts["settings"] = 1

    for item in payload.get("agent_memories") or []:
        if not isinstance(item, dict):
            counts["skipped_memories"] += 1
            continue
        memory_type = str(item.get("memory_type") or "").strip()
        memory_key = str(item.get("memory_key") or "").strip()
        content = str(item.get("content") or "").strip()
        if memory_type not in {"semantic", "episodic", "procedural"} or not memory_key or not content:
            counts["skipped_memories"] += 1
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.8)))
        except (TypeError, ValueError):
            confidence = 0.8
        conn.execute(
            """
            INSERT INTO agent_memories (
                memory_type, memory_key, content, confidence, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))
            ON CONFLICT(memory_type, memory_key) DO UPDATE SET
                content = excluded.content,
                confidence = excluded.confidence,
                status = 'active',
                updated_at = excluded.updated_at
            """,
            (
                memory_type,
                memory_key[:80],
                content[:500],
                confidence,
                item.get("created_at"),
                item.get("updated_at"),
            ),
        )
        counts["memories"] += 1

    for item in payload.get("question_favorites") or []:
        question_id = _resolve_question_id(conn, item)
        if not question_id:
            counts["skipped_question_favorites"] += 1
            continue
        before = conn.execute("SELECT COUNT(*) FROM question_favorites").fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO question_favorites (question_id, created_at) VALUES (?, COALESCE(?, CURRENT_TIMESTAMP))",
            (question_id, item.get("created_at")),
        )
        after = conn.execute("SELECT COUNT(*) FROM question_favorites").fetchone()[0]
        counts["question_favorites"] += max(0, after - before)

    for item in payload.get("paper_favorites") or []:
        paper_id = _resolve_paper_id(conn, item)
        if not paper_id:
            counts["skipped_paper_favorites"] += 1
            continue
        before = conn.execute("SELECT COUNT(*) FROM paper_favorites").fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO paper_favorites (paper_id, created_at) VALUES (?, COALESCE(?, CURRENT_TIMESTAMP))",
            (paper_id, item.get("created_at")),
        )
        after = conn.execute("SELECT COUNT(*) FROM paper_favorites").fetchone()[0]
        counts["paper_favorites"] += max(0, after - before)

    attempt_map = {}
    for item in payload.get("attempts") or []:
        old_id = item.get("id")
        question_id = _resolve_question_id(conn, item)
        if not question_id:
            counts["skipped_attempts"] += 1
            continue
        existing = conn.execute(
            "SELECT id, personal_note FROM attempts WHERE question_id = ? AND answer_text = ? AND created_at = ?",
            (question_id, item.get("answer_text", ""), item.get("created_at")),
        ).fetchone()
        if existing:
            attempt_map[item.get("id")] = existing["id"]
            if any(
                key in item
                for key in (
                    "grading_references_configured",
                    "grading_reference_ids",
                    "custom_reference_answer",
                    "personal_note",
                    "duration_seconds",
                    "paper_elapsed_seconds",
                    "paper_time_excluded",
                )
            ):
                reference_ids = _normalized_reference_ids(conn, question_id, item.get("grading_reference_ids", "[]"))
                conn.execute(
                    """
                    UPDATE attempts
                       SET grading_references_configured = ?,
                           grading_reference_ids = ?,
                           custom_reference_answer = ?,
                           personal_note = ?,
                           duration_seconds = ?,
                           paper_elapsed_seconds = ?,
                           paper_time_excluded = ?
                     WHERE id = ?
                    """,
                    (
                        1 if item.get("grading_references_configured") else 0,
                        reference_ids,
                        item.get("custom_reference_answer", ""),
                        item.get("personal_note", existing["personal_note"]),
                        _nonnegative_int(item.get("duration_seconds")),
                        _nonnegative_int(item.get("paper_elapsed_seconds")),
                        1 if item.get("paper_time_excluded") else 0,
                        existing["id"],
                    ),
                )
            continue
        reference_ids = _normalized_reference_ids(conn, question_id, item.get("grading_reference_ids", "[]"))
        columns = [
            "question_id",
            "answer_text",
            "word_count",
            "grading_result",
            "grading_references_configured",
            "grading_reference_ids",
            "custom_reference_answer",
            "personal_note",
            "duration_seconds",
            "paper_elapsed_seconds",
            "paper_time_excluded",
            "created_at",
        ]
        values = [
            question_id,
            item.get("answer_text", ""),
            int(item.get("word_count") or 0),
            item.get("grading_result", ""),
            1 if item.get("grading_references_configured") else 0,
            reference_ids,
            item.get("custom_reference_answer", ""),
            item.get("personal_note", ""),
            _nonnegative_int(item.get("duration_seconds")),
            _nonnegative_int(item.get("paper_elapsed_seconds")),
            1 if item.get("paper_time_excluded") else 0,
            item.get("created_at"),
        ]
        if old_id and not conn.execute("SELECT 1 FROM attempts WHERE id = ?", (old_id,)).fetchone():
            columns.insert(0, "id")
            values.insert(0, old_id)
        placeholders = ", ".join("?" for _ in columns)
        cursor = conn.execute(
            f"INSERT INTO attempts ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        attempt_map[old_id] = old_id if "id" in columns else cursor.lastrowid
        counts["attempts"] += 1

    for item in payload.get("text_annotations") or []:
        if not isinstance(item, dict):
            counts["skipped_annotations"] += 1
            continue
        target_type = item.get("target_type")
        question_id = None
        material_number = None
        attempt_id = None
        if target_type == "material":
            question_id = _resolve_question_id(conn, item)
            material_number = item.get("material_number")
            if not question_id:
                counts["skipped_annotations"] += 1
                continue
        elif target_type in {"answer", "note"}:
            attempt_id = attempt_map.get(item.get("attempt_id"))
            if not attempt_id:
                counts["skipped_annotations"] += 1
                continue
        else:
            counts["skipped_annotations"] += 1
            continue
        try:
            save_text_annotations(
                conn,
                target_type,
                item.get("annotations_json", "[]"),
                item.get("text_hash", ""),
                question_id=question_id,
                material_number=material_number,
                attempt_id=attempt_id,
            )
        except ValueError:
            counts["skipped_annotations"] += 1
            continue
        counts["annotations"] += 1

    report_map = {}
    for item in payload.get("grading_reports") or []:
        attempt_id = attempt_map.get(item.get("attempt_id"))
        if not attempt_id:
            counts["skipped_reports"] += 1
            continue
        existing = conn.execute(
            """
            SELECT id FROM grading_reports
             WHERE attempt_id = ? AND provider = ? AND model = ? AND report_text = ? AND created_at = ?
            """,
            (attempt_id, item.get("provider", ""), item.get("model", ""), item.get("report_text", ""), item.get("created_at")),
        ).fetchone()
        if existing:
            report_map[item.get("id")] = existing["id"]
            continue
        cursor = conn.execute(
            """
            INSERT INTO grading_reports (
                attempt_id, provider, model, report_text, prompt_text, raw_response, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                attempt_id,
                item.get("provider", ""),
                item.get("model", ""),
                item.get("report_text", ""),
                item.get("prompt_text", ""),
                item.get("raw_response", ""),
                item.get("status", "ok"),
                item.get("created_at"),
            ),
        )
        report_map[item.get("id")] = cursor.lastrowid
        counts["reports"] += 1

    for item in payload.get("grading_report_contexts") or []:
        if not isinstance(item, dict):
            counts["skipped_grading_contexts"] += 1
            continue
        report_id = report_map.get(item.get("report_id"))
        if not report_id:
            counts["skipped_grading_contexts"] += 1
            continue
        conn.execute(
            """
            INSERT INTO grading_report_contexts (
                report_id, rubric_id, pipeline_version, retrieval_json, result_json,
                validation_json, rubric_snapshot_json, api_call_count, latency_ms, created_at
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            ON CONFLICT(report_id) DO UPDATE SET
                pipeline_version = excluded.pipeline_version,
                retrieval_json = excluded.retrieval_json,
                result_json = excluded.result_json,
                validation_json = excluded.validation_json,
                rubric_snapshot_json = excluded.rubric_snapshot_json,
                api_call_count = excluded.api_call_count,
                latency_ms = excluded.latency_ms
            """,
            (
                report_id,
                item.get("pipeline_version", "smart-grading-v1"),
                item.get("retrieval_json", "[]"),
                item.get("result_json", "{}"),
                item.get("validation_json", "{}"),
                item.get("rubric_snapshot_json", "{}"),
                _nonnegative_int(item.get("api_call_count")),
                _nonnegative_int(item.get("latency_ms")),
                item.get("created_at"),
            ),
        )
        counts["grading_contexts"] += 1

    for item in payload.get("grading_feedback") or []:
        if not isinstance(item, dict):
            counts["skipped_grading_feedback"] += 1
            continue
        report_id = report_map.get(item.get("report_id"))
        report = conn.execute(
            "SELECT gr.attempt_id, a.question_id FROM grading_reports gr JOIN attempts a ON a.id = gr.attempt_id WHERE gr.id = ?",
            (report_id,),
        ).fetchone() if report_id else None
        if not report or not item.get("point_key"):
            counts["skipped_grading_feedback"] += 1
            continue
        conn.execute(
            """
            INSERT INTO grading_feedback (
                report_id, attempt_id, question_id, point_key, scope,
                corrected_status, corrected_quote, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))
            ON CONFLICT(report_id, point_key, scope) DO UPDATE SET
                corrected_status = excluded.corrected_status,
                corrected_quote = excluded.corrected_quote,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (
                report_id,
                report["attempt_id"],
                report["question_id"],
                item.get("point_key"),
                item.get("scope") if item.get("scope") in {"report", "question"} else "report",
                item.get("corrected_status") if item.get("corrected_status") in {"", "hit", "partial", "miss", "invalid"} else "",
                item.get("corrected_quote", ""),
                item.get("note", ""),
                item.get("created_at"),
                item.get("updated_at"),
            ),
        )
        counts["grading_feedback"] += 1

    for item in payload.get("training_plan_items") or []:
        if not isinstance(item, dict):
            counts["skipped_training_plan_items"] += 1
            continue
        question_id = _resolve_question_id(conn, item)
        if not question_id:
            counts["skipped_training_plan_items"] += 1
            continue
        title = item.get("title")
        if not title:
            counts["skipped_training_plan_items"] += 1
            continue
        existing = conn.execute(
            "SELECT 1 FROM training_plan_items WHERE question_id = ? AND title = ?",
            (question_id, title)
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO training_plan_items (question_id, title, reason, target_date, status, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?)
            """,
            (
                question_id,
                title,
                item.get("reason", ""),
                item.get("target_date", ""),
                item.get("status", "todo"),
                item.get("created_at"),
                item.get("completed_at")
            )
        )
        counts["training_plan_items"] += 1

    run_map = {}
    for item in payload.get("agent_runs") or []:
        if not isinstance(item, dict):
            counts["skipped_runs"] += 1
            continue
        old_id = item.get("id")
        subject_type = item.get("subject_type", "")
        subject_id = item.get("subject_id")
        if subject_type == "attempt":
            subject_id = attempt_map.get(subject_id)
        elif subject_type == "question":
            subject_id = _resolve_question_id(conn, {"question_code": item.get("question_code")}) or _resolve_question_id(conn, {"question_id": subject_id})
            
        existing = conn.execute(
            "SELECT id FROM agent_runs WHERE task_type = ? AND created_at = ?",
            (item.get("task_type"), item.get("created_at"))
        ).fetchone()
        if existing:
            run_map[old_id] = existing["id"]
            continue
            
        cursor = conn.execute(
            """
            INSERT INTO agent_runs (task_type, subject_type, subject_id, status, user_goal, input_summary, final_text, provider, model, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?)
            """,
            (
                item.get("task_type"),
                subject_type,
                subject_id,
                item.get("status", "created"),
                item.get("user_goal", ""),
                item.get("input_summary", ""),
                item.get("final_text", ""),
                item.get("provider", ""),
                item.get("model", ""),
                item.get("created_at"),
                item.get("completed_at")
            )
        )
        run_map[old_id] = cursor.lastrowid
        counts["runs"] += 1

    conversation_map = {}
    for item in payload.get("agent_conversations") or []:
        if not isinstance(item, dict):
            counts["skipped_conversations"] += 1
            continue
        old_id = item.get("id")
        title = item.get("title")
        entrypoint = item.get("entrypoint", "chat")
        status = item.get("status", "active")
        
        existing = conn.execute(
            "SELECT id FROM agent_conversations WHERE title = ? AND created_at = ?",
            (title, item.get("created_at"))
        ).fetchone()
        if existing:
            conversation_map[old_id] = existing["id"]
            continue
            
        cursor = conn.execute(
            """
            INSERT INTO agent_conversations (title, entrypoint, status, created_at, updated_at)
            VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (title, entrypoint, status, item.get("created_at"), item.get("updated_at"))
        )
        conversation_map[old_id] = cursor.lastrowid
        counts["conversations"] += 1

    message_map = {}
    for item in payload.get("agent_messages") or []:
        if not isinstance(item, dict):
            counts["skipped_messages"] += 1
            continue
        old_id = item.get("id")
        old_conv_id = item.get("conversation_id")
        new_conv_id = conversation_map.get(old_conv_id)
        if not new_conv_id:
            counts["skipped_messages"] += 1
            continue
        
        old_run_id = item.get("run_id")
        new_run_id = run_map.get(old_run_id)
        
        existing = conn.execute(
            "SELECT id FROM agent_messages WHERE conversation_id = ? AND content = ? AND created_at = ?",
            (new_conv_id, item.get("content", ""), item.get("created_at"))
        ).fetchone()
        if existing:
            message_map[old_id] = existing["id"]
            continue
            
        cursor = conn.execute(
            """
            INSERT INTO agent_messages (conversation_id, run_id, role, message_type, content, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                new_conv_id,
                new_run_id,
                item.get("role", "user"),
                item.get("message_type", "text"),
                item.get("content", ""),
                item.get("metadata_json", "{}"),
                item.get("created_at")
            )
        )
        message_map[old_id] = cursor.lastrowid
        counts["messages"] += 1

    for item in payload.get("agent_feedback") or []:
        if not isinstance(item, dict):
            counts["skipped_feedback"] += 1
            continue
        old_run_id = item.get("run_id")
        new_run_id = run_map.get(old_run_id)
        if not new_run_id:
            counts["skipped_feedback"] += 1
            continue
            
        existing = conn.execute(
            "SELECT 1 FROM agent_feedback WHERE run_id = ?",
            (new_run_id,)
        ).fetchone()
        if existing:
            continue
            
        conn.execute(
            """
            INSERT INTO agent_feedback (run_id, rating, note, created_at)
            VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                new_run_id,
                int(item.get("rating") or 1),
                item.get("note", ""),
                item.get("created_at")
            )
        )
        counts["feedback"] += 1

    return counts



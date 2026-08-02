"""Attempt editing, annotation, favorite, and package controllers."""

from ...answer_formatting import normalize_answer_format_json
from ..runtime import (
    apply_versioned_autosave,
    attempt_grading_references,
    autosave_identity,
    build_grading_package,
    connect,
    count_cjk_chars,
    json,
    manual_grading_basis,
    nonnegative_int,
    paper_attempt_duration_seconds,
    parse_qs,
    question_paper_duration_seconds,
    return_path_from_form,
    save_text_annotations,
    select_relevant_materials,
)


class PracticeController:
    def handle_attempt(self, path):
        try:
            question_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        form = parse_qs(data)
        answer_text = form.get("answer_text", [""])[0]
        answer_format_json = normalize_answer_format_json(
            form.get("answer_format_json", ["[]"])[0], answer_text
        )
        duration_seconds = nonnegative_int(form.get("duration_seconds", ["0"])[0])
        paper_time_excluded = 1 if form.get("paper_time_excluded", [""])[0] else 0
        if answer_text.strip():
            with connect(self.db_path) as conn:
                question = conn.execute("SELECT paper_id FROM questions WHERE id = ?", (question_id,)).fetchone()
                if not question:
                    self.send_error(404)
                    return
                paper_elapsed_seconds = 0
                if question["paper_id"]:
                    paper_elapsed_seconds = paper_attempt_duration_seconds(
                        conn,
                        question["paper_id"],
                        exclude_question_id=question_id,
                    )
                    saved_question_seconds = question_paper_duration_seconds(conn, question_id)
                    paper_elapsed_seconds += (
                        saved_question_seconds if paper_time_excluded else max(saved_question_seconds, duration_seconds)
                    )
                cursor = conn.execute(
                    """
                    INSERT INTO attempts (
                        question_id, answer_text, answer_format_json, word_count,
                        duration_seconds, paper_elapsed_seconds, paper_time_excluded
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        question_id,
                        answer_text,
                        answer_format_json,
                        count_cjk_chars(answer_text),
                        duration_seconds,
                        paper_elapsed_seconds,
                        paper_time_excluded,
                    ),
                )
                attempt_id = cursor.lastrowid
            self.redirect(f"/attempts/{attempt_id}")
            return
        self.redirect(f"/questions/{question_id}")

    def handle_update_attempt(self, path):
        try:
            attempt_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        form = parse_qs(data, keep_blank_values=True)
        answer_text = form.get("answer_text", [""])[0]
        answer_format_json = normalize_answer_format_json(
            form.get("answer_format_json", ["[]"])[0], answer_text
        )
        session_id, revision = autosave_identity(form)
        is_autosave = bool(session_id and revision) or self.headers.get("X-Gongkao-Autosave") == "1"
        with connect(self.db_path) as conn:
            exists = conn.execute("SELECT 1 FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if not exists:
            self.send_error(404)
            return

        def save_answer():
            with connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE attempts
                       SET answer_text = ?, answer_format_json = ?, word_count = ?
                     WHERE id = ?
                    """,
                    (answer_text, answer_format_json, count_cjk_chars(answer_text), attempt_id),
                )
                if "annotations_json" in form:
                    try:
                        annotations = json.loads(form.get("annotations_json", ["[]"])[0] or "[]")
                    except json.JSONDecodeError:
                        annotations = None
                    if annotations is not None:
                        save_text_annotations(
                            conn,
                            "answer",
                            annotations,
                            form.get("annotations_text_hash", [""])[0],
                            attempt_id=attempt_id,
                        )

        accepted = apply_versioned_autosave(
            ("answer", attempt_id),
            session_id,
            revision,
            save_answer,
            self.app_context.autosave,
        )
        if is_autosave:
            self.send_json({"ok": True, "accepted": accepted, "revision": revision})
            return
        self.redirect(f"/attempts/{attempt_id}")

    def handle_attempt_note(self, path):
        try:
            attempt_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        form = parse_qs(data, keep_blank_values=True)
        note_text = form.get("personal_note", [""])[0]
        session_id, revision = autosave_identity(form)
        with connect(self.db_path) as conn:
            exists = conn.execute("SELECT 1 FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if not exists:
            self.send_error(404)
            return

        def save_note():
            with connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE attempts SET personal_note = ? WHERE id = ?",
                    (note_text, attempt_id),
                )
                if "annotations_json" in form:
                    try:
                        annotations = json.loads(form.get("annotations_json", ["[]"])[0] or "[]")
                    except json.JSONDecodeError:
                        annotations = None
                    if annotations is not None:
                        save_text_annotations(
                            conn,
                            "note",
                            annotations,
                            form.get("annotations_text_hash", [""])[0],
                            attempt_id=attempt_id,
                        )

        accepted = apply_versioned_autosave(
            ("note", attempt_id),
            session_id,
            revision,
            save_note,
            self.app_context.autosave,
        )
        self.send_json({"ok": True, "accepted": accepted, "revision": revision})

    def handle_text_annotations(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            self.send_json({"error": "invalid_payload"}, status=400)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("标注数据格式不正确。")
            with connect(self.db_path) as conn:
                key = save_text_annotations(
                    conn,
                    payload.get("target_type"),
                    payload.get("annotations", []),
                    payload.get("text_hash", ""),
                    question_id=payload.get("question_id"),
                    material_number=payload.get("material_number"),
                    attempt_id=payload.get("attempt_id"),
                )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        self.send_json({"ok": True, "annotation_key": key})

    def handle_favorite(self, path, kind):
        try:
            item_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        fallback = f"/{kind}/{item_id}"
        return_to = return_path_from_form(data, fallback)
        if kind == "questions":
            target_table, favorite_table, target_column = "questions", "question_favorites", "question_id"
        else:
            target_table, favorite_table, target_column = "papers", "paper_favorites", "paper_id"
        with connect(self.db_path) as conn:
            if not conn.execute(f"SELECT 1 FROM {target_table} WHERE id = ?", (item_id,)).fetchone():
                self.send_error(404)
                return
            existing = conn.execute(f"SELECT id FROM {favorite_table} WHERE {target_column} = ?", (item_id,)).fetchone()
            if existing:
                conn.execute(f"DELETE FROM {favorite_table} WHERE id = ?", (existing["id"],))
            else:
                conn.execute(
                    f"INSERT OR IGNORE INTO {favorite_table} ({target_column}) VALUES (?)",
                    (item_id,),
                )
        self.redirect(return_to)

    def handle_delete_attempt(self, path):
        try:
            attempt_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        return_to = return_path_from_form(data, "/attempts")
        with connect(self.db_path) as conn:
            attempt = conn.execute("SELECT question_id FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            if attempt:
                conn.execute("DELETE FROM attempts WHERE id = ?", (attempt_id,))
        self.redirect(return_to)

    def blank_package(self, path):
        try:
            question_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        with connect(self.db_path) as conn:
            question = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
            if not question:
                self.send_error(404)
                return
            refs = conn.execute(
                "SELECT * FROM reference_answers WHERE question_id = ? ORDER BY organization", (question_id,)
            ).fetchall()
            materials = (
                conn.execute(
                    "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
                    (question["paper_id"],),
                ).fetchall()
                if question["paper_id"]
                else []
            )
        relevant_materials = select_relevant_materials(question, materials)
        with connect(self.db_path) as conn:
            grading_basis = manual_grading_basis(conn, question, relevant_materials, refs)
        self.send_text(
            build_grading_package(question, refs, materials=materials, grading_basis=grading_basis),
            f"申论第{question['question_number'] or question['id']}题-批改包.md",
            "text/markdown; charset=utf-8",
        )

    def attempt_package(self, path):
        try:
            attempt_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        with connect(self.db_path) as conn:
            attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            if not attempt:
                self.send_error(404)
                return
            question = conn.execute("SELECT * FROM questions WHERE id = ?", (attempt["question_id"],)).fetchone()
            refs = conn.execute(
                "SELECT * FROM reference_answers WHERE question_id = ? ORDER BY organization", (question["id"],)
            ).fetchall()
            materials = (
                conn.execute(
                    "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
                    (question["paper_id"],),
                ).fetchall()
                if question["paper_id"]
                else []
            )
        selected_refs, selected_ids, custom_answer = attempt_grading_references(attempt, refs)
        relevant_materials = select_relevant_materials(question, materials)
        with connect(self.db_path) as conn:
            grading_basis = manual_grading_basis(conn, question, relevant_materials, selected_refs)
        self.send_text(
            build_grading_package(
                question,
                selected_refs,
                attempt,
                materials,
                custom_answer,
                grading_basis,
            ),
            f"申论第{question['question_number'] or question['id']}题-作答.md",
            "text/markdown; charset=utf-8",
        )

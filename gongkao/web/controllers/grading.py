"""Grading workflow and report controllers."""

from ...answer_formatting import normalize_answer_format_json
from ..runtime import (
    ACTIVE_JOB_STATUSES,
    AiConfigError,
    AiRequestError,
    annotation_data_attributes,
    apply_report_feedback,
    attempt_grading_references,
    build_ai_prompt,
    build_grading_package,
    build_revised_answer_retry_prompt,
    chat_completion,
    connect,
    count_cjk_chars,
    create_grading_job,
    esc,
    evidence_return_path,
    form_value,
    format_beijing_time,
    format_duration,
    grading_job_payload,
    grading_references_from_form,
    grading_report_return_path,
    invalidate_question_rubrics,
    json,
    layout,
    local_url,
    manual_grading_basis,
    markdownish,
    next_question_path,
    nonnegative_int,
    normalize_revised_answer_word_count,
    parse_qs,
    parse_revised_answer_repair,
    pre,
    re,
    replace_revised_answer_body,
    report_answer_snapshot,
    return_path_from_form,
    return_path_from_query,
    revised_answer_word_count_status,
    rubric_cache_status,
    save_attempt_grading_references,
    select_relevant_materials,
    start_grading_job,
    tabbed_materials,
    tabbed_references,
    workflow_header,
)


class GradingController:
    def page_grading_report(self, path, query):
        try:
            report_id = int(path.removeprefix("/grading-reports/").strip("/"))
        except ValueError:
            self.send_error(404)
            return
        with connect(self.db_path) as conn:
            report = conn.execute(
                "SELECT attempt_id FROM grading_reports WHERE id = ?",
                (report_id,),
            ).fetchone()
        if not report:
            self.send_error(404)
            return
        return_to = return_path_from_query(query, "")
        target = local_url(
            f"/attempts/{report['attempt_id']}",
            return_to=return_to,
            fragment=f"report-{report_id}",
        )
        self.redirect(target)

    def _attempt_context(self, attempt_id):
        with connect(self.db_path) as conn:
            attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            if not attempt:
                return None
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
            reports = conn.execute(
                "SELECT * FROM grading_reports WHERE attempt_id = ? ORDER BY created_at DESC, id DESC", (attempt_id,)
            ).fetchall()
            settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
            annotations = conn.execute(
                """
                SELECT * FROM text_annotations
                 WHERE (target_type = 'material' AND question_id = ?)
                    OR (target_type IN ('answer', 'note') AND attempt_id = ?)
                """,
                (question["id"], attempt_id),
            ).fetchall()
        return attempt, question, refs, materials, reports, settings, annotations

    def page_attempt_detail(self, path, query=None, flashes=None, preview_report=None):
        if flashes is None and isinstance(query, list):
            flashes = query
            query = {}
        try:
            attempt_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        context = self._attempt_context(attempt_id)
        if not context:
            self.send_error(404)
            return
        attempt, question, refs, materials, reports, settings, annotations = context
        query = query or {}
        back_href = f"/questions/{question['id']}"
        report_parent_return = return_path_from_query(query, back_href)
        context_return = evidence_return_path(query)
        citation_target = bool(context_return)
        active_section = "papers" if question["paper_id"] else "index"
        relevant_materials = select_relevant_materials(question, materials)
        selected_refs, selected_ref_ids, custom_reference_answer = attempt_grading_references(attempt, refs)
        requested_job_id = query.get("grading_job", [""])[0]
        with connect(self.db_path) as conn:
            cache_info = rubric_cache_status(
                conn,
                question["id"],
                [dict(row) for row in selected_refs],
                dict(question),
                [dict(row) for row in relevant_materials],
            )
            grading_basis = manual_grading_basis(
                conn,
                question,
                relevant_materials,
                selected_refs,
            )
            if str(requested_job_id).isdigit():
                grading_job = conn.execute(
                    "SELECT * FROM grading_jobs WHERE id = ? AND attempt_id = ?",
                    (int(requested_job_id), attempt_id),
                ).fetchone()
            else:
                grading_job = conn.execute(
                    "SELECT * FROM grading_jobs WHERE attempt_id = ? ORDER BY id DESC LIMIT 1",
                    (attempt_id,),
                ).fetchone()
            report_contexts = {
                row["report_id"]: row
                for row in conn.execute(
                    """
                    SELECT c.*, COALESCE(NULLIF(c.rubric_snapshot_json, '{}'), r.rubric_json, '{}') AS rubric_json
                      FROM grading_report_contexts c
                 LEFT JOIN grading_rubrics r ON r.id = c.rubric_id
                     WHERE c.report_id IN (SELECT id FROM grading_reports WHERE attempt_id = ?)
                    """,
                    (attempt_id,),
                ).fetchall()
            }
            next_href, next_label = next_question_path(conn, question)
        try:
            grading_job_options = json.loads(grading_job["options_json"] or "{}") if grading_job else {}
        except (TypeError, json.JSONDecodeError):
            grading_job_options = {}
        if "smart" in query:
            smart_grading_enabled = query.get("smart", ["1"])[0] != "0"
        else:
            smart_grading_enabled = (settings["grading_mode"] or "enhanced") == "enhanced"
        if "deep" in query:
            deep_thinking_enabled = query.get("deep", ["0"])[0] != "0"
        else:
            deep_thinking_enabled = True
        deep_thinking_explicit = ' data-preference-explicit="1"' if "deep" in query else ""
        smart_grading_checked = " checked" if smart_grading_enabled else ""
        deep_thinking_checked = " checked" if deep_thinking_enabled else ""
        attempt_duration_text = format_duration(attempt["duration_seconds"])
        paper_elapsed_text = format_duration(attempt["paper_elapsed_seconds"], "")
        timing_parts = [f"本题用时 {attempt_duration_text}"]
        if paper_elapsed_text and not attempt["paper_time_excluded"]:
            timing_parts.append(f"整卷累计 {paper_elapsed_text}")
        if attempt["paper_time_excluded"]:
            timing_parts.append("不计入套卷")
        timing_summary = " · ".join(timing_parts)
        package = build_grading_package(
            question,
            selected_refs,
            attempt,
            materials,
            custom_reference_answer,
            grading_basis,
        )
        package_basis_note = (
            "当前批改包已包含智能批改生成并经材料引文校验的 AI 评分基准。"
            if cache_info["cached"]
            else "本题尚未生成 AI 智能评分基准。批改包不会再导出本地聚类候选；Codex 会依据题目、材料和参考答案重新提炼采分点。"
        )
        refs_html = tabbed_references(refs, f"attempt-{attempt_id}")
        material_annotations = {row["material_number"]: row for row in annotations if row["target_type"] == "material"}
        annotation_by_type = {
            row["target_type"]: row for row in annotations if row["target_type"] in {"answer", "note"}
        }
        materials_html = tabbed_materials(
            relevant_materials,
            f"attempt-{attempt_id}",
            highlight_scope=f"question-{question['id']}",
            question_id=question["id"],
            saved_annotations=material_annotations,
        )
        answer_annotation_attrs = annotation_data_attributes(annotation_by_type.get("answer"))
        note_annotation_attrs = annotation_data_attributes(annotation_by_type.get("note"))
        reference_options = []
        for reference in refs:
            checked = " checked" if int(reference["id"]) in selected_ref_ids else ""
            preview = re.sub(r"\s+", " ", reference["answer_text"]).strip()
            if len(preview) > 72:
                preview = preview[:72] + "..."
            reference_options.append(f"""
              <label class="grading-reference-option">
                <input type="checkbox" name="reference_id" value="{reference["id"]}"{checked}>
                <span><strong>{esc(reference["organization"])}</strong><small>{esc(preview)}</small></span>
              </label>""")
        if not reference_options:
            reference_options.append('<p class="muted reference-empty">本题暂无现有参考答案，可在下方自行补充。</p>')
        reports_html = []
        for report in reports:
            report_return_to = grading_report_return_path(
                attempt_id,
                report["id"],
                report_parent_return,
            )
            display_report_text = normalize_revised_answer_word_count(
                report["report_text"],
                question["word_limit"] or "",
            )
            report_word_status = revised_answer_word_count_status(
                display_report_text,
                question["word_limit"] or "",
            )
            report_word_warning = ""
            report_annotation_scope = f"report-{report['id']}"
            if report_word_status["over_limit"]:
                report_word_warning = f"""
                <div class="report-preview-warning report-word-limit-warning" role="alert">
                  <div><strong>修改版答案超出字数限制 {report_word_status["over_by"]} 字</strong><span>当前 {report_word_status["actual_chars"]} 字 · 应低于 {report_word_status["max_chars"]} 字</span></div>
                </div>"""
            context_row = report_contexts.get(report["id"])
            smart_context_html = ""
            smart_score_html = ""
            result = {}
            if context_row:
                try:
                    evidence = json.loads(context_row["retrieval_json"] or "[]")
                    result = json.loads(context_row["result_json"] or "{}")
                    rubric = json.loads(context_row["rubric_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    evidence, result, rubric = [], {}, {}
                if result.get("dimension_scores"):
                    dimension_cards = []
                    for dimension in result.get("dimension_scores", []):
                        dimension_cards.append(
                            '<div class="grading-dimension-card">'
                            f"<span>{esc(dimension.get('label') or dimension.get('dimension') or '评分维度')}</span>"
                            f"<strong>{esc(format(float(dimension.get('display_score') or 0), 'g'))}"
                            f"<small>/{esc(format(float(dimension.get('display_max_score') or 0), 'g'))}</small></strong>"
                            f"<p>{esc(dimension.get('reason') or '')}</p>"
                            "</div>"
                        )
                    stale_score = result.get("score_status") == "stale"
                    score_status_html = (
                        '<span class="grading-score-stale">待重新批改</span>'
                        if stale_score
                        else '<span class="grading-score-valid">综合评分</span>'
                    )
                    smart_score_html = f"""
                    <section class="grading-score-overview{" is-stale" if stale_score else ""}">
                      <div class="grading-score-total">
                        <div>{score_status_html}<p>{esc(result.get("overall_summary") or "综合分析作答整体情况后评分。")}</p></div>
                        <strong>{esc(format(float(result.get("display_score") or 0), "g"))}<small>/{esc(format(float(result.get("display_max_score") or 100), "g"))}</small></strong>
                      </div>
                      <div class="grading-dimension-grid">{"".join(dimension_cards)}</div>
                    </section>"""
                role_labels = {
                    "current_scoring": "本题评分",
                    "method_calibration": "方法校准",
                    "personalization": "个性化提醒",
                }
                evidence_items = []
                for card in evidence:
                    title = esc(card.get("title") or card.get("evidence_id") or "本地证据")
                    try:
                        card_href = local_url(card.get("url"), return_to=report_return_to) if card.get("url") else ""
                    except ValueError:
                        card_href = ""
                    title_html = f'<a href="{esc(card_href)}">{title}</a>' if card_href else f"<strong>{title}</strong>"
                    evidence_items.append(
                        f"<li>{title_html}<span>{esc(role_labels.get(card.get('role'), card.get('role') or '辅助证据'))}</span>"
                        f"<small>{esc(card.get('snippet') or '')}</small></li>"
                    )
                if not evidence_items:
                    evidence_items.append(
                        "<li><strong>本次未使用跨题数据</strong><small>报告仍仅依据本题评分基准生成。</small></li>"
                    )
                point_by_key = {point.get("point_key"): point for point in rubric.get("points", [])}
                consensus_summary = rubric.get("consensus_summary") or {}
                retrieval_badge = " · 检索质量降级" if consensus_summary.get("degraded") else ""
                feedback_items = []
                for match in result.get("point_matches", []):
                    point = point_by_key.get(match.get("point_key")) or {}
                    coverage_percent = round(
                        float(
                            match.get(
                                "coverage_ratio",
                                0.5 if match.get("status") == "partial" else (1 if match.get("status") == "hit" else 0),
                            )
                        )
                        * 100
                    )
                    status_options = []
                    for value, label in (
                        ("hit", "命中"),
                        ("partial", "部分命中"),
                        ("miss", "未命中"),
                        ("invalid", "该点不成立"),
                    ):
                        selected = " selected" if match.get("status") == value else ""
                        status_options.append(f'<option value="{value}"{selected}>{label}</option>')
                    feedback_items.append(f"""
                    <form class="grading-point-feedback" method="post" action="/grading-reports/{report["id"]}/feedback">
                      <input type="hidden" name="point_key" value="{esc(match.get("point_key"))}">
                      <div><strong>{esc(point.get("label") or match.get("point_key"))}</strong><small>AI 覆盖判断 {coverage_percent}% · 建议权重 {esc(point.get("weight", match.get("weight", 0)))}</small></div>
                      <select name="corrected_status">{"".join(status_options)}</select>
                      <input name="corrected_quote" value="{esc(match.get("answer_quote"))}" placeholder="用户答案中的连续原句">
                      <input name="note" value="" placeholder="纠正理由（可选）">
                      <label class="check-line compact"><input type="checkbox" name="scope" value="question"> 同题后续批改也采用</label>
                      <button class="button ghost small" type="submit">保存纠正</button>
                    </form>""")
                smart_context_html = f"""
                <details class="grading-context-panel">
                  <summary>本次使用的数据 · {context_row["api_call_count"]} 次 API · {context_row["latency_ms"]} ms{retrieval_badge}</summary>
                  <ul class="grading-evidence-list">{"".join(evidence_items)}</ul>
                </details>
                <details class="grading-feedback-panel">
                  <summary>逐点纠错</summary>
                  <p class="muted">纠正后逐点判断立即更新，综合分标记为待重新批改且不再计入统计。勾选“同题后续”会让该纠正影响下一次本题基准。</p>
                  <div class="grading-feedback-list">{"".join(feedback_items)}</div>
                </details>"""
            report_source_text = report_answer_snapshot(
                report["prompt_text"],
                result,
                attempt["answer_text"],
            )
            reports_html.append(f"""
            <article class="report-card" id="report-{report["id"]}">
              <header><strong>{esc(report["provider"] or "Codex")}</strong><span>{esc(report["model"] or "手动保存")} · {esc(format_beijing_time(report["created_at"]))}</span></header>
              {report_word_warning}
              {smart_score_html}
              <div class="report-body">{markdownish(display_report_text, report_return_to, report_source_text, report_annotation_scope)}</div>
              {smart_context_html}
            </article>""")
        failed_preview = preview_report or grading_job_options.get("_failed_preview") or {}
        preview_html = ""
        if failed_preview.get("report_text"):
            preview_status = failed_preview.get("word_count_status") or {}
            preview_job_id = grading_job["id"] if grading_job else "basic"
            preview_id = f"grading-preview-{preview_job_id}"
            preview_return_to = local_url(
                f"/attempts/{attempt_id}",
                return_to=report_parent_return,
                fragment=preview_id,
            )
            preview_report_text = normalize_revised_answer_word_count(
                failed_preview.get("report_text") or "",
                question["word_limit"] or "",
            )
            over_by = nonnegative_int(preview_status.get("over_by"))
            actual_chars = nonnegative_int(preview_status.get("actual_chars"))
            hard_limit = nonnegative_int(preview_status.get("max_chars"))
            preview_html = f"""
            <article class="report-card report-preview-card" id="{esc(preview_id)}">
              <header><strong>API 批改报告</strong><span>超出字数限制</span></header>
              <div class="report-preview-warning" role="alert">
                <div><strong>修改版答案超出字数限制 {over_by} 字</strong><span>当前 {actual_chars} 字 · 应低于 {hard_limit} 字</span></div>
                <button class="button ghost small" type="button" data-preview-copy>复制报告</button>
              </div>
              <textarea data-preview-source hidden>{esc(preview_report_text)}</textarea>
              <div class="report-body">{markdownish(preview_report_text, preview_return_to, failed_preview.get("answer_snapshot") or grading_job_options.get("answer_snapshot") or attempt["answer_text"], preview_id)}</div>
            </article>"""
        if not reports_html and not preview_html:
            reports_html.append(
                f'<div class="grading-empty"><span>01</span><h3>答案已经保存，下一步开始批改</h3><p>使用当前批改设置生成报告，完成后这里会直接展示结果。</p><button class="button primary" type="submit" form="grading-reference-form" formaction="/attempts/{attempt_id}/grade" data-grade-submit{"" if settings["mode"] == "api" else " disabled"}>开始智能批改</button></div>'
            )
        api_disabled = "" if settings["mode"] == "api" else " disabled"
        active_job_html = ""
        if grading_job and grading_job["status"] in set(ACTIVE_JOB_STATUSES) | {"failed", "interrupted"}:
            payload = grading_job_payload(grading_job)
            active_job_html = f"""
              <div class="smart-grade-status {"is-error" if payload["status"] in {"failed", "interrupted"} else ""}" data-grading-job data-job-id="{payload["job_id"]}" data-job-status="{esc(payload["status"])}">
                <div><strong data-grading-job-message>{esc(payload["message"])}</strong><span data-grading-job-progress>{payload["progress"]}%</span></div>
                <progress max="100" value="{payload["progress"]}" data-grading-job-bar></progress>
                <p data-grading-job-error>{esc(payload["error"])}</p>
              </div>"""
        report_section = f"""
        <section class="inline-report-stack">
          <div class="section-heading"><div><p class="eyebrow">AI Report</p><h2>批改报告</h2></div><span>{len(reports)} 份已保存</span></div>
          {preview_html}
          {"".join(reports_html)}
        </section>"""
        more_html = f"""
          <a href="/attempts">全部作答记录</a>
          <a href="/attempts/{attempt_id}/package.md">下载批改包</a>
          <form method="post" action="/attempts/{attempt_id}/delete" data-confirm="确认删除这次作答和对应批改报告吗？"><button class="menu-danger" type="submit">删除本次作答</button></form>
        """
        body = f"""
        {workflow_header("grading", question, question=question, attempt=attempt, more_html=more_html, context_return=context_return)}
        <section class="grading-layout grading-result-first" data-resizable-attempt-pane data-resize-storage-key="gongkao.gradingPaneWidth.v2" data-default-side-width="480" data-default-side-ratio="0.42" data-min-main-width="440" data-min-side-width="360">
          <article class="grading-source">
            <section class="text-block answer-editor"><div class="section-heading"><div><h2>我的答案</h2></div><span>{esc(timing_summary)}</span></div><form method="post" action="/attempts/{attempt_id}/update" data-answer-form data-dirty-submit data-autosave-url="/attempts/{attempt_id}/update" data-word-limit="{esc(question["word_limit"])}"><textarea name="answer_text" data-answer-hidden hidden>{esc(attempt["answer_text"])}</textarea><input type="hidden" name="answer_format_json" value="{esc(attempt["answer_format_json"] or "[]")}" data-answer-format-hidden><input type="hidden" name="annotations_json" data-annotations-hidden><input type="hidden" name="annotations_text_hash" data-annotations-hash-hidden><div class="answer-editor-toolbar" data-editor-toolbar data-editor-target="#grading-answer-input"><span class="answer-editor-toolbar-label">排版</span><button type="button" class="is-active" data-editor-align="left" title="左对齐">左</button><button type="button" data-editor-align="center" title="居中">中</button><button type="button" data-editor-align="right" title="右对齐">右</button></div><div id="grading-answer-input" class="direct-text-editor" contenteditable="true" role="textbox" aria-label="我的答案" spellcheck="false" data-answer-input data-text-annotation data-paragraph-alignments="{esc(attempt["answer_format_json"] or "[]")}" data-highlight-scope="attempt-{attempt_id}" data-annotation-type="answer" data-annotation-id="{attempt_id}" data-annotation-target="answer" data-attempt-id="{attempt_id}" {answer_annotation_attrs} data-empty-text="答案为空，输入后可直接标亮和划线。">{esc(attempt["answer_text"])}</div><div class="answer-editor-footer"><div class="answer-counter" aria-live="polite"><span>当前 <strong data-word-count>{attempt["word_count"]}</strong> 字</span><span data-line-status>行数：0</span><span data-current-line-status>本行：0/25格</span><span data-word-status></span><span data-autosave-status>已保存</span></div><div class="answer-editor-actions"><button class="button ghost small" type="button" data-clear-text-annotations>清除标注</button><button class="button primary small" type="submit">立即保存</button></div></div></form></section>
            <section class="text-block attempt-note-panel">
              <div class="section-heading"><div><h2>复盘笔记</h2></div><span data-attempt-note-status>自动保存开启</span></div>
              <div class="answer-editor-toolbar" data-editor-toolbar data-editor-target="#grading-note-input"><span class="answer-editor-toolbar-label">排版</span><button type="button" class="is-active" data-editor-align="left" title="左对齐">左</button><button type="button" data-editor-align="center" title="居中">中</button><button type="button" data-editor-align="right" title="右对齐">右</button></div>
              <div id="grading-note-input" class="direct-text-editor note-editor" contenteditable="true" role="textbox" aria-label="复盘笔记" spellcheck="false" data-attempt-note-input data-save-url="/attempts/{attempt_id}/note" data-text-annotation data-highlight-scope="attempt-{attempt_id}" data-annotation-type="note" data-annotation-id="{attempt_id}" data-annotation-target="note" data-attempt-id="{attempt_id}" {note_annotation_attrs} data-empty-text="缺的要点来自材料哪里？为什么找出来？下次遇到怎么识别？">{esc(attempt["personal_note"])}</div>
              <div class="note-editor-footer"><button class="button ghost small" type="button" data-clear-text-annotations>清除标注</button></div>
            </section>
            {report_section}
            <a class="grading-next" href="{esc(next_href)}"><span>{esc(next_label)}</span><b>➜</b></a>
          </article>
          <button class="pane-resizer" type="button" aria-label="调整批改侧栏宽度" data-pane-resizer></button>
          <aside class="grading-tools" data-session-scroll="grading-tools">
            <details class="grading-context-disclosure grading-compare-panel"{' open data-default-open="1"' if reports else ""}>
              <summary>查看题目、材料与参考答案</summary>
              <div class="grading-context-body">
                <section><h2>题目</h2><p class="paper-line">第{question["question_number"] or "?"}题 · {esc(question["question_type"])}</p><div class="preline prompt-strong">{pre(question["prompt"])}</div><div class="preline">{pre(question["requirements"])}</div></section>
                <section><h2>相关材料</h2>{materials_html}</section>
                <section><h2>参考答案</h2>{refs_html}</section>
              </div>
            </details>
            <section class="tool-panel api-grade-panel grading-primary-action">
              <p class="eyebrow">Grading</p><h2>{"重新批改" if reports else "开始批改"}</h2>
              <p class="muted">模型：{esc(settings["provider_name"])} / {esc(settings["model"])}</p>
              <button class="button primary" type="submit" form="grading-reference-form" formaction="/attempts/{attempt_id}/grade" data-grade-submit{api_disabled}>{"重新生成报告" if reports else "开始智能批改"}</button>
              <div class="grade-loading" aria-live="polite"><span>生成中，请稍候</span><i></i></div>
              {active_job_html}
              {f'<form method="post" action="/questions/{question["id"]}/rubric/rebuild"><input type="hidden" name="return_to" value="/attempts/{attempt_id}"><button class="button ghost small" type="submit">重建本题基准</button></form>' if cache_info["cached"] and smart_grading_enabled else ""}
              {'<p class="warning-note">设置页切换到 API 自动模式后可用。</p>' if settings["mode"] != "api" else ""}
            </section>
            <section class="tool-panel">
              <h2>AI 训练教练</h2>
              <p class="muted">围绕本次作答生成失分拆解、修改动作和延伸训练建议。</p>
              <a class="button primary grading-review-action" href="/agent?mode=review&attempt_id={attempt_id}">复盘本题</a>
            </section>
            <details class="grading-advanced tool-panel">
              <summary>批改设置与高级工具</summary>
              <div class="grading-advanced-body">
            <section class="grading-reference-panel">
              <div class="reference-picker-heading">
                <div><h2>批改参考</h2><p class="muted">默认全选，可按本次作答单独调整。</p></div>
                <span data-reference-count>{len(selected_refs)} / {len(refs)}</span>
              </div>
              <form id="grading-reference-form" method="post" action="/attempts/{attempt_id}/grading-references" data-grading-references data-preserve-sidebar>
                <div class="smart-grading-options">
                  <label class="check-line smart-mode-option" title="使用本题不等权评分基准进行综合评分；辅助数据只用于点评建议"><input type="checkbox" name="use_smart_grading" value="1"{smart_grading_checked}> 智能批改 <small>综合评分</small></label>
                  <label class="check-line"><input type="checkbox" name="use_analogies" value="1" checked> 相似题方法</label>
                  <label class="check-line"><input type="checkbox" name="use_knowledge" value="1" checked> 教材知识</label>
                  <label class="check-line"><input type="checkbox" name="use_history" value="1" checked> 个人历史</label>
                  <label class="check-line smart-mode-option smart-depth-option" title="启用模型高级思考，批改更深入但等待时间会明显增加"><input type="checkbox" name="use_deep_thinking" value="1" data-deep-thinking-preference{deep_thinking_explicit}{deep_thinking_checked}> 深度思考 <small>较慢</small></label>
                </div>
                <p class="muted smart-grading-mode-note">取消“智能批改”即使用原基础批改；取消“深度思考”可缩短模型响应时间。</p>
                <div class="reference-picker-actions">
                  <button type="button" data-reference-select-all>全选</button>
                  <button type="button" data-reference-clear>清空</button>
                </div>
                <div class="grading-reference-list">{"".join(reference_options)}</div>
                <label class="custom-reference-field"><span>自定义参考答案</span><textarea name="custom_reference_answer" rows="5" placeholder="可选。填写后会作为额外一份参考答案送入提示词。">{esc(custom_reference_answer)}</textarea></label>
                <p class="reference-dirty-note" data-reference-dirty-note hidden>参考设置有修改。请先保存，再复制或下载手动批改包。</p>
                <button class="button primary" type="submit" data-reference-save hidden>保存参考设置</button>
              </form>
            </section>
            <section class="tool-panel">
              <h2>Codex 手动模式</h2>
              <p class="muted">复制下面的批改包发给 Codex；收到报告后粘贴保存。</p>
              <p class="package-basis-note{" is-ready" if cache_info["cached"] else ""}">{esc(package_basis_note)}</p>
              <textarea id="grading-package" class="package-text" readonly rows="12">{esc(package)}</textarea>
              <button class="button primary" type="button" data-package-copy>复制批改包</button>
              <a class="button ghost" href="/attempts/{attempt_id}/package.md" data-package-download>下载 Markdown</a>
            </section>
            <section class="tool-panel">
              <h2>粘贴报告</h2>
              <form method="post" action="/attempts/{attempt_id}/reports">
                <label><span>来源</span><input name="provider" value="Codex"></label>
                <label><span>报告正文</span><textarea name="report_text" rows="9" placeholder="把 Codex 或其他模型生成的批改报告粘贴到这里。"></textarea></label>
                <button class="button primary" type="submit">保存报告</button>
              </form>
            </section>
              </div>
            </details>
          </aside>
        </section>
        """
        self.send_html(
            layout(
                "批改工作台 - 研申",
                body,
                active_section,
                flashes,
                transient_route=citation_target,
            )
        )

    def handle_save_report(self, path):
        try:
            attempt_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        provider = form_value(data, "provider", "Codex") or "Codex"
        report_text = form_value(data, "report_text")
        if report_text:
            with connect(self.db_path) as conn:
                attempt = conn.execute(
                    """
                    SELECT a.answer_text, q.word_limit
                      FROM attempts a
                      JOIN questions q ON q.id = a.question_id
                     WHERE a.id = ?
                    """,
                    (attempt_id,),
                ).fetchone()
                report_text = normalize_revised_answer_word_count(
                    report_text,
                    attempt["word_limit"] if attempt else "",
                )
                status = revised_answer_word_count_status(
                    report_text,
                    attempt["word_limit"] if attempt else "",
                )
                if status["over_limit"]:
                    self.page_attempt_detail(
                        f"/attempts/{attempt_id}",
                        [
                            (
                                "error",
                                f"修改版答案未低于字数硬限制，至少还需压缩 {status['over_by']} 字，已拒绝保存。手工报告不会自动调用 API 返修。",
                            )
                        ],
                    )
                    return
                conn.execute(
                    """
                    INSERT INTO grading_reports (
                        attempt_id, provider, model, report_text, prompt_text, status
                    ) VALUES (?, ?, 'manual', ?, ?, 'ok')
                    """,
                    (
                        attempt_id,
                        provider,
                        report_text,
                        "本次作答：\n"
                        + json.dumps(
                            {"answer_text": attempt["answer_text"] if attempt else ""},
                            ensure_ascii=False,
                        ),
                    ),
                )
        self.redirect(f"/attempts/{attempt_id}")

    def handle_grading_references(self, path):
        try:
            attempt_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        context = self._attempt_context(attempt_id)
        if not context:
            self.send_error(404)
            return
        attempt, question, refs, materials, reports, settings, _ = context
        selected_refs, selected_ids, custom_answer = grading_references_from_form(data, refs)
        with connect(self.db_path) as conn:
            save_attempt_grading_references(conn, attempt_id, selected_ids, custom_answer)
        self.redirect(f"/attempts/{attempt_id}")

    def handle_grading_job_status(self, path):
        try:
            job_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM grading_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            self.send_json({"error": "批改任务不存在"}, status=404)
            return
        self.send_json(grading_job_payload(row))

    def handle_grading_report_feedback(self, path):
        try:
            report_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        scope = form_value(data, "scope", "report")
        try:
            with connect(self.db_path) as conn:
                report = conn.execute(
                    "SELECT gr.attempt_id, a.question_id FROM grading_reports gr JOIN attempts a ON a.id = gr.attempt_id WHERE gr.id = ?",
                    (report_id,),
                ).fetchone()
                if not report:
                    self.send_error(404)
                    return
                apply_report_feedback(
                    conn,
                    report_id,
                    form_value(data, "point_key"),
                    form_value(data, "corrected_status"),
                    form_value(data, "corrected_quote"),
                    form_value(data, "note"),
                    scope,
                )
                if scope == "question":
                    invalidate_question_rubrics(conn, report["question_id"])
                attempt_id = report["attempt_id"]
        except ValueError as exc:
            self.page_attempt_detail(
                f"/attempts/{report['attempt_id']}" if "report" in locals() and report else "/attempts",
                [("error", str(exc))],
            )
            return
        self.redirect(f"/attempts/{attempt_id}#report-{report_id}")

    def handle_rubric_rebuild(self, path):
        try:
            question_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        return_to = return_path_from_form(data, f"/questions/{question_id}")
        with connect(self.db_path) as conn:
            if not conn.execute("SELECT 1 FROM questions WHERE id = ?", (question_id,)).fetchone():
                self.send_error(404)
                return
            invalidate_question_rubrics(conn, question_id)
        self.redirect(return_to)

    def handle_api_grade(self, path):
        try:
            attempt_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(data, keep_blank_values=True)
        if "answer_text" in form:
            answer_text = form.get("answer_text", [""])[0]
            answer_format_json = normalize_answer_format_json(
                form.get("answer_format_json", ["[]"])[0], answer_text
            )
            with connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE attempts
                       SET answer_text = ?, answer_format_json = ?, word_count = ?
                     WHERE id = ?
                    """,
                    (answer_text, answer_format_json, count_cjk_chars(answer_text), attempt_id),
                )
        context = self._attempt_context(attempt_id)
        if not context:
            self.send_error(404)
            return
        attempt, question, refs, materials, reports, settings, _ = context
        if settings["mode"] != "api":
            self.page_attempt_detail(
                f"/attempts/{attempt_id}", [("error", "当前不是 API 自动模式，请先在设置页切换。")]
            )
            return
        if data:
            selected_refs, selected_ids, custom_answer = grading_references_from_form(data, refs)
            with connect(self.db_path) as conn:
                save_attempt_grading_references(conn, attempt_id, selected_ids, custom_answer)
        else:
            selected_refs, selected_ids, custom_answer = attempt_grading_references(attempt, refs)
        if data:
            use_smart_grading = "use_smart_grading" in form
            use_deep_thinking = "use_deep_thinking" in form
        else:
            use_smart_grading = (settings["grading_mode"] or "enhanced") == "enhanced"
            use_deep_thinking = False
        if use_smart_grading:
            options = {
                "analogies": "use_analogies" in form if data else True,
                "knowledge": "use_knowledge" in form if data else True,
                "history": "use_history" in form if data else True,
                "deep_thinking": use_deep_thinking,
            }
            with connect(self.db_path) as conn:
                fresh_attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                fresh_settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                job, created = create_grading_job(
                    conn,
                    fresh_attempt,
                    fresh_settings,
                    selected_ids,
                    custom_answer,
                    options,
                )
            if created:
                start_grading_job(self.db_path, job["id"], chat_completion)
            self.redirect(f"/attempts/{attempt_id}?grading_job={job['id']}")
            return
        prompt = build_ai_prompt(
            question,
            selected_refs,
            attempt,
            materials,
            custom_answer,
        )
        try:
            report_text, raw = chat_completion(
                settings,
                prompt,
                {"thinking": "enabled" if use_deep_thinking else "disabled"},
            )
            report_text = normalize_revised_answer_word_count(
                report_text,
                question["word_limit"] or "",
            )
            stored_raw = raw
            status = revised_answer_word_count_status(
                report_text,
                question["word_limit"] or "",
            )
            if status["over_limit"]:
                retry_prompt = build_revised_answer_retry_prompt(
                    prompt,
                    report_text,
                    question["word_limit"] or "",
                )
                repair_response, repair_raw = chat_completion(
                    settings,
                    retry_prompt,
                    {"thinking": "disabled"},
                )
                repaired_answer = parse_revised_answer_repair(repair_response)
                stored_raw = f"{raw}\n\n--- localized revised-answer repair ---\n{repair_raw}"
                if repaired_answer:
                    report_text = replace_revised_answer_body(
                        report_text,
                        repaired_answer,
                        question["word_limit"] or "",
                    )
        except (AiConfigError, AiRequestError) as exc:
            self.page_attempt_detail(f"/attempts/{attempt_id}", [("error", str(exc))])
            return
        status = revised_answer_word_count_status(
            report_text,
            question["word_limit"] or "",
        )
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO grading_reports (
                    attempt_id, provider, model, report_text, prompt_text, raw_response, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'ok')
                """,
                (attempt_id, settings["provider_name"], settings["model"], report_text, prompt, stored_raw),
            )
            report_id = cursor.lastrowid
        self.redirect(
            local_url(
                f"/attempts/{attempt_id}",
                fragment=f"report-{report_id}",
                smart=0,
                deep=1 if use_deep_thinking else 0,
            )
        )

"""Settings, import, export, and local-record management controllers."""

from ..runtime import (
    back_link,
    cgi,
    connect,
    create_import_record,
    esc,
    export_personal_data,
    finish_import_record,
    format_beijing_time,
    import_answers,
    import_personal_data,
    import_questions,
    json,
    layout,
    logging,
    masked_key,
    os,
    parse_qs,
    settings_credits,
    user_data_dir,
)


class SettingsController:
    def _agent_index_status(self):
        with connect(self.db_path) as conn:
            state = conn.execute(
                "SELECT * FROM agent_context_worker_state WHERE id = 1"
            ).fetchone()
            index_state = conn.execute(
                "SELECT dirty, full_rebuild, rebuilt_at FROM agent_context_index_state WHERE id = 1"
            ).fetchone()
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM agent_context_pending WHERE status = 'pending'"
            ).fetchone()[0]
            failed_count = conn.execute(
                "SELECT COUNT(*) FROM agent_context_pending WHERE status = 'failed'"
            ).fetchone()[0]
            failed_rows = conn.execute(
                """
                SELECT source_type, source_id, retry_count, last_error
                  FROM agent_context_pending
                 WHERE status = 'failed'
              ORDER BY queued_at DESC
                 LIMIT 3
                """
            ).fetchall()

        raw_status = (state["status"] if state else "idle") or "idle"
        if index_state and index_state["full_rebuild"] and raw_status == "idle":
            raw_status = "queued_rebuild"
        elif pending_count and raw_status == "idle":
            raw_status = "queued"
        labels = {
            "idle": "索引已就绪",
            "queued": "等待后台整理",
            "queued_rebuild": "等待完整重建",
            "running": "正在增量整理",
            "retrying": "等待重试",
            "rebuilding": "正在完整重建",
            "completed_with_errors": "已完成，部分任务失败",
            "failed": "索引任务异常",
        }
        type_labels = {
            "": "空闲",
            "attempt": "个人作答",
            "grading_report": "批改报告",
            "question": "题目",
            "material": "材料",
            "reference_answer": "参考答案",
            "full_rebuild": "完整索引",
        }
        processed = int(state["processed_count"] or 0) if state else 0
        total = int(state["total_count"] or 0) if state else 0
        if pending_count and total < processed + pending_count:
            total = processed + pending_count
        progress = round(min(100, processed * 100 / total)) if total else (100 if not pending_count else 0)
        failed = [
            {
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "retry_count": int(row["retry_count"] or 0),
                "last_error": row["last_error"] or "未知错误",
            }
            for row in failed_rows
        ]
        return {
            "status": raw_status,
            "status_label": labels.get(raw_status, "后台整理中"),
            "current_type": type_labels.get((state["current_type"] if state else "") or "", "索引任务"),
            "processed_count": processed,
            "total_count": total,
            "pending_count": int(pending_count),
            "failed_count": int(failed_count),
            "progress": progress,
            "last_error": (state["last_error"] if state else "") or "",
            "updated_at": format_beijing_time(state["updated_at"]) if state and state["updated_at"] else "尚未运行",
            "rebuilt_at": format_beijing_time(index_state["rebuilt_at"]) if index_state and index_state["rebuilt_at"] else "尚未完成",
            "failed_tasks": failed,
        }

    def handle_settings_index_status(self):
        self.send_json(self._agent_index_status())

    def page_import(self, flashes=None):
        with connect(self.db_path) as conn:
            imports = conn.execute("SELECT * FROM imports ORDER BY imported_at DESC, id DESC LIMIT 20").fetchall()
        rows = []
        for item in imports:
            errors = json.loads(item["errors_json"])
            rows.append(f"""
            <tr><td>{esc(format_beijing_time(item["imported_at"]))}</td><td>{esc(item["filename"])}</td>
            <td><span class="status {esc(item["status"])}">{esc(item["status"])}</span></td>
            <td>{item["question_count"]}</td><td>{item["answer_count"]}</td><td>{len(errors)}</td></tr>""")
        if not rows:
            rows.append('<tr><td colspan="6" class="muted">暂无导入记录。</td></tr>')
        body = f"""
        <section class="page-head">
          <div><p class="eyebrow">Import</p><h1>表格导入</h1></div>
          <div class="actions">
            <a class="button ghost" href="/templates/questions_template.csv">题目模板</a>
            <a class="button ghost" href="/templates/answers_template.csv">答案模板</a>
            <a class="button ghost" href="/templates/sample_questions.csv">题目 CSV 模板</a>
            <a class="button ghost" href="/templates/sample_answers.csv">答案 CSV 模板</a>
          </div>
        </section>
        <section class="import-panel">
          <form method="post" enctype="multipart/form-data">
            <label class="file-field"><span>题目表 CSV/XLSX</span><input type="file" name="question_file" accept=".csv,.xlsx"></label>
            <label class="file-field"><span>答案表 CSV/XLSX</span><input type="file" name="answer_file" accept=".csv,.xlsx"></label>
            <button class="button primary" type="submit">导入到本地题库</button>
          </form>
          <div class="template-notes"><h2>导入规则</h2><p>题目用“题目编号”作为稳定主键；答案表用“题目编号 + 来源名”关联。同编号题目会更新，同题同来源答案会更新。</p><p>建议先导入题目表，再导入答案表；也可以一次同时上传两张表。</p></div>
        </section>
        <section class="table-panel"><h2>最近导入</h2><table><thead><tr><th>时间</th><th>文件</th><th>状态</th><th>题目</th><th>答案</th><th>错误</th></tr></thead><tbody>{"".join(rows)}</tbody></table></section>
        """
        self.send_html(layout("导入 - 研申", body, "import", flashes))

    def handle_import(self):
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")},
        )
        question_file = (
            form["question_file"]
            if "question_file" in form and getattr(form["question_file"], "filename", "")
            else None
        )
        answer_file = (
            form["answer_file"] if "answer_file" in form and getattr(form["answer_file"], "filename", "") else None
        )
        if question_file is None and answer_file is None:
            self.page_import([("error", "请至少选择一个题目表或答案表。")])
            return
        display_name = " + ".join(field.filename for field in [question_file, answer_file] if field is not None)
        errors, flashes = [], []
        question_total = answer_total = 0
        with connect(self.db_path) as conn:
            import_id = create_import_record(conn, display_name)
            if question_file is not None:
                result = import_questions(conn, question_file, import_id)
                question_total = result["imported"] + result["updated"]
                errors.extend(result["errors"])
                flashes.append(("success", f"题目导入：新增 {result['imported']}，更新 {result['updated']}。"))
            if answer_file is not None:
                result = import_answers(conn, answer_file, import_id)
                answer_total = result["imported"] + result["updated"]
                errors.extend(result["errors"])
                flashes.append(("success", f"答案导入：新增 {result['imported']}，更新 {result['updated']}。"))
            finish_import_record(conn, import_id, "error" if errors else "ok", errors, question_total, answer_total)
        flashes.extend(("error", error) for error in errors[:8])
        if len(errors) > 8:
            flashes.append(("error", f"还有 {len(errors) - 8} 条错误，请在导入记录中查看。"))
        self.page_import(flashes)

    def page_settings(self, flashes=None):
        with connect(self.db_path) as conn:
            settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
            agent_settings = conn.execute("SELECT * FROM agent_ai_settings WHERE id = 1").fetchone()
        mode_codex = " checked" if settings["mode"] == "codex" else ""
        mode_api = " checked" if settings["mode"] == "api" else ""
        grading_enhanced = " checked" if (settings["grading_mode"] or "enhanced") == "enhanced" else ""
        grading_basic = " checked" if settings["grading_mode"] == "basic" else ""
        agent_inherits_grading = " checked" if agent_settings["use_grading_api"] else ""
        agent_uses_custom = " checked" if not agent_settings["use_grading_api"] else ""
        key_status = masked_key(settings["api_key"]) or f"环境变量：{esc(settings['api_key_env'] or '未设置')}"
        agent_key_status = masked_key(agent_settings["api_key"]) or (
            f"环境变量：{esc(agent_settings['api_key_env'] or '未设置')}"
        )
        body = f"""
        <section class="page-head settings-page-head">
          <div><p class="eyebrow">Local Settings</p><h1>设置</h1><p>管理显示方式、本地数据与智能批改连接。</p></div>
          {settings_credits()}
        </section>
        <section class="settings-layout">
          <div class="settings-column settings-main-column">
            <form class="settings-panel settings-ai-panel" method="post">
            <div class="settings-section-heading"><span>01</span><div><h2>AI 批改与教练</h2><p>配置批改流程以及 AI 教练使用的模型服务。</p></div></div>
            <div class="mode-grid">
              <label class="mode-card"><input type="radio" name="mode" value="codex"{mode_codex}><strong>Codex 手动模式</strong><span>生成批改包并复制到 Codex，报告可粘贴回来保存。</span></label>
              <label class="mode-card"><input type="radio" name="mode" value="api"{mode_api}><strong>API 自动模式</strong><span>调用 OpenAI-compatible 接口自动生成报告。</span></label>
            </div>
            <h3>API 批改能力</h3>
            <div class="mode-grid grading-mode-grid">
              <label class="mode-card"><input type="radio" name="grading_mode" value="enhanced"{grading_enhanced}><strong>智能批改（推荐）</strong><span>综合本题材料与机构共识评分，并结合历史提供改进建议。</span></label>
              <label class="mode-card"><input type="radio" name="grading_mode" value="basic"{grading_basic}><strong>基础批改</strong><span>保留原有当前题直接批改链路，作为故障回退。</span></label>
            </div>
            <div class="settings-fields">
              <label><span>服务商名称</span><input name="provider_name" value="{esc(settings["provider_name"])}" placeholder="DeepSeek"></label>
              <label><span>Base URL</span><input name="api_base_url" value="{esc(settings["api_base_url"])}" placeholder="https://api.deepseek.com"></label>
              <label><span>模型名</span><input name="model" value="{esc(settings["model"])}" placeholder="deepseek-v4-pro"></label>
              <label><span>Temperature</span><input name="temperature" value="{esc(settings["temperature"])}" inputmode="decimal"></label>
              <label><span>API Key 环境变量</span><input name="api_key_env" value="{esc(settings["api_key_env"])}" placeholder="DEEPSEEK_API_KEY"></label>
              <label><span>API Key</span><input name="api_key" value="" autocomplete="off" placeholder="{esc(key_status)}"></label>
            </div>
            <label class="check-line"><input type="checkbox" name="clear_api_key" value="1"> 清除已保存的 API Key</label>
            <p class="warning-note">API 自动模式会把题目、整卷材料、参考答案和你的答案发送给你配置的模型服务。</p>
            <section class="coach-api-settings" id="ai-coach-settings" data-coach-api-settings>
              <div class="settings-subheading">
                <div><h3>AI 教练连接</h3><p>选择复用批改连接，或为教练单独指定模型。</p></div>
              </div>
              <div class="mode-grid coach-connection-grid" role="radiogroup" aria-label="AI 教练连接方式">
                <label class="mode-card"><input type="radio" name="agent_connection_mode" value="inherit"{agent_inherits_grading}><strong>沿用批改 API</strong><span>教练直接使用上方的服务商、模型和密钥。</span></label>
                <label class="mode-card"><input type="radio" name="agent_connection_mode" value="custom"{agent_uses_custom}><strong>单独设置</strong><span>为教练配置另一套 OpenAI-compatible 服务。</span></label>
              </div>
              <div class="coach-api-collapsible" data-coach-api-fields aria-hidden="true">
                <div class="coach-api-collapsible-inner">
                  <div class="settings-fields">
                    <label><span>服务商名称</span><input name="agent_provider_name" value="{esc(agent_settings["provider_name"])}" placeholder="DeepSeek"></label>
                    <label><span>Base URL</span><input name="agent_api_base_url" value="{esc(agent_settings["api_base_url"])}" placeholder="https://api.deepseek.com"></label>
                    <label><span>模型名</span><input name="agent_model" value="{esc(agent_settings["model"])}" placeholder="deepseek-v4-pro"></label>
                    <label><span>Temperature</span><input name="agent_temperature" value="{esc(agent_settings["temperature"])}" inputmode="decimal"></label>
                    <label><span>API Key 环境变量</span><input name="agent_api_key_env" value="{esc(agent_settings["api_key_env"])}" placeholder="DEEPSEEK_API_KEY"></label>
                    <label><span>API Key</span><input name="agent_api_key" value="" autocomplete="off" placeholder="{esc(agent_key_status)}"></label>
                  </div>
                  <label class="check-line"><input type="checkbox" name="clear_agent_api_key" value="1"> 清除已保存的教练 API Key</label>
                </div>
              </div>
            </section>
            <button class="button primary" type="submit">保存设置</button>
            </form>
          </div>

          <div class="settings-column settings-side-column">
          <section class="settings-panel settings-data-panel">
            <div class="settings-section-heading"><span>02</span><div><h2>数据管理</h2><p>题库维护、个人记录备份与本地数据清理。</p></div></div>
            <div class="settings-data-grid">
              <div class="settings-block settings-block-inline">
                <div><h3>资料维护</h3><p class="muted">导入新的题目与参考答案。</p></div>
                <a class="button primary" href="/import">导入题目/答案</a>
              </div>
              <form class="settings-block settings-block-inline settings-export" method="get" action="/settings/export">
                <div><h3>导出记录</h3><label class="check-line"><input type="checkbox" name="include_api_key" value="1"> 包含 API Key</label></div>
                <button class="button primary" type="submit">导出记录/配置</button>
              </form>
              <form class="settings-block settings-block-inline settings-import" method="post" action="/settings/import" enctype="multipart/form-data">
                <div><h3>导入记录</h3><label class="file-picker"><span class="file-label">备份 JSON</span><input type="file" name="backup_file" accept="application/json,.json" data-file-input><span class="file-picker-row"><span class="file-picker-button">选择文件</span><span class="file-picker-name" data-file-name>未选择文件</span></span></label></div>
                <button class="button primary settings-import-submit" type="submit">导入并合并</button>
              </form>
              <div class="settings-block settings-block-inline">
                <div><h3>管理本地记录</h3><p class="muted">查看保存位置或选择性清理。</p></div>
                <a class="button secondary" href="/settings/local-records">进入记录管理</a>
              </div>
            </div>
          </section>
          <section class="settings-panel settings-restore-panel" data-startup-restore-settings>
            <div class="settings-section-heading"><span>03</span><div><h2>启动与恢复</h2><p>重新打开应用时，继续上次离开的位置。</p></div></div>
            <div class="settings-toggle-list">
              <label class="settings-toggle-row">
                <span><strong>恢复上次界面</strong><small>启动后直接回到上次浏览的页面。</small></span>
                <input type="checkbox" data-startup-restore="last-page">
              </label>
              <label class="settings-toggle-row">
                <span><strong>恢复滚动位置</strong><small>保留页面和批改工具栏的阅读位置。</small></span>
                <input type="checkbox" data-startup-restore="scroll">
              </label>
            </div>
          </section>
          <section class="settings-panel display-settings" data-display-settings>
            <div class="settings-section-heading"><span>04</span><div><h2>显示与缩放</h2><p>全屏自动适配屏幕；按 Esc 返回窗口模式。</p></div></div>
            <div class="display-profile-grid" role="group" aria-label="窗口显示方式">
              <button class="display-profile-card" type="button" data-display-profile="fullscreen"><strong>全屏模式</strong><small>自动识别分辨率</small></button>
              <button class="display-profile-card" type="button" data-display-profile="window"><strong>窗口模式</strong><small>保持当前窗口大小</small></button>
            </div>
            <div class="zoom-setting">
              <div><strong>界面大小</strong></div>
              <div class="zoom-stepper" role="group" aria-label="调整界面大小">
                <button type="button" data-ui-zoom-adjust="-1" aria-label="缩小界面">−</button>
                <output data-ui-zoom-value>100%</output>
                <button type="button" data-ui-zoom-adjust="1" aria-label="放大界面">+</button>
              </div>
              <p>也可使用 Ctrl +、Ctrl −、Ctrl 0 调整。</p>
            </div>
          </section>
          </div>
        </section>
        """
        self.send_html(layout("设置 - 研申", body, "settings", flashes))

    def page_settings_local_records(self, flashes=None, clear_browser_state=False):
        with connect(self.db_path) as conn:
            local_counts = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM attempts) AS attempts,
                  (SELECT COUNT(*) FROM grading_reports) AS reports,
                  (SELECT COUNT(*) FROM text_annotations) AS annotations,
                  (SELECT COUNT(*) FROM question_favorites) + (SELECT COUNT(*) FROM paper_favorites) AS favorites,
                  (SELECT COUNT(*) FROM agent_conversations) AS conversations,
                  (SELECT COUNT(*) FROM agent_messages) AS messages,
                  (SELECT COUNT(*) FROM agent_runs) AS runs,
                  (SELECT COUNT(*) FROM training_plan_items) AS plans
                """
            ).fetchone()
        clear_browser_marker = "<span data-clear-local-record-state hidden></span>" if clear_browser_state else ""
        record_directory = user_data_dir()
        body = f"""
        {clear_browser_marker}
        <section class="page-head local-record-page-head">
          <div><p class="eyebrow">Local Records</p><h1>管理本地记录</h1><p>只管理个人使用数据，不会改动题库、材料、参考答案或 API 配置。</p></div>
          <div class="settings-head-actions">{settings_credits()}{back_link("/settings", "返回设置")}</div>
        </section>
        <section class="local-record-page">
          <div class="record-location-card">
            <div><span>记录保存位置</span><code>{esc(str(record_directory))}</code></div>
            <form method="post" action="/settings/local-records/open">
              <button class="button secondary" type="submit">打开记录位置</button>
            </form>
          </div>
          <form class="local-record-manager" method="post" action="/settings/local-records/clear" data-confirm="确认清理选中的本地记录吗？此操作无法撤销，建议先导出备份。">
            <div class="local-record-heading">
              <div><h2>选择要清理的记录</h2><p>可多选。批量删除不可撤回，建议先在设置页导出备份。</p></div>
              <span>危险操作</span>
            </div>
            <div class="local-record-options">
              <label class="local-record-option"><input type="checkbox" name="record_scope" value="attempts"><span><strong>作答与批改</strong><small>{local_counts["attempts"]} 次作答 · {local_counts["reports"]} 份报告</small></span></label>
              <label class="local-record-option"><input type="checkbox" name="record_scope" value="annotations"><span><strong>全部标注记录</strong><small>{local_counts["annotations"]} 组；批量清理不可撤回</small></span></label>
              <label class="local-record-option"><input type="checkbox" name="record_scope" value="favorites"><span><strong>收藏记录</strong><small>{local_counts["favorites"]} 个题目或试卷收藏</small></span></label>
              <label class="local-record-option"><input type="checkbox" name="record_scope" value="agent"><span><strong>AI 教练记录</strong><small>{local_counts["conversations"]} 条线程 · {local_counts["messages"]} 条消息 · {local_counts["runs"]} 次运行</small></span></label>
              <label class="local-record-option"><input type="checkbox" name="record_scope" value="plans"><span><strong>训练计划</strong><small>{local_counts["plans"]} 个计划项</small></span></label>
              <label class="local-record-option"><input type="checkbox" name="record_scope" value="browser"><span><strong>界面记忆与本地草稿</strong><small>筛选、滚动位置、草稿、计时、显示预设和面板宽度</small></span></label>
            </div>
            <button class="button danger" type="submit">清理选中记录</button>
          </form>
        </section>
        """
        self.send_html(layout("管理本地记录 - 研申", body, "settings", flashes))

    def handle_settings(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        form = parse_qs(data)
        with connect(self.db_path) as conn:
            current = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
            current_agent = conn.execute("SELECT * FROM agent_ai_settings WHERE id = 1").fetchone()
            api_key = form.get("api_key", [""])[0].strip()
            if form.get("clear_api_key", [""])[0] == "1":
                api_key = ""
            elif not api_key:
                api_key = current["api_key"]
            agent_api_key = form.get("agent_api_key", [""])[0].strip()
            if form.get("clear_agent_api_key", [""])[0] == "1":
                agent_api_key = ""
            elif not agent_api_key:
                agent_api_key = current_agent["api_key"]
            try:
                temperature = float(form.get("temperature", ["0.2"])[0] or 0.2)
            except ValueError:
                temperature = 0.2
            try:
                agent_temperature = float(
                    form.get("agent_temperature", [str(current_agent["temperature"])])[0] or 0.2
                )
            except ValueError:
                agent_temperature = 0.2
            conn.execute(
                """
                UPDATE ai_settings
                   SET mode = ?, provider_name = ?, api_base_url = ?, api_key = ?,
                       api_key_env = ?, model = ?, temperature = ?, grading_mode = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = 1
                """,
                (
                    form.get("mode", ["api"])[0],
                    form.get("provider_name", ["DeepSeek"])[0].strip() or "DeepSeek",
                    form.get("api_base_url", ["https://api.deepseek.com"])[0].strip() or "https://api.deepseek.com",
                    api_key,
                    form.get("api_key_env", ["DEEPSEEK_API_KEY"])[0].strip(),
                    form.get("model", ["deepseek-v4-pro"])[0].strip() or "deepseek-v4-pro",
                    temperature,
                    form.get("grading_mode", ["enhanced"])[0]
                    if form.get("grading_mode", ["enhanced"])[0] in {"enhanced", "basic"}
                    else "enhanced",
                ),
            )
            conn.execute(
                """
                UPDATE agent_ai_settings
                   SET use_grading_api = ?, provider_name = ?, api_base_url = ?, api_key = ?,
                       api_key_env = ?, model = ?, temperature = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = 1
                """,
                (
                    0 if form.get("agent_connection_mode", ["inherit"])[0] == "custom" else 1,
                    form.get("agent_provider_name", [current_agent["provider_name"]])[0].strip()
                    or "DeepSeek",
                    form.get("agent_api_base_url", [current_agent["api_base_url"]])[0].strip()
                    or "https://api.deepseek.com",
                    agent_api_key,
                    form.get("agent_api_key_env", [current_agent["api_key_env"]])[0].strip(),
                    form.get("agent_model", [current_agent["model"]])[0].strip() or "deepseek-v4-pro",
                    agent_temperature,
                ),
            )
        self.page_settings([("success", "设置已保存。")])

    def handle_settings_local_records_clear(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(data)
        allowed = {"attempts", "annotations", "favorites", "agent", "plans", "browser"}
        scopes = {value for value in form.get("record_scope", []) if value in allowed}
        if not scopes:
            self.page_settings_local_records([("error", "请至少选择一类要清理的本地记录。")])
            return

        labels = []
        with connect(self.db_path) as conn:
            if "attempts" in scopes:
                conn.execute("DELETE FROM attempts")
                labels.append("作答与批改")
            if "annotations" in scopes:
                conn.execute("DELETE FROM text_annotations")
                labels.append("标注")
            if "favorites" in scopes:
                conn.execute("DELETE FROM question_favorites")
                conn.execute("DELETE FROM paper_favorites")
                labels.append("收藏")
            if "agent" in scopes:
                conn.execute("DELETE FROM agent_conversations")
                conn.execute("DELETE FROM agent_runs")
                labels.append("AI 教练记录")
            if "plans" in scopes:
                conn.execute("DELETE FROM training_plan_items")
                labels.append("训练计划")
        if "browser" in scopes:
            labels.append("界面记忆与本地草稿")
        self.page_settings_local_records(
            [("success", f"已清理：{'、'.join(labels)}。")],
            clear_browser_state="browser" in scopes,
        )

    def handle_settings_local_records_open(self):
        record_directory = user_data_dir()
        record_directory.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(record_directory))
        except (AttributeError, OSError) as exc:
            logging.warning("Could not open local record directory %s: %s", record_directory, exc)
            self.page_settings_local_records([("error", f"无法打开记录位置：{record_directory}")])
            return
        self.page_settings_local_records([("success", "已打开本地记录位置。")])

    def handle_settings_export(self, query):
        include_api_key = query.get("include_api_key", [""])[0] == "1"
        with connect(self.db_path) as conn:
            payload = export_personal_data(conn, include_api_key=include_api_key)
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        self.send_text(content, "gongkao-personal-backup.json", "application/json; charset=utf-8")

    def handle_settings_import(self):
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")},
        )
        backup = form["backup_file"] if "backup_file" in form and getattr(form["backup_file"], "filename", "") else None
        if backup is None:
            self.page_settings([("error", "请选择要导入的备份 JSON 文件。")])
            return
        try:
            raw = backup.file.read()
            payload = json.loads(raw.decode("utf-8-sig"))
            with connect(self.db_path) as conn:
                counts = import_personal_data(conn, payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.page_settings([("error", f"导入失败：{exc}")])
            return
        message = "导入完成：作答 {attempts} 条，报告 {reports} 份，标注 {annotations} 组，对话 {conversations} 次，消息 {messages} 条，计划项 {training_plan_items} 个，配置 {settings} 项。".format(
            **counts
        )
        skipped = (
            counts.get("skipped_attempts", 0)
            + counts.get("skipped_reports", 0)
            + counts.get("skipped_question_favorites", 0)
            + counts.get("skipped_paper_favorites", 0)
            + counts.get("skipped_annotations", 0)
            + counts.get("skipped_conversations", 0)
            + counts.get("skipped_messages", 0)
            + counts.get("skipped_training_plan_items", 0)
        )
        if skipped:
            message += " 另有 {skipped_attempts} 条作答、{skipped_reports} 份报告、{skipped_annotations} 组标注、{skipped_conversations} 次对话、{skipped_messages} 条消息、{skipped_training_plan_items} 个计划项因目标无法匹配被跳过。".format(
                **counts
            )
        self.page_settings([("success", message)])

"""AI coach workspace controllers."""

import time

from ...ai_config import load_effective_agent_settings
from ..runtime import (
    MODULES,
    AgentRunError,
    AiConfigError,
    AiRequestError,
    _cleanup_orphaned_pending_messages,
    _int_or_none,
    active_memories,
    add_feedback,
    agent_response_html,
    agent_summary_for_display,
    back_link,
    chat_completion,
    clear_memories,
    connect,
    conversation_payload,
    delete_conversation,
    delete_memory,
    esc,
    first_screen_judgement,
    form_value,
    format_beijing_time,
    get_feedback,
    get_run,
    get_run_steps,
    json,
    latest_eval_results,
    layout,
    load_knowledge_items,
    logging,
    module_definition,
    option_list,
    parse_qs,
    re,
    recent_conversations,
    recent_runs,
    render_agent_message_row,
    render_agent_pending_status,
    render_agent_rag_panel,
    render_agent_thread_list,
    resolve_api_key,
    return_path_from_form,
    return_path_from_query,
    run_agent,
    run_eval_suite,
    select_options,
    sort_question_types,
    sort_regions,
    start_or_continue_chat_async,
    valid_module_id,
)


class AgentController:
    def page_agent(self, query, flashes=None):
        mode = query.get("mode", [""])[0].strip()
        attempt_id = query.get("attempt_id", [""])[0].strip()
        selected_module = valid_module_id(query.get("module", ["overview"])[0].strip())
        visible_modules = ("overview", "summary", "analysis", "countermeasure", "document", "essay")
        if selected_module not in visible_modules:
            selected_module = "overview"
        selected_definition = module_definition(selected_module)
        with connect(self.db_path) as conn:
            settings = load_effective_agent_settings(conn)
            judgement = first_screen_judgement(conn)
            conversations = recent_conversations(conn, 50)
            runs = recent_runs(conn, 4)
            question_types = sort_question_types(
                [row["question_type"] for row in conn.execute("SELECT DISTINCT question_type FROM questions")]
            )
            regions = sort_regions([row["region"] for row in conn.execute("SELECT DISTINCT region FROM questions")])
        default_question_type = selected_definition["question_type"]
        type_options = option_list(question_types, default_question_type)
        region_options = option_list(regions, "")
        status_options = select_options(
            [
                ("", "不限"),
                ("unattempted", "未作答"),
                ("attempted", "已作答"),
                ("ungraded", "未批改"),
                ("graded", "已批改"),
            ],
            "",
        )
        conversation_rows = render_agent_thread_list(conversations)
        run_rows = []
        for run in runs:
            run_summary = agent_summary_for_display(run["input_summary"] or run["user_goal"] or "尚无摘要")
            run_rows.append(f"""
            <article class="agent-run-row">
              <a href="/agent/runs/{run["id"]}"><strong>{esc(run["task_type"])}</strong><span>{esc(run["status"])} · {esc(format_beijing_time(run["created_at"]))}</span></a>
              <p>{esc(run_summary)}</p>
            </article>""")
        if not run_rows:
            run_rows.append('<p class="muted">还没有 AI 教练运行记录。</p>')
        mode_label = "AI 教练已就绪" if settings and resolve_api_key(settings) else "连接模型后可用"
        summary = judgement["context"]["summary"]
        type_context = "".join(
            f"""
            <div class="agent-context-row">
              <span>{esc(item["name"])}</span>
              <strong>{item["questions"]} / {item["total"]}</strong>
            </div>"""
            for item in judgement["context"]["type_stats"]
        )
        module_tabs = "".join(
            f'<a class="agent-module-tab{" active" if module_id == selected_module else ""}" href="/agent?module={esc(module_id)}">'
            f"<strong>{esc(definition['label'])}</strong><span>{esc(definition['focus'])}</span></a>"
            for module_id, definition in MODULES.items()
            if module_id in visible_modules
        )
        module_workbench = f"""
        <section class="agent-module-workbench">
          <div class="agent-module-tabs">{module_tabs}</div>
        </section>"""
        current_attempt_review = ""
        composer_hidden_scope = ""
        composer_context_note = f"当前素材：{esc(selected_definition['label'])}"
        composer_placeholder = (
            f"自由提问：{esc(selected_definition['label'])}我的缺点是什么？最大失分点在哪里？下一步怎么改？"
        )
        if mode == "review" and attempt_id:
            composer_hidden_scope = f"""
              <input type="hidden" name="entrypoint" value="recent_review">
              <input type="hidden" name="attempt_id" value="{esc(attempt_id)}">"""
            composer_context_note = "当前上下文：本题复盘"
            composer_placeholder = "针对这次作答提问：这个结构能不能这样写？最大失分点在哪？下一版怎么改？"
            current_attempt_review = f"""
            <form method="post" action="/agent/conversations" class="agent-inline-action">
              <input type="hidden" name="entrypoint" value="recent_review">
              <input type="hidden" name="attempt_id" value="{esc(attempt_id)}">
              <input type="hidden" name="message" value="复盘本题">
              <button class="agent-review-button" type="submit">
                <strong>复盘本题</strong>
                <span>已带入当前作答、题目材料和批改报告</span>
              </button>
            </form>"""
        body = f"""
        <section class="agent-workspace">
          <aside class="agent-thread-rail" aria-label="教练线程">
            <div class="agent-rail-head">
              <div><p class="eyebrow">AI Coach</p><h1>AI 训练教练</h1></div>
              <a class="button small primary" href="/agent">新咨询</a>
            </div>
            <div class="agent-thread-list">{conversation_rows}</div>
          </aside>
          <article class="agent-main">
            <header class="agent-main-head">
              <div>
                <span class="agent-mode-pill">{esc(mode_label)}</span>
                <h2>{esc(judgement["lead"])}</h2>
                <p>可以先选一个模块帮助 AI 聚焦，也可以直接输入你想问的问题。</p>
              </div>
            </header>
            <section class="agent-message-stream">
              {module_workbench}
              {current_attempt_review}
            </section>
            <form class="agent-composer" method="post" action="/agent/conversations" data-agent-composer>
              <input type="hidden" name="module" value="{esc(selected_module)}">
              {composer_hidden_scope}
              <details class="agent-scope-settings">
                <summary>详细筛选</summary>
                <div class="agent-scope-grid">
                  <label><span>题型</span><select name="question_type">{type_options}</select></label>
                  <label><span>地区</span><select name="region">{region_options}</select></label>
                  <label><span>作答状态</span><select name="work_status">{status_options}</select></label>
                  <label><span>关键词</span><input name="q" placeholder="题目、试卷或训练主题"></label>
                </div>
              </details>
              <textarea name="message" rows="3" placeholder="{esc(composer_placeholder)}"></textarea>
              <div class="agent-composer-footer">
                <span class="muted">{composer_context_note}</span>
                <button class="button primary" type="submit">发送</button>
              </div>
            </form>
          </article>
        </section>
        """
        self.send_html(layout("AI 训练教练 - 研申", body, "agent", flashes))

    def page_agent_conversation(self, path):
        try:
            conversation_id = int(path.strip("/").split("/")[2])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        with connect(self.db_path) as conn:
            conversation, messages = conversation_payload(conn, conversation_id)
            if not conversation:
                self.send_error(404)
                return
            conversations = recent_conversations(conn, 50)
            run_ids = [message["run_id"] for message in messages if message["run_id"]]
            run_steps_by_id = {}
            run_rows = []
            for run_id in run_ids[-5:]:
                run = get_run(conn, run_id)
                if run:
                    run_rows.append(run)
                run_steps_by_id[run_id] = get_run_steps(conn, run_id)
            conversation_module = ""
            for message in reversed(messages):
                try:
                    metadata = json.loads(message["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                if metadata.get("module"):
                    conversation_module = metadata["module"]
                    break
        message_rows = []
        has_pending_message = False
        last_role = messages[-1]["role"] if messages else ""
        for message in messages:
            is_pending = message["message_type"] == "pending"
            if is_pending:
                has_pending_message = True
            message_rows.append(
                render_agent_message_row(message, self.path, run_steps_by_id.get(message["run_id"], []))
            )
            continue
            try:
                message_metadata = json.loads(message["metadata_json"] or "{}")
            except json.JSONDecodeError:
                message_metadata = {}
            pending_status = (
                render_agent_pending_status(run_steps_by_id.get(message["run_id"], [])) if is_pending else ""
            )
            run_link = (
                f"""
                <div class="agent-message-actions">
                  <a class="button small" href="/agent/runs/{message["run_id"]}">工具轨迹</a>
                </div>"""
                if message["run_id"]
                else ""
            )
            message_rows.append(f"""
            <article class="agent-message {esc(message["role"])}{" is-pending" if is_pending else ""}"{' data-agent-pending="1"' if is_pending else ""}>
              <header><strong>{"我" if message["role"] == "user" else "AI 教练"}</strong><span>{"生成中" if is_pending else esc(format_beijing_time(message["created_at"]))}</span></header>
              <div class="report-body">{agent_response_html(message["content"], self.path) if message["role"] == "assistant" else f"<p>{esc(message['content'])}</p>"}</div>
              {render_agent_rag_panel(message_metadata, self.path) if message["role"] == "assistant" and not is_pending else ""}
              {pending_status}
              {('<div class="agent-thinking-dots" aria-label="生成中"><span></span><span></span><span></span></div>' if is_pending else "")}
              {run_link}
            </article>""")
        conversation_rows = render_agent_thread_list(
            conversations,
            active_conversation_id=conversation_id,
            return_to=f"/agent/conversations/{conversation_id}",
        )
        trace_rows = []
        for run in run_rows:
            run_summary = agent_summary_for_display(run["input_summary"] or run["user_goal"] or "查看工具轨迹")
            trace_rows.append(f"""
            <article class="agent-run-row">
              <a href="/agent/runs/{run["id"]}"><strong>{esc(run["task_type"])}</strong><span>{esc(run["status"])}</span></a>
              <p>{esc(run_summary)}</p>
            </article>""")
        if not trace_rows:
            trace_rows.append('<p class="muted">本线程还没有关联的运行轨迹。</p>')
        awaiting_attr = ' data-agent-awaiting="1"' if last_role == "user" else ""
        body = f"""
        <section class="agent-workspace">
          <aside class="agent-thread-rail" aria-label="教练线程">
            <div class="agent-rail-head">
              <div><p class="eyebrow">Threads</p><h1>教练线程</h1></div>
              <a class="button small primary" href="/agent">新咨询</a>
            </div>
            <div class="agent-thread-list">{conversation_rows}</div>
          </aside>
          <article class="agent-main">
            <header class="agent-main-head">
              <div>
                <span class="agent-mode-pill">Coach Thread #{conversation_id}{" · " + esc(module_definition(conversation_module)["label"]) if conversation_module else ""}</span>
                <h2>{esc(conversation["title"] or "AI 教练对话")}</h2>
                <p>继续追问时会沿用这个线程里的训练意图和上下文。</p>
              </div>
              <div class="agent-head-actions"><a class="button ghost" href="/agent">返回首页</a></div>
            </header>
            <section class="agent-message-stream"{awaiting_attr}>
              {"".join(message_rows)}
            </section>
            <form class="agent-composer" method="post" action="/agent/conversations/{conversation_id}/messages" data-agent-composer>
              <input type="hidden" name="module" value="{esc(conversation_module)}">
              <textarea name="message" rows="3" placeholder="继续追问…"></textarea>
              <div class="agent-composer-footer"><span class="muted">上下文会保留在当前线程中</span><button class="button primary" type="submit">发送</button></div>
            </form>
          </article>
        </section>
        """
        self.send_html(layout("AI 教练对话 - 研申", body, "agent"))

    def page_agent_knowledge(self, path, query):
        knowledge_id = path.removeprefix("/agent/knowledge/").strip()
        card = next((item for item in load_knowledge_items() if item.get("id") == knowledge_id), None)
        if not card:
            self.send_error(404)
            return
        return_to = return_path_from_query(query, "/agent")

        def list_block(title, values):
            rows = "".join(f"<li>{esc(value)}</li>" for value in (values or []) if str(value).strip())
            return f"<h3>{esc(title)}</h3><ul>{rows}</ul>" if rows else ""

        body = f"""
        <section class="page-head">
          <div><p class="eyebrow">Knowledge Evidence</p><h1>{esc(card.get("title"))}</h1></div>
          <div class="actions">{back_link(return_to, "返回引用位置")}</div>
        </section>
        <section class="settings-panel report-body">
          <p class="muted">证据 ID：{esc(card.get("id"))} · 模块：{esc(module_definition(card.get("module"))["label"])}</p>
          <p>{esc(card.get("content"))}</p>
          {list_block("示例", card.get("examples"))}
          {list_block("常见误区", card.get("pitfalls"))}
          {list_block("适用范围", card.get("applicable_when"))}
          {list_block("不适用范围", card.get("not_applicable_when"))}
        </section>
        """
        self.send_html(layout(f"教材证据 - {card.get('title')}", body, "agent"))

    def page_agent_memories(self, flashes=None):
        with connect(self.db_path) as conn:
            rows = active_memories(conn, limit=100)
        type_labels = {
            "semantic": "用户信息",
            "episodic": "训练事件",
            "procedural": "交互偏好",
        }
        cards = []
        for row in rows:
            source_link = (
                f'<a href="/agent/conversations/{row["source_conversation_id"]}">来源对话</a>'
                if row["source_conversation_id"]
                else "无来源对话"
            )
            cards.append(f"""
            <article class="settings-panel">
              <div class="section-head">
                <div><p class="eyebrow">{esc(type_labels.get(row["memory_type"], row["memory_type"]))}</p><h2>{esc(row["memory_key"])}</h2></div>
                <form method="post" action="/agent/memories/{row["id"]}/delete" data-confirm="删除这条长期记忆吗？">
                  <button class="button small ghost" type="submit">删除</button>
                </form>
              </div>
              <p>{esc(row["content"])}</p>
              <p class="muted">置信度 {float(row["confidence"]):.2f} · {source_link} · 更新于 {esc(format_beijing_time(row["updated_at"]))}</p>
            </article>
            """)
        if not cards:
            cards.append(
                '<section class="text-block"><p class="muted">还没有长期记忆。只有当你明确说出目标考试、训练节奏、回答偏好、稳定短板或已取得的改进时，系统才会记录。</p></section>'
            )
        body = f"""
        <section class="page-head">
          <div><p class="eyebrow">Agent Memory</p><h1>长期记忆</h1><p>这些内容会跨对话线程生效，但不会代替题目、作答和批改证据。</p></div>
          <div class="actions">
            <a class="button ghost" href="/agent">返回 AI 教练</a>
            <form method="post" action="/agent/memories/clear" data-confirm="清空全部长期记忆吗？对话记录不会被删除。">
              <button class="button ghost" type="submit">清空记忆</button>
            </form>
          </div>
        </section>
        <section class="settings-grid">{"".join(cards)}</section>
        """
        self.send_html(layout("长期记忆 - 研申", body, "agent", flashes))

    def page_agent_setup(self, query, flashes=None):
        self.redirect("/settings#ai-coach-settings")

    def page_agent_evals(self, flashes=None):
        with connect(self.db_path) as conn:
            rows = latest_eval_results(conn, 20)
        result_rows = []

        def fmt_metric(value):
            if value is None:
                return "-"
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return esc(value)

        for row in rows:
            try:
                metrics = json.loads(row["metrics_json"] or "{}")
            except json.JSONDecodeError:
                metrics = {}
            ragas_scores = metrics.get("ragas_scores") or {}
            ragas_status = metrics.get("ragas_status") or "not_run"
            ragas_average = fmt_metric(metrics.get("ragas_average"))
            layers = metrics.get("layers") or {}
            planner_layer = layers.get("planner") or {}
            retriever_layer = layers.get("retriever") or {}
            tool_layer = layers.get("tool") or {}
            answer_layer = layers.get("answer") or {}
            runtime = metrics.get("runtime") or {}
            token_usage = runtime.get("token_usage") or {}
            run_match = re.search(r"\brun_id=(\d+)", row["notes"] or "")
            trace_link = (
                f'<a class="button small ghost" href="/agent/runs/{run_match.group(1)}">查看 Trace</a>'
                if run_match
                else ""
            )
            result_rows.append(f"""
            <article class="agent-eval-row">
              <header><strong>{esc(row["case_title"])}</strong><span>{row["score"]:.1f}</span></header>
              <p>{esc(row["suite_name"])} · {esc(row["task_type"])} · {esc(format_beijing_time(row["created_at"]))}</p>
              <div class="agent-eval-metrics">
                <span>Tools {metrics.get("tool_call_accuracy", 0)}</span>
                <span>Goal {metrics.get("agent_goal_accuracy", 0)}</span>
                <span>Response {metrics.get("response_completeness", 0)}</span>
                <span>Evidence {metrics.get("evidence_proxy", 0)}</span>
                <span>Internal {metrics.get("internal_score", row["score"])}</span>
                <span>Planner {fmt_metric(planner_layer.get("accuracy"))}</span>
                <span>Recall@10 {fmt_metric(retriever_layer.get("recall_at_10"))}</span>
                <span>MRR {fmt_metric(retriever_layer.get("mrr"))}</span>
                <span>Tool layer {fmt_metric(tool_layer.get("accuracy"))}</span>
                <span>Answer layer {fmt_metric(answer_layer.get("goal_accuracy"))}</span>
                <span>Ragas {esc(ragas_status)} · {ragas_average}</span>
                <span>Faithfulness {fmt_metric(ragas_scores.get("faithfulness"))}</span>
                <span>Context P {fmt_metric(ragas_scores.get("context_precision"))}</span>
                <span>Context R {fmt_metric(ragas_scores.get("context_recall"))}</span>
                <span>Factual {fmt_metric(ragas_scores.get("factual_correctness"))}</span>
              </div>
              <div class="agent-eval-footer">
                <small>{esc(row["notes"])} · {esc(runtime.get("model") or "unknown model")} · {fmt_metric(runtime.get("duration_ms"))} ms · {token_usage.get("total_tokens", "-")} tokens · prompt {esc(runtime.get("prompt_version") or "-")} · retrieval {esc(runtime.get("retrieval_version") or "-")}</small>
                {trace_link}
                <form method="post" action="/agent/evals/{row["id"]}/delete" data-confirm="删除这条评测记录吗？">
                  <button class="button small ghost" type="submit">删除</button>
                </form>
              </div>
            </article>""")
        if not result_rows:
            result_rows.append('<p class="muted">还没有评测结果。运行一次固定回归集即可生成。</p>')
        body = f"""
        <section class="page-head">
          <div><p class="eyebrow">Agent Evals</p><h1>AI 教练评测</h1></div>
          <div class="actions"><a class="button ghost" href="/agent">返回 AI 教练</a></div>
        </section>
        <section class="agent-result-layout">
          <article class="agent-result-main">
            <section class="text-block">
              <h2>固定回归集</h2>
              <p class="muted">版本化回归集包含 60 条人工可审查用例。页面默认运行 5 条 smoke 用例；完整集、无模型 CI 子集和 baseline/candidate 对比请使用 scripts/run_agent_eval.py。</p>
              <div class="actions">
              <form method="post" action="/agent/evals/run">
                <button class="button primary" type="submit">运行评测</button>
              </form>
              <form method="post" action="/agent/evals/clear" data-confirm="清空所有评测记录吗？这不会删除教练对话或运行记录。">
                <button class="button ghost" type="submit">清空记录</button>
              </form>
              </div>
            </section>
            <section class="agent-eval-list">
              {"".join(result_rows)}
            </section>
          </article>
          <aside class="agent-side">
            <section class="tool-panel">
              <h2>什么时候用</h2>
              <p class="muted">改过 Agent 后跑；换模型后跑；觉得回复质量变差时跑。平时做题和复盘不用管它。</p>
              <h2>指标说明</h2>
              <p class="muted">Planner 看查询规划；Recall@10 与 MRR 看 gold evidence 召回；Tool 看工具轨迹；Answer 看目标覆盖；Ragas 看事实忠实度。每条记录可进入 Trace 定位具体步骤。</p>
            </section>
          </aside>
        </section>
        """
        self.send_html(layout("AI 教练评测 - 研申", body, "agent", flashes))

    def page_agent_run(self, path):
        try:
            run_id = int(path.strip("/").split("/")[2])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        with connect(self.db_path) as conn:
            run = get_run(conn, run_id)
            if not run:
                self.send_error(404)
                return
            steps = get_run_steps(conn, run_id)
            feedback = get_feedback(conn, run_id)
        step_rows = []
        for step in steps:
            try:
                output = json.loads(step["output_json"] or "{}")
            except json.JSONDecodeError:
                output = {}
            preview = json.dumps(output, ensure_ascii=False, indent=2)[:1200]
            step_rows.append(f"""
            <details class="agent-step">
              <summary><strong>{step["step_index"]}. {esc(step["step_type"])}</strong><span>{esc(step["tool_name"])}</span></summary>
              <pre>{esc(preview)}</pre>
            </details>""")
        feedback_html = (
            "".join(f'<p class="muted">评分 {item["rating"]}/5：{esc(item["note"])}</p>' for item in feedback)
            or '<p class="muted">还没有反馈。</p>'
        )
        body = f"""
        <section class="page-head">
          <div><p class="eyebrow">Agent Run #{run_id} · {esc(run["status"])}</p><h1>AI 教练结果</h1></div>
          <div class="actions">
            <a class="button ghost" href="/agent">返回 AI 教练</a>
          </div>
        </section>
        <section class="agent-result-layout">
          <article class="agent-result-main">
            <section class="report-card">
              <header><strong>{esc(run["task_type"])}</strong><span>{esc(run["provider"])} / {esc(run["model"])} · {esc(format_beijing_time(run["created_at"]))}</span></header>
              <div class="report-body">{agent_response_html(run["final_text"], f"/agent/runs/{run_id}")}</div>
            </section>
          </article>
          <aside class="agent-side">
            <section class="tool-panel">
              <h2>运行摘要</h2>
              <p class="muted">{esc(agent_summary_for_display(run["input_summary"] or run["user_goal"] or "无摘要"))}</p>
            </section>
            <section class="tool-panel">
              <h2>工具轨迹</h2>
              <div class="agent-step-list">{"".join(step_rows) or '<p class="muted">没有工具轨迹。</p>'}</div>
            </section>
            <section class="tool-panel">
              <h2>反馈</h2>
              {feedback_html}
              <form method="post" action="/agent/runs/{run_id}/feedback">
                <label><span>评分</span><select name="rating"><option value="5">5 - 有用</option><option value="4">4</option><option value="3">3</option><option value="2">2</option><option value="1">1 - 没用</option></select></label>
                <label><span>备注</span><textarea name="note" rows="4"></textarea></label>
                <button class="button primary" type="submit">保存反馈</button>
              </form>
            </section>
          </aside>
        </section>
        """
        self.send_html(layout("AI 教练结果 - 研申", body, "agent"))

    def handle_agent_run(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(data, keep_blank_values=True)
        task_type = form.get("task_type", ["diagnosis"])[0]
        attempt_id = _int_or_none(form.get("attempt_id", [""])[0])
        subject_id = attempt_id if task_type == "review" else None
        filters = {
            "question_type": form.get("question_type", [""])[0].strip(),
            "region": form.get("region", [""])[0].strip(),
            "work_status": form.get("work_status", [""])[0].strip(),
            "q": form.get("q", [""])[0].strip(),
        }
        user_goal = form.get("user_goal", [""])[0].strip()
        auto_approve = form.get("save_plan", [""])[0] == "1"
        try:
            run_id = run_agent(
                self.db_path,
                task_type,
                subject_id=subject_id,
                user_goal=user_goal,
                filters=filters,
                auto_approve=auto_approve,
                module=form.get("module", [""])[0].strip(),
            )
        except AgentRunError as exc:
            self.page_agent({}, [("error", f"AI 教练运行失败：{exc}")])
            return
        self.redirect(f"/agent/runs/{run_id}")

    def handle_agent_conversation(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(data, keep_blank_values=True)
        filters = {
            "question_type": form.get("question_type", [""])[0].strip(),
            "region": form.get("region", [""])[0].strip(),
            "work_status": form.get("work_status", [""])[0].strip(),
            "q": form.get("q", [""])[0].strip(),
        }
        try:
            conversation_id, _ = start_or_continue_chat_async(
                self.db_path,
                user_text=form.get("message", [""])[0].strip(),
                entrypoint=form.get("entrypoint", ["chat"])[0].strip() or "chat",
                filters=filters,
                module=form.get("module", [""])[0].strip(),
                auto_approve=False,
                review_attempt_id=_int_or_none(form.get("attempt_id", [""])[0]),
            )
        except AgentRunError as exc:
            self.page_agent({}, [("error", f"AI 教练运行失败：{exc}")])
            return
        self.redirect(f"/agent/conversations/{conversation_id}")

    def handle_agent_message(self, path):
        try:
            conversation_id = int(path.strip("/").split("/")[2])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            conversation_id, _ = start_or_continue_chat_async(
                self.db_path,
                conversation_id=conversation_id,
                user_text=form_value(data, "message", ""),
                module=form_value(data, "module", ""),
                entrypoint="chat",
                auto_approve=False,
            )
        except AgentRunError as exc:
            self.page_agent({}, [("error", f"AI 教练运行失败：{exc}")])
            return
        self.redirect(f"/agent/conversations/{conversation_id}")

    def handle_agent_conversation_status(self, path):
        try:
            conversation_id = int(path.strip("/").split("/")[2])
        except (ValueError, IndexError):
            self.send_json({"error": "not_found"}, status=404)
            return
        try:
            with connect(self.db_path) as conn:
                conversation = conn.execute(
                    "SELECT id FROM agent_conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if not conversation:
                    self.send_json({"conversation_id": conversation_id, "pending": False, "awaiting": False, "deleted": True})
                    return
                _cleanup_orphaned_pending_messages(conn, conversation_id)
                latest = conn.execute(
                    """
                    SELECT *
                      FROM agent_messages
                     WHERE conversation_id = ?
                  ORDER BY id DESC LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
                if not latest:
                    self.send_json({"conversation_id": conversation_id, "pending": False, "awaiting": False})
                    return
                is_pending = latest["message_type"] == "pending"
                steps = []
                if latest["run_id"]:
                    steps = [
                        {"tool_name": step["tool_name"], "step_type": step["step_type"], "index": step["step_index"]}
                        for step in get_run_steps(conn, latest["run_id"])
                    ]
                self.send_json(
                    {
                        "conversation_id": conversation_id,
                        "latest_message_id": latest["id"],
                        "latest_role": latest["role"],
                        "latest_type": latest["message_type"],
                        "run_id": latest["run_id"],
                        "pending": is_pending,
                        "awaiting": latest["role"] == "user",
                        "steps": steps,
                        "message_html": ""
                        if is_pending
                        else render_agent_message_row(latest, f"/agent/conversations/{conversation_id}", steps),
                    }
                )
        except Exception as exc:
            logging.exception("Agent conversation status failed")
            self.send_json({"error": str(exc), "retry": True}, status=503)

    def handle_agent_conversation_delete(self, path):
        try:
            conversation_id = int(path.strip("/").split("/")[2])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        return_to = return_path_from_form(data, "/agent")
        for attempt in range(5):
            try:
                with connect(self.db_path) as conn:
                    delete_conversation(conn, conversation_id)
                break
            except Exception as exc:
                if "locked" in str(exc).lower() and attempt < 4:
                    time.sleep(0.15 * (attempt + 1))
                else:
                    raise
        if return_to.startswith(f"/agent/conversations/{conversation_id}"):
            return_to = "/agent"
        self.redirect(return_to)

    def handle_agent_setup_save(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        provider_name = form_value(data, "provider_name", "DeepSeek")
        api_base_url = form_value(data, "api_base_url", "https://api.deepseek.com")
        model = form_value(data, "model", "deepseek-v4-pro")
        api_key = form_value(data, "api_key", "")
        api_key_env = form_value(data, "api_key_env", "")
        with connect(self.db_path) as conn:
            current = conn.execute("SELECT * FROM agent_ai_settings WHERE id = 1").fetchone()
            stored_key = api_key or current["api_key"]
            conn.execute(
                """
                UPDATE agent_ai_settings
                   SET use_grading_api = 0,
                       provider_name = ?,
                       api_base_url = ?,
                       api_key = ?,
                       api_key_env = ?,
                       model = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = 1
                """,
                (provider_name, api_base_url, stored_key, api_key_env, model),
            )
        self.redirect("/agent")

    def handle_agent_setup_test(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        settings = {
            "provider_name": form_value(data, "provider_name", "DeepSeek"),
            "api_base_url": form_value(data, "api_base_url", "https://api.deepseek.com"),
            "model": form_value(data, "model", "deepseek-v4-pro"),
            "api_key": form_value(data, "api_key", ""),
            "api_key_env": form_value(data, "api_key_env", ""),
            "temperature": 0,
        }
        try:
            chat_completion(settings, "请只回复 OK，用于测试连接。")
        except (AiConfigError, AiRequestError) as exc:
            self.page_agent_setup({"provider": [settings["provider_name"]]}, [("error", f"连接测试失败：{exc}")])
            return
        self.page_agent_setup({"provider": [settings["provider_name"]]}, [("success", "连接测试成功，可以保存配置。")])

    def handle_agent_memory_delete(self, path):
        try:
            memory_id = int(path.strip("/").split("/")[2])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        with connect(self.db_path) as conn:
            if not delete_memory(conn, memory_id):
                self.send_error(404)
                return
        self.redirect("/agent/memories")

    def handle_agent_memories_clear(self):
        with connect(self.db_path) as conn:
            clear_memories(conn)
        self.redirect("/agent/memories")

    def handle_agent_eval_run(self):
        try:
            results = run_eval_suite(self.db_path)
        except AgentRunError as exc:
            self.page_agent_evals([("error", f"评测运行失败：{exc}")])
            return
        self.page_agent_evals([("success", f"评测完成：{len(results)} 个用例。")])

    def handle_agent_eval_delete(self, path):
        try:
            eval_id = int(path.strip("/").split("/")[2])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM agent_eval_results WHERE id = ?", (eval_id,))
        self.redirect("/agent/evals")

    def handle_agent_eval_clear(self):
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM agent_eval_results")
        self.redirect("/agent/evals")

    def handle_agent_feedback(self, path):
        try:
            run_id = int(path.strip("/").split("/")[2])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else ""
        with connect(self.db_path) as conn:
            add_feedback(
                conn,
                run_id,
                form_value(data, "rating", "5"),
                form_value(data, "note", ""),
            )
        self.redirect(f"/agent/runs/{run_id}")

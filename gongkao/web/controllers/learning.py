"""Learning records, notes, and statistics controllers."""

from ..runtime import (
    activity_level,
    build_module_score_statistics,
    build_training_statistics,
    connect,
    esc,
    format_beijing_time,
    format_duration,
    layout,
    local_url,
    math,
    option_list,
    pagination_html,
    render_module_score_detail,
    requested_page,
    sort_exam_types,
    sort_question_types,
    sort_regions,
    timedelta,
    year_range_filter,
)


class LearningController:
    def page_statistics(self, query=None):
        query = query or {}
        score_mode = query.get("score_mode", ["first"])[0]
        score_mode = "best" if score_mode == "best" else "first"
        with connect(self.db_path) as conn:
            stats = build_training_statistics(conn)
            module_scores = build_module_score_statistics(conn, score_mode)

        activity = stats["daily"]
        first_date = activity[0][0]
        grid_start = first_date - timedelta(days=(first_date.weekday() + 1) % 7)
        activity_counts = dict(activity)
        activity_cells = []
        cursor = grid_start
        while cursor <= activity[-1][0]:
            count = activity_counts.get(cursor)
            if count is None:
                activity_cells.append('<i class="activity-cell is-empty" aria-hidden="true"></i>')
            else:
                level = activity_level(count)
                activity_cells.append(
                    f'<i class="activity-cell level-{level}" title="{cursor.strftime("%Y-%m-%d")}：{count} 道题" '
                    f'aria-label="{cursor.strftime("%Y-%m-%d")}，{count} 道题"></i>'
                )
            cursor += timedelta(days=1)
        activity_weeks = math.ceil(len(activity_cells) / 7)
        month_labels = []
        previous_month = None
        for week in range(activity_weeks):
            week_date = grid_start + timedelta(days=week * 7)
            month = week_date.month
            label = ""
            if month != previous_month and week_date >= first_date:
                label = f"{month}月"
            month_labels.append(f"<span>{label}</span>")
            previous_month = month
        type_rows = "".join(
            f"""
            <article class="stat-row">
              <header><strong>{item["name"]}</strong><span>{item["questions"]} / {item["total"]} 题 · 作答 {item["attempts"]} 次 · 批改 {item["reports"]} 次</span></header>
              <div class="stat-bar"><i style="width:{min(100, item["completion"])}%"></i></div>
              <small>题库完成度 {item["completion"]}%</small>
            </article>"""
            for item in stats["type_stats"]
        )
        region_max = max((count for _, count in stats["regions"]), default=0) or 1
        region_rows = (
            "".join(
                f'<div class="region-row"><span>{esc(region)}</span><div class="stat-bar"><i style="width:{round(count * 100 / region_max)}%"></i></div><strong>{count}</strong></div>'
                for region, count in stats["regions"][:12]
            )
            or '<p class="muted">提交作答后，这里会显示练习地区分布。</p>'
        )
        score_value = f"{stats['average_score']:.1f}" if stats["average_score"] is not None else "—"
        total_duration = format_duration(stats["total_duration_seconds"], "—")
        last_practice = format_beijing_time(stats["last_practice"]) if stats["last_practice"] else "暂无"
        module_cards = "".join(render_module_score_detail(module) for module in module_scores["modules"])
        if not module_cards:
            module_cards = (
                '<p class="muted">还没有能识别出分数的批改报告。报告中包含“总分：13/20”这类格式后会自动统计。</p>'
            )
        first_active = " active" if score_mode == "first" else ""
        best_active = " active" if score_mode == "best" else ""

        body = f"""
        <div class="statistics-compact">
        <section class="page-head">
          <div><p class="eyebrow">Training Analytics</p><h1>训练统计</h1></div>
          <div class="actions segmented-actions" aria-label="分数统计口径">
            <a class="button ghost{first_active}" href="/statistics?score_mode=first">按首次作答</a>
            <a class="button ghost{best_active}" href="/statistics?score_mode=best">按最高分</a>
          </div>
        </section>
        <section class="stat-metrics">
          <div><strong>{stats["attempt_count"]}</strong><span>总作答次数</span></div>
          <div><strong>{stats["question_count"]}</strong><span>做过的题目</span></div>
          <div><strong>{stats["report_count"]}</strong><span>批改报告</span></div>
          <div><strong>{stats["word_count"]}</strong><span>累计作答字数</span></div>
          <div><strong>{esc(total_duration)}</strong><span>总练习时长</span></div>
          <div><strong>{stats["active_days"]}</strong><span>活跃天数</span></div>
          <div><strong>{stats["streak"]}</strong><span>连续练习天数</span></div>
          <div><strong>{score_value}</strong><span>AI 百分制均分</span></div>
          <div><strong>{stats["favorite_questions"] + stats["favorite_papers"]}</strong><span>收藏总数</span></div>
        </section>
        <section class="statistics-panel module-score-section">
          <div class="section-heading">
            <div><p class="eyebrow">Score By Module</p><h2>模块均分与趋势</h2></div>
            <span>{"每题取最高分" if score_mode == "best" else "每题取第一次作答"} · 已识别 {module_scores["scored_questions"]} 题</span>
          </div>
          <div class="module-score-grid">{module_cards}</div>
        </section>
        <section class="statistics-grid">
          <div class="statistics-column statistics-left-column">
            <article class="statistics-panel activity-panel">
              <div class="section-heading"><div><p class="eyebrow">Practice Activity</p><h2>近期练习</h2></div><span>最近练习：{esc(last_practice)}</span></div>
              <div class="activity-scroll">
                <div class="activity-months" style="--activity-weeks:{activity_weeks}">{"".join(month_labels)}</div>
                <div class="activity-body">
                  <div class="activity-weekdays" aria-hidden="true"><span>一</span><span>三</span><span>五</span></div>
                  <div class="activity-grid" style="--activity-weeks:{activity_weeks}" aria-label="过去一年每日练习题目数">{"".join(activity_cells)}</div>
                </div>
              </div>
              <div class="activity-legend"><span>少</span><i class="level-0"></i><i class="level-1"></i><i class="level-2"></i><i class="level-3"></i><i class="level-4"></i><span>多</span></div>
            </article>
            <article class="statistics-panel regions-panel">
              <div class="section-heading"><div><p class="eyebrow">Regions</p><h2>练习地区</h2></div></div>
              <div class="region-list">{region_rows}</div>
            </article>
          </div>
          <div class="statistics-column statistics-right-column">
            <article class="statistics-panel type-progress-panel">
              <div class="section-heading"><div><p class="eyebrow">Question Types</p><h2>题型进度</h2></div></div>
              <div class="stat-list">{type_rows}</div>
            </article>
          </div>
        </section>
        </div>
        """
        self.send_html(layout("训练统计 - 研申", body, "statistics"))

    def page_attempts(self, query):
        page = requested_page(query)
        page_size = 10
        filters = {
            "exam_type": query.get("exam_type", [""])[0],
            "region": query.get("region", [""])[0],
            "question_type": query.get("question_type", [""])[0],
            "paper_id": query.get("paper_id", [""])[0],
            "year_from": query.get("year_from", ["2020"])[0],
            "year_to": query.get("year_to", [""])[0],
            "status": query.get("status", [""])[0],
            "q": query.get("q", [""])[0],
        }
        clauses, params = [], []
        if filters["exam_type"]:
            clauses.append("q.exam_type = ?")
            params.append(filters["exam_type"])
        if filters["region"]:
            clauses.append("q.region = ?")
            params.append(filters["region"])
        if filters["question_type"]:
            clauses.append("q.question_type = ?")
            params.append(filters["question_type"])
        if filters["paper_id"]:
            clauses.append("q.paper_id = ?")
            params.append(filters["paper_id"])
        if filters["year_from"]:
            clauses.append("q.year >= ?")
            params.append(filters["year_from"])
        if filters["year_to"]:
            clauses.append("q.year <= ?")
            params.append(filters["year_to"])
        if filters["q"]:
            clauses.append("(q.title LIKE ? OR q.paper_name LIKE ? OR q.prompt LIKE ? OR a.answer_text LIKE ?)")
            term = f"%{filters['q']}%"
            params.extend([term, term, term, term])
        where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
        having_sql = ""
        if filters["status"] == "graded":
            having_sql = "HAVING report_count > 0"
        elif filters["status"] == "ungraded":
            having_sql = "HAVING report_count = 0"
        elif filters["status"] == "noted":
            having_sql = "HAVING has_note > 0"
        with connect(self.db_path) as conn:
            total_items = conn.execute(
                f"""
                SELECT COUNT(*)
                  FROM (
                    SELECT a.id, COUNT(gr.id) AS report_count,
                           CASE WHEN TRIM(COALESCE(a.personal_note, '')) <> '' THEN 1 ELSE 0 END AS has_note
                      FROM attempts a
                      JOIN questions q ON q.id = a.question_id
                 LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                         {where_sql}
                  GROUP BY a.id
                         {having_sql}
                  )
                """,
                params,
            ).fetchone()[0]
            total_pages = max(1, math.ceil(total_items / page_size))
            page = min(page, total_pages)
            offset = (page - 1) * page_size
            attempts = conn.execute(
                f"""
                SELECT a.*, q.title, q.year, q.region, q.exam_type, q.paper_name,
                       q.question_number, q.question_type, COUNT(gr.id) AS report_count,
                       CASE WHEN TRIM(COALESCE(a.personal_note, '')) <> '' THEN 1 ELSE 0 END AS has_note
                  FROM attempts a
                  JOIN questions q ON q.id = a.question_id
             LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                 {where_sql}
              GROUP BY a.id
                 {having_sql}
              ORDER BY a.created_at DESC, a.id DESC
                 LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
            exam_types = sort_exam_types(
                [row["exam_type"] for row in conn.execute("SELECT DISTINCT exam_type FROM questions")]
            )
            regions = sort_regions([row["region"] for row in conn.execute("SELECT DISTINCT region FROM questions")])
            question_types = sort_question_types(
                [row["question_type"] for row in conn.execute("SELECT DISTINCT question_type FROM questions")]
            )
            papers = conn.execute(
                "SELECT id, year, region, paper_name, paper_category FROM papers ORDER BY year DESC, region, paper_name"
            ).fetchall()

        paper_options = ['<option value="">全部</option>']
        for paper in papers:
            label = f"{paper['year']} {paper['region']} {paper['paper_name']} {paper['paper_category'] or ''}".strip()
            sel = " selected" if str(paper["id"]) == filters["paper_id"] else ""
            paper_options.append(f'<option value="{paper["id"]}"{sel}>{esc(label)}</option>')
        status_options = [
            ("", "全部"),
            ("graded", "已批改"),
            ("ungraded", "未批改"),
            ("noted", "有笔记"),
        ]
        status_html = "".join(
            f'<option value="{value}"{" selected" if value == filters["status"] else ""}>{label}</option>'
            for value, label in status_options
        )
        pager = pagination_html("/attempts", filters, page, total_items, page_size)
        has_active_filters = any(
            filters[name] for name in ("exam_type", "region", "question_type", "paper_id", "year_to", "status", "q")
        ) or (filters["year_from"] and filters["year_from"] != "2020")

        rows = []
        for attempt in attempts:
            excerpt = attempt["answer_text"][:140] + ("..." if len(attempt["answer_text"]) > 140 else "")
            state = "已批改" if attempt["report_count"] else "待批改"
            state_class = "graded" if attempt["report_count"] else "pending"
            note_badge = (
                '<span class="note-badge">有笔记</span>'
                if attempt["has_note"]
                else '<span class="note-badge is-empty" aria-hidden="true">有笔记</span>'
            )
            duration_badge = (
                f'<small class="record-duration">用时 {esc(format_duration(attempt["duration_seconds"]))}</small>'
                if attempt["duration_seconds"]
                else '<small class="record-duration">未计时</small>'
            )
            attempt_href = local_url(f"/attempts/{attempt['id']}", return_to=self.path)
            rows.append(f"""
            <article class="record-card">
              <a class="record-main" href="{esc(attempt_href)}" aria-label="打开作答：{esc(attempt["title"])}">
                <div class="card-top"><span>{esc(format_beijing_time(attempt["created_at"]))}</span></div>
                <h2>{esc(attempt["title"])}</h2>
                <p class="paper-line">{esc(attempt["year"])} {esc(attempt["region"])} {esc(attempt["exam_type"])} · {esc(attempt["question_type"])} · {esc(attempt["paper_name"])} · 第{attempt["question_number"] or "?"}题</p>
                <p>{esc(excerpt)}</p>
              </a>
              <div class="record-side"><div class="record-metrics"><div class="record-state {state_class}"><b>{esc(state)}</b></div><span class="record-word-count"><strong>{attempt["word_count"]}</strong><span>字</span></span>{duration_badge}<small>{attempt["report_count"]} 份报告</small>{note_badge}</div><form method="post" action="/attempts/{attempt["id"]}/delete" data-confirm="确认删除这次作答和对应批改报告吗？"><input type="hidden" name="return_to" value="{esc(self.path)}"><button class="record-delete" type="submit">删除</button></form></div>
            </article>""")
        if not rows:
            if has_active_filters:
                rows.append(
                    '<div class="empty-state"><h2>当前筛选下没有作答</h2><p>你的历史记录仍在，只是没有记录符合上方条件。</p><a class="button primary" href="/attempts" data-filter-reset>清除筛选并查看全部</a></div>'
                )
            else:
                rows.append(
                    '<div class="empty-state"><h2>还没有作答记录</h2><p>从题目详情页提交答案后，这里会自动出现记录。</p><a class="button primary" href="/papers">开始第一次练习</a></div>'
                )

        body = f"""
        <section class="page-head"><div><p class="eyebrow">Practice Log</p><h1>作答记录</h1></div></section>
        <form class="filters records-filter auto-filter" method="get">
          <label><span>考试类型</span><select name="exam_type">{option_list(exam_types, filters["exam_type"])}</select></label>
          <label><span>地区</span><select name="region">{option_list(regions, filters["region"])}</select></label>
          <label><span>题型</span><select name="question_type">{option_list(question_types, filters["question_type"])}</select></label>
          <label><span>试卷</span><select name="paper_id">{"".join(paper_options)}</select></label>
          {year_range_filter(filters)}
          <label><span>批改状态</span><select name="status">{status_html}</select></label>
          <label class="search"><span>关键词</span><input name="q" value="{esc(filters["q"])}" placeholder="题目、试卷、答案"></label>
          <button class="button primary" type="submit">筛选</button><a class="button ghost" href="/attempts" data-filter-reset>重置</a>
        </form>
        <section class="record-list">{"".join(rows)}</section>
        {pager}
        """
        self.send_html(layout("作答记录 - 研申", body, "attempts"))

    def page_notes(self, query):
        page = requested_page(query)
        page_size = 10
        filters = {"q": query.get("q", [""])[0]}
        clauses = ["TRIM(COALESCE(a.personal_note, '')) <> ''"]
        params = []
        if filters["q"]:
            clauses.append(
                "(q.title LIKE ? OR q.paper_name LIKE ? OR q.prompt LIKE ? OR a.personal_note LIKE ? OR a.answer_text LIKE ?)"
            )
            term = f"%{filters['q']}%"
            params.extend([term, term, term, term, term])
        where_sql = "WHERE " + " AND ".join(clauses)
        with connect(self.db_path) as conn:
            total_items = conn.execute(
                f"""
                SELECT COUNT(*)
                  FROM attempts a
                  JOIN questions q ON q.id = a.question_id
                 {where_sql}
                """,
                params,
            ).fetchone()[0]
            total_pages = max(1, math.ceil(total_items / page_size))
            page = min(page, total_pages)
            offset = (page - 1) * page_size
            notes = conn.execute(
                f"""
                SELECT a.id, a.personal_note, a.created_at,
                       q.title, q.year, q.region, q.exam_type, q.paper_name,
                       q.question_number, q.question_type,
                       COUNT(gr.id) AS report_count
                  FROM attempts a
                  JOIN questions q ON q.id = a.question_id
             LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                 {where_sql}
              GROUP BY a.id
              ORDER BY a.created_at DESC, a.id DESC
                 LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        rows = []
        for note in notes:
            excerpt = note["personal_note"][:180] + ("..." if len(note["personal_note"]) > 180 else "")
            state = "已批改" if note["report_count"] else "待批改"
            state_class = "graded" if note["report_count"] else "pending"
            rows.append(f"""
            <article class="record-card note-record">
              <a class="record-main" href="/attempts/{note["id"]}" aria-label="打开笔记：{esc(note["title"])}">
                <div class="card-top"><span>{esc(format_beijing_time(note["created_at"]))}</span></div>
                <h2>{esc(note["title"])}</h2>
                <p class="paper-line">{esc(note["year"])} {esc(note["region"])} {esc(note["exam_type"])} · {esc(note["question_type"])} · {esc(note["paper_name"])} · 第{note["question_number"] or "?"}题</p>
                <p>{esc(excerpt)}</p>
              </a>
              <div class="record-side"><div class="record-metrics"><div class="record-state {state_class}"><b>{esc(state)}</b></div><span class="note-badge">有笔记</span><small>{note["report_count"]} 份报告</small></div></div>
            </article>""")
        if not rows:
            rows.append(
                '<div class="empty-state"><h2>还没有复盘笔记</h2><p>在批改工作台写下本次作答的复盘后，这里会自动汇总。</p></div>'
            )
        pager = pagination_html("/notes", filters, page, total_items, page_size)
        body = f"""
        <section class="page-head"><div><p class="eyebrow">Review Notes</p><h1>笔记</h1></div></section>
        <form class="filters records-filter notes-filter auto-filter" method="get">
          <label class="search"><span>关键词</span><input name="q" value="{esc(filters["q"])}" placeholder="题目、试卷、笔记内容"></label>
          <button class="button primary" type="submit">筛选</button><a class="button ghost" href="/notes" data-filter-reset>重置</a>
        </form>
        <section class="record-list note-list">{"".join(rows)}</section>
        {pager}
        """
        self.send_html(layout("笔记 - 研申", body, "notes"))

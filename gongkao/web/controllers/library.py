"""Question and paper library controllers."""

from ..runtime import (
    PAPER_WORK_STATUS_OPTIONS,
    QUESTION_WORK_STATUS_OPTIONS,
    connect,
    esc,
    evidence_return_path,
    favorite_button,
    format_beijing_time,
    format_duration,
    layout,
    local_url,
    math,
    option_list,
    pagination_html,
    paper_attempt_duration_seconds,
    pre,
    progress_width,
    question_paper_duration_seconds,
    re,
    referenced_material_numbers,
    report_status,
    requested_page,
    requested_page_size,
    return_path_from_query,
    select_options,
    select_relevant_materials,
    should_use_whole_paper_materials,
    sort_exam_types,
    sort_question_types,
    sort_regions,
    tabbed_materials,
    tabbed_references,
    workflow_header,
    year_range_filter,
)


class LibraryController:
    def page_index(self, query):
        page = requested_page(query)
        page_size = requested_page_size(query)
        filters = {
            "exam_type": query.get("exam_type", [""])[0],
            "region": query.get("region", [""])[0],
            "question_type": query.get("question_type", [""])[0],
            "year_from": query.get("year_from", ["2020"])[0],
            "year_to": query.get("year_to", [""])[0],
            "organization": query.get("organization", [""])[0],
            "work_status": query.get("work_status", [""])[0],
            "q": query.get("q", [""])[0],
            "sort_refs": "1" if query.get("sort_refs", [""])[0] == "1" else "",
        }
        clauses, params = [], []
        for key in ["exam_type", "region", "question_type"]:
            if filters[key]:
                clauses.append(f"q.{key} = ?")
                params.append(filters[key])
        if filters["year_from"]:
            clauses.append("q.year >= ?")
            params.append(filters["year_from"])
        if filters["year_to"]:
            clauses.append("q.year <= ?")
            params.append(filters["year_to"])
        if filters["q"]:
            clauses.append("(q.title LIKE ? OR q.prompt LIKE ? OR q.materials LIKE ?)")
            term = f"%{filters['q']}%"
            params.extend([term, term, term])
        if filters["organization"]:
            clauses.append(
                "EXISTS (SELECT 1 FROM reference_answers rf WHERE rf.question_id = q.id AND COALESCE(NULLIF(rf.canonical_organization, ''), rf.organization) = ?)"
            )
            params.append(filters["organization"])
        where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
        having_clauses = []
        if filters["work_status"] == "attempted":
            having_clauses.append("COUNT(DISTINCT a.id) > 0")
        elif filters["work_status"] == "unattempted":
            having_clauses.append("COUNT(DISTINCT a.id) = 0")
        elif filters["work_status"] == "graded":
            having_clauses.append("COUNT(DISTINCT gr.id) > 0")
        elif filters["work_status"] == "ungraded":
            having_clauses.append("COUNT(DISTINCT a.id) > 0 AND COUNT(DISTINCT gr.id) = 0")
        having_sql = "HAVING " + " AND ".join(having_clauses) if having_clauses else ""
        question_order = (
            "reference_count DESC, q.year DESC, q.zhejiang_relevance DESC, q.id DESC"
            if filters["sort_refs"]
            else "q.year DESC, q.zhejiang_relevance DESC, q.id DESC"
        )

        with connect(self.db_path) as conn:
            total_items = conn.execute(
                f"""
                SELECT COUNT(*)
                  FROM (
                    SELECT q.id,
                           COUNT(DISTINCT COALESCE(NULLIF(r.canonical_organization, ''), r.organization)) AS reference_count
                      FROM questions q
                 LEFT JOIN reference_answers r ON r.question_id = q.id
                 LEFT JOIN attempts a ON a.question_id = q.id
                 LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                         {where_sql}
                  GROUP BY q.id
                         {having_sql}
                  )
                """,
                params,
            ).fetchone()[0]
            total_pages = max(1, math.ceil(total_items / page_size))
            page = min(page, total_pages)
            offset = (page - 1) * page_size
            questions = conn.execute(
                f"""
                SELECT q.*,
                       COUNT(DISTINCT COALESCE(NULLIF(r.canonical_organization, ''), r.organization)) AS reference_count,
                       COUNT(DISTINCT a.id) AS attempt_count,
                       COUNT(DISTINCT gr.id) AS report_count,
                       (SELECT a2.id
                          FROM attempts a2
                         WHERE a2.question_id = q.id
                      ORDER BY a2.created_at DESC, a2.id DESC
                         LIMIT 1) AS latest_attempt_id,
                       EXISTS (SELECT 1 FROM question_favorites f WHERE f.question_id = q.id) AS is_favorite
                  FROM questions q
             LEFT JOIN reference_answers r ON r.question_id = q.id
             LEFT JOIN attempts a ON a.question_id = q.id
             LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                 {where_sql}
              GROUP BY q.id
                 {having_sql}
              ORDER BY {question_order}
                 LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
            stats = conn.execute(
                """
                SELECT COUNT(*) AS question_count,
                       COALESCE(SUM(refs), 0) AS reference_count,
                       COALESCE(SUM(attempts), 0) AS attempt_count,
                       COALESCE(SUM(reports), 0) AS report_count
                  FROM (
                    SELECT q.id, COUNT(DISTINCT r.id) AS refs,
                           COUNT(DISTINCT a.id) AS attempts,
                           COUNT(DISTINCT gr.id) AS reports
                      FROM questions q
                 LEFT JOIN reference_answers r ON r.question_id = q.id
                 LEFT JOIN attempts a ON a.question_id = q.id
                 LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                  GROUP BY q.id
                  )
                """
            ).fetchone()
            facets = {
                "exam_types": sort_exam_types(
                    [row["exam_type"] for row in conn.execute("SELECT DISTINCT exam_type FROM questions")]
                ),
                "regions": sort_regions(
                    [row["region"] for row in conn.execute("SELECT DISTINCT region FROM questions")]
                ),
                "question_types": sort_question_types(
                    [row["question_type"] for row in conn.execute("SELECT DISTINCT question_type FROM questions")]
                ),
                "organizations": [
                    row["name"]
                    for row in conn.execute(
                        "SELECT DISTINCT COALESCE(NULLIF(canonical_organization, ''), organization) AS name FROM reference_answers ORDER BY name"
                    )
                ],
            }
        pager = pagination_html("/", {**filters, "per_page": page_size}, page, total_items, page_size)
        sort_refs_checked = " checked" if filters["sort_refs"] else ""
        quick_filter_fields = "".join(
            f'<input type="hidden" name="{esc(name)}" value="{esc(filters[name])}">'
            for name in ["exam_type", "region", "question_type", "year_from", "year_to", "organization", "work_status"]
        )
        if filters["sort_refs"]:
            quick_filter_fields += '<input type="hidden" name="sort_refs" value="1">'
        active_filter_count = sum(
            bool(filters[name])
            for name in ["exam_type", "region", "question_type", "year_to", "organization", "work_status", "sort_refs"]
        ) + int(bool(filters["year_from"] and filters["year_from"] != "2020"))
        filter_panel_hidden = " hidden"
        filter_expanded = "false"
        filter_count_html = f'<span class="filter-count">{active_filter_count}</span>' if active_filter_count else ""

        cards = []
        return_to = self.path
        for question in questions:
            status = report_status(question["attempt_count"], question["report_count"])
            status_class = "graded" if question["report_count"] else ("done" if question["attempt_count"] else "todo")
            question_href = (
                f"/attempts/{question['latest_attempt_id']}"
                if question["latest_attempt_id"]
                else f"/questions/{question['id']}"
            )
            cards.append(f"""
            <article class="question-card card-shell" data-reference-count="{question["reference_count"]}">
              <a class="card-main-link" href="{question_href}" aria-label="{esc(question["title"])}"></a>
              {favorite_button("questions", question["id"], question["is_favorite"], return_to, compact=True)}
              <div class="card-top"><span>{esc(question["year"])} · {esc(question["region"])}</span><b>{question["reference_count"]} 份机构答案</b></div>
              <h2>{esc(question["title"])}</h2>
              <div class="paper-line">{esc(question["paper_name"] or "未归入试卷")} · {("第" + str(question["question_number"]) + "题") if question["question_number"] else "题号待校对"}</div>
              <p>{esc(question["prompt"])}</p>
              <div class="tags">
                <span>{esc(question["exam_type"])}</span><span>{esc(question["question_type"])}</span>
                <span class="work-status {status_class}">{status}</span>
              </div>
            </article>""")
        if not cards:
            cards.append(
                '<div class="empty-state"><h2>还没有题目</h2><p>从导入页上传题目表和答案表后，这里会变成你的申论训练台。</p><a class="button primary" href="/import">去导入</a></div>'
            )

        metrics_html = f"""
          <div class="metrics">
            <div><strong>{stats["question_count"] or 0}</strong><span>题目</span></div>
            <div><strong>{stats["reference_count"] or 0}</strong><span>参考答案</span></div>
            <div><strong>{stats["attempt_count"] or 0}</strong><span>作答记录</span></div>
            <div><strong>{stats["report_count"] or 0}</strong><span>批改报告</span></div>
          </div>"""
        grid_html = (
            f'<section class="question-grid" data-adaptive-pagination '
            f'data-page="{page}" data-page-size="{page_size}">{"".join(cards)}</section>'
        )
        if self.headers.get("X-Gongkao-List-Partial") == "1":
            self.send_html(f'<div data-list-partial>{metrics_html}{grid_html}{pager}</div>')
            return

        body = f"""
        <section class="page-head">
            <div><p class="eyebrow">题库</p><h1>全部题目</h1><p class="page-lede">按年份、地区与题型找到下一道训练题。</p></div>
          {metrics_html}
        </section>
        <section class="library-toolbar" aria-label="题目搜索与筛选">
          <form class="library-search" method="get">
            <input type="hidden" name="per_page" value="{page_size}">
            {quick_filter_fields}
            <label><span class="sr-only">搜索题目</span><input name="q" value="{esc(filters["q"])}" placeholder="搜索材料、题干或标题"></label>
            <button class="button primary" type="submit">搜索</button>
          </form>
          <button class="button ghost filter-panel-toggle" type="button" data-filter-panel-toggle aria-controls="question-filter-panel" aria-expanded="{filter_expanded}">筛选{filter_count_html}</button>
        </section>
        <form id="question-filter-panel" class="filters overview-filters question-overview-filters auto-filter library-filter-panel" method="get"{filter_panel_hidden}>
          <input type="hidden" name="per_page" value="{page_size}">
          <input type="hidden" name="q" value="{esc(filters["q"])}">
          <label><span>考试类型</span><select name="exam_type">{option_list(facets["exam_types"], filters["exam_type"])}</select></label>
          <label><span>地区</span><select name="region">{option_list(facets["regions"], filters["region"])}</select></label>
          <label><span>题型</span><select name="question_type">{option_list(facets["question_types"], filters["question_type"])}</select></label>
          <label><span>作答状态</span><select name="work_status">{select_options(QUESTION_WORK_STATUS_OPTIONS, filters["work_status"])}</select></label>
          {year_range_filter(filters)}
          <label class="organization-filter"><span>答案来源</span><input name="organization" list="organization-options" value="{esc(filters["organization"])}" placeholder="输入来源名"><datalist id="organization-options">{"".join(f'<option value="{esc(name)}"></option>' for name in facets["organizations"])}</datalist></label>
          <label class="filter-sort-toggle" title="按每道题收录的机构答案数量从多到少排列"><input type="checkbox" name="sort_refs" value="1"{sort_refs_checked}><span>答案数优先</span></label>
          <button class="button primary" type="submit">筛选</button><a class="button ghost" href="/" data-filter-reset>重置</a>
        </form>
        {grid_html}
        {pager}
        """
        self.send_html(layout("题库 - 研申", body, "index"))

    def page_question_by_code(self, path, query):
        question_code = path.removeprefix("/questions/by-code/").strip().upper()
        if not re.fullmatch(r"GKS-\d+-Q\d+", question_code):
            self.send_error(404)
            return
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT id FROM questions WHERE question_code = ?", (question_code,)).fetchone()
        if not row:
            self.send_error(404)
            return
        return_to = return_path_from_query(query, "/")
        self.redirect(local_url(f"/questions/{row['id']}", return_to=return_to))

    def page_papers(self, query):
        page = requested_page(query)
        page_size = requested_page_size(query)
        filters = {
            "exam_type": query.get("exam_type", [""])[0],
            "region": query.get("region", [""])[0],
            "paper_category": query.get("paper_category", [""])[0],
            "year_from": query.get("year_from", ["2020"])[0],
            "year_to": query.get("year_to", [""])[0],
            "work_status": query.get("work_status", [""])[0],
            "q": query.get("q", [""])[0],
            "sort_refs": "1" if query.get("sort_refs", [""])[0] == "1" else "",
        }
        clauses, params = [], []
        for key in ["exam_type", "region", "paper_category"]:
            if filters[key]:
                clauses.append(f"p.{key} = ?")
                params.append(filters[key])
        if filters["year_from"]:
            clauses.append("p.year >= ?")
            params.append(filters["year_from"])
        if filters["year_to"]:
            clauses.append("p.year <= ?")
            params.append(filters["year_to"])
        if filters["q"]:
            clauses.append("(p.paper_name LIKE ? OR p.paper_category LIKE ?)")
            term = f"%{filters['q']}%"
            params.extend([term, term])
        where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
        paper_having = []
        if filters["work_status"] == "started":
            paper_having.append("COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN q.id END) > 0")
        elif filters["work_status"] == "untouched":
            paper_having.append("COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN q.id END) = 0")
        elif filters["work_status"] == "completed":
            paper_having.append(
                "COUNT(DISTINCT q.id) > 0 AND COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN q.id END) >= COUNT(DISTINCT q.id)"
            )
        elif filters["work_status"] == "uncompleted":
            paper_having.append("COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN q.id END) < COUNT(DISTINCT q.id)")
        elif filters["work_status"] == "graded":
            paper_having.append("COUNT(DISTINCT CASE WHEN gr.id IS NOT NULL THEN q.id END) > 0")
        elif filters["work_status"] == "ungraded":
            paper_having.append(
                "COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN q.id END) > 0 AND COUNT(DISTINCT CASE WHEN gr.id IS NOT NULL THEN q.id END) = 0"
            )
        having_sql = "HAVING " + " AND ".join(paper_having) if paper_having else ""
        paper_order = (
            "average_reference_count DESC, reference_count DESC, p.year DESC, p.zhejiang_relevance DESC, p.region, p.paper_category"
            if filters["sort_refs"]
            else "p.year DESC, p.zhejiang_relevance DESC, p.region, p.paper_category"
        )
        with connect(self.db_path) as conn:
            total_items = conn.execute(
                f"""
                SELECT COUNT(*)
                  FROM (
                    SELECT p.id
                      FROM papers p
                 LEFT JOIN questions q ON q.paper_id = p.id
                 LEFT JOIN attempts a ON a.question_id = q.id
                 LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                         {where_sql}
                  GROUP BY p.id
                         {having_sql}
                  )
                """,
                params,
            ).fetchone()[0]
            total_pages = max(1, math.ceil(total_items / page_size))
            page = min(page, total_pages)
            offset = (page - 1) * page_size
            papers = conn.execute(
                f"""
                SELECT p.*,
                       COUNT(DISTINCT q.id) AS question_count,
                       COUNT(DISTINCT m.id) AS material_count,
                       COUNT(DISTINCT CASE WHEN r.id IS NOT NULL THEN
                           CAST(q.id AS TEXT) || CHAR(31) || COALESCE(NULLIF(r.canonical_organization, ''), r.organization)
                       END) AS reference_count,
                       CASE WHEN COUNT(DISTINCT q.id) > 0
                            THEN CAST(COUNT(DISTINCT CASE WHEN r.id IS NOT NULL THEN
                                CAST(q.id AS TEXT) || CHAR(31) || COALESCE(NULLIF(r.canonical_organization, ''), r.organization)
                            END) AS REAL) / COUNT(DISTINCT q.id)
                            ELSE 0 END AS average_reference_count,
                       COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN q.id END) AS attempted_questions,
                       COUNT(DISTINCT CASE WHEN gr.id IS NOT NULL THEN q.id END) AS graded_questions,
                       EXISTS (SELECT 1 FROM paper_favorites f WHERE f.paper_id = p.id) AS is_favorite
                  FROM papers p
             LEFT JOIN questions q ON q.paper_id = p.id
             LEFT JOIN paper_materials m ON m.paper_id = p.id
             LEFT JOIN reference_answers r ON r.question_id = q.id
             LEFT JOIN attempts a ON a.question_id = q.id
             LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                 {where_sql}
              GROUP BY p.id
                 {having_sql}
              ORDER BY {paper_order}
                 LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
            facets = {
                "exam_types": sort_exam_types(
                    [row["exam_type"] for row in conn.execute("SELECT DISTINCT exam_type FROM papers")]
                ),
                "regions": sort_regions([row["region"] for row in conn.execute("SELECT DISTINCT region FROM papers")]),
                "paper_categories": [
                    row["paper_category"]
                    for row in conn.execute(
                        "SELECT DISTINCT paper_category FROM papers WHERE paper_category <> '' ORDER BY paper_category"
                    )
                ],
            }
        pager = pagination_html("/papers", {**filters, "per_page": page_size}, page, total_items, page_size)
        sort_refs_checked = " checked" if filters["sort_refs"] else ""
        quick_filter_fields = "".join(
            f'<input type="hidden" name="{esc(name)}" value="{esc(filters[name])}">'
            for name in ["exam_type", "region", "paper_category", "work_status", "year_from", "year_to"]
        )
        if filters["sort_refs"]:
            quick_filter_fields += '<input type="hidden" name="sort_refs" value="1">'
        active_filter_count = sum(
            bool(filters[name])
            for name in ["exam_type", "region", "paper_category", "work_status", "year_to", "sort_refs"]
        ) + int(bool(filters["year_from"] and filters["year_from"] != "2020"))
        filter_panel_hidden = " hidden"
        filter_expanded = "false"
        filter_count_html = f'<span class="filter-count">{active_filter_count}</span>' if active_filter_count else ""
        cards = []
        return_to = self.path
        for paper in papers:
            total = paper["question_count"] or 0
            attempted = paper["attempted_questions"] or 0
            graded = paper["graded_questions"] or 0
            average_reference_count = float(paper["average_reference_count"] or 0)
            paper_href = f"/papers/{paper['id']}"
            cards.append(f"""
            <article class="question-card paper-card card-shell" data-average-reference-count="{average_reference_count:.6f}">
              <a class="card-main-link" href="{paper_href}" aria-label="{esc(paper["paper_name"])}"></a>
              {favorite_button("papers", paper["id"], paper["is_favorite"], return_to, compact=True)}
              <div class="card-top"><span>{paper["year"]} · {esc(paper["region"])}</span><b>{paper["question_count"]} 题</b></div>
              <h2>{esc(paper["paper_name"])}</h2>
              <p>{esc(paper["exam_type"])} {esc(paper["paper_category"] or "未标注卷种")}，材料 {paper["material_count"]} 组，机构答案 {paper["reference_count"]} 份，平均每题 {average_reference_count:.1f} 份。</p>
              <div class="progress-strip">
                <div class="progress-meter attempted" title="已做 {attempted}/{total}" aria-label="已做 {attempted}/{total}"><i style="width: {progress_width(attempted, total)}"></i><span>已做 {attempted}/{total}</span></div>
                <div class="progress-meter graded" title="已批改 {graded}/{total}" aria-label="已批改 {graded}/{total}"><i style="width: {progress_width(graded, total)}"></i><span>已批改 {graded}/{total}</span></div>
              </div>
              <div class="tags"><span>{esc(paper["exam_type"])}</span><span>{esc(paper["paper_category"] or "未分卷")}</span></div>
            </article>""")
        if not cards:
            cards.append(
                '<div class="empty-state"><h2>还没有试卷</h2><p>导入题目后会自动生成试卷分类。</p><a class="button primary" href="/import">去导入</a></div>'
            )
        grid_html = (
            f'<section class="question-grid" data-adaptive-pagination '
            f'data-page="{page}" data-page-size="{page_size}">{"".join(cards)}</section>'
        )
        if self.headers.get("X-Gongkao-List-Partial") == "1":
            self.send_html(f'<div data-list-partial>{grid_html}{pager}</div>')
            return
        body = f"""
        <section class="page-head"><div><p class="eyebrow">Question Bank</p><h1>试卷题库</h1><p class="page-lede">按地区、年份与卷种快速进入整卷训练。</p></div></section>
        <section class="library-toolbar" aria-label="试卷搜索与筛选">
          <form class="library-search" method="get">
            <input type="hidden" name="per_page" value="{page_size}">
            {quick_filter_fields}
            <label><span class="sr-only">搜索试卷</span><input name="q" value="{esc(filters["q"])}" placeholder="搜索试卷名称或卷种"></label>
            <button class="button primary" type="submit">搜索</button>
          </form>
          <button class="button ghost filter-panel-toggle" type="button" data-filter-panel-toggle aria-controls="paper-filter-panel" aria-expanded="{filter_expanded}">筛选{filter_count_html}</button>
        </section>
        <form id="paper-filter-panel" class="filters overview-filters paper-overview-filters auto-filter library-filter-panel" method="get"{filter_panel_hidden}>
          <input type="hidden" name="per_page" value="{page_size}">
          <input type="hidden" name="q" value="{esc(filters["q"])}">
          <label><span>考试类型</span><select name="exam_type">{option_list(facets["exam_types"], filters["exam_type"])}</select></label>
          <label><span>地区</span><select name="region">{option_list(facets["regions"], filters["region"])}</select></label>
          <label><span>卷种</span><select name="paper_category">{option_list(facets["paper_categories"], filters["paper_category"])}</select></label>
          <label><span>作答状态</span><select name="work_status">{select_options(PAPER_WORK_STATUS_OPTIONS, filters["work_status"])}</select></label>
          {year_range_filter(filters)}
          <label class="filter-sort-toggle" title="按试卷平均每道题的机构答案数量从多到少排列"><input type="checkbox" name="sort_refs" value="1"{sort_refs_checked}><span>题均答案优先</span></label>
          <button class="button primary" type="submit">筛选</button><a class="button ghost" href="/papers" data-filter-reset>重置</a>
        </form>
        {grid_html}
        {pager}
        """
        self.send_html(layout("试卷 - 研申", body, "papers"))

    def page_paper_detail(self, path, query):
        try:
            paper_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return
        selected_q = int(query.get("q", ["0"])[0] or 0)
        with connect(self.db_path) as conn:
            paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if not paper:
                self.send_error(404)
                return
            questions = conn.execute(
                """
                SELECT q.*,
                       COUNT(DISTINCT a.id) AS attempt_count,
                       COUNT(DISTINCT gr.id) AS report_count
                  FROM questions q
             LEFT JOIN attempts a ON a.question_id = q.id
             LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                 WHERE q.paper_id = ?
              GROUP BY q.id
              ORDER BY CASE WHEN q.question_number = 0 THEN 999 ELSE q.question_number END, q.id
                """,
                (paper_id,),
            ).fetchall()
            materials = conn.execute(
                "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
                (paper_id,),
            ).fetchall()
            is_favorite = bool(conn.execute("SELECT 1 FROM paper_favorites WHERE paper_id = ?", (paper_id,)).fetchone())
        if questions and not selected_q:
            selected_q = questions[0]["id"]
        current_q = next((q for q in questions if q["id"] == selected_q), questions[0] if questions else None)
        saved_paper_seconds = 0
        saved_question_seconds = 0
        current_attempts = []
        if current_q:
            with connect(self.db_path) as conn:
                saved_paper_seconds = paper_attempt_duration_seconds(
                    conn, paper_id, exclude_question_id=current_q["id"]
                )
                saved_question_seconds = question_paper_duration_seconds(conn, current_q["id"])
                current_attempts = conn.execute(
                    """
                    SELECT a.*, COUNT(gr.id) AS report_count
                      FROM attempts a
                 LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                     WHERE a.question_id = ?
                  GROUP BY a.id
                  ORDER BY a.created_at DESC, a.id DESC
                    """,
                    (current_q["id"],),
                ).fetchall()
        q_tabs = []
        for index, question in enumerate(questions, start=1):
            number = question["question_number"] or index
            display_number = f"{int(number):02d}" if str(number).isdigit() else str(number)
            active = " active-tab" if current_q and question["id"] == current_q["id"] else ""
            state_class = " graded" if question["report_count"] else (" done" if question["attempt_count"] else " todo")
            state_label = report_status(question["attempt_count"], question["report_count"])
            tab_href = local_url(f"/papers/{paper_id}", q=question["id"])
            q_tabs.append(
                f'<a class="paper-tab{active}{state_class}" href="{tab_href}" title="{state_label}"><span class="paper-tab-number">{display_number}</span><span class="paper-tab-copy"><strong>第{number}题</strong></span><span class="paper-tab-state"><span>{state_label}</span><i class="tab-check" aria-hidden="true"></i></span></a>'
            )

        question_box = '<p class="muted">这套卷还没有题目。</p>'
        if current_q:
            question_href = local_url(
                f"/questions/{current_q['id']}",
                practice="paper",
                timer="auto",
            )
            latest_attempt_link = (
                f'<a class="button secondary" href="/attempts/{current_attempts[0]["id"]}">查看最近批改</a>'
                if current_attempts
                else ""
            )
            question_box = f"""
            <section class="paper-question-focus">
              <div class="paper-focus-label"><span>第{current_q["question_number"] or "?"}题</span><b>{esc(current_q["question_type"])}</b></div>
              <h2>{esc(current_q["title"])}</h2>
              <div class="preline prompt-strong">{pre(current_q["prompt"])}</div>
              <div class="paper-requirements"><strong>作答要求</strong><div class="preline">{pre(current_q["requirements"])}</div></div>
              <div class="paper-actions"><a class="button primary" href="{question_href}">开始作答</a>{latest_attempt_link}</div>
            </section>"""
        paper_attempt_rows = []
        for attempt in current_attempts:
            excerpt = attempt["answer_text"][:80] + ("..." if len(attempt["answer_text"]) > 80 else "")
            duration_text = format_duration(attempt["duration_seconds"], "未计时")
            excluded_text = " · 不计入套卷" if attempt["paper_time_excluded"] else ""
            attempt_href = f"/attempts/{attempt['id']}"
            paper_attempt_rows.append(f"""
            <article class="attempt">
              <header><strong>{esc(format_beijing_time(attempt["created_at"]))}</strong><span>{attempt["word_count"]} 字 · 用时 {esc(duration_text)} · {attempt["report_count"]} 报告{excluded_text}</span></header>
              <p>{esc(excerpt)}</p>
              <div class="attempt-actions"><a class="button small primary" href="{esc(attempt_href)}">进入批改</a><a class="button small" href="/attempts/{attempt["id"]}/package.md">下载批改包</a></div>
            </article>""")
        if not paper_attempt_rows:
            paper_attempt_rows.append('<p class="muted">本题还没有作答记录。</p>')
        paper_attempt_list = (
            f'<details class="paper-attempt-history"><summary>本题历史作答 <span>{len(current_attempts)} 次</span></summary><div class="paper-attempt-history-body">{"".join(paper_attempt_rows)}</div></details>'
            if current_q
            else ""
        )
        relevant_materials = select_relevant_materials(current_q, materials) if current_q else materials
        material_numbers = referenced_material_numbers(current_q) if current_q else []
        material_sections = []
        for material in relevant_materials:
            title = material["title"] or ("材料" + str(material["material_number"]))
            material_sections.append(
                f'<section class="material-section" id="material-{material["material_number"]}">'
                f'<h2>{esc(title)}</h2><div class="preline">{pre(material["content"])}</div></section>'
            )
        material_box = "".join(material_sections)
        if not material_box:
            material_box = '<p class="muted">这道题还没有录入相关材料原文。</p>'
        current_number = current_q["question_number"] if current_q else ""
        if current_q and should_use_whole_paper_materials(current_q):
            material_range = "综合写作题需参考整卷材料，已显示全部给定资料"
        else:
            material_range = (
                "、".join("材料" + str(number) for number in material_numbers)
                if material_numbers
                else "未在题干中标明，暂显示整卷材料"
            )
        attempted_count = sum(1 for question in questions if question["attempt_count"])
        graded_count = sum(1 for question in questions if question["report_count"])
        canonical_paper_path = local_url(f"/papers/{paper_id}", q=current_q["id"] if current_q else None)
        more_html = favorite_button("papers", paper_id, is_favorite, canonical_paper_path)
        if current_q:
            more_html += f'<a href="/questions/{current_q["id"]}/package.md">下载本题批改包</a>'
        body = f"""
        {workflow_header("paper", paper, question=current_q, attempt=current_attempts[0] if current_attempts else None, more_html=more_html)}
        <section class="paper-workspace">
          <aside class="paper-outline">
            <div class="paper-progress-summary">
              <div class="paper-progress-heading"><div><span>整卷进度</span><small>按自己的节奏完成</small></div><strong><b>{attempted_count}</b><em>/{len(questions)}</em></strong></div>
              <div class="paper-progress-track"><i style="width:{progress_width(attempted_count, len(questions))}"></i></div>
              <div class="paper-progress-meta"><span>{graded_count} 题已批改</span><span>{attempted_count} 题已作答</span></div>
            </div>
            <nav class="paper-tabs" aria-label="试卷题目">{"".join(q_tabs)}</nav>
            <div class="practice-timer paper-practice-timer readonly-timer-row" data-paper-summary-timer data-question-timer-key="question-{current_q["id"] if current_q else "none"}" data-paper-base-seconds="{saved_paper_seconds}" data-question-base-seconds="{saved_question_seconds}">
              <span class="timer-pair"><span>本题</span><strong data-timer-display>00:00</strong></span>
              <span class="timer-pair"><span>整卷</span><strong data-timer-display>00:00</strong></span>
            </div>
          </aside>
          <main class="paper-reading">
            {question_box}
            {paper_attempt_list}
            <section class="paper-materials">
              <div class="materials-heading"><span>第{esc(current_number)}题相关材料</span><strong>{len(relevant_materials)} / {len(materials)} 则</strong></div>
              <p class="material-range">{esc(material_range)}</p>
              {material_box}
            </section>
          </main>
        </section>
        """
        self.send_html(layout(f"{paper['paper_name']} - 研申", body, "papers"))

    def page_question(self, path, query):
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
            references = conn.execute(
                "SELECT * FROM reference_answers WHERE question_id = ? ORDER BY COALESCE(NULLIF(canonical_organization, ''), organization), organization",
                (question_id,),
            ).fetchall()
            attempts = conn.execute(
                """
                SELECT a.*, COUNT(gr.id) AS report_count
                  FROM attempts a
             LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
                 WHERE a.question_id = ?
              GROUP BY a.id
              ORDER BY a.created_at DESC, a.id DESC
                """,
                (question_id,),
            ).fetchall()
            materials = (
                conn.execute(
                    "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
                    (question["paper_id"],),
                ).fetchall()
                if question["paper_id"]
                else []
            )
            material_annotations = {
                row["material_number"]: row
                for row in conn.execute(
                    """
                    SELECT * FROM text_annotations
                     WHERE target_type = 'material' AND question_id = ?
                    """,
                    (question_id,),
                )
            }
            is_favorite = bool(
                conn.execute("SELECT 1 FROM question_favorites WHERE question_id = ?", (question_id,)).fetchone()
            )
            saved_paper_seconds = (
                paper_attempt_duration_seconds(conn, question["paper_id"], exclude_question_id=question_id)
                if question["paper_id"]
                else 0
            )
            saved_question_seconds = question_paper_duration_seconds(conn, question_id) if question["paper_id"] else 0
        relevant_materials = select_relevant_materials(question, materials)
        context_return = evidence_return_path(query)
        citation_target = bool(context_return)
        active_section = "papers" if question["paper_id"] else "index"
        paper_timer_display = (
            '<span class="timer-pair linked-timer"><span>整卷累计</span><strong data-paper-derived-display>00:00</strong></span>'
            if question["paper_id"]
            else ""
        )
        paper_exclude_toggle = (
            '<label class="timer-exclude-toggle"><input type="checkbox" name="paper_time_excluded" value="1" data-paper-time-excluded><span>不统计</span></label>'
            if question["paper_id"]
            else ""
        )
        # Only explicit timed entry points start the clock. The workflow's
        # "02 写答案" link intentionally has no timer query parameter.
        auto_timer_attr = (
            ' data-timer-autostart="1"'
            if query.get("timer", [""])[0] == "auto"
            else ""
        )

        materials_html = tabbed_materials(
            relevant_materials,
            f"question-{question_id}",
            highlight_scope=f"question-{question_id}",
            question_id=question_id,
            saved_annotations=material_annotations,
        )
        refs_html = tabbed_references(references, f"question-{question_id}")

        attempts_html = []
        for attempt in attempts:
            excerpt = attempt["answer_text"][:120] + ("..." if len(attempt["answer_text"]) > 120 else "")
            duration_text = format_duration(attempt["duration_seconds"], "未计时")
            attempt_href = f"/attempts/{attempt['id']}"
            attempts_html.append(f"""
            <article class="attempt">
              <header><strong>{esc(format_beijing_time(attempt["created_at"]))}</strong><span>{attempt["word_count"]} 字 · 用时 {esc(duration_text)} · {attempt["report_count"]} 报告</span></header>
              <p>{esc(excerpt)}</p><div class="attempt-actions"><a class="button small primary" href="{esc(attempt_href)}">进入批改</a><a class="button small" href="/attempts/{attempt["id"]}/package.md">下载批改包</a></div>
            </article>""")
        if not attempts_html:
            attempts_html.append('<p class="muted">还没有作答记录。</p>')

        question_href = f"/questions/{question['id']}"
        more_html = favorite_button("questions", question_id, is_favorite, question_href)
        more_html += f'<a href="/questions/{question_id}/package.md">下载空白批改包</a>'
        body = f"""
        {workflow_header("answer", question, question=question, attempt=attempts[0] if attempts else None, more_html=more_html, context_return=context_return)}
        <section class="answer-workspace" data-resizable-attempt-pane data-resize-storage-key="gongkao.attemptPaneWidth" data-default-side-width="500" data-min-main-width="340" data-min-side-width="320">
          <article class="answer-reading">
            <section class="question-brief">
              <div class="question-brief-meta"><span>{esc(question["question_type"])}</span><span>{esc(question["word_limit"] or "字数未标注")}</span></div>
              <div class="preline prompt-strong">{pre(question["prompt"])}</div>
              <div class="question-requirements"><strong>作答要求</strong><div class="preline">{pre(question["requirements"])}</div></div>
            </section>
            <section class="reading-materials"><div class="section-heading"><div><p class="eyebrow">Reading</p><h2>本题相关材料</h2></div><span>{len(relevant_materials)} 则</span></div>{materials_html}</section>
            <details class="reference-disclosure">
              <summary><span>参考答案</span><small>{len(references)} 份，作答后再看</small></summary>
              <div class="reference-disclosure-body">{refs_html}</div>
            </details>
            <details class="answer-history">
              <summary>历史作答 <span>{len(attempts)} 次</span></summary>
              <div class="answer-history-body">{"".join(attempts_html)}</div>
            </details>
          </article>
          <button class="pane-resizer" type="button" aria-label="调整答题区宽度" data-pane-resizer></button>
          <aside class="answer-compose">
            <form method="post" action="/questions/{question["id"]}/attempts" class="attempt-form focused-attempt-form" data-answer-form data-word-limit="{esc(question["word_limit"])}">
              <div class="practice-timer combined-practice-timer" data-practice-timer data-timer-kind="question" data-timer-key="question-{question["id"]}" data-paper-base-seconds="{saved_paper_seconds}" data-question-base-seconds="{saved_question_seconds}" data-timer-clear-on-submit="1"{auto_timer_attr}>
                <span class="timer-pair"><span>本题用时</span><strong data-timer-display>00:00</strong></span>
                {paper_timer_display}
                {paper_exclude_toggle}
                <button type="button" class="button ghost small" data-timer-toggle>开始</button>
                <button type="button" class="button ghost small" data-timer-reset>重置</button>
              </div>
              <input type="hidden" name="duration_seconds" value="0" data-duration-input>
              <input type="hidden" name="paper_elapsed_seconds" value="0" data-paper-duration-input>
              <div class="answer-input-field"><span>我的答案</span>
                <div class="answer-editor-toolbar" data-editor-toolbar data-editor-target="#question-answer-input">
                  <span class="answer-editor-toolbar-label">排版</span>
                  <button type="button" class="is-active" data-editor-align="left" title="左对齐">左</button>
                  <button type="button" data-editor-align="center" title="居中">中</button>
                  <button type="button" data-editor-align="right" title="右对齐">右</button>
                </div>
                <textarea name="answer_text" data-answer-hidden hidden></textarea>
                <input type="hidden" name="answer_format_json" value="[]" data-answer-format-hidden>
                <div id="question-answer-input" class="direct-text-editor answer-compose-editor" contenteditable="true" role="textbox" aria-label="我的答案" spellcheck="false" data-answer-input data-empty-text="从材料中提炼要点，在这里完成本次作答……"></div>
              </div>
              <div class="answer-counter" aria-live="polite"><span>当前 <strong data-word-count>0</strong> 字</span><span data-line-status>行数：0</span><span data-current-line-status>本行：0/25格</span><span data-word-status>开始作答后实时统计</span><span data-autosave-status>草稿自动保存开启</span></div>
              <button class="button primary answer-submit" type="submit">提交作答并进入批改</button>
            </form>
          </aside>
        </section>"""
        self.send_html(
            layout(
                f"{question['title']} - 研申",
                body,
                active_section,
                transient_route=citation_target,
            )
        )

    def page_favorites(self, query):
        kind = query.get("kind", ["questions"])[0]
        if kind not in ("questions", "papers"):
            kind = "questions"
        with connect(self.db_path) as conn:
            counts = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM question_favorites) AS questions,
                  (SELECT COUNT(*) FROM paper_favorites) AS papers
                """
            ).fetchone()
            questions = (
                conn.execute(
                    """
                SELECT q.*, f.created_at AS favorited_at,
                       COUNT(DISTINCT r.id) AS reference_count,
                       COUNT(DISTINCT a.id) AS attempt_count,
                       COUNT(DISTINCT gr.id) AS report_count,
                       (SELECT a2.id
                          FROM attempts a2
                         WHERE a2.question_id = q.id
                      ORDER BY a2.created_at DESC, a2.id DESC
                         LIMIT 1) AS latest_attempt_id
                  FROM question_favorites f
                  JOIN questions q ON q.id = f.question_id
             LEFT JOIN reference_answers r ON r.question_id = q.id
             LEFT JOIN attempts a ON a.question_id = q.id
             LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
              GROUP BY q.id
              ORDER BY f.created_at DESC, f.id DESC
                """
                ).fetchall()
                if kind == "questions"
                else []
            )
            papers = (
                conn.execute(
                    """
                SELECT p.*, f.created_at AS favorited_at,
                       COUNT(DISTINCT q.id) AS question_count,
                       COUNT(DISTINCT m.id) AS material_count,
                       COUNT(DISTINCT r.id) AS reference_count,
                       COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN q.id END) AS attempted_questions,
                       COUNT(DISTINCT CASE WHEN gr.id IS NOT NULL THEN q.id END) AS graded_questions
                  FROM paper_favorites f
                  JOIN papers p ON p.id = f.paper_id
             LEFT JOIN questions q ON q.paper_id = p.id
             LEFT JOIN paper_materials m ON m.paper_id = p.id
             LEFT JOIN reference_answers r ON r.question_id = q.id
             LEFT JOIN attempts a ON a.question_id = q.id
             LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
              GROUP BY p.id
              ORDER BY f.created_at DESC, f.id DESC
                """
                ).fetchall()
                if kind == "papers"
                else []
            )

        cards = []
        if kind == "questions":
            for question in questions:
                status = report_status(question["attempt_count"], question["report_count"])
                status_class = (
                    "graded" if question["report_count"] else ("done" if question["attempt_count"] else "todo")
                )
                question_href = (
                    f"/attempts/{question['latest_attempt_id']}"
                    if question["latest_attempt_id"]
                    else f"/questions/{question['id']}"
                )
                cards.append(f"""
                <article class="question-card card-shell">
                  <a class="card-main-link" href="{question_href}" aria-label="{esc(question["title"])}"></a>
                  {favorite_button("questions", question["id"], True, self.path, compact=True)}
                  <div class="card-top"><span>{question["year"]} · {esc(question["region"])}</span><b>{question["reference_count"]} 答案</b></div>
                  <h2>{esc(question["title"])}</h2>
                  <div class="paper-line">{esc(question["paper_name"])} · 第{question["question_number"] or "?"}题</div>
                  <p>{esc(question["prompt"])}</p>
                  <div class="tags"><span>{esc(question["question_type"])}</span><span class="work-status {status_class}">{status}</span></div>
                </article>""")
        else:
            for paper in papers:
                total = paper["question_count"] or 0
                attempted = paper["attempted_questions"] or 0
                graded = paper["graded_questions"] or 0
                cards.append(f"""
                <article class="question-card paper-card card-shell">
                  <a class="card-main-link" href="/papers/{paper["id"]}" aria-label="{esc(paper["paper_name"])}"></a>
                  {favorite_button("papers", paper["id"], True, self.path, compact=True)}
                  <div class="card-top"><span>{paper["year"]} · {esc(paper["region"])}</span><b>{total} 题</b></div>
                  <h2>{esc(paper["paper_name"])}</h2>
                  <p>{esc(paper["exam_type"])} {esc(paper["paper_category"])}，材料 {paper["material_count"]} 组，参考答案 {paper["reference_count"]} 份。</p>
                  <div class="progress-strip">
                    <div class="progress-meter" aria-label="已做 {attempted}/{total}"><i style="width:{progress_width(attempted, total)}"></i><span>已做 {attempted}/{total}</span></div>
                    <div class="progress-meter" aria-label="已批改 {graded}/{total}"><i style="width:{progress_width(graded, total)}"></i><span>已批改 {graded}/{total}</span></div>
                  </div>
                  <div class="tags"><span>{esc(paper["exam_type"])}</span><span>{esc(paper["paper_category"])}</span></div>
                </article>""")
        if not cards:
            noun = "题目" if kind == "questions" else "试卷"
            cards.append(
                f'<div class="empty-state"><h2>还没有收藏的{noun}</h2><p>在{noun}卡片或详情页点击星标后，会集中显示在这里。</p></div>'
            )

        body = f"""
        <section class="page-head">
          <div><p class="eyebrow">Review Collection</p><h1>收藏夹</h1></div>
          <div class="metrics"><div><strong>{counts["questions"]}</strong><span>收藏题目</span></div><div><strong>{counts["papers"]}</strong><span>收藏试卷</span></div></div>
        </section>
        <nav class="view-tabs" aria-label="收藏类型">
          <a class="{"active" if kind == "questions" else ""}" href="/favorites?kind=questions">收藏的题目 <span>{counts["questions"]}</span></a>
          <a class="{"active" if kind == "papers" else ""}" href="/favorites?kind=papers">收藏的试卷 <span>{counts["papers"]}</span></a>
        </nav>
        <section class="question-grid">{"".join(cards)}</section>
        """
        self.send_html(layout("收藏夹 - 研申", body, "favorites"))

    def page_coverage(self, query):
        group = query.get("group", [""])[0]
        clauses, params = [], []
        if group:
            clauses.append("target_group = ?")
            params.append(group)
        where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
        with connect(self.db_path) as conn:
            targets = conn.execute(
                f"""
                SELECT t.*,
                       COUNT(DISTINCT q.id) AS question_count,
                       COUNT(DISTINCT CASE WHEN q.is_full_original = 1 THEN q.id END) AS full_original_count,
                       COUNT(DISTINCT r.id) AS reference_count
                  FROM coverage_targets t
             LEFT JOIN questions q
                    ON q.year = t.year
                   AND q.exam_type = t.exam_type
                   AND q.region = t.region
             LEFT JOIN reference_answers r ON r.question_id = q.id
                 {where_sql}
              GROUP BY t.id
              ORDER BY t.priority ASC, t.year DESC, t.zhejiang_relevance DESC, t.region, t.exam_type
                """,
                params,
            ).fetchall()
            groups = [
                row["target_group"]
                for row in conn.execute(
                    "SELECT DISTINCT target_group FROM coverage_targets ORDER BY priority, target_group"
                )
            ]

        rows = []
        for target in targets:
            full_count = target["full_original_count"] or 0
            if target["question_count"] and full_count == target["question_count"]:
                status = "已有原文"
            elif full_count:
                status = "部分原文待补"
            elif target["question_count"]:
                status = "有题目待补原文"
            else:
                status = target["status"]
            rows.append(f"""
            <tr>
              <td>{target["year"]}</td>
              <td>{esc(target["target_group"])}</td>
              <td>{esc(target["region"])}</td>
              <td>{esc(target["exam_type"])}</td>
              <td>{esc(target["paper_name"])}</td>
              <td><span class="status {"ok" if target["question_count"] and full_count == target["question_count"] else "error"}">{esc(status)}</span></td>
              <td>{target["question_count"] or 0}</td>
              <td>{full_count}</td>
              <td>{target["reference_count"] or 0}</td>
              <td>{esc(target["notes"])}</td>
            </tr>""")

        body = f"""
        <section class="page-head">
          <div><p class="eyebrow">Coverage</p><h1>覆盖清单</h1></div>
          <div class="actions"><a class="button ghost" href="/templates/questions_template.csv">下载原文导入模板</a></div>
        </section>
        <form class="filters coverage-filter auto-filter" method="get">
          <label><span>范围</span><select name="group">{option_list(groups, group)}</select></label>
          <button class="button primary" type="submit">筛选</button><a class="button ghost" href="/coverage" data-filter-reset>重置</a>
        </form>
        <section class="table-panel">
          <h2>2020 年后目标范围</h2>
          <table>
            <thead><tr><th>年份</th><th>分组</th><th>地区</th><th>考试</th><th>目标卷</th><th>状态</th><th>题目</th><th>完整原文</th><th>参考答案</th><th>备注</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
        </section>
        """
        self.send_html(layout("覆盖清单 - 研申", body, "coverage"))

"""Question and paper library controllers."""

import json
from urllib.parse import parse_qs, urlparse

from ...ai import chat_completion
from ...ai_solver import get_question_solution, solve_question_with_ai
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
    markdownish,
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
from ...services.paper_builder import (
    add_paper_question,
    add_question_reference_answer,
    create_custom_paper,
    parse_raw_paper_text,
)
from ...services.url_importer import (
    FENBI_CREDENTIALS,
    IMPORT_SESSIONS,
    MAX_BRIDGE_BYTES,
    UrlImportError,
    build_fenbi_bookmarklet,
    draft_from_bridge_payload,
    fenbi_credentials_summary,
    fetch_source_draft,
    parse_fenbi_credentials,
    parse_source_url,
    public_import_summary,
)


def fenbi_score_tree_html(raw_tree):
    """Render a stored Fenbi score-analysis tree as a compact nested list."""

    try:
        tree = json.loads(raw_tree) if isinstance(raw_tree, str) else raw_tree
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(tree, dict):
        return ""

    def render_node(node):
        children = [child for child in (node.get("children") or []) if isinstance(child, dict)]
        full_mark = node.get("full_mark")
        if full_mark is not None:
            try:
                full_mark = float(full_mark)
            except (TypeError, ValueError):
                full_mark = None
        score_suffix = f"（{full_mark:g}分）" if full_mark else ""
        comment = str(node.get("comment") or "").strip()
        html = (
            "<li>"
            f"<strong>{esc(node.get('name') or '得分分析')}</strong> {esc(score_suffix)}"
        )
        if comment:
            html += f'<div class="muted" style="font-size:0.82rem;margin:0.2rem 0 0.35rem;">{esc(comment)}</div>'
        if children:
            html += "<ul>" + "".join(render_node(child) for child in children) + "</ul>"
        html += "</li>"
        return html

    return (
        '<section class="fenbi-score-tree" style="margin-top:1rem;padding:1rem;border:1px solid var(--line, #e2e8f0);border-radius:8px;background:var(--bg-card, #ffffff);">'
        '<h3 style="margin:0 0 0.5rem;">粉笔踩分树</h3>'
        f'<ul style="margin:0;padding-left:1.2rem;">{render_node(tree)}</ul>'
        "</section>"
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
        <section class="page-head">
          <div><p class="eyebrow">Question Bank</p><h1>试卷题库</h1><p class="page-lede">按地区、年份与卷种快速进入整卷训练。</p></div>
          <div class="actions">
            <a class="button primary" href="/papers/new">录入新套卷</a>
            <a class="button ghost" href="/import">批量表格导入</a>
          </div>
        </section>
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
        q_tabs.append(
            f'<a class="paper-tab add-question-tab" href="/papers/{paper_id}/questions/new" title="为此套卷添加新题目" style="border-style:dashed;opacity:0.85;"><span class="paper-tab-number">+</span><span class="paper-tab-copy"><strong>添加新题</strong></span></a>'
        )

        question_box = f'<div class="empty-state"><h2>这套卷还没有题目</h2><p>你可以直接为这套卷录入第一道题目。</p><a class="button primary" href="/papers/{paper_id}/questions/new">+ 录入题目</a></div>'
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
              <div class="paper-actions"><a class="button primary" href="{question_href}">开始作答</a>{latest_attempt_link}<a class="button ghost" href="/papers/{paper_id}/questions/new">+ 添加新题</a></div>
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
            ai_solution = get_question_solution(conn, question_id)
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
        score_tree_html = ""
        for reference in references:
            reference = dict(reference)
            if reference.get("score_tree_json"):
                score_tree_html = fenbi_score_tree_html(reference["score_tree_json"])
                if score_tree_html:
                    break

        solve_error = query.get("solve_error", [""])[0]
        solve_error_banner = ""
        if solve_error:
            if solve_error == "no_api_key":
                solve_error_banner = '<div class="banner error" style="padding:0.75rem 1rem;margin-top:1rem;margin-bottom:1rem;background:#fee2e2;border:1px solid #ef4444;border-radius:6px;color:#991b1b;font-size:0.9rem;">⚠️ 无法执行 AI 解题：尚未配置 AI API Key，请前往 <a href="/settings" style="text-decoration:underline;font-weight:600;">设置页面</a> 配置模型与 API Key。</div>'
            else:
                solve_error_banner = f'<div class="banner error" style="padding:0.75rem 1rem;margin-top:1rem;margin-bottom:1rem;background:#fee2e2;border:1px solid #ef4444;border-radius:6px;color:#991b1b;font-size:0.9rem;">⚠️ AI 解题遇到问题: {esc(solve_error)}</div>'

        if ai_solution:
            ai_solution_html = f"""
            <section class="ai-solution-card" style="margin-top: 1.25rem; border: 1px solid var(--line, #e2e8f0); border-radius: 8px; padding: 1.25rem; background: var(--bg-card, #ffffff);">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; border-bottom: 1px solid var(--line, #e2e8f0); padding-bottom: 0.75rem; flex-wrap: wrap; gap: 0.5rem;">
                <div>
                  <span class="badge ok" style="margin-right: 0.5rem;">Shenlun.skill 体系自主解题</span>
                  <strong style="font-size: 1.05rem;">名师双标答与采分点</strong>
                  <small class="muted" style="margin-left: 0.5rem;">模型: {esc(ai_solution["model_name"] or "AI")} · {esc(format_beijing_time(ai_solution["created_at"]))}</small>
                </div>
                <form method="post" action="/questions/{question_id}/solve" style="margin: 0;">
                  <button class="button small ghost" type="submit">重新推导</button>
                </form>
              </div>
              <div class="tabbed-content" data-tabs>
                <div class="content-tabs" role="tablist" aria-label="AI解题方案">
                  <button class="content-tab active-tab" id="sol-tab-xiaomage" type="button" role="tab" aria-selected="true" aria-controls="sol-panel-xiaomage" data-tab-target="sol-panel-xiaomage">小马哥原词流标答</button>
                  <button class="content-tab" id="sol-tab-bailu" type="button" role="tab" aria-selected="false" aria-controls="sol-panel-bailu" data-tab-target="sol-panel-bailu">白鹭提炼流标答</button>
                  <button class="content-tab" id="sol-tab-rubric" type="button" role="tab" aria-selected="false" aria-controls="sol-panel-rubric" data-tab-target="sol-panel-rubric">客观采分点清单</button>
                  <button class="content-tab" id="sol-tab-audit" type="button" role="tab" aria-selected="false" aria-controls="sol-panel-audit" data-tab-target="sol-panel-audit">参考答案审计纠错</button>
                </div>
                <article class="tab-panel active-panel" id="sol-panel-xiaomage" role="tabpanel" aria-labelledby="sol-tab-xiaomage">
                  <div class="markdown-body" style="padding: 0.75rem 0;">{markdownish(ai_solution["xiaomage_answer"] or "未生成")}</div>
                </article>
                <article class="tab-panel" id="sol-panel-bailu" role="tabpanel" aria-labelledby="sol-tab-bailu" hidden>
                  <div class="markdown-body" style="padding: 0.75rem 0;">{markdownish(ai_solution["bailu_answer"] or "未生成")}</div>
                </article>
                <article class="tab-panel" id="sol-panel-rubric" role="tabpanel" aria-labelledby="sol-tab-rubric" hidden>
                  <div class="markdown-body" style="padding: 0.75rem 0;">{markdownish(ai_solution["scoring_points"] or "未生成")}</div>
                </article>
                <article class="tab-panel" id="sol-panel-audit" role="tabpanel" aria-labelledby="sol-tab-audit" hidden>
                  <div class="markdown-body" style="padding: 0.75rem 0;">{markdownish(ai_solution["reference_audit"] or "无审计信息")}</div>
                </article>
              </div>
            </section>
            """
        else:
            ai_solution_html = f"""
            <section class="card" style="margin-top: 1.25rem; padding: 1rem 1.25rem; border: 1px dashed var(--accent, #3b82f6); border-radius: 8px; background: rgba(59, 130, 246, 0.04);">
              <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem;">
                <div>
                  <strong style="font-size: 0.98rem; display: block; margin-bottom: 0.25rem;">🤖 AI 导师自主解题 (Shenlun.skill 体系)</strong>
                  <span class="muted" style="font-size: 0.85rem;">机构参考答案不准确？点击由 AI 导师严格依据材料，自底向上推导“小马哥原词流”与“白鹭提炼流”双标答与客观采分清单。</span>
                </div>
                <form method="post" action="/questions/{question_id}/solve" style="margin: 0;">
                  <button class="button primary small" type="submit">AI 导师自主解题</button>
                </form>
              </div>
            </section>
            """

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
              <div class="reference-disclosure-body">
                {refs_html}
                {score_tree_html}
                <div style="margin-top: 1rem; padding: 1rem; border: 1px solid var(--line, #e2e8f0); border-radius: 6px; background: var(--bg-card, #ffffff);">
                  <strong style="display:block;margin-bottom:0.5rem;font-size:0.92rem;">+ 录入新参考答案</strong>
                  <p class="muted" style="font-size: 0.82rem; margin-bottom: 0.75rem;">录入机构（华图、粉笔、中公等）或个人答案。AI 评分将以真题材料和名师体系为主，对参考答案进行客观审计纠错。</p>
                  <form method="post" action="/questions/{question_id}/references/new">
                    <div style="display: flex; gap: 0.75rem; margin-bottom: 0.5rem; flex-wrap: wrap;">
                      <input type="text" name="organization" placeholder="机构/来源（如：粉笔、华图、自拟）" required style="flex: 1; min-width: 180px;" />
                      <input type="number" name="score" placeholder="预估满分(选填)" style="width: 120px;" />
                    </div>
                    <textarea name="answer_text" rows="4" placeholder="粘贴参考答案正文..." required style="width: 100%; margin-bottom: 0.5rem; font-family: inherit; font-size: 0.88rem;"></textarea>
                    <button class="button small secondary" type="submit">保存参考答案</button>
                  </form>
                </div>
              </div>
            </details>
            {solve_error_banner}
            {ai_solution_html}
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
        page = requested_page(query)
        page_size = 12
        with connect(self.db_path) as conn:
            counts = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM question_favorites) AS questions,
                  (SELECT COUNT(*) FROM paper_favorites) AS papers
                """
            ).fetchone()
            total_items = int(counts[kind] or 0)
            total_pages = max(1, math.ceil(total_items / page_size))
            page = min(page, total_pages)
            offset = (page - 1) * page_size
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
                  LIMIT ? OFFSET ?
                """,
                    (page_size, offset),
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
                  LIMIT ? OFFSET ?
                """,
                    (page_size, offset),
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

        pager = pagination_html("/favorites", {"kind": kind}, page, total_items, page_size)
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
        {pager}
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

    def page_paper_new(self, query=None):
        query = query or {}
        import_session_id = (query.get("import_session") or [""])[0].strip()
        import_session = IMPORT_SESSIONS.get(import_session_id) if import_session_id else None
        import_draft = import_session.get("draft") if import_session else None
        import_seed = json.dumps(import_draft, ensure_ascii=False, default=str) if import_draft else "null"
        # Keep the JSON in a non-executable script node and escape HTML-sensitive
        # characters so imported source text cannot break the page.
        import_seed = (
            import_seed.replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        import_source_url = ""
        if import_draft:
            import_source_url = str(import_draft.get("source_url") or "")
        credentials_summary = fenbi_credentials_summary(FENBI_CREDENTIALS.load())
        body = """
        <section class="page-head">
          <div>
            <p class="eyebrow">Personal Exam Builder</p>
            <h1>新建试卷 / 录入新套卷</h1>
            <p class="page-lede">一句话填写试卷信息，点击 ➕ 依序录入给定资料与题目（题目+一份参考答案）。</p>
          </div>
          <div class="actions">
            <a class="button ghost" href="/papers">返回套卷列表</a>
          </div>
        </section>

        <!-- URL_IMPORT_PANEL -->

        <!-- 1. 试卷基本信息（一句话填写） -->
        <section class="card" style="margin-bottom: 1.5rem; padding: 1.5rem; border: 1px solid var(--line, #e2e8f0); border-radius: 8px; background: var(--bg-card, #ffffff);">
          <div style="margin-bottom: 0.25rem;">
            <label for="field-paper-name" style="display: block; font-weight: 700; font-size: 1.05rem; margin-bottom: 0.4rem; color: var(--text, #1e293b);">
              试卷基本信息 <span style="color: #ef4444;">*</span>
            </label>
            <p class="muted" style="margin: 0 0 0.6rem 0; font-size: 0.88rem;">一句话填写好即可，例如：2024申论模考一、某某个人专项练习。</p>
            <input id="field-paper-name" name="paper_name" type="text" required placeholder="输入试卷名称或一句话描述..." style="width: 100%; padding: 0.75rem 1rem; font-size: 1rem; border: 1px solid var(--line, #cbd5e1); border-radius: 6px; box-sizing: border-box;">
          </div>
        </section>

        <!-- 2. 给定资料（使用➕，点击新增输入框，往里面按顺序输入给定资料） -->
        <section class="card" style="margin-bottom: 1.5rem; padding: 1.5rem; border: 1px solid var(--line, #e2e8f0); border-radius: 8px; background: var(--bg-card, #ffffff);">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
            <div>
              <h2 style="margin: 0; font-size: 1.15rem; color: var(--text, #1e293b);">给定资料</h2>
              <p class="muted" style="margin: 0.25rem 0 0 0; font-size: 0.85rem;">按顺序输入给定资料，点击右侧 ➕ 即可新增资料输入框。</p>
            </div>
            <button type="button" class="button secondary" onclick="addMaterialField()" style="display: inline-flex; align-items: center; gap: 0.35rem; font-weight: 600;">
              <span style="font-size: 1.2rem; line-height: 1;">+</span> 新增给定资料
            </button>
          </div>

          <div id="materials-container" style="display: flex; flex-direction: column; gap: 1rem;">
            <!-- 动态资料输入框容器 -->
          </div>
        </section>

        <!-- 3. 题目与参考答案（点击新增题目的➕，新增两个输入框，一个是题目，一个是参考答案。答案只有一份） -->
        <section class="card" style="margin-bottom: 1.5rem; padding: 1.5rem; border: 1px solid var(--line, #e2e8f0); border-radius: 8px; background: var(--bg-card, #ffffff);">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
            <div>
              <h2 style="margin: 0; font-size: 1.15rem; color: var(--text, #1e293b);">题目与参考答案</h2>
              <p class="muted" style="margin: 0.25rem 0 0 0; font-size: 0.85rem;">点击 ➕ 新增题目，每道题提供题目输入框与一份参考答案输入框。AI 将依材料自主做题，参考答案仅作对照纠错。</p>
            </div>
            <button type="button" class="button secondary" onclick="addQuestionField()" style="display: inline-flex; align-items: center; gap: 0.35rem; font-weight: 600;">
              <span style="font-size: 1.2rem; line-height: 1;">+</span> 新增题目
            </button>
          </div>

          <div id="questions-container" style="display: flex; flex-direction: column; gap: 1.25rem;">
            <!-- 动态题目卡片容器 -->
          </div>
        </section>

        <!-- 4. 底部操作栏 -->
        <section style="display: flex; align-items: center; gap: 1rem; margin-top: 1.5rem; margin-bottom: 3rem;">
          <button type="button" class="button primary" id="btn-save-paper" onclick="submitCustomPaper()" style="font-size: 1.05rem; padding: 0.75rem 2.25rem; font-weight: 600;">
            保存卷子并开始写题
          </button>
          <a class="button ghost" href="/papers">取消</a>
          <span id="submit-status" style="font-size: 0.9rem;"></span>
        </section>

        <script>
        function addMaterialField(initialText = '') {
          const container = document.getElementById('materials-container');
          const div = document.createElement('div');
          div.className = 'material-card';
          div.style.cssText = 'border: 1px solid var(--line, #e2e8f0); border-radius: 6px; padding: 1rem; background: var(--bg-alt, #f8fafc);';
          div.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <strong class="mat-badge" style="color: var(--blue, #2563eb); font-size: 0.95rem;">给定资料</strong>
              <button type="button" class="button ghost small" onclick="removeMaterialField(this)" style="color: #ef4444; padding: 0.2rem 0.5rem;" title="删除此篇资料">✕ 删除</button>
            </div>
            <textarea class="mat-content" rows="6" placeholder="请按顺序输入或粘贴资料正文..." style="width: 100%; box-sizing: border-box; font-family: inherit; font-size: 0.92rem; padding: 0.75rem; border: 1px solid var(--line, #cbd5e1); border-radius: 6px;"></textarea>
          `;
          if (initialText) {
            div.querySelector('.mat-content').value = initialText;
          }
          container.appendChild(div);
          renumberMaterials();
        }

        function removeMaterialField(btn) {
          const card = btn.closest('.material-card');
          if (card) {
            card.remove();
            renumberMaterials();
          }
        }

        function renumberMaterials() {
          const cards = document.querySelectorAll('#materials-container .material-card');
          cards.forEach((card, idx) => {
            const badge = card.querySelector('.mat-badge');
            if (badge) badge.innerText = `给定资料 ${idx + 1}`;
            const textarea = card.querySelector('.mat-content');
            if (textarea && !textarea.value) {
              textarea.placeholder = `请按顺序输入或粘贴第 ${idx + 1} 篇给定资料正文...`;
            }
          });
        }

        function addQuestionField(initialPrompt = '', initialRef = '', initialReferenceMeta = null) {
          const container = document.getElementById('questions-container');
          const div = document.createElement('div');
          div.className = 'question-card-item';
          div.style.cssText = 'border: 1px solid var(--line, #e2e8f0); border-radius: 6px; padding: 1.25rem; background: var(--bg-alt, #f8fafc);';
          div.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
              <div style="display: flex; align-items: center; gap: 0.5rem;">
                <strong class="q-badge" style="color: var(--green, #16a34a); font-size: 1rem;">第 1 题</strong>
                <span style="font-size: 0.8rem; color: var(--muted, #64748b);">（题目 + 一份参考答案）</span>
              </div>
              <button type="button" class="button ghost small" onclick="removeQuestionField(this)" style="color: #ef4444; padding: 0.2rem 0.5rem;" title="删除此题">✕ 删除题目</button>
            </div>
            <div style="margin-bottom: 0.85rem;">
              <label style="display: block; font-weight: 600; font-size: 0.9rem; margin-bottom: 0.35rem; color: var(--text, #1e293b);">
                题目（题干与作答要求） <span style="color: #ef4444;">*</span>
              </label>
              <textarea class="q-prompt" rows="3" placeholder="在此输入题目内容与作答要求，例如：根据“给定资料1”，概括某某的主要做法。要求：全面、准确、有条理，不超过200字。（20分）" style="width: 100%; box-sizing: border-box; font-family: inherit; font-size: 0.92rem; padding: 0.75rem; border: 1px solid var(--line, #cbd5e1); border-radius: 6px;"></textarea>
            </div>
            <div>
              <label style="display: block; font-weight: 600; font-size: 0.9rem; margin-bottom: 0.35rem; color: var(--text, #1e293b);">
                参考答案（一份） <small class="muted" style="font-weight: normal;">（选填，AI 将以给定资料为最高真理源独立做题并进行纠错）</small>
              </label>
              <textarea class="q-ref-answer" rows="4" placeholder="在此输入此题的一份参考答案（选填。AI 将依据 Shenlun.skill 体系自主做题，参考答案不准确时绝不作为扣分依据）" style="width: 100%; box-sizing: border-box; font-family: inherit; font-size: 0.92rem; padding: 0.75rem; border: 1px solid var(--line, #cbd5e1); border-radius: 6px;"></textarea>
            </div>
          `;
          if (initialPrompt) {
            div.querySelector('.q-prompt').value = initialPrompt;
          }
          if (initialRef) {
            div.querySelector('.q-ref-answer').value = initialRef;
          }
          div.dataset.referenceMeta = JSON.stringify(initialReferenceMeta || {});
          container.appendChild(div);
          renumberQuestions();
        }

        function removeQuestionField(btn) {
          const card = btn.closest('.question-card-item');
          if (card) {
            card.remove();
            renumberQuestions();
          }
        }

        function renumberQuestions() {
          const cards = document.querySelectorAll('#questions-container .question-card-item');
          cards.forEach((card, idx) => {
            const badge = card.querySelector('.q-badge');
            if (badge) badge.innerText = `第 ${idx + 1} 题`;
          });
        }

        // 初始化默认展示 1 个资料框和 1 个题目框
        document.addEventListener('DOMContentLoaded', () => {
          if (document.querySelectorAll('#materials-container .material-card').length === 0) {
            addMaterialField();
          }
          if (document.querySelectorAll('#questions-container .question-card-item').length === 0) {
            addQuestionField();
          }
        });

        async function submitCustomPaper() {
          const nameInput = document.getElementById('field-paper-name');
          const paperName = (nameInput.value || '').trim();
          const statusEl = document.getElementById('submit-status');
          const btn = document.getElementById('btn-save-paper');

          if (!paperName) {
            nameInput.focus();
            statusEl.innerHTML = '<span style="color: #ef4444; font-weight: 600;">请填写试卷名称或一句话描述</span>';
            return;
          }

          // 收集材料
          const matEls = document.querySelectorAll('#materials-container .mat-content');
          const materials = [];
          matEls.forEach((el, idx) => {
            const val = el.value.trim();
            if (val) {
              materials.push({ material_number: idx + 1, content: val, title: `给定资料${idx + 1}` });
            }
          });

          // 收集题目
          const qCards = document.querySelectorAll('#questions-container .question-card-item');
          const questions = [];
          qCards.forEach((card, idx) => {
            const prompt = (card.querySelector('.q-prompt')?.value || '').trim();
            const ref = (card.querySelector('.q-ref-answer')?.value || '').trim();
            if (prompt) {
              let referenceAnswer = ref;
              try {
                const meta = JSON.parse(card.dataset.referenceMeta || '{}');
                if (ref && Object.keys(meta).length) {
                  referenceAnswer = { ...meta, answer_text: ref };
                }
              } catch (error) {
                referenceAnswer = ref;
              }
              questions.push({
                question_number: idx + 1,
                prompt: prompt,
                reference_answer: referenceAnswer
              });
            }
          });

          if (questions.length === 0) {
            statusEl.innerHTML = '<span style="color: #ef4444; font-weight: 600;">请至少录入一道题目的内容</span>';
            return;
          }

          statusEl.innerHTML = '<span style="color: var(--blue, #2563eb);">正在保存试卷与题目...</span>';
          btn.disabled = true;

          try {
            const res = await fetch('/papers/new', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                paper_name: paperName,
                materials: materials,
                questions: questions,
                source_url: (window.urlImportSource && window.urlImportSource.url) || '',
                source_kind: (window.urlImportSource && window.urlImportSource.kind) || '',
                source_note: (window.urlImportDraft && window.urlImportDraft.import_note) || ''
              })
            });
            const data = await res.json();
            if (data.ok && data.redirect) {
              statusEl.innerHTML = '<span style="color: #16a34a; font-weight: 600;">✓ 保存成功，正在跳转...</span>';
              window.location.href = data.redirect;
            } else {
              statusEl.innerHTML = '<span style="color: #ef4444; font-weight: 600;">保存失败: ' + (data.error || '未知错误') + '</span>';
              btn.disabled = false;
            }
          } catch (err) {
            statusEl.innerHTML = '<span style="color: #ef4444; font-weight: 600;">网络异常: ' + err.message + '</span>';
            btn.disabled = false;
          }
        }
        </script>
        """
        import_panel = """
        <section id="url-import-panel" class="card url-import-panel">
          <div class="url-import-head">
            <div class="url-import-kicker"><span class="url-import-kicker-dot"></span><span>Fenbi · URL Import</span></div>
            <h2 class="url-import-title">从 URL 自动录入</h2>
            <p class="url-import-description">粘贴粉笔套卷解析页或练习页 URL。练习页未提交时，应用会自动填写占位答案并提交，以获取每道题的粉笔踩分树；已提交页面会直接读取踩分树。参考答案与踩分树仅供批改对照。</p>
          </div>
          <div class="url-import-url-row">
            <label class="url-import-field-label" for="url-import-input">来源页面 URL</label>
            <div class="url-import-url-control">
              <input id="url-import-input" class="url-import-url-input" type="url" placeholder="https://spa.fenbi.com/ti/exam/solution/..." value="__URL_IMPORT_SOURCE_HTML__">
              <button type="button" class="button primary" id="url-import-button">直接导入并预览</button>
            </div>
          </div>
          <details id="url-import-credentials" class="url-import-credentials">
            <summary class="url-import-credentials-summary">
              <span class="url-import-summary-main"><strong>粉笔登录态</strong><small>首次使用或登录失效时填写</small></span>
              <span class="url-import-summary-hint">可选</span>
            </summary>
            <div class="url-import-credentials-body">
              <div class="url-import-field url-import-cookie-field">
                <div class="url-import-label-row">
                  <label class="url-import-field-label" for="url-import-cookie">Cookie-Editor 导出的粉笔 Cookie</label>
                  <span class="url-import-field-hint">支持 JSON / Cookie Header</span>
                </div>
                <textarea id="url-import-cookie" class="url-import-cookie" rows="4" placeholder="首次使用：把 Cookie-Editor 导出的 JSON 数组完整粘贴到这里；也支持 Cookie: sess=...; userid=... 格式。只会保留粉笔域名 Cookie。"></textarea>
              </div>
              <div class="url-import-field">
                <label class="url-import-field-label" for="url-import-device-id">DeviceSid <span class="url-import-optional">可选</span></label>
                <input id="url-import-device-id" class="url-import-device-id" type="text" placeholder="如果提示 DeviceSid 无效，可从粉笔浏览器登录态中填写">
              </div>
              <div class="url-import-actions">
                <label class="url-import-remember"><input id="url-import-remember" class="url-import-remember-checkbox" type="checkbox" checked><span class="url-import-remember-text">保存到本机，之后只输入 URL</span></label>
                <button type="button" class="button ghost small" id="url-import-clear-credentials">清除本机登录态</button>
              </div>
              <p class="url-import-credential-state"><span class="url-import-state-label">本机状态</span><span id="url-import-credential-state">__URL_IMPORT_CREDENTIAL_STATE__</span></p>
              <p class="url-import-help">凭据保存在本机应用数据目录的受限文件中，只发送到粉笔域名，不会写入题库或日志。</p>
            </div>
          </details>
          <div id="url-import-status"></div>
          <div id="url-import-preview" hidden></div>
          <script type="application/json" id="url-import-seed">__URL_IMPORT_SEED__</script>
        </section>
        """
        credential_state = (
            f"已保存（{credentials_summary['cookie_count']} 个 Cookie"
            + ("，含 DeviceSid" if credentials_summary["device_id_present"] else "")
            + ")"
            if credentials_summary["configured"]
            else "尚未保存"
        )
        import_panel = (
            import_panel
            .replace("__URL_IMPORT_SOURCE_HTML__", esc(import_source_url))
            .replace("__URL_IMPORT_CREDENTIAL_STATE__", esc(credential_state))
            .replace("__URL_IMPORT_SEED__", import_seed)
        )
        body = body.replace("<!-- URL_IMPORT_PANEL -->", import_panel)
        self.send_html(layout("录入新套卷 - 研申", body, "papers"))

    def handle_paper_parse_text(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        raw_text = ""
        if self.headers.get("Content-Type", "").startswith("application/json"):
            try:
                payload = json.loads(body)
                raw_text = payload.get("raw_text", "")
            except Exception:
                pass
        else:
            form = parse_qs(body)
            raw_text = form.get("raw_text", [""])[0]

        if not raw_text.strip():
            self.send_json({"ok": False, "error": "文本内容为空"}, status=400)
            return

        try:
            parsed = parse_raw_paper_text(raw_text)
            self.send_json({"ok": True, "parsed": parsed})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def _local_absolute_url(self, path):
        host, port = self.server.server_address[:2]
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        return f"http://{host}:{port}{local_url(path)}"

    def handle_paper_url_import(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 768 * 1024:
            self.send_json({"ok": False, "error": "URL 导入请求过大"}, status=413)
            self.close_connection = True
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "请求数据不是有效 JSON"}, status=400)
            return

        if data.get("clear_credentials"):
            FENBI_CREDENTIALS.clear()
            self.send_json({
                "ok": True,
                "credentials": fenbi_credentials_summary(None),
                "message": "已清除本机保存的粉笔登录态",
            })
            return

        source_url = str(data.get("source_url") or "").strip()
        cookie_text = data.get("cookie_text", data.get("cookie", ""))
        cookie_text = str(cookie_text or "")
        device_id = str(data.get("device_id") or "").strip()
        has_explicit_credentials = bool(cookie_text.strip() or device_id)
        remember_session = data.get("remember_session", True) is not False
        try:
            source = parse_source_url(source_url)
            if has_explicit_credentials:
                credentials = parse_fenbi_credentials(cookie_text, device_id)
            else:
                credentials = FENBI_CREDENTIALS.load()
            result = fetch_source_draft(
                source.source_url,
                credentials=credentials,
                auto_submit=data.get("auto_submit", True) is not False,
            )
        except UrlImportError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        except Exception as exc:
            self.send_json({"ok": False, "error": f"URL 导入失败：{exc}"}, status=502)
            return

        stored_credentials = False
        if has_explicit_credentials and remember_session and result.get("ok"):
            try:
                FENBI_CREDENTIALS.save(credentials)
                stored_credentials = True
            except (OSError, UrlImportError) as exc:
                self.send_json({"ok": False, "error": f"导入成功，但保存本机粉笔登录态失败：{exc}"}, status=500)
                return

        session_id = IMPORT_SESSIONS.create(source, result.get("draft"))
        response = public_import_summary(result)
        response["session_id"] = session_id
        response["credentials"] = fenbi_credentials_summary(credentials)
        response["stored_credentials"] = stored_credentials
        if result.get("requires_browser_bridge") and not result.get("requires_credentials"):
            callback_url = self._local_absolute_url("/papers/import-url/bridge/payload")
            response["bookmarklet"] = build_fenbi_bookmarklet(session_id, callback_url)
            response["preview_url"] = self._local_absolute_url(f"/papers/new?import_session={session_id}")
        self.send_json(response)

    def handle_paper_url_bridge_payload(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        origin = self._allowed_bridge_origin()
        if length > MAX_BRIDGE_BYTES:
            self.send_json({"ok": False, "error": "浏览器桥接数据过大"}, status=413, cors_origin=origin)
            self.close_connection = True
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "浏览器桥接数据不是有效 JSON"}, status=400, cors_origin=origin)
            return
        session_id = str(data.get("session_id") or "").strip()
        session = IMPORT_SESSIONS.get(session_id)
        if not session:
            self.send_json({"ok": False, "error": "导入会话不存在或已过期，请重新粘贴 URL"}, status=404, cors_origin=origin)
            return
        requested_url = str(data.get("source_url") or session.get("source", {}).get("source_url") or "").strip()
        try:
            requested_host = (urlparse(requested_url).hostname or "").lower().rstrip(".")
            if requested_host in {"127.0.0.1", "localhost", "::1"}:
                raise UrlImportError("请在已登录的粉笔套卷解析页点击书签脚本；不要直接在研申本地页面点击")
            source = parse_source_url(requested_url)
            saved_source = session.get("source") or {}
            if (
                source.key != saved_source.get("key")
                or source.source_kind != saved_source.get("source_kind")
                or source.routecs != saved_source.get("routecs")
            ):
                raise UrlImportError("浏览器当前页面与最初提交的套卷 URL 不一致")
            draft = draft_from_bridge_payload(data.get("payload") or {}, source.source_url)
            if not IMPORT_SESSIONS.set_draft(session_id, draft):
                raise UrlImportError("导入会话已过期，请重新开始")
        except UrlImportError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400, cors_origin=origin)
            return
        except Exception as exc:
            self.send_json({"ok": False, "error": f"浏览器桥接解析失败：{exc}"}, status=422, cors_origin=origin)
            return

        response = public_import_summary({"ok": True, "draft": draft})
        response["redirect"] = self._local_absolute_url(f"/papers/new?import_session={session_id}")
        self.send_json(response, cors_origin=origin)

    def handle_paper_create(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        data = {}
        if self.headers.get("Content-Type", "").startswith("application/json"):
            try:
                data = json.loads(body)
            except Exception:
                pass
        else:
            form = parse_qs(body)
            data = {k: v[0] for k, v in form.items()}

        paper_name = data.get("paper_name", "").strip()
        if not paper_name:
            paper_name = "个人申论练习卷"

        materials = []
        raw_mats = data.get("materials")
        if isinstance(raw_mats, list):
            materials = raw_mats
        elif isinstance(raw_mats, str):
            try:
                materials = json.loads(raw_mats)
            except Exception:
                materials = [raw_mats]
        elif data.get("materials_json"):
            try:
                materials = json.loads(data["materials_json"])
            except Exception:
                materials = []
        elif data.get("materials_text"):
            parsed = parse_raw_paper_text(data["materials_text"])
            materials = parsed.get("materials", [])

        questions = []
        raw_qs = data.get("questions")
        if isinstance(raw_qs, list):
            questions = raw_qs
        elif isinstance(raw_qs, str):
            try:
                questions = json.loads(raw_qs)
            except Exception:
                questions = []
        elif data.get("questions_json"):
            try:
                questions = json.loads(data["questions_json"])
            except Exception:
                questions = []

        source_url = str(data.get("source_url") or "").strip()
        source_kind = str(data.get("source_kind") or "").strip()
        source_note = str(data.get("source_note") or "").strip()

        with connect(self.db_path) as conn:
            paper_id = create_custom_paper(
                conn=conn,
                paper_name=paper_name,
                year=data.get("year"),
                region=data.get("region"),
                exam_type=data.get("exam_type"),
                paper_category=data.get("paper_category"),
                materials=materials,
                source_url=source_url,
                source_kind=source_kind,
                source_note=source_note,
            )
            for idx, q_data in enumerate(questions, start=1):
                if isinstance(q_data, str):
                    q_data = {"prompt": q_data}
                prompt = (q_data.get("prompt") or "").strip()
                if not prompt:
                    continue
                ref_ans = q_data.get("reference_answer")
                ref_text = ""
                ref_org = (q_data.get("reference_org") or "参考答案").strip()
                ref_scoring_points = (q_data.get("scoring_points") or "").strip()
                ref_notes = (q_data.get("reference_notes") or "").strip()
                ref_is_reviewed = 1
                if isinstance(ref_ans, dict):
                    ref_text = (ref_ans.get("answer_text") or ref_ans.get("answerText") or "").strip()
                    ref_org = (ref_ans.get("organization") or ref_ans.get("orgName") or ref_org).strip()
                    ref_scoring_points = (ref_ans.get("scoring_points") or ref_ans.get("scoringPoints") or ref_scoring_points).strip()
                    ref_notes = (ref_ans.get("notes") or ref_notes).strip()
                    ref_is_reviewed = 1 if ref_ans.get("is_reviewed", 1) else 0
                elif isinstance(ref_ans, str):
                    ref_text = ref_ans.strip()
                q_id = add_paper_question(
                    conn=conn,
                    paper_id=paper_id,
                    title=q_data.get("title", ""),
                    question_type=q_data.get("question_type", ""),
                    prompt=prompt,
                    original_text=q_data.get("original_text") or prompt,
                    materials=q_data.get("materials") or q_data.get("materials_scope") or "",
                    requirements=q_data.get("requirements", ""),
                    word_limit=q_data.get("word_limit", ""),
                    score=q_data.get("score"),
                    question_number=q_data.get("question_number") or idx,
                    source_url=q_data.get("source_url") or source_url,
                    source_kind=q_data.get("source_kind") or source_kind,
                    source_provider=q_data.get("source_provider") or data.get("source_provider") or "",
                    source_note=q_data.get("source_note") or source_note,
                    reference_answer=ref_ans,
                    reference_org=ref_org,
                    scoring_points=ref_scoring_points,
                    reference_notes=ref_notes,
                    reference_is_reviewed=ref_is_reviewed,
                )

        if self.headers.get("Content-Type", "").startswith("application/json") or self.headers.get("Accept", "").startswith("application/json"):
            self.send_json({"ok": True, "paper_id": paper_id, "redirect": f"/papers/{paper_id}"})
            return
        self.redirect(f"/papers/{paper_id}")

    def page_question_new(self, path, query=None):
        try:
            paper_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return

        with connect(self.db_path) as conn:
            paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if not paper:
                self.send_error(404)
                return
            existing_questions = conn.execute(
                "SELECT * FROM questions WHERE paper_id = ? ORDER BY question_number, id",
                (paper_id,),
            ).fetchall()
            materials = conn.execute(
                "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
                (paper_id,),
            ).fetchall()

        next_q_num = len(existing_questions) + 1
        mat_options = "".join(
            f'<span style="margin-right: 0.75rem;"><label><input type="checkbox" name="material_numbers" value="{m["material_number"]}"> 材料{m["material_number"]}</label></span>'
            for m in materials
        ) or '<span class="muted">该试卷暂无材料</span>'

        body = f"""
        <section class="page-head">
          <div>
            <p class="eyebrow"><a href="/papers/{paper_id}">{esc(paper["paper_name"])}</a></p>
            <h1>添加题目</h1>
            <p class="page-lede">为此套卷录入新题目及作答要求，支持同步录入参考答案。</p>
          </div>
          <div class="actions">
            <a class="button ghost" href="/papers/{paper_id}">返回试卷</a>
          </div>
        </section>

        <form class="settings-panel" method="post" action="/papers/{paper_id}/questions/new" style="border: 1px solid var(--line, #e2e8f0); border-radius: 8px; padding: 1.5rem; background: var(--bg-card, #ffffff);">
          <div class="settings-fields" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1rem; margin-bottom: 1rem;">
            <label>
              <span>题型 *</span>
              <select name="question_type" required>
                <option value="归纳概括">归纳概括</option>
                <option value="提出对策">提出对策</option>
                <option value="综合分析">综合分析</option>
                <option value="应用文写作">应用文写作</option>
                <option value="文章写作">文章写作（大作文）</option>
                <option value="其他题型">其他题型</option>
              </select>
            </label>
            <label><span>题号</span><input name="question_number" type="number" value="{next_q_num}"></label>
            <label><span>分值(分)</span><input name="score" type="number" value="20" placeholder="如：20"></label>
            <label><span>字数限制</span><input name="word_limit" placeholder="如：不超过300字、1000-1200字"></label>
          </div>

          <div style="margin-bottom: 1rem;">
            <label><span>题干 (Prompt) *</span>
              <textarea name="prompt" rows="3" required placeholder="如：根据“给定资料1”，概括某某做法的主要成效。" style="width: 100%; font-family: inherit; margin-top: 0.25rem;"></textarea>
            </label>
          </div>

          <div style="margin-bottom: 1rem;">
            <label><span>作答要求 (Requirements)</span>
              <textarea name="requirements" rows="2" placeholder="如：全面、准确、有条理，不超过250字。" style="width: 100%; font-family: inherit; margin-top: 0.25rem;"></textarea>
            </label>
          </div>

          <div style="margin-bottom: 1.25rem;">
            <label style="display: block; margin-bottom: 0.25rem;"><span>关联材料范围</span></label>
            <div style="padding: 0.5rem; background: var(--bg-alt, #f8fafc); border-radius: 6px;">
              {mat_options}
            </div>
            <input type="text" name="materials_scope" placeholder="或直接输入范围文本，如：给定资料1、给定资料2" style="margin-top: 0.5rem; width: 100%;">
          </div>

          <fieldset style="border: 1px dashed var(--line, #cbd5e1); border-radius: 6px; padding: 1rem; margin-bottom: 1.5rem;">
            <legend style="padding: 0 0.5rem; font-size: 0.9rem; font-weight: 600;">可选：同步录入参考答案</legend>
            <p class="muted" style="font-size: 0.82rem; margin-bottom: 0.75rem;">如已有机构（华图、粉笔、中公等）参考答案可在此录入。AI 批改将结合材料和名师体系对参考答案进行客观纠错与审计。</p>
            <div style="display: flex; gap: 0.75rem; margin-bottom: 0.5rem; flex-wrap: wrap;">
              <input type="text" name="ref_organization" placeholder="机构名称（如：粉笔、华图、自拟）" style="flex: 1; min-width: 180px;">
              <input type="number" name="ref_score" placeholder="预估满分" style="width: 120px;">
            </div>
            <textarea name="ref_answer_text" rows="5" placeholder="粘贴参考答案正文..." style="width: 100%; font-family: inherit; font-size: 0.88rem;"></textarea>
          </fieldset>

          <div style="display: flex; gap: 1rem; align-items: center;">
            <button class="button primary" type="submit">保存题目</button>
            <a class="button ghost" href="/papers/{paper_id}">取消</a>
          </div>
        </form>
        """
        self.send_html(layout("添加题目 - 研申", body, "papers"))

    def handle_question_create(self, path):
        try:
            paper_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(body)

        question_type = form.get("question_type", ["归纳概括"])[0]
        prompt = form.get("prompt", [""])[0].strip()
        requirements = form.get("requirements", [""])[0].strip()
        word_limit = form.get("word_limit", [""])[0].strip()
        try:
            score = int(form.get("score", ["20"])[0] or "20")
        except ValueError:
            score = 20
        try:
            question_number = int(form.get("question_number", ["1"])[0] or "1")
        except ValueError:
            question_number = 1

        mat_numbers = form.get("material_numbers", [])
        materials_scope = form.get("materials_scope", [""])[0].strip()
        if not materials_scope and mat_numbers:
            materials_scope = "给定资料" + "、给定资料".join(sorted(mat_numbers, key=int))

        if not prompt:
            self.send_error(400, "题干不能为空")
            return

        with connect(self.db_path) as conn:
            question_id = add_paper_question(
                conn=conn,
                paper_id=paper_id,
                question_type=question_type,
                prompt=prompt,
                requirements=requirements,
                word_limit=word_limit,
                score=score,
                question_number=question_number,
                materials_scope=materials_scope,
            )
            ref_org = form.get("ref_organization", [""])[0].strip()
            ref_text = form.get("ref_answer_text", [""])[0].strip()
            if ref_text:
                try:
                    ref_score = int(form.get("ref_score", [""])[0] or str(score))
                except ValueError:
                    ref_score = score
                add_question_reference_answer(
                    conn=conn,
                    question_id=question_id,
                    organization=ref_org or "参考答案",
                    answer_text=ref_text,
                    score=ref_score,
                )

        self.redirect(f"/questions/{question_id}")

    def handle_reference_create(self, path):
        try:
            question_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(body)

        organization = form.get("organization", [""])[0].strip()
        answer_text = form.get("answer_text", [""])[0].strip()
        score_val = form.get("score", [""])[0].strip()
        score = None
        if score_val:
            try:
                score = int(score_val)
            except ValueError:
                pass

        if not organization:
            organization = "参考答案"
        if not answer_text:
            self.redirect(f"/questions/{question_id}")
            return

        with connect(self.db_path) as conn:
            add_question_reference_answer(
                conn=conn,
                question_id=question_id,
                organization=organization,
                answer_text=answer_text,
                score=score,
            )

        self.redirect(f"/questions/{question_id}")

    def handle_question_ai_solve(self, path):
        try:
            question_id = int(path.strip("/").split("/")[1])
        except (ValueError, IndexError):
            self.send_error(404)
            return

        with connect(self.db_path) as conn:
            question = conn.execute("SELECT id FROM questions WHERE id = ?", (question_id,)).fetchone()
            if not question:
                self.send_error(404)
                return
            ai_settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
            if not ai_settings or not ai_settings["api_key"]:
                self.redirect(f"/questions/{question_id}?solve_error=no_api_key")
                return
            try:
                solve_question_with_ai(conn, question_id)
            except Exception as e:
                import urllib.parse
                err_msg = urllib.parse.quote(str(e))
                self.redirect(f"/questions/{question_id}?solve_error={err_msg}")
                return

        self.redirect(f"/questions/{question_id}")

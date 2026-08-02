"""Shared rendering helpers used by the web controllers."""

import cgi
import html
import json
import logging
import math
import mimetypes
import os
import re
import threading
from datetime import datetime, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from ..agent_chat import (
    _cleanup_orphaned_pending_messages,
    conversation_payload,
    start_or_continue_chat,
    start_or_continue_chat_async,
)
from ..agent_coach import first_screen_judgement
from ..agent_eval import latest_eval_results, run_eval_suite
from ..agent_graph import AgentRunError, run_agent
from ..agent_modules import MODULES, load_knowledge_items, module_definition, valid_module_id
from ..agent_store import (
    active_memories,
    add_feedback,
    clear_memories,
    delete_conversation,
    delete_memory,
    get_feedback,
    get_messages,
    get_run,
    get_run_steps,
    recent_conversations,
    recent_runs,
)
from ..ai import AiConfigError, AiRequestError, chat_completion, masked_key, resolve_api_key
from ..db import connect, init_db, prepare_user_database
from ..grading import (
    build_ai_prompt,
    build_grading_package,
    build_revised_answer_retry_prompt,
    count_cjk_chars,
    normalize_revised_answer_word_count,
    parse_revised_answer_repair,
    referenced_material_numbers,
    replace_revised_answer_body,
    revised_answer_word_count_status,
    select_relevant_materials,
    should_use_whole_paper_materials,
)
from ..grading_pipeline.orchestration import (
    ACTIVE_JOB_STATUSES,
    apply_report_feedback,
    create_grading_job,
    grading_job_payload,
    invalidate_question_rubrics,
    manual_grading_basis,
    rubric_cache_status,
    start_grading_job,
)
from ..importer import create_import_record, finish_import_record, import_answers, import_questions
from ..paths import resource_root, seed_db_path, user_data_dir, user_db_path
from ..services.personal_records import (
    PERSONAL_BACKUP_VERSION,
    _int_or_none,
    annotation_data_attributes,
    export_personal_data,
    import_personal_data,
    normalize_text_annotations,
    save_text_annotations,
    text_annotation_key,
)
from ..statistics import build_module_score_statistics, build_training_statistics, parse_report_score
from ..timeutils import BEIJING_TIMEZONE, format_beijing_time
from .context import ApplicationContext
from .routing import dispatch_get, dispatch_post
from .templating import render_layout

ROOT = resource_root()
DB_PATH = user_db_path()
DEFAULT_APP_CONTEXT = ApplicationContext.create(DB_PATH, ROOT)
AUTOSAVE_LOCK = DEFAULT_APP_CONTEXT.autosave.lock
AUTOSAVE_REVISIONS = DEFAULT_APP_CONTEXT.autosave.revisions
APP_BUILD = "1.3.9"
ASSET_VERSION = f"gk-{APP_BUILD.rsplit('.', 1)[-1]}"

FILTER_RESTORE_BOOTSTRAP = ""

STARTUP_RESTORE_BOOTSTRAP = """<script>
(function () {
  try {
    var namespace = "gongkao.viewState.v2:";
    var checkedKey = namespace + "startup:checked";
    if (window.sessionStorage.getItem(checkedKey) === "1") return;
    window.sessionStorage.setItem(checkedKey, "1");
    if (window.location.pathname !== "/home") return;
    var raw = window.localStorage.getItem(namespace + "startup-restore-preferences");
    var preferences = raw ? JSON.parse(raw) : {};
    if (preferences.restoreLastPage === false) return;
    var saved = window.localStorage.getItem(namespace + "startup:last-route");
    if (!saved || saved === "/home") return;
    var target = new URL(saved, window.location.origin);
    if (target.origin !== window.location.origin
        || target.pathname.startsWith("/static/")
        || target.pathname === "/settings/export") return;
    document.documentElement.style.visibility = "hidden";
    window.location.replace(target.pathname + target.search + target.hash);
  } catch (error) {
    // Startup recovery is optional when browser storage is unavailable.
  }
})();
</script>"""


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def pre(value):
    return esc(value).replace("\n", "<br>")


def layout(title, body, active="index", flashes=None, *, transient_route=False, sidebar_extra=""):
    return render_layout(
        ROOT,
        title=title,
        body=body,
        active=active,
        flashes=flashes or [],
        transient_route=transient_route,
        sidebar_extra=sidebar_extra,
        app_build=APP_BUILD,
        asset_version=ASSET_VERSION,
        startup_bootstrap=STARTUP_RESTORE_BOOTSTRAP,
        filter_bootstrap=FILTER_RESTORE_BOOTSTRAP,
    )


def option_list(values, selected):
    output = ['<option value="">全部</option>']
    for value in values:
        sel = " selected" if value == selected else ""
        output.append(f'<option value="{esc(value)}"{sel}>{esc(value)}</option>')
    return "".join(output)


def form_value(data, key, default=""):
    return parse_qs(data).get(key, [default])[0].strip()


def nonnegative_int(value, default=0):
    try:
        parsed = int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def format_duration(seconds, empty="未计时"):
    seconds = nonnegative_int(seconds)
    if seconds <= 0:
        return empty
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def paper_attempt_duration_seconds(conn, paper_id, exclude_question_id=None):
    if not paper_id:
        return 0
    extra_clause = ""
    params = [paper_id]
    if exclude_question_id:
        extra_clause = "AND q.id <> ?"
        params.append(exclude_question_id)
    value = conn.execute(
        f"""
        SELECT COALESCE(SUM(question_seconds), 0)
          FROM (
                SELECT q.id,
                       COALESCE(MAX(
                           CASE
                               WHEN COALESCE(a.paper_time_excluded, 0) = 0
                               THEN a.duration_seconds
                               ELSE 0
                           END
                       ), 0) AS question_seconds
                  FROM questions q
             LEFT JOIN attempts a ON a.question_id = q.id
                 WHERE q.paper_id = ?
                   {extra_clause}
              GROUP BY q.id
          )
        """,
        params,
    ).fetchone()[0]
    return nonnegative_int(value)


def question_paper_duration_seconds(conn, question_id):
    value = conn.execute(
        """
        SELECT COALESCE(MAX(duration_seconds), 0)
          FROM attempts
         WHERE question_id = ?
           AND COALESCE(paper_time_excluded, 0) = 0
        """,
        (question_id,),
    ).fetchone()[0]
    return nonnegative_int(value)


def safe_return_path(value, fallback):
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return fallback
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return fallback
    return value


def safe_static_path(root, relative_path):
    root = Path(root).resolve()
    candidate = (root / str(relative_path).lstrip("/\\")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def return_path_from_query(query, fallback):
    return safe_return_path((query or {}).get("return_to", [""])[0], fallback)


def return_path_from_form(data, fallback):
    return safe_return_path(form_value(data, "return_to"), fallback)


def local_url(path, *, return_to="", fragment="", **params):
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise ValueError("local URL required")
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None or value == "":
            query.pop(key, None)
        else:
            query[key] = [str(value)]
    if return_to:
        query["return_to"] = [safe_return_path(return_to, "/")]
    encoded = urlencode(query, doseq=True)
    anchor = fragment or parsed.fragment
    return f"{parsed.path}{'?' + encoded if encoded else ''}{'#' + anchor if anchor else ''}"


def back_link(href, label, css_class="button ghost"):
    return f'<a class="{esc(css_class)}" href="{esc(href)}" data-nav-back>{esc(label)}</a>'


def settings_credits():
    return f"""
    <aside class="settings-contributor">
      <p><span>贡献者：</span><strong>Nullapse</strong></p>
      <p>GitHub：<a href="https://github.com/Nullapse/YanShen" target="_blank" rel="noopener noreferrer">https://github.com/Nullapse/YanShen</a></p>
      <p>声明：本应用完全免费，仅供个人学习与研究使用。</p>
      <p><span>版本：</span><strong data-app-build>{APP_BUILD}</strong></p>
    </aside>
    """


def evidence_return_path(query):
    """Return a transient evidence origin without changing workflow hierarchy."""
    candidate = return_path_from_query(query, "")
    if candidate.startswith("/agent") or is_report_citation_return(candidate):
        return candidate
    return ""


def workflow_header(stage, paper=None, question=None, attempt=None, *, more_html="", context_return=""):
    paper = paper or question or {}
    paper_keys = set(paper.keys()) if hasattr(paper, "keys") else set()
    paper_id = paper["paper_id"] if "paper_id" in paper_keys else paper["id"] if "id" in paper_keys else None
    question_id = question["id"] if question is not None else None
    attempt_id = attempt["id"] if attempt is not None else None
    paper_href = local_url(f"/papers/{paper_id}", q=question_id) if paper_id else "/papers"
    question_href = f"/questions/{question_id}" if question_id else ""
    attempt_href = f"/attempts/{attempt_id}" if attempt_id else ""

    if stage == "paper":
        back_href, back_label, title = "/papers", "返回题库", paper["paper_name"]
    elif stage == "answer":
        back_href = paper_href if paper_id else "/"
        back_label = "返回试卷" if paper_id else "返回全部题目"
        title = question["title"]
    else:
        back_href, back_label, title = question_href, "返回题目", "本次批改"

    crumb_items = ['<a href="/papers">题库</a>']
    if paper_id:
        paper_name = paper["paper_name"] or "未命名试卷"
        crumb_items.append(f'<a href="{esc(paper_href)}">{esc(paper_name)}</a>')
    if question is not None and stage != "paper":
        number = question["question_number"] or "?"
        crumb_items.append(f'<a href="{esc(question_href)}">第{esc(number)}题</a>')
    if stage == "grading":
        crumb_items.append("<span>批改</span>")

    stages = []
    stage_items = (
        ("paper", "1", "看试卷", paper_href),
        ("answer", "2", "写答案", question_href),
        ("grading", "3", "看批改", attempt_href),
    )
    for key, number, label, href in stage_items:
        classes = ["workflow-stage"]
        if key == stage:
            classes.append("is-current")
        elif (stage == "answer" and key == "paper") or (stage == "grading" and key in {"paper", "answer"}):
            classes.append("is-complete")
        content = f"<b>{number}</b><span>{label}</span>"
        stages.append(
            f'<a class="{" ".join(classes)}" href="{esc(href)}">{content}</a>'
            if href
            else f'<span class="{" ".join(classes)} is-disabled">{content}</span>'
        )

    meta_parts = []
    if paper:
        for key in ("year", "region", "exam_type"):
            value = paper[key] if key in paper_keys else ""
            if value:
                meta_parts.append(esc(value))
    context_html = (
        f'<a class="workflow-context-return" href="{esc(context_return)}">关闭引用并返回原位置</a>'
        if context_return
        else ""
    )
    more_menu = (
        f'<div class="workflow-more" data-workflow-menu>'
        f'<button type="button" class="workflow-more-toggle" data-workflow-menu-toggle '
        f'aria-haspopup="menu" aria-expanded="false">更多</button>'
        f'<div class="workflow-more-menu" data-workflow-menu-popover role="menu" hidden>{more_html}</div>'
        f'</div>'
        if more_html
        else ""
    )
    return f"""
    <header class="workflow-header">
      <div class="workflow-topline">
        <nav class="workflow-breadcrumb" aria-label="当前位置">{"<i>/</i>".join(crumb_items)}</nav>
        {context_html}
      </div>
      <div class="workflow-title-row">
        <div><p>{" · ".join(meta_parts)}</p><h1>{esc(title)}</h1></div>
        <div class="workflow-head-actions">{back_link(back_href, back_label, "workflow-back")}{more_menu}</div>
      </div>
      <div class="workflow-stagebar"><nav class="workflow-stages" aria-label="做题进度">{"".join(stages)}</nav></div>
    </header>"""


def next_question_path(conn, question):
    if not question["paper_id"]:
        return "/", "返回全部题目"
    rows = conn.execute(
        """
        SELECT id FROM questions
         WHERE paper_id = ?
         ORDER BY CASE WHEN question_number = 0 THEN 999 ELSE question_number END, id
        """,
        (question["paper_id"],),
    ).fetchall()
    ids = [row["id"] for row in rows]
    try:
        index = ids.index(question["id"])
    except ValueError:
        index = len(ids)
    if index + 1 < len(ids):
        return f"/questions/{ids[index + 1]}?practice=paper&timer=auto", "进入下一题"
    return f"/papers/{question['paper_id']}?q={question['id']}", "返回试卷总览"


def grading_report_return_path(attempt_id, report_id, parent_return_to="/attempts"):
    """Return to one exact report while retaining that report page's parent."""
    return local_url(
        f"/attempts/{int(attempt_id)}",
        return_to=safe_return_path(parent_return_to, "/attempts"),
        fragment=f"report-{int(report_id)}",
    )


def is_report_citation_return(value):
    parsed = urlparse(safe_return_path(value, ""))
    return parsed.path.startswith("/attempts/") and parsed.fragment.startswith("report-")


def autosave_identity(form):
    session_id = (form.get("autosave_session", [""])[0] or "").strip()[:100]
    try:
        revision = max(0, int(form.get("autosave_revision", ["0"])[0] or 0))
    except (TypeError, ValueError):
        revision = 0
    return session_id, revision


def apply_versioned_autosave(
    resource_key,
    session_id,
    revision,
    save_callback,
    autosave_state=None,
):
    state = autosave_state or DEFAULT_APP_CONTEXT.autosave
    return state.apply(resource_key, session_id, revision, save_callback)


def agent_summary_for_display(value):
    text = value or ""
    text = re.sub(r"；?AI 百分制均分：[^；。]*", "", text)
    text = re.sub(r"；{2,}", "；", text).strip("； ")
    return text


AGENT_THREAD_GROUPS = (
    ("review", "本题复盘"),
    ("overview", "总览"),
    ("summary", "归纳概括"),
    ("analysis", "综合分析"),
    ("countermeasure", "提出对策"),
    ("document", "公文写作"),
    ("essay", "综合写作"),
)


def agent_thread_group(conversation):
    if (conversation["entrypoint"] or "") == "recent_review":
        return "review"
    try:
        metadata = json.loads(conversation["latest_metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    module = valid_module_id(str(metadata.get("module") or "").strip())
    return module if module in {item[0] for item in AGENT_THREAD_GROUPS} else "overview"


def render_agent_thread_list(conversations, active_conversation_id=None, return_to="/agent"):
    grouped = {group_id: [] for group_id, _ in AGENT_THREAD_GROUPS}
    for conversation in conversations:
        grouped[agent_thread_group(conversation)].append(conversation)
    sections = []
    for group_id, label in AGENT_THREAD_GROUPS:
        items = grouped[group_id]
        if not items:
            continue
        rows = []
        for conversation in items:
            active = " active" if conversation["id"] == active_conversation_id else ""
            updated_at = format_beijing_time(conversation["updated_at"])
            title_text = conversation["title"] or "AI 教练对话"
            rows.append(
                f"""
                <article class="agent-thread-row{active}" data-updated="{esc(updated_at)}">
                  <a href="/agent/conversations/{conversation["id"]}" title="{esc(title_text)} · {esc(updated_at)}">
                    <strong>{esc(title_text)}</strong>
                    <span class="agent-thread-time">{esc(updated_at)}</span>
                  </a>
                  <form method="post" action="/agent/conversations/{conversation["id"]}/delete" data-confirm="删除这条教练线程吗？">
                    <input type="hidden" name="return_to" value="{esc(return_to)}">
                    <button class="agent-thread-delete-button" type="submit" aria-label="删除线程">×</button>
                  </form>
                </article>"""
            )
        sections.append(
            f"""
            <section class="agent-thread-group" data-thread-group="{esc(group_id)}">
              <h2><span>{esc(label)}</span><small>{len(items)}</small></h2>
              <div class="agent-thread-group-items">{"".join(rows)}</div>
            </section>"""
        )
    if sections:
        return "".join(sections)
    return '<p class="muted agent-thread-empty">还没有教练线程。直接问一句“我今天练什么”就能开始。</p>'


def attempt_grading_references(attempt, references):
    configured = bool(attempt["grading_references_configured"])
    valid_by_id = {int(reference["id"]): reference for reference in references}
    if configured:
        try:
            stored_ids = {int(value) for value in json.loads(attempt["grading_reference_ids"] or "[]")}
        except (TypeError, ValueError, json.JSONDecodeError):
            stored_ids = set()
    else:
        stored_ids = set(valid_by_id)
    selected = [reference for reference in references if int(reference["id"]) in stored_ids]
    custom_answer = attempt["custom_reference_answer"] or ""
    return selected, {int(reference["id"]) for reference in selected}, custom_answer


def grading_references_from_form(data, references):
    form = parse_qs(data, keep_blank_values=True)
    requested_ids = set()
    for value in form.get("reference_id", []):
        try:
            requested_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    selected = [reference for reference in references if int(reference["id"]) in requested_ids]
    selected_ids = [int(reference["id"]) for reference in selected]
    custom_answer = form.get("custom_reference_answer", [""])[0].strip()
    return selected, selected_ids, custom_answer


def save_attempt_grading_references(conn, attempt_id, selected_ids, custom_answer):
    conn.execute(
        """
        UPDATE attempts
           SET grading_references_configured = 1,
               grading_reference_ids = ?,
               custom_reference_answer = ?
         WHERE id = ?
        """,
        (json.dumps(selected_ids, ensure_ascii=False), custom_answer, attempt_id),
    )


def favorite_button(kind, item_id, is_favorite, return_to, compact=False):
    label = "取消收藏" if is_favorite else "收藏"
    compact_class = " compact" if compact else ""
    active_class = " active" if is_favorite else ""
    return f"""
    <form class="favorite-form{compact_class}" method="post" action="/{kind}/{item_id}/favorite">
      <input type="hidden" name="return_to" value="{esc(return_to)}">
      <button class="favorite-button{active_class}" type="submit" title="{label}" aria-label="{label}">
        <svg aria-hidden="true" viewBox="0 0 24 24">
          <path d="M12 3.7l2.47 5.01 5.53.8-4 3.9.94 5.51L12 16.32l-4.94 2.6.94-5.51-4-3.9 5.53-.8L12 3.7z"></path>
        </svg>
      </button>
    </form>"""


def report_status(attempt_count, report_count):
    if report_count:
        return "已批改"
    if attempt_count:
        return "已作答"
    return "未做"


REGION_ORDER = [
    "全国",
    "安徽",
    "北京",
    "福建",
    "甘肃",
    "广东",
    "广西",
    "贵州",
    "海南",
    "河北",
    "河南",
    "黑龙江",
    "湖北",
    "湖南",
    "吉林",
    "江苏",
    "江西",
    "辽宁",
    "内蒙古",
    "宁夏",
    "青海",
    "山东",
    "山西",
    "陕西",
    "上海",
    "四川",
    "天津",
    "西藏",
    "新疆",
    "云南",
    "浙江",
    "重庆",
    "广州",
    "深圳",
]
QUESTION_TYPE_ORDER = ["归纳概括", "综合分析", "提出对策", "公文写作", "综合写作"]
DEFAULT_PAGE_SIZE = 12


def sort_regions(values):
    order = {value: index for index, value in enumerate(REGION_ORDER)}
    return sorted(values, key=lambda value: (order.get(value, 999), value))


def _exam_region(exam_type):
    for suffix in ("省考", "市考", "公考", "选调"):
        if exam_type.endswith(suffix):
            return exam_type[: -len(suffix)]
    return exam_type


def sort_exam_types(values):
    region_order = {value: index for index, value in enumerate(REGION_ORDER)}

    def key(value):
        if value == "国考":
            return (0, 0, value)
        if value == "公安院校联考":
            return (9, 0, value)
        group = 2 if value.endswith("选调") else 1
        return (group, region_order.get(_exam_region(value), 999), value)

    return sorted(values, key=key)


def sort_question_types(values):
    order = {value: index for index, value in enumerate(QUESTION_TYPE_ORDER)}
    return sorted(values, key=lambda value: (order.get(value, 999), value))


def progress_width(done, total):
    if not total:
        return "0%"
    return f"{max(0, min(100, round(done * 100 / total)))}%"


def recommended_timed_paper(conn, latest_paper_id=None):
    """Choose the nearest paper with an unfinished non-essay question.

    Papers use the same order as the default paper library.  The latest
    practice paper wins when it is still unfinished; otherwise we expand to
    its left and right neighbours until an eligible paper is found.  综合写作
    questions do not participate in completion checks.
    """
    papers = conn.execute(
        """
        SELECT p.id, p.paper_name, p.year, p.region,
               SUM(CASE WHEN q.question_type <> '综合写作' THEN 1 ELSE 0 END) AS eligible_questions,
               SUM(CASE WHEN q.question_type <> '综合写作'
                         AND NOT EXISTS (
                             SELECT 1 FROM attempts a WHERE a.question_id = q.id
                         ) THEN 1 ELSE 0 END) AS unattempted_questions,
               (
                   SELECT q2.id
                     FROM questions q2
                    WHERE q2.paper_id = p.id
                      AND q2.question_type <> '综合写作'
                      AND NOT EXISTS (
                          SELECT 1 FROM attempts a2 WHERE a2.question_id = q2.id
                      )
                 ORDER BY CASE WHEN q2.question_number = 0 THEN 999 ELSE q2.question_number END,
                          q2.id
                    LIMIT 1
               ) AS next_question_id
          FROM papers p
          JOIN questions q ON q.paper_id = p.id
      GROUP BY p.id
      ORDER BY p.year DESC, p.zhejiang_relevance DESC, p.region, p.paper_category, p.id DESC
        """
    ).fetchall()
    if not papers:
        return None

    def unfinished(row):
        return row["eligible_questions"] > 0 and row["unattempted_questions"] > 0

    start_index = next(
        (index for index, paper in enumerate(papers) if paper["id"] == latest_paper_id),
        None,
    )
    if start_index is None:
        return next((paper for paper in papers if unfinished(paper)), None)
    if unfinished(papers[start_index]):
        return papers[start_index]
    for distance in range(1, len(papers)):
        left = start_index - distance
        right = start_index + distance
        if left >= 0 and unfinished(papers[left]):
            return papers[left]
        if right < len(papers) and unfinished(papers[right]):
            return papers[right]
    return None


def activity_level(count):
    if count <= 0:
        return 0
    if count < 3:
        return 1
    if count < 5:
        return 2
    if count < 7:
        return 3
    return 4


def module_trend_svg(points):
    if not points:
        return '<div class="module-trend-empty">暂无可绘制的分数趋势。</div>'
    width, height = 560, 190
    left, right, top, bottom = 42, 18, 18, 32
    chart_w = width - left - right
    chart_h = height - top - bottom
    values = [float(point["average"]) for point in points]

    def x_at(index):
        if len(points) == 1:
            return left + chart_w / 2
        return left + chart_w * index / (len(points) - 1)

    def y_at(value):
        return top + chart_h * (100 - value) / 100

    polyline = " ".join(f"{x_at(index):.1f},{y_at(value):.1f}" for index, value in enumerate(values))
    circles = []
    labels = []
    for index, point in enumerate(points):
        x = x_at(index)
        y = y_at(float(point["average"]))
        title = f"{point['label']}题：{point['average']}分（{point['start_date']} 至 {point['end_date']}）"
        circles.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{esc(title)}</title></circle>')
        labels.append(f'<text x="{x:.1f}" y="{height - 8}" text-anchor="middle">{esc(point["label"])}</text>')
    grid = []
    for tick in (0, 25, 50, 75, 100):
        y = y_at(tick)
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}"></line>')
        grid.append(f'<text x="8" y="{y + 4:.1f}">{tick}</text>')
    return f"""
    <svg class="module-trend-chart" viewBox="0 0 {width} {height}" role="img" aria-label="每 5 题均值趋势">
      <g class="trend-grid">{"".join(grid)}</g>
      <polyline points="{polyline}"></polyline>
      <g class="trend-points">{"".join(circles)}</g>
      <g class="trend-labels">{"".join(labels)}</g>
    </svg>"""


def render_module_score_detail(module):
    trend = module_trend_svg(module.get("trend") or [])
    questions = list(reversed(module.get("questions") or []))
    rows = (
        "".join(
            f"""
        <tr>
          <td><a href="/attempts/{item["attempt_id"]}">{esc(item["title"] or "查看作答")}</a></td>
          <td>{esc(item["created_date"])}</td>
          <td>{esc(item["region"])} {esc(item["year"])}</td>
          <td>{esc(format_duration(item.get("duration_seconds"), "—"))}</td>
          <td>{item["score"]:.1f}</td>
        </tr>"""
            for item in questions
        )
        or '<tr><td colspan="5">暂无已识别分数的批改报告。</td></tr>'
    )
    average = module["average_score"]
    average_text = f"{average:.1f}" if average is not None else "—"
    latest = module["latest_score"]
    latest_text = f"{latest:.1f}" if latest is not None else "—"
    return f"""
    <details class="module-score-card">
      <summary>
        <span>{esc(module["name"])}</span>
        <strong>{average_text}</strong>
        <small>{module["question_count"]} 题 · 最近 {latest_text}</small>
      </summary>
      <div class="module-score-detail">
        <div class="section-heading"><div><p class="eyebrow">5-question Rolling Average</p><h3>每 5 题均值趋势</h3></div></div>
        {trend}
        <div class="report-table-wrap module-score-table-wrap" tabindex="0" aria-label="模块题目分数列表，默认显示最近 10 题，可滚动查看更早题目">
          <table class="report-table module-score-table">
            <thead><tr><th>题目</th><th>作答日期</th><th>地区年份</th><th>用时</th><th>百分制</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
      </div>
    </details>"""


QUESTION_WORK_STATUS_OPTIONS = [
    ("", "全部"),
    ("attempted", "已作答"),
    ("unattempted", "未作答"),
    ("graded", "已批改"),
    ("ungraded", "未批改"),
]
YEAR_FILTER_MIN = 2020
YEAR_FILTER_MAX = max(2026, datetime.now().year)
PAPER_WORK_STATUS_OPTIONS = [
    ("", "全部"),
    ("started", "已开始"),
    ("untouched", "未开始"),
    ("completed", "已完成"),
    ("uncompleted", "未完成"),
    ("graded", "已批改"),
    ("ungraded", "未批改"),
]


def select_options(options, selected):
    return "".join(
        f'<option value="{esc(value)}"{" selected" if value == selected else ""}>{esc(label)}</option>'
        for value, label in options
    )


def normalized_year(value, fallback):
    try:
        year = int(value)
    except (TypeError, ValueError):
        year = fallback
    return min(max(year, YEAR_FILTER_MIN), YEAR_FILTER_MAX)


def year_range_filter(filters):
    start = normalized_year(filters.get("year_from"), YEAR_FILTER_MIN)
    end = normalized_year(filters.get("year_to"), YEAR_FILTER_MAX) if filters.get("year_to") else YEAR_FILTER_MAX
    if start > end:
        start, end = end, start
    hidden_to = "" if end >= YEAR_FILTER_MAX else str(end)
    return f"""
          <div class="year-range-field" data-year-range data-min-year="{YEAR_FILTER_MIN}" data-max-year="{YEAR_FILTER_MAX}">
            <input type="hidden" name="year_from" value="{start}" data-year-from-hidden>
            <input type="hidden" name="year_to" value="{esc(hidden_to)}" data-year-to-hidden>
            <div class="year-range-head"><span class="field-label">年份范围</span><strong data-year-range-label>{start} - {end}</strong></div>
            <div class="year-range-sliders" aria-label="年份范围">
              <span class="year-range-track" aria-hidden="true"></span>
              <input type="range" min="{YEAR_FILTER_MIN}" max="{YEAR_FILTER_MAX}" step="1" value="{start}" data-year-from>
              <input type="range" min="{YEAR_FILTER_MIN}" max="{YEAR_FILTER_MAX}" step="1" value="{end}" data-year-to>
            </div>
          </div>"""


def requested_page(query):
    try:
        return max(1, int(query.get("page", ["1"])[0]))
    except (TypeError, ValueError):
        return 1


def requested_page_size(query):
    try:
        return max(4, min(40, int(query.get("per_page", [str(DEFAULT_PAGE_SIZE)])[0])))
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE


def pagination_html(path, filters, page, total_items, page_size):
    if not total_items:
        return ""
    total_pages = max(1, math.ceil(total_items / page_size))
    page = min(max(1, page), total_pages)
    start_item = (page - 1) * page_size + 1
    end_item = min(total_items, page * page_size)

    def href(target):
        params = {key: value for key, value in filters.items() if value not in ("", None)}
        if target > 1:
            params["page"] = target
        query_string = urlencode(params)
        return path + (f"?{query_string}" if query_string else "")

    page_numbers = {1, total_pages}
    page_numbers.update(range(max(1, page - 2), min(total_pages, page + 2) + 1))
    ordered = sorted(page_numbers)
    links = []
    previous = 0
    for number in ordered:
        if previous and number - previous > 1:
            links.append('<span class="page-gap" aria-hidden="true">…</span>')
        if number == page:
            links.append(f'<span class="page-number current" aria-current="page">{number}</span>')
        else:
            links.append(f'<a class="page-number" href="{esc(href(number))}">{number}</a>')
        previous = number

    prev_link = (
        f'<a class="page-step" href="{esc(href(page - 1))}">上一页</a>'
        if page > 1
        else '<span class="page-step disabled">上一页</span>'
    )
    next_link = (
        f'<a class="page-step" href="{esc(href(page + 1))}">下一页</a>'
        if page < total_pages
        else '<span class="page-step disabled">下一页</span>'
    )
    hidden_fields = "".join(
        f'<input type="hidden" name="{esc(key)}" value="{esc(value)}">'
        for key, value in filters.items()
        if value not in ("", None) and key != "page"
    )
    return f"""
    <nav class="pagination" aria-label="分页">
      <p>第 {start_item}-{end_item} 项，共 {total_items} 项</p>
      <div class="pagination-controls">
        <div class="page-links">{prev_link}{"".join(links)}{next_link}</div>
        <form class="page-jump" method="get" action="{esc(path)}">
          {hidden_fields}
          <label><span>跳至</span><input name="page" type="number" min="1" max="{total_pages}" value="{page}" inputmode="numeric" aria-label="页码"><span>页</span></label>
          <button class="button small" type="submit">跳转</button>
        </form>
      </div>
    </nav>"""


def tabbed_materials(
    materials,
    scope,
    highlight_scope=None,
    question_id=None,
    saved_annotations=None,
):
    if not materials:
        return '<p class="muted">未录入材料原文。</p>'
    tabs = []
    panels = []
    saved_annotations = saved_annotations or {}
    for index, material in enumerate(materials):
        title = material["title"] or ("材料" + str(material["material_number"]))
        tab_id = f"{scope}-material-tab-{index}"
        panel_id = f"{scope}-material-panel-{index}"
        selected = index == 0
        tabs.append(
            f'<button class="content-tab{" active-tab" if selected else ""}" id="{tab_id}" '
            f'type="button" role="tab" aria-selected="{"true" if selected else "false"}" '
            f'aria-controls="{panel_id}" data-tab-target="{panel_id}">{esc(title)}</button>'
        )
        content = material["content"]
        if highlight_scope:
            material_keys = material.keys() if hasattr(material, "keys") else material
            material_id = material["id"] if "id" in material_keys else f"{scope}-{material['material_number']}"
            persistence_attrs = ""
            if question_id is not None:
                persistence_attrs = (
                    f'data-annotation-target="material" data-question-id="{int(question_id)}" '
                    f"{annotation_data_attributes(saved_annotations.get(int(material['material_number'])))} "
                )
            material_body = (
                f'<div class="preline material-text" data-material-highlight '
                f'data-highlight-scope="{esc(highlight_scope)}" '
                f'data-material-id="{esc(material_id)}" '
                f'data-material-number="{esc(material["material_number"])}" '
                f"{persistence_attrs}>{esc(content)}</div>"
            )
        else:
            material_body = f'<div class="preline">{pre(content)}</div>'
        panels.append(
            f'<section class="tab-panel{" active-panel" if selected else ""}" id="{panel_id}" '
            f'role="tabpanel" aria-labelledby="{tab_id}"{" hidden" if not selected else ""}>'
            f"{material_body}</section>"
        )
    if highlight_scope:
        tab_header = (
            '<div class="content-tabs-row">'
            f'<div class="content-tabs" role="tablist" aria-label="相关材料">{"".join(tabs)}</div>'
            '<button class="button small ghost" type="button" data-clear-active-material-highlights>清除本材料标注</button>'
            "</div>"
        )
    else:
        tab_header = f'<div class="content-tabs" role="tablist" aria-label="相关材料">{"".join(tabs)}</div>'
    return f'<div class="tabbed-content" data-tabs>{tab_header}{"".join(panels)}</div>'


def tabbed_references(references, scope):
    if not references:
        return '<p class="muted">暂无参考答案。</p>'
    tabs = []
    panels = []
    for index, ref in enumerate(references):
        organization = ref["organization"]
        canonical = ref["canonical_organization"] or organization
        group_label = f'<small class="org-group">归组：{esc(canonical)}</small>' if canonical != organization else ""
        tab_id = f"{scope}-reference-tab-{index}"
        panel_id = f"{scope}-reference-panel-{index}"
        selected = index == 0
        tabs.append(
            f'<button class="content-tab{" active-tab" if selected else ""}" id="{tab_id}" '
            f'type="button" role="tab" aria-selected="{"true" if selected else "false"}" '
            f'aria-controls="{panel_id}" data-tab-target="{panel_id}">{esc(organization)}</button>'
        )
        panels.append(
            f'<article class="reference tab-panel{" active-panel" if selected else ""}" id="{panel_id}" '
            f'role="tabpanel" aria-labelledby="{tab_id}"{" hidden" if not selected else ""}>'
            f"<header><div><strong>{esc(organization)}</strong>{group_label}</div>"
            f"<span>{'已校对' if ref['is_reviewed'] else '待校对'}</span></header>"
            f'<div class="preline answer">{pre(ref["answer_text"])}</div></article>'
        )
    return (
        f'<div class="tabbed-content" data-tabs><div class="content-tabs" role="tablist" '
        f'aria-label="参考答案">{"".join(tabs)}</div>{"".join(panels)}</div>'
    )


def evidence_href(kind, item_id, return_to="", suffix_hash=""):
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return ""
    if kind == "attempt":
        return local_url(f"/attempts/{item_id}", return_to=return_to, fragment=suffix_hash)
    if kind == "report":
        return local_url(f"/grading-reports/{item_id}", return_to=return_to, fragment=suffix_hash)
    if kind in ("question", "material", "reference"):
        return local_url(f"/questions/{item_id}", return_to=return_to, fragment=suffix_hash)
    return ""


def knowledge_evidence_href(knowledge_id, return_to=""):
    knowledge_id = str(knowledge_id or "").strip()
    if not re.fullmatch(r"knowledge:[a-z0-9][a-z0-9:_-]+", knowledge_id, flags=re.I):
        return ""
    return local_url(f"/agent/knowledge/{quote(knowledge_id, safe=':')}", return_to=return_to)


@lru_cache(maxsize=1)
def knowledge_evidence_titles():
    return {
        str(item.get("id") or ""): str(item.get("title") or "").strip()
        for item in load_knowledge_items()
        if item.get("id")
    }


def knowledge_evidence_label(knowledge_id):
    title = knowledge_evidence_titles().get(str(knowledge_id or "").strip(), "")
    return f"知识卡 · {title}" if title else "查看知识卡"


def question_code_href(question_code, return_to=""):
    question_code = str(question_code or "").strip().upper()
    if not re.fullmatch(r"GKS-\d+-Q\d+", question_code):
        return ""
    return local_url(f"/questions/by-code/{quote(question_code, safe='')}", return_to=return_to)


def link_agent_evidence_refs(html_text, return_to=""):
    def replace_ref(match):
        label = match.group(0)
        kind = match.group(1).lower()
        id1 = match.group(2)
        id2 = match.group(3)

        href = ""
        if kind == "grading_report":
            href = evidence_href("report", id1, return_to)
        elif kind == "personal_note":
            if "attempt" in label:
                href = evidence_href("attempt", id1, return_to)
            else:
                href = evidence_href("question", id1, return_to)
        elif kind == "material":
            if id2 is not None:
                href = evidence_href("material", id1, return_to, f"material-{id2}")
            else:
                href = evidence_href("material", id1, return_to)
        elif kind == "reference":
            if id2 is not None:
                href = evidence_href("reference", id1, return_to, f"reference-{id2}")
            else:
                href = evidence_href("reference", id1, return_to)
        elif kind == "report":
            href = evidence_href("report", id1, return_to)
        elif kind == "attempt":
            href = evidence_href("attempt", id1, return_to)
        elif kind == "question":
            href = evidence_href("question", id1, return_to)

        return f'<a class="agent-evidence-link" href="{esc(href)}">{label}</a>' if href else label

    def attempt_id_fallback(match):
        val = match.group(1)
        href = evidence_href("attempt", val, return_to)
        return f'<a class="agent-evidence-link" href="{esc(href)}">作答ID:{val}</a>' if href else match.group(0)

    def question_id_fallback(match):
        val = match.group(1)
        href = evidence_href("question", val, return_to)
        return f'<a class="agent-evidence-link" href="{esc(href)}">题目ID:{val}</a>' if href else match.group(0)

    def knowledge_ref(match):
        knowledge_id = match.group(1)
        href = knowledge_evidence_href(knowledge_id, return_to)
        label = knowledge_evidence_label(knowledge_id)
        return (
            f'<a class="agent-evidence-link" href="{esc(href)}" '
            f'title="{esc(label)}">{esc(label)}</a>'
            if href
            else esc(label)
        )

    html_text = re.sub(
        r"(?<![\w/-])(grading_report|personal_note|material|reference|report|attempt|question)[:：](?:attempt-|question-)?(\d+)(?:[:：-](?:attempt-|question-)?(\d+))?",
        replace_ref,
        html_text,
        flags=re.I,
    )
    html_text = re.sub(
        r"(?<![\w/-])(?:attempt_id\s*[:：=]?\s*|作答ID\s*[:：=]?\s*)(\d+)", attempt_id_fallback, html_text, flags=re.I
    )
    html_text = re.sub(
        r"(?<![\w/-])(?:question_id\s*[:：=]?\s*|题目ID\s*[:：=]?\s*|qid\s*[:：=]?\s*)(\d+)",
        question_id_fallback,
        html_text,
        flags=re.I,
    )
    html_text = re.sub(r"(?<![\w/-])(knowledge:[a-z0-9][a-z0-9:_-]+)", knowledge_ref, html_text, flags=re.I)
    html_text = re.sub(
        r"\[(?:evidence|证据)[:：]\s*(<a class=\"agent-evidence-link\"[^>]*>.*?</a>)\s*\]",
        r"\1",
        html_text,
        flags=re.I,
    )
    return html_text


def inline_markdown(value, return_to=""):
    text = esc(value)
    text = re.sub(
        r"\[(GKS-\d+-Q\d+)\]\([^)]+\)",
        lambda match: (
            f'<a class="agent-evidence-link" href="{esc(question_code_href(match.group(1), return_to))}">查看题目</a>'
            if question_code_href(match.group(1), return_to)
            else "查看题目"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(r"(?<![/=\w-])\bGKS-\d+-Q\d+\b", "内部题目", text, flags=re.I)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = link_agent_evidence_refs(text, return_to)
    # Models sometimes wrap evidence IDs in Markdown code ticks. Once the ID
    # becomes a styled evidence link, retaining the surrounding <code> creates
    # a second rectangular background around the pill.
    text = re.sub(
        r'<code>\s*(<a class="agent-evidence-link"[^>]*>.*?</a>)\s*</code>',
        r"\1",
        text,
        flags=re.I | re.S,
    )
    return text


GRADING_ANNOTATION_PATTERN = re.compile(
    r"\[(好|可改|删|补|亮点|润色|修改|删减|补充|关键)\|([^\]]+)\]"
)

GRADING_ANNOTATION_META = {
    "好": ("亮点", "good", "positive"),
    "亮点": ("亮点", "good", "positive"),
    "润色": ("轻微润色", "polish", "low"),
    "可改": ("建议修改", "revise", "medium"),
    "修改": ("建议修改", "revise", "medium"),
    "删": ("建议删减", "delete", "high"),
    "删减": ("建议删减", "delete", "high"),
    "补": ("建议补充", "add", "high"),
    "补充": ("建议补充", "add", "high"),
    "关键": ("关键问题", "critical", "critical"),
}

GRADING_SEVERITIES = {"positive", "low", "medium", "high", "critical"}


def report_answer_snapshot(prompt_text="", result=None, fallback=""):
    """Return the answer used when this report was generated."""
    result = result if isinstance(result, dict) else {}
    if isinstance(result.get("answer_snapshot"), str):
        return result["answer_snapshot"]

    prompt_text = str(prompt_text or "")
    structured_match = re.search(r"本次作答：\s*", prompt_text)
    if structured_match:
        try:
            payload, _ = json.JSONDecoder().raw_decode(prompt_text[structured_match.end() :].lstrip())
            if isinstance(payload, dict) and isinstance(payload.get("answer_text"), str):
                return payload["answer_text"]
        except (json.JSONDecodeError, TypeError):
            pass

    package_marker = "## 我的答案"
    standard_marker = "## 请按以下标准批改"
    if package_marker in prompt_text and standard_marker in prompt_text:
        package_section = prompt_text.split(package_marker, 1)[1].split(standard_marker, 1)[0]
        blocks = [block.strip() for block in re.split(r"\n\s*\n", package_section) if block.strip()]
        if blocks:
            return blocks[-1]
    return str(fallback or "")


def _grading_annotation_items(value):
    items = []
    for match in GRADING_ANNOTATION_PATTERN.finditer(value or ""):
        fields = [field.strip() for field in match.group(2).split("|")]
        if len(fields) < 2:
            continue
        kind = match.group(1)
        label, css_class, default_severity = GRADING_ANNOTATION_META[kind]
        severity = fields[3] if len(fields) > 3 and fields[3] in GRADING_SEVERITIES else default_severity
        items.append(
            {
                "kind": kind,
                "text": fields[0],
                "note": fields[1],
                "anchor": fields[2] if len(fields) > 2 else "",
                "severity": severity,
                "suggestion": fields[4] if len(fields) > 4 else "",
                "label": label,
                "css_class": css_class,
            }
        )
    return items


def _locate_grading_annotations(source_text, items):
    occupied = []
    located = [None] * len(items)
    for index, item in enumerate(items):
        if item["css_class"] == "add":
            continue
        start = -1
        if item["text"]:
            search_from = 0
            while True:
                candidate = source_text.find(item["text"], search_from)
                if candidate < 0:
                    break
                end = candidate + len(item["text"])
                if not any(candidate < used_end and end > used_start for used_start, used_end in occupied):
                    start = candidate
                    occupied.append((candidate, end))
                    break
                search_from = candidate + 1
        located[index] = {**item, "start": start, "end": start + len(item["text"]) if start >= 0 else -1}

    for index, item in enumerate(items):
        if item["css_class"] != "add":
            continue
        start = -1
        if item["anchor"]:
            candidate = source_text.find(item["anchor"])
            if candidate >= 0:
                start = candidate + len(item["anchor"])
                for used_start, used_end in occupied:
                    if used_start < start < used_end:
                        start = used_end
                        break
        located[index] = {**item, "start": start, "end": start}
    return located


def _render_annotated_source(source_text, items, scope):
    located_items = [
        (number, item)
        for number, item in enumerate(items, start=1)
        if item["start"] >= 0
    ]
    if not located_items:
        return ""
    chunks = []
    cursor = 0
    for number, item in sorted(located_items, key=lambda pair: (pair[1]["start"], pair[1]["end"])):
        chunks.append(esc(source_text[cursor : item["start"]]))
        if item["css_class"] == "add":
            chunks.append(
                f'<span class="grading-insert-anchor severity-{esc(item["severity"])}" '
                f'id="{esc(scope)}-mark-{number}" tabindex="0" data-annotation-id="{number}" '
                f'aria-label="在此处补充"><i></i><sup>{number}</sup></span>'
            )
            cursor = item["start"]
            continue
        marked_text = esc(source_text[item["start"] : item["end"]])
        if item["css_class"] == "delete":
            marked_text = f"<del>{marked_text}</del>"
        chunks.append(
            f'<mark class="grading-source-mark {esc(item["css_class"])} severity-{esc(item["severity"])}" '
            f'id="{esc(scope)}-mark-{number}" tabindex="0" data-annotation-id="{number}">'
            f'{marked_text}<sup>{number}</sup></mark>'
        )
        cursor = item["end"]
    chunks.append(esc(source_text[cursor:]))
    return (
        '<div class="grading-annotation-source grading-review-document">'
        '<div class="grading-annotation-source-label"><strong>批改时答案快照</strong>'
        "<span>标号与右侧批注一一对应</span></div>"
        f'<div class="grading-annotation-source-text">{"".join(chunks)}</div>'
        "</div>"
    )


def render_grading_annotations(value, source_text="", scope="grading-annotation"):
    items = _grading_annotation_items(value)
    if not items:
        return ""
    items = _locate_grading_annotations(source_text or "", items)
    # Keep both columns in document order. Models commonly return an insertion
    # suggestion before later inline edits; preserving that response order
    # makes its connector cross every annotation between the card and anchor.
    items = sorted(
        items,
        key=lambda item: (
            item["start"] < 0,
            item["start"] if item["start"] >= 0 else math.inf,
            item["end"] if item["end"] >= 0 else math.inf,
        ),
    )
    source_html = _render_annotated_source(source_text or "", items, scope)
    rows = []
    for number, item in enumerate(items, start=1):
        location_link = (
            f'<a href="#{esc(scope)}-mark-{number}">定位原文</a>'
            if item["start"] >= 0
            else '<span class="grading-annotation-unlocated">未能定位</span>'
        )
        quote_html = ""
        if item["css_class"] == "add" or item["start"] < 0:
            text_html = (
                f"<del>{esc(item['text'])}</del>"
                if item["css_class"] == "delete"
                else esc(item["suggestion"] or item["text"])
            )
            quote_html = f'<p class="grading-annotation-quote">{text_html}</p>'
        suggestion_html = ""
        if item["suggestion"] and item["css_class"] != "add":
            suggestion_html = (
                f'<p class="grading-annotation-suggestion"><strong>建议：</strong>'
                f'{esc(item["suggestion"])}</p>'
            )
        rows.append(
            f"""
            <li class="grading-annotation-note {esc(item["css_class"])} severity-{esc(item["severity"])}"
                id="{esc(scope)}-note-{number}" data-annotation-id="{number}">
              <div><span>{number}</span><strong>{esc(item["label"])}</strong>{location_link}</div>
              {quote_html}
              <p>{esc(item["note"])}</p>
              {suggestion_html}
            </li>"""
        )
    return (
        '<section class="grading-annotation-map" data-grading-review>'
        f"{source_html}"
        '<svg class="grading-review-connectors" aria-hidden="true"></svg>'
        f'<ol class="grading-annotation-notes grading-review-comments">{"".join(rows)}</ol>'
        "</section>"
    )


def _is_table_separator(line):
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def markdownish(value, return_to="", source_text="", annotation_scope="grading-annotation"):
    lines = (value or "").splitlines()
    output = []
    in_list = False
    in_ol = False
    index = 0

    def close_lists():
        nonlocal in_list, in_ol
        if in_list:
            output.append("</ul>")
            in_list = False
        if in_ol:
            output.append("</ol>")
            in_ol = False

    while index < len(lines):
        raw = lines[index]
        line = raw.strip()
        if not line:
            close_lists()
            index += 1
            continue
        if line == "## 维度评分":
            close_lists()
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("## "):
                index += 1
            continue
        annotation_markers = tuple(f"[{kind}|" for kind in GRADING_ANNOTATION_META)
        if any(marker in line for marker in annotation_markers):
            close_lists()
            annotation_lines = []
            while index < len(lines):
                annotation_line = lines[index].strip()
                if not any(marker in annotation_line for marker in annotation_markers):
                    break
                annotation_lines.append(annotation_line)
                index += 1
            output.append(
                render_grading_annotations(
                    "\n".join(annotation_lines),
                    source_text=source_text,
                    scope=annotation_scope,
                )
                or f"<p>{inline_markdown(line, return_to)}</p>"
            )
            continue
        if "|" in line and index + 1 < len(lines) and _is_table_separator(lines[index + 1].strip()):
            close_lists()
            raw_headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
            headers = [inline_markdown(cell, return_to) for cell in raw_headers]
            rows = []
            index += 2
            while index < len(lines) and "|" in lines[index].strip():
                cells = [
                    inline_markdown(cell.strip(), return_to) for cell in lines[index].strip().strip("|").split("|")
                ]
                rows.append(cells)
                index += 1
            head = "".join(f"<th>{cell}</th>" for cell in headers)
            body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
            table_class = " report-score-table" if "命中情况" in raw_headers else ""
            output.append(
                f'<div class="report-table-wrap"><table class="report-table{table_class}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
            )
            continue
        if line.startswith("### "):
            close_lists()
            output.append(f"<h3>{inline_markdown(line[4:], return_to)}</h3>")
        elif line.startswith("## "):
            close_lists()
            output.append(f"<h2>{inline_markdown(line[3:], return_to)}</h2>")
        elif line.startswith("# "):
            close_lists()
            output.append(f"<h2>{inline_markdown(line[2:], return_to)}</h2>")
        elif line.startswith(("- ", "* ")):
            if in_ol:
                output.append("</ol>")
                in_ol = False
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{inline_markdown(line[2:], return_to)}</li>")
        elif re.match(r"^\d+[.、]\s+", line):
            if in_list:
                output.append("</ul>")
                in_list = False
            if not in_ol:
                output.append("<ol>")
                in_ol = True
            list_item = re.sub(r"^\d+[.、]\s+", "", line)
            output.append(f"<li>{inline_markdown(list_item, return_to)}</li>")
        else:
            close_lists()
            output.append(f"<p>{inline_markdown(line, return_to)}</p>")
        index += 1
    close_lists()
    return "".join(output)


def split_agent_structured_json(value):
    text = value or ""
    for match in reversed(list(re.finditer(r"`{3,}json\s*(.*?)\s*`{3,}", text, flags=re.S | re.I))):
        raw_json = match.group(1).strip()
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and {"summary", "weaknesses", "next_actions", "recommended_questions"} & set(
            parsed
        ):
            cleaned = (text[: match.start()] + text[match.end() :]).strip()
            return cleaned, parsed
    return text, None


def render_agent_structured_output(data, return_to=""):
    if not data:
        return ""
    weaknesses = data.get("weaknesses") or []
    actions = data.get("next_actions") or []
    questions = data.get("recommended_questions") or []
    weakness_rows = "".join(
        f"""
        <article class="agent-structured-item">
          <strong>{esc(item.get("name") or "短板")}</strong>
          <span>{esc(item.get("severity") or "medium")}</span>
          <p>{esc(item.get("reason") or "")}</p>
          <small>{link_agent_evidence_refs(esc("；".join(str(ref) for ref in (item.get("evidence_refs") or []) if ref)), return_to)}</small>
        </article>"""
        for item in weaknesses[:4]
        if isinstance(item, dict)
    )
    action_rows = "".join(
        f"""
        <article class="agent-structured-item">
          <strong>{esc(item.get("action") or "下一步")}</strong>
          <span>{esc(item.get("timebox") or "")}</span>
          <p>{esc(item.get("target") or "")}</p>
        </article>"""
        for item in actions[:5]
        if isinstance(item, dict)
    )
    question_rows = "".join(
        f"""
        <article class="agent-structured-item">
          <strong>{esc(item.get("title") or "推荐题目")}</strong>
          <span>{link_agent_evidence_refs(esc("question_id " + str(item.get("question_id"))) if item.get("question_id") else "", return_to)}</span>
          <p>{esc(item.get("reason") or "")}</p>
        </article>"""
        for item in questions[:4]
        if isinstance(item, dict) and (item.get("title") or item.get("question_id"))
    )
    if not any([data.get("summary"), weakness_rows, action_rows, question_rows]):
        return ""
    return f"""
    <details class="agent-structured-output">
      <summary><strong>结构化提取</strong><span>{esc(data.get("summary") or "用于系统提取短板、行动项和推荐题")}</span></summary>
      <div class="agent-structured-content">
        <p class="muted">这是 AI 回复末尾的机器可读 JSON 渲染结果，主要用于系统记录行动项和评测；正文回答仍以上方内容为准。</p>
        {f"<p>{esc(data.get('summary'))}</p>" if data.get("summary") else ""}
        {f'<div class="agent-structured-grid">{weakness_rows}</div>' if weakness_rows else ""}
        {f'<div class="agent-structured-grid">{action_rows}</div>' if action_rows else ""}
        {f'<div class="agent-structured-grid">{question_rows}</div>' if question_rows else ""}
      </div>
    </details>"""


def agent_response_html(value, return_to=""):
    text, structured = split_agent_structured_json(value)
    return markdownish(text, return_to) + render_agent_structured_output(structured, return_to)


def _agent_rag_source_label(source_type):
    return {
        "question": "题目",
        "attempt": "作答",
        "material": "材料",
        "grading_report": "批改",
        "reference_answer": "参考",
        "knowledge": "教材",
        "aggregate": "统计",
        "weakness_profile": "画像",
        "candidate_question": "候选题",
        "report": "报告",
        "note": "笔记",
    }.get(source_type or "", source_type or "证据")


def _agent_rag_card_href(card, return_to=""):
    url = (card.get("url") or "").strip()
    if not url:
        evidence_id = str(card.get("evidence_id") or "").strip()
        if evidence_id.startswith("knowledge:"):
            return knowledge_evidence_href(evidence_id, return_to)
        if card.get("attempt_id"):
            return evidence_href("attempt", card.get("attempt_id"), return_to)
        if card.get("question_id"):
            return evidence_href("question", card.get("question_id"), return_to)
        return ""
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or not url.startswith("/"):
        return ""
    query = parse_qs(parsed.query)
    if return_to and "return_to" not in query:
        query["return_to"] = [return_to]
    encoded = urlencode(query, doseq=True)
    return f"{parsed.path}{'?' + encoded if encoded else ''}"


def render_agent_rag_panel(metadata, return_to=""):
    rag = (metadata or {}).get("rag") or {}
    cards = rag.get("evidence_cards") or []
    if not rag and not cards:
        return ""
    sufficiency = rag.get("evidence_sufficiency") or {}
    level = sufficiency.get("level") or "unknown"
    label = sufficiency.get("label") or "证据状态"
    note = sufficiency.get("note") or ""
    card_rows = []
    for card in cards[:8]:
        href = _agent_rag_card_href(card, return_to)
        title = card.get("title") or card.get("evidence_id") or "证据"
        source_label = _agent_rag_source_label(card.get("source_type"))
        ids = " · ".join(
            item
            for item in [
                f"题目 {card.get('question_id')}" if card.get("question_id") else "",
                f"作答 {card.get('attempt_id')}" if card.get("attempt_id") else "",
                card.get("evidence_id") or "",
            ]
            if item
        )
        action = f'<a href="{esc(href)}">查看原文</a>' if href else ""
        card_rows.append(
            f"""
            <article class="agent-rag-card">
              <header><span>{esc(source_label)}</span>{action}</header>
              <strong>{esc(title)}</strong>
              {f"<small>{esc(ids)}</small>" if ids else ""}
              {f"<p>{esc(card.get('claim'))}</p>" if card.get("claim") else ""}
              {f"<blockquote>{esc(card.get('content'))}</blockquote>" if card.get("content") else ""}
            </article>"""
        )
    debug_rows = [
        ("路由", rag.get("rag_route") or ""),
        (
            "计划",
            " / ".join(
                str(item)
                for item in [
                    (rag.get("query_plan") or {}).get("action"),
                    (rag.get("query_plan") or {}).get("scope"),
                ]
                if item
            ),
        ),
        ("证据源", "、".join(str(item) for item in ((rag.get("query_plan") or {}).get("sources") or []))),
        ("证据范围", "仅当前题" if rag.get("current_attempt_only") else "可用历史/候选证据"),
        ("证据数量", str(rag.get("evidence_card_count") or len(cards))),
        ("检索策略", rag.get("retrieval_policy") or ""),
        ("允许引用", "、".join(str(item) for item in (rag.get("allowed_evidence_ids") or []))),
    ]
    debug_html = "".join(f"<div><dt>{esc(name)}</dt><dd>{esc(value)}</dd></div>" for name, value in debug_rows if value)
    return f"""
    <details class="agent-rag-panel agent-insight-disclosure">
      <summary>
        <span class="agent-insight-title"><strong>本次依据</strong><small>{esc(rag.get("rag_route") or "RAG")}</small></span>
        <span class="agent-rag-sufficiency {esc(level)}">{esc(label)}</span>
      </summary>
      <div class="agent-insight-content">
        {f'<p class="agent-rag-note">{esc(note)}</p>' if note else ""}
        {f'<div class="agent-rag-grid">{"".join(card_rows)}</div>' if card_rows else '<p class="muted">本轮没有可展示的证据卡。</p>'}
        <details class="agent-rag-debug">
          <summary>RAG 调试</summary>
          <dl>{debug_html}</dl>
        </details>
      </div>
    </details>"""


def render_agent_pending_status(steps):
    step_names = {step["tool_name"] for step in steps or []}
    phases = [
        ("classify_module", "理解问题"),
        ("load_user_context", "读取训练资料"),
        ("retrieve_candidates", "筛选上下文"),
        ("build_rag_context", "检索证据"),
        ("ChatOpenAI", "生成回复"),
    ]
    first_todo_seen = False
    chips = []
    for tool_name, label in phases:
        if tool_name in step_names:
            state = "done"
            prefix = "✓ "
        elif not first_todo_seen:
            state = "current"
            prefix = ""
            first_todo_seen = True
        else:
            state = "todo"
            prefix = ""
        chips.append(f'<span class="{state}">{esc(prefix + label)}</span>')
    return f"""
              <div class="agent-status-steps" aria-label="真实生成进度">
                {"".join(chips)}
              </div>"""


def render_agent_message_row(message, current_path, run_steps=None):
    is_pending = message["message_type"] == "pending"
    try:
        message_metadata = json.loads(message["metadata_json"] or "{}")
    except json.JSONDecodeError:
        message_metadata = {}
    pending_status = render_agent_pending_status(run_steps or []) if is_pending else ""
    run_link = (
        f"""
                <div class="agent-message-actions">
                  <a class="button small" href="/agent/runs/{message["run_id"]}">工具轨迹</a>
                </div>"""
        if message["run_id"]
        else ""
    )
    role_label = "我" if message["role"] == "user" else "AI 教练"
    time_label = "生成中" if is_pending else esc(format_beijing_time(message["created_at"]))
    body_html = (
        agent_response_html(message["content"], current_path)
        if message["role"] == "assistant"
        else f"<p>{esc(message['content'])}</p>"
    )
    rag_panel = (
        render_agent_rag_panel(message_metadata, current_path)
        if message["role"] == "assistant" and not is_pending
        else ""
    )
    dots = (
        '<div class="agent-thinking-dots" aria-label="生成中"><span></span><span></span><span></span></div>'
        if is_pending
        else ""
    )
    return f"""
            <article class="agent-message {esc(message["role"])}{" is-pending" if is_pending else ""}"{' data-agent-pending="1"' if is_pending else ""} data-agent-message-id="{message["id"]}">
              <header><strong>{role_label}</strong><span>{time_label}</span></header>
              <div class="report-body">{body_html}</div>
              {rag_panel}
              {pending_status}
              {dots}
              {run_link}
            </article>"""

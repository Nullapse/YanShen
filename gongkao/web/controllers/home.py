"""Home dashboard controller."""

from ..runtime import (
    BEIJING_TIMEZONE,
    build_module_score_statistics,
    build_training_statistics,
    connect,
    datetime,
    esc,
    format_beijing_time,
    format_duration,
    json,
    layout,
    local_url,
    parse_report_score,
    quote,
    re,
    recommended_timed_paper,
    timedelta,
)


class HomeController:
    def page_home(self):
        now = datetime.now(BEIJING_TIMEZONE)
        today_iso = now.strftime("%Y-%m-%d")

        def home_question_title(row, limit=58):
            if not row:
                return ""
            title = re.sub(r"\s+", " ", row["title"] or "").strip()
            if title and not re.fullmatch(r"第?\s*\d+\s*题", title):
                return title
            prompt = re.sub(r"\s+", " ", row["prompt"] or "").strip()
            return prompt[:limit] + ("…" if len(prompt) > limit else "") if prompt else (title or "申论练习")

        if now.hour < 5:
            greeting = "夜深了，先完成最重要的一步。"
        elif now.hour < 11:
            greeting = "早上好，先把今天的一题写好。"
        elif now.hour < 14:
            greeting = "中午好，留一点时间给今天的练习。"
        elif now.hour < 18:
            greeting = "下午好，继续推进今天的训练。"
        else:
            greeting = "晚上好，把今天的练习收个尾。"

        with connect(self.db_path) as conn:
            stats = build_training_statistics(conn)
            module_scores = build_module_score_statistics(conn, "first")
            today_attempts = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE date(created_at, '+8 hours') = ?",
                (today_iso,),
            ).fetchone()[0]
            today_reports = conn.execute(
                "SELECT COUNT(*) FROM grading_reports WHERE status = 'ok' AND date(created_at, '+8 hours') = ?",
                (today_iso,),
            ).fetchone()[0]
            latest_attempt = conn.execute(
                """
                SELECT a.id AS attempt_id, a.word_count, a.duration_seconds, a.created_at,
                       q.id AS question_id, q.title, q.prompt, q.year, q.region, q.exam_type,
                       q.question_type, q.word_limit, q.paper_id, q.paper_name, q.question_number,
                       (SELECT gr.id FROM grading_reports gr
                         WHERE gr.attempt_id = a.id AND gr.status = 'ok'
                         ORDER BY gr.id DESC LIMIT 1) AS latest_report_id
                  FROM attempts a
                  JOIN questions q ON q.id = a.question_id
              ORDER BY a.created_at DESC, a.id DESC
                 LIMIT 1
                """
            ).fetchone()
            latest_report = conn.execute(
                """
                SELECT gr.id AS report_id, gr.attempt_id, gr.created_at,
                       q.title, q.question_type
                  FROM grading_reports gr
                  JOIN attempts a ON a.id = gr.attempt_id
                  JOIN questions q ON q.id = a.question_id
                 WHERE gr.status = 'ok'
              ORDER BY gr.created_at DESC, gr.id DESC
                 LIMIT 1
                """
            ).fetchone()
            recommended = conn.execute(
                """
                SELECT q.id AS question_id, q.title, q.prompt, q.year, q.region, q.exam_type,
                       q.question_type, q.word_limit, q.paper_name, q.question_number,
                       COUNT(r.id) AS reference_count
                  FROM questions q
             LEFT JOIN reference_answers r ON r.question_id = q.id
                 WHERE NOT EXISTS (
                       SELECT 1 FROM attempts a WHERE a.question_id = q.id
                 )
              GROUP BY q.id
              ORDER BY q.year DESC, reference_count DESC, q.zhejiang_relevance DESC, q.id DESC
                 LIMIT 1
                """
            ).fetchone()
            if recommended is None:
                recommended = conn.execute(
                    """
                    SELECT q.id AS question_id, q.title, q.prompt, q.year, q.region, q.exam_type,
                           q.question_type, q.word_limit, q.paper_name, q.question_number,
                           COUNT(r.id) AS reference_count
                      FROM questions q
                 LEFT JOIN reference_answers r ON r.question_id = q.id
                  GROUP BY q.id
                  ORDER BY q.year DESC, reference_count DESC, q.id DESC
                     LIMIT 1
                    """
                ).fetchone()
            recommended_paper = recommended_timed_paper(
                conn,
                latest_attempt["paper_id"] if latest_attempt else None,
            )
            pending_grades = conn.execute(
                """
                SELECT COUNT(*) FROM attempts a
                 WHERE NOT EXISTS (
                       SELECT 1 FROM grading_reports gr
                        WHERE gr.attempt_id = a.id AND gr.status = 'ok'
                 )
                """
            ).fetchone()[0]
            recent_attempts = conn.execute(
                """
                SELECT a.id AS attempt_id, a.word_count, a.created_at,
                       q.title, q.prompt, q.year, q.region, q.exam_type, q.question_type,
                       gr.report_text
                  FROM attempts a
                  JOIN questions q ON q.id = a.question_id
             LEFT JOIN grading_reports gr
                    ON gr.id = (
                       SELECT gr2.id FROM grading_reports gr2
                        WHERE gr2.attempt_id = a.id AND gr2.status = 'ok'
                        ORDER BY gr2.id DESC LIMIT 1
                    )
              ORDER BY a.created_at DESC, a.id DESC
                 LIMIT 3
                """
            ).fetchall()
            unattempted_pool = conn.execute(
                """
                SELECT q.id AS question_id, q.title, q.prompt, q.year, q.region, q.exam_type, q.question_type, q.word_limit,
                       p.paper_name, q.question_number
                  FROM questions q
             LEFT JOIN papers p ON p.id = q.paper_id
                 WHERE NOT EXISTS (SELECT 1 FROM attempts a WHERE a.question_id = q.id)
              ORDER BY RANDOM()
                 LIMIT 20
                """
            ).fetchall()

        daily_counts = dict(stats["daily"])
        week_start = now.date() - timedelta(days=now.weekday())
        week_labels = ("一", "二", "三", "四", "五", "六", "日")
        week_cells = []
        week_total = 0
        for offset, label in enumerate(week_labels):
            day = week_start + timedelta(days=offset)
            count = daily_counts.get(day, 0)
            week_total += count
            classes = []
            if count:
                classes.append("done")
            if day == now.date():
                classes.append("today")
            class_html = f' class="{" ".join(classes)}"' if classes else ""
            week_cells.append(
                f'<span{class_html} title="{esc(day.isoformat())} · {count} 题"><b>{label}</b><i>{count if count else "·"}</i></span>'
            )
        sidebar_week_html = f"""
        <section class="home-week-card" aria-label="本周每天做题情况">
          <header><span>本周节奏</span><strong>{week_total}<small>题</small></strong></header>
          <div>{"".join(week_cells)}</div>
          <a href="/statistics">查看训练统计</a>
        </section>"""

        scored_modules = {
            item["name"]: item["average_score"]
            for item in module_scores["modules"]
            if item["average_score"] is not None
        }
        type_stats = stats["type_stats"]
        if scored_modules:
            weak_name, weak_value = min(scored_modules.items(), key=lambda item: item[1])
            insight_label = "近期均分"
            insight_copy = f"近期批改中，{weak_name}均分为 {weak_value:.0f}。建议下一次练习重点检查任务边界、要点层次和表达完整度。"
        else:
            weak_type = min(
                type_stats,
                key=lambda item: (item["attempts"], item["completion"]),
                default={"name": "归纳概括", "completion": 0},
            )
            weak_name = weak_type["name"]
            insight_label = "训练覆盖"
            insight_copy = f"还没有足够的批改数据形成稳定判断。建议先完成一轮{weak_name}练习，主页会随着训练记录逐步给出更准确的建议。"
        weak_href = f"/?question_type={quote(weak_name)}&work_status=unattempted"

        pool_data = [
            {
                "question_id": row["question_id"],
                "title": home_question_title(row),
                "year": row["year"],
                "region": row["region"],
                "exam_type": row["exam_type"],
                "question_type": row["question_type"] or "未分类",
                "word_limit": row["word_limit"] or "",
                "question_number": row["question_number"],
            }
            for row in unattempted_pool
        ]
        pool_json = json.dumps(pool_data, ensure_ascii=False)

        hero = latest_attempt or recommended
        if latest_attempt:
            hero_href = f"/attempts/{latest_attempt['attempt_id']}"
            hero_kicker = "上次练到这里"
            hero_action = "继续复盘" if latest_attempt["latest_report_id"] else "继续作答"
            hero_action_detail = "查看批改报告与复盘" if latest_attempt["latest_report_id"] else "返回未完成作答"
            hero_word_count = latest_attempt["word_count"] or 0
            hero_duration = format_duration(latest_attempt["duration_seconds"], "未计时")
        elif recommended:
            hero_href = f"/questions/{recommended['question_id']}"
            hero_kicker = "从这一题开始"
            hero_action = "开始作答"
            hero_action_detail = "进入答题页 · 草稿自动保存"
            hero_word_count = 0
            hero_duration = "尚未开始"
        else:
            hero_href = "/papers"
            hero_kicker = "建立你的训练节奏"
            hero_action = "浏览题库"
            hero_action_detail = "选择试卷并开始训练"
            hero_word_count = 0
            hero_duration = "尚未开始"

        word_limit_text = hero["word_limit"] if hero else ""
        word_limit_numbers = [int(value) for value in re.findall(r"\d+", word_limit_text or "")]
        word_limit = max(word_limit_numbers, default=0)
        hero_progress = min(100, round(hero_word_count * 100 / word_limit)) if word_limit else 0
        hero_title = home_question_title(hero) if hero else "从题库挑选一套试卷，开始第一次练习"
        hero_context = f"{hero['year']} {hero['region']} · {hero['exam_type']}" if hero else "研申 · 本地训练"
        hero_meta = []
        if hero:
            hero_meta.append(hero["question_type"] or "未分类")
            if hero["word_limit"]:
                hero_meta.append(hero["word_limit"])
            if hero["paper_name"]:
                hero_meta.append(f"第{hero['question_number'] or '?'}题")
        hero_meta_html = "".join(f"<span>{esc(value)}</span>" for value in hero_meta)
        hero_progress_copy = (
            f"<strong>{hero_word_count}<small> / {word_limit} 字</small></strong>"
            if word_limit
            else f"<strong>{hero_word_count}<small> 字</small></strong>"
        )

        daily_target = 2
        daily_done = min(daily_target, today_attempts)
        daily_percent = round(daily_done * 100 / daily_target)
        review_href = (
            f"/attempts/{latest_report['attempt_id']}#report-{latest_report['report_id']}"
            if latest_report
            else "/attempts?status=graded"
        )
        review_title = "复盘最近一次批改" if latest_report else "查看批改记录"
        review_note = (
            "今天已完成"
            if today_reports
            else ("从教练反馈中提炼一个改进点" if latest_report else "完成批改后会在这里生成任务")
        )
        practice_done = today_attempts > 0
        practice_note = f"今天已完成 {today_attempts} 次作答" if practice_done else "建议用时 20 分钟"
        plan_rows = [
            (review_title, review_href),
            (f"完成 1 道{hero['question_type'] if hero else '申论'}题", hero_href),
            (f"{weak_name}专项训练", weak_href),
            ("完成 1 道综合分析题", "/questions?question_type=%E7%BB%BC%E5%90%88%E5%88%86%E6%9E%90"),
            ("积累规范表述与金句", "/statistics"),
        ]

        def resolve_target_subtext(title, href):
            t = (title or "").strip()
            h = (href or "").strip()
            if "/attempts" in h or "复盘" in t or "批改" in t:
                return "跳转至 批改记录与复盘"
            if "/papers" in h or "套卷" in t or "模拟" in t:
                return "跳转至 真题与定时套卷"
            if "归纳概括" in t or "归纳概括" in h:
                return "跳转至 题库 · 归纳概括"
            if "综合分析" in t or "综合分析" in h:
                return "跳转至 题库 · 综合分析"
            if "提出对策" in t or "提出对策" in h:
                return "跳转至 题库 · 提出对策"
            if "贯彻执行" in t or "公文" in t or "贯彻" in h:
                return "跳转至 题库 · 贯彻执行"
            if "文章写作" in t or "大作文" in t or "文章" in h:
                return "跳转至 题库 · 大作文专项"
            if "/statistics" in h or "积累" in t or "金句" in t:
                return "跳转至 训练复盘与统计"
            return "跳转至 题库检索"

        plan_html = []
        plan_editor_rows = []
        for index, (title, href) in enumerate(plan_rows, 1):
            subtext = resolve_target_subtext(title, href)
            plan_html.append(
                f'<a class="home-plan-item" href="{esc(href)}" data-home-plan-item data-plan-index="{index - 1}"><i>{index}</i><div class="home-plan-item-content"><strong>{esc(title)}</strong><small class="home-plan-item-sub">{esc(subtext)}</small></div><b>➜</b></a>'
            )
            plan_editor_rows.append(f"""
              <label class="home-plan-editor-row">
                <span>任务 {index}</span>
                <input type="text" maxlength="48" value="{esc(title)}" data-home-plan-title="{index - 1}">
                <span>跳转到</span>
                <select data-home-plan-target="{index - 1}">
                  <option value="auto">自动智能跳转</option>
                  <option value="/questions">题库检索</option>
                  <option value="/papers">真题套卷</option>
                  <option value="/attempts">批改记录</option>
                </select>
                <button type="button" class="home-task-del" data-delete-task="{index - 1}">×</button>
              </label>""")

        score_rows = []
        for item in type_stats:
            if item["name"] in scored_modules:
                value = scored_modules[item["name"]]
                width = min(100, max(4, value))
                value_label = f"{value:.0f}"
            else:
                value = item["completion"]
                width = min(100, max(0, value))
                value_label = f"{value:.0f}%" if value else "—"
            score_rows.append(
                f'<div><span>{esc(item["name"])}</span><i><b style="width:{width}%"></b></i><strong>{esc(value_label)}</strong></div>'
            )

        recent_rows = []
        for attempt in recent_attempts:
            date_text = format_beijing_time(attempt["created_at"])
            date_parts = date_text[:10].split("-") if date_text else ["", "", ""]
            score = parse_report_score(attempt["report_text"] or "")
            score_html = (
                f'<span class="home-score good">{score:.0f}<small>分</small></span>'
                if score is not None
                else '<span class="home-score pending">—</span>'
            )
            state = "已批改" if score is not None else "待批改"
            state_class = "" if score is not None else " pending"
            recent_rows.append(f"""
              <a class="home-recent-row" href="/attempts/{attempt["attempt_id"]}">
                <time><strong>{esc(date_parts[2] if len(date_parts) > 2 else "")}</strong><span>{esc((date_parts[1] if len(date_parts) > 1 else "").lstrip("0"))}月</span></time>
                <span class="home-recent-copy"><strong>{esc(home_question_title(attempt, 46))}</strong><small>{esc(attempt["year"])} {esc(attempt["region"])} · {esc(attempt["question_type"])} · {attempt["word_count"]} 字</small></span>
                {score_html}<em class="home-row-status{state_class}">{state}</em><b>➜</b>
              </a>""")
        if not recent_rows:
            recent_rows.append(
                '<div class="home-empty"><strong>还没有练习记录</strong><p>完成第一道题后，这里会呈现你的作答、分数和复盘入口。</p><a href="/papers">去题库看看</a></div>'
            )

        quick_question_href = f"/questions/{recommended['question_id']}" if recommended else "/"
        quick_paper_href = (
            local_url(f"/papers/{recommended_paper['id']}", q=recommended_paper["next_question_id"])
            if recommended_paper
            else "/papers"
        )
        pending_copy = f"{pending_grades} 份作答待批改" if pending_grades else "查看批改记录与反馈"
        grading_href = "/attempts?status=ungraded" if pending_grades else "/attempts"
        last_practice = format_beijing_time(stats["last_practice"]) if stats["last_practice"] else "尚未开始"
        average_score = f"{stats['average_score']:.0f}" if stats["average_score"] is not None else "—"

        body = f"""
        <div class="home-dashboard" data-home-date="{today_iso}">
          <section class="home-welcome">
            <div><p class="eyebrow">YOUR STUDY DESK</p><h1>{esc(greeting)}</h1><p>最近练习：{esc(last_practice)} · 共 {stats["attempt_count"]} 条作答记录</p></div>
            <div class="home-welcome-side"><time data-home-clock>{now.strftime("%Y年%m月%d日 %H:%M")}</time></div>
          </section>

          <section class="home-focus-grid">
            <article class="home-continue-card" data-unattempted-pool="{esc(pool_json)}">
              <div class="home-ink-rings" aria-hidden="true"></div>
              <div class="home-continue-head"><span><i></i>{esc(hero_kicker)}</span><button class="home-text-link" type="button" data-home-switch-question>换一题</button></div>
              <div class="home-continue-body"><p>{esc(hero_context)}</p><h2>{esc(hero_title)}</h2><div>{hero_meta_html}</div></div>
              <div class="home-draft-progress"><div><span>作答进度</span>{hero_progress_copy}</div><i><b style="width:{hero_progress}%"></b></i><p>◷ 已用时 {esc(hero_duration)} · 内容自动保存在本地</p></div>
              <div class="home-continue-actions"><a class="home-primary-action" href="{esc(hero_href)}"><strong>{esc(hero_action)}</strong><small>{esc(hero_action_detail)}</small><b>➜</b></a></div>
            </article>

            <article class="home-plan-card">
              <div class="home-card-heading"><div><p class="eyebrow">TODAY'S PLAN</p><h2>今日训练</h2></div></div>
              <div class="home-plan-list">{"".join(plan_html)}</div>
              <button class="home-text-link" type="button" data-home-plan-open>调整今日计划 <span aria-hidden="true"></span></button>
            </article>
          </section>

          <section class="home-quick-section">
            <div class="home-section-title"><p class="eyebrow">QUICK START</p><h2>快速开始</h2></div>
            <div class="home-quick-grid">
              <a class="home-quick-card coral" href="{esc(quick_question_href)}"><i>↝</i><span><strong>随机一题</strong><small>从未练题目中抽取</small></span><b>➜</b></a>
              <a class="home-quick-card moss" href="{esc(quick_paper_href)}"><i>◷</i><span><strong>定时套卷</strong><small>{esc(recommended_paper["paper_name"] if recommended_paper else "模拟考场节奏")}</small></span><b>➜</b></a>
              <a class="home-quick-card ochre" href="{esc(grading_href)}"><i>✦</i><span><strong>智能批改</strong><small>{esc(pending_copy)}</small></span><b>➜</b></a>
              <a class="home-quick-card blue" href="/statistics"><i>↺</i><span><strong>训练复盘</strong><small>{stats["report_count"]} 份报告 · 均分 {average_score}</small></span><b>➜</b></a>
            </div>
          </section>

          <section class="home-lower-grid">
            <article class="home-recent-card">
              <div class="home-card-heading"><div><p class="eyebrow">RECENT PRACTICE</p><h2>最近练习</h2></div><a href="/attempts">全部 {stats["attempt_count"]} 条</a></div>
              <div class="home-recent-list">{"".join(recent_rows)}</div>
            </article>
            <article class="home-insight-card">
              <div class="home-card-heading"><div><p class="eyebrow">WEEKLY INSIGHT</p><h2>能力观察</h2></div><span class="home-ai-tag">✦ AI 教练</span></div>
              <p class="home-insight-mode">{esc(insight_label)} · 首次作答口径</p>
              <div class="home-skill-bars">{"".join(score_rows)}</div>
              <a class="home-insight-action" href="{esc(weak_href)}">开始专项训练</a>
            </article>
          </section>
        </div>
        <div class="home-plan-modal" data-home-plan-modal hidden>
          <section class="home-plan-dialog" role="dialog" aria-modal="true" aria-labelledby="home-plan-title">
            <header><div><p class="eyebrow">DAILY PLAN</p><h2 id="home-plan-title">调整今日计划</h2></div><button type="button" data-home-plan-close aria-label="关闭">×</button></header>
            <p>计划只保存在本机，并按日期分别记录。任务入口仍对应当前推荐内容。</p>
            <div class="home-task-pool">
              <span class="field-label">💡 推荐任务库 (点击可直接加入今日计划)：</span>
              <div class="home-task-chips">
                <button type="button" class="home-task-chip" data-add-pool-task="复盘批改反馈">✦ 复盘批改反馈</button>
                <button type="button" class="home-task-chip" data-add-pool-task="完成 1 道申论真题">📝 完成 1 道真题</button>
                <button type="button" class="home-task-chip" data-add-pool-task="完成 1 套定时模拟考">⏱️ 定时模拟考</button>
                <button type="button" class="home-task-chip" data-add-pool-task="归纳概括专项训练">🔍 归纳概括专项</button>
                <button type="button" class="home-task-chip" data-add-pool-task="提出对策专项训练">💡 提出对策训练</button>
                <button type="button" class="home-task-chip" data-add-pool-task="大作文提纲拟定">✒️ 大作文提纲</button>
                <button type="button" class="home-task-chip" data-add-pool-task="积累规范表述与金句">📚 积累规范表述</button>
              </div>
            </div>
            <form data-home-plan-form>
              <div class="home-plan-editor" data-home-plan-editor-container>
                {"".join(plan_editor_rows)}
              </div>
              <div class="home-plan-actions-row">
                <button type="button" class="button ghost small" data-home-add-row>➕ 添加自定义任务</button>
              </div>
              <footer><button class="button ghost" type="button" data-home-plan-reset>恢复推荐</button><div><button class="button ghost" type="button" data-home-plan-close>取消</button><button class="button primary" type="submit">保存计划</button></div></footer>
            </form>
          </section>
        </div>
        """
        self.send_html(layout("主页 - 研申", body, "home", sidebar_extra=sidebar_week_html))

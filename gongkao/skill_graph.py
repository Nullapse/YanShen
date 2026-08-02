import json

QUESTION_TYPE_MODULE = {
    "归纳概括": "summary",
    "综合分析": "analysis",
    "提出对策": "countermeasure",
    "公文写作": "document",
    "综合写作": "essay",
}

MODULE_PRIORITIES = {
    "summary": ("scope", "evidence", "merge", "abstraction", "wordlimit"),
    "analysis": ("object", "relation", "cause", "viewpoint", "evaluation"),
    "countermeasure": ("match", "cause", "actor", "action", "feasible"),
    "document": ("genre", "identity", "audience", "purpose", "format", "tone"),
    "essay": ("thesis", "title", "subpoints", "evidence", "analysis"),
    "overview": ("review", "taxonomy", "evidence", "timing", "rewrite", "transfer"),
}

ERROR_SKILLS = {
    "材料遗漏": ("summary:evidence", "summary:scope", "overview:review"),
    "采分点遗漏": ("summary:dimensions", "summary:merge", "overview:taxonomy"),
    "结构表达": ("analysis:structure", "essay:subpoints", "document:body"),
    "审题偏差": ("summary:scope", "summary:verbs", "document:purpose"),
    "语言概括": ("summary:abstraction", "summary:wordlimit", "essay:analysis"),
    "格式规范": ("document:genre", "document:format", "document:ending"),
    "对策针对性": ("countermeasure:match", "countermeasure:cause", "countermeasure:action"),
}


def _skill_key(card):
    parts = str(card.get("id") or "").split(":")
    if len(parts) >= 4:
        return f"{card['module']}:{parts[2]}"
    safe = str(card.get("skill") or card.get("title") or "skill").strip().replace(" ", "-")
    return f"{card.get('module') or 'overview'}:{safe}"


def rebuild_skill_graph(conn, knowledge_cards):
    conn.execute("DELETE FROM agent_skill_edges")
    conn.execute("DELETE FROM agent_skill_nodes")
    descriptions = {}
    for card in knowledge_cards:
        key = _skill_key(card)
        descriptions.setdefault(
            key,
            {
                "module": card.get("module") or "overview",
                "label": card.get("skill") or card.get("title") or key,
                "description": str(card.get("content") or "")[:240],
            },
        )
    for key, item in descriptions.items():
        conn.execute(
            "INSERT INTO agent_skill_nodes (skill_key, module, label, description) VALUES (?, ?, ?, ?)",
            (key, item["module"], item["label"], item["description"]),
        )
    for card in knowledge_cards:
        key = _skill_key(card)
        relation = {
            "counterexample": "reveals_error",
            "diagnostic": "diagnoses",
            "rewrite": "repairs",
            "example": "demonstrates",
        }.get(card.get("kind"), "teaches")
        conn.execute(
            """
            INSERT OR REPLACE INTO agent_skill_edges (
                source_type, source_key, skill_key, relation, weight, metadata_json, updated_at
            ) VALUES ('knowledge', ?, ?, ?, 1, ?, CURRENT_TIMESTAMP)
            """,
            (
                card["id"],
                key,
                relation,
                json.dumps({"kind": card.get("kind"), "difficulty": card.get("difficulty")}, ensure_ascii=False),
            ),
        )
    for row in conn.execute("SELECT id, question_code, question_type FROM questions"):
        module = QUESTION_TYPE_MODULE.get(row["question_type"])
        for index, skill in enumerate(MODULE_PRIORITIES.get(module, ())):
            key = f"{module}:{skill}"
            if key not in descriptions:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_skill_edges (
                    source_type, source_key, skill_key, relation, weight, metadata_json, updated_at
                ) VALUES ('question', ?, ?, 'practices', ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    str(row["id"]),
                    key,
                    round(1.0 - index * 0.08, 3),
                    json.dumps({"question_code": row["question_code"], "question_type": row["question_type"]}, ensure_ascii=False),
                ),
            )
    for problem_type, skills in ERROR_SKILLS.items():
        for index, key in enumerate(skills):
            if key not in descriptions:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_skill_edges (
                    source_type, source_key, skill_key, relation, weight, metadata_json, updated_at
                ) VALUES ('error', ?, ?, 'indicates_gap', ?, '{}', CURRENT_TIMESTAMP)
                """,
                (problem_type, key, round(1.0 - index * 0.12, 3)),
            )
    return {
        "skills": conn.execute("SELECT COUNT(*) FROM agent_skill_nodes").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM agent_skill_edges").fetchone()[0],
    }


def related_skill_context(conn, source_type, source_key, limit=12):
    return conn.execute(
        """
        SELECT n.*, e.relation, e.weight, e.metadata_json
          FROM agent_skill_edges e
          JOIN agent_skill_nodes n ON n.skill_key = e.skill_key
         WHERE e.source_type = ? AND e.source_key = ?
      ORDER BY e.weight DESC, n.skill_key
         LIMIT ?
        """,
        (source_type, str(source_key), max(1, int(limit))),
    ).fetchall()

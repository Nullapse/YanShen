import json
from collections import Counter

PROBLEM_PATTERNS = {
    "材料遗漏": ("材料", "遗漏", "漏", "未提及", "缺少", "信息点"),
    "采分点遗漏": ("采分", "要点", "点不全", "核心点", "得分点"),
    "结构表达": ("结构", "层次", "逻辑", "条理", "段落"),
    "审题偏差": ("审题", "题意", "对象", "任务", "范围"),
    "语言概括": ("概括", "表达", "啰嗦", "凝练", "准确"),
    "格式规范": ("格式", "称谓", "落款", "公文", "文种"),
    "对策针对性": ("对策", "针对", "可操作", "措施", "建议"),
}


def _evidence_ref(row: dict) -> str:
    source = row.get("source_type") or "context"
    source_id = row.get("source_id")
    if source == "knowledge":
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if metadata.get("knowledge_id"):
            return metadata["knowledge_id"]
    if row.get("attempt_id"):
        return f"{source}:attempt-{row['attempt_id']}"
    if row.get("question_id"):
        return f"{source}:question-{row['question_id']}"
    return f"{source}:{source_id}"


def problem_categories(chunks: list[dict]) -> list[dict]:
    counter = Counter()
    for chunk in chunks:
        text = f"{chunk.get('title', '')} {chunk.get('body', '')}"
        for label, keywords in PROBLEM_PATTERNS.items():
            if any(keyword in text for keyword in keywords):
                counter[label] += 1
    if not counter:
        counter["证据不足"] = 1
    return [
        {"name": name, "count": count}
        for name, count in counter.most_common(8)
    ]


def profile_snapshot(conn, module_id: str, question_type: str = "") -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
          FROM agent_weakness_profile
         WHERE module = ?
           AND question_type = ?
      ORDER BY severity DESC, frequency DESC, updated_at DESC
         LIMIT 8
        """,
        (module_id, question_type or ""),
    ).fetchall()
    profile = []
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
        except json.JSONDecodeError:
            evidence = []
        profile.append(
            {
                "problem_type": row["problem_type"],
                "frequency": row["frequency"],
                "severity": row["severity"],
                "evidence_refs": evidence,
                "last_seen_at": row["last_seen_at"],
            }
        )
    return profile


def update_weakness_profile(
    conn,
    module_id: str,
    question_type: str,
    categories: list[dict],
    chunks: list[dict],
) -> None:
    nowish = max(
        (chunk.get("created_at") for chunk in chunks if chunk.get("created_at")),
        default="CURRENT_TIMESTAMP",
    )
    evidence_by_problem = {}
    for category in categories:
        label = category["name"]
        keywords = PROBLEM_PATTERNS.get(label, (label,))
        matched = []
        for chunk in chunks:
            text = f"{chunk.get('title', '')} {chunk.get('body', '')}"
            if any(keyword in text for keyword in keywords):
                matched.append(
                    {
                        "ref": _evidence_ref(chunk),
                        "title": chunk.get("title") or "",
                        "source_type": chunk.get("source_type") or "",
                        "score": chunk.get("score"),
                    }
                )
            if len(matched) >= 3:
                break
        evidence_by_problem[label] = matched
    for category in categories:
        label = category["name"]
        count = int(category["count"])
        severity = min(1.0, round(count / max(3, len(chunks) or 1), 3))
        conn.execute(
            """
            INSERT INTO agent_weakness_profile (
                module, question_type, problem_type, frequency, severity,
                evidence_json, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(module, question_type, problem_type) DO UPDATE SET
                frequency = excluded.frequency,
                severity = excluded.severity,
                evidence_json = excluded.evidence_json,
                last_seen_at = excluded.last_seen_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                module_id,
                question_type or "",
                label,
                count,
                severity,
                json.dumps(
                    evidence_by_problem.get(label) or [],
                    ensure_ascii=False,
                ),
                nowish,
            ),
        )

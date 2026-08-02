def where_for_scope(scope: dict) -> tuple[list[str], list]:
    clauses = []
    params = []
    filters = scope.get("filters") or {}
    if filters.get("question_type"):
        clauses.append("question_type = ?")
        params.append(filters["question_type"])
    if filters.get("region"):
        clauses.append("region = ?")
        params.append(filters["region"])
    source_types = [
        str(value)
        for value in (scope.get("source_types") or [])
        if str(value)
    ]
    if source_types:
        placeholders = ", ".join("?" for _ in source_types)
        clauses.append(f"source_type IN ({placeholders})")
        params.extend(source_types)
    return clauses, params

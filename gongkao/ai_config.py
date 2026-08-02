"""Shared accessors for grading and AI coach model settings."""


def row_mapping(row):
    return dict(row) if row is not None else {}


def load_grading_settings(conn):
    return row_mapping(conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone())


def load_agent_settings(conn):
    return row_mapping(conn.execute("SELECT * FROM agent_ai_settings WHERE id = 1").fetchone())


def load_effective_agent_settings(conn):
    grading = load_grading_settings(conn)
    agent = load_agent_settings(conn)
    if not agent or agent.get("use_grading_api", 1):
        return {**grading, "mode": "api"}
    return {
        **grading,
        "mode": "api",
        "provider_name": agent.get("provider_name") or "DeepSeek",
        "api_base_url": agent.get("api_base_url") or "https://api.deepseek.com",
        "api_key": agent.get("api_key") or "",
        "api_key_env": agent.get("api_key_env") or "",
        "model": agent.get("model") or "deepseek-v4-pro",
        "temperature": agent.get("temperature", 0.2),
    }

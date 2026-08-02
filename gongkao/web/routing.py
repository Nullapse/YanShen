from dataclasses import dataclass
from typing import Any, Literal

ArgumentMode = Literal["none", "query", "path", "path_query", "path_extra"]


@dataclass(frozen=True)
class Route:
    handler_name: str
    exact: str = ""
    prefix: str = ""
    suffix: str = ""
    arguments: ArgumentMode = "none"
    extra: Any = None

    def matches(self, path: str) -> bool:
        if self.exact:
            return path == self.exact
        return (
            (not self.prefix or path.startswith(self.prefix))
            and (not self.suffix or path.endswith(self.suffix))
        )

    def dispatch(self, handler, path: str, query: dict) -> None:
        callback = getattr(handler, self.handler_name)
        if self.arguments == "query":
            callback(query)
        elif self.arguments == "path":
            callback(path)
        elif self.arguments == "path_query":
            callback(path, query)
        elif self.arguments == "path_extra":
            callback(path, self.extra)
        else:
            callback()


GET_ROUTE_GROUPS = {
    "home": (
        Route("page_home", exact="/home"),
    ),
    "library": (
        Route("page_index", exact="/", arguments="query"),
        Route("page_papers", exact="/papers", arguments="query"),
        Route("page_favorites", exact="/favorites", arguments="query"),
        Route("page_paper_detail", prefix="/papers/", arguments="path_query"),
        Route("page_question_by_code", prefix="/questions/by-code/", arguments="path_query"),
        Route("blank_package", prefix="/questions/", suffix="/package.md", arguments="path"),
        Route("page_question", prefix="/questions/", arguments="path_query"),
        Route("page_coverage", exact="/coverage", arguments="query"),
    ),
    "learning": (
        Route("page_notes", exact="/notes", arguments="query"),
        Route("page_statistics", exact="/statistics", arguments="query"),
        Route("attempt_package", prefix="/attempts/", suffix="/package.md", arguments="path"),
        Route("page_attempts", exact="/attempts", arguments="query"),
        Route("page_attempt_detail", prefix="/attempts/", arguments="path_query"),
    ),
    "agent": (
        Route("page_agent", exact="/agent", arguments="query"),
        Route("page_agent_setup", exact="/agent/setup", arguments="query"),
        Route("page_agent_memories", exact="/agent/memories"),
        Route("page_agent_evals", exact="/agent/evals"),
        Route("page_agent_knowledge", prefix="/agent/knowledge/", arguments="path_query"),
        Route(
            "handle_agent_conversation_status",
            prefix="/agent/conversations/",
            suffix="/status",
            arguments="path",
        ),
        Route("page_agent_conversation", prefix="/agent/conversations/", arguments="path"),
        Route("page_agent_run", prefix="/agent/runs/", arguments="path"),
    ),
    "grading": (
        Route(
            "handle_grading_job_status",
            prefix="/grading-jobs/",
            suffix="/status",
            arguments="path",
        ),
        Route("page_grading_report", prefix="/grading-reports/", arguments="path_query"),
    ),
    "settings": (
        Route("page_import", exact="/import"),
        Route("handle_settings_export", exact="/settings/export", arguments="query"),
        Route("handle_settings_index_status", exact="/settings/index-status"),
        Route("page_settings_local_records", exact="/settings/local-records"),
        Route("page_settings", exact="/settings"),
    ),
}


POST_ROUTE_GROUPS = {
    "settings": (
        Route("handle_import", exact="/import"),
        Route("handle_text_annotations", exact="/annotations"),
        Route("handle_settings_import", exact="/settings/import"),
        Route("handle_settings_local_records_clear", exact="/settings/local-records/clear"),
        Route("handle_settings_local_records_open", exact="/settings/local-records/open"),
        Route("handle_settings", exact="/settings"),
    ),
    "agent": (
        Route("handle_agent_run", exact="/agent/runs"),
        Route("handle_agent_conversation", exact="/agent/conversations"),
        Route(
            "handle_agent_message",
            prefix="/agent/conversations/",
            suffix="/messages",
            arguments="path",
        ),
        Route(
            "handle_agent_conversation_delete",
            prefix="/agent/conversations/",
            suffix="/delete",
            arguments="path",
        ),
        Route("handle_agent_setup_save", exact="/agent/setup/save"),
        Route("handle_agent_setup_test", exact="/agent/setup/test"),
        Route("handle_agent_memories_clear", exact="/agent/memories/clear"),
        Route(
            "handle_agent_memory_delete",
            prefix="/agent/memories/",
            suffix="/delete",
            arguments="path",
        ),
        Route("handle_agent_eval_run", exact="/agent/evals/run"),
        Route("handle_agent_eval_clear", exact="/agent/evals/clear"),
        Route(
            "handle_agent_eval_delete",
            prefix="/agent/evals/",
            suffix="/delete",
            arguments="path",
        ),
        Route(
            "handle_agent_feedback",
            prefix="/agent/runs/",
            suffix="/feedback",
            arguments="path",
        ),
    ),
    "grading": (
        Route(
            "handle_grading_report_feedback",
            prefix="/grading-reports/",
            suffix="/feedback",
            arguments="path",
        ),
        Route(
            "handle_rubric_rebuild",
            prefix="/questions/",
            suffix="/rubric/rebuild",
            arguments="path",
        ),
        Route(
            "handle_grading_references",
            prefix="/attempts/",
            suffix="/grading-references",
            arguments="path",
        ),
        Route("handle_save_report", prefix="/attempts/", suffix="/reports", arguments="path"),
        Route("handle_api_grade", prefix="/attempts/", suffix="/grade", arguments="path"),
    ),
    "practice": (
        Route(
            "handle_favorite",
            prefix="/questions/",
            suffix="/favorite",
            arguments="path_extra",
            extra="questions",
        ),
        Route(
            "handle_favorite",
            prefix="/papers/",
            suffix="/favorite",
            arguments="path_extra",
            extra="papers",
        ),
        Route("handle_attempt", prefix="/questions/", suffix="/attempts", arguments="path"),
        Route("handle_update_attempt", prefix="/attempts/", suffix="/update", arguments="path"),
        Route("handle_attempt_note", prefix="/attempts/", suffix="/note", arguments="path"),
        Route("handle_delete_attempt", prefix="/attempts/", suffix="/delete", arguments="path"),
    ),
}


def _dispatch(route_groups, handler, path: str, query: dict) -> bool:
    for routes in route_groups.values():
        for route in routes:
            if route.matches(path):
                route.dispatch(handler, path, query)
                return True
    return False


def dispatch_get(handler, path: str, query: dict) -> bool:
    return _dispatch(GET_ROUTE_GROUPS, handler, path, query)


def dispatch_post(handler, path: str) -> bool:
    return _dispatch(POST_ROUTE_GROUPS, handler, path, {})

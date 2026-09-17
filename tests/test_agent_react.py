import json
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from gongkao.agent_graph import AgentRunError, _graph_for
from gongkao.agent_rag import build_rag_context
from gongkao.agent_react import MAX_MODEL_CALLS, MAX_TOOL_CALLS, execute_tool, observation, tool_specs, validate_call


def call(name="load_user_context", args=None, identifier="c1"):
    return {"id": identifier, "name": name, "args": args or {}, "type": "tool_call"}


class ReactGraphTests(unittest.TestCase):
    def run_graph(self, responses, execute=None, **overrides):
        prepared = overrides.pop("prepared", ({}, {}))
        on_progress = overrides.pop("on_progress", None)
        chunks = overrides.pop("chunks", None)
        client = Mock()
        client.bind_tools.return_value = client
        client.invoke.side_effect = responses
        if chunks is not None:
            client.stream.side_effect = chunks
        state = {
            "db_path": "unused",
            "run_id": 1,
            "task_type": "diagnosis",
            "user_goal": "分析近期训练",
            "subject_ids": [],
        }
        state.update(overrides)
        with (
            patch(
                "gongkao.agent_graph._load_langgraph", return_value=(Mock(return_value=client), StateGraph, START, END)
            ),
            patch("gongkao.agent_graph.resolve_api_key", return_value="test"),
            patch("gongkao.agent_graph.connect", return_value=nullcontext(Mock())),
            patch("gongkao.agent_graph.complete_run") as complete,
            patch("gongkao.agent_graph._record_step"),
            patch("gongkao.agent_graph.prepare_selected_evidence", return_value=prepared),
            patch(
                "gongkao.agent_graph.execute_tool",
                side_effect=execute or (lambda s, n, a: {"user_context": {"summary": {"attempt_count": 2}}}),
            ) as tool,
        ):
            graph = _graph_for(
                {"model": "test", "api_base_url": "https://example.invalid/v1", "temperature": 0}, "unused",
                on_progress=on_progress,
            )
            result = graph.invoke(state)
        return result, client, tool, complete

    def test_model_selects_tool_observes_result_then_finishes(self):
        result, client, tool, complete = self.run_graph(
            [AIMessage(content="", tool_calls=[call()]), AIMessage(content="近期有两次作答，可进一步检查遗漏点。")]
        )
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["tool_calls_count"], 1)
        self.assertEqual(tool.call_args.args[1], "load_user_context")
        messages = client.invoke.call_args_list[1].args[0]
        observation_message = next(m for m in messages if isinstance(m, ToolMessage))
        self.assertEqual(observation_message.tool_call_id, "c1")
        self.assertEqual(json.loads(observation_message.content)["data"]["user_context"]["summary"]["attempt_count"], 2)
        complete.assert_called_once()

    def test_direct_reply_does_not_execute_tools(self):
        result, _, tool, _ = self.run_graph([AIMessage(content="请提供要复盘的题目。")])
        tool.assert_not_called()
        self.assertEqual(result["stop_reason"], "completed")

    def test_parallel_calls_are_correlated_and_executed_in_order(self):
        _, client, tool, _ = self.run_graph(
            [
                AIMessage(
                    content="", tool_calls=[call(identifier="a"), call("retrieve_candidates", {"limit": 2}, "b")]
                ),
                AIMessage(content="已整理建议。"),
            ]
        )
        messages = [m for m in client.invoke.call_args_list[1].args[0] if isinstance(m, ToolMessage)]
        self.assertEqual([m.tool_call_id for m in messages], ["a", "b"])
        self.assertEqual(tool.call_count, 2)

    def test_repeated_call_uses_cache_but_still_counts_budget(self):
        result, _, tool, _ = self.run_graph(
            [
                AIMessage(content="", tool_calls=[call(identifier="a")]),
                AIMessage(content="", tool_calls=[call(identifier="b")]),
                AIMessage(content="完成"),
            ]
        )
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(result["tool_calls_count"], 2)

    def test_invalid_arguments_return_observation_and_allow_recovery(self):
        result, client, _, _ = self.run_graph(
            [AIMessage(content="", tool_calls=[call("unknown")]), AIMessage(content="请补充作答。")],
            execute=validate_call,
        )
        message = next(m for m in client.invoke.call_args_list[1].args[0] if isinstance(m, ToolMessage))
        self.assertFalse(json.loads(message.content)["ok"])
        self.assertEqual(result["stop_reason"], "completed")

    def test_runaway_model_stops_at_budget_without_more_tools(self):
        result, client, tool, _ = self.run_graph(
            [AIMessage(content="", tool_calls=[call(identifier=f"c{i}")]) for i in range(MAX_MODEL_CALLS)]
        )
        self.assertEqual(client.invoke.call_count, 4)
        self.assertLess(client.invoke.call_count, MAX_MODEL_CALLS)
        self.assertEqual(result["stop_reason"], "budget")
        self.assertEqual(client.bind_tools.call_args.kwargs["tool_choice"], "none")

    def test_tool_budget_forces_final_round(self):
        result, client, _, _ = self.run_graph(
            [
                AIMessage(content="", tool_calls=[call(identifier=str(i)) for i in range(MAX_TOOL_CALLS)]),
                AIMessage(content="根据已取得的资料整理。"),
            ]
        )
        self.assertEqual(result["tool_calls_count"], 6)
        self.assertEqual(client.bind_tools.call_args.kwargs["tool_choice"], "none")

    def test_provider_error_does_not_leak_raw_message(self):
        with self.assertRaises(AgentRunError) as raised:
            self.run_graph([RuntimeError("secret-provider-body")])
        self.assertNotIn("secret-provider-body", str(raised.exception))

    def test_expired_deadline_never_calls_model(self):
        with patch("gongkao.agent_graph.MAX_RUN_SECONDS", -1):
            result, client, tool, complete = self.run_graph([])
        client.invoke.assert_not_called()
        tool.assert_not_called()
        self.assertEqual(result["stop_reason"], "budget")
        complete.assert_called_once()

    def test_invalid_tool_json_fails_without_execution(self):
        with self.assertRaises(AgentRunError):
            self.run_graph(
                [
                    AIMessage(
                        content="",
                        invalid_tool_calls=[{"name": "search_evidence", "args": "{", "id": "x", "error": "bad"}],
                    )
                ]
            )


class ToolContractTests(unittest.TestCase):
    def test_current_attempt_scope_does_not_offer_global_queries(self):
        state = {"subject_ids": [7], "context_plan": {"rag_query_plan": {"scope": "current_attempt"}}}
        names = {t["function"]["name"] for t in tool_specs(state)}
        self.assertEqual(names, {"review_current_attempts", "search_evidence", "read_source"})
        with self.assertRaises(ValueError):
            validate_call(state, "load_user_context", {})
        with self.assertRaises(ValueError):
            validate_call(state, "review_current_attempts", {"subject_ids": [8]})

    def test_notes_only_scope(self):
        state = {"subject_ids": [7], "context_plan": {"rag_query_plan": {"scope": "notes_only"}}}
        self.assertEqual([t["function"]["name"] for t in tool_specs(state)], ["search_evidence", "read_source"])

    def test_limits_and_unknown_fields_validated_before_database(self):
        for args in ({"limit": True}, {"limit": 9}, {"limit": 0}, {"limit": 2, "db_path": "other"}, {}):
            with self.assertRaises(ValueError):
                validate_call({}, "retrieve_candidates", args)
        for query in ("", " " * 20, "x" * 501, 1):
            with self.assertRaises(ValueError):
                validate_call({}, "search_evidence", {"query": query})

    def test_observation_is_bounded_valid_json(self):
        result = observation({"rows": [{"body": "材料" * 20000} for _ in range(100)]})
        self.assertLessEqual(len(result), 16000)
        self.assertTrue(json.loads(result)["truncated"])

    def test_search_query_cannot_change_task_scope(self):
        state = {
            "db_path": "unused",
            "task_type": "review",
            "subject_ids": [7],
            "user_goal": "复盘这道题",
            "context_plan": {"rag_query_plan": {"scope": "current_attempt"}},
        }
        with (
            patch("gongkao.agent_react.connect", return_value=nullcontext(Mock())),
            patch("gongkao.agent_react.build_rag_context", return_value={}) as build,
            patch("gongkao.agent_react.get_attempts_review_context", return_value={}),
        ):
            execute_tool(state, "search_evidence", {"query": "分析全部历史"})
        self.assertEqual(build.call_args.args[2], "复盘这道题")
        self.assertEqual(build.call_args.kwargs["retrieval_query"], "分析全部历史")
        self.assertEqual(build.call_args.kwargs["subject_ids"], [7])

    def test_query_rewrite_reaches_retriever_with_original_routing(self):
        with patch("gongkao.agent_rag.cards_from_knowledge", return_value=[]) as search:
            result = build_rag_context(Mock(), "diagnosis", "公文写作怎么写", retrieval_query="通知 格式")
        self.assertEqual(result["rag_route"], "writing_guidance")
        self.assertEqual(search.call_args.args[1], "通知 格式")


if __name__ == "__main__":
    unittest.main()

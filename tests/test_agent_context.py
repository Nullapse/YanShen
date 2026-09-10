import json
import unittest

from langchain_core.messages import AIMessage, ToolMessage

from gongkao.agent_context import ContextBudgetError, input_units, pack_messages, receipt, result_id, stable_prefix
from gongkao.agent_graph import _response_usage
from gongkao.agent_react import observation
from tests import test_agent_react
from tests.test_agent_react import call


class ContextTests(unittest.TestCase):
    def group(self, identifier, reference):
        return [
            AIMessage(content="", tool_calls=[call(identifier=identifier)]),
            ToolMessage(content=receipt(reference), tool_call_id=identifier),
        ]

    def test_duplicate_has_single_payload_and_surviving_reference_is_expanded(self):
        payload = observation({"body": "材料" * 500})
        ref = result_id(payload)
        transcript = self.group("a", ref) + self.group("b", ref)
        messages, _ = pack_messages([], transcript, [], {ref: payload})
        results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
        self.assertIn("data", results[0])
        self.assertNotIn("data", results[1])
        # Force removal of the first complete pair. The remaining reference must expand.
        messages, metrics = pack_messages([], transcript, [], {ref: payload}, limit=3500)
        self.assertEqual(metrics["dropped_tool_rounds"], 1)
        tools = [m for m in messages if isinstance(m, ToolMessage)]
        self.assertEqual(tools[0].tool_call_id, "b")
        self.assertIn("data", json.loads(tools[0].content))

    def test_missing_pair_or_store_is_rejected(self):
        with self.assertRaises(ContextBudgetError):
            pack_messages([], [AIMessage(content="", tool_calls=[call()])], [], {})
        with self.assertRaises(ContextBudgetError):
            pack_messages([], self.group("a", "gone"), [], {})

    def test_large_final_observation_shrinks_and_keeps_valid_json(self):
        payload = observation({"body": "材料" * 3000})
        ref = result_id(payload)
        messages, metrics = pack_messages([], self.group("a", ref), [], {ref: payload}, limit=2200)
        self.assertLessEqual(input_units(messages, []), 2200)
        self.assertIsNotNone(metrics["tool_text_limit"])
        self.assertTrue(json.loads(messages[-1].content)["truncated"])

    def test_schema_and_mandatory_task_consume_budget(self):
        with self.assertRaises(ContextBudgetError):
            stable_prefix([("system", "规则"), ("human", "中" * 12000)], [])
        with self.assertRaises(ContextBudgetError):
            pack_messages([("human", "问题")], [], [{"description": "x" * 40000}], {})

    def test_optional_history_pruned_without_losing_current_task(self):
        prefix, removed = stable_prefix(
            [("system", "规则"), ("human", "旧" * 4000), ("assistant", "答"), ("human", "当前问题")], []
        )
        self.assertGreater(removed, 0)
        self.assertEqual(prefix[-1].content, "当前问题")

    def test_usage_distinguishes_unknown_from_zero_and_counts_provider_hits(self):
        self.assertNotIn("cached_input_tokens", _response_usage(AIMessage(content="x")))
        msg = AIMessage(
            content="x",
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            response_metadata={"token_usage": {"prompt_tokens_details": {"cached_tokens": 80}}},
        )
        self.assertEqual(_response_usage(msg)["cache_hit_ratio"], 0.8)

    def test_nested_module_context_is_not_duplicated(self):
        module = {"marker": "unique-evidence"}
        data = json.loads(observation({"rag_context": {"module_context": module}, "module_context": module}))
        self.assertNotIn("module_context", data["data"]["rag_context"])
        self.assertEqual(data["data"]["module_context"], module)

    def test_real_graph_keeps_prefix_and_only_one_copy_of_tool_data(self):
        _, client, tool, _ = test_agent_react.ReactGraphTests().run_graph(
            [
                AIMessage(content="", tool_calls=[call(identifier="a")]),
                AIMessage(content="", tool_calls=[call(identifier="b")]),
                AIMessage(content="完成"),
            ],
            execute=lambda s, n, a: {"user_context": {"marker": "UNIQUE_DATA_MARKER"}},
        )
        first = client.invoke.call_args_list[0].args[0]
        second = client.invoke.call_args_list[1].args[0]
        third = client.invoke.call_args_list[2].args[0]
        self.assertEqual(first, second[: len(first)])
        self.assertEqual(second, third[: len(second)])
        self.assertEqual(sum(m.content.count("UNIQUE_DATA_MARKER") for m in third), 1)
        self.assertEqual(tool.call_count, 1)

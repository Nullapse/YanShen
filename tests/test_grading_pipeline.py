import unittest

from gongkao.grading_pipeline import (
    MAX_MODEL_CALLS,
    GradingErrorCategory,
    GradingRunState,
    classify_grading_error,
)


class GradingPipelineTest(unittest.TestCase):
    def test_model_call_budget_is_explicit_and_bounded(self):
        state = GradingRunState()
        for _ in range(MAX_MODEL_CALLS):
            state.reserve_model_call()
        self.assertEqual(state.api_calls, MAX_MODEL_CALLS)
        with self.assertRaises(RuntimeError):
            state.reserve_model_call()

    def test_validation_errors_have_a_stable_category(self):
        category = classify_grading_error(ValueError("rubric JSON 校验失败"))
        self.assertEqual(category, GradingErrorCategory.RESPONSE_VALIDATION)

    def test_data_errors_are_distinguished_from_internal_errors(self):
        self.assertEqual(
            classify_grading_error(RuntimeError("批改任务不存在")),
            GradingErrorCategory.DATA,
        )
        self.assertEqual(
            classify_grading_error(RuntimeError("unexpected state")),
            GradingErrorCategory.INTERNAL,
        )


if __name__ == "__main__":
    unittest.main()

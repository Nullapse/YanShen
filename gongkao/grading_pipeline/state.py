import time
from dataclasses import dataclass, field
from enum import StrEnum

ACTIVE_JOB_STATUSES = (
    "queued",
    "preparing",
    "building_rubric",
    "reusing_rubric",
    "retrieving",
    "grading",
    "validating",
    "repairing_answer",
)
MAX_MODEL_CALLS = 2


class GradingErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    MODEL_REQUEST = "model_request"
    RESPONSE_VALIDATION = "response_validation"
    DATA = "data"
    INTERNAL = "internal"


@dataclass
class GradingRunState:
    """Mutable state for one bounded grading pipeline execution."""

    started: float = field(default_factory=time.monotonic)
    raw_parts: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    api_calls: int = 0

    def reserve_model_call(self) -> None:
        if self.api_calls >= MAX_MODEL_CALLS:
            raise RuntimeError(f"批改模型调用已达到上限（{MAX_MODEL_CALLS} 次）")
        self.api_calls += 1

    def add_raw_response(self, raw: str) -> None:
        self.raw_parts.append(raw)

    def latency_ms(self) -> int:
        return round((time.monotonic() - self.started) * 1000)


def classify_grading_error(error: Exception) -> GradingErrorCategory:
    message = str(error).lower()
    if any(token in message for token in ("api key", "api_key", "未配置", "configuration")):
        return GradingErrorCategory.CONFIGURATION
    if any(token in message for token in ("timeout", "connection", "http", "request")):
        return GradingErrorCategory.MODEL_REQUEST
    if isinstance(error, (ValueError, TypeError)) or any(
        token in message for token in ("json", "响应", "校验", "rubric")
    ):
        return GradingErrorCategory.RESPONSE_VALIDATION
    if any(token in message for token in ("不存在", "database", "sqlite")):
        return GradingErrorCategory.DATA
    return GradingErrorCategory.INTERNAL

from typing import NotRequired, TypedDict


class GradingJobOptions(TypedDict):
    reference_ids: list[int]
    custom_reference_answer: str
    analogies: bool
    knowledge: bool
    history: bool
    deep_thinking: bool


class GradingEvidence(TypedDict, total=False):
    evidence_id: str
    role: str
    source_type: str
    source_id: int | str
    attempt_id: int
    title: str
    body: str
    confidence: float


class PointMatch(TypedDict):
    point_key: str
    status: str
    coverage_ratio: float
    answer_quote: str
    reason: str
    weight: NotRequired[float]
    importance: NotRequired[str]


class DimensionScore(TypedDict):
    dimension: str
    label: str
    max_score: float
    score: float
    reason: str
    display_max_score: NotRequired[float]
    display_score: NotRequired[float]


class GradingAnnotation(TypedDict):
    kind: str
    quote: str
    replacement: str
    reason: str
    point_key: str
    severity: str
    anchor: str


class GradingResult(TypedDict, total=False):
    schema_version: str
    score_status: str
    point_matches: list[PointMatch]
    dimension_scores: list[DimensionScore]
    annotations: list[GradingAnnotation]
    personalized_findings: list[dict]
    material_reading: list[str]
    optimization_suggestions: list[str]
    reference_fusion: str
    overall_summary: str
    revised_answer: str
    score: float
    display_score: float
    display_max_score: float | int
    content_score: float
    validation_errors: list[str]
    answer_snapshot: str

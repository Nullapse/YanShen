import json
import re
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from statistics import median

from .common import question_display_max_score

CALIBRATION_POLICY_VERSION = "exam-anchor-calibration-v1"
_SCORE_RE = re.compile(r"(\d{2}(?:\.\d+)?)\s*分", re.I)
_CONTEXT_RE = re.compile(r"实战|考场|申论|考生|选手|总分|状元|作答|回忆|答案", re.I)
_IDENTITY_NOISE_RE = re.compile(
    r"小红书|xhs|考生|选手|答案|实战|申论|考场|回忆|网友|作答|高分|大神|状元",
    re.I,
)


def parse_exam_score_label(organization):
    """Extract an explicitly labelled whole-paper score from a source name."""
    text = str(organization or "").strip()
    values = [float(value) for value in _SCORE_RE.findall(text)]
    values = [value for value in values if 60 <= value <= 100]
    if len(values) != 1 or not _CONTEXT_RE.search(text):
        return None
    return values[0]


def normalize_candidate_identity(organization):
    text = _SCORE_RE.sub("", str(organization or "")).casefold()
    text = _IDENTITY_NOISE_RE.sub("", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _answer_hash(value):
    compact = re.sub(r"\s+", "", str(value or ""))
    return sha256(compact.encode("utf-8")).hexdigest()


def build_exam_anchor_manifest(conn):
    """Build metadata-only calibration anchors; answer bodies never leave the DB."""
    questions = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT id, paper_id, paper_name, question_number, question_type, "
            "prompt, requirements, word_limit, title, original_text FROM questions"
        )
    }
    paper_questions = defaultdict(list)
    for question in questions.values():
        paper_questions[question["paper_id"]].append(question)

    grouped = defaultdict(dict)
    labels = defaultdict(set)
    for row in conn.execute(
        "SELECT id, question_id, organization, answer_text FROM reference_answers"
    ):
        question = questions.get(row["question_id"])
        if not question:
            continue
        score = parse_exam_score_label(row["organization"])
        if score is None:
            continue
        identity = normalize_candidate_identity(row["organization"])
        # Some sources are consistently named only "85.5分考场答案". They may
        # form an anchor only when that paper/score has exactly one answer for
        # every question; all incomplete or conflicting anonymous groups stay
        # ambiguous below.
        identity = identity or "__anonymous__"
        key = (question["paper_id"], identity, score)
        answer_key = (row["question_id"], _answer_hash(row["answer_text"]))
        grouped[key][answer_key] = row["id"]
        labels[key].add(str(row["organization"] or ""))

    anchors = []
    for (paper_id, identity, score), answers in grouped.items():
        answer_question_ids = {value[0] for value in answers}
        variants_by_question = Counter(value[0] for value in answers)
        all_questions = paper_questions.get(paper_id, [])
        all_question_ids = {value["id"] for value in all_questions}
        paper_max = round(sum(float(question_display_max_score(value)) for value in all_questions), 2)
        covered_max = round(
            sum(float(question_display_max_score(questions[value])) for value in answer_question_ids),
            2,
        )
        conflicting_variants = any(count > 1 for count in variants_by_question.values())
        complete = (
            not conflicting_variants
            and bool(all_question_ids)
            and answer_question_ids == all_question_ids
            and paper_max == 100
        )
        missing_identity = identity == "__anonymous__"
        classification = "complete_exam" if complete else (
            "ambiguous" if conflicting_variants or missing_identity else "partial_exam"
        )
        reason = (
            "conflicting_answer_variants"
            if conflicting_variants
            else ("candidate_identity_missing" if missing_identity and not complete else "")
        )
        anchors.append(
            {
                "anchor_id": sha256(f"{paper_id}:{identity}:{score:g}".encode()).hexdigest()[:16],
                "paper_id": paper_id,
                "paper_name": all_questions[0]["paper_name"] if all_questions else "",
                "candidate_key": identity if not missing_identity else "anonymous_complete_exam",
                "actual_score": score,
                "classification": classification,
                "reason": reason,
                "question_ids": sorted(answer_question_ids),
                "reference_ids": sorted(answers.values()),
                "covered_max_score": covered_max,
                "paper_max_score": paper_max,
                "source_labels": sorted(labels[(paper_id, identity, score)]),
            }
        )
    anchors.sort(key=lambda item: (str(item.get("classification")), int(item.get("paper_id") or 0), str(item.get("anchor_id") or "")))
    return {
        "schema_version": "exam-anchor-manifest-v1",
        "policy_version": CALIBRATION_POLICY_VERSION,
        "counts": dict(Counter(item["classification"] for item in anchors)),
        "anchors": anchors,
    }


def fit_high_score_calibration(residual_rows):
    """Fit a one-parameter correction and validate it by held-out paper."""
    rows = [
        row for row in residual_rows or []
        if row.get("classification") == "complete_exam"
        and row.get("paper_id") is not None
        and row.get("actual_score") is not None
        and row.get("predicted_score") is not None
    ]
    residuals = [float(row["actual_score"]) - float(row["predicted_score"]) for row in rows]
    paper_count = len({row["paper_id"] for row in rows})
    direction = max(Counter(1 if value > 0 else (-1 if value < 0 else 0) for value in residuals).values(), default=0)
    raw_offset = median(residuals) if residuals else 0.0
    shrunk_offset = max(-3.0, min(3.0, raw_offset * len(rows) / (len(rows) + 8))) if rows else 0.0

    baseline_mae = sum(abs(value) for value in residuals) / len(residuals) if residuals else 0.0
    held_out_errors = []
    for held_out_paper in {row["paper_id"] for row in rows}:
        training = [
            float(row["actual_score"]) - float(row["predicted_score"])
            for row in rows
            if row["paper_id"] != held_out_paper
        ]
        if not training:
            continue
        fold_offset = median(training) * len(training) / (len(training) + 8)
        fold_offset = max(-3.0, min(3.0, fold_offset))
        held_out_errors.extend(
            abs((float(row["actual_score"]) - float(row["predicted_score"])) - fold_offset)
            for row in rows
            if row["paper_id"] == held_out_paper
        )
    calibrated_mae = (
        sum(held_out_errors) / len(held_out_errors)
        if len(held_out_errors) == len(rows) and rows
        else baseline_mae
    )
    enabled = (
        len(rows) >= 4
        and paper_count >= 3
        and direction / len(rows) >= 0.75
        and calibrated_mae < baseline_mae
    )
    return {
        "policy_version": CALIBRATION_POLICY_VERSION,
        "enabled": enabled,
        "anchor_count": len(rows),
        "paper_count": paper_count,
        "offset": round(shrunk_offset, 3) if enabled else 0.0,
        "applicable_band": [65, 100],
        "full_effect_from": 75,
        "max_adjustment": 3.0,
        "baseline_mae": round(baseline_mae, 3),
        "calibrated_mae": round(calibrated_mae, 3),
        "validation_method": "leave_one_paper_out",
        "reason": "validated_high_score_offset" if enabled else "activation_gate_not_met",
    }


def load_calibration_policy(path=None):
    if path is None:
        path = Path(__file__).resolve().parents[2] / "knowledge" / "grading_calibration_v1.json"
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        policy = payload.get("policy") if isinstance(payload, dict) else None
        if isinstance(policy, dict) and policy.get("policy_version") == CALIBRATION_POLICY_VERSION:
            return policy
    except (OSError, ValueError, TypeError):
        pass
    return fit_high_score_calibration([])


def apply_score_calibration(score, policy=None):
    raw_score = round(float(score or 0), 1)
    policy = policy or load_calibration_policy()
    adjustment = 0.0
    if policy.get("enabled") and raw_score > 55:
        blend = min(1.0, max(0.0, (raw_score - 55) / 15))
        limit = abs(float(policy.get("max_adjustment") or 5.0))
        adjustment = max(-limit, min(limit, float(policy.get("offset") or 0))) * blend
    final_score = round(max(0.0, min(100.0, raw_score + adjustment)), 1)
    return final_score, {
        "policy_version": policy.get("policy_version") or CALIBRATION_POLICY_VERSION,
        "raw_score": raw_score,
        "adjustment": round(final_score - raw_score, 1),
        "anchor_count": int(policy.get("anchor_count") or 0),
        "applicable_band": policy.get("applicable_band") or [65, 100],
        "enabled": bool(policy.get("enabled")),
        "reason": policy.get("reason") or "activation_gate_not_met",
    }

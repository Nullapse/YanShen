import json

VALID_PARAGRAPH_ALIGNMENTS = {"left", "center", "right"}


def answer_paragraph_alignments(value, answer_text=""):
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        parsed = []
    if not isinstance(parsed, list):
        parsed = []
    line_count = max(1, str(answer_text or "").count("\n") + 1)
    alignments = [
        item if item in VALID_PARAGRAPH_ALIGNMENTS else "left"
        for item in parsed[:line_count]
    ]
    while alignments and alignments[-1] == "left":
        alignments.pop()
    return alignments


def normalize_answer_format_json(value, answer_text=""):
    return json.dumps(
        answer_paragraph_alignments(value, answer_text),
        ensure_ascii=False,
        separators=(",", ":"),
    )

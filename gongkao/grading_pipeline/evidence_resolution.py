import re
import unicodedata

_ELLIPSIS_RE = re.compile(r"(?:…{1,}|\.{3,})")


def _search_form(value):
    """Return punctuation-insensitive text plus an index map to the source."""
    normalized = []
    positions = []
    for index, char in enumerate(str(value or "")):
        for candidate in unicodedata.normalize("NFKC", char):
            if candidate.isspace() or unicodedata.category(candidate).startswith("P"):
                continue
            normalized.append(candidate.casefold())
            positions.append(index)
    return "".join(normalized), positions


def _span(answer_text, start, end, mode):
    text = str(answer_text or "")[start:end]
    return {
        "start": start,
        "end": end,
        "text": text,
        "mode": mode,
    }


def resolve_answer_evidence(quote, answer_text):
    """Resolve a model quote without turning formatting differences into misses."""
    quote = str(quote or "").strip()
    answer_text = str(answer_text or "")
    if not quote or not answer_text:
        return {"status": "unresolved", "quote": quote, "spans": []}

    exact_start = answer_text.find(quote)
    if exact_start >= 0:
        span = _span(answer_text, exact_start, exact_start + len(quote), "exact")
        return {"status": "resolved", "quote": span["text"], "spans": [span]}

    fragments = [value.strip() for value in _ELLIPSIS_RE.split(quote) if value.strip()]
    if len(fragments) > 1:
        spans = []
        cursor = 0
        for fragment in fragments:
            resolved = resolve_answer_evidence(fragment, answer_text[cursor:])
            if resolved["status"] != "resolved" or not resolved["spans"]:
                spans = []
                break
            for item in resolved["spans"]:
                item = dict(item)
                item["start"] += cursor
                item["end"] += cursor
                item["mode"] = "ordered_fragments"
                spans.append(item)
            cursor = spans[-1]["end"]
        if spans:
            return {
                "status": "resolved",
                "quote": "……".join(item["text"] for item in spans),
                "spans": spans,
            }

    answer_search, answer_positions = _search_form(answer_text)
    quote_search, _ = _search_form(quote)
    normalized_start = answer_search.find(quote_search) if quote_search else -1
    if normalized_start >= 0:
        source_start = answer_positions[normalized_start]
        source_end = answer_positions[normalized_start + len(quote_search) - 1] + 1
        span = _span(answer_text, source_start, source_end, "normalized")
        return {"status": "resolved", "quote": span["text"], "spans": [span]}

    return {"status": "unresolved", "quote": quote, "spans": []}

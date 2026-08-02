from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

KNOWLEDGE_SCHEMA_VERSION = "knowledge-card-v2"
VALID_MODULES = {"overview", "summary", "analysis", "countermeasure", "document", "essay"}
VALID_REVIEW_STATUSES = {"reviewed", "machine_reviewed", "draft"}


class KnowledgeSource(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    section: str = Field(min_length=1, max_length=160)
    license: str = Field(min_length=2, max_length=80)
    visibility: Literal["public", "private"] = "public"


class KnowledgeReview(BaseModel):
    status: Literal["reviewed", "machine_reviewed", "draft"]
    reviewer: str = Field(min_length=2, max_length=120)
    reviewed_at: str
    notes: str = Field(default="", max_length=300)

    @field_validator("reviewed_at")
    @classmethod
    def validate_date(cls, value):
        date.fromisoformat(value)
        return value


class KnowledgeCard(BaseModel):
    schema_version: Literal["knowledge-card-v2"] = KNOWLEDGE_SCHEMA_VERSION
    id: str = Field(pattern=r"^knowledge:[a-z0-9][a-z0-9:_-]+$")
    title: str = Field(min_length=4, max_length=120)
    module: str
    kind: str = Field(min_length=3, max_length=60)
    skill: str = Field(min_length=2, max_length=80)
    difficulty: int = Field(ge=1, le=5)
    tags: list[str] = Field(min_length=2, max_length=16)
    content: str = Field(min_length=40, max_length=1800)
    examples: list[str] = Field(min_length=1, max_length=6)
    counterexamples: list[str] = Field(min_length=1, max_length=6)
    pitfalls: list[str] = Field(min_length=1, max_length=6)
    applicable_when: list[str] = Field(min_length=1, max_length=6)
    not_applicable_when: list[str] = Field(min_length=1, max_length=6)
    source: KnowledgeSource
    review: KnowledgeReview
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("module")
    @classmethod
    def validate_module(cls, value):
        if value not in VALID_MODULES:
            raise ValueError(f"invalid module: {value}")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values):
        normalized = list(dict.fromkeys(" ".join(str(value).split()) for value in values if str(value).strip()))
        if len(normalized) < 2:
            raise ValueError("at least two unique tags are required")
        return normalized

    @model_validator(mode="after")
    def validate_hash(self):
        expected = compute_card_hash(self.model_dump(exclude={"content_hash"}))
        if self.content_hash != expected:
            raise ValueError(f"content_hash mismatch: expected {expected}")
        return self


def compute_card_hash(payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def finalize_card(payload):
    value = dict(payload)
    value.setdefault("schema_version", KNOWLEDGE_SCHEMA_VERSION)
    value["content_hash"] = compute_card_hash({key: item for key, item in value.items() if key != "content_hash"})
    return KnowledgeCard.model_validate(value).model_dump()


def knowledge_files(root):
    root = Path(root)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return sorted(root.glob("*.jsonl"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    names = list(manifest.get("public_files") or []) + list(manifest.get("private_files") or [])
    return [root / name for name in names if (root / name).exists()]


def load_knowledge_cards(root, include_private=True):
    cards = []
    seen = set()
    for path in knowledge_files(root):
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    card = KnowledgeCard.model_validate(item).model_dump()
                except Exception as exc:
                    raise ValueError(f"Invalid knowledge card at {path}:{line_number}: {exc}") from exc
                if card["id"] in seen:
                    raise ValueError(f"Duplicate knowledge id: {card['id']}")
                seen.add(card["id"])
                if not include_private and card["source"]["visibility"] != "public":
                    continue
                card["_source_file"] = path.name
                cards.append(card)
    return cards

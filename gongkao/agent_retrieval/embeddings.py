import re
from hashlib import blake2b
from math import sqrt

VECTOR_DIM = 128
FEATURE_HASH_MODEL = "feature-hash-v1"


def _hash_index(token: str) -> int:
    digest = blake2b(token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") % VECTOR_DIM


def tokenize(text: str) -> list[str]:
    text = str(text or "").lower()
    tokens = []
    for part in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_]+", text):
        tokens.append(part)
        if re.search(r"[\u4e00-\u9fff]", part):
            tokens.extend(part[index : index + 1] for index in range(len(part)))
            tokens.extend(
                part[index : index + 2]
                for index in range(max(0, len(part) - 1))
            )
            tokens.extend(
                part[index : index + 3]
                for index in range(max(0, len(part) - 2))
            )
    return [token for token in tokens if token.strip()]


def embed_text(text: str) -> tuple[list[float], float]:
    vector = [0.0] * VECTOR_DIM
    for token in tokenize(text):
        vector[_hash_index(token)] += 1.0
    norm = sqrt(sum(value * value for value in vector))
    if norm:
        vector = [round(value / norm, 6) for value in vector]
    return vector, norm


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    return sum(a * b for a, b in zip(left, right))

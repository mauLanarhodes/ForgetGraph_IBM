"""
Embedding helper.

Loads all-MiniLM-L6-v2 once at import time.
embed(texts) returns L2-normalised vectors as plain Python lists.
"""

from sentence_transformers import SentenceTransformer

_model = SentenceTransformer("all-MiniLM-L6-v2")


def embed(texts: list[str]) -> list[list[float]]:
    vectors = _model.encode(texts, normalize_embeddings=True)
    return [v.tolist() for v in vectors]

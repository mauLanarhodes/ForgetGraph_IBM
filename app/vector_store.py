"""
Chroma vector-store helpers.

Uses chromadb.HttpClient to talk to a running Chroma server.
The API computes embeddings itself; Chroma's built-in embedding function
is not used.
"""

import os
from typing import Any

import chromadb

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = "8001"
_DEFAULT_COLLECTION = "acme_chunks"


def _client() -> chromadb.HttpClient:
    host = os.environ.get("ACME_CHROMA_HOST", _DEFAULT_HOST)
    port = int(os.environ.get("ACME_CHROMA_PORT", _DEFAULT_PORT))
    return chromadb.HttpClient(host=host, port=port)


def _collection_name() -> str:
    return os.environ.get("ACME_COLLECTION", _DEFAULT_COLLECTION)


def get_collection():
    client = _client()
    return client.get_or_create_collection(
        name=_collection_name(),
        metadata={"hnsw:space": "cosine"},
    )


def add_chunks(chunks_data: list[dict[str, Any]]) -> None:
    """
    chunks_data is a list of dicts with keys:
        chunk_id, doc_id, owner_id, seq, text, embedding
    """
    collection = get_collection()
    ids = [c["chunk_id"] for c in chunks_data]
    documents = [c["text"] for c in chunks_data]
    embeddings = [c["embedding"] for c in chunks_data]
    metadatas = [
        {"doc_id": c["doc_id"], "owner_id": c["owner_id"], "seq": c["seq"]}
        for c in chunks_data
    ]
    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )


def query(owner_id: str, embedding: list[float], top_k: int) -> list[dict]:
    """
    Returns passages ranked by distance (ascending — closer is better).
    Each passage: {chunk_id, doc_id, text, distance}
    """
    collection = get_collection()
    results = collection.query(
        query_embeddings=[embedding],
        n_results=top_k,
        where={"owner_id": owner_id},
        include=["documents", "metadatas", "distances"],
    )
    passages = []
    ids = results["ids"][0]
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    dists = results["distances"][0]
    for chunk_id, text, meta, dist in zip(ids, docs, metas, dists):
        passages.append(
            {
                "chunk_id": chunk_id,
                "doc_id": meta["doc_id"],
                "text": text,
                "distance": dist,
            }
        )
    return passages


def delete_document_vectors(doc_id: str) -> None:
    collection = get_collection()
    collection.delete(where={"document_id": doc_id})


def recreate_collection() -> None:
    client = _client()
    name = _collection_name()
    try:
        client.delete_collection(name)
    except Exception:
        pass
    client.create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )

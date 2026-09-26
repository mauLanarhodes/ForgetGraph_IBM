"""
FastAPI application — Acme Notes reference app.

Wires together: db, chunking, embeddings, vector_store.

Logging: for every request prints  METHOD /path → STATUS  to stdout.
Note content is never logged.
"""

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import chunking, db, embeddings, vector_store

# ---------------------------------------------------------------------------
# Startup / lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.get_connection()
    db.init_schema(conn)
    conn.close()
    yield


app = FastAPI(title="Acme Notes", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Logging middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def log_requests(request: Request, call_next):
    response = await call_next(request)
    print(f"{request.method} {request.url.path} → {response.status_code}")
    return response


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

class DocumentIn(BaseModel):
    id: str
    owner_id: str
    title: str
    body: str


@app.post("/documents", status_code=201)
def create_document(doc: DocumentIn):
    conn = db.get_connection()
    try:
        # Check duplicate
        existing = conn.execute(
            "SELECT id FROM documents WHERE id = ?", (doc.id,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Document ID already exists")

        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO documents (id, owner_id, title, body, created_at) VALUES (?,?,?,?,?)",
            (doc.id, doc.owner_id, doc.title, doc.body, created_at),
        )

        chunk_texts = chunking.split_into_chunks(doc.body)
        chunk_rows = []
        chunks_data = []
        for seq, text in enumerate(chunk_texts):
            chunk_id = f"{doc.id}:{seq}"
            chunk_rows.append((chunk_id, doc.id, doc.owner_id, seq, text))
            chunks_data.append(
                {
                    "chunk_id": chunk_id,
                    "doc_id": doc.id,
                    "owner_id": doc.owner_id,
                    "seq": seq,
                    "text": text,
                    "embedding": None,  # filled below
                }
            )

        conn.executemany(
            "INSERT INTO chunks (id, doc_id, owner_id, seq, text) VALUES (?,?,?,?,?)",
            chunk_rows,
        )
        conn.commit()

        # Embed and add to Chroma
        texts = [c["text"] for c in chunks_data]
        vectors = embeddings.embed(texts)
        for c, v in zip(chunks_data, vectors):
            c["embedding"] = v
        vector_store.add_chunks(chunks_data)

        return {"id": doc.id, "chunks": len(chunk_texts)}
    finally:
        conn.close()


@app.get("/documents")
def list_documents(owner_id: str = Query(...)):
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, title FROM documents WHERE owner_id = ? ORDER BY created_at",
            (owner_id,),
        ).fetchall()
        return [{"id": r["id"], "title": r["title"]} for r in rows]
    finally:
        conn.close()


@app.get("/documents/{doc_id}")
def get_document(doc_id: str):
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT id, owner_id, title, body, created_at FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        return dict(row)
    finally:
        conn.close()


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str):
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")

        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.commit()
        vector_store.delete_document_vectors(doc_id)
        return {"deleted": True, "doc_id": doc_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

class QueryIn(BaseModel):
    owner_id: str
    question: str
    top_k: int = 3


@app.post("/query")
def query_notes(req: QueryIn):
    emb = embeddings.embed([req.question])[0]
    passages = vector_store.query(req.owner_id, emb, req.top_k)
    return {"passages": passages}


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.post("/admin/reindex")
def reindex():
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, doc_id, owner_id, seq, text FROM chunks"
        ).fetchall()
        chunks_data = [
            {
                "chunk_id": r["id"],
                "doc_id": r["doc_id"],
                "owner_id": r["owner_id"],
                "seq": r["seq"],
                "text": r["text"],
                "embedding": None,
            }
            for r in rows
        ]

        texts = [c["text"] for c in chunks_data]
        vectors = embeddings.embed(texts)
        for c, v in zip(chunks_data, vectors):
            c["embedding"] = v

        vector_store.recreate_collection()
        if chunks_data:
            vector_store.add_chunks(chunks_data)

        return {"vectors": len(chunks_data)}
    finally:
        conn.close()


_FIXTURE_PATH = Path(__file__).parent / "seed_data" / "synthetic_docs.json"


@app.post("/admin/reset")
def reset():
    if os.environ.get("ACME_FIXTURE_MODE") != "1":
        raise HTTPException(status_code=404, detail="Not found")

    fixture = json.loads(_FIXTURE_PATH.read_text())

    # Drop and recreate SQLite tables
    conn = db.get_connection()
    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute("DROP TABLE IF EXISTS documents")
    conn.execute("PRAGMA foreign_keys = ON")
    db.init_schema(conn)
    conn.close()

    # Recreate Chroma collection
    vector_store.recreate_collection()

    # Insert each fixture note via the same code path as POST /documents
    total_chunks = 0
    for note in fixture:
        doc = DocumentIn(
            id=note["id"],
            owner_id=note["owner_id"],
            title=note["title"],
            body=note["body"],
        )
        result = create_document(doc)
        total_chunks += result["chunks"]

    return {
        "documents": len(fixture),
        "chunks": total_chunks,
        "vectors": total_chunks,
    }

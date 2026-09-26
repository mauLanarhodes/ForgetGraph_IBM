# Acme Notes — Architecture

Reference application for the ForgetGraph demo. Acme Notes is a small note-taking assistant: a user saves notes, and the assistant answers questions by returning the most relevant passages from that user's own notes.

| | |
|---|---|
| Status | Current |
| Owner | Platform team (fictional) |
| Last reviewed | 2026-09-26 |

> **All data in this project is synthetic.** Users (`user-042`, `user-077`), notes, names and places are invented. No personal, client or social-media data is used anywhere.

## 1. Components

| Component | Technology | How it runs | Location |
|---|---|---|---|
| API service | Python 3.11+, FastAPI, uvicorn | `uvicorn app.main:app --port 8000` | `app/` |
| Relational store | SQLite | a file, opened by the API | `data/acme.db` |
| Vector store | Chroma server | `chroma run --path data/chroma --port 8001` | `data/chroma/` |
| Embedding model | sentence-transformers `all-MiniLM-L6-v2` | loaded once inside the API process | local model cache |

The API computes embeddings itself and passes them to Chroma explicitly; Chroma's built-in embedding function is not used. Everything runs locally, with no API keys and no network calls once the model has been downloaded.

Configuration (environment variables):

| Variable | Default | Purpose |
|---|---|---|
| `ACME_DB_PATH` | `data/acme.db` | SQLite file |
| `ACME_CHROMA_HOST` | `127.0.0.1` | Chroma server host |
| `ACME_CHROMA_PORT` | `8001` | Chroma server port |
| `ACME_COLLECTION` | `acme_chunks` | Chroma collection name |
| `ACME_FIXTURE_MODE` | unset | When `1`, enables `POST /admin/reset` |

## 2. Code layout

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI routes; wires the modules below together |
| `app/db.py` | `get_connection()` opens a new SQLite connection per request; `init_schema()` creates the tables. All SQL goes through this module. |
| `app/chunking.py` | `split_into_chunks(body) -> list[str]` |
| `app/embeddings.py` | Loads the model once; `embed(texts) -> list[list[float]]`, normalized |
| `app/vector_store.py` | Chroma client and helpers: `add_chunks()`, `query()`, `delete_document_vectors(doc_id)`, `recreate_collection()` |
| `app/seed_data/synthetic_docs.json` | Fixture notes (section 7) |

## 3. Data model

### SQLite (`data/acme.db`)

```sql
CREATE TABLE IF NOT EXISTS documents (
  id         TEXT PRIMARY KEY,   -- e.g. 'doc-003'
  owner_id   TEXT NOT NULL,      -- e.g. 'user-042'
  title      TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at TEXT NOT NULL       -- ISO 8601, UTC
);

CREATE TABLE IF NOT EXISTS chunks (
  id       TEXT PRIMARY KEY,     -- '{doc_id}:{seq}', e.g. 'doc-003:1'
  doc_id   TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,        -- copied from the note so the index can be rebuilt from this table alone
  seq      INTEGER NOT NULL,     -- 0-based position within the note
  text     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);
```

The `chunks` table is the source of truth for chunk text. The vector index is derived from it and can be rebuilt from it at any time (section 4.4).

### Chroma (`acme_chunks` collection)

One record per chunk, using cosine distance:

| Field | Value |
|---|---|
| id | the chunk id, e.g. `doc-003:1` |
| document | the chunk text |
| embedding | `all-MiniLM-L6-v2` vector of the chunk text |
| metadata | `{"doc_id": "doc-003", "owner_id": "user-042", "seq": 1}` |

## 4. Flows

### 4.1 Save a note — `POST /documents`

Request: `{"id": "doc-003", "owner_id": "user-042", "title": "...", "body": "..."}`. The caller supplies note IDs so fixture IDs stay stable. Returns `201 {"id": "doc-003", "chunks": 3}`, or `409` if the ID already exists.

1. Insert the `documents` row.
2. Split the body into chunks: split on blank lines into paragraphs, then pack consecutive paragraphs (joined by a blank line) into chunks of at most 600 characters. A single paragraph longer than 600 characters becomes its own chunk.
3. Insert one `chunks` row per chunk.
4. Embed the chunk texts and add them to `acme_chunks` with the ids and metadata in section 3.

### 4.2 Ask a question — `POST /query`

Request: `{"owner_id": "user-042", "question": "...", "top_k": 3}`; `top_k` is optional and defaults to 3. Returns `200 {"passages": [{"chunk_id", "doc_id", "text", "distance"}]}` in rank order.

1. Embed the question.
2. Query `acme_chunks` for the `top_k` nearest records, filtered to `owner_id`.
3. Return the passages verbatim. There is no text-generation step: the assistant quotes passages directly.

### 4.3 Delete a note — `DELETE /documents/{doc_id}`

Returns `200 {"deleted": true, "doc_id": "doc-003"}`, or `404` if the note doesn't exist.

1. Delete the `documents` row. Its `chunks` rows are removed automatically by the foreign key's `ON DELETE CASCADE`.
2. Delete the note's vectors from `acme_chunks` using the metadata filter `doc_id == {doc_id}`.

### 4.4 Rebuild the index — `POST /admin/reindex`

Used when the embedding model changes. Deletes and recreates `acme_chunks`, then embeds and adds every row in `chunks`. It reads the `chunks` table alone, which is why `owner_id` is stored on each chunk. Returns `200 {"vectors": n}`.

### 4.5 Reset the fixture — `POST /admin/reset`

Available only when `ACME_FIXTURE_MODE=1`; otherwise returns `404`. Drops `chunks`, then `documents`, recreates both, deletes and recreates `acme_chunks`, then saves every note in `app/seed_data/synthetic_docs.json` through the same code path as `POST /documents`. Returns `200 {"documents": n, "chunks": n, "vectors": n}`.

### 4.6 Other endpoints

- `GET /documents?owner_id=user-042` returns `[{"id", "title"}]` for that owner.
- `GET /documents/{doc_id}` returns the note, or `404`.
- `GET /health` returns `{"ok": true}`.

There is no authentication. This is a local fixture and must not be exposed on a network.

## 5. Where a note's data lives

| Location | What it holds | Written by | Removed on delete by |
|---|---|---|---|
| SQLite `documents` | title, body, owner | `POST /documents` | the delete handler (4.3, step 1) |
| SQLite `chunks` | chunk text, owner, position | `POST /documents` | `ON DELETE CASCADE` from `documents` |
| Chroma `acme_chunks` | chunk text, embedding, metadata | `POST /documents`, `POST /admin/reindex` | the delete handler (4.3, step 2) |
| API process memory | nothing is kept between requests | — | — |
| Application logs (stdout) | method, path and status code only; never note content | every request | not applicable |

## 6. Deletion guarantee

When `DELETE /documents/{id}` returns 200, no content from that note remains in any location in section 5, and `POST /query` can no longer return it.

The guarantee covers logical deletion only. It does not cover:

- backups or snapshots (none are configured for this fixture);
- physical remnants on disk: unless SQLite's `secure_delete` is on, deleted rows can persist in free pages until `VACUUM`, and the vector store's internal files may retain data until its own compaction;
- copies outside this system, such as downloads or screenshots.

## 7. Fixture data

`app/seed_data/synthetic_docs.json` is a JSON array of notes in this shape:

```json
{
  "id": "doc-003",
  "owner_id": "user-042",
  "title": "Autumn offsite plan",
  "body": "…three paragraphs separated by \n\n…",
  "canary": "FG-CANARY-3F9K",
  "probe_question": "What is the approved budget for the autumn offsite?"
}
```

`canary` and `probe_question` are test metadata, and the API ignores them. Each note's body contains its canary exactly once, so any copy of that passage can be found by exact search; `probe_question` is a question that should retrieve the note.

| id | owner | title | canary |
|---|---|---|---|
| doc-001 | user-042 | Weekly team sync notes | FG-CANARY-1A7Q |
| doc-002 | user-042 | Tidewater release checklist | FG-CANARY-2C4M |
| doc-003 | user-042 | Autumn offsite plan | FG-CANARY-3F9K |
| doc-004 | user-077 | Kitchen renovation ideas | FG-CANARY-4H2D |
| doc-005 | user-042 | Travel budget guidelines | FG-CANARY-5J8R |
| doc-006 | user-077 | Book club reading list | FG-CANARY-6K3T |

Every body is two to four short paragraphs of invented content with no real people, companies or places. `doc-005` also discusses budgets, so a budget question still retrieves something when `doc-003` is absent.

`doc-003` is the default demonstration record. Its body is these three paragraphs, joined by blank lines:

> The Harbor Street team will hold its autumn offsite on 14 and 15 October at Thistlewick Lodge, two hours north of the city. Twelve people are attending. Day one covers the roadmap for the Tidewater release and a retrospective on the spring launch. Day two is reserved for the hiring plan and a workshop on on-call rotations, with an early finish so people can travel home before dark.
>
> The approved budget for the offsite is 18,400 credits, covering the venue, two catered lunches, one group dinner and a shuttle from the office. Any spend above the budget needs sign-off from the finance lead. Reference code FG-CANARY-3F9K appears on every invoice for this event so finance can reconcile the charges.
>
> Open questions: whether to book a second shuttle for the return trip, whether the Tidewater demo will be ready for day one, and who will take notes during the hiring session. Decisions will be recorded in the team channel after the planning call on 7 October. This note is synthetic test data for the Acme Notes reference app and describes no real people, places or events.

With the chunking rule in 4.1, `doc-003` produces three chunks (`doc-003:0` to `doc-003:2`), and its canary is in `doc-003:1`.

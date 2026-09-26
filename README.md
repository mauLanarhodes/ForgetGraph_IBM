# Acme Notes

Reference application for the ForgetGraph demo.  A small note-taking assistant: users save notes and the assistant answers questions by returning the most relevant passages from that user's own notes.

All data in this project is **synthetic**.  No personal, client or social-media data is used anywhere.

See [`docs/architecture.md`](docs/architecture.md) for the full specification.

---

## Run the reference app

### Prerequisites

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1. Start the Chroma vector store

```bash
chroma run --path data/chroma --port 8001
```

### 2. Start the API (fixture mode enabled)

In a second terminal:

```bash
source .venv/bin/activate
ACME_FIXTURE_MODE=1 uvicorn app.main:app --port 8000
```

### 3. Seed the database

```bash
curl -s -X POST http://127.0.0.1:8000/admin/reset | python3 -m json.tool
```

Expected response:

```json
{
  "documents": 6,
  "chunks": 18,
  "vectors": 18
}
```

(Exact chunk count depends on note bodies; the document count is always 6.)

### 4. Try a query

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"owner_id": "user-042", "question": "What is the approved budget for the autumn offsite?"}' \
  | python3 -m json.tool
```

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ACME_DB_PATH` | `data/acme.db` | SQLite file path |
| `ACME_CHROMA_HOST` | `127.0.0.1` | Chroma server host |
| `ACME_CHROMA_PORT` | `8001` | Chroma server port |
| `ACME_COLLECTION` | `acme_chunks` | Chroma collection name |
| `ACME_FIXTURE_MODE` | *(unset)* | Set to `1` to enable `POST /admin/reset` |

---

## Run the smoke test

```bash
source .venv/bin/activate
python scripts/smoke_test.py
```

The first run downloads the embedding model (~90 MB) and may take a few minutes.

---

## Inspector

The ForgetGraph inspector checks whether a record and its canary have been fully removed from every store. It is read-only (except `reset` and experiments) and independent of the app under test.

### CLI commands

Reset the fixture to its seed data:
```bash
source .venv/bin/activate
python -m inspector.cli reset
```

Inspect a record (read-only):
```bash
python -m inspector.cli inspect doc-003
```

Run a full deletion experiment (resets, deletes, and re-inspects):
```bash
python -m inspector.cli experiment doc-003
```

Each command prints JSON. An experiment also writes its evidence to `reports/runs/{run_id}.json`. Settings come from `FG_*` environment variables; see the configuration table in `.bob/skills/trace-deletion/inspector-contract.md`.

### Run the tests

```bash
source .venv/bin/activate
python -m pytest -q
```

The tests build their own stores in temporary directories and use a fake app client, so they never touch the running app, `data/` or `reports/`.

### Register the MCP server with Bob

Copy `.bob/mcp.example.json` to `.bob/mcp.json` and replace each `/ABSOLUTE/PATH/forgetgraph` with the absolute path to this repository (`.bob/mcp.json` is gitignored). Then restart the `forgetgraph` server from Bob's MCP settings; restart it again after changing anything in `inspector/`.

The server exposes three tools: `reset_fixture`, `inspect_record`, and `run_deletion_experiment`. Only `inspect_record` is in `alwaysAllow`, which takes effect only when MCP auto-approval is on in Bob's Auto-Approve settings. The other two stay on manual approval because they change data.


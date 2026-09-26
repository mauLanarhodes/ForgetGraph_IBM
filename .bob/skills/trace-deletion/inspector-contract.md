# ForgetGraph inspector — contract

Supporting file for the `trace-deletion` skill, and the specification for `inspector/`. The inspector is deterministic Python with no model calls. It observes; it never judges.

## Principles

- **Independent of the app under test.** It doesn't import app code or reuse the app's key names, filters or helper functions. An inspector that shared the app's assumptions would inherit the app's bugs.
- **Read-only**, except `reset_fixture` and the single delete call inside `run_deletion_experiment`.
- **Logical checks only.** It reads through SQL and the Chroma API, and never opens database files as raw bytes: deleted rows can linger in free pages, which would produce matches that say nothing about the app's delete path.
- **Honest scope.** Every result carries a `not_checked` list.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FG_APP_URL` | `http://127.0.0.1:8000` | Base URL of the app under test |
| `FG_DATA_DIR` | `data` | Root for database discovery and the file sweep |
| `FG_EXCLUDE` | `data/chroma` | Comma-separated paths skipped by discovery and the sweep |
| `FG_CHROMA_HOST` | `127.0.0.1` | Chroma server host |
| `FG_CHROMA_PORT` | `8001` | Chroma server port |
| `FG_SEED_FILE` | `app/seed_data/synthetic_docs.json` | Source of canaries, owners and probe questions |
| `FG_RUNS_DIR` | `reports/runs` | Where experiment evidence is written |
| `FG_SETTLE_SECONDS` | `1.0` | Wait after the delete before inspecting again |
| `FG_HEALTH_TIMEOUT` | `30` | Seconds to wait for the app's health check |

Before any tool touches the app, it polls `GET {FG_APP_URL}/health` for up to `FG_HEALTH_TIMEOUT` seconds, so a server that is still reloading produces a wait rather than a false result.

## What counts as a match

For a record ID `R` with canary `C`:

- **keyed match**: a column value, Chroma metadata value or item ID equal to `R`, or an ID that starts with `R:`;
- **content match**: any text field containing `C`.

Report both kinds. Counts are rows or items, not individual column matches. Each hit records the location, the row or item ID, the rule that matched, and an excerpt of at most 80 characters around the match. The canary appears in only one paragraph of a record, so the record's other chunks are found by keyed matches.

## Discovery

- **SQLite**: every `*.db`, `*.sqlite` and `*.sqlite3` file under `FG_DATA_DIR`, excluding `FG_EXCLUDE`. Open read-only (`file:{path}?mode=ro` with `uri=True`) with a fresh connection per call, and don't change any database settings. Scan every table listed in `sqlite_master` and every column, compared as text. Don't rely on declared foreign keys or on the app's queries.
- **Chroma**: every collection on the server. Fetch all items with their documents and metadatas (a full scan, which is fine at fixture size) and match on the item ID, every metadata value and the document text. Don't assume metadata key names.
- **Files**: every `.json`, `.jsonl`, `.ndjson`, `.log`, `.txt`, `.csv` and `.md` file under `FG_DATA_DIR`, excluding `FG_EXCLUDE`. Content matches only.
- **Retrieval probe**: `POST {FG_APP_URL}/query` with `{"owner_id": <owner from the seed file>, "question": <probe_question>, "top_k": 5}`. It's a hit if any passage has `doc_id` equal to `R` or text containing `C`.

## Tools

The descriptions below are what Bob sees when choosing a tool; keep them as written.

### `reset_fixture()`

> Resets the local fixture app to its seed data by calling POST /admin/reset. Use it before an experiment to start from a known state. It changes data, so use it only on the synthetic fixture. Returns the counts of documents, chunks and vectors after the reset.

### `inspect_record(record_id, canary=None, probe_question=None)`

> Read-only. Searches every reachable store (all SQLite tables, all Chroma collections and text files in the data directory) and the app's /query endpoint for one record, by exact ID and by its canary string. canary and probe_question default to the values in the seed file. Returns every hit with its location, plus the list of places that were not checked.

### `run_deletion_experiment(record_id, reset_first=True)`

> Runs one deletion experiment on a synthetic record: resets the fixture unless reset_first is false, inspects, deletes the record through DELETE /documents/{record_id}, waits briefly, and inspects again. Returns the before and after results, the delete response and a verdict of clean, residuals or invalid. Writes the full evidence to reports/runs/{run_id}.json.

Verdicts:

- `clean`: found before the delete, the delete returned 2xx, zero hits afterward, and the probe no longer returns the record;
- `residuals`: any hit after the delete, or the probe still returns the record;
- `invalid`: the record wasn't found before the delete, the delete returned a non-2xx status, or the app or Chroma couldn't be reached.

## Output format

`inspect_record` returns JSON like this, shown for `doc-003` straight after a reset:

```json
{
  "record_id": "doc-003",
  "canary": "FG-CANARY-3F9K",
  "checked_at": "2026-09-28T15:04:05Z",
  "locations": [
    {"store": "sqlite", "location": "data/acme.db#documents", "keyed": 1, "content": 1, "hits": []},
    {"store": "sqlite", "location": "data/acme.db#chunks", "keyed": 3, "content": 1, "hits": []},
    {"store": "chroma", "location": "acme_chunks", "keyed": 3, "content": 1, "hits": []}
  ],
  "files": [],
  "retrieval_probe": {
    "question": "What is the approved budget for the autumn offsite?",
    "returned_record": true,
    "returned_canary": true
  },
  "totals": {"locations_with_hits": 3, "keyed": 7, "content": 3},
  "errors": [],
  "not_checked": [
    "Backups and snapshots",
    "Physical remnants in database files (free pages, WAL): checks are logical, not forensic",
    "Chroma internal storage (logs, index segments): checked through the API only",
    "Files outside FG_DATA_DIR or inside FG_EXCLUDE",
    "In-process caches: visible only through the retrieval probe",
    "Third-party services and model weights"
  ]
}
```

`hits` is abbreviated here; in real output it holds one entry per matching row or item. Every discovered location is listed, including those with zero hits, so "checked and clean" can be told apart from "not checked".

## CLI

The same functions from a terminal, printing the same JSON. Use it for rehearsals so they don't spend Bobcoins:

```
python -m inspector.cli reset
python -m inspector.cli inspect doc-003
python -m inspector.cli experiment doc-003
```

## Register with Bob

Project-level `.bob/mcp.json`, with absolute paths (on Windows the interpreter is `.venv\Scripts\python.exe`):

```json
{
  "mcpServers": {
    "forgetgraph": {
      "command": "/ABSOLUTE/PATH/forgetgraph/.venv/bin/python",
      "args": ["-m", "inspector.mcp_server"],
      "cwd": "/ABSOLUTE/PATH/forgetgraph",
      "env": {
        "FG_APP_URL": "http://127.0.0.1:8000",
        "FG_CHROMA_PORT": "8001"
      },
      "alwaysAllow": ["inspect_record"]
    }
  }
}
```

`alwaysAllow` takes effect only when MCP auto-approval is switched on in Bob's Auto-Approve settings. Keep `reset_fixture` and `run_deletion_experiment` on manual approval, because they change data.

The paths are machine-specific, so keep this file out of git: add `.bob/mcp.json` to `.gitignore` and commit a copy with placeholder paths as `.bob/mcp.example.json`.

An experiment can wait up to 30 seconds for the app, and Bob's default MCP timeout is 1 minute, so raise this server's timeout to 3 minutes in Bob's MCP settings.

## Build notes

- Write the server in Python in this repo, not in Bob's default MCP folder. Use version 1.x of the official Python MCP SDK (`FastMCP` from `mcp.server.fastmcp`) over stdio, and pin `mcp<2`: version 2 renamed `FastMCP` and changed other APIs.
- Layout: `inspector/core.py` (discovery, matching, experiment, verdict), `inspector/cli.py`, `inspector/mcp_server.py`. The CLI and the MCP server are thin wrappers around `core.py`.
- In the server process, nothing may write to stdout except the MCP protocol. Log to stderr.
- Tools never raise. An unreachable app or Chroma, an unreadable file or a record missing from the seed file becomes an entry in `errors` or `not_checked`.
- Create the Chroma client with `anonymized_telemetry=False`.
- Make the Chroma client and the app's HTTP calls injectable, so tests can supply their own.
- Unit-test the inspector against temporary stores it creates itself. Don't write tests that assert how the app's delete path currently behaves.

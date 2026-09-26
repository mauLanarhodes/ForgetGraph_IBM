"""
tests/test_inspector.py

Every test builds its own stores under pytest's tmp_path: a SQLite file, an
in-process chromadb.PersistentClient and text files. The app is a FakeApp
object or a closed local port. No test touches the running app, data/ or reports/.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import chromadb
import pytest
from chromadb.config import Settings

from inspector import core

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / ".bob" / "skills" / "trace-deletion" / "inspector-contract.md"

RECORD = "doc-003"
CANARY = "FG-CANARY-3F9K"
OWNER = "user-042"
PARAGRAPHS = [
    "The team meets at the lodge on day one.",
    f"The approved budget is 18,400 credits. Reference code {CANARY} is on every invoice.",
    "Open questions remain about the shuttle.",
]
SEED = [
    {
        "id": RECORD,
        "owner_id": OWNER,
        "title": "Autumn offsite plan",
        "body": "\n\n".join(PARAGRAPHS),
        "canary": CANARY,
        "probe_question": "What is the approved budget for the autumn offsite?",
    },
    {
        "id": "doc-0031",
        "owner_id": OWNER,
        "title": "Offsite follow-up",
        "body": "A note whose ID only starts like doc-003.",
        "canary": "FG-CANARY-9Z9Z",
        "probe_question": "What happened after the offsite?",
    },
]
NO_HITS = {"locations_with_hits": 0, "keyed": 0, "content": 0}


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Point every FG_* setting at tmp_path and at closed ports."""
    data = tmp_path / "data"
    data.mkdir()
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(SEED), encoding="utf-8")
    values = {
        "FG_APP_URL": f"http://127.0.0.1:{_closed_port()}",
        "FG_DATA_DIR": str(data),
        "FG_EXCLUDE": str(data / "excluded"),
        "FG_CHROMA_HOST": "127.0.0.1",
        "FG_CHROMA_PORT": str(_closed_port()),
        "FG_SEED_FILE": str(seed),
        "FG_RUNS_DIR": str(tmp_path / "runs"),
        "FG_SETTLE_SECONDS": "0",
        "FG_HEALTH_TIMEOUT": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.fixture
def data(env) -> Path:
    return Path(env["FG_DATA_DIR"])


@pytest.fixture
def chroma(tmp_path):
    return chromadb.PersistentClient(
        path=str(tmp_path / "chroma"), settings=Settings(anonymized_telemetry=False)
    )


def build_acme(data: Path, chroma, notes=SEED) -> None:
    """Stores shaped like the app's: documents, chunks, and one vector per chunk."""
    with closing(sqlite3.connect(data / "acme.db")) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE documents (id TEXT PRIMARY KEY, owner_id TEXT, title TEXT, body TEXT, created_at TEXT);
            CREATE TABLE chunks (id TEXT PRIMARY KEY, doc_id TEXT, owner_id TEXT, seq INTEGER, text TEXT);
            """
        )
        collection = chroma.get_or_create_collection("acme_chunks", embedding_function=None)
        for note in notes:
            conn.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
                (note["id"], note["owner_id"], note["title"], note["body"], "2026-09-26T00:00:00Z"),
            )
            for seq, text in enumerate(note["body"].split("\n\n")):
                chunk_id = f"{note['id']}:{seq}"
                conn.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                    (chunk_id, note["id"], note["owner_id"], seq, text),
                )
                collection.add(
                    ids=[chunk_id],
                    documents=[text],
                    embeddings=[[1.0, float(seq)]],
                    metadatas=[{"doc_id": note["id"], "owner_id": note["owner_id"], "seq": seq}],
                )


@pytest.fixture
def acme(data, chroma) -> Path:
    build_acme(data, chroma)
    return data


class FakeApp:
    """Stands in for the app's HTTP API, acting on the test's own stores."""

    def __init__(self, data: Path, chroma, *, healthy=True, delete_status=None,
                 leave_chunks=False, leave_vectors=False, stale_cache=False):
        self.data = data
        self.chroma = chroma
        self.healthy = healthy
        self.delete_status = delete_status
        self.leave_chunks = leave_chunks
        self.leave_vectors = leave_vectors
        self.stale_cache = stale_cache
        self.cache: dict[str, list] = {}
        self.calls: list = []

    def wait_healthy(self):
        return self.healthy

    def reset(self):
        self.calls.append("reset")
        (self.data / "acme.db").unlink(missing_ok=True)
        if any(c.name == "acme_chunks" for c in self.chroma.list_collections()):
            self.chroma.delete_collection("acme_chunks")
        build_acme(self.data, self.chroma)
        chunks = sum(len(n["body"].split("\n\n")) for n in SEED)
        return 200, {"documents": len(SEED), "chunks": chunks, "vectors": chunks}

    def query(self, owner_id, question, top_k):
        self.calls.append("query")
        if not any(c.name == "acme_chunks" for c in self.chroma.list_collections()):
            return 200, {"passages": []}
        got = self.chroma.get_collection("acme_chunks").get(
            where={"owner_id": owner_id}, include=["documents", "metadatas"]
        )
        passages = [
            {"chunk_id": i, "doc_id": m["doc_id"], "text": d, "distance": 0.5}
            for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])
        ][:top_k]
        if self.stale_cache:  # the first answer is served forever
            passages = self.cache.setdefault(question, passages)
        return 200, {"passages": passages}

    def delete(self, record_id):
        self.calls.append(("delete", record_id))
        if self.delete_status is not None:
            return self.delete_status, {"detail": "forced by the test"}
        with closing(sqlite3.connect(self.data / "acme.db")) as conn, conn:
            if not conn.execute("DELETE FROM documents WHERE id = ?", (record_id,)).rowcount:
                return 404, {"detail": "Not Found"}
            if not self.leave_chunks:
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (record_id,))
        if not self.leave_vectors:
            self.chroma.get_collection("acme_chunks").delete(where={"doc_id": record_id})
        return 200, {"deleted": True, "doc_id": record_id}


def inspect(data: Path, chroma, **kwargs) -> dict:
    return core.inspect_record(RECORD, app=FakeApp(data, chroma), chroma=chroma, **kwargs)


def location(result: dict, suffix: str) -> dict:
    return next(loc for loc in result["locations"] + result["files"] if loc["location"].endswith(suffix))


def hit_locations(result: dict) -> list[str]:
    return [
        loc["location"]
        for loc in result["locations"] + result["files"]
        if loc.get("keyed") or loc["content"]
    ]


# ---------------------------------------------------------------------------
# 1. Keyed matches
# ---------------------------------------------------------------------------

def test_keyed_matches_exact_id_and_prefix_but_not_a_longer_id(data, chroma):
    values = ["doc-003", "doc-003:7", "doc-0031", "doc-0031:0", "xdoc-003", "doc-003-draft", "DOC-003"]
    with closing(sqlite3.connect(data / "keys.db")) as conn, conn:
        conn.execute("CREATE TABLE refs (ref TEXT)")
        conn.executemany("INSERT INTO refs VALUES (?)", [(v,) for v in values])
    chroma.get_or_create_collection("keyed_items", embedding_function=None).add(
        ids=["doc-003", "doc-003:2", "doc-0031", "doc-0031:2"], embeddings=[[1.0, 0.0]] * 4
    )

    result = inspect(data, chroma)

    refs = location(result, "keys.db#refs")
    assert (refs["keyed"], refs["content"]) == (2, 0)
    assert sorted(h["matches"][0]["excerpt"] for h in refs["hits"]) == ["doc-003", "doc-003:7"]
    items = location(result, "keyed_items")
    assert (items["keyed"], items["content"]) == (2, 0)
    assert sorted(h["id"] for h in items["hits"]) == ["doc-003", "doc-003:2"]
    assert all(h["rules"] == ["keyed"] for h in refs["hits"] + items["hits"])


# ---------------------------------------------------------------------------
# 2. Content matches
# ---------------------------------------------------------------------------

def test_content_matches_count_rows_once_and_survive_odd_column_types(data, chroma):
    long_body = "x" * 300 + CANARY + "y" * 300
    with closing(sqlite3.connect(data / "notes.db")) as conn, conn:
        conn.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, ref TEXT, title TEXT, body TEXT, size INTEGER, raw BLOB)"
        )
        conn.executemany(
            "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "other", f"Title with {CANARY}", long_body, 10, None),  # two columns, one row
                (2, RECORD, "Plain title", f"Body with {CANARY}", 20, None),  # keyed and content
                (3, "other", "Plain", "Plain", 30, CANARY.encode()),  # canary inside a BLOB
                (4, "other", None, None, None, b"\x00\xff\xfe"),  # NULLs and binary BLOB
                (5, "other", "Plain", "Plain", 50, None),  # no hit
            ],
        )

    notes = location(inspect(data, chroma), "notes.db#notes")

    assert "error" not in notes
    assert (notes["keyed"], notes["content"]) == (1, 3)
    by_id = {h["id"]: h for h in notes["hits"]}
    assert set(by_id) == {"1", "2", "3"}
    assert by_id["1"]["rules"] == ["content"]
    assert [m["field"] for m in by_id["1"]["matches"]] == ["title", "body"]
    assert by_id["2"]["rules"] == ["keyed", "content"]
    assert [m["field"] for m in by_id["3"]["matches"]] == ["raw"]
    for hit in notes["hits"]:
        for match in hit["matches"]:
            assert len(match["excerpt"]) <= 80
        assert hit["location"] == notes["location"]
    assert CANARY in by_id["1"]["matches"][1]["excerpt"]


# ---------------------------------------------------------------------------
# 3. Discovery
# ---------------------------------------------------------------------------

def _make_db(path: Path, *tables_with_rows: tuple[str, list[str]], view: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        for table, rows in tables_with_rows:
            conn.execute(f"CREATE TABLE {table} (val TEXT)")
            conn.executemany(f"INSERT INTO {table} VALUES (?)", [(r,) for r in rows])
        if view:
            conn.execute(f"CREATE VIEW everything AS SELECT * FROM {tables_with_rows[0][0]}")
            conn.execute(f"CREATE INDEX idx_val ON {tables_with_rows[0][0]}(val)")


def test_discovery_finds_nested_databases_and_listed_files_only(data, chroma, monkeypatch):
    monkeypatch.setenv("FG_EXCLUDE", f" {data / 'excluded'} , {data / 'archive'} ")
    _make_db(data / "top.db", ("hits", [RECORD]), ("empty", ["unrelated"]), view=True)
    _make_db(data / "a" / "b" / "nested.sqlite", ("hits", [f"{RECORD}:1"]))
    _make_db(data / "a" / "deeper.sqlite3", ("hits", [f"text {CANARY}"]))
    _make_db(data / "excluded" / "hidden.db", ("hits", [RECORD]))
    (data / "archive").mkdir()
    (data / "archive" / "old.log").write_text(CANARY, encoding="utf-8")
    files = data / "files"
    files.mkdir()
    listed = [".json", ".jsonl", ".ndjson", ".log", ".txt", ".csv", ".md"]
    for suffix in listed:
        (files / f"note{suffix}").write_text(f"first line\nsaw {CANARY} here\n", encoding="utf-8")
    (files / "clean.txt").write_text("nothing to see", encoding="utf-8")
    for suffix in [".py", ".html", ".yaml", ".bak", ".db-wal"]:
        (files / f"other{suffix}").write_text(CANARY, encoding="utf-8")

    result = inspect(data, chroma)

    def rel(loc: dict) -> str:
        return Path(loc["location"]).relative_to(data).as_posix()

    sqlite = {rel(loc): loc for loc in result["locations"] if loc["store"] == "sqlite"}
    assert sorted(sqlite) == [
        "a/b/nested.sqlite#hits",
        "a/deeper.sqlite3#hits",
        "top.db#empty",
        "top.db#hits",
    ]
    assert (sqlite["top.db#empty"]["keyed"], sqlite["top.db#empty"]["content"]) == (0, 0)
    assert sqlite["a/deeper.sqlite3#hits"]["content"] == 1
    swept = {rel(f): f for f in result["files"]}
    assert sorted(swept) == sorted([f"files/note{s}" for s in listed] + ["files/clean.txt"])
    assert swept["files/clean.txt"]["content"] == 0
    assert all(swept[f"files/note{s}"]["hits"][0]["id"] == "line 2" for s in listed)
    assert result["totals"] == {"locations_with_hits": 3 + len(listed), "keyed": 2, "content": 1 + len(listed)}
    assert "errors" not in result


# ---------------------------------------------------------------------------
# 4. Read-only
# ---------------------------------------------------------------------------

def test_inspect_leaves_the_sqlite_file_byte_identical(acme, chroma):
    db = acme / "acme.db"
    before = db.read_bytes()
    listing = sorted(os.listdir(acme))

    result = inspect(acme, chroma)

    assert result["totals"]["keyed"] > 0
    assert db.read_bytes() == before
    assert sorted(os.listdir(acme)) == listing  # no journal, -wal or -shm left behind


# ---------------------------------------------------------------------------
# 5. Chroma
# ---------------------------------------------------------------------------

def test_chroma_scans_every_collection_by_id_metadata_and_document(data, chroma):
    chroma.get_or_create_collection("items", embedding_function=None).add(
        ids=["doc-003:0", "vec-1", "vec-2", "vec-3", "vec-4", "vec-5"],
        embeddings=[[1.0, 0.0]] * 6,
        documents=["plain", "plain", f"text with {CANARY}", "plain", "plain", "plain"],
        # source_ref, summary and related are key names the Acme app doesn't use.
        metadatas=[
            {"source_ref": "none"},
            {"source_ref": RECORD},
            {"source_ref": "doc-999"},
            {"summary": f"quotes {CANARY}"},
            {"source_ref": "doc-0031"},
            {"related": ["doc-777", f"{RECORD}:1"]},
        ],
    )
    chroma.get_or_create_collection("other_items", embedding_function=None).add(
        ids=["unrelated"], embeddings=[[0.0, 1.0]], documents=["nothing"], metadatas=[{"source_ref": "doc-999"}]
    )
    chroma.get_or_create_collection("empty_items", embedding_function=None)

    result = inspect(data, chroma)

    collections = {loc["location"]: loc for loc in result["locations"] if loc["store"] == "chroma"}
    assert sorted(collections) == ["empty_items", "items", "other_items"]
    items = collections["items"]
    assert (items["keyed"], items["content"]) == (3, 2)
    fields = {h["id"]: [(m["field"], m["rule"]) for m in h["matches"]] for h in items["hits"]}
    assert fields == {
        "doc-003:0": [("id", "keyed")],
        "vec-1": [("metadata.source_ref", "keyed")],
        "vec-2": [("document", "content")],
        "vec-3": [("metadata.summary", "content")],
        "vec-5": [("metadata.related[1]", "keyed")],
    }
    for name in ["other_items", "empty_items"]:
        assert (collections[name]["keyed"], collections[name]["content"]) == (0, 0)


# ---------------------------------------------------------------------------
# 6. Verdicts
# ---------------------------------------------------------------------------

def test_verdict_clean(acme, chroma):
    result = core.run_deletion_experiment(RECORD, reset_first=False, app=FakeApp(acme, chroma), chroma=chroma)

    assert result["verdict"] == "clean", result["reasons"]
    assert result["before"]["totals"] == {"locations_with_hits": 3, "keyed": 7, "content": 3}
    assert result["before"]["retrieval_probe"]["returned_record"] is True
    assert result["before"]["retrieval_probe"]["owner_id"] == OWNER
    assert result["delete"]["status"] == 200
    assert result["after"]["totals"] == NO_HITS
    assert result["after"]["retrieval_probe"]["returned_record"] is False


@pytest.mark.parametrize(
    "leave, residual, probe_returns",
    [("chunks", "acme.db#chunks", False), ("vectors", "acme_chunks", True)],
)
def test_verdict_residuals_from_a_store(acme, chroma, leave, residual, probe_returns):
    app = FakeApp(acme, chroma, **{f"leave_{leave}": True})

    result = core.run_deletion_experiment(RECORD, reset_first=False, app=app, chroma=chroma)

    assert result["verdict"] == "residuals", result["reasons"]
    assert [loc.rsplit("/", 1)[-1] for loc in hit_locations(result["after"])] == [residual]
    assert (location(result["after"], residual)["keyed"], location(result["after"], residual)["content"]) == (3, 1)
    assert result["after"]["retrieval_probe"]["returned_record"] is probe_returns


def test_verdict_residuals_from_the_probe_alone(acme, chroma):
    app = FakeApp(acme, chroma, stale_cache=True)

    result = core.run_deletion_experiment(RECORD, reset_first=False, app=app, chroma=chroma)

    assert result["verdict"] == "residuals", result["reasons"]
    assert result["after"]["totals"] == NO_HITS
    assert result["after"]["retrieval_probe"]["returned_record"] is True


def test_verdict_invalid_when_the_record_is_missing_before_the_delete(data, chroma):
    build_acme(data, chroma, notes=[n for n in SEED if n["id"] != RECORD])
    app = FakeApp(data, chroma)

    result = core.run_deletion_experiment(RECORD, reset_first=False, app=app, chroma=chroma)

    assert result["verdict"] == "invalid"
    assert result["reasons"] == ["The record was not found before the delete"]
    assert ("delete", RECORD) not in app.calls


def test_verdict_invalid_when_the_delete_is_not_2xx(acme, chroma):
    app = FakeApp(acme, chroma, delete_status=500)

    result = core.run_deletion_experiment(RECORD, reset_first=False, app=app, chroma=chroma)

    assert result["verdict"] == "invalid"
    assert result["delete"]["status"] == 500
    assert any("HTTP 500" in r for r in result["reasons"])


def test_verdict_invalid_when_the_app_is_unreachable(acme, chroma):
    # No fake: FG_APP_URL points at a closed local port.
    result = core.run_deletion_experiment(RECORD, reset_first=False, chroma=chroma)

    assert result["verdict"] == "invalid"
    assert any("App unreachable" in r for r in result["reasons"])
    assert result["before"] is None and result["delete"] is None
    assert Path(result["run_file"]).exists()


def test_verdict_invalid_when_chroma_is_unreachable(acme, chroma):
    # The fake app still has its stores; the inspector's Chroma is a closed port.
    app = FakeApp(acme, chroma)

    result = core.run_deletion_experiment(RECORD, reset_first=False, app=app)

    assert result["verdict"] == "invalid"
    assert any("Chroma unreachable" in r for r in result["reasons"])
    assert ("delete", RECORD) not in app.calls


# ---------------------------------------------------------------------------
# 7. Evidence file
# ---------------------------------------------------------------------------

def test_experiment_writes_evidence_to_runs_dir_and_returns_its_path(acme, chroma, env):
    app = FakeApp(acme, chroma)

    result = core.run_deletion_experiment(RECORD, app=app, chroma=chroma)

    run_file = Path(result["run_file"])
    assert run_file.parent == Path(env["FG_RUNS_DIR"])
    assert run_file.name == f"{result['run_id']}.json"
    assert json.loads(run_file.read_text(encoding="utf-8")) == result
    assert app.calls[0] == "reset"
    assert result["reset"] == {"documents": 2, "chunks": 4, "vectors": 4}
    assert result["verdict"] == "clean", result["reasons"]


# ---------------------------------------------------------------------------
# 8. MCP server over stdio
# ---------------------------------------------------------------------------

def _contract_descriptions() -> dict[str, str]:
    text = CONTRACT.read_text(encoding="utf-8")
    return dict(re.findall(r"^### `(\w+)\(.*\)`\n\n> (.+)$", text, flags=re.MULTILINE))


def test_mcp_server_lists_the_contract_tools_and_reports_an_unreachable_app():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def talk():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "inspector.mcp_server"],
            cwd=str(ROOT),
            env=dict(os.environ),  # the autouse fixture's FG_* values, FG_HEALTH_TIMEOUT=1
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            call = await session.call_tool("inspect_record", {"record_id": RECORD})
            return tools, call

    tools, call = asyncio.run(talk())

    expected = _contract_descriptions()
    assert sorted(expected) == ["inspect_record", "reset_fixture", "run_deletion_experiment"]
    assert {t.name: t.description for t in tools} == expected
    assert {t.name: sorted(t.inputSchema.get("properties", {})) for t in tools} == {
        "reset_fixture": [],
        "inspect_record": ["canary", "probe_question", "record_id"],
        "run_deletion_experiment": ["record_id", "reset_first"],
    }

    assert not call.isError
    result = json.loads(call.content[0].text)
    assert result["record_id"] == RECORD and result["canary"] == CANARY
    assert any(e.startswith("App unreachable") for e in result["errors"])
    assert any(e.startswith("Chroma unreachable") for e in result["errors"])
    assert result["retrieval_probe"]["returned_record"] is None
    assert result["not_checked"] == core.NOT_CHECKED

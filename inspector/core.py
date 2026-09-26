"""
inspector/core.py — discovery, matching and the deletion experiment.

The inspector is deterministic Python with no model calls. It observes; it
never judges. The specification is .bob/skills/trace-deletion/inspector-contract.md.
The app under test is reached only through its HTTP API and its seed file.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
TEXT_SUFFIXES = {".json", ".jsonl", ".ndjson", ".log", ".txt", ".csv", ".md"}
EXCERPT_CHARS = 80
PROBE_TOP_K = 5

NOT_CHECKED = [
    "Backups and snapshots",
    "Physical remnants in database files (free pages, WAL): checks are logical, not forensic",
    "Chroma internal storage (logs, index segments): checked through the API only",
    "Files outside FG_DATA_DIR or inside FG_EXCLUDE",
    "In-process caches: visible only through the retrieval probe",
    "Third-party services and model weights",
]


# ---------------------------------------------------------------------------
# Configuration — read from the environment on every call
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    app_url: str
    data_dir: Path
    exclude: tuple[Path, ...]
    chroma_host: str
    chroma_port: int
    seed_file: Path
    runs_dir: Path
    settle_seconds: float
    health_timeout: float

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ.get
        return cls(
            app_url=env("FG_APP_URL", "http://127.0.0.1:8000").rstrip("/"),
            data_dir=Path(env("FG_DATA_DIR", "data")),
            exclude=tuple(
                Path(p.strip()) for p in env("FG_EXCLUDE", "data/chroma").split(",") if p.strip()
            ),
            chroma_host=env("FG_CHROMA_HOST", "127.0.0.1"),
            chroma_port=int(env("FG_CHROMA_PORT", "8001")),
            seed_file=Path(env("FG_SEED_FILE", "app/seed_data/synthetic_docs.json")),
            runs_dir=Path(env("FG_RUNS_DIR", "reports/runs")),
            settle_seconds=float(env("FG_SETTLE_SECONDS", "1.0")),
            # Not in the contract: lets tests shorten the 30-second health wait.
            health_timeout=float(env("FG_HEALTH_TIMEOUT", "30")),
        )

    def health_error(self) -> str:
        return f"App unreachable: GET {self.app_url}/health did not succeed within {self.health_timeout:g}s"


# ---------------------------------------------------------------------------
# The app under test, through its HTTP API only
# ---------------------------------------------------------------------------

class HttpApp:
    """HTTP client for the app. Tests pass an object with the same four methods."""

    def __init__(self, base_url: str, health_timeout: float):
        self.base_url = base_url
        self.health_timeout = health_timeout

    def wait_healthy(self) -> bool:
        """Poll GET /health until it answers 2xx or health_timeout passes."""
        deadline = time.monotonic() + self.health_timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                status, _ = self._request("GET", "/health", timeout=max(0.1, min(5.0, remaining)))
                if 200 <= status < 300:
                    return True
            except OSError:
                pass
            if time.monotonic() + 0.5 >= deadline:
                return False
            time.sleep(0.5)

    def reset(self) -> tuple[int, Any]:
        return self._request("POST", "/admin/reset", timeout=120)

    def query(self, owner_id: str, question: str, top_k: int) -> tuple[int, Any]:
        body = {"owner_id": owner_id, "question": question, "top_k": top_k}
        return self._request("POST", "/query", body)

    def delete(self, record_id: str) -> tuple[int, Any]:
        return self._request("DELETE", "/documents/" + urllib.parse.quote(record_id, safe=""))

    def _request(self, method: str, path: str, body: Any = None, timeout: float = 30) -> tuple[int, Any]:
        """Return (status, parsed body). Raises OSError when the app can't be reached."""
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _parse_body(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _parse_body(e.read())
        except http.client.HTTPException as e:
            raise ConnectionError(f"{type(e).__name__}: {e}") from e


def _parse_body(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def _chroma_client(cfg: Config) -> Any:
    return chromadb.HttpClient(
        host=cfg.chroma_host,
        port=cfg.chroma_port,
        settings=Settings(anonymized_telemetry=False),
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _excerpt(text: str, needle: str) -> str:
    """At most EXCERPT_CHARS characters of text around the first occurrence of needle."""
    pad = max(0, (EXCERPT_CHARS - len(needle)) // 2)
    start = max(0, min(text.find(needle) - pad, len(text) - EXCERPT_CHARS))
    return text[start:start + EXCERPT_CHARS]


def _field_matches(
    field: str, value: Any, record_id: str, canary: str | None, keyed: bool = True
) -> list[dict]:
    """Apply the keyed and content rules to one value, compared as text."""
    if isinstance(value, (list, tuple)):  # Chroma array metadata
        return [
            m
            for i, v in enumerate(value)
            for m in _field_matches(f"{field}[{i}]", v, record_id, canary, keyed)
        ]
    if value is None:
        return []
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    matches = []
    if keyed and (text == record_id or text.startswith(record_id + ":")):
        matches.append({"field": field, "rule": "keyed", "excerpt": _excerpt(text, record_id)})
    if canary and canary in text:
        matches.append({"field": field, "rule": "content", "excerpt": _excerpt(text, canary)})
    return matches


def _hit(location: str, hit_id: str, matches: list[dict]) -> dict:
    """One hit per row, item or line, however many of its fields matched."""
    rules = [r for r in ("keyed", "content") if any(m["rule"] == r for m in matches)]
    return {"location": location, "id": hit_id, "rules": rules, "matches": matches}


def _location(store: str, name: str, hits: list[dict], error: str | None = None) -> dict:
    loc = {
        "store": store,
        "location": name,
        "keyed": sum("keyed" in h["rules"] for h in hits),
        "content": sum("content" in h["rules"] for h in hits),
        "hits": hits,
    }
    if error:
        loc["error"] = error
    return loc


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover(cfg: Config, suffixes: set[str]) -> list[Path]:
    """Regular files under FG_DATA_DIR with one of the suffixes, outside FG_EXCLUDE."""
    excluded = [p.resolve() for p in cfg.exclude]

    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved.is_relative_to(e) for e in excluded)

    found = []
    for dirpath, dirnames, filenames in os.walk(cfg.data_dir):
        base = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not is_excluded(base / d))
        for name in sorted(filenames):
            path = base / name
            if path.suffix.lower() in suffixes and not path.is_symlink() and not is_excluded(path):
                found.append(path)
    return found


# ---------------------------------------------------------------------------
# SQLite — through SQL only, read-only, one fresh connection per call
# ---------------------------------------------------------------------------

def _scan_sqlite(path: Path, record_id: str, canary: str | None) -> list[dict]:
    """One location per table in sqlite_master, including tables with no hits."""
    uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        return [_location("sqlite", str(path), [], f"could not open: {e}")]
    try:
        try:
            tables = [
                name
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid"
                )
            ]
        except sqlite3.Error as e:
            return [_location("sqlite", str(path), [], f"could not list tables: {e}")]
        return [_scan_table(conn, f"{path}#{t}", t, record_id, canary) for t in tables]
    finally:
        conn.close()


def _scan_table(
    conn: sqlite3.Connection, location: str, table: str, record_id: str, canary: str | None
) -> dict:
    """Compare every column of every row as text. Rows are identified by primary key, else rowid."""
    quoted = '"' + table.replace('"', '""') + '"'
    hits: list[dict] = []
    try:
        info = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
        pk = [col[1] for col in sorted(info, key=lambda col: col[5]) if col[5]]
        cur = conn.execute(f"SELECT * FROM {quoted}" if pk else f"SELECT rowid, * FROM {quoted}")
        columns = [d[0] for d in cur.description]
        if not pk:
            columns = columns[1:]
        for row in cur:
            if pk:
                values = row
                row_id = ", ".join(str(row[columns.index(c)]) for c in pk)
            else:
                values = row[1:]
                row_id = f"rowid {row[0]}"
            matches = [
                m for col, v in zip(columns, values) for m in _field_matches(col, v, record_id, canary)
            ]
            if matches:
                hits.append(_hit(location, row_id, matches))
    except sqlite3.Error as e:
        return _location("sqlite", location, hits, str(e))
    return _location("sqlite", location, hits)


# ---------------------------------------------------------------------------
# Chroma — every collection, full scan through the API
# ---------------------------------------------------------------------------

def _scan_chroma(client: Any, record_id: str, canary: str | None) -> list[dict]:
    """One location per collection. Raises if the server can't be reached."""
    locations = []
    for collection in client.list_collections():
        name = collection.name
        try:
            got = collection.get(include=["documents", "metadatas"])
        except Exception as e:
            locations.append(_location("chroma", name, [], f"could not read: {e}"))
            continue
        ids = got["ids"]
        documents = got.get("documents") or [None] * len(ids)
        metadatas = got.get("metadatas") or [None] * len(ids)
        hits = []
        for item_id, document, metadata in zip(ids, documents, metadatas):
            matches = _field_matches("id", item_id, record_id, canary)
            for key, value in (metadata or {}).items():
                matches += _field_matches(f"metadata.{key}", value, record_id, canary)
            matches += _field_matches("document", document, record_id, canary, keyed=False)
            if matches:
                hits.append(_hit(name, item_id, matches))
        locations.append(_location("chroma", name, hits))
    return locations


# ---------------------------------------------------------------------------
# File sweep — content matches only, one hit per matching line
# ---------------------------------------------------------------------------

def _scan_file(path: Path, record_id: str, canary: str | None) -> dict:
    location = str(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"location": location, "content": 0, "hits": [], "error": str(e)}
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        matches = _field_matches("text", line, record_id, canary, keyed=False)
        if matches:
            hits.append(_hit(location, f"line {number}", matches))
    return {"location": location, "content": len(hits), "hits": hits}


# ---------------------------------------------------------------------------
# Retrieval probe
# ---------------------------------------------------------------------------

def _probe(app: Any, owner_id: str, question: str, record_id: str, canary: str | None) -> dict:
    """POST /query as the record's owner. Returns the returned_* flags, or an error."""
    try:
        status, body = app.query(owner_id, question, PROBE_TOP_K)
    except OSError as e:
        return {"error": f"POST /query failed: {e}"}
    passages = body.get("passages") if isinstance(body, dict) else None
    if not 200 <= status < 300 or not isinstance(passages, list):
        return {"error": f"POST /query returned HTTP {status}: {str(body)[:200]}"}
    return {
        "returned_record": any(p.get("doc_id") == record_id for p in passages),
        "returned_canary": bool(canary) and any(canary in str(p.get("text", "")) for p in passages),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _seed_note(seed_file: Path, record_id: str) -> dict | None:
    notes = json.loads(seed_file.read_text(encoding="utf-8"))
    return next((n for n in notes if n.get("id") == record_id), None)


def inspect_record(
    record_id: str,
    canary: str | None = None,
    probe_question: str | None = None,
    *,
    app: Any = None,
    chroma: Any = None,
) -> dict:
    """Search every reachable store and the retrieval probe for one record. Read-only."""
    cfg = Config.from_env()
    app = app or HttpApp(cfg.app_url, cfg.health_timeout)
    errors: list[str] = []
    not_checked = list(NOT_CHECKED)

    note: dict = {}
    try:
        note = _seed_note(cfg.seed_file, record_id) or {}
    except (OSError, ValueError) as e:
        errors.append(f"Seed file {cfg.seed_file} unreadable: {e}")
    canary = canary or note.get("canary")
    probe_question = probe_question or note.get("probe_question")
    owner_id = note.get("owner_id")
    if not canary:
        not_checked.append("Content matches: no canary was given or found in the seed file")

    app_ok = app.wait_healthy()
    if not app_ok:
        errors.append(cfg.health_error())

    if not cfg.data_dir.is_dir():
        errors.append(f"FG_DATA_DIR {cfg.data_dir} is not a directory")
    locations = []
    for path in _discover(cfg, SQLITE_SUFFIXES):
        locations += _scan_sqlite(path, record_id, canary)
    try:
        client = chroma if chroma is not None else _chroma_client(cfg)
        locations += _scan_chroma(client, record_id, canary)
    except Exception as e:
        errors.append(f"Chroma unreachable at {cfg.chroma_host}:{cfg.chroma_port}: {e}")
    files = [_scan_file(path, record_id, canary) for path in _discover(cfg, TEXT_SUFFIXES)]
    errors += [f"{loc['location']}: {loc['error']}" for loc in locations + files if "error" in loc]

    probe: dict[str, Any] = {
        "question": probe_question,
        "owner_id": owner_id,
        "returned_record": None,
        "returned_canary": None,
    }
    if not (probe_question and owner_id):
        not_checked.append("Retrieval probe: no probe question and owner for this record in the seed file")
    elif not app_ok:
        probe["error"] = "not run: the app is unreachable"
    else:
        probe.update(_probe(app, owner_id, probe_question, record_id, canary))
        if "error" in probe:
            errors.append(f"Retrieval probe: {probe['error']}")

    with_hits = [loc for loc in locations if loc["keyed"] or loc["content"]]
    with_hits += [f for f in files if f["content"]]
    result = {
        "record_id": record_id,
        "canary": canary,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "locations": locations,
        "files": files,
        "retrieval_probe": probe,
        "totals": {
            "locations_with_hits": len(with_hits),
            "keyed": sum(loc["keyed"] for loc in locations),
            "content": sum(loc["content"] for loc in locations) + sum(f["content"] for f in files),
        },
        "not_checked": not_checked,
    }
    if errors:
        result["errors"] = errors
    return result


def reset_fixture(*, app: Any = None) -> dict:
    """POST /admin/reset. Returns the app's counts, or {"error": ...}."""
    cfg = Config.from_env()
    app = app or HttpApp(cfg.app_url, cfg.health_timeout)
    if not app.wait_healthy():
        return {"error": cfg.health_error()}
    try:
        status, body = app.reset()
    except OSError as e:
        return {"error": f"POST /admin/reset failed: {e}"}
    if not 200 <= status < 300:
        return {"error": f"POST /admin/reset returned HTTP {status}", "response": body}
    return body


def _present(result: dict) -> bool:
    """Any keyed or content hit, or the probe returning the record or its canary."""
    probe = result["retrieval_probe"]
    return bool(
        result["totals"]["keyed"]
        or result["totals"]["content"]
        or probe["returned_record"]
        or probe["returned_canary"]
    )


def _unobserved(result: dict, when: str) -> list[str]:
    """Why an inspection can't support a verdict: errors, or a probe that didn't run."""
    reasons = [f"{when} the delete: {e}" for e in result.get("errors", [])]
    probe = result["retrieval_probe"]
    if probe["returned_record"] is None and "error" not in probe:
        reasons.append(f"{when} the delete: the retrieval probe did not run")
    return reasons


def run_deletion_experiment(
    record_id: str,
    reset_first: bool = True,
    *,
    app: Any = None,
    chroma: Any = None,
) -> dict:
    """Reset, inspect, delete through the app, settle, inspect again, and write the evidence.

    Stops before the delete once the run is already invalid, so a delete is
    never sent when its effect couldn't be observed.
    """
    cfg = Config.from_env()
    app = app or HttpApp(cfg.app_url, cfg.health_timeout)
    started = datetime.now(timezone.utc)
    run_id = f"{re.sub(r'[^A-Za-z0-9._-]', '_', record_id)}-{started:%Y%m%dT%H%M%S%fZ}"
    reasons: list[str] = []
    evidence: dict[str, Any] = {
        "run_id": run_id,
        "record_id": record_id,
        "verdict": None,
        "reasons": reasons,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "settings": {
            "app_url": cfg.app_url,
            "data_dir": str(cfg.data_dir),
            "exclude": [str(p) for p in cfg.exclude],
            "chroma": f"{cfg.chroma_host}:{cfg.chroma_port}",
            "settle_seconds": cfg.settle_seconds,
            "reset_first": reset_first,
        },
        "reset": None,
        "before": None,
        "delete": None,
        "after": None,
    }

    if not app.wait_healthy():
        reasons.append(cfg.health_error())
    if not reasons and reset_first:
        evidence["reset"] = reset_fixture(app=app)
        if "error" in evidence["reset"]:
            reasons.append(f"Reset failed: {evidence['reset']['error']}")
    if not reasons:
        evidence["before"] = before = inspect_record(record_id, app=app, chroma=chroma)
        reasons += _unobserved(before, "Before")
        if not reasons and not _present(before):
            reasons.append("The record was not found before the delete")
    if not reasons:
        try:
            status, body = app.delete(record_id)
        except OSError as e:
            reasons.append(f"DELETE /documents/{record_id} failed: {e}")
        else:
            evidence["delete"] = {"status": status, "body": body}
            if not 200 <= status < 300:
                reasons.append(f"DELETE /documents/{record_id} returned HTTP {status}")
            time.sleep(cfg.settle_seconds)
            evidence["after"] = after = inspect_record(record_id, app=app, chroma=chroma)
            reasons += _unobserved(after, "After")

    if reasons:
        evidence["verdict"] = "invalid"
    elif _present(evidence["after"]):
        evidence["verdict"] = "residuals"
    else:
        evidence["verdict"] = "clean"

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    run_file = cfg.runs_dir / f"{run_id}.json"
    evidence["run_file"] = str(run_file)
    run_file.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence

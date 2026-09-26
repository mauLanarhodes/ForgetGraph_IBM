#!/usr/bin/env python3
"""
scripts/smoke_test.py — self-contained smoke test for Acme Notes.

Starts Chroma and uvicorn as subprocesses on free ports, runs 9 checks
against the API contract, then shuts both down regardless of outcome.

Usage:
    python scripts/smoke_test.py
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
VENV_BIN = ROOT / ".venv" / "bin"

FAILURES: list[str] = []


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(base: str, path: str) -> tuple[int, dict]:
    url = f"{base}{path}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def _post(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            return e.code, json.loads(body_bytes)
        except Exception:
            return e.code, {}


def _delete(base: str, path: str) -> tuple[int, dict]:
    url = f"{base}{path}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✓  {name}")
    else:
        msg = f"  ✗  {name}" + (f": {detail}" if detail else "")
        print(msg)
        FAILURES.append(msg)


def wait_for_health(base: str, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = _get(base, "/health")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_for_chroma(host: str, port: int, timeout: int = 120) -> bool:
    """Poll Chroma's /api/v1 or /api/v2 endpoint until it responds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for path in ["/api/v2", "/api/v1"]:
            try:
                url = f"http://{host}:{port}{path}"
                with urllib.request.urlopen(url, timeout=5):
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    chroma_port = _free_port()
    api_port = _free_port()
    api2_port = _free_port()
    api_base = f"http://127.0.0.1:{api_port}"
    api2_base = f"http://127.0.0.1:{api2_port}"

    chroma_proc = None
    api_proc = None
    api2_proc = None

    tmpdir = tempfile.mkdtemp(prefix="acme_smoke_")
    db_path = os.path.join(tmpdir, "acme.db")
    chroma_data = os.path.join(tmpdir, "chroma")
    os.makedirs(chroma_data, exist_ok=True)

    env_base = {
        **os.environ,
        "ACME_DB_PATH": db_path,
        "ACME_CHROMA_HOST": "127.0.0.1",
        "ACME_CHROMA_PORT": str(chroma_port),
        "ACME_COLLECTION": "acme_chunks_smoke",
    }

    try:
        # ------------------------------------------------------------------
        # Start Chroma
        # ------------------------------------------------------------------
        print(f"Starting Chroma on port {chroma_port} …")
        chroma_proc = subprocess.Popen(
            [
                str(VENV_BIN / "chroma"),
                "run",
                "--path", chroma_data,
                "--port", str(chroma_port),
                "--host", "127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env_base,
        )

        if not wait_for_chroma("127.0.0.1", chroma_port):
            print("ERROR: Chroma did not start in time.")
            return 1
        print("  Chroma ready.")

        # ------------------------------------------------------------------
        # Start API with ACME_FIXTURE_MODE=1
        # ------------------------------------------------------------------
        print(f"Starting API (fixture mode) on port {api_port} …")
        api_env = {**env_base, "ACME_FIXTURE_MODE": "1"}
        api_proc = subprocess.Popen(
            [
                str(VENV_BIN / "uvicorn"),
                "app.main:app",
                "--host", "127.0.0.1",
                "--port", str(api_port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=api_env,
            cwd=str(ROOT),
        )
        if not wait_for_health(api_base):
            print("ERROR: API did not start in time.")
            return 1
        print("  API ready.\n")

        # ------------------------------------------------------------------
        # CHECK 1: POST /admin/reset → 6 docs, equal chunk and vector counts
        # ------------------------------------------------------------------
        print("Check 1: POST /admin/reset")
        status, body = _post(api_base, "/admin/reset")
        check("status 200", status == 200, f"got {status}")
        check("documents == 6", body.get("documents") == 6, str(body))
        chunks_count = body.get("chunks", -1)
        vectors_count = body.get("vectors", -1)
        check("chunks present", chunks_count > 0, str(body))
        check("chunks == vectors", chunks_count == vectors_count, str(body))
        print()

        # ------------------------------------------------------------------
        # CHECK 2: GET /documents?owner_id=user-042 and GET /documents/doc-003
        # ------------------------------------------------------------------
        print("Check 2: Document listing and retrieval")
        status, body = _get(api_base, "/documents?owner_id=user-042")
        check("list status 200", status == 200, f"got {status}")
        user042_ids = {d["id"] for d in body} if isinstance(body, list) else set()
        expected_042 = {"doc-001", "doc-002", "doc-003", "doc-005"}
        check("user-042 owns exactly 4 docs", user042_ids == expected_042, str(user042_ids))

        status, body = _get(api_base, "/documents?owner_id=user-077")
        user077_ids = {d["id"] for d in body} if isinstance(body, list) else set()
        expected_077 = {"doc-004", "doc-006"}
        check("user-077 owns exactly 2 docs", user077_ids == expected_077, str(user077_ids))

        status, body = _get(api_base, "/documents/doc-003")
        check("GET doc-003 status 200", status == 200, f"got {status}")
        check("GET doc-003 has body field", "body" in body, str(body))
        print()

        # ------------------------------------------------------------------
        # Load seed data for checks 3+
        # ------------------------------------------------------------------
        seed_path = ROOT / "app" / "seed_data" / "synthetic_docs.json"
        seed_notes = json.loads(seed_path.read_text())

        # ------------------------------------------------------------------
        # CHECK 3: Each note's probe_question (as its owner) returns it in top 3
        # ------------------------------------------------------------------
        print("Check 3: Semantic retrieval — probe questions")
        for note in seed_notes:
            status, body = _post(api_base, "/query", {
                "owner_id": note["owner_id"],
                "question": note["probe_question"],
                "top_k": 3,
            })
            passages = body.get("passages", []) if status == 200 else []
            returned_doc_ids = [p["doc_id"] for p in passages]
            check(
                f"{note['id']} probe returns note in top 3",
                note["id"] in returned_doc_ids,
                f"got {returned_doc_ids}",
            )
        print()

        # ------------------------------------------------------------------
        # CHECK 4: doc-003 probe → chunk doc-003:1 with canary in text
        # ------------------------------------------------------------------
        print("Check 4: doc-003 probe → doc-003:1 with canary FG-CANARY-3F9K")
        doc003 = next(n for n in seed_notes if n["id"] == "doc-003")
        status, body = _post(api_base, "/query", {
            "owner_id": "user-042",
            "question": doc003["probe_question"],
            "top_k": 3,
        })
        passages = body.get("passages", []) if status == 200 else []
        chunk_ids = [p["chunk_id"] for p in passages]
        target = next((p for p in passages if p["chunk_id"] == "doc-003:1"), None)
        check("doc-003:1 in results", "doc-003:1" in chunk_ids, str(chunk_ids))
        check(
            "doc-003:1 text contains FG-CANARY-3F9K",
            target is not None and "FG-CANARY-3F9K" in target.get("text", ""),
            str(target),
        )
        print()

        # ------------------------------------------------------------------
        # CHECK 5: user-077 queries never return user-042 notes
        # ------------------------------------------------------------------
        print("Check 5: user-077 queries never return user-042 notes")
        questions = [n["probe_question"] for n in seed_notes if n["owner_id"] == "user-042"]
        for q in questions:
            status, body = _post(api_base, "/query", {
                "owner_id": "user-077",
                "question": q,
                "top_k": 3,
            })
            passages = body.get("passages", []) if status == 200 else []
            leaked = [p for p in passages if p["doc_id"] in expected_042]
            check(
                f"no user-042 docs returned for '{q[:40]}…'",
                len(leaked) == 0,
                str([p["doc_id"] for p in leaked]),
            )
        print()

        # ------------------------------------------------------------------
        # CHECK 6: POST /documents with existing ID → 409
        # ------------------------------------------------------------------
        print("Check 6: Duplicate ID → 409")
        status, body = _post(api_base, "/documents", {
            "id": "doc-001",
            "owner_id": "user-042",
            "title": "Dup",
            "body": "Duplicate body.",
        })
        check("duplicate returns 409", status == 409, f"got {status}")
        print()

        # ------------------------------------------------------------------
        # CHECK 7: POST /admin/reindex → same vector count as reset
        # ------------------------------------------------------------------
        print("Check 7: POST /admin/reindex")
        status, body = _post(api_base, "/admin/reindex")
        check("reindex status 200", status == 200, f"got {status}")
        reindex_vectors = body.get("vectors", -1)
        check(
            "reindex vector count == reset chunk count",
            reindex_vectors == chunks_count,
            f"reindex={reindex_vectors}, reset chunks={chunks_count}",
        )
        print()

        # ------------------------------------------------------------------
        # CHECK 8: DELETE /documents/doc-003
        # ------------------------------------------------------------------
        print("Check 8: DELETE /documents/doc-003")
        status, body = _delete(api_base, "/documents/doc-003")
        check("first DELETE 200", status == 200, f"got {status}")
        check(
            "response shape correct",
            body.get("deleted") is True and body.get("doc_id") == "doc-003",
            str(body),
        )

        status, _ = _delete(api_base, "/documents/doc-003")
        check("second DELETE 404", status == 404, f"got {status}")

        status, _ = _get(api_base, "/documents/doc-003")
        check("GET after delete 404", status == 404, f"got {status}")
        print()

        # ------------------------------------------------------------------
        # CHECK 9: Second API process without ACME_FIXTURE_MODE → 404 for reset
        # ------------------------------------------------------------------
        print("Check 9: /admin/reset without ACME_FIXTURE_MODE → 404")
        api2_env = {**env_base}  # no ACME_FIXTURE_MODE
        api2_proc = subprocess.Popen(
            [
                str(VENV_BIN / "uvicorn"),
                "app.main:app",
                "--host", "127.0.0.1",
                "--port", str(api2_port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=api2_env,
            cwd=str(ROOT),
        )
        if not wait_for_health(api2_base, timeout=60):
            check("second API started", False, "did not start in time")
        else:
            status, _ = _post(api2_base, "/admin/reset")
            check("reset returns 404 without fixture mode", status == 404, f"got {status}")
        print()

    finally:
        # ------------------------------------------------------------------
        # Teardown
        # ------------------------------------------------------------------
        for proc in (api2_proc, api_proc, chroma_proc):
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------
    print("=" * 60)
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  {f}")
        return 1
    else:
        print("ALL CHECKS PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(main())

"""
inspector/mcp_server.py — MCP server exposing the three ForgetGraph inspector tools.

Run with:
    python -m inspector.mcp_server
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import core

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP("forgetgraph")


@mcp.tool(
    name="reset_fixture",
    description=(
        "Resets the local fixture app to its seed data by calling POST /admin/reset. "
        "Use it before an experiment to start from a known state. "
        "It changes data, so use it only on the synthetic fixture. "
        "Returns the counts of documents, chunks and vectors after the reset."
    ),
)
def reset_fixture() -> dict:
    return core.reset_fixture()


@mcp.tool(
    name="inspect_record",
    description=(
        "Read-only. Searches every reachable store (all SQLite tables, all Chroma collections "
        "and text files in the data directory) and the app's /query endpoint for one record, "
        "by exact ID and by its canary string. "
        "canary and probe_question default to the values in the seed file. "
        "Returns every hit with its location, plus the list of places that were not checked."
    ),
)
def inspect_record(
    record_id: str,
    canary: str | None = None,
    probe_question: str | None = None,
) -> dict:
    return core.inspect_record(record_id, canary=canary, probe_question=probe_question)


@mcp.tool(
    name="run_deletion_experiment",
    description=(
        "Runs one deletion experiment on a synthetic record: resets the fixture unless "
        "reset_first is false, inspects, deletes the record through "
        "DELETE /documents/{record_id}, waits briefly, and inspects again. "
        "Returns the before and after results, the delete response and a verdict of "
        "clean, residuals or invalid. "
        "Writes the full evidence to reports/runs/{run_id}.json."
    ),
)
def run_deletion_experiment(
    record_id: str,
    reset_first: bool = True,
) -> dict:
    return core.run_deletion_experiment(record_id, reset_first=reset_first)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")

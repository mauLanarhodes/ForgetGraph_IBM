"""
inspector/cli.py — thin CLI wrapper around inspector/core.py.

Usage:
    python -m inspector.cli reset
    python -m inspector.cli inspect <record_id>
    python -m inspector.cli experiment <record_id>
"""

from __future__ import annotations

import json
import sys

from . import core


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: python -m inspector.cli <reset|inspect|experiment> [record_id]", file=sys.stderr)
        sys.exit(1)

    cmd = args[0]

    if cmd == "reset":
        result = core.reset_fixture()
        print(json.dumps(result, indent=2))

    elif cmd == "inspect":
        if len(args) < 2:
            print("Usage: python -m inspector.cli inspect <record_id>", file=sys.stderr)
            sys.exit(1)
        record_id = args[1]
        result = core.inspect_record(record_id)
        print(json.dumps(result, indent=2))

    elif cmd == "experiment":
        if len(args) < 2:
            print("Usage: python -m inspector.cli experiment <record_id>", file=sys.stderr)
            sys.exit(1)
        record_id = args[1]
        result = core.run_deletion_experiment(record_id)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

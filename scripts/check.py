"""Self-check: every vector validates against events.schema.json.

This is the only thing CI in this repo runs — baton-spec ships data, not a
validation library, so producer repos (baton, baton-proxy, baton-extmcp,
baton-ts) each validate against this schema with their own ecosystem's
JSON Schema tooling (jsonschema for Python, ajv for TS). This script just
guards against shipping a schema/vectors pair that disagree with each other.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    schema = json.loads((ROOT / "events.schema.json").read_text())
    vectors = sorted((ROOT / "vectors").glob("*.json"))
    if not vectors:
        print("no vectors found", file=sys.stderr)
        return 1

    failed = False
    for vector_path in vectors:
        event = json.loads(vector_path.read_text())
        try:
            jsonschema.validate(event, schema)
        except jsonschema.ValidationError as e:
            print(f"FAIL {vector_path.name}: {e.message}", file=sys.stderr)
            failed = True
        else:
            print(f"ok   {vector_path.name}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

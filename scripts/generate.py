"""Regenerate events.schema.json and vectors/*.json from the reference
implementation (baton-sdk, Python).

Usage (run from a baton checkout with baton-sdk installed, e.g. its own
.venv):

    cd ../baton && .venv/bin/python ../baton-spec/scripts/generate.py

The schema is exported directly from baton.events.Event (the discriminated
union all producers must match). The vectors are real emitted envelopes —
captured by driving one scenario through the mcp adapter (the capture path
that exercises every event type, including surface_snapshot, which only the
mcp/fastmcp adapters emit) and reading back FileSink's JSONL output — not
hand-authored examples, so they can't drift from what the SDK actually puts
on the wire.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def write_schema() -> None:
    from baton.events import Event

    schema = TypeAdapter(Event).json_schema()
    (ROOT / "events.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
    print("wrote events.schema.json")


async def _capture_events(events_path: str) -> None:
    from baton.integrations.mcp import VendorConfig, install_baton
    from baton.integrations.mcp._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink

    mcp = FastMCP("spec-vector-generator")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    @mcp.tool()
    def boom() -> None:
        raise ValueError("simulated failure")

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="spec-vectors",
            vendor_display_name="Spec Vector Generator",
            consent_token="ct_spec_vectors",
            sink=FileSink(events_path),
        ),
    )
    try:
        await mcp.call_tool(
            "spec-vectors_annotate",
            {"intent": "look something up", "expected_outcome": "a match"},
        )
        await mcp.call_tool("lookup", {"name": "alice"})
        try:
            await mcp.call_tool("boom", {})
        except Exception:
            pass
    finally:
        await handle.aclose()


def write_vectors() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        events_path = str(Path(tmp) / "events.jsonl")
        asyncio.run(_capture_events(events_path))
        with open(events_path) as f:
            events = [json.loads(line) for line in f if line.strip()]

    vectors_dir = ROOT / "vectors"
    vectors_dir.mkdir(exist_ok=True)

    seen_types: set[str] = set()
    for event in events:
        event_type = event["event_type"]
        if event_type in seen_types:
            continue
        seen_types.add(event_type)
        out = vectors_dir / f"{event_type}.json"
        out.write_text(json.dumps(event, indent=2) + "\n")
        print(f"wrote vectors/{event_type}.json")

    missing = {"tool_call_start", "tool_call_end", "tool_call_error", "annotation", "surface_snapshot"} - seen_types
    if missing:
        raise SystemExit(f"scenario did not produce every event type, missing: {missing}")


if __name__ == "__main__":
    write_schema()
    write_vectors()

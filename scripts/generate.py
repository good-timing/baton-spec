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
import re
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
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink

    mcp = FastMCP("spec-vector-generator")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    @mcp.tool()
    def boom() -> None:
        raise ValueError("simulated failure")

    # ⚠ The SECOND failure shape (SPEC §11.4.3), and it needs its own tool
    # because the two produce structurally different ``tool_call_error``
    # payloads: this one populates ``result``, ``boom`` cannot. A scenario
    # holding only ``boom`` puts the new field in the schema with no vector
    # exercising it — present, and never once pinned.
    @mcp.tool()
    def soft_fail() -> Any:
        import mcp.types as mcp_types

        content = [mcp_types.TextContent(type="text", text="simulated returned failure")]
        try:
            return mcp_types.CallToolResult(content=content, is_error=True)
        except Exception:  # mcp 1.x spells it the other way
            return mcp_types.CallToolResult(content=content, isError=True)

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="spec-vectors",
            vendor_display_name="Spec Vector Generator",
            consent_token="ct_spec_vectors",
            sink=FileSink(events_path),
            # Explicit because the DEFAULT moved out from under this script:
            # ``proactive_mode`` now defaults to "off", under which the
            # annotation tool refuses a signal_type-less call and returns
            # ok:False instead of emitting. That is a real product default, but
            # here it silently dropped the ``annotation`` vector — the scenario
            # must exercise every event type, so the mode is pinned rather than
            # inherited.
            proactive_mode="on",
        ),
    )
    try:
        # The TOOL's parameter names are not the payload's field names, and
        # this call sent the payload's until it started failing: the annotation
        # tool takes ``user_goal`` / ``expected_result`` (agent-facing wording)
        # and writes them into the envelope as ``intent`` / ``expected_outcome``.
        # ``user_goal`` is REQUIRED, so the old spelling raised rather than
        # producing a wrong vector — which is why only the schema half of this
        # script had been running.
        # The tool's name comes from the handle: the SDK derives it from the
        # server's name, so a literal goes stale.
        await mcp.call_tool(
            handle.annotation_tool_name,
            {"user_goal": "look something up", "expected_result": "a match"},
        )
        await mcp.call_tool("lookup", {"name": "alice"})
        try:
            await mcp.call_tool("boom", {})
        except Exception:
            pass
        try:
            await mcp.call_tool("soft_fail", {})
        except Exception:
            pass
    finally:
        await handle.aclose()


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _pin_volatile(events: list[dict[str, Any]]) -> None:
    """Replace what changes on every run, so a regeneration diffs only where the
    SDK's output did.

    Clock-based ids become fixed UUIDs, mapped consistently so a call's start
    and end still share one ``call_id``; timestamps and durations become
    constants. ``surface_hash`` is untouched — it must match the real surface.
    """
    pinned: dict[str, str] = {}

    def pin(match: re.Match[str]) -> str:
        return pinned.setdefault(match.group(0), f"00000000-0000-7000-8000-{len(pinned) + 1:012d}")

    for event in events:
        for field in ("event_id", "session_id", "call_id"):
            if isinstance(event.get(field), str):
                event[field] = _UUID.sub(pin, event[field])
        event["captured_at"] = "2026-01-01T00:00:00Z"
        if isinstance(event.get("payload"), dict) and "duration_ms" in event["payload"]:
            event["payload"]["duration_ms"] = 0


def write_vectors() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        events_path = str(Path(tmp) / "events.jsonl")
        asyncio.run(_capture_events(events_path))
        with open(events_path) as f:
            events = [json.loads(line) for line in f if line.strip()]
    _pin_volatile(events)

    vectors_dir = ROOT / "vectors"
    vectors_dir.mkdir(exist_ok=True)

    seen_types: set[str] = set()
    for event in events:
        event_type = event["event_type"]
        # ``tool_call_error`` has TWO structurally different shapes and needs a
        # vector each (SPEC §11.4.3): a raise leaves ``result`` null, a
        # returned error flag populates it. One-vector-per-type would pin
        # whichever the scenario happened to call first and leave the other
        # shape — the one this field was added for — unexercised.
        name = event_type
        if event_type == "tool_call_error" and (event.get("payload") or {}).get("result") is not None:
            name = "tool_call_error.returned"
        if name in seen_types:
            continue
        seen_types.add(name)
        out = vectors_dir / f"{name}.json"
        out.write_text(json.dumps(event, indent=2) + "\n")
        print(f"wrote vectors/{name}.json")

    missing = {
        "tool_call_start",
        "tool_call_end",
        "tool_call_error",
        "tool_call_error.returned",
        "annotation",
        "surface_snapshot",
    } - seen_types
    if missing:
        raise SystemExit(f"scenario did not produce every event type, missing: {missing}")


if __name__ == "__main__":
    write_schema()
    write_vectors()

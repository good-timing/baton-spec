"""Regenerate events.schema.json, vectors/*.json and scrub/cases.json from
the reference implementation (baton-sdk, Python).

Usage (run from a baton checkout with baton-sdk installed, e.g. its own
.venv):

    cd ../baton && .venv/bin/python ../baton-spec/scripts/generate.py

The schema is exported directly from baton.events.Event (the discriminated
union all producers must match). The vectors are real emitted envelopes —
captured by driving two scenarios through the mcp adapter (the capture path
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
            # By default no annotation tool is registered.
            report_mode="on",
        ),
    )
    try:
        await mcp.call_tool("lookup", {"name": "alice"})
        try:
            await mcp.call_tool("boom", {})
        except Exception:
            pass
        try:
            await mcp.call_tool("soft_fail", {})
        except Exception:
            pass
        # The tool's name comes from the handle: the SDK derives it from the
        # server's name, so a literal goes stale.
        await mcp.call_tool(
            handle.annotation_tool_name,
            {
                "user_goal": "look something up",
                "what_happened": "asked for a match; the tool returned an error with no reason",
                "tool_name": "soft_fail",
            },
        )
    finally:
        await handle.aclose()


async def _capture_identified_events(events_path: str) -> None:
    from baton import Organization, Principal
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink

    mcp = FastMCP("spec-vector-generator")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    def resolve_principal(_context: Any) -> Principal:
        return Principal(
            principal_id="user-123",
            display_name="Jane",
            organization=Organization(organization_id="org-456", display_name="Acme"),
        )

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="spec-vectors",
            consent_token="ct_spec_vectors",
            sink=FileSink(events_path),
            resolve_principal=resolve_principal,
        ),
    )
    try:
        await mcp.call_tool("lookup", {"name": "alice", "user_goal": "1: find alice; success is a match"})
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


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_vectors() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        events_path = str(Path(tmp) / "events.jsonl")
        identified_path = str(Path(tmp) / "identified.jsonl")
        asyncio.run(_capture_events(events_path))
        asyncio.run(_capture_identified_events(identified_path))
        events = _read_jsonl(events_path)
        identified = _read_jsonl(identified_path)
    # Pinned together so the two scenarios never share a pinned id.
    _pin_volatile(events + identified)
    # SPEC §11.4.6: agent_text stays out of the vectors until every producer sends it.
    for event in events + identified:
        seams = (event.get("payload") or {}).get("seam_augmentations")
        if isinstance(seams, dict):
            seams.pop("agent_text", None)

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
        # An annotation is a note or a report (SPEC §11.4), told apart by
        # ``what_happened``. The SDK no longer files notes, so ``annotation.json``
        # is not regenerated: it stays as the note older producers still send,
        # for a consumer to test that rule against.
        if event_type == "annotation":
            name = "annotation.report"
        if name in seen_types:
            continue
        seen_types.add(name)
        out = vectors_dir / f"{name}.json"
        out.write_text(json.dumps(event, indent=2) + "\n")
        print(f"wrote vectors/{name}.json")

    identified_start = next((e for e in identified if e["event_type"] == "tool_call_start"), None)
    if identified_start is None:
        raise SystemExit("the identified scenario produced no tool_call_start")
    (vectors_dir / "tool_call_start.identified.json").write_text(json.dumps(identified_start, indent=2) + "\n")
    print("wrote vectors/tool_call_start.identified.json")

    missing = {
        "tool_call_start",
        "tool_call_end",
        "tool_call_error",
        "tool_call_error.returned",
        "annotation.report",
        "surface_snapshot",
    } - seen_types
    if missing:
        raise SystemExit(f"scenario did not produce every event type, missing: {missing}")


# Every character Python's or JS's `\s` matches, plus U+180E and U+200B, which
# neither does, so a producer whose whitespace set differs from SPEC §7's shows up.
_SPACES = (
    " \t\n\r\f\v\x1c\x1d\x1e\x1f\x85\xa0\u1680\u180e"
    + "".join(chr(c) for c in range(0x2000, 0x200C))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_FOREIGN_DIGITS = ["٠١٢٣٤٥٦٧٨٩", "०१२३४५६७८९", "０１２３４５６７８９"]
# What sits beside a match: ASCII, then characters a Unicode-aware `\b` or case fold reads differently.
_EDGES = ["", " ", "a", "Z", "7", "_", "-", ".", "(", "\u00e9", "\u00df", "\u0131", "\u0130", "\u212a", "\u017f", "\u03a9", "\U0001f600", "x\u0301"]
_FIELD_KEYS = [
    "email", "Email", "PHONE", "ssn", "api_key", "API_KEY", "token", "Token", "secret", "password",
    "PassWord", "user_name", "USER_NAME", "TO\u212aEN", "pa\u017f\u017fword",
]
_PLAIN_KEYS = ["tokens", "username", "note"]
# A key matches a field name by plain lowercase, not case folding. Python and JS
# lower U+0130 to "i\u0307", which is no field name; Go lowers it to "i".
_DOTTED_I_KEYS = ["AP\u0130_KEY", "EMA\u0130L"]
# SPEC §7's set, mostly used so phone and bearer matches are common.
_IN_SET_SPACES = " \t\n\r\f\xa0"


def _scrub_input(rng: Any) -> Any:
    def sep() -> str:
        if rng.random() < 0.7:
            return rng.choice(["", "-", ".", *_IN_SET_SPACES])
        return rng.choice(_SPACES)

    def digits(n: int) -> str:
        out = "".join(rng.choice("0123456789") for _ in range(n))
        if n and rng.random() < 0.15:
            table = rng.choice(_FOREIGN_DIGITS)
            i = rng.randrange(n)
            out = out[:i] + table[int(out[i])] + out[i + 1 :]
        return out

    def card() -> str:
        body = [rng.randrange(10) for _ in range(rng.choice([12, 13, 15, 18, 19]))]
        total = sum(d if i % 2 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(reversed(body)))
        check = (10 - total % 10) % 10
        if rng.random() < 0.3:
            check = (check + 1) % 10
        return "".join(map(str, [*body, check]))

    def chars(alphabet: str, n: int) -> str:
        return "".join(rng.choice(alphabet) for _ in range(n))

    b64 = lambda n: chars("ABCxyz0189_-", n)
    domain = lambda: rng.choice(["example.com", "ex.co", "x.c", "mail.example.org"])
    local_part = lambda: rng.choice(["jane", "j.doe+x", "a_b", "\u00e9", b64(4)])
    email = lambda: f"{local_part()}@{domain()}"
    phone = lambda: f"{rng.choice(['', '+1', '1'])}{sep()}{rng.choice(['(555)', '555'])}{sep()}{digits(3)}{sep()}{digits(4)}"
    jwt = lambda: f"eyJ{b64(rng.randrange(1, 8))}.{b64(rng.randrange(0, 8))}.{b64(rng.randrange(1, 8))}"
    bearer = lambda: f"{rng.choice(['Bearer', 'bearer', 'BEARER'])}{sep()}{chars('ABCxyz0189_-.+/=', rng.choice([15, 16, 24]))}"
    sk_key = lambda: f"sk-{b64(rng.choice([19, 20, 30]))}"
    aws_key = lambda: "AKIA" + chars("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", rng.choice([15, 16, 17]))
    long_number = lambda: digits(rng.randrange(8, 21))
    # Two rules wanting the same text, so a producer that runs them in another order shows up.
    contested = lambda: rng.choice(
        [
            lambda: f"Bearer {jwt()}",
            lambda: f"{sk_key()}@{domain()}",
            lambda: f"{aws_key()}@{domain()}",
            lambda: f"{digits(rng.randrange(0, 6))}{phone()}{digits(rng.randrange(0, 6))}",
            lambda: f"{phone()}@{domain()}",
            lambda: f"{card()}@example.com",
        ]
    )()
    # Phone and bearer are the patterns whitespace reaches, so they come up most.
    makers = [email, phone, phone, phone, card, jwt, bearer, bearer, bearer, sk_key, aws_key, long_number, contested, contested]

    def text() -> str:
        parts = [rng.choice(_EDGES) + rng.choice(makers)() + rng.choice(_EDGES) for _ in range(rng.choice([1, 1, 2]))]
        return rng.choice(["", " ", "\t", ";"]).join(parts)

    def leaf() -> Any:
        return rng.choice([text, text, text, lambda: "", lambda: rng.randrange(-10, 10**12), lambda: 1.5, lambda: True, lambda: None])()

    def keys(n: int) -> list[str]:
        # A key holding PII too: keys are not scrubbed, only values.
        return rng.sample([*_FIELD_KEYS, *_PLAIN_KEYS, email()], n)

    def tree(depth: int) -> Any:
        if depth == 0 or rng.random() < 0.3:
            return leaf()
        if rng.random() < 0.5:
            return [tree(depth - 1) for _ in range(rng.randrange(0, 3))]
        return {k: tree(depth - 1) for k in keys(rng.randrange(0, 3))}

    kind = rng.random()
    if kind < 0.55:
        return text()
    if kind < 0.85:
        return tree(4)
    # Straddles the depth limit (SPEC §7), with field keys on the way down and
    # a container, maybe empty, at the bottom.
    node: Any = rng.choice([text(), [], {}, [text()], {keys(1)[0]: text()}])
    for _ in range(rng.randrange(8, 13)):
        node = [node] if rng.random() < 0.5 else {keys(1)[0]: node}
    return node


def write_scrub_cases(random_count: int = 800) -> None:
    import random

    from baton.scrub import _PATTERNS, REDACT_FIELD_NAMES, Scrubber

    def scrubbed(value: Any) -> dict[str, Any]:
        # Parsed back so Python scrubs exactly the value every producer reads.
        value = json.loads(json.dumps(value))
        scrubber = Scrubber()
        output = scrubber(value)
        return {"input": value, "output": output, "counts": dict(sorted(scrubber.counts.items()))}

    rng = random.Random(7)
    cases = [scrubbed(_scrub_input(rng)) for _ in range(random_count)]
    hit = {category for case in cases for category in case["counts"]}
    categories = {category for category, _ in _PATTERNS} | {"cc", "depth_limit"}
    missing = (categories | {f"field:{name}" for name in REDACT_FIELD_NAMES}) - hit
    if missing:
        raise SystemExit(f"the scrub cases redact nothing under {sorted(missing)}")
    # One case per key with nothing above it: in a random case a parent key or the
    # depth limit can decide the value.
    cases += [scrubbed({key: "v"}) for key in [*_FIELD_KEYS, *_PLAIN_KEYS, *_DOTTED_I_KEYS]]
    out = ROOT / "scrub" / "cases.json"
    out.parent.mkdir(exist_ok=True)
    # One case per line, so a regeneration diffs per case; ASCII escapes keep
    # U+00A0 and U+2003 apart from a space in that diff.
    out.write_text("[\n" + ",\n".join(json.dumps(case, ensure_ascii=True) for case in cases) + "\n]\n")
    print(f"wrote scrub/cases.json ({len(cases)} cases)")


if __name__ == "__main__":
    # First, so its coverage check stops the run before anything is written.
    write_scrub_cases()
    write_schema()
    write_vectors()

# baton-spec

The Baton wire contract. `SPEC.md` is the prose spec, and the files below are
its machine-readable form (the event envelope is §11.4). Every Baton producer —
`baton-sdk` (Python), `baton-proxy`, `baton-extmcp`, and the TypeScript SDK —
submodules this repo and validates its own emitted events against it. No
producer owns this repo; it's the neutral thing they all agree with.

## Contents

- `events.schema.json` — JSON Schema for the discriminated event union
  (`tool_call_start`, `tool_call_end`, `tool_call_error`, `annotation`,
  `surface_snapshot`, `client_report`, and the lifecycle types), exported
  directly from `baton-sdk`'s Pydantic models.
- `vectors/*.json` — real, conformant example events, captured from an
  actual SDK run (not hand-authored), for producer test suites to validate
  against. One per type, plus a second shape where a type has two
  (`tool_call_error.returned`, `annotation.report`) and one with intent and
  identity filled (`tool_call_start.identified`). Which members a producer
  may differ on is SPEC §11.4.6.
- `scrub/cases.json` — generated inputs with what `baton-sdk`'s default
  `Scrubber` returns for each, and its redaction counts (SPEC §7). A
  producer's scrubber should give the same output and counts for every case.
  The last cases hold one key each, so a producer that lowercases a key
  differently, such as one with U+0130 (`İ`), fails one of them.
- `collector/fake_collector.py` and `collector/scenarios.json` — a collector
  that fails on purpose (slow, 500, 429, 400, hang, disconnect, a refusal of
  the first event only, no batch endpoint, or a 413), and the scenarios each
  SDK runs its HTTP sink through. It takes single events and batches (SPEC
  §2.5). The checks are yes or no: writes never block, close keeps its limit,
  nothing raises, every event is delivered or counted, a loss is reported in
  one `client_report` once events get through, and five events go in one
  request. Python 3,
  standard library only.
- `collector/shutdown.json` — shutdown scenarios (SPEC §11.2 rule 7) each
  SDK runs through its MCP install path against the same collector: close
  and flush send what is queued, a second close waits for the first, a call
  after close is dropped and counted with one warning, and a hung collector
  neither slows a call nor holds close.
- `collector/sessions.json` — how the session ids group (SPEC §3.4) over
  the in-process transport, stateful and stateless streamable HTTP.
- `collector/intent.json` — the injected `user_goal` (SPEC §5.1.2): it is
  listed as required, a call without it is still served, and it and the
  retired names land in the intent members, not in `params`.
- `bench/bench.json` and `scripts/compare_bench.py` — what each SDK's speed
  and load runner measures against the same collector: the time Baton adds
  to a call, a burst of 10,000 calls, the collector hung or down, and memory
  growth over 20,000 calls with the collector healthy and hung. The
  compare script prints the last release and the new code side by side.
  It reports; it decides nothing.

## Using this from a producer repo

```sh
git submodule add https://github.com/good-timing/baton-spec.git baton-spec
git submodule update --init
```

Then, in your own test suite, load `baton-spec/events.schema.json` and
validate your emitted (wire-serialized) events against it with your
ecosystem's JSON Schema validator — `jsonschema` for Python, `ajv` for
TypeScript. `vectors/*.json` are useful both as known-good fixtures and as
a sanity check that your test setup can produce the same shape.

In CI, remember to check out submodules
(`actions/checkout@v5` with `submodules: true`).

## Regenerating

`baton-sdk` (Python, in the sibling `baton` repo) is the reference
implementation. To regenerate `events.schema.json` and `vectors/*.json`
and `scrub/cases.json` after a spec change:

```sh
cd ../baton && .venv/bin/python ../baton-spec/scripts/generate.py
```

`scripts/check.py` is a self-check (run in this repo's CI) that the shipped
schema and vectors agree with each other — it does not validate any
producer's actual output; that's each producer's own job.

## No back-compat guarantees (yet)

None of Baton's packages have external production customers. This repo
does not version the schema or support multiple concurrent schema
versions — when the spec changes, update the schema/vectors here and every
producer's submodule pointer together. Revisit this once there's a real
external deployment to avoid breaking.

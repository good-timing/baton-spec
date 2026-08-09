# baton-spec

The machine-readable wire contract for the Baton event envelope (see
`baton`'s `docs/SPEC.md` §11.4 for the prose spec). Every Baton producer —
`baton-sdk` (Python), `baton-proxy`, `baton-extmcp`, and the TypeScript SDK —
submodules this repo and validates its own emitted events against it. No
producer owns this repo; it's the neutral thing they all agree with.

## Contents

- `events.schema.json` — JSON Schema for the discriminated event union
  (`tool_call_start`, `tool_call_end`, `tool_call_error`, `annotation`,
  `surface_snapshot`), exported directly from `baton-sdk`'s Pydantic models.
- `vectors/*.json` — one real, conformant example event per type, captured
  from an actual SDK run (not hand-authored), for producer test suites to
  validate against.

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
after a spec change:

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

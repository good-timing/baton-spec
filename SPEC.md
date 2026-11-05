# Baton Protocol — Specification

*The wire protocol for structured **event** handoff and **response** return between Baton-instrumented capture surfaces (today: vendor MCP middleware and the library API) and a collector.*

*Stability: **stable from 1.0**. §13 lists what is experimental. Here a `src/` or `tests/` path means one in `baton`.*

*Vocabulary: a **report** is what an agent files, in its own words, when a tool call goes wrong or no tool fits (§5.1). How a collector sorts reports into kinds is its own business and is not part of this spec (§11.5.5). The eight `signal_type` values (§3.1) are words an agent used to pick; from `baton-sdk` 0.8.15 no agent picks one.*

> **"Console" vs "collector".** This spec describes the **HTTPS wire contract** that an `HttpSink` ships events to. The receiver is called the **Console** throughout for brevity, but it's just whatever HTTP collector the vendor points the sink at — a self-hosted ingest service, a hosted Good Timing Console, or a third-party-built one. Vendors who only need local capture can use `StdoutSink` / `FileSink` and ignore this spec entirely; the event envelope (§11.4) is sink-agnostic.

---

## 0. Status

| Field | Value |
|---|---|
| Spec version | 1.0 |
| Date | 2026-11-04 |
| Wire format | JSON over HTTPS |
| Wire encoding | UTF-8 |
| Auth | Bearer token (vendor API key) + consent token (§9) |
| Signing | **Out of scope for the current version** (see §14 open questions). HTTPS + bearer is the current trust model. |
| Open license | **Apache 2.0** — see LICENSE. |

The keywords MUST, MUST NOT, SHOULD, SHOULD NOT, MAY are used as in RFC 2119.

---

## 1. Roles

The protocol involves four parties:

1. **End user** — the human using an agent runtime.
2. **Calling agent** — the LLM-driven agent (Claude Code, Cursor, Cowork, ChatGPT, etc.) that invokes vendor tools on behalf of the end user.
3. **Vendor capture surface** — the SDK boundary where Baton observes agent–tool interactions. Today: a vendor MCP server wrapped via `install_baton(mcp, ...)` middleware, or vendor code instrumented directly with `baton.Client` / `AsyncClient` for the Skills (non-MCP) pattern. Future: customer-side agent-runtime plugins (§14).
4. **Vendor collector** — the vendor's collector, receiving event streams from the SDK.

Baton is the protocol substrate connecting (3) ↔ (4).

The diagram below shows the MCP middleware path; the library API path replaces the MCP transport with direct in-process function calls but ships the same wire envelope (§11.4) to the collector.

```
End User ──► Calling Agent ──MCP──► Vendor MCP Server [Baton SDK] ──HTTPS──► Vendor Collector
                                          ▲                                      │
                                          └────── HTTPS (return channel) ────────┘
```

---

## 2. Wire protocol

### 2.1 Transport
- All Baton wire traffic MUST be HTTPS.
- Primary endpoint (relative to the collector base URL):
  - `POST /v0/events/batch` — SDK → collector, several events in one request (§2.5)
  - `POST /v0/events` — SDK → collector, one event per request (event-stream ingest; see §11.4 for the envelope). A producer uses it only when the collector has no batch endpoint (§2.5)
  - `GET /v0/settings?server=<vendor_id>` — collector → SDK, the server's capture settings, read only by a producer the vendor told to (§2.6)

### 2.2 Encoding
- All payloads MUST be JSON, UTF-8.
- All timestamps MUST be RFC 3339 with timezone (e.g., `2026-05-13T14:22:01.512Z`).
- Durations MUST be milliseconds as integers.

### 2.3 Auth
Every request MUST include both:

| Where | Field | Purpose |
|---|---|---|
| HTTP header | `Authorization: Bearer <vendor_api_key>` | Identifies the vendor. Issued out-of-band by the collector. |
| Body | `consent_token` (every event) | Per-end-user proof of consent. v0 form: UUID granted at SDK init. v0.x will extend to OAuth-scoped tokens. |

The collector MUST reject any request without a valid bearer. The collector MUST reject any event payload without a `consent_token` matching the SDK's registered consent records.

### 2.4 Idempotency
- Event POSTs MUST carry a client-generated `event_id` (UUIDv7 recommended for sortability). The collector MUST treat repeated POSTs with the same `event_id` in the same workspace as the same event (no duplicate ingestion). The same `event_id` in another workspace is a different event.

### 2.5 Batches
`POST /v0/events/batch` carries `{"events": [envelope, ...]}`.

- **Caps.** A batch holds at most **100** events and its body is at most **1 MiB** (1,048,576 bytes). A collector MUST accept a batch within both caps. A producer MUST NOT send a larger one, so it cuts batches by size as well as by count. An event that alone makes a body over 1 MiB is never sent: the producer counts it `refused` (§11.2 rule 8). A producer may measure an event when it is recorded or later, before the event is sent; an event close finds still waiting before it was measured is `unsent_at_close`.
- **One tenant.** Every event in a batch has the same `tenant_id` and `vendor_id`. A collector answers `400` `payload_malformed` to a batch that mixes them.
- **Answer.** `200` with `{"results": [...]}`, one result per event, in the order sent: `{"event_id": ..., "status": "accepted" | "duplicate" | "refused", "error": {"code": ..., "message": ...}}`. `error` is present only on `refused`, with a §12 code. `duplicate` is an `event_id` the collector already holds for that workspace (§2.4). A refused event does not affect the others in its batch. A producer reads a result by its `event_id`, a string. A result whose `status` is `"refused"` refuses its event even when its `error` cannot be read. When several refused results name one `event_id`, the last one's code counts. An event with no readable result in a `2xx` answer counts as delivered. A `2xx` whose body cannot be read before the request times out is a failed request, and the batch is sent again.
- **Errors for the whole batch.** `401` and `403` refuse every event in it. `400` refuses every event, and is the answer to a body that is not an object with an `events` array, to an empty batch, to more than 100 events, and to mixed tenants. `413` `payload_too_large` means the body is over the cap. `429` means too many requests: the producer keeps the events and sends nothing until the wait is over. The wait is the `Retry-After` header read as a whole number of seconds (RFC 9110 `delay-seconds`), at least 1 and at most 60; a `429` with any other `Retry-After`, or none, is handled like a `5xx`. A `429` with a readable `Retry-After` does not count towards the circuit breaker (§11.2 rule 4). Close does not send during the wait: what is waiting is `unsent_at_close`. A `5xx`, a network error, or a `3xx` (a producer does not follow redirects) keeps the events for a later try. Resending a batch is safe, because `event_id` makes each event idempotent.
- **A collector with no batch endpoint.** A producer that gets `404` or `405` from `/v0/events/batch` sends to `/v0/events`, one event per request, for the rest of the process, and sends the batch it was holding that way. It counts nothing as lost for the `404` or `405`. Events still wait for the same timer, then go one per request.

### 2.6 Capture settings

*Experimental (§13): this endpoint and its document may change after 1.0.*

A vendor can choose to manage some capture settings in the collector instead of in code. A producer reads them only when the vendor turned that on (`VendorConfig(settings="console")` in `baton-sdk`, `{ settings: "console" }` in `baton-ts`, `Config{Settings: baton.SettingsConsole}` in the Go SDK). Otherwise it never calls this endpoint and uses the values in the vendor's code. The settings change what Baton records and what Baton adds to the tool list. They never change what the vendor's server does.

**The request.** `GET /v0/settings?server=<vendor_id>`, with the same `Authorization` bearer the producer sends events with (§2.3) and its `tenant_id` in the `X-Baton-Tenant-Id` header. A producer sends the last `ETag` it received as `If-None-Match`.

| Answer | Producer |
|---|---|
| `200` with the document and an `ETag` | uses the document, if it is valid (below) |
| `304` | keeps the document it holds |
| `204` | nothing is saved for this server: uses the values in the vendor's code, and drops any document it held |
| anything else, a network error, or a body it cannot read | keeps the document it holds, or the code values if it has none |

**When.** Once in the background when the SDK is installed or created, then on a fixed interval, 60 seconds by default and never less than 15. A call never waits on a read. Each process reads on its own.

**The document.**

```json
{
  "version": 7,
  "audiences": [
    {"id": "beta", "match": {"label": ["beta"]},
     "capture": {"goal": "required", "reports": "on", "results": "full"}},
    {"id": "sampled", "match": {"share": 0.1},
     "capture": {"goal": "required", "reports": "on", "results": "full"}},
    {"id": "everyone", "match": {},
     "capture": {"goal": "required", "reports": "on", "results": "off"}}
  ]
}
```

A document is valid when `version` is a positive integer, `audiences` is a non-empty list, and every audience has a string `id` and object `match` and `capture`. A producer ignores an invalid document and keeps what it held. Within a valid document:

- **The first audience that matches wins.** Audiences are checked in order. The collector saves only documents whose last audience has an empty `match`, so every caller matches one.
- **`match` keys combine with AND.** An empty `match` matches every caller.
  - `label`: a list of labels. Matches when the caller's `labels` (§11.4) hold any of them.
  - `client`: a list of names. Matches when the event's `client_observed.info.name` (§11.4), the name the client declared, equals one of them. A request with no declared name matches no `client` rule.
  - `share`: a number above 0 and at most 1. Matches a fixed share of identified callers (below). A value outside that range makes the audience match nobody.
  - A key the producer does not know, or a value of the wrong type, makes that audience match nobody.
- **`share`.** The caller's key is `principal.id`, or `organization.id` when there is no principal. A caller with neither never matches `share`. The bucket is the first 8 bytes of SHA-256 over the UTF-8 bytes of `vendor_id`, a line feed (U+000A) and the key, read as an unsigned big-endian integer, modulo 10000. The caller matches when the bucket is less than `share` × 10000 rounded to the nearest integer, a half rounding up (`floor(share × 10000 + 0.5)`). So one caller stays in or out until the share changes, and raising it keeps everyone already in.
- **`capture`** holds three settings. A value the producer does not know falls back to the vendor's code value for that setting alone.

| key | values | vendor's code setting (`baton-sdk` / `baton-ts` / Go) | takes effect |
|---|---|---|---|
| `goal` | `"required"`, `"optional"` | `intent_param_mode` / `intentParamMode` / `IntentParamMode` | the next `tools/list` |
| `reports` | `"on"`, `"off"` | `report_mode` / `reportMode` / `ReportMode` | the next `tools/list` |
| `results` | `"full"`, `"off"` | `result_capture_mode` / `resultCaptureMode` / `ResultCaptureMode` | the next call |

Each event records which settings it ran under in `capture` (§11.4).

**The tool list and the instructions.**

- Each `tools/list` is answered with the caller's `goal` and `reports`. A tool that declares its own `user_goal` keeps its own.
- With `reports` off, the report tool is left out of that caller's list. It stays registered, so a report sent from an older list is still accepted.
- The server instructions go out once per connection, in the `initialize` result, so they are the same for every caller. They carry the sentence that points to the report tool while some caller can get `reports` on. When no document is held, the code's value decides. A new document changes them for connections that start after the next request the server serves.
- A `surface_snapshot` describes the server, not a caller (§5.6 rule 7), so its `seam_augmentations` carry the code's values.
- In `baton-sdk`, `report_mode` is `"on"` by default under `settings="console"`. A vendor who sets `report_mode="off"` gets no report tool, so `reports` can only take it away from some callers.

---

## 3. Inbound: Signal payload

Collector-side behaviour. Not part of this contract. The correlation modes (§3.4) stay here.

### 3.1 Field reference

Collector-side behaviour. Not part of this contract. The `signal_type` values below stay, because producers older than `baton-sdk` 0.8.15 send them on an `annotation` (§11.4).

**`signal_type` enum:**

| Value | Meaning |
|---|---|
| `failure` | Tool returned an error or timed out. |
| `retry_loop` | Same logical call attempted ≥N times in window. |
| `dead_end` | The user is trying something the tool cannot do; no good error path. |
| `parameter_confusion` | Agent is calling the tool wrong because schema isn't obvious. |
| `slow_performance` | Call(s) slow enough that the user may give up. |
| `abandonment` | Session ended without success after attempted use. |
| `feature_gap` | User wanted a capability that doesn't exist as a tool. |
| `other` | Anything that doesn't fit above. Use `intent` + `expected_outcome` to describe. |

**Who sets it.** From `baton-sdk` 0.8.15 an agent neither sees nor sends these words (§5.1.1).

### 3.2 Nested types

Collector-side behaviour. Not part of this contract.

### 3.3 Sample payload

Collector-side behaviour. Not part of this contract.

### 3.4 Correlation modes

There are two correlation modes.

**The mode is read from two envelope fields, not from a mode field.** A session whose events carry `transport_observed: "http"` and `session_id_origin: "minted"` (§11.4) is `per-call`; every other session is `session-stitched`. The envelope field `correlation_mode` was specified, never emitted, and removed on 2026-09-15 (§13): it could not tell a per-event stream from the old merge defect, because both sent fresh ids. `session_id_origin` can, and the two fields together mark the mode. A consumer MUST NOT infer the mode from the `session_id` value itself.

| Mode | Meaning | When the SDK selects it |
|---|---|---|
| `session-stitched` | Multiple tool calls and annotations in the same agent session share a stable `session_id`. | A session-scoped identifier is observable (rungs 3, 4, 4b below), OR no HTTP request is behind the call (stdio, in-memory), where one process serves one agent. |
| `per-call` | Each MCP request is its own session: a tool call's `tool_call_start` and its `tool_call_end` or `tool_call_error` share one minted `session_id`, and no other request carries it. | Rung 5 below: an HTTP request is behind the call and no session-scoped identifier is observable. Stateless streamable HTTP, and every streamable HTTP connection on MCP 2026-07-28+ (SEP-2567). |

**SDK layered fallback for resolving `session_id` (in priority order):**

0. ~~**Vendor-configured resolver hook** — `VendorConfig.resolve_session_id`, an optional SDK-side hook checked before every rung below.~~ **RETIRED 2026-09-12.**

   > Rung 0 fell to the same rule that retired rungs 1-2: it keyed the session on an identifier the SDK did not mint. It differed from those two only in **who supplied the value** — a vendor rather than a client — and the join rule does not draw that line. The SDK mints its own correlation key (`call_id`), and every other identifier is emitted as **data** and keyed on by nothing at capture time.
   >
   > **What a vendor knows about a caller now reaches Baton through `VendorConfig.resolve_principal`** (§11.4's identity ladder rung 0, which is a DIFFERENT ladder and is NOT retired; named `resolve_user` until 0.8.6).
   >
   > This rung was the only mechanism that worked on new-spec (SEP-2567) and stateless streamable HTTP, where nothing below it is observable by protocol design. No vendor had implemented it. Those shapes now end on rung 5 (per-call).
   >
   > Numbering is preserved rather than compacted, for the reason given under rungs 1-2.
1. ~~**W3C trace context** — the trace-id portion of `_meta.traceparent` as the `session_id`.~~ **RETIRED 2026-09-09.**
2. ~~**Vendor-supplied app-level handle** — `_meta["io.baton/session_id"]`.~~ **RETIRED 2026-09-09.**

   > Both rungs keyed the session on an identifier the SDK did not mint, which the join rule forbids: the SDK mints its own correlation key and every other identifier — `traceparent`, the MCP session id, `clientInfo`, the JSON-RPC request id — is emitted as **data** and keyed on by nothing at capture time. An id we mint cannot be removed by a spec revision, resampled, or broken by a vendor's propagation, and it loses joins rather than inventing them.
   >
   > Rung 1 was independently wrong on OpenTelemetry's own terms: a trace spans **one turn**, not one conversation, and OTel's `gen_ai.conversation.id` forbids synthesising a conversation id from another value. Using a trace-id as `session_id` over-fragments systematically — every turn becomes its own session.
   >
   > **Retired means not keyed on, not uncaptured.** `runtime_meta` forwards `_meta` unchanged, so `traceparent` and any vendor handle still arrive at the collector as data. Nothing observable was lost; the decision moved.
   >
   > Numbering is preserved rather than compacted — rungs 3, 4, 4b and 5 are referenced by name across the SDK, its tests and the design notes, and renumbering them would silently change what every one of those references means.
3. **Agent-runtime-specific session key** — per-runtime adapter table in §5.2 (e.g., a future `_meta["claudecode/sessionId"]` if Anthropic ships one).
4. **MCP protocol-level session — the `Mcp-Session-Id` header**, read directly from the request. Surrounding whitespace is not part of a field value (RFC 9110 §5.5), so the header is stripped before use and a blank one is a MISS that falls through to the next rung — a blank accepted as an id would file every such call under one session and merge unrelated conversations. Carried by old-spec (pre-2026-07-28) **stateful** streamable HTTP; removed for new-spec streamable HTTP per SEP-2567; never sent by SSE, which carries its id as a `session_id` query param; and not issued at all in stateless mode.
4b. **The server library's own per-connection session id** — e.g. FastMCP's `Context.session_id`. Usable only where the underlying MCP SDK keeps one session object per connection, so the id survives across calls (mcp 1.x). mcp 2.x rebuilds that object per request, minting a fresh value every call, so the rung is disabled there — as it is when the SDK version cannot be determined, because losing a join is recoverable and inventing one between two users is not. Applies only to a live HTTP request that rung 4 did not answer: in practice SSE, and stateless streamable HTTP, where it degrades to a per-request id. That per-request id names no session the client keeps, so its `session_id_origin` is `"minted"` (§11.4); on SSE it is `"observed"`.
5. **Per-call id** — when every live rung yields nothing (rungs 3, 4 and 4b; 0, 1 and 2 are retired and yield nothing by construction; numbering is preserved, so this reads as "0-4b" in older text) **and** the producer observed `transport_observed: "http"` (§11.4) for this request. The producer mints a new id for the request, and:

   - Every event of one tool call carries it: `tool_call_start` and its `tool_call_end` or `tool_call_error`. No event of any other request carries it. An annotation-tool call, a `tools/list` and every other request (§11.4.4, §11.4.5) get their own.
   - `session_id_origin` is `"minted"`.
   - `sequence_number` is numbered by the clock (§5.6 rule 3), not by a per-session counter. A per-session counter would fill its 4096 entries (§11.2 rule 5) with sessions that never come back, and push out live ones.

   **Fire only on `http`.** On every other state — `no-http-request`, `read-failed`, absent, or a value this document does not register — the ladder ends on the process-wide id minted at install, as before. On stdio and in-memory that id is correct, because one process serves one agent. This is a positive match on purpose: the cost of firing wrongly is splitting a stream that should be one session, so only positive evidence of a transport that can carry many callers fires it.

   ⚠ **A producer MUST NOT report an exception in its own transport read as `no-http-request`.** That state asserts a fact about the deployment — one process, one caller — and an unreadable context is not that fact. A producer whose read raises MUST emit `read-failed`. Folding the two makes a producer defect arrive as evidence that grouping is safe.

   **What it costs.** On these transports a conversation arrives as many sessions. `sequence_number` does not carry across them, and an annotation-tool call is not in the same session as the call it describes.

---

## 4. Outbound: Response payload

Collector-side behaviour. Not part of this contract.

### 4.1 Field reference

Collector-side behaviour. Not part of this contract.

### 4.2 Nested types

Collector-side behaviour. Not part of this contract.

### 4.3 Sample payload

Collector-side behaviour. Not part of this contract.

---

## 5. How the SDK obtains intent, expected_outcome, reports, and runtime context

**Two emission surfaces.** The SDK exposes two parallel paths that emit the same event envelope (§11.4) to the same sink:

1. **MCP middleware** (`install_baton(mcp, VendorConfig(...))`) — for vendors who expose their API as MCP tools. Intent comes from the agent via the injected `user_goal` param; reports, when the vendor turns them on, via the annotation tool described below; runtime context comes from the MCP `_meta` field.
2. **Library API** (`baton.Client` / `AsyncClient`) — for vendors whose customers reach the vendor API via agent-generated code (Skills pattern, not MCP). Intent / expected_outcome come from the developer as kwargs on `client.trace(intent=..., expected_outcome=..., workflow=...)`; reports come via `client.annotate(what_happened=..., tool_name=...)` or `trace.annotate(...)`.

The event envelope and sink behavior are identical across both paths. §5.1 to §5.5 describe the **MCP path** mechanism. The library path has no transport to read and no tool schema to extend, so the vendor's code passes what the middleware would have observed; §5.6 states what it passes and where each value lands on the wire.

On the MCP path, the spec separates two distinct sources, plumbed differently:

- **Agent-emitted** (`intent`, `expected_outcome`, and the report fields `what_happened` / `tool_name`): things only the LLM knows. The LLM's only structured output channel is tool calls, so the SDK exposes a dedicated annotation tool the agent calls.
- **Client-attached** (`session_id`, `runtime_metadata`): things the MCP client orchestrator knows out-of-band. MCP already provides a standard channel for this: the `_meta` JSON-RPC field, used in production by Databricks, OpenAI Agents SDK, and the C# MCP SDK.

Conflating these costs us in both directions. They are separate sections below.

### 5.1 Agent-emitted: the vendor-namespaced annotation tool

**Reports are opt-in in `baton-sdk` from 0.8.23: `VendorConfig.report_mode`, default `"off"`.** `baton-ts` from 0.5.8 (`reportMode`) and `baton-proxy` from 0.6.19 (`BATON_ANNOTATE`) do the same. `baton-extmcp` registers no annotation tool in any mode: the ExtMcp hook can pass, change or refuse a call, but not answer one. Under `"off"` the SDK MUST NOT register an annotation tool, and the only server instructions it adds are the subagent sentence (§5.1.2). Under `"on"` the SDK MUST register an annotation tool on the vendor's MCP server AND MUST set server-level `instructions` motivating its use. Both are required — tool registration alone is insufficient. Empirically: with description-only, calling agents do not call the annotation tool unprompted; with server-level instructions, they do, with high-quality content.

#### 5.1.1 Annotation tool

- **Tool name** (convention): `<server-name-slug>_annotate` (e.g., a server named `"Acme Knowledge Base"` registers `acme-knowledge-base_annotate`), falling back to `<vendor_id>_annotate`. The name is a COSMETIC LOCAL LABEL, never an identifier: nothing joins on it. ⚠ It IS carried on the wire — `surface_snapshot.seam_augmentations.injected_tools` is the SDK reporting what it added to the vendor's surface — so **a consumer MUST NOT treat that value as stable across an SDK upgrade**, and MUST match it by the `_annotate` suffix rather than by an expected exact name. No other field carries it: an annotation's `tool_name` names the tool the annotation is about, not this one, so no join breaks when it changes. The SDK reads the server object's own public `name` — measured to hold across the full supported band of both Python adapters, including the mcp 2.x `FastMCP`→`MCPServer` rename — because `vendor_id` is an opaque minted id and `srv-9f2a7c31_annotate` reads as noise to every agent listing the vendor's tools. The Go SDK reads no field: go-sdk keeps the server's name unexported, so the vendor passes `Install` or `New` the same `*mcp.Implementation` the server was built with, and the name is its `Name`. **A name the LIBRARY invented rather than the vendor** (fastmcp's `FastMCP-<4 hex>`, the official SDK's `FastMCP` or `mcp-server`) is not a name and MUST fall back to `<vendor_id>_annotate`; fastmcp's is minted per construction, so deriving from it would rename the tool on every restart. A derived name MUST be rejected in favour of `<vendor_id>_annotate` if it would push the §5.1.2 server-instructions render past its length cap — the tool name is interpolated there and so shares a budget with `vendor_display_name`, and a name the SDK CHOSE must never be what stops a vendor's server from starting. Capping the slug is not sufficient on its own: no rule bounds `vendor_display_name`, so the check must be against the rendered result. (A name the vendor passed EXPLICITLY is not degraded — it raises, naming both knobs.) Resolution MUST NOT raise: a cosmetic label may not stop a vendor's server from starting. Underscore namespacing — the name MUST match `^[a-zA-Z0-9_-]{1,64}$`. Dots are NOT permitted: Claude Desktop and other runtimes reject tool names containing dots, so the dot-namespaced form (`acme.annotate`) is unusable cross-runtime even though it reads naturally. Vendor MAY override via `VendorConfig.annotation_tool_name` if their internal naming differs, but the override MUST satisfy the same pattern.
- **Tool description**: vendor-branded, templated from `VendorConfig.vendor_display_name`. MUST NOT contain the string "Baton" or any reference to the SDK by name. See §5.4.
- **Signature:**
  ```
  <vendor>_annotate(
    user_goal: string,
    what_happened: string | null = null,
    tool_name: string | null = null,
    suggested_improvement: string | null = null,
    context: object | null = null,
  ) -> { ok: true }
  ```
  - `what_happened` is the report: one or two plain sentences in the agent's own words saying what it asked for, what came back, and why that was unusable. **A filled `what_happened` is what makes an annotation a report (§11.4).** A value that is empty or only whitespace counts as absent. The tool text MUST ask for the agent's own words and MUST NOT offer a list of categories to pick from: sorting reports is the collector's job, not the agent's (§11.5.5).
  - `tool_name` is the ONE tool the report is about. It has three states: a name; "no tool", which is the agent SAYING no tool exists for what the user asked; and null, which is nothing stated (every report from a producer older than 0.8.15). The agent-facing text asks for the word `none` for "no tool", and a producer sends what the agent wrote: it MUST NOT rewrite the value, and MUST NOT turn an empty string into null. A producer that only asks for reports MUST list `tool_name` under `required` in the annotation tool's advertised schema, so that an agent with no tool to name says so rather than leaving the field out; it MUST still take a report that omits it. A description that says "required" is not enough: measured 2026-10-09, with the field described as required but not listed, no agent sent `""` in six sessions with no tool to name and four left the field out; with it listed, one of six left it out. Asked for an empty string, no agent sent one in twelve sessions (three sent two quote characters, four sent `none`), which is why the text asks for the word `none` and a reader normalises (§11.5.5). It is never a list: an agent with a problem on two tools files two reports.
  - There is no `signal_type` parameter. A producer from `baton-sdk` 0.8.15 MUST NOT advertise one and MUST NOT send one. A producer that only asks for reports (§5.1.2, the default) MUST refuse a call with no `what_happened`, and the refusal text MUST name only parameters the agent can see.
  - `user_goal` is written as the injected param asks (§11.5.1 rule 2: `'<n>: <what this call is for>; success is <what its result should show>'`) and lands in the payload's `intent`. The tool takes no `expected_result` or `overall_task` since the single-field change (§13, 0.8.22), so an annotation from this SDK leaves `expected_outcome` and `workflow` null; older producers still fill them. An annotate call that still sends either name is served and the value dropped.
  - `suggested_improvement` is the agent's proposed product change. It was originally a free-form `context.*` key, promoted to top-level after empirical validation showed consistent recurrence.
  - `context` is a free-form JSON object for any other structured information the agent thinks would help. The SDK records `context` verbatim (subject to PII scrubbing per §7) and sends it on the wire in the annotation payload (§11.4).
  - **Informative — common `context` keys observed in the wild (validated 2026-05-13 spike, single-data-point caveat):**
    - When no tool covers the request: `requested_capability` (what the agent wished existed), `suggested_tool_signature` (a typed function signature the agent proposes), `why_existing_tools_dont_fit` (the agent's reasoning about gaps in the current tool surface).
    - When a call went wrong: `likely_cause`, `user_impact`, `error_class`, `downstream_blocked`.
    - For multi-step workflows: `plan`, `target_date`, `confidence_in_intent`.
  - `note` is **not a recognized field** — it was considered and rejected because `context.*` keys (`likely_cause`, `user_impact`, etc.) subsume it.
- The calling agent MAY call the annotation tool at any time during a session.
- A report needs no failing tool call before it.

#### 5.1.2 Server-level instructions (load-bearing)

The MCP spec defines a server-supplied `instructions` string that clients SHOULD surface to the calling LLM (Claude Code folds these into the system prompt; other compliant clients do similarly).

Under `report_mode="on"` the SDK MUST set the FastMCP server's `instructions` to motivate annotation tool use: one sentence pointing at the annotation tool, then the subagent sentence. The tool's description carries the report triggers, the recovery rule and the fields, so each rule is written once. The SDK does not ask for an annotation before a tool call, and its annotation tool refuses one (§5.1.1): each call's intent rides its own `tool_call_start`. Under `report_mode="off"` (the default) the SDK's text is the subagent sentence alone, `INTENT_INSTRUCTIONS` (§5.6), with the same placement below the vendor's text. The sentence stays because a main agent that delegates before loading the tools never reads the `user_goal` description, so without it the subagent cannot number its calls. It is removed when a tool declares its own `user_goal`, as under `"on"`, and the SDK's text is then empty. `intent_param_mode` is `"required"` (the default) or `"optional"`; the SDK refuses `"off"`, because nothing else would capture intent. Behavior:

- If the vendor has NOT set `instructions` before installing the SDK: SDK sets a default template (see §5.4).
- If the vendor HAS set `instructions`: SDK appends its template *below* the vendor's existing text (vendor's instructions stay primary; SDK's are additive). The vendor's text is kept byte for byte as the start of the result, followed by one blank line and the template. Empty or whitespace-only vendor text counts as not set.

**Where `user_goal` is removed.** For a tool whose schema it changed, a producer MUST remove `user_goal`, and any retired name it reserves for that tool, from a call's arguments in its outer `tools/call` seam, before any vendor code or any input check by the host sees them. A tool the producer does not know may declare those names itself, so the producer leaves them for its handler wrap. A host that checks arguments against the tool's schema would otherwise reject the call, and `"required"` would become enforced. Under `"required"` the name is listed as required in what `tools/list` returns, and a call that omits it is still served. A producer whose host gives it no outer seam MUST make sure the host's input check accepts the name.

The instructions text MUST be templated from `VendorConfig.vendor_display_name`. It MUST NOT contain the string "Baton" or reference the SDK by name. The default template is in §5.4.

**Truncation hazard (length cap).** Some runtimes truncate the surfaced `instructions` string: Claude Code cuts it at ~2087 characters, which can drop the rendered text mid-sentence and silently disable the load-bearing motivation. Because `vendor_display_name` and `annotation_tool_name` are interpolated into the template, a long value can push a rendered instructions block over that limit. The SDK MUST fail loudly at install time rather than ship a truncated block: it enforces a 1500-character safety cap on the rendered output (margin below the ~2087 runtime limit) and raises a `ValueError` directing the integrator to shorten `vendor_display_name` / `annotation_tool_name`. Implementations that append to an upstream server's existing `instructions` (rather than replacing it) apply the same cap to their appended suffix. When the vendor's text and the suffix together pass the runtime limit, the SDK MUST NOT raise, because the vendor's own instructions are a valid configuration, and MUST log a warning that names both lengths: the runtime then cuts the SDK's suffix, never the vendor's text, which is why the vendor's text comes first.

**Why both pieces are required (empirically validated):**
- Tool description alone: in the spike, Claude Code did not call the annotation tool across multiple unprompted attempts. Tool descriptions are read at tool-selection time, not at tool-use-decision time.
- Server-level instructions alone (no annotation tool): nothing to call.
- Both together (on a client that surfaces `instructions`): agent calls annotation proactively before vendor tool calls AND reactively after errors, with high-quality content (a usable account of what happened, a useful `suggested_improvement`).

**Why this shape:** the annotation tool is discoverable via standard `tools/list`; the instructions provide the use-time motivation; LLMs interact with tools as their only structured output channel; both mechanisms are standard MCP primitives requiring no transport extensions; vendor-controlled (whitelabel preserved); works across all MCP clients that respect the spec's `instructions` field.

#### 5.1.3 Per-runtime support matrix

The `instructions` field is part of the MCP spec but **clients are not uniformly compelled to surface it to the calling LLM**. Empirical testing produced this matrix:

| Runtime | Surfaces `instructions` to LLM? | Unprompted annotate behavior | `_meta` carried by client | Lazy tool loading? | SDK guidance |
|---|---|---|---|---|---|
| Claude Code | **Yes** (folded into LLM context) | Proactive + reactive, all top-level fields populated, zero duplication | `claudecode/toolUseId` (per-call UUID) + `progressToken` (per-request int) | No — eager `tools/list` at init | Default path; SPEC §5.1.2 instructions are sufficient. |
| Claude Desktop | **No** (silently ignored or filtered before reaching the LLM) | None unprompted. When explicitly told (e.g., "call `acme_annotate` first with intent and expected_outcome"), it calls correctly with `workflow` populated; but does NOT populate `suggested_improvement` proactively. Note: packing behavioral guidance into the *tool description* (vs. server `instructions`) was tested as a workaround and does NOT recover annotation behavior on Desktop — tool descriptions are documentation surface, not priming surface, on this runtime. | **None** — `_meta` always absent | Yes — Desktop loaded tools per-call ("Used [server] integration, loaded tools" surfaced in the UI on each tool use) | Annotation is opt-in only. Stronger Desktop-side mechanisms (vendor-supplied user-onboarding line, etc.) are deferred. |
| Cursor (Agent / Composer) | **Yes** (folded into LLM context) | Proactive + reactive, all top-level fields populated (`workflow`, `suggested_improvement` with concrete actionable content), rich context without duplication — equivalent to Claude Code behavior | `_meta.progressToken` only (per-request int). No Cursor-specific stable correlation key observed | Unknown — not directly tested | Default path; same as Claude Code. SPEC §5.1.2 instructions sufficient. |
| Cowork / ChatGPT Desktop / other | Unknown | Unknown | Unknown | Unknown | Test before relying on. Add a row here when validated. |

**Runtime coverage note:** the SDK ships server `instructions` because they help where supported and don't hurt where ignored. Graceful degradation: a Desktop user who connects to a Baton-wrapped vendor with `report_mode="on"` still gets the annotation tool available; their agent just won't proactively use it without explicit prompting.

### 5.2 Client-attached: the MCP `_meta` field

For `session_id` and `runtime_metadata` fields, the SDK SHOULD read from the MCP JSON-RPC `_meta` field on incoming tool-call messages. `_meta` is the spec-sanctioned side-channel for runtime/correlation context.

**Recognized keys: none.** The SDK reads no `io.baton/*` key.

> **Removed 2026-09-09.** This section previously specified four `io.baton/*` keys a client could set to drive SDK behaviour — `session_id`, `agent_runtime`, `mcp_transport`, `vendor_app_version`. **Nothing ever sent any of them**, on any transport, by any client, in any repo; and no `instructions` text ever told a client they existed, so the only way to discover one was to read this section. Two of the four were never read by any adapter either — a wire contract describing a mechanism on both ends of which sat nobody.
>
> They were removed for a reason stronger than disuse. `agent_runtime`'s override cost a real defect: the key was documented here in two contradictory spellings (this table said `io.baton/agent_runtime`, prose below said `_meta.baton.*`), the implementation followed the prose, and with no users there was nobody to notice for as long as it stood. `session_id` fell to **the join rule** — the SDK keys correlation on an identifier it mints and on nothing else, so a client-supplied session handle cannot be a grouping key at capture time.
>
> **Values are still captured.** `runtime_meta` forwards the whole `_meta` dict unchanged, so anything a client does send — including these keys, should one ever appear — arrives at the Console as data. Removing a key from this table stops the SDK *keying* on it; it does not stop the SDK *carrying* it.
>
> If a future need is real — the strongest candidate is a gateway asserting the true agent behind a `clientInfo` that names the gateway — reintroduce the key deliberately, together with a mechanism that tells clients it exists.

**W3C trace context keys** (standardized in MCP 2026-07-28 per SEP-414):

| `_meta` key | Where it lands on the event |
|---|---|
| `_meta.traceparent` | stays in `runtime_meta` with the rest of `_meta`; `trace_context` is reserved and not sent (§3.1). Carried as data only, **never a `session_id` source**: §3.4 rung 1 was retired 2026-09-09, because a trace spans one turn, not one conversation |
| `_meta.tracestate` | stays in `runtime_meta` with the rest of `_meta` |
| `_meta.baggage` | stays in `runtime_meta` with the rest of `_meta` |

**Fallback:** MCP clients populate their own keys, not ours, so the SDK MUST synthesize sensible defaults:
- `session_id`: a UUID generated per MCP session.
- `agent_runtime`: `"unknown"`.
- `mcp_transport`: detected from the SDK's own transport.

The SDK MUST NOT require the MCP client to populate `_meta`. Graceful degradation only.


**Per-runtime adapters (empirically validated):**

Different MCP clients populate `_meta` very differently. Observed behavior:

| Client | Key | Stability | SDK use |
|---|---|---|---|
| Claude Code | `_meta["claudecode/toolUseId"]` | Per-tool-invocation (changes between calls) | NOT a session_id substitute. MAY surface in `runtime_metadata` for vendor-side correlation with Claude Code's own tracing. |
| Claude Code | `_meta.progressToken` | Per-request integer, sortable within a session | MAY use as a session-relative call ordering hint. |
| Claude Desktop | **none** | n/a | Desktop does not populate `_meta` at all. SDK MUST rely entirely on synthesized fallback (SDK-generated UUID session_id). |
| Cursor | `_meta.progressToken` only | Per-request int | MAY use as a session-relative call ordering hint. No Cursor-specific stable correlation key (no `cursor/*` namespace observed). SDK MUST rely on synthesized session_id for cross-call correlation. |
| (Future runtimes) | TBD | TBD | Add per-runtime adapters as discovered. |

**MCP spec evolution note (2026-07-28 release candidate, ships July 28, 2026):** SEP-2567 removes the protocol-level session for streamable HTTP (`Mcp-Session-Id` header gone). On stdio, process lifetime continues to provide implicit session scoping. On streamable HTTP under the new spec, the SDK relies on the layered fallback in §3.4, which ends on rung 5: one session per request (`per-call` mode). `traceparent` (SEP-414) is forwarded in `runtime_meta` and keyed on by nothing (§3.4 rung 1).

**`agent_runtime` resolution (updated 2026-09-09).** The SDK reads the client's DECLARED identity where it exists, and falls back to the per-runtime key heuristic above. In priority order, first hit wins:

| # | source | carrier | notes |
|---|---|---|---|
| 1 | `_meta["io.modelcontextprotocol/clientInfo"].name` | the request | Reserved by MCP 2026-07-28. Empty from every AGENT client measured — a current Claude Code negotiates `2025-11-25` — but **live traffic on some client LIBRARIES**: fastmcp 4's `Client` negotiates `2026-07-28` and writes this key itself on every request (measured 2026-09-09). Where it is the library writing it, a caller cannot forge this tier: its value is overwritten with the client's own declaration. |
| 2 | the `initialize` handshake's `clientInfo.name` | the connection | **The tier that resolves in practice.** Read from the session's cached handshake params at tool-call time; no `initialize` hook is involved, and old-spec and new-spec clients both land here. |
| 3 | the per-runtime key heuristic (table above) | the request | Inferred. Demoted below both declarations, and near-unreachable in practice: every real client declares something, and the one client this heuristic knows declares the same answer anyway. |
| 4 | `"unknown"` | — | Not a knob. `VendorConfig.default_agent_runtime` was removed 2026-09-09. |

**Declared outranks inferred.** A key prefix such as `claudecode/` says where the *metadata* originated, not who is calling — a proxy forwards `_meta` verbatim — while `clientInfo` is the client stating what it is.

**Two limits on what rungs 1-2 prove.** A declaration names the immediate MCP client, so behind a gateway it reports the gateway; and it is self-asserted, never attested — a client chooses its own `clientInfo`. Attested identity is `principal` where `principal.source` is exactly `attested`, a different field on a different condition — and an `asserted` principal is not attested at all. Rungs 1 and 2 carry client-supplied text and are scrubbed and length-capped (128); rung 3 returns an SDK-owned constant and is neither.

**`VendorConfig.default_agent_runtime` is REMOVED** (2026-09-09). A vendor set it once at install, for every connection, so it could only be right in a single-client deployment — and with the declared tiers in place it would assert a runtime over a client that had just named itself. Nothing anywhere set it. When no tier answers, the event reports `"unknown"`, which is also what `surface_snapshot` carries: that event describes the SERVER and is captured outside any tool call, so it has no caller to name.

**This ladder is LEGACY.** It describes what a producer that predates `client_observed` (§11.4) puts in `agent_runtime`. A producer that emits `client_observed` MUST NOT run it: it sends `agent_runtime: "unknown"` on every event and leaves naming the client to the consumer, which holds every input this ladder read — the declaration in `client_observed.info`, and the `claudecode/*` keys in `runtime_meta`. The field stays in the envelope, with that one value, so a consumer or schema that requires it keeps accepting the event.

There remains no way for a caller **or a vendor** to *override* the resolved value — no key or config setting replaces it. That is not the same as trusting it: rungs 1 and 2 report a name the client chose for itself, so a client can call itself anything, including another product's name. Treat `agent_runtime` as self-reported, never as attested; attested identity is `principal` where `source` is exactly `attested` (§11.4), on a different condition. Both declared rungs are passed through `VendorConfig(scrubber=...)` and truncated at 128 characters, since they carry client-supplied text onto every event; rung 3 returns an SDK-owned constant and is neither.

**Implementation note (from spike):** FastMCP exposes `_meta` as a structured `Meta(...)` pydantic-like object via `context.fastmcp_context.request_context.meta`, not as a plain dict. The SDK MUST call `.model_dump()` (or equivalent) before reading keys, and MUST treat the dict as forward-compatible (unknown keys ignored, no schema validation).

### 5.3 Fallback: nulls

If §5.1 supplied no agent-emitted values, the SDK MUST set `intent` and `expected_outcome` to `null`. The event remains valid.

### 5.4 Whitelabel obligations

Any text the SDK surfaces to the **calling agent** (tool descriptions) or to the **end user** (elicitation prompts for consent per §9) MUST be templated from `VendorConfig.vendor_display_name`. The strings "Baton" and any reference to the SDK's own brand MUST NOT appear in these surfaces.

Where SDK-branded strings MAY appear:

| Surface | Whitelabel required? | Rationale |
|---|---|---|
| Tool name (`<server-name-slug>_annotate`, or `<vendor_id>_annotate`) | yes | Visible in `tools/list` |
| Tool description | yes | Read by the LLM |
| **Server `instructions`** (§5.1.2) | **yes** | **Folded into the LLM's context by compliant clients (Claude Code confirmed). Load-bearing for §5.1 — without it, agents don't call the annotation tool.** |
| Elicitation prompts | yes | Shown to the end user |
| Synthetic retry-surfacing responses | yes | Read by the LLM and possibly surfaced to user |
| Internal vendor logs | no | Vendor developer reads them |
| `User-Agent` HTTP header on outbound Console calls | no | Network-inspectable only |
| Misconfiguration exceptions (`baton.errors.*`) | no | Raised at vendor integration time |
| Import statements (`from baton import ...`) | no | Vendor reads source code |

Default templates the SDK ships (vendor MAY override individual strings via `VendorConfig.text_overrides`):

> ⚠ The block below is the original proactive wording and is not kept current. The shipped text is in `src/baton/integrations/_llm_text.py`; §5.1.2 describes its shape.

```
annotate_tool_description:
  "Attach context before or after calling {vendor_display_name} tools. Helps
   {vendor_display_name} understand what your user is trying to accomplish and
   surface any friction. You can also use this tool to raise a signal directly —
   e.g., when you've decided the user's goal isn't reachable with the current
   tools.

   - intent: brief description of what the user is trying to accomplish
   - expected_outcome: what you expect to receive back
   - what_happened: REQUIRED on a report. 1-2 plain sentences in YOUR OWN
                WORDS: what you asked for, what came back, and why it was
                unusable. Do NOT pick a category or invent a label
   - tool_name: REQUIRED on a report. The {vendor_display_name} tool that
                went wrong. Write none if no tool exists for the request
   - workflow: optional; short label for the user's current task, repeated
                verbatim until they switch tasks
   - suggested_improvement: optional; what product change would help
   - context: optional JSON object for any other structured info — see server
     instructions for common keys"

server_instructions:
  "This server is wrapped in the {vendor_display_name} usage and friction SDK.

   BEFORE invoking any {vendor_display_name} tool, you MUST call
   `{annotation_tool_name}` and populate these top-level fields when you have a
   value for them:
     - intent (REQUIRED): one-sentence description of what the user is trying to
       accomplish
     - expected_outcome (REQUIRED): what you expect the tool to return
     - workflow (REQUIRED): a short label for the user's current task (e.g.,
       'morning meeting prep'), repeated verbatim until they switch tasks. Not
       the overall theme of the conversation

   AFTER any {vendor_display_name} tool errors, times out, returns an unhelpful
   result, or the user shows signs of giving up, you MUST call
   `{annotation_tool_name}` again and populate these top-level fields:
     - what_happened (REQUIRED, your own words, NOT a category)
     - tool_name (REQUIRED): the tool that went wrong
     - suggested_improvement (REQUIRED whenever you can articulate one): what
       specific product change would have helped — a concrete sentence about
       what would have made this work
   Then keep going: work out why the call failed, fix the input, and try
   again. Do not repeat the same failing call. Reporting is not a substitute
   for recovering.

   IF the user asks for a capability that no {vendor_display_name} tool covers
   (they ask to schedule, mutate, or take an action and no matching tool exists
   in your available tools list), DO NOT just say 'I can't do that.' Instead,
   call `{annotation_tool_name}` IMMEDIATELY with:
     - what_happened: what the user wanted and that no tool does it
     - tool_name: none (no tool exists)
     - intent: what the user wanted
     - workflow: the user's current-task label
     - suggested_improvement: a sentence about what tool/integration would help
     - context: object with `requested_capability`, plus optionally
       `suggested_tool_signature` and `why_existing_tools_dont_fit`
   Then tell the user what you can't do. Do this once per user request, not
   once per call you make to work around the missing tool.

   `context` is for SUPPLEMENTARY information not covered by the top-level
   fields above. Always populate top-level fields when you have a value for
   them; use `context` for additional structured information. Common useful
   context keys: plan, alternatives_considered, likely_cause, user_impact,
   error_class, downstream_blocked, confidence_in_intent.

   These annotations help {vendor_display_name} understand and improve the
   product. They are sent only with end-user consent."

consent_prompt:
  "{vendor_display_name} noticed {signal_summary}. Send a report to
   {vendor_display_name} so they can improve the product?"

response_surfacing:
  "{vendor_display_name} responded to a recent issue with this tool. {human_explanation}"
```

The templates above give the shape and the required content; the exact strings a producer ships are its own and are held under the length cap of §5.1.2. The instructions name the report fields and say REQUIRED; the full wording of each field ("1-2 plain sentences in your own words: what you asked for, what came back, and why it was unusable") lives in the tool's field descriptions, which have no such cap.

The `server_instructions` template is load-bearing for §5.1.2. When set as the FastMCP server's `instructions`, Claude Code reliably calls the annotation tool proactively + reactively without per-prompt prompting. Without it, agents do not call the annotation tool unprompted even with a strong tool description.

Two framing notes empirically isolated:

1. **"You MUST" + "(REQUIRED…)" markers** on each top-level field are load-bearing. Milder framing ("call with …", "should populate …") yielded inconsistent field population — agents inferred the fields were optional and defaulted to filling `context` instead. Explicit MUST/REQUIRED markers produced full population on `workflow` (including on missing-tool reports) and `suggested_improvement` (on every kind of report), with no duplication between top-level and `context`.
2. **Anti-duplication framing backfires.** An earlier instructions variant said *"do NOT duplicate top-level field values inside `context`"* — this caused agents to skip top-level fields entirely. The validated text instead frames `context` positively as "supplementary" and emphasizes top-level fields as required-when-applicable. This is the difference between making the right thing easy versus making the wrong thing forbidden — the former works, the latter doesn't.

**Workflow semantics:** observed that agents treat `workflow` as session-stable — they carry the same `workflow` value across the proactive call and the corresponding reactive call within a prompt. SDK implementations SHOULD assume `workflow` is stable across one user request, not per-call.

### 5.5 Out of scope (current version)

- Inferring intent from agent chain-of-thought or runtime reasoning channels.
- Pulling intent from agent-runtime memory stores.
- Auto-prompting the agent for intent when an unannotated tool call enters middleware.
- Detecting `slow_performance`, `abandonment`, `dead_end`, `parameter_confusion` or `feature_gap`. The SDK records what it sees and does not sort events into kinds of problem (§11.2 rule 9). Detection is collector-side (§6.4).

The first three are future candidates if the layered approach proves insufficient in practice.

### 5.6 Library API path (normative)

A library producer emits the §11.4 envelope a middleware producer emits. Where the middleware **observes** a value, the library **takes it from the vendor's code**, and the value lands in the same field. A consumer never needs to know which path produced an event to read it.

| Value | Middleware source | Library source | Field |
|---|---|---|---|
| Intent | the injected `user_goal` param (§5.1) | `trace(intent=)` | `tool_call_start.payload.call_intent` |
| Expected outcome, task | none (producers before the single-field change injected `expected_result` / `overall_task`) | `trace(expected_outcome=, workflow=)` | `tool_call_start.payload.call_expected` / `call_workflow` |
| Who is calling, and their company | the vendor's resolver | `client.session(principal=)`, which takes what the resolver may return | `principal`, `organization` |
| Which client | `clientInfo`, registered headers | `client.session(meta=, client_info=, headers=)`: a `clientInfo` in `meta` first | `client_observed` |
| Session | the §3.4 ladder | `client.session(session_id=)`, else one minted id per block, or per call outside any block | `session_id`, `session_id_origin` |
| Turn (one user request) | the client's `_meta` | `client.session(turn_id=)` | `runtime_meta["baton/turn_id"]` |
| The client's `_meta` | the request's `_meta` | `client.session(meta=)` | `runtime_meta` |
| Tool surface | the server's tool list | `client.list_tools(tools, server_info=, capabilities=, instructions=)` | `surface_snapshot` (§11.4.2) |

**The session block.** The vendor opens `client.session(...)` once per request or conversation, and every `trace()` and `annotate()` inside it takes the block's session, principal, client and turn. Only the block sets them, so a call has one place to take them from. A `session_id` passed to the block is the vendor's own and is sent as `"observed"`: an id the producer minted MUST NOT be passed back in, or its origin is mislabelled. A call outside any block mints its own session. The block sets a context variable that `trace()` and `annotate()` read, and does nothing else.

**The turn.** A `turn_id` passed to the block is the vendor's own id for the user request its calls answer, such as one run of a multi-step flow that spans many blocks. Every event in the block carries it as `runtime_meta["baton/turn_id"]`, a turn identifier (§11.4.1). The producer never makes one up.

**A vendor serving MCP itself.** A vendor that handles MCP's JSON-RPC in its own code, with no MCP library for an adapter to wrap, gets what an adapter adds from three calls:

- `client.list_tools(tools, ...)` in its `tools/list` handler. It returns the vendor's tools with `user_goal` added exactly as an adapter adds it under `intent_param_mode="required"`, and sends a `surface_snapshot` of the tools as passed.
- `INTENT_INSTRUCTIONS` in its `initialize` result's `instructions`: the sentence that carries the turn number to a subagent (§5.1.2). The annotation tool's paragraphs are not part of it. It is exactly what the adapters send under `report_mode="off"`, their default.
- `client.session(meta=params["_meta"])` in its `tools/call` handler.

`runtime_meta` holds the `_meta` the vendor passed, scrubbed and with coordinates rounded as on the middleware path, plus `baton/turn_id` when the block has one. Null when it has neither.

A library producer MUST:

1. **Put intent on the call it describes, and nowhere else.** Intent passed to `trace()` goes on that call's `tool_call_start` as `call_intent` / `call_expected` / `call_workflow`, with `intent_source: "library_arg"`. It MUST NOT also be sent as a note.
2. **Mark the session id's origin.** `session_id_origin` is `"observed"` when the vendor passed `session_id`, and `"minted"` when the producer made one. `transport_observed` is `"library"` (§11.4).
3. **Number a session's events by time.** A vendor-supplied session id can span several processes, such as a stateless API behind several workers, so a counter that restarts at 1 per process puts their events out of order. Each number SHOULD be the larger of the session's previous number in this process plus one, and the current time in microseconds, which fits the field's 64-bit integer. The number orders events and is not their identity: two processes can reach the same number in one microsecond, and §11.2 rule 5 says what a consumer does then.
4. **Apply §11.4's rules to what the vendor passes.** `principal` follows the object's rules, including absent-as-a-whole. `client_observed.headers` holds registered headers only. Every `client_observed` value is scrubbed and truncated as on the middleware path. A `session_id` or `turn_id` that is not a non-blank string is dropped. Both ids are sent as passed, without the scrubber: they are keys that events are grouped on, and a scrubber that rewrote part of an id could make two ids equal.
5. **Send no `failure_kind`.** It names a failure the producer made above the vendor's handler (§11.4.3). A library rejects nothing itself: every failure it records is the vendor's code speaking.
6. **Keep `baton/turn_id` the vendor's.** A `baton/turn_id` key inside the `_meta` passed to `session(meta=)` came from the client and is dropped. Only the block's `turn_id` writes that key.
7. **Send a snapshot as the server's, not a caller's.** A `surface_snapshot` takes no block's session, principal or client: it has its own minted `session_id`, and no `transport_observed`, `principal` or `client_observed`. It is sent once per distinct `surface_hash` per client, among the last 256 that client was sent, hashed over the tools as passed, sorted by name, before `user_goal` is added. `seam_augmentations.instructions_suffix` is null, because whether the vendor served `INTENT_INSTRUCTIONS` is not visible to the producer.

A library producer SHOULD expose the injected params' names and descriptions (§5.1.2) as data, so a vendor can add them to its own API with the same wording an agent sees on an MCP server.

**Agent reports** (`trace.annotate(...)` for a traced call, or `client.annotate(...)` inside the block) carry what the vendor's code relays from its caller, in the block's or the trace's session.

**The annotation tool as data.** A library producer SHOULD expose the annotation tool as data. That means the name, description, input schema, refusal text and `report_mode="on"` instructions an adapter registers on a server named `vendor_display_name`, and a mapping from the tool's arguments to `annotate()`'s. The mapping MUST return nothing for a call with no `what_happened`, so the vendor sends the refusal and no annotation (rule 1). `baton-sdk` names these `report_tool(vendor_display_name=)`, a `ReportTool` and its `annotate_kwargs(arguments)`.

**Out of scope on this path:** the tool list events. `trace_context` is reserved and not sent (§3.1).

**`agent_runtime` is `"unknown"`.** The library cannot see the client any more than the middleware can name it, so it sends what the vendor passes as `client_observed` and leaves the name to the consumer (§11.4). `sdk_version` already names the producer.

---

## 6. Error events and agent reports

The SDK emits events at the MCP transport boundary whether or not anything went wrong.

### 6.1 SDK-emitted conditions (mechanical, narrow)

The SDK emits a `tool_call_error` event in two cases:

1. **Explicit error / timeout** — when a tool raises or times out, the SDK emits a `tool_call_error` event.
2. **A returned result carrying MCP's error flag** — the same event, for the same reason. MCP files a failed `tools/call` as a **successful** JSON-RPC response whose body sets `isError`; a JSON-RPC error is for protocol faults, not tool failures. A producer that classifies on exceptions alone therefore files those failures as successes. See §11.4.3 for the flag's spellings and the producer rule.

The SDK records each call and does not sort events into kinds of problem (§11.2 rule 9). It does not spot a retry loop, a dead-end pattern or a call slower than a threshold.

### 6.2 Agent reports

The calling agent files a report, a call to the annotation tool (§5.1) with `what_happened` filled. The agent describes; it does not classify.

What agents report, in practice:

- no available tool fits what the user asked for;
- a tool returned `ok` but the result is unusable for the user's goal;
- the agent sees, after the fact, that it called a tool wrongly;
- an error came back with no usable reason.

The annotation event ships with the static `consent_token` from `VendorConfig` (§9), like every other event.

### 6.3 Consent and dispatch

Every event the SDK emits — including those for failed calls — ships with the static `consent_token` configured at SDK init (§9). The SDK does not currently emit per-report consent prompts before transmission; per-report end-user prompts are deferred (see §14 open questions).

Events go to the collector (§2.1). The "no auto-send without consent" guarantee today is structural — the SDK won't initialize without a `consent_token`, and every event carries it — rather than per-report interactive.

### 6.4 Collector-side detection

Collector-side behaviour. Not part of this contract.

---

## 7. PII scrubbing

The SDK runs on the vendor's side; the vendor is the data controller for what it sends. The SDK accepts a vendor-supplied scrubber that MUST be applied to every payload before it leaves the SDK process.

**Current interface:** `VendorConfig.scrubber: Callable[[Any], Any]` — a function applied to params, results, error bodies, and annotation strings. The default, since `baton-sdk` 0.2.8, is the built-in `Scrubber`: pattern rules for email, bearer tokens, `sk-*` and `AKIA*` keys, JWTs, Luhn-checked card numbers and North American phone numbers, plus redaction of every string under a field named `email`, `phone`, `ssn`, `api_key`, `token`, `secret`, `password` or `user_name`. A key matches one of these names after plain lowercasing, as Python's `str.lower()`, not case folding: `"APİ_KEY"` (`U+0130`) lowers to `"api̇_key"` and `"paſſword"` (`U+017F`) stays as it is, so neither is a field name. A vendor opts out with an identity function. Pattern rules are not a guarantee: a name or a street address passes through, so a vendor handling sensitive data SHOULD supply its own scrubber. The SDK MUST NOT log unscrubbed payloads anywhere (stderr, stdout, files, exception messages).

**Digits, word boundaries and whitespace are fixed sets.** A pattern rule's digit is `0`–`9`, and its word boundary is between ASCII letters, digits and `_` and anything else. Its whitespace is space, tab, LF, CR, FF and the non-breaking space `U+00A0`, which text copied from web pages and documents often carries. Every runtime can match these the same way, but only if each says so: Python needs `re.ASCII` and an explicit whitespace class, and JavaScript and Go need an explicit whitespace class, because each runtime's `\s` is a different set. So a card or phone number written in other digits, such as Arabic-Indic, is not matched; supporting them is later work.

**Depth limit.** A scrubber that walks nested data stops at a depth limit (10 in both reference SDKs) so hostile input cannot exhaust the stack. It MUST NOT pass what lies past the limit through unscrubbed: it replaces each object or array at the limit with the marker `"[REDACTED:depth-limit]"` and scrubs a plain value there as it would at any depth, and the producer logs a warning the first time it does so, without the value. A consumer reads the marker as "withheld by the scrubber", never as the vendor's data.

A richer rule-based interface (declarative `scrub_rules` with `redact_key` / `mask_key` / `regex_mask` and sensible defaults for emails, API-key shapes, and common credential param names) is planned — see §14 open questions.

**Withholding is not scrubbing, and it is a different promise.** A scrubber TRANSFORMS a value that still crosses the network; `result_capture` (§11.4) declares that result-derived data does not cross at all. Under `result_capture: "off"` the scrubber MUST NOT be invoked on `result` — there is nothing to transform, and calling vendor code to produce a value the producer discards can only fail. The two compose as a ladder: this section's rules are the mechanism for the rungs that transform, §11.4's declaration is the rung that does not.

---

## 8. Return channel — persistence model

### 8.1 Async out-of-band notification (current pattern)

Collector-side behaviour. Not part of this contract.

### 8.2 Locked decisions

- **No SDK-side disk persistence.** The SDK MUST NOT persist state it derives from events to disk; any cache (when one lands) MUST be in-memory only. Cross-vendor sharding and remote MCP topologies make disk persistence useless as a unified view.

### 8.4 Deferred (see §14)

- **Runtime-memory adapters.** A `MemoryAdapter` interface (`write(scope, summary)`)
  for persisting response context to the agent runtime's own memory store.
  Mechanism not yet defined.

---

## 9. Consent flow

The vendor MUST configure a `consent_token` at SDK init (`VendorConfig.consent_token`). Every emitted event carries this token in the wire envelope; the collector MUST reject events whose `consent_token` doesn't match the SDK's registered consent records.

**Current model.** The token is a single static UUID granted at SDK init time. All events from the SDK instance ship with the same token. This is acceptable for single-end-user deployments (one agent runtime, one end user) and for development. It is **not** sufficient for multi-end-user deployments where consent must be scoped per user.

**What this model does not yet provide:**
- Per-report end-user prompts before transmission (the SDK does not currently emit MCP elicitations or consent UIs).
- Per-end-user OAuth-scoped tokens.
- A consent-refusal path inside the SDK (since there is no prompt to refuse).

Vendors who need richer consent semantics today MUST implement them outside the SDK (e.g., vendor-side consent storage that gates whether the SDK runs at all for a given user). See §14 open questions for the planned per-report consent flow and OAuth/DID upgrade path.

---

## 10. Console responsibilities (informative)

The Console MUST:
- Accept event POSTs at `/v0/events`, validate bearer + consent_token, return `201 Created` (or `200 OK` with existing record on idempotent retry by `event_id`).
- Accept batches at `/v0/events/batch` as §2.5 says.
- Reject malformed payloads with `400 Bad Request` + `{ "error": { "code": ..., "message": ... } }`.
- Reject bad auth with `401 Unauthorized`.

---

## 11. Capture and event schema

The SDK is a thin event emitter. This section says what a conforming SDK does (§11.2), what each event carries (§11.4), and how a consumer pairs a call's legs and reads a report (§11.4, §11.5.5).

### 11.1 Three-layer flow

Collector-side behaviour. Not part of this contract.

### 11.2 SDK side (capture) — what conforming SDKs MUST do

A conforming SDK MUST:

1. **Emit events at the capture boundary.** On the MCP middleware path: `tool_call_start` + `tool_call_end` (or `tool_call_error`) per real tool call; `annotation` per annotation-tool invocation. On the library API path: the same event types, emitted at `client.trace.__enter__` / `__exit__` boundaries and from `client.annotate(...)` / `trace.annotate(...)` calls. Schema in §11.4 is identical across both paths.
2. **PII-scrub at event-emit time.** Raw PII MUST NOT cross the network to the Console. Scrubbing rules per §7 cover captured data — params, results, error bodies, annotation text. **Members the vendor's own code supplies are the vendor's choice as data controller (§7)**: `principal` (§11.4), whose `"raw"` `id` and `display_name` are personal data by design and say so on the wire.
3. **Buffer events locally with bounded size.** Default bound: 1000 events. On overflow: drop oldest, emit `UserWarning(events_dropped)`. The buffer is in-process per SDK instance; it MUST NOT block the vendor's hot path on remote service availability.
4. **POST events to the collector in batches** (`POST /v0/events/batch`, §2.5) asynchronously with retry-and-backoff, one request at a time. A batch goes when 100 events are waiting, when the next event would take the body over 1 MiB, or 5 seconds after the oldest waiting event was recorded, whichever comes first; what is left after a batch goes waits for its own turn. A flush or close sends at once, except during a `429` wait (§2.5). On `413` the producer splits the batch in two and sends each half; a single event that gets `413` is `refused` (rule 8). The events in the request being sent are out of the buffer's bound (rule 3): the bound counts waiting events only, so an event is never pushed out while it is being sent. A failed batch is tried again when the timer next fires, not on the next write. Default timeout: 1s per request. Circuit-break after N consecutive failures. An event can therefore reach the collector up to 5 seconds after it was recorded, and a process that stops without close can lose those 5 seconds of events (rule 8 puts that outside the count).
5. **Assign sequence numbers per session.** Increasing per `session_id` within one process; §5.6 covers a session that spans processes. A producer MUST keep counters for at most 4096 sessions. When it needs room, it forgets the least recently used session. For an observed session (§11.4 `session_id_origin`), the next number MUST be the larger of two values: the previous number plus one (0 when it has none), and the clock in microseconds. So an observed session's numbers still rise after it is forgotten. A minted session that is forgotten MAY start again at 1; its numbers then repeat (below). A per-call session (§3.4 rung 5) keeps no counter; it is numbered by the clock. A consumer orders a session's events by `sequence_number`, then `event_id`, not by timestamps alone. **The number is not the event's identity; `event_id` is.** A library producer numbers by the clock (§5.6), so two events of one session can share a number. A consumer MUST NOT reject or fail an event for a repeated (`session_id`, `sequence_number`), and recognises a retried event by its `event_id`.
6. **Withhold result-derived data on request, and say so on the wire.** When configured to withhold results (§11.4 `result_capture`), a conforming SDK MUST emit nothing **derived from the tool's result** — `result`, and an `error_body` unwrapped from the result — MUST set `result_capture` on every affected event, and MUST still record and emit `error_type`, `tool_name` and `duration_ms`. The rule is keyed on PROVENANCE, not on a field name: `error_body` is fed from several sources and only the result-derived ones are withheld, a raised message among them unless the vendor marked it public (§11.4.3). A producer MUST NOT infer the withheld state from an absent value, and a consumer MUST NOT either. ⚠ **A producer that also emits ANY of the four resource/prompt `*_error` legs, or `tool_list_error`, carries two further obligations, neither of them about wire output: its own vendor-facing docs MUST say that those legs' messages survive the mode, and it MUST emit a `UserWarning` naming them when the mode is configured (§11.4.4).**
7. **Shut down without breaking the vendor's program.** A second close waits for the first to finish, except a close re-entered from inside the first (such as a signal handler on the closing thread), which returns at once rather than wait on itself. An event recorded after close is dropped, with one warning, and never raises. At process exit the producer tries to send what it holds, for 3 seconds by default; the vendor may change the limit. The order of priority is: never get in the vendor's way, then lose as few events as possible. So:
   - An explicit close or flush returns within that same limit, and never raises a sink's error. A flush MAY return once the events recorded before it are delivered or counted; it need not wait for events recorded while it runs.
   - Close first lets event writes already under way finish, then closes the sink.
   - A call that waits on one event's write waits at most 1 second. Then the caller goes on, and the write keeps going and is still delivered. While it is outstanding, later events are queued without waiting.
   - Work still running when the producer stops is cancelled, and counted in one warning. A runtime with no exit hook, such as Go, gives the vendor a close call to make instead.
8. **Count every event it does not deliver, by reason.** Every event the producer records is either delivered or counted under exactly one of these reasons, except at close (below):

   | reason | the event was lost because |
   |---|---|
   | `buffer_full` | the bounded buffer pushed it out (rule 3) |
   | `refused` | the collector answered with a 4xx other than 429, or refused the event in a batch's results (§2.5), or the event alone is over the batch cap and was never sent (§2.5); never for a batch's `404`/`405`, and never for a `413` the producer can still split |
   | `encode_failed` | the producer could not build, scrub or serialise it before a sink took it |
   | `write_failed` | a sink took it and failed to write it, or the producer's own loop had stopped |
   | `after_close` | it was recorded after close (rule 7) |
   | `unsent_at_close` | close ran out of time before it was seen delivered |

   The counts are for the whole process and only grow. An event that is retried and then lost counts once, under the reason that ended it. **At close the count is best effort.** When close's time runs out, the producer MAY count as `unsent_at_close` any event it has not seen delivered, including one still being sent that may yet arrive, and its counts MAY be off by the events in flight then. Close never waits past its limit (rule 7) to make a count exact. It still counts an event under `buffer_full`, `refused`, `encode_failed` or `after_close` when it already knows that reason. An event sent to several sinks MAY be counted once for each sink that lost it. These names are wire values (§11.4.7). A new reason is a SPEC change, and a consumer accepts a name it does not know. Outside this rule: process kill and freeze, and an exit without close. With no exit hook nothing runs to count; with one, no `client_report` can follow a count taken then, so the producer warns about the events it leaves unsent instead of counting them (rule 7).
9. **Records, does not classify.** An SDK records what happened on each request. It does not sort events into kinds of problem.
10. **Install nothing when nothing says where events go.** When an MCP install has no DSN (neither passed nor in `BATON_DSN`) and no setting that names who sends events or where they go (`vendor_id`, `tenant_id` or a sink), it logs a warning that it records nothing, and returns a handle whose close does nothing; the vendor's server starts as usual. A setting means one the install is given; an environment variable other than `BATON_DSN`, such as `BATON_TENANT_ID`, does not count, so a stale one cannot stop a server. Every other check still runs first and still fails: a server of the wrong kind, a DSN that does not parse, or a setting whose value is not valid. A check that depends on the identity a DSN supplies, such as the length of instructions that name the server, cannot run without one. This is Sentry's rule for a missing DSN; a server a customer runs must not fail to start over an unset variable. The library API (§5.6) is not an install and is unchanged.

A conforming SDK MUST NOT:
- Maintain session state beyond the bounded local buffer
- Correlate events across calls to spot a pattern, such as a retry loop or a dead end (rule 9)
- Implement a policy layer
- Implement egress Channels (ticketing systems, chat tools, knowledge bases, etc.)
- Block the vendor's hot path on Console availability

### 11.3 Console worker side (interpretation + egress) — what conforming Consoles MUST do

Collector-side behaviour. Not part of this contract.

### 11.4 Event schema (normative)

Additive-only from 1.0 (§13, "Stability from 1.0").

Every event has these fields:

```json
{
  "event_id": "01H4F...",                    // UUIDv7
  "event_type": "tool_call_end",             // see enum below
  "session_id": "...",                       // from layered fallback per §3.4
  "call_id": "0193f2c1-...",                 // REQUIRED on tool_call_start / _end / _error; minted per call, identical on both legs; see below
  "tenant_id": "...",                        // the account/customer; from VendorConfig
  "vendor_id": "...",                        // the wrapped vendor; matches VendorConfig.vendor_id (see note below)
  "sequence_number": 42,                     // increasing per session; by the clock on a per-call, library or observed session (§3.4 rung 5, §5.6) — NOT a mode discriminator
  "captured_at": "2026-05-19T16:42:03Z",     // SDK timestamp at emission
  "consent_token": "...",                    // from VendorConfig; see §9
  "sdk_version": "0.1.0",                    // also names the producer; see §13
  "agent_runtime": "unknown",                // LEGACY: a producer that sends `client_observed` sends "unknown" here; a consumer SHOULD NOT rely on it; see below
  "principal": {                             // optional; who the vendor resolved behind the call — absent as a WHOLE when nobody was; see below
    "id": "jane@example.com",                //   the value, exactly as the vendor's resolver returned it — UNTAGGED
    "source": "asserted",                    //   WHERE it came from — see the registry below
    "form": "raw",                           //   WHETHER it is a pseudonym or personal data, as the resolver stated
    "display_name": "jane"                   //   optional; what a person reads — the vendor chose it; see below
  },
  "organization": {                          // optional; the company the caller belongs to, as the vendor resolved it; see below
    "id": "org_acme", "source": "asserted", "form": "raw", "display_name": "Acme"
  },
  "transport_observed": "http",              // optional; what the SDK OBSERVED beneath the call, never what it concluded; see below
  "session_id_origin": "minted",             // optional; whether `session_id` names a session the client keeps; see below
  "client_observed": {                       // optional; what the client said about itself, uninterpreted; see below
    "info": {"name": "claude-code", "version": "2.1.292"},
    "headers": {"user-agent": "claude-code/2.1.292 (sdk-cli)"}
  },
  "labels": ["beta", "org_7f3k9q2"],         // optional; the vendor's names for the caller's groups; see below
  "capture": {"settings_version": 7, "audience": "beta"},  // optional; which collector settings the call ran under (§2.6); see below
  "runtime_meta": {"claudecode/toolUseId": "...", "progressToken": 1},  // optional; verbatim _meta from MCP request (PII-scrubbed); see §11.4.1
  "payload": { ... }                         // event-type-specific fields
}
```

**`principal` — who the vendor resolved behind this call (OPTIONAL object, nullable).** A person, a service account or an organisation: whatever the vendor's resolver returned. A resolver that knows the company SHOULD send it as `organization` (below) instead, which is never counted as a person. **Absent as a whole whenever no identity was resolved**, which is the common case and never an error. Four members: when the object is present **the first three are REQUIRED**, and `display_name` is optional:

| member | holds |
|---|---|
| `id` | the value itself — a pseudonym or a real identity, per `form`. **Carries no tag, prefix or scheme of any kind** |
| `source` | WHERE it came from: `"asserted"` from every current producer; `"attested"` only on events stored before §13 removed it |
| `form` | WHAT it is: `"hashed"` or `"raw"` |
| `display_name` | A name for a person to read, chosen by the vendor's resolver. Never an identifier — see below |

**`principal.display_name` — what a page shows for this principal (OPTIONAL, nullable).** The vendor's resolver returns it beside the id, and decides what is safe to show: a resolver reading an email might return the part before `@`. Absent and `null` are equivalent, and a consumer reads the value, not the key (the ruling `result` carries, §11.4.3).

- **It is personal data whenever present.** `form` classifies `id` only; a hashed `id` beside a real name is legitimate.
- **It is a label, never a key.** Two principals can share a name, and one principal's name can change.
- **Blank means absent.** A producer MUST NOT send a value that is empty after removing leading and trailing Unicode `White_Space` characters. Producer note: neither runtime's built-in strip matches that set — Python's `str.strip()` also removes `U+001C`–`U+001F`, and ECMAScript's `trim()` also removes `U+FEFF` and keeps `U+0085` (measured, 2026-10-02).
- **A usable value is sent verbatim; an unusable one is dropped alone.** A producer MUST NOT trim, truncate or rewrite the value — that would reintroduce the runtimes' disagreement. A non-string, a value longer than 128 Unicode code points, one that is not well-formed Unicode (a lone surrogate), or one containing U+0000 (which common stores refuse), is treated as absent and the rest of `principal` is still emitted; the precedence row's "unusable" refers to `id`.
- **The vendor's scrubber (§7) is NOT applied to it.** The resolver already chose what is safe to show; a scrubber that redacts names would turn every principal's label into one redaction constant.

**The grain is the vendor's**, so a collector grouping by `(tenant_id, vendor_id, principal.id)` groups at whatever grain that producer resolved, and MUST NOT assume one value is one human. It is also **not an agent-run key** — one principal commonly covers several concurrent runs, for instance every agent authenticating with one organisation's credential.

`id` was the flat envelope field `user_id` until 0.8.6, then the flat field `principal_id`, and moved in here at the release below (§13).

**Why an object, when every other field this envelope authors is flat.** Because `id` alone is not safe to read. Whether a value is a pseudonym or a real name is not a fact *about* the value that a consumer may go and look up later — it is part of what the value MEANS, and a consumer holding the id without it cannot tell personal data from a hash. Nesting makes that structural: there is no arrangement of this envelope in which an `id` arrives without its `form`, so the rule does not depend on a producer remembering to emit two fields or a consumer remembering to read both. The flat spelling was drafted first and rejected for exactly that reason — it expressed the same three facts while leaving the binding as prose, and prose is what the scheme-prefix encoding already was.

**It has one live provenance: the vendor's resolver.** A producer resolves `principal` ONLY by calling a per-request resolver the vendor configured, and emits nothing when none is configured or it has no answer. That resolver may read a bearer token the vendor's own verifier validated — a producer MAY ship ready-made resolvers that read the token's `sub` or `email` claim — but the producer cannot see what a resolver did, so every principal it emits is **asserted**: the vendor states who the user is and nothing in the protocol checks the claim. **The resolver can reach the host's whole verified-token record, as the host hands it over:** passed in (`authInfo` in the TypeScript SDK, `auth.TokenInfo` in Go), or through the host's own accessor, called from inside the resolver (`get_access_token()` in Python). A producer MAY also pass a part of it, such as Python's `claims`. It MUST NOT leave the resolver only that part, because which field names the person is the vendor's decision. ⚠ **Until the release recorded in §13 a producer ALSO read the verified token's `sub` on its own when no resolver answered, and stamped that `"attested"`.** That rung is removed: which claim names the person — or whether the token names a person at all, rather than a gateway's service account — is the vendor's decision, and a default the vendor never chose was making it. Which provenance an event carries is `principal.source`.

**Provenance and privacy are two facts and they ride two members.** Before they existed both were encoded in the value's scheme tag — the letter said where the principal came from, the presence of a tag said it was hashed — so `"raw"` mode, which emits no tag, dropped the provenance along with the pseudonymity, and no consumer could recover it. They are now `source` and `form`, independent and separately readable in every derivation mode. ⚠ **A third fact — which HMAC key produced the digest — rode the value for one more release and is now carried NOWHERE.** From 0.8.11 the tag is gone and nothing replaces it: `id` holds the value and nothing else. Why that is acceptable is the key-rotation row below.

Keep all of it apart from `agent_runtime`. `agent_runtime` is what the client *says* it is — self-asserted, never checked, and not a principal at all. `principal.id` names who the vendor's resolver said the person is. They answer different questions and they are trustworthy to different degrees.

| aspect | rule |
|---|---|
| source (attested) | **no longer produced** (§13). Was: the OIDC subject (`sub`) of the verified access token, keyed with its issuer (`iss`), read by the producer when no resolver answered. Remains registered so stored events keep their meaning. |
| source (asserted) | whatever a vendor's own per-request resolver returns — including a ready-made resolver the producer ships that reads a verified token's claim. The producer does not verify the claim and has no means to. A resolver keying on a token **MUST NOT key on `client_id`**, which names the OAuth application and is identical for every user of one app. |
| transport | The asserted source works on every transport. A resolver reading a bearer token is HTTP-only: MCP auth is ASGI middleware, so a stdio deployment has no token, and a stdio vendor's resolver names the user from whatever it authenticated out of band. |
| precedence | none: there is one source. A producer MUST NOT fall back to any identity the vendor's resolver did not return — a resolver that returns nothing, raises, or returns something unusable yields no `principal`. |
| derivation | **none: the producer sends `id` exactly as the resolver returned it.** A producer MUST NOT hash, canonicalise, scrub (§7) or otherwise rewrite it, and MUST NOT hold a key for doing so. It MAY bound its length (the reference producers truncate at 128 code points). An `id` that is blank (empty after removing Unicode `White_Space`, the same set as `display_name`'s), not well-formed Unicode (a lone surrogate) or contains U+0000 yields no `principal`. A vendor who must not send real identities derives a pseudonym **inside the resolver** and says so in `form`. |
| form | **stated by the resolver, never inferred by the producer** — not from the value's shape and not from anything configured. A resolver that states nothing has stated `"raw"`. A producer MUST emit an unregistered value as `"raw"`, the classification every consumer falls back to (below). |
| a changed derivation | A resolver that changes how it derives a pseudonym — a new key, a new function — gives every principal a new `id`. The old and new values cannot be matched. |

**A consumer MUST NOT assume `id` is a pseudonym.** With `form: "raw"`, which is what a resolver that says nothing emits, it is a real identity and carries whatever retention and residency obligations that implies. **The discriminator is `form`, and it is never the value's shape** — see below.

**A hashed `principal.id` is whatever the vendor's resolver derived, and the collector cannot reverse it.** No producer and no collector holds a key: the derivation, its secret and its stability all belong to the vendor. Whether one person keeps one value across tenants, issuers or time is the resolver's decision, and a consumer MUST NOT assume any of it.

**`principal.source` — where the principal came from (REQUIRED within the object).** Which mechanism resolved `id`, recorded independently of how the value was then derived. Registered values:

| value | the producer resolved the principal from | means |
|---|---|---|
| `"attested"` | the verified access token's `sub`, keyed with its `iss` — **read by the producer itself. No longer produced** (§13); registered so events stored before the removal keep their meaning | an identity provider checked this identity |
| `"asserted"` | a vendor-supplied per-request resolver, whatever it read | the vendor states who this is and **nothing in the protocol checked the claim**. The only value a current producer emits |

**Consumer rules.**

- **Trust an attestation only where the value is exactly `"attested"`.** New values MAY be registered here without a major version, so a consumer MUST NOT reject an event for carrying an unrecognised one, and MUST NOT treat it as any registered value. Stating the rule the other way round — trust unless `"asserted"` — grants an unrecognised future value the standing of a verified identity, which is the direction that cannot be walked back. A further source is already foreseen: a principal inferred from an alias is a guess, and must never resemble a checked id.
- The member carries no security property of its own. It is the producer's report about its own resolution, is not itself attested, and MUST NOT be used for authorization or tenancy decisions.

**`principal.form` — whether `id` is a pseudonym or personal data (REQUIRED within the object).** The privacy classification, and the ONLY thing a consumer may classify on. Registered values:

| value | `id` holds | means |
|---|---|---|
| `"hashed"` | a pseudonym the vendor's resolver derived | **the vendor's statement**, like `source`: nothing in the protocol checks that the value is a pseudonym or how it was derived |
| `"raw"` | the principal as the resolver returned it | **real identity**, carrying whatever retention and residency obligations that implies |

**Consumer rules.**

- **`id` is a pseudonym if and only if `form` is exactly `"hashed"`.** An unrecognised value is personal data. The failure that matters is personal data classified as a pseudonym, so anything unrecognised takes the safer classification, and an unrecognised value MUST NOT cause the event to be rejected.
- **A consumer MUST NOT read the classification off the value's shape.** `mailto:jane@example.com`, `acct:jane@example.com`, `urn:uuid:…` and `https://accounts.example.com/…` are all legitimate OIDC subject forms, and each reaches the wire exactly as its identity provider wrote it. A consumer testing for "letters followed by a colon" classifies every one of those as a safe pseudonym. Reading `form` instead removes that whole class of error: the classification no longer depends on what an identity provider happened to write.

**The object is all-or-nothing, and that is the guarantee it exists to give.** A producer MUST emit all three required members or omit `principal` entirely; `display_name` rides only on a complete object. There is no conformant event carrying an `id` whose `form` a consumer has to guess, and none carrying a `source` for an identity that was never resolved — a partial object is malformed, not a degraded reading. A consumer MAY therefore treat "`principal` is present" as "the classification is known", with no per-member fallback, which is the whole reason these three travel as one value rather than three fields.

**Events from earlier producers carry a flat field instead, and the shape is the vintage marker.** A producer predating this release emits `principal_id` (or, before 0.8.6, `user_id`) at the top level with no object. Reading those:

- **A flat value beginning with a RETIRED tag — `h1:`, `h2:`, `v1:` — MAY be read as `form: "hashed"`.** This is the one case where the value's shape may be read, and it is legitimate because that tag WAS the producer's declaration of pseudonymity under the rule that bound it at emission, stated in the only place the contract then offered. Without this clause every hashed principal ever emitted reclassifies as personal data at once, which is safe and wrong. It inherits the old rule's one weakness and adds none: a raw value that happened to begin with a retired tag was already misclassified under that rule, on exactly these events.
- **A flat value with no retired tag is `form: "raw"`** — personal data, per the old rule's own fail-safe.
- **`source` is recoverable only from `v1:`**, which was asserted. **`h1:` and `h2:` MUST NOT be read as `"attested"`** — see §13: two producers stamped `h1:` on principals nothing verified, so that tag names two populations the wire never separated.
- **These rules MUST NOT be applied to an event carrying the object.** They expire when no producer predating it remains.

**Events from 0.8.10-era producers carry the OBJECT with a TAGGED `id`, and here the VALUE is the vintage marker.** A producer between 0.8.10 and 0.8.11 emits the same three members, `form: "hashed"`, and an `id` beginning `h<n>:`. Reading those:

- **`id` is NOT comparable, byte for byte, with a 0.8.11 `id` for the same principal** — one is tagged and one is not, and the digest underneath is identical. **A consumer comparing hashed ids across the boundary MUST strip a leading `h<n>:` first.** There is **no backfill**, so a collector holds both spellings of one digest indefinitely and MUST NOT read them as two principals.
- **The tag is the ONLY thing that separates the two vintages, and only in `"hashed"` mode.** A `"raw"` value is untagged under both, and that costs nothing: no key produced it either way.
- ⚠ **This is the one place a consumer may read the value's shape, and it is bounded to exactly this.** It licenses a comparison, never a classification — `form` remains the only discriminator, and a tagged `id` is not evidence of anything beyond its own vintage.
- **The strip is safe in both directions.** Two key generations never share a digest, so stripping can merge two principals only if the HMAC itself collided. These rules expire when no producer predating 0.8.11 remains.

**The two members are independent, and every combination occurs** among stored events. An attested principal may have been emitted raw, and an asserted one hashed; `form` says nothing about trust and `source` says nothing about privacy. This is the point of separating them — the encoding they replaced could not express `"raw"` plus a provenance at all.

**`form` is the only discriminator, and from 0.8.11 it is the only thing that could be one.** The redundancy that used to exist — a tag that named the key and thereby implied the form — is gone, and its removal is why the rule is now simple to state: **a value carries no facts about itself.** Whether `id` is a pseudonym is `form`, in every mode including `"raw"`, which no tag ever reached.

**`principal` goes in its own envelope member, never inside `runtime_meta`.** That dict is the client's `_meta` forwarded verbatim (§11.4.1). A claim the producer makes about its own resolution must stay separable from text the client supplied.

**`organization` — the company or workspace the caller belongs to, as the vendor sees it (OPTIONAL object, nullable).** It is the vendor's own customer. The same resolver that returns `principal` returns it, beside a principal or alone, for instance when an org-wide API key names the company but not the person. **Absent as a whole when the resolver returned none.**

| member | holds |
|---|---|
| `id` | REQUIRED. The organization's id, under every rule `principal.id` follows: untagged, sent exactly as the resolver returned it, never hashed or rewritten by the producer, bounded at 128 code points. A blank `id`, one that is not well-formed Unicode, or one containing U+0000 yields no `organization` |
| `source` | REQUIRED. As `principal.source`: `"asserted"` from every current producer |
| `form` | REQUIRED. As `principal.form`, stated for this `id` alone |
| `display_name` | Optional. As `principal.display_name`: a label, never a key, not scrubbed, and dropped alone when unusable |

- **All-or-nothing, like `principal`.** A producer emits the three required members or omits `organization`.
- **It has its own `form`.** The organization's id can be hashed while the person's id is raw, or the other way round. A value carries no facts about itself, so one `form` cannot describe both.
- **It is independent of `principal`.** Either can be present without the other. An unusable organization is dropped alone and `principal` still ships; an unusable principal does not drop the organization.
- **Its one provenance is the vendor's resolver.** A producer MUST NOT derive it from token claims, from `principal`, or from anything else the resolver did not return.
- **It is never a person.** An event with an organization and no principal has no principal.
- **It goes in its own envelope member, never inside `runtime_meta`**, for the reason `principal` does.

**Ordering.** A collector that refuses unknown envelope members rejects the whole event, so the collector MUST accept `organization` before any producer sends it.

**`call_id` — the minted per-call correlation key (REQUIRED on `tool_call_start`, `tool_call_end` and `tool_call_error`; absent or null on every other type).** The SAME non-empty value on a tool call's start and its end or error. A collector MAY reject a tool-call leg that carries none. A consumer MUST pair a call's legs on `call_id` and on nothing else. A leg with no `call_id` stays unpaired, so a start reads as unanswered and an end as an orphan. A consumer MUST NOT fall back to a client's own per-call id (`runtime_meta["claudecode/toolUseId"]`) or to order of arrival.

Five rules, each of them a mistake this project has already made or nearly made:

- **The producer MUST mint it in a local variable inside the function that emits both legs.** That makes it per-call by construction and correct across processes. It MUST NOT be derived from the JSON-RPC request id (`ctx.request_id` in both Python MCP server libraries), which restarts at 1 per connection and therefore collides under exactly the merged-session conditions where pairing already hurts.
- **It MUST be opaque** — a bare UUID. No tenant, user, session or tool name encoded, or the join key becomes a data-handling concern in its own right.
- **It goes in its own envelope field, never inside `runtime_meta`.** That dict is the client's `_meta` forwarded verbatim (§11.4.1). An id the producer minted MUST stay separable from ids it merely observed.
- **A call that pauses has one `call_id` per round.** On protocol 2026-07-28 a call can return an input-required result, and the client resumes it in a new request. Each request is a round, and each round mints its own id: the start leg comes from the first round and the end or error leg from the last, so the two legs carry different `call_id`s and stay unpaired (§11.4). A consumer MUST NOT expect them to share one. The producer keeps no state between requests, so it cannot carry the id across rounds. A field linking the rounds may be added later; `call_id`'s meaning will not change.
- **It says WHICH CALL, never WHO.** The principal is `principal.id`, a different field on a different condition. Keep the two claims apart.

**`transport_observed` — what the SDK observed beneath this call (OPTIONAL, nullable).** A record of an OBSERVATION, deliberately not of a conclusion. Registered values:

| value | the producer observed | means |
|---|---|---|
| `"http"` | an HTTP request object was reachable from the call's context | the call arrived over an HTTP transport. One process MAY be serving many callers |
| `"no-http-request"` | a live MCP request with no HTTP request behind it | stdio or an in-memory transport. One process is one caller |
| `"read-failed"` | the producer's own read of the transport raised | **nothing about the transport.** Its own value so a consumer can COUNT how often the read fails, rather than a producer defect arriving disguised as a fact |
| `"library"` | the producer is the library API (§5.6): the vendor's code called it, not an MCP request | no transport could be observed. The vendor's code MAY be serving many callers |
| absent / `null` | the producer did not look | a producer predating this field, or an event no call caused, such as a `surface_snapshot` |

**Why the values name the observation.** "stdio" is not what is measured. The absence of an HTTP request covers real stdio AND the in-memory transport, and further transports exist which do not split into one-caller and many-caller. Naming the value after what was seen keeps the inference — and the right to revise it — with the consumer.

**Why `read-failed` is not folded into `no-http-request`.** They are opposite kinds of fact. `no-http-request` is evidence about a deployment; `read-failed` is evidence about the producer. Folding the second into the first delivers a producer bug to consumers as a statement about the customer's transport, and any consumer rule that treats `no-http-request` as safe to group would then group on a defect.

**What it is for.** It decides where the `session_id` ladder ends (§3.4). Under `no-http-request` the ladder ends on a process-wide id that describes one agent and is correct to group. Under `http` it ends on rung 5, one id per request. Producers before rung 5 was built (`baton-sdk` up to 0.8.20) sent the process-wide id under `http` too, where it covers every caller of a multi-user server; this field is what lets a consumer tell those events from the grouped-safe ones.

**Consumer rules.**

- New values MAY be registered here without a major version, so a consumer MUST NOT reject an event for carrying an unrecognised one, and MUST NOT treat it as any registered value. **A validating consumer that rejects the event rather than the value discards the whole call** — its `call_id` and `principal` with it — over an advisory field.
- The field is advisory and carries no security property. It is the producer's own report about its own process, is not attested, and MUST NOT be used for authorization or tenancy decisions.

**It goes in its own envelope field, never inside `runtime_meta`.** That dict is the client's `_meta` forwarded verbatim (§11.4.1); an observation the producer made about itself must stay separable from text the client supplied.

**`session_id_origin` — where `session_id` came from (OPTIONAL, nullable).** Registered values:

| value | means |
|---|---|
| `"observed"` | the id names a session the client keeps, read from the call: rung 3 or 4 of §3.4, or rung 4b on SSE |
| `"minted"` | the id names no session the client keeps: no live rung answered and the producer used an id it made itself (§3.4's terminus), or rung 4b answered with a per-request id on stateless HTTP |
| absent / `null` | the producer did not say: a producer predating this field |

**What it is for.** Before this field, a consumer could not tell a session the client kept from one the producer made, because §3.4 forbids reading anything into the `session_id` value. Together with `transport_observed` it names the stateless shape: `"minted"` under `"http"` is a server with no session to read, where one `session_id` covers one request or one process, never one conversation.

**Consumer rules.** As for `transport_observed`: the set is open, a consumer MUST NOT reject an event for an unregistered value and MUST NOT treat one as a registered value, and any rule keyed on this field MUST match `"minted"` positively. The field is advisory and MUST NOT be used for authorization or tenancy decisions.

**Producers that predate the field.** `baton-sdk` up to 0.8.15 and `baton-ts` up to 0.5.4 send no `session_id_origin`, and on those versions an id that starts with `sdk-` is the producer's own. A consumer MAY treat such an event as `"minted"`, for those `sdk_version` values only. This is a fact about named releases, not a rule about the prefix: no producer is required to keep the prefix, and a consumer MUST NOT read it on any other version.

**Ordering.** A collector that refuses unknown envelope members rejects the whole event, so the collector MUST accept this field before any producer sends it, as with `call_id`, `principal` and `transport_observed`.

**`client_observed` — what the client said about itself (OPTIONAL object).** The raw values a producer saw that identify the calling client, recorded without interpretation. **Absent or `null` when it would have no members**; as everywhere in this envelope the two are the same on the wire, and a consumer MUST NOT test for the key. Inside the object, a member with nothing to carry is omitted.

| member | holds | present when |
|---|---|---|
| `info` | `{name, version}` exactly as the client declared them in `clientInfo` | the client declared it on this request's `_meta`, or in the handshake of this connection. Any transport |
| `headers` | a map of request header name, lower-cased, to the value sent | an HTTP request is behind the call and carries at least one registered header |

**Registered headers.** A producer copies only the headers named here, and MUST NOT forward any other: `user-agent`, `x-anthropic-client`. A header sent on several lines is joined with `", "` where the producer can see each line; a producer whose framework hands it one line sends that line. New names MAY be registered here without a major version.

**`info` is what was declared, never what was inferred.** A producer that recognises a client some other way (§3.4's `claudecode/*` heuristic) reports that in `agent_runtime` and leaves `info` absent. Within `info`, `name` and `version` are each omitted when the client did not send one.

**Every value is client-supplied text.** A producer passes each one through `VendorConfig(scrubber=...)`, as it does `_meta`, and truncates it: `info.name` and `info.version` at 128 characters, as §3.4 does the declared name, and each header value at 256.

**Which events carry it.** Every event with a live request behind it (`tool_call_*`, `annotation`, `tool_list_*`, `resource_list_*`). Never `surface_snapshot`, which describes the vendor's server and not a caller, and never `client_report` (§11.4.7).

**What it is for.** It replaces `agent_runtime` as the way a client is named. That field is one string a producer resolved, so it could not say whether a name was declared or guessed, carried no version, and ended at `unknown` on a server that never sees the handshake. This object gives a consumer the inputs, so the label is computed where it can be corrected without an SDK release. A producer that emits it stops resolving `agent_runtime` (§3.4).

**Consumer rules.**

- A consumer MUST ignore a member or header name it does not recognise, and MUST NOT reject the event for it.
- A consumer MUST read a missing object, member or header as "not observed", never as "the client sent nothing".
- Nothing here is verified. It MUST NOT be used for authorization, tenancy or identity, and it is not a principal.
- A consumer MAY use `agent_runtime` as the label only where it is not `"unknown"`. That value can only have come from a producer that predates this object and resolved the name itself.

**It goes in its own envelope field, never inside `runtime_meta`.** That dict is the client's `_meta` forwarded verbatim (§11.4.1); a header is not `_meta`, and `info` may come from the handshake.

**`labels` — the vendor's names for the caller's groups (OPTIONAL list of strings; experimental, §13).** The vendor's resolver returns them beside the principal or organization: `beta`, `enterprise`, or one unguessable key per customer that the vendor keeps (`org_7f3k9q2`). A `label` rule matches on them (§2.6). **Absent or `null` when there are none**, and never an empty list.

- A producer sends every usable label, in the order the resolver gave them, without repeats, and at most 20. A label is usable when it is a string of at most 64 Unicode code points, is not empty after removing Unicode `White_Space`, is well-formed Unicode and holds no U+0000. An unusable label is dropped alone.
- Labels are sent as given, without the scrubber (§7): the vendor chose them as names it is happy for Baton to see.
- **They are not identity.** A consumer MUST NOT read them as a principal or use them for authorization.
- **Which events carry them.** Every event that carries `principal` or `organization` can carry them, including when the resolver returned labels alone.

**`capture` — which collector settings the call ran under (OPTIONAL object; experimental, §13).** Present only when the producer applied a settings document (§2.6). Absent when the vendor manages settings in code, and before the first document is read. Members, each omitted when it has no value:

| member | holds |
|---|---|
| `settings_version` | the document's `version` |
| `audience` | the `id` of the audience that matched this caller |
| `overlay` | reserved for the hash of the overlay a call saw; no producer sends it yet |

A consumer MUST NOT trust `settings_version` as proof a document was saved: any key holder can send any value. A consumer ignores a member it does not know.

**`tenant_id` vs `vendor_id`.** Both are required and they are not synonyms. `tenant_id` identifies the **account** the events belong to (the Baton customer). `vendor_id` identifies the **wrapped vendor** the SDK is instrumenting, and matches `VendorConfig.vendor_id`. In vendor-mode the account corresponds to a single wrapped vendor (the SDK currently sets `tenant_id` to the vendor's own id). In customer-mode a single account wraps several vendors under a distinct `tenant_id`. Implementations MUST NOT assume `tenant_id == vendor_id` in general. The collector MUST reject envelopes missing either field.

Event types and their payload shapes:

| `event_type` | Payload | Source |
|---|---|---|
| `tool_call_start` | `{tool_name, params, call_intent?, call_expected?, call_workflow?, intent_source?}` (params PII-scrubbed; the optional fields carry the injected per-call params when present — §13 changelog) | SDK middleware before vendor handler |
| `tool_call_end` | `{tool_name, result, duration_ms, result_capture?}` (result PII-scrubbed; `result_capture` present only when results are withheld — see below) | SDK middleware after vendor handler returns |
| `tool_call_error` | `{tool_name, error_type, error_body, duration_ms, result?, result_capture?, failure_kind?}` (`result` PII-scrubbed; present only for the returned-flag shape — §11.4.3; `result_capture` present only when results are withheld — see below; `failure_kind` present only when the PRODUCER manufactured the failure above the vendor's handler — §11.4.3) | SDK middleware **on exception, or on a returned result whose MCP error flag is set** |
| `annotation` | `{intent?, expected_outcome?, what_happened?, tool_name?, workflow?, suggested_improvement?, context?, signal_type?}` (all nullable; agent populates what it has) | SDK annotation tool handler / library `client.annotate(...)` / `trace.annotate(...)` |
| `surface_snapshot` | `{surface_hash, server_info?, capabilities?, instructions?, tools, seam_augmentations}` — top-level fields mirror baton-proxy's `enqueue_surface_snapshot`; `seam_augmentations.intent_param` shape differs (§11.4.2) | SDK middleware/wrap layer, once per observed `surface_hash` per install, among the last 256 it sent; the library's `list_tools`, once per hash per client among the last 256 it sent (§5.6); see §11.4.2 |
| `client_report` | `{instance_id, dropped}` — this process's lost-event counts by reason; see §11.4.7 | SDK HTTP sink after a delivery |

⚠ **These six are not the whole enum.** Fifteen more are specified below: the twelve resource and prompt lifecycle types in **§11.4.4**, and the three tool list types in **§11.4.5**. `EventType` has twenty-one values; a consumer reading this table as exhaustive will reject conforming traffic, which §11.4.4 forbids outright.

**`result_capture` — the producer declares that result-derived data was WITHHELD (OPTIONAL, on `tool_call_end` and `tool_call_error`).** A vendor may configure a producer to capture no tool-response data at all. When it does, the event says so, in its own member, rather than leaving a consumer to infer it from what is missing.

- **Absent means captured.** There is no `"full"` on the wire. Absent is correct for a producer predating this member and for one configured to capture normally, and both of those genuinely captured — so a consumer needs no version table to read it.
- **Absent and null are equivalent, and a consumer MUST NOT test for the KEY.** Same ruling this section's `result` already carries (§11.4.3), for the same mechanical reason: `baton-sdk` serializes with `model_dump(mode="json")` and no `exclude_none`, so a defaulted member reaches the wire as `"result_capture": null` rather than being omitted. A consumer that reads `"result_capture" in payload` therefore marks EVERY captured call withheld against the reference SDK — the exact inversion of the member's meaning. Read the VALUE, and read `null` as absent. (Adding `exclude_none` instead was considered and rejected: it would change every optional member on every event, including the `result: null` that §11.4.3 documents as deliberate key-set parity with `baton-ts`.)
- **Registered value: `"off"`** — nothing derived from the tool's result was emitted. A **string**, not a boolean, so a partial rung (shape or length without text) can be added as a second registered value rather than a second member.
- **It rides BOTH payloads**, because the returned-failure shape withholds `result` too (§11.4.3).
- **The config knob and this member are not the same name.** How a vendor configures it is a producer concern (`VendorConfig.result_capture_mode` in the reference SDK; `baton-proxy` offers no withholding mode and never sends this member); what reaches the wire is `result_capture`. Stated once here so a third producer does not invent a third spelling — the same separation `principal_id_mode` and `principal.form` already have.

**Why a member and not an absent value.** A missing `result` already means "the handler raised, there was no result" (§11.4.3), so absence cannot also mean "withheld by policy" — the two are indistinguishable, and a consumer reading absence as "the tool returned nothing" will manufacture a failure that never happened. This is the same rule `principal` is built on: a fact ABOUT a value does not belong inside the value, and a consumer must never have to guess a classification.

**Consumer rules.**

- A consumer MUST NOT treat a withheld `result` as an empty, null or failed response.
- A consumer MUST NOT read `result_capture` as a statement about `params`. Request data is unaffected by it.
- `error_type`, `tool_name` and `duration_ms` are unaffected.

**Annotation event sub-types.** A single `event_type=annotation` carries two semantically distinct flavors. The test is one rule, and every consumer MUST use this same rule wherever it tells the two apart:

> **An annotation is a REPORT when `payload.what_happened` is filled** (non-null and not only whitespace) **OR `payload.signal_type` is non-null. Otherwise it is a NOTE.**

- **Note** (proactive annotation). Written by the agent through the annotation tool at the *start* of a logical operation, to say what it is about to attempt. Carries `intent` / `expected_outcome` / `workflow`. **A producer MUST NOT build a note from a call's own intent**: that intent rides the call as `call_intent` / `call_expected` / `call_workflow` (§5.6), and a copy adds nothing. Producers before this rule sent one such copy per session, marked `intent_source: "injected_param"`. The library before `baton-sdk` 0.8.18 sent one for each `trace()` given intent, with no mark.
- **Report** (reactive annotation). Emitted *after* a tool call's outcome is known, or when no tool fits, to flag friction. Carries `what_happened` / `tool_name` / `suggested_improvement` / `context` (and may also carry `intent` / `expected_outcome` / `workflow` for self-describing context).

The second half of the test exists for producers older than `baton-sdk` 0.8.15, whose reports carry `signal_type` and no `what_happened`, and for every such report already stored. `signal_type` on an annotation is DEPRECATED: a producer from 0.8.15 never sends it, and it leaves the schema once no older producer is live (§13).

`tool_name` on an annotation names the tool the annotation is about: on a report, the tool the agent named, or the word `none` when no tool exists (§5.1.1); on a note an older producer copied from injected params, the tool whose call carried them. It does not say who wrote the annotation. A current producer writes none itself: each comes from the agent through the annotation tool, or from the vendor's code through `client.annotate(...)` / `trace.annotate(...)` (§5.6). Of the notes older producers built, the MCP copy carries `intent_source: "injected_param"`; the library's note from `trace()` intent carries no mark.

The wire format is intentionally unified — both flavors share the same envelope so order-preserving stream processors handle them identically.

#### 11.4.1 `runtime_meta` (optional)

On the MCP path, `session_id` is NOT a conversation-turn identifier. Over stdio, unless the runtime sends its own session key (§3.4 rung 3), it is the SDK's fallback id, so one server process across many user prompts produces one `session_id`. Over HTTP it comes from §3.4's ladder and can cover a single request (rung 5).

On the MCP path the SDK populates `runtime_meta` with the raw `_meta` dict from the MCP request, with the vendor's PII scrubber applied. On the library path it holds the `_meta` the vendor passed to `client.session(meta=)`, scrubbed the same way, and the vendor's turn id (§5.6).

A key that identifies a CONVERSATION MUST NOT be used as `session_id`, including through §3.4 rung 3's adapter table, which is for session-scoped keys only and has no entries today.

`baton/turn_id` is the one key Baton defines rather than observes. It is a turn identifier by definition, whatever the client. On any event whose `transport_observed` is not `"library"` the key came from a client's `_meta`, and nothing says what that client meant by it. A library producer drops a client's copy of the key (§5.6 rule 6), so on a library event it is always the vendor's.

The SDK does NOT interpret `runtime_meta` beyond capture; on the MCP path it remains "what the runtime supplied," verbatim.

#### 11.4.2 `surface_snapshot` — vendor-true tool surface, for identity + drift

Mirrors baton-proxy's `MessageProcessor._capture_surface` / `Emitter.enqueue_surface_snapshot`. The SDK snapshots the wrapped server's **vendor-true** surface — `server_info` / `capabilities` / `instructions` (captured once, at `install_baton(...)` time, BEFORE Baton mutates instructions) plus the full `tools` list with schemas (captured BEFORE Baton's `user_goal` injection) — hashes it (canonical JSON, sorted keys), and emits `surface_snapshot` once per observed `surface_hash` per install, among the last 256 hashes it sent. A repeated capture of an unchanged surface is not re-emitted; a hash that has dropped out of those 256 is sent again, and a repeated `surface_hash` is the same surface. A library producer snapshots the tool list its vendor passes to `client.list_tools(...)`, once per hash per client among the last 256 it sent, with no `transport_observed`, `principal` or `client_observed` (§5.6 rule 7). In the Go SDK, `server_info` is the `*mcp.Implementation` and `instructions` the `ServerOptions.Instructions` the vendor passes to `Install` or `New`. `capabilities` come from the first `initialize` or `server/discover` answer the SDK sees, before it adds to it; until one is seen they are null, and a second snapshot carries them.

`surface_hash` is the identity change specs and recipes are authored against (proxy's own `base_surface_hash`). This is why the snapshot excludes anything Baton itself adds: Baton's own injected tool (the annotation tool) is omitted from `tools` and recorded instead in `payload.seam_augmentations.injected_tools`, and the schema-injected `user_goal` param never appears in `tools[].inputSchema` — toggling `intent_param_mode` MUST NOT change `surface_hash`.

```json
{
  "surface_hash": "sha256:...",
  "server_info": {"name": "acme-mcp", "version": "1.2.0"},
  "capabilities": {"tools": {"listChanged": false}, ...},
  "instructions": "...",                              // vendor's own, pre-Baton-suffix; null if the vendor set none
  "tools": [{"name": "search", "description": "...", "inputSchema": {...}}, ...],
  "seam_augmentations": {
    "injected_tools": ["acme_annotate"],
    "intent_param": {"names": ["user_goal"], "required_names": ["user_goal"], "mode": "required"},
    "instructions_suffix": true
  }
}
```

**`seam_augmentations.intent_param` has one shape in `baton-sdk`, `baton-ts`, `baton-proxy` and `baton-extmcp`** (plural `names: list[str]`). All four inject one param, `user_goal`: `baton-sdk` from the single-field change (§13, 0.8.22), `baton-ts` from 0.5.8, `baton-proxy` from 0.6.19 and `baton-extmcp` from 0.2.1. Older `baton-ts` and `baton-proxy` releases inject three (`user_goal` + `expected_result` + `overall_task`), so a consumer reads `names` as data. `required_names` is sent from `baton-sdk` 0.8.12, `baton-ts` 0.5.3, `baton-proxy` 0.6.18 and `baton-extmcp` 0.2.1. An older release sends none, so a consumer reads a missing `required_names` as unknown, never as empty. `baton-extmcp` before 0.2.1 injected two params (`user_goal` + `expected_result`) and recorded them as `seam_augmentations.injected`, not `intent_param`. The top-level fields have the same shape across the four, and so do `injected_tools` and `instructions_suffix`, but their values follow each producer's settings. With reports off, the default from `baton-sdk` 0.8.23, `baton-ts` 0.5.8 and `baton-proxy` 0.6.19, `injected_tools` is `[]`, and `baton-extmcp` always sends `[]`. `instructions_suffix` is `false` when Baton's text is gone from the served instructions: under `report_mode="off"` that text is only the subagent sentence, which a tool declaring its own `user_goal` removes (§5.1.2). A library snapshot from `baton-sdk` 0.8.23 sends `instructions_suffix` null, meaning unknown, because the vendor serves the instructions itself. `baton-extmcp` sends null until a handshake response has passed through it, because the gateway's config decides whether it sees `initialize` at all. A consumer reads null as unknown, never as false.

**Capture mechanism differs by adapter** (both converge on the same wire shape):
- `baton.integrations.standalone` (standalone `fastmcp` library) has a real `on_list_tools` middleware hook — the snapshot is captured and (if new) emitted on every `tools/list` response, using the vendor-true tools `call_next` returns before this adapter's own injection loop runs.
- `baton.integrations.official` (official SDK) exposes no such hook. The snapshot is built from data already captured during tool registration (install-time scan + the patched `add_tool` for later registrations) and lazily hashed + emitted on the next tool call — the first point every install is guaranteed to reach an async context. A server that's listed but never has a tool called on it won't get a snapshot.

`session_id` on this event is the process-level fallback session, not a per-call resolved session (mirrors proxy, which omits `session_id` on this call entirely).

#### 11.4.3 `tool_call_error` — the two failure shapes, and the flag's spellings

**A failed MCP tool call is a 200.** The protocol files it as a successful
JSON-RPC response whose `CallToolResult` body sets the error flag; a JSON-RPC
error means a protocol fault, not a tool failure. So a tool call fails in two
shapes, and a producer MUST emit `tool_call_error` for both:

1. **RAISE** — the vendor handler raises. The producer holds a live exception.
   `error_type` is the exception class name, `error_body` its message, and
   there is no result object to record. ⚠ **A class-name `error_type` means the
   handler raised ONLY when `failure_kind` is absent or null.** When that member
   carries a request-side value the handler never ran, and the class name
   describes a failure the SDK made above it — on MCP 2.x an unknown or disabled
   tool arrives as `"ProtocolError"`. A consumer MUST read `failure_kind` before
   concluding anything about the handler from this member. **Under `result_capture: "off"`
   (§11.4) the message is withheld unless the vendor marked it public.** A
   raised message is the tool's answer on the failure path, and it carries the
   same data a returned one does. The mark is the MCP library's tool-error
   class (`ToolError` in Python's `mcp` and `fastmcp`): a vendor raises it to
   give the agent a message, and `fastmcp`'s `mask_error_details` lets it
   through for that reason. So under `"off"`:
   - the vendor's exception is that class → `error_body` is its message, and
     the event carries no marker;
   - any other exception → `error_body` is `""` and `result_capture` is
     `"off"`. `error_type` and `duration_ms` are sent as in `"full"`.

   "The vendor's exception" is the one the handler raised. A producer whose
   host wraps every exception in its own tool-error class (both Python
   libraries chain the original as `__cause__`) reads the original. A
   tool-error the vendor chained onto another exception is withheld: the
   chain says its text may describe the cause. A producer whose host has no
   such class withholds every raised message, unless it gives the vendor its
   own type to raise for that purpose. A message raised with that type is
   kept, exactly as the host's class would be.
2. **RETURN** — the handler returns normally, and the result carries the error
   flag. `error_type` is the registered value **`"tool_error"`**, `error_body`
   is the human-readable reason unwrapped from the result's `content` text
   parts, and `result` carries **the full result envelope**, PII-scrubbed and
   **not unwrapped** to the developer's return value the way `tool_call_end`
   unwraps it. The envelope is what holds the flag and the reason; on an error
   result `structured_content` is typically `null` and the text lives in
   `content`. **Under `result_capture: "off"` (§11.4) BOTH
   are withheld**: `result` is not emitted, and `error_body` — which on this
   shape is unwrapped FROM the result — is emitted as the empty string, because
   it is a required member of this payload and cannot be dropped. An empty
   `error_body` is therefore ambiguous on its own; `result_capture` is what
   distinguishes "withheld" from "the failure carried no message".

**⚠ On the LIBRARY API path both members are vendor-supplied, so this subsection's shapes describe the middleware only.** `Client` / `AsyncClient`'s trace takes `observed(error_type=..., error_body=...)` from the vendor directly: nothing is unwrapped from a result, so under `result_capture: "off"` that `error_body` is KEPT — the vendor's own code passed it, by name, to the call that records it. **Nothing on a library-path `tool_call_error` is result-derived, so it carries NO marker**, and a `"off"` error event there is byte-identical to a `"full"` one. That is §11.4's rule ("present only when results are withheld") applied, not an exemption from it: the reference SDK never sets `result` on this payload either, so there is nothing to withhold and nothing to declare. The marker on this path rides `tool_call_end` alone.

`error_type` here is likewise whatever the vendor passed, so on the library path it is not evidence of which shape produced the event — a vendor may pass `"tool_error"` for a failure they classified themselves. That is not new; the member has always been vendor-supplied there. `result_capture` is simply the first rule whose reading depends on it, which is why it is stated rather than left to be re-derived.

**`failure_kind` — the producer NAMES a failure it made ABOVE the vendor's handler (OPTIONAL, `tool_call_error` only).** The two shapes above are both the vendor's code speaking: it raised, or it returned a flag. A third class exists and is neither — the SDK rejected the call before the handler ran, or ran it and then rejected its output. `error_type` cannot carry this, because clauses (1) and (2) above make it the RAISE/RETURN discriminator — an exception class name or `"tool_error"` — and these are not a point on that axis. So the producer says what it did, in its own member, rather than leaving a consumer to pattern-match the message.

| value | shape | `error_body` under `result_capture: "off"` |
|---|---|---|
| `unknown_tool` | the handler never ran — no tool by that name | **stays** |
| `tool_disabled` | the handler never ran — the tool exists and is off | **stays** |
| `invalid_argument` | the handler never ran — arguments rejected | **stays**, scrubbed: it can echo argument values |
| `output_schema_mismatch` | the handler RETURNED and the server's check of its output rejected it | **goes** — the message describes what the tool returned |
| `refused` | the handler never ran — code above it refused the call, such as a server subclass's own `call_tool` checking auth or credits, or the producer cannot tell why | **goes** — the message is the vendor's own code speaking |

- **Absent means the vendor's handler spoke for itself** — it raised, or it returned an error. Absent is therefore right both for a producer predating this member and for every failure the vendor's own code produced, so a consumer needs no version table.
- **Absent and null are equivalent, and a consumer MUST NOT test for the KEY.** Same ruling, and the same mechanical reason, as `result_capture` (§11.4): `baton-sdk` serializes with `model_dump(mode="json")` and no `exclude_none`, so a defaulted member reaches the wire as `"failure_kind": null`. Read the VALUE.
- **A string with registered values, not a closed enum**, for the reason `result_capture` is one: a later registered value must not turn a conforming producer into a `ValidationError` on a path §11.2 requires to fail open. Additive-only, like §11.6's action vocabulary.
- **`tool_call_error` only.** A producer that files one of these as `tool_call_end` is reporting a success the caller never saw; correcting that is what this member is for, so it has no meaning on a success payload. This is the one difference from `result_capture`, which rides both.
- **Send a narrower value only when the producer saw it happen.** A producer that knows the handler never ran, but not why, sends `refused`. It MUST NOT guess `invalid_argument` or any other value. Only a producer with two seams knows the handler never ran (below); one with a single seam sends no value at all, except as the host-check exception below allows.
- **A producer MUST NOT sort these by inspecting the result object.** All four reach the producer as the same shape — a `CallToolResult` with the error flag set, or a thrown protocol error. The discriminator is whether the producer's own inner capture ran: it did not → the handler never ran → request-side; it did, and the failure was raised above it → the handler returned → result-side. A producer with only one seam cannot emit this member correctly and MUST omit it, except as the host-check exception below allows.
- **Exception: a host check with a fixed message.** A producer with one seam MAY name `invalid_argument` or `output_schema_mismatch` when its host library rejects the call with a fixed message, only for tools the host checks, and a test on every host version the producer supports pins each message. This is the one case where reading the failure is allowed, and it has a cost: a tool that returns the same words is mislabelled. So under `result_capture: "off"` such a producer withholds the text of both, whatever the table above says. The Go SDK does this for go-sdk's typed tools, whose argument and output checks run inside the tool's handler, below any middleware: `validating "arguments": ` is `invalid_argument`, and `validating tool output: ` or `marshaling output: ` is `output_schema_mismatch` for a tool with an output schema. From go 0.2.0 it also names `refused` for a call that the vendor's middleware, passed in `Config.Middleware`, stops; middleware the vendor adds outside `Config.Middleware` is not observed.
- **A producer MUST NOT synthesise `output_schema_mismatch` for a failure its host server did not reject.** This value means the MCP server the producer runs inside rejected the handler's output. Where that server ACCEPTED it and something downstream rejected it, the producer observed a SUCCESS — and a sensor that contradicts the host it is embedded in is fabricating a fact rather than recording one, the same principle as the one-seam clause above. ⚠ **On standalone `fastmcp` the line falls between `mcp` majors, not inside one SDK.** `fastmcp` converts output inside the middleware chain (`Tool.convert_result`, within `Tool.run`, within the chain's `call_next`) and rejects a result that cannot be structured content at all: a tool declaring `dict[str, int]` whose handler returns a list raises `structured_content must be a dict or None`, which reaches a middleware's own `except` and is a `tool_call_error` needing no new member. `fastmcp` itself does not check VALUES. On `fastmcp` 2.x and 3.x, which run on `mcp` 1.x, the low-level `mcp` server does: after the middleware chain returns it validates `structuredContent` against `outputSchema` and returns `isError` with `Output validation error: …` for wrong values (`{"a": "oops"}`) and for a missing structured result. `baton-sdk` names that `output_schema_mismatch`: its outer `tools/call` seam sees the error result after its inner capture saw the handler return, and the inner capture's terminal event waits for it. ⚠ That discriminator is "the server answered with an error after the handler returned", so on `mcp` 1.x an error result made by a vendor middleware placed outside Baton's is named the same; this is accepted. On `mcp` 2.x no such check runs, so nothing is named there. On `fastmcp` 4.x the `mcp` 2.x low-level server has no output check, so the VALUE class passes the server and is invisible to every server-side seam; **that is the case this clause forbids naming.** Measured on `fastmcp` 2.14.7 (`mcp` 1.30.0), 3.4.1 (`mcp` 1.27.2) and 4.0.3 (`mcp` 2.2.0). ⚠ **And what catches the value class downstream is CONDITIONAL, so no producer may rely on it either:** the Python MCP client validates only when it has listed the tool and logs that it cannot otherwise (`mcp/client/session.py`, both majors), and a non-Python client need not validate at all. A vendor shipping a value-violating tool behind a non-validating client gets no error anywhere. The official `mcp` server does check values, inside `Tool.run` — `output_model.model_validate(...)` on 1.x, `self._output_adapter(output_model).validate_python(...)` on 2.x, where `output_model` is `Annotated[Any, …]` and carries no `.model_validate` (measured 1.20.0 and 2.2.0) — so that surface does OBSERVE the value class. ⚠ Whether it may NAME it is decided against by the one-seam clause above, not by this one: a single-seam producer's only discriminator is the exception's `__cause__`, which that clause forbids relying on, and a vendor handler that itself raises a pydantic `ValidationError` is indistinguishable from an output-schema mismatch. **Nothing in this subsection obliges any producer to emit this member.** A producer that cannot observe a class of failure MUST say so in its own vendor-facing documentation — the DOCUMENTATION half of §11.4.4's withholding duty and not its `UserWarning` half, for that half's reason: the fact is not assertable from any payload, so the text a vendor reads while wrapping is the only place it can reach them.
- A tool that returns "you do not have access" is the vendor's own prose and carries no `failure_kind`. The same words from code that refused the call before the tool ran carry `refused`: the producer names WHERE the call stopped, not what the message means.

**`error_body` is fed from several provenances, and only the result-derived one is withheld.** The two shapes above are the SDK's; a middlebox producer has more. `baton-proxy` fills this member from four places — the upstream's JSON-RPC `error.message` (the wrapped server failed rather than returned, so it is the RAISE analogue; a proxy sees no exception class, so it would be WITHHELD), text unwrapped from a returned result (WITHHELD), strings the proxy itself authors for a dropped or unanswered call (KEPT — they describe the producer, not the customer), and an exception in the proxy's own process (KEPT, same reason). **So a rule stated as "drop `error_body`" is wrong**: it would delete a producer's own diagnostics. The rule is *nothing derived from the tool's result*, and each producer sorts its own sources by that test.

**`result` on this event is the reason it can be added at all**: reclassifying
the RETURN shape out of `tool_call_end` would otherwise move a structured body
into a flat truncated string. A consumer that reads bodies MUST read `result`
on `tool_call_error` as well as on `tool_call_end`.

**⚠ `result` is a BODY, not a discriminator, and MUST NOT be used as one.**
This subsection said "absent" on the RAISE shape until 2026-09-24 and then said
"null" until later the same day; **both were wrong, in both directions**:

- **Absence does not mean RAISE.** The field is optional, and a conformant
  producer may omit it or send `null`. `baton-sdk` and `baton-ts` send `null`;
  `baton-proxy` deliberately OMITS it, on the grounds that an explicit null
  asserts a body that never existed, and `baton-extmcp` calls that emitter
  without one. So a consumer MUST treat an absent `result` and a null `result`
  identically, and MUST NOT `KeyError` on the first.

  ⚠ **Why the two SDKs send it was stated here as "an explicit declaration so
  the key set matches Python's vector", and that reason is VOID** — removed
  rather than left standing, because two sibling producers implement from this
  paragraph and an implementer reading it would add a cross-SDK key-set
  comparison that the rule above has already made unenforceable. `baton-ts`'s own
  conformance suite stopped comparing key sets for exactly this reason (§11.4:
  absent ≡ null, and a consumer MUST NOT test for the KEY), so nothing can
  observe the declaration. What remains is per-producer and not normative:
  `baton-sdk`'s is a defaulted-and-dumped model field, and on `tool_call_end`
  `baton-ts`'s is its schema default too — measured, dropping the explicit
  literal there changes no byte on the wire. A conforming producer picks either,
  and a consumer cannot tell.
- **Null does not mean RAISE either.** `baton-sdk`'s envelope serializer answers
  `None` when an envelope cannot be made JSON-safe, so a RETURN-shape failure
  can legitimately carry `result: null`.

**The discriminator is `error_type`**: exactly `"tool_error"` for the RETURN
shape, the exception class name for the RAISE shape. That is the rule this
subsection already states for the producer, and it is the only one a consumer
can rely on. (`baton-spec/scripts/generate.py` names its two vectors on the
`result is not None` test instead — sound for that ONE scenario, whose envelope
serializes, and not a rule to copy.)

**⚠ It separates the two SHAPES; it is not a closed set of VALUES, and a
consumer MUST NOT treat it as one.** `error_type` on this event is an
unconstrained string — `baton-spec`'s `events.schema.json` types
`ToolCallErrorPayload.error_type` as `{"type": "string"}` with no enum. A wire sensor
legitimately emits values outside both shapes, for failures neither shape covers
(see the wire-sensor rule below). Measured 2026-09-28 in `baton-proxy`: one lane
emits `"tool_error"`, and four emit a JSON-RPC code as a string (`"-32601"`),
`"proxy_pending_evicted"`, `"proxy_upstream_unreachable"` or
`"proxy_upstream_closed"`. A consumer reading the rule as exhaustive misreads
all four as exception class names.

**⚠ In PYTHON the flag has two spellings, and which one appears depends on the
producer's library version, not on the producer.** Measured 2026-09-22 across
eight (library, version) cells:

| producer library | spelling |
|---|---|
| official `mcp` 1.20 – 1.27.x | `isError` |
| official `mcp` 2.x (the `mcp_types` rewrite) | `is_error` |
| standalone `fastmcp` 2.14.7 | **no flag exists** — `ToolResult` has no such field |
| standalone `fastmcp` 3.x / 4.x | `is_error` |

**⚠ In TYPESCRIPT there is ONE spelling, `isError`, on every version.**
Measured 2026-09-24 on `@modelcontextprotocol/sdk` 1.x and
`@modelcontextprotocol/server` 2.x. `is_error` is a PYTHON attribute name that
`mcp` 2.x's `mcp_types` rewrite introduced; MCP's own JSON-RPC schema is
camelCase, and the TypeScript SDKs have no snake_case era to inherit. A
TypeScript producer that also probed `is_error` would be reading for a shape no
version of either library produces.

**Producer rule (Python).** Detection MUST duck-type both spellings and SHOULD
probe `is_error` **before** `isError`, because that is the current generation's
spelling and therefore the common case; an `mcp` 1.x result, which has only the
camel name, still falls through to the second probe. Probing camel-first is not
wrong, only worse: in a process that has imported `fastmcp`, `CallToolResult`
carries a camelCase compatibility property that raises a deprecation warning
(once per process) and that the library has announced it will remove.

⚠ **An earlier revision of this paragraph justified the order differently and
was wrong in three ways** — it named `fastmcp`'s own `ToolResult` (whose
`.isError` raises plainly and warns nothing), implied the standalone producer
was the one affected (it is the official one, and only alongside `fastmcp`),
and said "every error result" where the warning is once per process. The order
stands; the reason is restated because two sibling producers implement from it.

**⚠ The `content` guard is scoped by VANTAGE POINT, and this paragraph
required it of everyone until 2026-09-24.**

⚠ **The predicate is what the producer receives ON THAT CALL, not which
package it is** — this paragraph bucketed by package until later on
2026-09-24, and `baton-sdk`'s official adapter falls in both buckets:
`Tool.run`'s `convert_result` is a caller's argument, which that adapter
forwards, so the same function may receive a converted result or the vendor's
raw return depending on the call.

A producer that receives a **library-CONVERTED result** MUST additionally
require a list-valued `content`. The case this
excludes is a caller that receives the vendor's return value **unconverted**
(`Tool.run(convert_result=False)`), where an object carrying an `is_error`
attribute for its own reasons would otherwise read as a failed tool call. ⚠ It
is **not** what excludes a vendor dict spelling `{"isError": true, …}`: under
the conversion the server normally applies, that dict becomes a real
`CallToolResult` whose flag is `false`, and the flag is what settles it. This
example was stated the other way here until code review mutated the guard away
and found nothing reddened.

A producer that receives the **vendor's LITERAL return, in a position where
that literal is what the transport sends** — `baton-ts`, which wraps the tool
executor — MUST NOT require `content`, and the guard is a defect there rather
than a safeguard. ⚠ The second clause is what makes the two rules compatible
rather than contradictory: a Python caller passing `convert_result=False` also
holds a literal, but nothing downstream is obliged to ship it, so the exclusion
the guard buys still applies there. Measured 2026-09-24 on both TypeScript majors: a
tool returning `{isError: true, rows: 0}`, with no `content` at all, reaches the
CLIENT as `{content: [], isError: true}`. The SDK merges the literal into the
wire result, so spelling the flag at the top level of a tool's return IS how a
TypeScript tool reports failure, and there is no "object that merely carries the
attribute" to exclude. Requiring `content` would make the sensor file a failure
its own caller can see as a success — the exact miscount this subsection exists
to remove. The invariant that holds at this vantage point is stronger and is
the one to implement: **for a failure the handler itself reports, the predicate
agrees with what the client received.**

A **wire sensor** — `baton-proxy`, `baton-extmcp` — reads `isError` off the JSON
result: one spelling (W1), no attribute, no envelope object, so neither rule
reaches it. ⚠ **The `content` clause MUST NOT be applied here, and that was
settled by measurement on 2026-09-24 after standing open.** `CallToolResult`'s
own JSON schema lists `content` as REQUIRED with no default, so the clause
cannot fire for a conformant server — while a server that omits it has its
failures read as successes. It excludes nothing and loses something.
`baton-proxy` applied it until `a8e02bf` and now tests the flag alone, which is
what `baton-extmcp` (`servicer.py:324`) always did; the two wire sensors agree.
⚠ **What refuses a vendor's own `isError` key at this vantage point is the
message kind, not the body shape**: a producer here MUST read the flag only on
a `tools/call` result, because resource and prompt bodies are vendor data.

**⚠ Which SHAPE a wire sensor emits, measured 2026-09-28 — and it is not
"always the RETURN form".** An earlier draft of this paragraph's companion note
prescribed exactly that, generalising from the one lane a probe had exercised.
Two rules, because a wire sensor observes two disjoint classes of failure:

1. **For a failure the TOOL itself reported**, a wire sensor MUST emit the
   RETURN form. It cannot do otherwise and MUST NOT try: the server converts a
   raised handler exception into a 200 carrying the flag before the bytes leave
   the process, so both shapes of this subsection arrive as one object and the
   exception class name is not recoverable at this vantage point. This is the
   mirror of the TypeScript request-handler asymmetry recorded below.
2. **A wire sensor ALSO observes failures neither shape covers**, and no
   in-process sensor can see them at all: a JSON-RPC `error` response (a
   protocol fault, as this subsection opens by saying), a request the upstream
   never answered, and a transport that died mid-call. For these a producer MUST
   NOT emit `error_type: "tool_error"` — that would assert the vendor's tool
   reported a failure about a call it never answered — and MUST NOT synthesise a
   `result`, there being no envelope to record (omitted or null, which this
   subsection already treats as equivalent). It names the class in `error_type`,
   which the schema leaves unconstrained; `baton-proxy` uses the JSON-RPC code,
   `"proxy_pending_evicted"`, `"proxy_upstream_unreachable"` and
   `"proxy_upstream_closed"`. ⚠ Producer names are deliberately NOT registered
   here: freezing one producer's internal vocabulary into a cross-producer
   document would make every new lane a spec change, and the consumer contract
   needs only a stable label, which the schema's plain `string` already admits
   and no rule here narrows.

A version on which the flag does not exist emits `tool_call_end` as before;
that is correct, not a gap.

**⚠ A handler-level sensor cannot see a failure the SDK manufactures ABOVE it,
and that IS a gap — recorded, not closed.** Measured 2026-09-24 on both
TypeScript majors: output-schema validation runs after the tool executor, so a
handler returning `content` and no `structuredContent` against an `outputSchema`
hands the sensor a success-shaped object (correctly classified) while the SDK
sends the client `{isError: true, content: [{text: "Output validation error:
…"}]}`. The same class covers an unknown or disabled tool and an
input-validation failure. A producer wrapping the executor MUST NOT claim to
agree with the client in general; only for failures the handler itself reports.
⚠ **This is a TypeScript-specific asymmetry, not a general one**, and the
first draft of this paragraph said the opposite. `baton-sdk`'s adapters wrap
`Tool.run`, and on `mcp` 2.x both argument validation and output conversion
happen INSIDE that method — a conversion failure raises `UnexpectedToolError`
from the same `try` the tool body runs in, so it reaches the Python adapter as
a RAISE and is filed as `tool_call_error` correctly. Read in the installed
2.x source, not run; `mcp` 1.x is unread. The gap above is a property of where
the TypeScript SDKs put validation, not of executor-wrapping as a technique.

**Moving a sensor up to the request handler does not simply close it — measured
on TypeScript.** Both TypeScript majors convert a thrown handler error into a returned `isError`
INSIDE that handler, so above that seam the two shapes of this subsection are
the same object and `error_type` collapses to `"tool_error"` for both — the
RAISE shape's exception class name is not recoverable there. The throw/return
distinction exists only BELOW the request handler. A producer that wants both
must compose two seams, not move one; until one does, **the wire sensors are
where this class is visible**, which is the same asymmetry the wire probe
recorded the other way round for `fastmcp` 2.14.7.

**Consumer rule.** A consumer MUST key on `event_type`, not on the flag. The
flag's spelling inside `result` is **era-native** — producers record what the
library called it and MUST NOT normalize it, because normalizing rewrites the
meaning of data already stored. A consumer that reads the flag directly (from
`tool_call_end` bodies emitted before this change, for instance) MUST accept
both spellings.

**⚠ Not retroactive for every producer.** A consumer reclassifying stored
`tool_call_end` rows can recover the official-1.x and fastmcp-3/4 populations,
whose bodies carry a flag. It cannot recover official-2.x rows: those bodies
never carried one, because `baton-sdk`'s own serializer unwrapped the envelope
away before emission. Nothing on the envelope names the producing library or
its version, so that population is not merely unrecoverable but **uncountable**.

#### 11.4.4 Resource and prompt lifecycle events — twelve types, one envelope

**⚠ These twelve were SHIPPED BY A PRODUCER BEFORE they were specified, and this subsection records the shape rather than designing it.** `baton-proxy` has emitted all twelve in production since the resource/prompt work landed; the reference Console's ingest lists every one of them; and until this entry neither this document, `baton-spec/events.schema.json` nor `baton-sdk`'s models defined any of them. So where a choice below looks inconsistent, the inconsistency is **the producer's and is preserved**, flagged rather than corrected — correcting it here would make the schema of record disagree with the only producer there is.

| `event_type` | Payload |
|---|---|
| `resource_list_start` | `{}` |
| `resource_list_end` | `{count, duration_ms?}` |
| `resource_list_error` | `{error_type, error_body, duration_ms?}` |
| `resource_read_start` | `{uri, params}` |
| `resource_read_end` | `{uri, duration_ms?}` |
| `resource_read_error` | `{uri, error_type, error_body, duration_ms?}` |
| `prompt_list_start` | `{}` |
| `prompt_list_end` | `{count, duration_ms?}` |
| `prompt_list_error` | `{error_type, error_body, duration_ms?}` |
| `prompt_get_start` | `{name, params}` |
| `prompt_get_end` | `{name, duration_ms?}` |
| `prompt_get_error` | `{name, error_type, error_body, duration_ms?}` |

**The envelope is §11.4's, unchanged.** That is what lets one collector endpoint accept every event type and one worker order them on `(session_id, sequence_number)`. A conforming producer MUST fill the same required envelope fields it fills on a tool call.

**`principal` and `organization` are carried.** A producer that has a resolver (§11.4) calls it once for each of these requests and puts what it returns on the start and on the terminal event of that request, as §11.4.5 states for a `tools/list` request. The resolver is given the request with no tool name and no arguments: the URI, the prompt name and the prompt arguments are not passed to it. A producer with no resolver sends none, so a consumer MUST read the value and MUST NOT expect one on these types.

**No payload here carries a BODY, and that is normative rather than an omission.**

- A `resource_read` records its URI and its timing, never the content it fetched. A `prompt_get` records the prompt's name, never the messages it rendered.
- **So `result_capture` (§11.4) does not appear on any of these payloads, and a producer MUST NOT add it.** The member declares that result-derived data was withheld; there is nothing on these payloads for a withhold to remove, so an SDK matching this shape needs no withhold extension. A producer that ever begins capturing a resource body MUST bring the rule with it in the same release.
- **The `*_start` payloads DO carry caller data in `params`**, and it MUST be PII-scrubbed per §7 like any other payload. `result_capture` says nothing about it either way: §11.4 states outright that the member is not a statement about request data.

**⚠ `params` is ALWAYS PRESENT on both `*_start` payloads — never absent, never null.** The table spelled it `params?` until 2026-10-02, read off `baton_proxy.proxy`, which is one layer too high: `baton_proxy/emitter.py` coerces at the payload (`"params": dict(params) if params else {}`), so an empty request sends `{}`. A consumer branching on `params is None` for the empty case never matches, and a second producer told to copy this shape field for field would emit `null` and diverge from the producer it was copying. ⚠ **And the published SCHEMA does not agree with this table — an open gap, not a rule.** `events.py` keeps `params: dict[str, Any] | None = None`, so the generated `events.schema.json` types it `object|null` with `default: null` and `required: ["uri"]`. A producer emitting `params: null` therefore PASSES cross-repo conformance while contradicting the row above. This table records the producer, per this subsection's preamble; the schema is lenient. Closing it means making the field required in `baton-sdk`'s model and regenerating — a submodule push with the usual ordering — and nobody has decided to. ⚠ No `baton-spec` vector covers any of the twelve types either, so neither half is exercised.

**⚠ `params` is NOT built the same way on the two `*_start` payloads.** On `resource_read_start` it is the request's params with `_meta` removed and nothing else — **so `uri` appears twice**, once in its own member and once inside `params`. On `prompt_get_start` it is the request's `arguments` member alone, so `name` does **not** appear inside `params`. Measured in `baton_proxy.proxy`. A consumer MUST read the dedicated `uri` / `name` member and MUST NOT expect the subject inside `params`; a second producer SHOULD copy these shapes rather than infer a rule from one of them.

**⚠ `count` on `resource_list_end` counts the `resources` array ALONE.** Resource TEMPLATES are a separate MCP method with their own result array and are not added in. A server exposing only templates therefore reports `count: 0`, which is correct, and a consumer MUST NOT read it as "this server has no resources".

**⚠ There is no returned-flag shape here, so §11.4.3's `error_type` discriminator has no analogue.** `CallToolResult`'s error flag is a TOOL concept: a failing resource read or prompt get comes back as a JSON-RPC error, which §11.4.3 opens by calling a protocol fault. These four error payloads therefore carry **no `result`** and **no `failure_kind`**, and a producer MUST NOT synthesise either.

**⚠ `error_type` is an unconstrained string and the producers do NOT agree on how to spell it.** A **wire sensor** holds the upstream's JSON-RPC error and files its numeric `code` as a string (`baton-proxy`); an **in-process SDK sensor** holds a live exception and files its class name, as §11.4.3's RAISE shape already does for tools. Both are conforming — the schema constrains neither, and §11.4.3 states that this member separates SHAPES and is not a closed set of values — and a consumer MUST NOT read one producer's spelling as the vocabulary. For `error_body` under a withholding mode, see the clause below.

**`error_body` is KEPT on all FOUR error legs under every capture mode.** `result_capture` does not appear on these payloads (clause above), so the wire carries no statement either way.

⚠ **Body text CAN reach a consumer through these legs.** Measured on both MCP majors under a withholding mode (`response_capture_canary_1002`): a string planted in the vendor's handler reaches the consumer's database on `resource_read_error`, `resource_list_error` and `prompt_get_error`. The fourth, `prompt_list_error`, has no caller-reachable path on either major and was not exercised. A handler that quotes what it fetched — `Could not parse ${body.slice(0, 200)}` — sends that text. A producer cannot determine which thrown strings quote a body, so the leak is not addressable per string.

⚠ **Withholding all four instead is not available as a remedy.** `resource_list_error` and `prompt_list_error` carry only `{error_type, error_body, duration_ms?}`, so withholding leaves them no subject member at all; the two READ legs would keep `uri` or `name`. §11.2 item 6's floor for a tool keeps `tool_name`; a list leg has no analogue.

**A producer that emits ANY of these four legs, or `tool_list_error` (§11.4.5), under a withholding mode MUST do both of the following:**

1. **State it in its own vendor-facing documentation** — its README or equivalent, the text a vendor reads when configuring the mode — that those legs' messages survive the mode.
2. **Emit a `UserWarning`**, or its language's equivalent, when the mode is CONFIGURED: once per configuration, not per event, naming which of the five that producer emits.

Read the predicate disjunctively: ANY of the five binds, not all five. A producer that offers the mode while emitting none of them carries neither obligation.

⚠ **Neither obligation is assertable from a payload**, so no `baton-spec` vector covers either. The warning is checked by configuring a producer and capturing its warnings; the documentation rule is not machine-checkable.

**Who the predicate binds.** `baton-sdk`, `baton-ts` and the Go SDK offer the mode, emit all five, and do both. `baton-proxy` emits all five and offers no mode, so the obligations first bind it in the release that gives it one.

**⚠ These legs have no pairing key.** No producer sends `call_id` on any of these twelve types, and the `call_id` rule (§11.4) pairs on nothing else. A producer MAY mint one and the first that does needs no schema change; until then a consumer has nothing to pair a start with its end on.

**Consumer rules.**

- **A consumer MUST NOT reject an event for carrying an unrecognised `event_type`**, here or anywhere. The reference Console accepts all twelve and reads none of them, which is conforming: these types are additive and a consumer adopts them when it has a use.
- A consumer MUST NOT infer from a `*_list_end` `count` that it has seen the surface. `surface_snapshot` (§11.4.2) is what describes the surface; a count describes one call's answer.

#### 11.4.5 Tool list events — three types

A producer sends these when a client asks for the server's tools (`tools/list`). They record that a client looked at the server, whether or not it then called a tool.

| `event_type` | Payload |
|---|---|
| `tool_list_start` | `{}` |
| `tool_list_end` | `{count, duration_ms?}` |
| `tool_list_error` | `{error_type, error_body, duration_ms?}` |

**The shapes are §11.4.4's list shapes**, and its rules for `error_type`, `error_body`, pairing and `result_capture` apply here unchanged. Vendor code can run inside a `tools/list` request, as a handler, an override or middleware, so `tool_list_error` can carry the vendor's text. Measured on both `baton-sdk` adapters and both `baton-ts` majors, and pinned by a test in each, under a withholding mode. These three are specified apart from the twelve only because they were designed here and not recorded from a producer.

**The envelope is §11.4's, unchanged**, `client_observed` included. No `call_id`, as in §11.4.4.

**`principal` and `organization` are carried.** A producer that has a resolver (§11.4) calls it once for each `tools/list` request and puts what it returns on the start and on the terminal event of that request. The resolver is given the request with no tool name and no arguments. Every rule §11.4 sets for `principal` and `organization` applies.

**One pair per request.** A producer emits a start and one terminal event for every `tools/list` request it serves. A client that pages through the list makes several requests and gets a pair for each. A listing the server's own library runs for itself, with no request from a client behind it, gets no events.

**`count` is the length of the `tools` array in that one response.** It includes any tool the producer itself added to the list. It counts one page, not the server's surface: `surface_snapshot` (§11.4.2) describes the surface.

**No payload carries the list.** A producer MUST NOT put tool names, descriptions or schemas on these events.

**Session id.** A listing carries the session id any other request from that client would carry (§3.4). Where a producer can only give each request a session id of its own, each listing arrives as a session of its own.

**Not the handshake.** A producer does not emit an event for `initialize`. Every client lists tools; not every client sends a handshake the server sees.

#### 11.4.6 Comparing a producer against the vectors

`baton-spec/vectors/` holds real `baton-sdk` events, made by `scripts/generate.py`. A producer checks itself by running the same scenario and comparing its events with them. Absent and null count as equal (§11.4). Every other member must match, except these, which differ for reasons that are not a wire difference:

| member | why it may differ | what to check instead |
|---|---|---|
| `event_id`, `session_id`, `captured_at` | new on every run | — |
| `call_id` | new on every run | a call's start and its end or error share one value |
| `sdk_version` | names the producer (§13) | — |
| `agent_runtime`, `client_observed`, `transport_observed` | depend on how the test drives the server (`generate.py` calls tools with no client), not on the producer | each against its own rules |
| `payload.duration_ms` | timing | — |
| `payload.result` in the `tool_call_end` and `tool_call_error.returned` vectors | each library hands the producer a different object for the same return | its shape |
| `payload.error_type` in the `tool_call_error` vector (raised) | an exception class name is per language | a non-empty string |
| `surface_snapshot` `payload.surface_hash`, `server_info`, `capabilities`, `tools` | each library derives a tool's schema its own way | `seam_augmentations` must match |

`error_type` in the `tool_call_error.returned` vector is `"tool_error"` from every producer and is not exempt. `tool_call_start.identified.json` pins `call_intent`, `intent_source`, `principal` and `organization`: a vendor resolver returns a person with a company, and the call carries `user_goal`.

#### 11.4.7 `client_report` — how many events this process lost

*Experimental (§13): this event's members and counting may change after 1.0.*

A producer that sends over HTTP reports the counts from §11.2 rule 8, so the Console can show how many events an install lost and why.

```json
{
  "instance_id": "0193f2c1-7b6e-7c11-9a0e-3f5c2d4b8a10",
  "dropped": {"refused": 3, "buffer_full": 1}
}
```

**`instance_id`** is a UUID the producer mints once per process. Every report from that process carries the same value, whichever client or sink sends it.

**`dropped`** maps a §11.2 rule 8 reason to the number of events lost for it since the process started. `unsent_at_close` is an estimate (§11.2 rule 8). A reason with no loss is left out. The numbers are running totals, never "since the last report", so a lost report loses no information.

**When it is sent.** Each HTTP sink decides for itself. It sends a report only after it delivers an event, and only if the totals differ from the last report it delivered. At most one per sink per minute. A healthy process never sends one. A producer does not send one at close: the collector is often the reason for the loss, so a report then would mostly be lost too. Loss in the last minute before exit may therefore go unreported.

**A report is never itself counted.** A producer MUST NOT add a lost `client_report` to its counts. This holds wherever the report is lost: in the buffer, refused, unencodable or unsent at close. Until a report with the current totals is delivered, the next delivery may send it again. A collector that predates this type refuses it, and then the sink sends no more reports and does not log the refusal as a lost event. That is: a `400` or `422` to a report sent alone (one event per request, §2.5), or a batch result that refuses the report with `payload_malformed`. Nothing else stops reports: not a whole-batch `400` or `422`, which another event in the batch may have caused, and not a refusal with another code such as `quota_exhausted`. A due report takes one of the 100 places, and its bytes under the 1 MiB cap, in each batch it can ride: one whose events have its `tenant_id` and `vendor_id`. So it goes even when its own tenant's events fill every batch. A batch it cannot ride keeps all 100 places, and the report waits while another tenant's events are first. When the first waiting event does not fit beside the report, the report goes first, alone, and the event goes in the next batch. A batch that is kept for a later try keeps its report: the same event, with the same `event_id`. A report taken into a batch with no events, or taken while the producer sends one event per request (§2.5), is not kept when that batch fails: the next delivery builds a fresh one, so a collector that fails reports alone does not hold up the events behind them. A report taken with events and then split off by a `413` is kept.

**Envelope.** `tenant_id`, `vendor_id` and `consent_token` are those of the events the sink sends. `session_id` is the `instance_id`, and is not a session: a consumer MUST NOT count it as one or join other events to it. `sequence_number` increases across the process's reports, such as by the clock (§5.6). `captured_at` is when the report was built. No `call_id`, `principal`, `organization`, `client_observed`, `transport_observed` or `runtime_meta`, as on `surface_snapshot` (§11.4.2).

**Consumer rules.**

- Tolerate a reason name it does not know.
- The counts are for the process, not the sink. A process that sends to two tenants reports the same totals to each.

### 11.5 Annotation correlation rules

#### 11.5.1 Turn-vs-session distinction

Collector-side behaviour. Not part of this contract, except one producer rule. A producer MUST NOT reject or fail a call over a missing or malformed turn number. A label counts as numbered only when an integer of at most six digits is followed by a separator (`:`, `.`, `)` or a dash) and then a space or the end of the label, so `2024 tax prep`, `3-day forecast` and `10:30 standup` are plain labels.

#### 11.5.2 Annotation correlation within a turn

Collector-side behaviour. Not part of this contract.

#### 11.5.3 Channels MUST consume Signals, not events

Collector-side behaviour. Not part of this contract.

#### 11.5.4 Tool-call leg pairing

The pairing rule is in the `call_id` definition (§11.4).

#### 11.5.5 Reading a report

The agent says what happened; it does not classify (§5.1.1). Whether a collector sorts reports into kinds, which kinds, and by what method are the collector's own business and are not part of this spec. This section holds only what any collector needs to read a report's fields correctly.

**A report that names no tool** is one whose `tool_name` is `none` or `""`. Agents spell it several ways, so a reader SHOULD strip surrounding spaces, brackets and quote marks, and then read an empty result, `none`, `n/a` or `null`, in any letter case, as `""`. This covers `(none)` and a value that is itself two quote characters, which agents send when told to pass an empty string. A null `tool_name` stays "not stated" and is not read this way. It is the agent saying no tool exists for the request.

**`signal_type` is not a classification.** When an older producer sent one, it is the word that agent picked. A report filed before `baton-sdk` 0.8.15 keeps the `signal_type` its agent sent.

**A withheld result is not an empty one.** Where a result was withheld (`result_capture`, §11.4.3), a reader reads the report without it.

### 11.6 Action vocabulary (additive-only)

Collector-side behaviour. Not part of this contract.

### 11.7 Channel kind classification (Console-side)

Collector-side behaviour. Not part of this contract.

### 11.8 Future policy direction (informative)

Collector-side behaviour. Not part of this contract.

---

## 12. Errors

Standard problem-details-ish error body for all 4xx/5xx:

```json
{ "error": { "code": "consent_token_invalid", "message": "..." } }
```

A collector MAY send other members beside `error`, such as a `detail` for older
clients. A client reads `error.code` and MUST treat an unknown code by its
status alone.

Defined error codes:
- `auth_invalid` — bearer missing, malformed, unknown or revoked, or a key that
  does not belong to the named workspace (401). These MUST share this one code
  and one message: a caller without a valid key must not learn which check
  failed, or whether the key or the workspace exists.
- `consent_token_invalid` — consent token unknown or expired (401)
- `payload_malformed` — the body or an event in it does not match the schema
  (400 or 422)
- `payload_too_large` — the request body is over the collector's size limit (413)
- `tenant_suspended` — the key is valid but its workspace is suspended (403)
- `key_scope_refused` — the key is valid but its kind is not accepted here; for
  example a publishable key on an endpoint that needs a workspace key (403)
- `key_server_mismatch` — the key is bound to one server and the request names
  another (403)
- `quota_exhausted` — the workspace has used its monthly event quota (403, or a
  refused result in a batch, §2.5). Not 429: a client MUST NOT retry it before
  the quota resets.
- `vendor_unknown` — `vendor_id` not registered (403)
- `method_not_allowed` — the path exists but not for this HTTP method (405)
- `rate_limited` — too many requests for now; retry later (429)
- `server_error` — anything else (500)

`tenant_suspended`, `key_scope_refused` and `key_server_mismatch` are sent only
to a caller whose key verified, so they say nothing a key holder could not
already learn.

---

## 13. Compatibility & versioning

- The wire format's version is this spec's (§0). `sdk_version` is the producer's own version, and an SDK's major follows its public API, not the wire.
- Before 1.0, a breaking change could ship in a minor (and, by choice, in a patch).
- **`sdk_version` also names the producer, by its prefix.** `baton-sdk` sends the bare version (`0.9.1`), `baton-ts` sends `ts-` and the version (`ts-0.6.1`), `baton-proxy` sends `baton-proxy/` and the version, and the Go SDK MUST send `go-` and the version. A new producer MUST pick a prefix no other producer uses. There is no separate field for the producer's name.

### Stability from 1.0

From 1.0, the wire format is additive-only: no event type, field or value is removed or changes meaning without a 2.0. A producer MAY add an optional field, a new event type or a new value where this spec says new values may be registered, so a consumer MUST ignore what it does not know. Everything in this spec is stable from 1.0, except the items in the experimental table below.

| Experimental (may change after 1.0) | Where |
|---|---|
| `labels` and `capture`, including `capture.overlay` | §11.4 |
| `GET /v0/settings` and its document | §2.6 |
| `client_report` | §11.4.7 |
| `trace_context` (reserved, not sent) | §5.2 |

`agent_runtime` is stable but LEGACY: producers that send `client_observed` send `"unknown"`, and a consumer SHOULD NOT rely on it (§5.2).

**Each SDK's public API follows semver from 1.0** too: a breaking change to it is a new major. Each SDK's README says what is public. `baton-sdk`'s pre-rename import paths (`baton.integrations.mcp`, `baton.integrations.fastmcp`) are permanent aliases (`tests/test_import_path_aliases.py`). A Go SDK major after 1 also changes its module path (`/v2`). `baton-proxy` stays at 0.x and sends 1.0 events.

### Changes since 1.0

A reference here to a §13 entry by date or version, such as "§13, 0.8.22", names an entry in the git history of `baton`'s `docs/SPEC.md`, where the changes up to 1.0 are.

---

## 14. Open questions

Open spec-level design questions. Resolutions land in subsequent minor versions per §13.

- **Signed payloads.** Current versions trust HTTPS + bearer. A future version should define HMAC or asymmetric signing (likely tied to an OAuth or DID direction).
- **Runtime-memory adapters.** `MemoryAdapter` interface + first adapter (project-context files like `AGENTS.md` or per-runtime equivalents).
- **Auto-send (enterprise mode).** Bypass per-report consent under vendor policy.
- **Vendor event enrichment from vendor app context.** Mechanism for vendors to attach extra structured context (logs, traces) without breaking the "Baton only sees MCP transport" boundary — probably a `vendor_context` field with a vendor-defined schema, scrubbed and bounded.
- **`signal_type` values.** The eight are frozen: nothing new is added to a field no agent fills.
- **Background dispatch.** Dispatch is currently synchronous on the agent's hot path. Moving it off the critical path (background task / queue worker) is a future improvement; the SDK's bounded local buffer + retry-with-backoff is the substrate.
- **Per-report end-user consent flow.** Today (§9) the vendor supplies a single static `consent_token` at SDK init and every event ships with the same token — workable for single-end-user deployments, but not for multi-end-user vendor MCP servers. The planned design: on a failed call or a report, the SDK emits an MCP **elicitation prompt** (or, on transports without elicitation support, surfaces a synthetic tool response asking the user to call a vendor-namespaced `<vendor_id>_consent` tool) describing what happened, who will receive the report (vendor display name), what will be sent (high-level summary), and a Y/N choice with an optional "always for this session." On refusal, the SDK MUST discard the payload and MUST NOT retry sending it unless a new failure or report occurs. Open: which failures and reports warrant a prompt vs. which can ride on session-level consent? How does this compose with the per-end-user OAuth/DID upgrade path?
- **Richer PII scrub interface.** The current `VendorConfig.scrubber: Callable` (§7) puts all the burden on the vendor — they have to know what shapes to expect, recursively walk dicts, and reimplement common patterns (emails, API-key shapes, credential param keys). A richer interface would ship declarative rules — `scrub_rules: list[Rule]` with rule kinds `redact_key` / `mask_key` / `regex_mask`, targeting `params` / `error_body` / `result_content` / `intent` / `expected_outcome` — plus sensible defaults for common patterns. A typed `Scrubber` protocol (`scrub_params(tool_name, params)` / `scrub_result(tool_name, result)` / etc.) and a `DefaultScrubber(extra_key_denylist=..., extra_regex_rules=...)` baseline would let most vendors say `VendorConfig(scrub_keys=["password", "email"])` and be done. Sentry / OpenTelemetry pattern. The current `Callable` shape would become an escape hatch alongside the richer surface. **Its relationship to `result_capture` (§11.4) is settled and should not be re-litigated:** these rules are the mechanism for the rungs that TRANSFORM a value, and `result_capture` is the declaration of the rung that transforms nothing because nothing crosses. A rule cannot express that rung — it produces a value where the wire needs a fact, and a consumer that cannot tell withholding from an empty response manufactures failures (§11.4). When these rules land, `result_capture` gains the partial rung as a second registered value and the two meet there.
- **`proactive_mode` parity across producers.** _`baton-sdk` has since removed its knob and always refuses a pre-call annotation; the bullet below predates that._ _Partly closed 2026-09-01 (§13): baton-proxy now has the knob. **baton-ts still does not**, and is the remainder of this open question._ `VendorConfig.proactive_mode` (§13, 2026-08-10b) made the pre-call annotation request optional and defaulted it **off** in baton-sdk (Python): the injected params already carry `call_intent`/`call_expected`/`call_workflow` on every `tool_call_start`, so the proactive turn is redundant as a data carrier. baton-ts still unconditionally renders the "BEFORE invoking any tool … you MUST call" clause into its instructions, so every TS-wrapped server pays the proactive turn whether or not that deployment wants it, and it should gain the same option. **A spelling caution for whoever ports it:** the proxy's `on` is not the SDK's `on` — the proxy drops the pre-call clause in both modes, so its knob governs only the tool description and the handler. A third producer picking yet another meaning for the same word is the failure this bullet now exists to prevent; state which legs the knob governs when adding it. The *default* is deliberately left open here: baton-sdk wraps a server the vendor owns, whereas baton-proxy sits in front of servers it does not own, so flipping proxy's default silently changes capture on live traffic its operator may not control. **Ordering constraint:** proxy must not turn proactive off before the `overall_task` port (§13, 2026-08-10) lands — until proxy injects that param it emits no `call_workflow`, so disabling proactive annotation would drop the task label entirely rather than move it to a better-measured field. baton-extmcp is moot: it injects no annotation tool and no instructions suffix, so it has no proactive request to disable.
- **Inline annotation via reserved tool-param prefix.** One alternative to the separate annotation tool (§5.1) is letting the agent pass `intent` / `expected_outcome` as arguments on the regular tool call — e.g., `vendor_tool(query="...", _baton_intent="...", _baton_expected="...")` — with the SDK extracting and stripping the reserved-prefix params before forwarding to the vendor handler. Single round-trip instead of two; nudges agents toward populating intent by exposing the fields directly on the tool schemas they're already looking at. Tradeoffs not yet evaluated: collision with vendor-owned param namespaces, whether agents actually populate the fields when threaded inline, schema-pollution concerns. Not implemented; revisit if the §5.1 path's turn-count cost becomes a blocker.
- **Console-provided tool proxy (SDK action surface).** The SDK MAY register Console-provided tools as stateless proxies on the vendor's MCP server. Vendors opt in via `VendorConfig`; the SDK fetches the Console's tool catalogue at install time and registers handlers that forward calls to Console verbatim. The SDK contains no business logic about when to invoke these tools or how to interpret results — all of that lives in Console. Open design questions: catalogue fetch auth (same API key as the sink?), startup resilience (static snapshot fallback when Console unreachable at install time?), tool description/schema serving (dynamic from Console vs. baked into SDK release?), event written by Console on each proxy call for audit trail. Deferred until Console tool catalogue design is ready.

- **Customer-side capture via agent-runtime plugins.** The two emission surfaces in §5 (MCP middleware, library API) both run on the vendor's side and require the vendor to integrate. A third path — plugins inside the agent runtime itself (Claude Code hooks, Cursor extensions, etc.) — would capture tool usage from the customer's side, complementing the vendor-side paths when the vendor hasn't integrated. Same `Sink` ABC and same event envelope (§11.4); the plugin translates runtime-native hook events into Baton events and hands them to a sink. Open design questions: per-event vendor inference (parsing `mcp__<vendor>__<tool>` namespaces or similar), multi-vendor consent model (one plugin captures across many vendors per session — who consents to what?), sink routing across multiple vendor collectors, dual-source dedup when a Baton-wrapped MCP server is also installed (likely keyed on `_meta.claudecode/toolUseId` or equivalent). Payload completeness: a plugin sees user prompt + tool call + observed outcome but **not** agent-emitted `expected_outcome` — the four-things-in-one-context payload is incomplete on this path unless a synthesizer is added. **Implementation note:** when this lands, it will ship as a **separate package** (e.g., `baton-claude-code`), not as an integration under `baton.integrations.*`, because the deployer (end user, not vendor), install mechanism (runtime plugin system, not pip), and release cadence (couples to the runtime's hook API, not the wire spec) all differ. The wire envelope is the contract that binds the separate package back to this spec — which is why this §14 entry exists rather than being deferred entirely.

---

*Spec ends here.*

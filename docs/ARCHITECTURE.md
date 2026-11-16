# Architecture

## 1. Principles

1. **A node is a file.** All state — claims, service registry, per-service state, run history, messages,
   audit log — lives in one SQLite database. Copying the file copies the node.
2. **Services are declared, not discovered by a registry.** A service is a manifest (plus a function). The
   dependency graph is *derived* from manifests; there is no central catalogue to trust or operate.
3. **Services share knowledge only through claims and messages.** They never call each other directly, so any
   service can be replaced without the others knowing.
4. **Provenance is not optional.** There is no way to write a claim without a source service, and derived
   claims cite their inputs.
5. **Coordination, not centralised intelligence.** The orchestrator decides *when* and *in what order*; it holds
   no domain logic.

## 2. The service contract (manifest)

The seven elements of a service map directly onto manifest fields (`oas/manifest.py`):

| Element | Manifest field(s) | Notes |
|---|---|---|
| Purpose | `id`, `version`, `purpose` | `id`: lowercase, digits, `_ . -`, ≤64 chars |
| Configuration | `config`, `config_schema` | typed schema with defaults/required; validated at registration |
| State | *(runtime)* `ctx.state` | JSON dict, persisted per service |
| Triggers | `triggers` | `interval`, `event`, `dependency`, `message`, `manual` |
| Execution | `entrypoint` (`module:function`) or a bound callable | function takes one `ServiceContext` |
| Output | `writes` (claim types), `can_message` | enforced permissions |
| Continuity | `continuity` = `persist` \| `ephemeral`, `retry`, `timeout_seconds`, `circuit_breaker` | how progress/failure carries over |

Additionally `reads` declares which claim types a service may consume. Unknown manifest keys are rejected.

## 3. Claims and provenance (`oas/store.py`)

A claim has: `type`, `content` (any JSON), `source_service`, `inputs` (claim ids it was derived from),
`confidence` (0–1, supplied by the service), `created_at`, `supersedes`, `origin` (node id).

- **Content-addressed id.** `id = sha256(canonical JSON of type, content, source_service, inputs, confidence,
  created_at, supersedes)`. Anyone can recompute it, so a claim's integrity can be verified without a
  central authority. Identical claims deduplicate (writes are idempotent).
- **History.** A claim is *current* unless another claim supersedes it. A service may supersede only its own
  claims, of the same type, once (linear history). A supersession only counts when the superseding claim has the
  **same source service, origin and type** as its target; otherwise it is ignored everywhere (current view,
  lineage, pruning). This stops a peer hiding someone else's claim by naming it in `supersedes` before it arrives.
- **Lineage.** `Store.lineage(id)` walks `inputs` and `supersedes` back to the roots and reports any ancestors
  that are not present locally (e.g. after a partial import).
- **Confidence is a number the service supplies.** The framework stores and propagates it but does not
  calibrate it. The example reporter uses "weakest link" (minimum of its inputs) by convention.

## 4. Permissions (`oas/context.py`)

Services act only through a `ServiceContext`:

- `read(type)` requires a matching `reads` pattern (`fnmatch` wildcards allowed in the *manifest*).
- `write(type, …)` requires a matching `writes` pattern; the claim type itself must be a literal.
- `write(..., inputs=[…])` — cited claims must exist and be readable by the service.
- `send(to, …)` requires a matching `can_message` pattern and an existing target.
- Every denied action is recorded in the audit table, and the run fails.

Permission enforcement is **cooperative** (see STATUS: services are trusted in-process code).

## 5. Execution semantics (`oas/orchestrator.py`)

Each run is **all-or-nothing**. During the function, claims, state changes, outgoing messages and consumed
inbox messages are *buffered*. Only if the function returns normally does the orchestrator commit them —
together with the run record, trigger cursors and next-due time — in **one SQLite transaction**.

- **Retries** with exponential backoff (`retry.max_attempts`, `retry.backoff_seconds` × 2^(n-1)). State from a
  failed attempt is discarded.
- **Timeouts.** In thread mode the function runs in a worker thread; on timeout the run fails and the context is
  cancelled so a late write can never commit, but the thread itself cannot be killed. With
  `isolation: "process"` (section 13) a timeout kills the process.
- **Circuit breaker.** After `circuit_breaker` consecutive failed runs the service is disabled with a recorded
  reason; `enable` re-enables it. Registering a new version also re-enables it.
- **Execution lease.** Before running, an orchestrator atomically takes a per-service lease (default 300 s). A
  second orchestrator sharing the database gets `skipped`. A crashed process's lease simply expires. After
  taking the lease, due-ness is re-checked so a service another orchestrator just completed is not re-run.
- **Persistence.** State, cursors and claims survive restarts; entrypoints are re-imported on demand.

## 6. Triggers and scheduling

| Trigger | Fires when | Cursor |
|---|---|---|
| `cron` (`expr`, 5 fields, UTC) | the next matching minute after the last consumed slot has passed; missed slots coalesce into one run; a failed slot is consumed (no hot retry) | `cron:<expr>` cursor (next slot) |
| `interval` (seconds) | `next_due ≤ now` (first tick runs immediately) | `next_due`, advanced after every completed run, failed or not |
| `event` (`claim_type`, wildcards ok) | new claims of that type since the last successful run, **excluding the service's own local writes** | per-trigger claim sequence |
| `dependency` (`service`) | that service has a successful run newer than the last one seen | run sequence |
| `message` | unconsumed messages are waiting | inbox itself |
| `manual` | never automatically | — |

Cursors advance only on a successful run (at-least-once processing). `tick(max_rounds)`:
1. computes which services are due, ordered topologically by the derived graph;
2. re-evaluates each service just before running it (earlier runs may change what is due);
3. repeats so effects cascade within a single tick, until nothing is due or `max_rounds` is reached
   (`truncated=True`). A service that failed or was skipped is not retried within the same tick.

## 7. Graph, validation, discovery

`Orchestrator.graph()` derives edges producer→consumer from `writes`/`reads` overlap, `dependency` triggers and
`can_message`. It returns an execution order and strongly connected components (cycles). `validate()` reports
unknown dependency targets (error), unserved reads/events, dangling message targets and cycles (warnings).
`discover(dir)` registers every `*.json`/`*.toml` manifest in a directory; `find_providers`/`find_consumers`
answer "who writes/reads this claim type" from manifests alone.

## 8. Federation (`oas/federation.py`)

`export_bundle` produces a JSON file of claims; `import_bundle` verifies and merges it on another node:

- recomputes every id (tampered/corrupt claims are rejected individually; the rest are accepted);
- is idempotent; keeps the original `origin`; rejects claims that claim this node as origin but are unknown;
- refuses a foreign claim that tries to supersede a claim owned by a different service/origin;
- imported claims trigger local `event` services exactly like local ones.

Integrity comes from the content-addressed ids; **authenticity comes from signatures** (section 12). Unsigned claims
are accepted unless the importer requires signatures, in which case `source_service` and `origin` are only as
trustworthy as the signing key's trust policy.

## 9. Requirement → code → tests

| Requirement | Implementation | Tests |
|---|---|---|
| Independently configured services | `manifest.py`, `Manifest.resolve_config` | `test_manifest.py`, `test_executor.py::test_config_defaults…` |
| Standardised interfaces | `Manifest`, `ServiceContext` | `test_manifest.py`, `test_permissions.py` |
| Scheduling | `due_reasons`, `tick` | `test_scheduler.py` |
| Execution | `run_service`, `_commit` | `test_executor.py` |
| Persistent state | `Store.get_state/set_state`, continuity | `test_executor.py`, `test_store.py`, `test_pipeline.py::…restart…` |
| Inter-service communication | claims + `send/inbox` | `test_scheduler.py` (message), `test_permissions.py` |
| Shared knowledge | claims table, `query_claims` | `test_store.py`, `test_pipeline.py` |
| Provenance | content-addressed ids, `inputs`, `supersedes`, `lineage` | `test_store.py`, `test_permissions.py`, `test_pipeline.py`, `test_federation.py` |
| Permissions | `ServiceContext._deny`, audit | `test_permissions.py` |
| Error handling | retries, timeout, breaker, rollback | `test_executor.py`, `test_robustness.py` |
| Orchestration | `graph.py`, `tick`, `validate` | `test_scheduler.py`, `test_pipeline.py` |
| Concurrency safety | execution leases | `test_concurrency.py`, `test_robustness.py::CrashSafety` |
| No central dependency | file-based nodes, `federation.py`, `transport.py` | `test_federation.py`, `test_cli.py`, `test_transport.py` |
| Semantic contracts | `schema.py`, `claim_schemas` | `test_schema.py` |
| Authenticity and trust | `ed25519.py`, signatures, trusted signers | `test_signing.py` |
| Killable, contained services | `isolation.py` | `test_isolation.py`, `test_regressions.py` |
| Calendar schedules | `cron.py` | `test_cron.py` |
| Retention | `Store.prune_*` | `test_retention.py` |
| Hostile-input handling | import/sync containment, supersession rule | `test_regressions.py` |

## 10. Claim schemas (`oas/schema.py`)

A manifest may declare `claim_schemas: {claim_type: schema}` for types it reads or writes (keys may be wildcards).
The schema language is a JSON-Schema subset (type, enum, const, properties, required, additionalProperties,
items, min/max Items/Length, minimum/maximum); unknown keywords are rejected so a typo cannot silently disable
validation. Schemas are checked when a service writes (violations fail the run and are audited), when a bundle is
imported (against schemas declared by *any locally registered service* for that type), and `validate()` warns when
services declare different schemas for the same type. A type nobody declares a schema for is unvalidated.

## 11. Cron triggers (`oas/cron.py`)

`{"kind": "cron", "expr": "0 9 * * 1-5"}`: five numeric fields, UTC, lists/ranges/steps, classic day-of-month OR
day-of-week rule. The framework has no scheduler loop of its own: something must call `tick()` (a loop, systemd
timer, cron job); due cron slots are evaluated at that moment.

## 12. Signed claims and trust (`oas/ed25519.py`, `oas/store.py`, `oas/federation.py`)

Every node has an Ed25519 key (stored in its database, never exported). Claims written locally are signed at
commit. The signature covers `"oas-claim/1:" + claim_id + ":" + origin`: the id commits to type, content, source
service, inputs, confidence, time and supersession, and the origin is added because the id does not cover it.
On import a present signature must verify; `--require-signatures` rejects unsigned claims; `--require-trusted`
additionally requires the signer's key to be registered (`oas trust add <pubkey> --label L --service PATTERN ...`)
**and** the claim's `source_service` to match that key's allowed patterns, so a trusted key cannot vouch for
services it was not granted. Signatures survive relaying (A to B to C). Ed25519 is a pure-Python implementation
(RFC 8032 vectors verified); see STATUS for its limits.

## 13. Process isolation (`oas/isolation.py`)

`"isolation": "process"` (requires an `entrypoint`, optional `max_memory_mb`) runs the service in a child
interpreter. The child has no database handle: each context call is a JSON-lines RPC over a socketpair to the
parent, which runs it on the real permission-checked `ServiceContext`. The parent enforces the timeout by killing
the child, contains crashes and memory exhaustion, and commits the run atomically as usual. This isolates
failures, not a malicious same-user process (see STATUS).

## 14. Transports (`oas/transport.py`)

- **Directory sync** (`oas sync DIR`): each node atomically writes `<node_id>.bundle.json` and imports everyone
  else's. The directory can be a git repo, Syncthing folder, network share or USB stick.
- **HTTP** (`oas serve` / `oas pull URL`): read-only `GET /bundle?since=N&type=T` and `GET /whoami`. No auth or TLS.
Both go through `import_bundle`, so schemas, signatures and trust policy apply identically.

## 15. Retention (`oas/store.py`)

`prune_runs` (keeps the newest N per service plus the latest success), `prune_audit`, `prune_messages` (consumed
only) and `prune_superseded_claims` (never current claims; later lineage reports the gap). Nothing runs
automatically (`oas prune ...`).

## 16. Extending

- **New service:** write a function `fn(ctx)` and a manifest; register with `Orchestrator.register` or drop the
  manifest in a directory and `discover` it.
- **Swap an implementation:** re-register the same `id` with a new version/function. State, claims and
  history are kept; the other services are unaffected (`test_pipeline.py::test_replacing_a_service…`).
- **Add a node:** point another process/machine at its own database and exchange bundles.

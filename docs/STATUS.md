# Status, verification record and limitations (v0.1.0)

Build window: started 23:35:35 UTC on 2026-11-15, hard stop 00:35:35 UTC.

Rule used throughout: a capability is listed under "Verified" only if a test in this repository exercises it
and passed. Everything else is listed under "Not verified" or "Not implemented".

## Publication

**Published** at <https://github.com/NG-Aligned/open-autonomous-services> (branch `main`).

Publication happened on 2026-11-16, *after* the 60-minute build window had closed (the code was complete and
verified at about 23:59 UTC on 2026-11-15). It was delayed only because the build session lacked write access to
the GitHub organisation until the Claude GitHub App was installed. No code changed between the verification
below and the push; a fresh clone of the published repository was re-tested afterwards and all 91 tests passed.

The `LICENSE` file is the Apache-2.0 text copied from a license file available in the build environment
(201 lines, headed "Apache License, Version 2.0, January 2004"). Please compare it with
<https://www.apache.org/licenses/LICENSE-2.0.txt> and fill in the copyright holder you want.

## Verified (all measured in this environment)

| Check | Result |
|---|---|
| Full test suite | **91 tests, all passing** (`python -m unittest discover -s tests`) |
| Python versions | passes on 3.11, 3.12, 3.13 (Linux). 3.11 is the declared minimum (`tomllib`) |
| Resource hygiene | suite passes with `-W error::ResourceWarning` (no leaked DB connections) |
| Coverage | library modules (everything in `oas/` except `cli.py`/`__main__.py`): **97%** of statements. The CLI is exercised by subprocess tests (`test_cli.py`, `test_robustness.py`) and so is not counted in the coverage figure |
| Clean install | `pip install .` in a fresh virtualenv, then `oas init / discover / tick` ran the example pipeline successfully |
| README example | the "Writing a service" snippet was executed verbatim and printed `{'text': 'hello world'}` |
| Concurrency | two orchestrators (threads, separate connections) ticking one database file: exactly one run and one claim, in 5 trials per test run, and the test passed 15 consecutive runs |
| Crash safety | a process hard-killed (`os._exit`) mid-run left no claims, state or run record; the database reopened cleanly; the orphaned lease refused a prompt retry and expired as designed |

Capability-level results (see the requirement → test table in `ARCHITECTURE.md` for the mapping):

- **Manifests:** validation of ids, triggers, config schema/defaults/types, retry, continuity, timeout, breaker;
  JSON and TOML loading; unknown keys rejected.
- **Claims and provenance:** deterministic content-addressed ids; supersession and "current" view; lineage across
  inputs and supersession chains, including missing ancestors.
- **Permissions:** undeclared reads/writes/citations/messages denied and audited; wildcard permissions; literal
  claim types enforced; ownership rule for supersession; confidence range; non-JSON content rejected.
- **Execution:** atomic commit (claims + state + messages + cursors); rollback on failure; retries with
  exponential backoff; timeouts with cancelled late writes never committing; circuit breaker open / blocked /
  re-enabled / reset; ephemeral vs persistent continuity; re-registering a new version keeps state and claims.
- **Scheduling and orchestration:** interval, event (with own-write exclusion and at-least-once redelivery after
  failure), dependency, message and manual triggers; topological ordering independent of registration order;
  cascading within a tick; cycle detection and bounded execution; failed services not retried within a tick;
  disabled services skipped; interval failures do not hot-loop.
- **Federation:** export/import round trip preserves ids, origin and lineage; idempotent; tampered claims
  rejected individually; forged local origin rejected; foreign supersession of a local claim rejected;
  malformed bundles rejected; imported claims trigger local event services.
- **End to end:** the three-service research pipeline (monitor → evaluator → reporter) runs through
  discovery, cascading ticks, supersession chain, lineage to the source papers, restart with state intact,
  and replacement of a service implementation.

## Requirement status

| Requirement | Status |
|---|---|
| Independently configured services | Done |
| Standardised interfaces | Done (manifest + `ServiceContext`) |
| Scheduling | Done for interval/event/dependency/message triggers; **no cron/calendar expressions** |
| Execution | Done |
| Persistent state | Done |
| Inter-service communication | Done (claims for shared knowledge, queued messages for direct signals) |
| Shared knowledge | Done |
| Provenance | Done for integrity and lineage; **no cryptographic signatures** |
| Permissions | Done as in-process enforcement; **not a sandbox** |
| Error handling | Done |
| Orchestration | Done (derived graph, bounded cascading, leases) |
| Open, documented, modular | Done (README, ARCHITECTURE, this file; Apache-2.0) |
| Published | Done (after the window; see Publication) |

## Known limitations (please read)

Security
- **Services are trusted code.** Permissions are enforced through `ServiceContext`, but a service is ordinary
  Python in the same process and could open the SQLite file directly or import anything. Manifest `entrypoint`s
  import modules, so never register manifests/entrypoints from sources you do not trust.
- **No authentication of federated claims.** Bundles are integrity-checked (content hash) but not signed.
  `source_service` and `origin` are self-asserted, so a malicious peer can fabricate claims "from" any service id
  (only ids it computes correctly are accepted, and it cannot supersede claims owned by another service/origin
  that exist locally). A supersession whose target is not yet present locally is accepted, because it cannot be
  checked.
- No encryption, access control on the CLI, or secrets handling.

Execution
- **Timeouts cannot kill a thread.** A timed-out function keeps running in the background. Its buffered claims
  and state are discarded, but side effects it performs outside the context (network calls, files) are neither
  rolled back nor stopped. CPU-bound runaways keep consuming CPU.
- **Only context writes are transactional.** External side effects are the service author's responsibility.
- **Leases are time-based** (default 300 s). A run longer than the lease can be started a second time by another
  orchestrator; set `Orchestrator(lease_seconds=…)` above your longest run. A crashed process blocks its service
  until the lease expires.
- Concurrency was tested with threads on a local file. Multiple machines sharing one database over a network
  filesystem are **not supported** (SQLite locking is unreliable there). Multi-node use is via bundle exchange.
- Event delivery is at-least-once. Identical claims (same content, source, time) deduplicate, so a repeated
  identical write is a no-op and does not wake consumers.
- Cycles are detected and execution is bounded by `max_rounds`, but cycles are not prevented.

Data model
- **Claim content is not schema-validated.** Types are plain strings and content is free JSON, so two services can
  agree on a type name and still disagree about meaning. This is the main gap between this framework and a true
  interoperability standard.
- **Confidence is supplied, not computed or calibrated** by the framework.
- No retention, pruning or compaction of claims, runs, messages or the audit log.
- Queries load rows and filter wildcard types in Python. **Performance and scale were not measured.**

Scope
- Discovery is local: manifests in directories plus provider/consumer lookup from manifests. There is no network
  transport, peer discovery, or remote invocation.
- One manifest per service id (re-registering replaces it); no side-by-side versions. A module that has already
  been imported is not hot-reloaded within a running process.
- The example services are deterministic rule-based stubs. **No LLM-backed service and no live data feed was
  built or tested**, although nothing prevents a service function from calling one.
- Tested on Linux only. Not tested on macOS or Windows.

## Suggested next steps (not started)

1. Claim-type schemas (manifest-declared JSON schema for `content`) validated on write and import.
2. Signed claims (per-node keys) so `origin`/`source_service` become authenticated.
3. A transport for bundle exchange (e.g. git or HTTP sync) and peer discovery.
4. Subprocess isolation for services, so timeouts can kill and permissions become a real boundary.
5. Cron-style schedules, retention policies, and a benchmark at realistic claim volumes.

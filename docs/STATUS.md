# Status, verification record and limitations (v0.2.0)

Rule used throughout: a capability is listed under "Verified" only if a test in this repository exercises it and
it passed. Everything else is listed under "Not verified" or "Not implemented". Numbers below were measured in
the build environment (Linux, one machine) and are quoted as measured.

Build history
- **v0.1.0**, 2026-11-15 23:35-23:59 UTC (about 24 minutes of the 60-minute budget): core framework.
- **v0.2.0**, 2026-11-16 (the remaining budget): claim schemas, signed claims, process isolation, cron schedules,
  transports, retention, query optimisation, and a self-review that found and fixed four defects (below).
- The repository was published only after v0.1.0 was built, because the build session had no write access to the
  GitHub organisation until the Claude GitHub App was installed.

## Verified

| Check | Result |
|---|---|
| Full test suite | **159 tests**, all passing (`python -m unittest discover -s tests`) |
| Python versions | passes on 3.11, 3.12, 3.13 (Linux). 3.11 runs with 1 test skipped (see below) |
| Resource hygiene | suite passes with `-W error::ResourceWarning` (no leaked database connections) |
| Flakiness | concurrency suite passed 15 consecutive runs; isolation suite 12; transport suite 10; full suite 5 |
| Coverage | **84%** of statements overall (1,730 statements). The CLI (`cli.py`, `__main__.py`) and the *child half* of `isolation.py` run in subprocesses, so coverage.py does not see them; they are exercised by subprocess tests (`test_cli.py`, `test_isolation.py`, `test_transport.py`). Every other module is 91-100% |
| Clean install | `pip install .` in a fresh virtualenv; `oas init / discover / tick / lineage / export / import` ran the example pipeline (done for v0.1.0; v0.2.0 commands are covered by the CLI tests) |
| Ed25519 | matches RFC 8032 test vectors (run on all versions); additionally cross-checked against the `cryptography` package on 25 random keys/messages (ran on 3.12 and 3.13; **skipped on 3.11**, where the system copy of `cryptography` is broken) |
| Concurrency | two orchestrators on one database file run a service once (5 trials per run) |
| Crash safety | a hard-killed process leaves no partial commit; the lease expires and the service recovers |

What each area was tested for (the requirement to test map is in `ARCHITECTURE.md`):

- **Manifests, claims, provenance, permissions, execution, scheduling, orchestration, federation**: as in v0.1.0.
- **Claim schemas**: validator keywords and every violation kind; malformed schemas rejected; enforced on write
  (audited, nothing committed) and on import (against schemas declared by local services); `validate()` warns when
  services disagree about a type's schema.
- **Signed claims**: local claims signed at commit; signature covers the claim id *and* origin; altered origin,
  swapped, garbage and half-present signatures rejected on import; unsigned claims accepted by default and
  rejected under `--require-signatures`; trusted-signer policy limits which services a key may vouch for;
  key persists across restarts and is never exported; old databases upgrade in place.
- **Process isolation**: runs in another process; state, logs, reads, citations and messages round-trip over RPC;
  permissions enforced in the parent and audited; the child can catch `PermissionDenied`; **a timeout kills the
  process (the test checks the child's PID is gone)**; crash and exception contained; memory limit contains an
  allocation hog; prints cannot corrupt the protocol; a child printing 2 MB does not deadlock; retries apply.
- **Cron**: parsing, 13 next-fire cases (month ends, leap day, day-of-month/day-of-week rule, Sunday as 0 or 7),
  invalid expressions, fires once per slot, missed slots coalesce, a failed slot is consumed (no hot retry).
- **Transports**: three nodes converge through a shared directory; re-sync is idempotent; bad files are reported
  without blocking good ones; trust policy applies; HTTP pull with `since`/type filters, read-only server
  (405 on writes), non-http URLs refused; CLI serve + pull between processes.
- **Retention**: runs, audit, consumed messages and superseded claims pruned; the latest success per service
  and all current claims and unconsumed messages are never deleted; dependency triggers keep working after pruning.

## Self-review: defects found and fixed in v0.2.0

Each was reproduced with a script before being fixed and now has a regression test (`tests/test_regressions.py`).

1. **Supersession hijack (integrity).** A peer could hide someone else's claim by publishing a claim that names it
   in `supersedes` *before* the target arrived (ownership cannot be checked until it does). Fixed: a supersession
   only counts when the superseding claim has the same service, origin and type; ignored edges are also ignored
   by lineage and pruning; imports reject a known cross-owner or cross-type target.
2. **Isolation deadlock.** An isolated child that printed a lot (its prints are redirected to stderr) blocked
   forever on an undrained pipe until the timeout. Fixed: stderr goes to a temporary file.
3. **One hostile file aborted a whole directory sync** (deeply nested JSON raised `RecursionError`), and on
   Python 3.11 deeply nested claim content aborted a whole import. Fixed: per-file and per-claim containment.
4. **Resource leak**: `Store` leaked its SQLite connection if construction failed. Also: `node_id` (which becomes
   a filename in directory sync) is now validated.

A performance regression introduced by fix 1 (queries 100x slower, caught by re-running the benchmark) was found
and fixed in the same session; a test now guards the query plan.

**Not done: an independent review.** A fresh reviewing agent was started to audit this code but failed immediately
with an API rate-limit error, so no independent review took place. The self-review above is the author reviewing
their own work, and was necessarily limited to what I thought to probe. Treat the security-relevant modules
(`context.py`, `federation.py`, `store.py` signing, `isolation.py`) as unaudited.

## Measured performance (`python benchmarks/bench.py 20000`)

| Operation | Time |
|---|---|
| Service run writing 1,000 claims (signed, one transaction) | 2,718 ms (2.7 ms/claim; signing dominates) |
| Direct insert of 20,000 unsigned claims | 255 ms |
| Query one literal type among 21,000 claims | 22 ms |
| Query a wildcard type among 21,000 claims | 45 ms |
| Query with `limit=10` | under 1 ms |
| Event-trigger check with 21,000 claims | 20 ms |
| Lineage of a 300-deep supersession chain | 6 ms |
| `tick()` running 100 due services | 323 ms (3.2 ms/service) |
| `tick()` with nothing due (102 services) | 4 ms |
| Export 21,300 claims | 145 ms |
| Import 300 signed claims (verifies signatures) | 872 ms (2.9 ms/claim) |
| Import 5,000 unsigned claims | 98 ms |
| Process-isolated service run | about 43 ms/run (versus 0.1 ms in-process) |

Pure-Python Ed25519 costs about 3 ms per sign or verify, which caps signed writes at roughly 350 claims/s per
process; pass `Store(path, sign=False)` to trade authenticity for throughput. These are single-machine
micro-benchmarks on small claims, not a capacity guarantee. Nothing above 21,000 claims was measured.

## Known limitations (please read)

Security
- **Process isolation is not a hardened sandbox.** Isolated services run as the same OS user, so malicious code
  could still read the database file or use the network directly. It contains crashes, hangs and memory use;
  permissions are enforced because the child only holds an RPC channel, not because it is denied the OS.
  Thread-mode services (the default) are trusted in-process code. Never register manifests/entrypoints from
  untrusted sources: they cause modules to be imported.
- **Signatures prove who signed, not that content is true.** The trust policy says which keys may vouch for which
  service names; there is no key rotation, revocation, or web-of-trust. Unsigned claims are accepted unless you
  require signatures. A signed claim cannot be un-signed or retracted except by superseding it.
- **The private signing key is stored in plaintext in the node's SQLite file.** Protect the file; it is never
  included in exports.
- The pure-Python Ed25519 is not constant-time (see `oas/ed25519.py`). Swap in a native library if timing
  attacks matter to you.
- **HTTP transport has no authentication or encryption.** Bind to localhost or a trusted network. Integrity does
  not depend on the transport (ids and signatures are checked on import), but confidentiality does not exist.
  A peer can still send large or numerous claims (resource exhaustion); there are size limits on pull
  (256 MB) but no rate limits.
- A claim superseding a target that has not yet arrived is accepted, but does not hide that target
  (see defect 1). A supersession by a different owner is simply ignored.

Execution
- **Thread-mode timeouts cannot kill a thread** (use `isolation: "process"` for killable timeouts). Side effects
  outside the context are neither rolled back nor stopped.
- Leases are time-based (default 300 s); a run longer than the lease can be started a second time by another
  orchestrator. A crashed process blocks its service until the lease expires.
- Concurrency was tested with threads/processes on a local file. A database on a network filesystem is **not
  supported**; use bundle exchange between nodes.
- Event delivery is at-least-once. Identical claims (same content, source and timestamp) deduplicate.
- Cycles are detected and bounded by `max_rounds`, not prevented.
- Cron: UTC only, 5 fields, numeric values only (no `mon`/`jan` names or `@daily`). Missed slots coalesce into
  one run. Resolution is one minute and depends on how often something calls `tick()`; the framework does not
  run its own scheduler loop.

Data model
- Claim types are plain strings; schemas are an opt-in JSON-Schema *subset* (see `oas/schema.py`), checked only
  where a service declares one. Two services that declare no schema can still disagree about meaning.
- Confidence is supplied by services, not computed or calibrated.
- Pruning deletes history; `lineage()` then reports pruned ancestors as missing. Nothing prunes automatically.
- Bundle exchange sends whole claim sets (optionally `since` a sequence number or by type); there is no efficient
  delta protocol between arbitrary peers.

Scope
- Discovery is local (manifests in directories, provider/consumer lookup). There is no peer discovery.
- One manifest per service id; re-registering replaces it. A module already imported is not hot-reloaded.
- The example services are deterministic rule-based stubs. **No LLM-backed service and no live data feed was
  built or tested**, though nothing prevents a service function from calling one.
- Linux only was tested. Process isolation uses `subprocess` + `socketpair` + `resource` and has not been tried
  on macOS or Windows (the memory limit is skipped where `resource` is unavailable).

## Suggested next steps (not started)

1. An independent security review of `context.py`, `federation.py`, signing and `isolation.py`.
2. Key rotation/revocation and encrypted key storage; optional native Ed25519 backend.
3. Authentication and TLS for the HTTP transport; delta sync between peers; peer discovery.
4. A built-in scheduler loop (`oas run`) and richer cron (names, time zones).
5. A reference LLM-backed service and a benchmark at millions of claims.

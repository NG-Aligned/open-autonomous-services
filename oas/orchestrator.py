"""Scheduler, executor and orchestrator.

Coordination only: the orchestrator derives order from manifests, evaluates
triggers, runs services with retries/timeouts/circuit-breaking and commits each
run atomically. It holds no domain intelligence.
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .context import ServiceContext
from .errors import (ManifestError, ServiceDisabled, ServiceNotBound, ServiceNotFound,
                     ServiceTimeout)
from . import cron as _cron
from .isolation import DEFAULT_TIMEOUT, run_in_process
from .federation import import_bundle as _import_bundle
from .graph import build_edges, find_cycles, topo_order, types_overlap
from .manifest import Manifest, can_read, load_manifest, matches
from .store import Store

ServiceFn = Callable[[ServiceContext], None]


@dataclass
class RunResult:
    service_id: str
    status: str                      # "success" | "failed" | "skipped"
    attempts: int
    run_id: str | None = None
    claims_written: int = 0
    error: str | None = None
    logs: list[str] = field(default_factory=list)


@dataclass
class TickResult:
    runs: list[RunResult]
    rounds: int
    truncated: bool


def load_entrypoint(entrypoint: str, base_dir: str | None) -> ServiceFn:
    """Import 'module:function'. The module is trusted code (see docs/LIMITATIONS.md)."""
    mod_name, _, attr = entrypoint.partition(":")
    if base_dir and base_dir not in sys.path:
        sys.path.insert(0, base_dir)
    module = importlib.import_module(mod_name)
    fn = getattr(module, attr, None)
    if not callable(fn):
        raise ServiceNotBound(f"entrypoint {entrypoint} is not callable")
    return fn


class Orchestrator:
    def __init__(self, store: Store, *, sleep: Callable[[float], None] = time.sleep,
                 lease_seconds: float = 300.0) -> None:
        self.store = store
        self.sleep = sleep
        self.lease_seconds = lease_seconds
        self._owner = uuid.uuid4().hex
        self._fns: dict[str, ServiceFn] = {}

    # ------------------------------------------------------------------
    # Registration and discovery
    # ------------------------------------------------------------------
    @property
    def clock(self) -> Callable[[], float]:
        return self.store.clock

    def register(self, manifest: Manifest | dict[str, Any], fn: ServiceFn | None = None,
                 base_dir: str | None = None) -> Manifest:
        m = manifest if isinstance(manifest, Manifest) else Manifest.from_dict(manifest)
        m.resolve_config()
        if fn is None and not m.entrypoint and m.id not in self._fns:
            raise ManifestError(f"service '{m.id}' has no entrypoint and no function was bound")
        self.store.upsert_service(m.to_dict(), base_dir)
        if fn is not None:
            self._fns[m.id] = fn
        else:
            self._fns.pop(m.id, None)
        self.store.audit(m.id, "registered", f"version {m.version}", True)
        return m

    def bind(self, service_id: str, fn: ServiceFn) -> None:
        if self.store.get_service(service_id) is None:
            raise ServiceNotFound(service_id)
        self._fns[service_id] = fn

    def discover(self, directory: str | Path) -> tuple[list[str], dict[str, str]]:
        """Register every *.json / *.toml manifest in a directory. Bad manifests are reported, not fatal."""
        registered: list[str] = []
        errors: dict[str, str] = {}
        d = Path(directory).resolve()
        for p in sorted(list(d.glob("*.json")) + list(d.glob("*.toml"))):
            try:
                m = load_manifest(p)
                self.register(m, base_dir=str(d))
                registered.append(m.id)
            except Exception as exc:  # noqa: BLE001 - report, do not abort discovery
                errors[p.name] = f"{type(exc).__name__}: {exc}"
        return registered, errors

    def enable(self, service_id: str) -> None:
        self._require(service_id)
        self.store.update_service(service_id, enabled=True, consecutive_failures=0, disabled_reason=None)
        self.store.audit(service_id, "enabled", "", True)

    def disable(self, service_id: str, reason: str = "disabled by operator") -> None:
        self._require(service_id)
        self.store.update_service(service_id, enabled=False, disabled_reason=reason)
        self.store.audit(service_id, "disabled", reason, True)

    def _require(self, service_id: str) -> dict[str, Any]:
        svc = self.store.get_service(service_id)
        if svc is None:
            raise ServiceNotFound(service_id)
        return svc

    def _manifests(self) -> dict[str, Manifest]:
        return {s["id"]: Manifest.from_dict(s["manifest"]) for s in self.store.list_services()}

    def schemas_for(self, claim_type: str) -> list[dict[str, Any]]:
        """All schemas any registered service declares for a literal claim type."""
        out: list[dict[str, Any]] = []
        for m in self._manifests().values():
            out += m.schemas_for(claim_type)
        return out

    def import_bundle(self, bundle: dict[str, Any], *, require_trusted: bool = False,
                      require_signatures: bool = False) -> dict[str, Any]:
        """Import a bundle: integrity, signatures, local schemas and (optionally) the trusted-signer policy.

        With require_trusted, every claim must be signed by a key registered via Store.add_trusted_signer whose
        allowed service patterns match the claim's source_service.
        """
        return _import_bundle(self.store, bundle, schema_lookup=self.schemas_for,
                              trusted=self.store.trusted_signers() if require_trusted else None,
                              require_signatures=require_signatures or require_trusted)

    def find_providers(self, claim_type: str) -> list[str]:
        return sorted(i for i, m in self._manifests().items()
                      if any(types_overlap(w, claim_type) for w in m.writes))

    def find_consumers(self, claim_type: str) -> list[str]:
        return sorted(i for i, m in self._manifests().items()
                      if any(types_overlap(r, claim_type) for r in m.reads))

    # ------------------------------------------------------------------
    # Graph and validation
    # ------------------------------------------------------------------
    def graph(self) -> dict[str, Any]:
        ms = self._manifests()
        edges = build_edges(ms)
        nodes = sorted(ms)
        order, _ = topo_order(nodes, edges)
        return {
            "edges": [{"from": a, "to": b, "via": sorted(v)} for (a, b), v in sorted(edges.items())],
            "order": order,
            "cycles": find_cycles(nodes, edges),
        }

    def validate(self) -> list[dict[str, str]]:
        ms = self._manifests()
        issues: list[dict[str, str]] = []
        all_writes = [w for m in ms.values() for w in m.writes]
        for m in ms.values():
            for t in m.triggers:
                if t["kind"] == "dependency" and t["service"] not in ms:
                    issues.append({"level": "error", "service": m.id,
                                   "message": f"dependency trigger on unknown service '{t['service']}'"})
                if t["kind"] == "event" and not any(types_overlap(w, t["claim_type"]) for w in all_writes):
                    issues.append({"level": "warning", "service": m.id,
                                   "message": f"no registered service writes '{t['claim_type']}' (event may only fire via imports)"})
            for r in m.reads:
                if not any(types_overlap(w, r) for w in all_writes):
                    issues.append({"level": "warning", "service": m.id,
                                   "message": f"reads '{r}' but no registered service writes it"})
            for target in m.can_message:
                if not any(matches([target], other) for other in ms):
                    issues.append({"level": "warning", "service": m.id,
                                   "message": f"can_message target '{target}' matches no registered service"})
        by_type: dict[str, list[tuple[str, str]]] = {}
        for m in ms.values():
            for ctype, sch in m.claim_schemas.items():
                by_type.setdefault(ctype, []).append((m.id, json.dumps(sch, sort_keys=True)))
        for ctype, decls in sorted(by_type.items()):
            if len({d[1] for d in decls}) > 1:
                who = ",".join(sorted({d[0] for d in decls}))
                issues.append({"level": "warning", "service": who,
                               "message": f"services declare different schemas for claim type '{ctype}'"})
        for cyc in self.graph()["cycles"]:
            issues.append({"level": "warning", "service": ",".join(cyc),
                           "message": "dependency cycle; execution is bounded by max_rounds"})
        return issues

    # ------------------------------------------------------------------
    # Trigger evaluation
    # ------------------------------------------------------------------
    def due_reasons(self, service_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Why (if at all) a service should run now, plus cursor updates to commit on success."""
        svc = self._require(service_id)
        if not svc["enabled"]:
            return [], {}
        m = Manifest.from_dict(svc["manifest"])
        now = self.clock()
        cursors: dict[str, Any] = svc["cursors"]
        reasons: list[dict[str, Any]] = []
        updates: dict[str, Any] = {}
        snapshot = self.store.max_seq()
        for t in m.triggers:
            kind = t["kind"]
            if kind == "interval":
                if svc["next_due"] <= now:
                    reasons.append({"kind": "interval", "seconds": t["seconds"]})
            elif kind == "cron":
                key = f"cron:{t['expr']}"
                nxt = cursors.get(key)
                if nxt is None:                      # first evaluation: next fire after registration
                    nxt = _cron.next_fire(t["expr"], svc["registered_at"])
                if nxt <= now:                       # missed firings (downtime) coalesce into one run
                    reasons.append({"kind": "cron", "expr": t["expr"], "scheduled_for": nxt})
                    updates[key] = _cron.next_fire(t["expr"], now)
            elif kind == "event":
                key = f"event:{t['claim_type']}"
                new = self.store.query_claims([t["claim_type"]], current_only=False,
                                              after_seq=cursors.get(key, 0), exclude_local_service=service_id)
                new = [c for c in new if c.seq is not None and c.seq <= snapshot]
                if new:
                    reasons.append({"kind": "event", "claim_type": t["claim_type"], "count": len(new),
                                    "claim_ids": [c.id for c in new if can_read(m.reads, c.type)][:5]})
                    updates[key] = snapshot
            elif kind == "dependency":
                key = f"dep:{t['service']}"
                s = self.store.last_success_seq(t["service"])
                if s > cursors.get(key, 0):
                    reasons.append({"kind": "dependency", "service": t["service"]})
                    updates[key] = s
            elif kind == "message":
                n = len(self.store.pending_messages(service_id))
                if n:
                    reasons.append({"kind": "message", "count": n})
        return reasons, updates

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _resolve_fn(self, service_id: str, manifest: Manifest, base_dir: str | None) -> ServiceFn:
        fn = self._fns.get(service_id)
        if fn is not None:
            return fn
        if manifest.entrypoint:
            fn = load_entrypoint(manifest.entrypoint, base_dir)
            self._fns[service_id] = fn
            return fn
        raise ServiceNotBound(f"no function bound for '{service_id}'")

    @staticmethod
    def _call(fn: ServiceFn, ctx: ServiceContext, timeout: float | None) -> None:
        if timeout is None:
            fn(ctx)
            return
        holder: dict[str, BaseException] = {}

        def target() -> None:
            try:
                fn(ctx)
            except BaseException as exc:  # noqa: BLE001 - re-raised in caller
                holder["exc"] = exc

        t = threading.Thread(target=target, daemon=True, name=f"oas-{ctx.service_id}")
        t.start()
        t.join(timeout)
        if t.is_alive():
            ctx._cancelled = True
            raise ServiceTimeout(f"exceeded {timeout}s")
        if "exc" in holder:
            raise holder["exc"]

    def run_service(self, service_id: str, *, reasons: list[dict[str, Any]] | None = None,
                    cursor_updates: dict[str, Any] | None = None, recheck: bool = False) -> RunResult:
        """Run one service under an execution lease so concurrent orchestrators cannot double-run it.

        Returns status "skipped" if another orchestrator holds the lease, or (with recheck=True)
        if the service stopped being due once the lease was taken.
        """
        svc = self._require(service_id)
        if not svc["enabled"]:
            raise ServiceDisabled(f"{service_id}: {svc['disabled_reason'] or 'disabled'}")
        if not self.store.acquire_lease(service_id, self._owner, self.lease_seconds):
            return RunResult(service_id, "skipped", 0, None, 0, "service is leased by another orchestrator")
        try:
            if recheck:
                reasons, cursor_updates = self.due_reasons(service_id)
                if not reasons:
                    return RunResult(service_id, "skipped", 0, None, 0, "no longer due")
                svc = self._require(service_id)
            return self._run_leased(service_id, svc, reasons, cursor_updates)
        finally:
            self.store.release_lease(service_id, self._owner)

    def _run_leased(self, service_id: str, svc: dict[str, Any], reasons: list[dict[str, Any]] | None,
                    cursor_updates: dict[str, Any] | None) -> RunResult:
        manifest = Manifest.from_dict(svc["manifest"])
        reasons = reasons if reasons is not None else [{"kind": "manual"}]
        interval = min((t["seconds"] for t in manifest.triggers if t["kind"] == "interval"), default=None)
        max_attempts = manifest.retry["max_attempts"]
        backoff = manifest.retry["backoff_seconds"]
        last_error: str | None = None
        last_logs: list[str] = []
        run_id = ""
        for attempt in range(1, max_attempts + 1):
            run_id = uuid.uuid4().hex[:12]
            started = self.clock()
            ctx: ServiceContext | None = None
            try:
                config = manifest.resolve_config()
                state = {} if manifest.continuity == "ephemeral" else self.store.get_state(service_id)
                ctx = ServiceContext(self.store, manifest, config, json.loads(json.dumps(state)), reasons, run_id)
                if manifest.isolation == "process":
                    run_in_process(ctx, manifest.entrypoint, svc["base_dir"],
                                   manifest.timeout_seconds if manifest.timeout_seconds is not None else DEFAULT_TIMEOUT,
                                   manifest.max_memory_mb)
                else:
                    fn = self._resolve_fn(service_id, manifest, svc["base_dir"])
                    self._call(fn, ctx, manifest.timeout_seconds)
                written = self._commit(ctx, manifest, svc, cursor_updates or {}, interval, started, attempt, reasons)
            except Exception as exc:  # noqa: BLE001 - every service failure is contained here
                if ctx is not None:
                    ctx._cancelled = True
                last_error = f"{type(exc).__name__}: {exc}"
                last_logs = list(ctx._logs) if ctx is not None else []
                status = "timeout" if isinstance(exc, ServiceTimeout) else "failed"
                self.store.add_run(run_id=run_id, service_id=service_id, attempt=attempt, status=status,
                                   error=last_error, started_at=started, finished_at=self.clock(),
                                   claims_written=0, trigger=reasons, log=last_logs)
                if attempt < max_attempts:
                    self.sleep(backoff * (2 ** (attempt - 1)))
                continue
            return RunResult(service_id, "success", attempt, run_id, written, None, list(ctx._logs))
        return self._record_exhausted(service_id, manifest, svc, interval, max_attempts, run_id,
                                      last_error or "unknown error", last_logs, cursor_updates or {})

    def _commit(self, ctx: ServiceContext, manifest: Manifest, svc: dict[str, Any],
                cursor_updates: dict[str, Any], interval: float | None, started: float,
                attempt: int, reasons: list[dict[str, Any]]) -> int:
        json.dumps(ctx.state, allow_nan=False)  # fail before opening the transaction
        written = 0
        with self.store.transaction():
            for claim in ctx._pending:
                if self.store.insert_claim(self.store.sign_claim(claim) if self.store.sign else claim):
                    written += 1
            self.store.set_state(manifest.id, ctx.state if manifest.continuity == "persist" else {})
            for to, topic, payload in ctx._outgoing:
                self.store.add_message(manifest.id, to, topic, payload)
            self.store.mark_consumed(ctx._consumed)
            cursors = dict(self.store.get_service(manifest.id)["cursors"])
            cursors.update(cursor_updates)
            fields: dict[str, Any] = {"cursors": cursors, "consecutive_failures": 0}
            if interval is not None:
                fields["next_due"] = self.clock() + interval
            self.store.update_service(manifest.id, **fields)
            self.store.add_run(run_id=ctx.run_id, service_id=manifest.id, attempt=attempt, status="success",
                               error=None, started_at=started, finished_at=self.clock(),
                               claims_written=written, trigger=reasons, log=ctx._logs)
        return written

    def _record_exhausted(self, service_id: str, manifest: Manifest, svc: dict[str, Any],
                          interval: float | None, attempts: int, run_id: str, error: str,
                          logs: list[str], cursor_updates: dict[str, Any]) -> RunResult:
        fresh = self._require(service_id)
        failures = fresh["consecutive_failures"] + 1
        fields: dict[str, Any] = {"consecutive_failures": failures}
        if interval is not None:
            fields["next_due"] = self.clock() + interval
        cron_updates = {k: v for k, v in cursor_updates.items() if k.startswith("cron:")}
        if cron_updates:                 # a failed scheduled slot is consumed (like interval): no hot retry loop
            fields["cursors"] = {**fresh["cursors"], **cron_updates}
        self.store.update_service(service_id, **fields)
        if failures >= manifest.circuit_breaker:
            reason = f"circuit breaker: {failures} consecutive failures; last error: {error}"
            self.store.update_service(service_id, enabled=False, disabled_reason=reason)
            self.store.audit(service_id, "circuit_breaker_open", reason, True)
        return RunResult(service_id, "failed", attempts, run_id, 0, error, logs)

    # ------------------------------------------------------------------
    # Scheduling loop
    # ------------------------------------------------------------------
    def tick(self, max_rounds: int = 10) -> TickResult:
        """Run everything that is due, in dependency order, cascading until idle or bounded."""
        results: list[RunResult] = []
        failed: set[str] = set()
        rounds = 0
        truncated = False
        while True:
            ids = [s["id"] for s in self.store.list_services() if s["enabled"] and s["id"] not in failed]
            order, _ = topo_order(sorted(ids), build_edges(self._manifests()))
            due = [i for i in order if i in ids and self.due_reasons(i)[0]]
            if not due:
                break
            if rounds >= max_rounds:
                truncated = True
                break
            rounds += 1
            for sid in due:
                svc = self.store.get_service(sid)
                if svc is None or not svc["enabled"]:
                    continue
                reasons, updates = self.due_reasons(sid)  # recompute: earlier runs may have changed it
                if not reasons:
                    continue
                res = self.run_service(sid, reasons=reasons, cursor_updates=updates, recheck=True)
                results.append(res)
                if res.status != "success":      # failed or skipped: do not retry within this tick
                    failed.add(sid)
        return TickResult(results, rounds, truncated)

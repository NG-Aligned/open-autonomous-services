"""The ServiceContext: the only handle a service has on the outside world.

Writes, messages and state changes are buffered and committed atomically by the
orchestrator only if the service function returns successfully.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from .errors import ClaimError, MessageError, PermissionDenied, ServiceCancelled
from .manifest import Manifest, has_wildcard, matches
from . import schema as _schema
from .store import Claim, Store, compute_claim_id

MAX_LOG_LINES = 200


def _jsonable(value: Any, what: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ClaimError(f"{what} is not JSON-serialisable: {exc}") from None


class ServiceContext:
    def __init__(self, store: Store, manifest: Manifest, config: dict[str, Any],
                 state: dict[str, Any], trigger: list[dict[str, Any]], run_id: str) -> None:
        self._store = store
        self._manifest = manifest
        self.service_id = manifest.id
        self.config = config
        self.state = state
        self.trigger = trigger
        self.run_id = run_id
        self._pending: list[Claim] = []
        self._pending_by_id: dict[str, Claim] = {}
        self._outgoing: list[tuple[str, str, Any]] = []
        self._consumed: set[int] = set()
        self._logs: list[str] = []
        self._cancelled = False

    # -- internals ------------------------------------------------------
    def _guard(self) -> None:
        if self._cancelled:
            raise ServiceCancelled(f"{self.service_id}: run was cancelled")

    def _deny(self, action: str, detail: str) -> None:
        self._store.audit(self.service_id, action, detail, allowed=False)
        raise PermissionDenied(f"{self.service_id}: {action} denied ({detail})")

    def _can_read(self, claim_type: str) -> bool:
        reads = self._manifest.reads
        return claim_type in reads or matches(reads, claim_type)

    def _lookup(self, claim_id: str) -> Claim | None:
        return self._pending_by_id.get(claim_id) or self._store.get_claim(claim_id)

    # -- public API -----------------------------------------------------
    def now(self) -> float:
        return self._store.clock()

    def log(self, message: str) -> None:
        if len(self._logs) < MAX_LOG_LINES:
            self._logs.append(str(message)[:500])

    def read(self, claim_type: str, *, current_only: bool = True, after_seq: int | None = None,
             limit: int | None = None, source_service: str | None = None) -> list[Claim]:
        """Read committed claims of a permitted type (own uncommitted writes are not visible)."""
        self._guard()
        if not self._can_read(claim_type):
            self._deny("read", claim_type)
        return self._store.query_claims([claim_type], current_only=current_only, after_seq=after_seq,
                                        limit=limit, source_service=source_service)

    def get_claim(self, claim_id: str) -> Claim | None:
        self._guard()
        claim = self._lookup(claim_id)
        if claim is not None and claim.id not in self._pending_by_id and not self._can_read(claim.type):
            self._deny("read", claim.type)
        return claim

    def write(self, claim_type: str, content: Any, *, confidence: float = 1.0,
              inputs: Iterable[str] = (), supersedes: str | None = None) -> str:
        """Buffer a claim. Returns its content-addressed id."""
        self._guard()
        if not claim_type or has_wildcard(claim_type):
            raise ClaimError("claim type must be a non-empty literal (no wildcards)")
        if not matches(self._manifest.writes, claim_type):
            self._deny("write", claim_type)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            raise ClaimError("confidence must be a number between 0 and 1")
        content = _jsonable(content, "claim content")
        for sch in self._manifest.schemas_for(claim_type):
            problems = _schema.validate(content, sch)
            if problems:
                self._store.audit(self.service_id, "schema_violation", f"{claim_type}: {problems[0]}", False)
                raise ClaimError(f"content of '{claim_type}' violates its schema: {problems[0]}")
        input_ids = tuple(sorted(set(inputs)))
        for cid in input_ids:
            cited = self._lookup(cid)
            if cited is None:
                raise ClaimError(f"input claim {cid[:12]} does not exist")
            if cid not in self._pending_by_id and not self._can_read(cited.type):
                self._deny("cite", f"{cited.type} (input {cid[:12]})")
        if supersedes is not None:
            target = self._lookup(supersedes)
            if target is None:
                raise ClaimError(f"superseded claim {supersedes[:12]} does not exist")
            if target.type != claim_type:
                raise ClaimError("a claim may only supersede a claim of the same type")
            if target.source_service != self.service_id:
                self._deny("supersede", f"claim {supersedes[:12]} belongs to {target.source_service}")
            if self._store.is_superseded(supersedes) or any(p.supersedes == supersedes for p in self._pending):
                raise ClaimError(f"claim {supersedes[:12]} is already superseded")
        created_at = self.now()
        confidence = float(confidence)
        cid = compute_claim_id(claim_type, content, self.service_id, input_ids, confidence,
                               created_at, supersedes)
        if cid not in self._pending_by_id:
            claim = Claim(id=cid, type=claim_type, content=content, source_service=self.service_id,
                          inputs=input_ids, confidence=confidence, created_at=created_at,
                          supersedes=supersedes, origin=self._store.node_id, run_id=self.run_id)
            self._pending.append(claim)
            self._pending_by_id[cid] = claim
        return cid

    def send(self, to_service: str, topic: str, payload: Any = None) -> None:
        self._guard()
        if not matches(self._manifest.can_message, to_service):
            self._deny("message", to_service)
        if self._store.get_service(to_service) is None:
            raise MessageError(f"unknown service '{to_service}'")
        self._outgoing.append((to_service, str(topic), _jsonable(payload, "message payload")))

    def inbox(self) -> list[dict[str, Any]]:
        """Pending messages for this service; consumed only if the run succeeds."""
        self._guard()
        msgs = self._store.pending_messages(self.service_id)
        self._consumed.update(m["id"] for m in msgs)
        return msgs

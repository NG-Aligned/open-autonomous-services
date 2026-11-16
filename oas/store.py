"""SQLite-backed store: claims with provenance, service rows, state, runs, messages, audit."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Iterator

from . import ed25519
from .errors import ClaimError, StoreError
from .manifest import matches

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  content TEXT NOT NULL,
  source_service TEXT NOT NULL,
  inputs TEXT NOT NULL,
  confidence REAL NOT NULL,
  created_at REAL NOT NULL,
  supersedes TEXT,
  origin TEXT NOT NULL,
  run_id TEXT,
  signer TEXT,
  signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_type ON claims(type);
CREATE INDEX IF NOT EXISTS idx_claims_supersedes ON claims(supersedes);
CREATE INDEX IF NOT EXISTS idx_claims_type_seq ON claims(type, seq);
CREATE TABLE IF NOT EXISTS services(
  id TEXT PRIMARY KEY,
  manifest TEXT NOT NULL,
  base_dir TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  disabled_reason TEXT,
  next_due REAL NOT NULL DEFAULT 0,
  cursors TEXT NOT NULL DEFAULT '{}',
  registered_at REAL NOT NULL,
  lease_owner TEXT,
  lease_until REAL
);
CREATE TABLE IF NOT EXISTS service_state(
  service_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, service_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  status TEXT NOT NULL, error TEXT, started_at REAL NOT NULL, finished_at REAL NOT NULL,
  claims_written INTEGER NOT NULL DEFAULT 0, trigger TEXT, log TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_service TEXT NOT NULL, to_service TEXT NOT NULL, topic TEXT NOT NULL,
  payload TEXT NOT NULL, created_at REAL NOT NULL, consumed_at REAL);
CREATE TABLE IF NOT EXISTS trusted_signers(
  pubkey TEXT PRIMARY KEY, label TEXT NOT NULL, services TEXT NOT NULL, added_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, service_id TEXT, action TEXT NOT NULL, detail TEXT, allowed INTEGER NOT NULL);
"""


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def compute_claim_id(
    type: str, content: Any, source_service: str, inputs: Iterable[str],
    confidence: float, created_at: float, supersedes: str | None,
) -> str:
    """Content-addressed claim id: sha256 over the canonical claim body."""
    try:
        body = canonical({
            "type": type, "content": content, "source_service": source_service,
            "inputs": list(inputs), "confidence": confidence,
            "created_at": created_at, "supersedes": supersedes,
        })
    except (TypeError, ValueError) as exc:
        raise ClaimError(f"claim is not JSON-serialisable: {exc}") from None
    except RecursionError:      # raised on some Python versions for deeply nested content
        raise ClaimError("claim content is nested too deeply") from None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


SIG_DOMAIN = b"oas-claim/1:"


def signing_message(claim_id: str, origin: str) -> bytes:
    """What a signature covers: the content-addressed id (which commits to type, content, source_service,
    inputs, confidence, time and supersession) plus the origin node id (which the id does not cover)."""
    return SIG_DOMAIN + claim_id.encode("ascii") + b":" + origin.encode("utf-8")


def verify_wire_signature(wire: dict[str, Any]) -> str:
    """'unsigned' | 'valid' | 'invalid' for a wire-format claim."""
    signer, sig = wire.get("signer"), wire.get("signature")
    if not signer and not sig:
        return "unsigned"
    try:
        ok = ed25519.verify(bytes.fromhex(signer), signing_message(wire["id"], wire["origin"]),
                            bytes.fromhex(sig))
    except (TypeError, ValueError):
        return "invalid"
    return "valid" if ok else "invalid"


@dataclass(frozen=True)
class Claim:
    id: str
    type: str
    content: Any
    source_service: str
    inputs: tuple
    confidence: float
    created_at: float
    supersedes: str | None
    origin: str
    seq: int | None = None
    run_id: str | None = None
    signer: str | None = None
    signature: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "content": self.content,
            "source_service": self.source_service, "inputs": list(self.inputs),
            "confidence": self.confidence, "created_at": self.created_at,
            "supersedes": self.supersedes, "origin": self.origin,
            "signer": self.signer, "signature": self.signature,
        }


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        id=row["id"], type=row["type"], content=json.loads(row["content"]),
        source_service=row["source_service"], inputs=tuple(json.loads(row["inputs"])),
        confidence=row["confidence"], created_at=row["created_at"],
        supersedes=row["supersedes"], origin=row["origin"], seq=row["seq"], run_id=row["run_id"],
        signer=row["signer"], signature=row["signature"],
    )


class Store:
    def __init__(self, path: str = ":memory:", *, clock: Callable[[], float] = time.time,
                 node_id: str | None = None, sign: bool = True) -> None:
        self.path = str(path)
        self.sign = sign   # sign locally created claims (~3 ms each); disable for bulk throughput
        self.clock = clock
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._in_tx = False
        try:
            with self._lock:
                self._conn.executescript(SCHEMA)
                cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(services)")}
                for col, typ in (("lease_owner", "TEXT"), ("lease_until", "REAL")):
                    if col not in cols:  # upgrade databases created before leases existed
                        self._conn.execute(f"ALTER TABLE services ADD COLUMN {col} {typ}")
                ccols = {r["name"] for r in self._conn.execute("PRAGMA table_info(claims)")}
                for col in ("signer", "signature"):
                    if col not in ccols:  # upgrade databases created before signatures existed
                        self._conn.execute(f"ALTER TABLE claims ADD COLUMN {col} TEXT")
                row = self._conn.execute("SELECT value FROM meta WHERE key='node_id'").fetchone()
                if row:
                    self.node_id = row["value"]
                else:
                    self.node_id = node_id or uuid.uuid4().hex[:12]
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.node_id) or self.node_id.startswith("."):
                        raise StoreError("node_id must be 1-64 characters of letters, digits, _ . - (it is used in filenames)")
                    self._conn.execute("INSERT INTO meta(key, value) VALUES('node_id', ?)", (self.node_id,))
                seed_row = self._conn.execute("SELECT value FROM meta WHERE key='signing_seed'").fetchone()
                if seed_row:
                    self._seed = bytes.fromhex(seed_row["value"])
                else:
                    self._seed = os.urandom(32)
                    self._conn.execute("INSERT INTO meta(key, value) VALUES('signing_seed', ?)", (self._seed.hex(),))
                self.public_key = ed25519.public_key(self._seed).hex()
        except BaseException:
            self._conn.close()      # do not leak the connection if construction fails
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- helpers --------------------------------------------------------
    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._in_tx:
                raise StoreError("nested transactions are not supported")
            self._conn.execute("BEGIN IMMEDIATE")
            self._in_tx = True
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._in_tx = False

    # -- claims ---------------------------------------------------------
    def insert_claim(self, claim: Claim, run_id: str | None = None) -> bool:
        cur = self._run(
            "INSERT OR IGNORE INTO claims(id,type,content,source_service,inputs,confidence,"
            "created_at,supersedes,origin,run_id,signer,signature) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (claim.id, claim.type, canonical(claim.content), claim.source_service,
             json.dumps(list(claim.inputs)), claim.confidence, claim.created_at,
             claim.supersedes, claim.origin, run_id or claim.run_id, claim.signer, claim.signature),
        )
        return cur.rowcount == 1

    def get_claim(self, claim_id: str) -> Claim | None:
        row = self._one("SELECT * FROM claims WHERE id=?", (claim_id,))
        return _row_to_claim(row) if row else None

    def is_superseded(self, claim_id: str) -> bool:
        """True if a claim by the same service/origin and of the same type supersedes it.

        A claim naming someone else's claim as `supersedes` does not hide it: otherwise a peer could suppress a
        claim by publishing a superseder before the target arrives (ownership cannot be checked until it does).
        """
        return self._one(
            "SELECT 1 FROM claims c2 JOIN claims t ON t.id = c2.supersedes WHERE c2.supersedes=?"
            " AND c2.source_service = t.source_service AND c2.origin = t.origin AND c2.type = t.type",
            (claim_id,)) is not None

    def max_seq(self) -> int:
        row = self._one("SELECT COALESCE(MAX(seq),0) AS m FROM claims")
        return int(row["m"])

    def query_claims(
        self, types: list[str] | None = None, *, current_only: bool = True,
        source_service: str | None = None, after_seq: int | None = None,
        limit: int | None = None, exclude_local_service: str | None = None,
    ) -> list[Claim]:
        sql = "SELECT * FROM claims WHERE 1=1"
        params: list[Any] = []
        if current_only:
            sql += (" AND NOT EXISTS (SELECT 1 FROM claims AS c2 INDEXED BY idx_claims_supersedes WHERE c2.supersedes = claims.id"
                    " AND c2.source_service = claims.source_service AND c2.origin = claims.origin"
                    " AND c2.type = claims.type)")
        if source_service is not None:
            sql += " AND source_service=?"
            params.append(source_service)
        if after_seq is not None:
            sql += " AND seq>?"
            params.append(after_seq)
        if exclude_local_service is not None:
            sql += " AND NOT (source_service=? AND origin=?)"
            params.extend([exclude_local_service, self.node_id])
        python_filter = False
        if types:
            clauses: list[str] = []
            type_params: list[str] = []
            for pat in types:
                if not any(c in pat for c in "*?["):
                    clauses.append("type=?")
                elif "[" not in pat:                       # only * and ?: SQLite GLOB is equivalent to fnmatch
                    clauses.append("type GLOB ?")
                else:                                      # character classes differ between GLOB and fnmatch
                    python_filter = True
                    break
                type_params.append(pat)
            if not python_filter:
                sql += " AND (" + " OR ".join(clauses) + ")"
                params.extend(type_params)
        sql += " ORDER BY seq"
        if limit and not python_filter:
            sql += " LIMIT ?"
            params.append(int(limit))
        claims = [_row_to_claim(r) for r in self._all(sql, tuple(params))]
        if python_filter:
            claims = [c for c in claims if matches(types, c.type)]
            return claims[:limit] if limit else claims
        return claims

    def sign_claim(self, claim: Claim) -> Claim:
        """Return the claim signed with this node's key (only claims originating here may be signed)."""
        if claim.origin != self.node_id:
            raise ClaimError("a node can only sign claims it originates")
        sig = ed25519.sign(self._seed, signing_message(claim.id, claim.origin))
        return replace(claim, signer=self.public_key, signature=sig.hex())

    def verify_claim(self, claim_id: str) -> str:
        c = self.get_claim(claim_id)
        if c is None:
            raise KeyError(claim_id)
        return verify_wire_signature(c.to_wire())

    def add_trusted_signer(self, pubkey: str, label: str, services: list[str]) -> None:
        if len(pubkey) != 64 or any(ch not in "0123456789abcdef" for ch in pubkey):
            raise ClaimError("pubkey must be 64 lowercase hex characters")
        self._run("INSERT INTO trusted_signers(pubkey,label,services,added_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(pubkey) DO UPDATE SET label=excluded.label, services=excluded.services",
                  (pubkey, label, json.dumps(services), self.clock()))

    def remove_trusted_signer(self, pubkey: str) -> bool:
        return self._run("DELETE FROM trusted_signers WHERE pubkey=?", (pubkey,)).rowcount == 1

    def trusted_signers(self) -> dict[str, dict[str, Any]]:
        return {r["pubkey"]: {"label": r["label"], "services": json.loads(r["services"])}
                for r in self._all("SELECT * FROM trusted_signers ORDER BY label")}

    def lineage(self, claim_id: str) -> dict[str, Any]:
        if self.get_claim(claim_id) is None:
            raise KeyError(claim_id)
        seen: set[str] = set()
        missing: set[str] = set()
        out: list[dict[str, Any]] = []
        queue: list[tuple[str, int, str]] = [(claim_id, 0, "root")]
        while queue:
            cid, depth, rel = queue.pop(0)
            if cid in seen:
                continue
            c = self.get_claim(cid)
            if c is None:
                missing.add(cid)
                continue
            seen.add(cid)
            out.append({"id": cid, "type": c.type, "source_service": c.source_service,
                        "confidence": c.confidence, "relation": rel, "depth": depth,
                        "origin": c.origin, "signature": verify_wire_signature(c.to_wire())})
            for i in c.inputs:
                queue.append((i, depth + 1, "input"))
            if c.supersedes:
                target = self.get_claim(c.supersedes)
                if target is None or (target.source_service == c.source_service and target.origin == c.origin
                                      and target.type == c.type):   # an invalid supersession edge is ignored
                    queue.append((c.supersedes, depth + 1, "supersedes"))
        return {"root": claim_id, "ancestors": out, "missing": sorted(missing - seen)}

    # -- services -------------------------------------------------------
    @staticmethod
    def _svc(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "manifest": json.loads(row["manifest"]), "base_dir": row["base_dir"],
            "enabled": bool(row["enabled"]), "consecutive_failures": row["consecutive_failures"],
            "disabled_reason": row["disabled_reason"], "next_due": row["next_due"],
            "cursors": json.loads(row["cursors"]), "registered_at": row["registered_at"],
        }

    def upsert_service(self, manifest: dict[str, Any], base_dir: str | None) -> None:
        now = self.clock()
        with self._lock:
            row = self._conn.execute("SELECT id FROM services WHERE id=?", (manifest["id"],)).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE services SET manifest=?, base_dir=?, enabled=1, consecutive_failures=0,"
                    " disabled_reason=NULL WHERE id=?",
                    (canonical(manifest), base_dir, manifest["id"]))
            else:
                self._conn.execute(
                    "INSERT INTO services(id,manifest,base_dir,next_due,registered_at) VALUES(?,?,?,?,?)",
                    (manifest["id"], canonical(manifest), base_dir, now, now))

    def get_service(self, service_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM services WHERE id=?", (service_id,))
        return self._svc(row) if row else None

    def list_services(self) -> list[dict[str, Any]]:
        return [self._svc(r) for r in self._all("SELECT * FROM services ORDER BY id")]

    def update_service(self, service_id: str, **fields: Any) -> None:
        allowed = {"enabled", "consecutive_failures", "disabled_reason", "next_due", "cursors"}
        bad = set(fields) - allowed
        if bad:
            raise StoreError(f"cannot update service fields: {sorted(bad)}")
        if "cursors" in fields:
            fields["cursors"] = canonical(fields["cursors"])
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        sets = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE services SET {sets} WHERE id=?", (*fields.values(), service_id))

    def acquire_lease(self, service_id: str, owner: str, seconds: float) -> bool:
        """Atomically take the per-service execution lease unless another owner holds an unexpired one."""
        now = self.clock()
        cur = self._run(
            "UPDATE services SET lease_owner=?, lease_until=? WHERE id=? "
            "AND (lease_until IS NULL OR lease_until<=?)",
            (owner, now + seconds, service_id, now))
        return cur.rowcount == 1

    def release_lease(self, service_id: str, owner: str) -> None:
        self._run("UPDATE services SET lease_owner=NULL, lease_until=NULL WHERE id=? AND lease_owner=?",
                  (service_id, owner))

    # -- state ----------------------------------------------------------
    def get_state(self, service_id: str) -> dict[str, Any]:
        row = self._one("SELECT state FROM service_state WHERE service_id=?", (service_id,))
        return json.loads(row["state"]) if row else {}

    def set_state(self, service_id: str, state: dict[str, Any]) -> None:
        self._run(
            "INSERT INTO service_state(service_id,state,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(service_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (service_id, canonical(state), self.clock()))

    # -- runs -----------------------------------------------------------
    def add_run(self, *, run_id: str, service_id: str, attempt: int, status: str,
                error: str | None, started_at: float, finished_at: float,
                claims_written: int, trigger: Any, log: list[str]) -> int:
        cur = self._run(
            "INSERT INTO runs(run_id,service_id,attempt,status,error,started_at,finished_at,"
            "claims_written,trigger,log) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, service_id, attempt, status, error, started_at, finished_at,
             claims_written, json.dumps(trigger, default=str), json.dumps(log)))
        return int(cur.lastrowid)

    def last_success_seq(self, service_id: str) -> int:
        row = self._one("SELECT COALESCE(MAX(seq),0) AS m FROM runs WHERE service_id=? AND status='success'",
                        (service_id,))
        return int(row["m"])

    def list_runs(self, service_id: str | None = None) -> list[dict[str, Any]]:
        if service_id:
            rows = self._all("SELECT * FROM runs WHERE service_id=? ORDER BY seq", (service_id,))
        else:
            rows = self._all("SELECT * FROM runs ORDER BY seq")
        out = []
        for r in rows:
            d = dict(r)
            d["log"] = json.loads(d["log"] or "[]")
            d["trigger"] = json.loads(d["trigger"] or "null")
            out.append(d)
        return out

    # -- messages -------------------------------------------------------
    def add_message(self, from_service: str, to_service: str, topic: str, payload: Any) -> int:
        cur = self._run(
            "INSERT INTO messages(from_service,to_service,topic,payload,created_at) VALUES(?,?,?,?,?)",
            (from_service, to_service, topic, canonical(payload), self.clock()))
        return int(cur.lastrowid)

    def pending_messages(self, to_service: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM messages WHERE to_service=? AND consumed_at IS NULL ORDER BY id", (to_service,))
        return [{"id": r["id"], "from": r["from_service"], "topic": r["topic"],
                 "payload": json.loads(r["payload"]), "created_at": r["created_at"]} for r in rows]

    def mark_consumed(self, ids: Iterable[int]) -> None:
        now = self.clock()
        for i in ids:
            self._run("UPDATE messages SET consumed_at=? WHERE id=? AND consumed_at IS NULL", (now, i))

    # -- retention ------------------------------------------------------
    def prune_runs(self, keep_last_per_service: int) -> int:
        """Delete old run records, keeping the newest N per service plus each service's latest success
        (dependency triggers read the latest successful run, so it is never removed)."""
        if keep_last_per_service < 1:
            raise StoreError("keep_last_per_service must be >= 1")
        cur = self._run(
            "DELETE FROM runs WHERE seq NOT IN ("
            " SELECT seq FROM (SELECT seq, ROW_NUMBER() OVER (PARTITION BY service_id ORDER BY seq DESC) AS rn"
            "                  FROM runs) WHERE rn <= ?)"
            " AND seq NOT IN (SELECT MAX(seq) FROM runs WHERE status='success' GROUP BY service_id)",
            (keep_last_per_service,))
        return cur.rowcount

    def prune_audit(self, older_than_seconds: float) -> int:
        return self._run("DELETE FROM audit WHERE ts < ?", (self.clock() - older_than_seconds,)).rowcount

    def prune_messages(self, older_than_seconds: float) -> int:
        """Delete consumed messages older than the cutoff. Unconsumed messages are never deleted."""
        return self._run("DELETE FROM messages WHERE consumed_at IS NOT NULL AND consumed_at < ?",
                         (self.clock() - older_than_seconds,)).rowcount

    def prune_superseded_claims(self, older_than_seconds: float) -> int:
        """Delete superseded claims created before the cutoff. Current claims are never deleted.

        This removes history: lineage() will report pruned ancestors as missing.
        """
        cur = self._run(
            "DELETE FROM claims WHERE created_at < ? AND EXISTS (SELECT 1 FROM claims AS c2 INDEXED BY idx_claims_supersedes WHERE c2.supersedes = claims.id"
            " AND c2.source_service = claims.source_service AND c2.origin = claims.origin AND c2.type = claims.type)",
            (self.clock() - older_than_seconds,))
        return cur.rowcount

    # -- audit ----------------------------------------------------------
    def audit(self, service_id: str | None, action: str, detail: str, allowed: bool) -> None:
        self._run("INSERT INTO audit(ts,service_id,action,detail,allowed) VALUES(?,?,?,?,?)",
                  (self.clock(), service_id, action, detail, int(allowed)))

    def list_audit(self, service_id: str | None = None) -> list[dict[str, Any]]:
        if service_id:
            rows = self._all("SELECT * FROM audit WHERE service_id=? ORDER BY id", (service_id,))
        else:
            rows = self._all("SELECT * FROM audit ORDER BY id")
        return [dict(r) for r in rows]

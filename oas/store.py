"""SQLite-backed store: claims with provenance, service rows, state, runs, messages, audit."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

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
  run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_type ON claims(type);
CREATE INDEX IF NOT EXISTS idx_claims_supersedes ON claims(supersedes);
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
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "content": self.content,
            "source_service": self.source_service, "inputs": list(self.inputs),
            "confidence": self.confidence, "created_at": self.created_at,
            "supersedes": self.supersedes, "origin": self.origin,
        }


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        id=row["id"], type=row["type"], content=json.loads(row["content"]),
        source_service=row["source_service"], inputs=tuple(json.loads(row["inputs"])),
        confidence=row["confidence"], created_at=row["created_at"],
        supersedes=row["supersedes"], origin=row["origin"], seq=row["seq"], run_id=row["run_id"],
    )


class Store:
    def __init__(self, path: str = ":memory:", *, clock: Callable[[], float] = time.time,
                 node_id: str | None = None) -> None:
        self.path = str(path)
        self.clock = clock
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._in_tx = False
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(services)")}
            for col, typ in (("lease_owner", "TEXT"), ("lease_until", "REAL")):
                if col not in cols:  # upgrade databases created before leases existed
                    self._conn.execute(f"ALTER TABLE services ADD COLUMN {col} {typ}")
            row = self._conn.execute("SELECT value FROM meta WHERE key='node_id'").fetchone()
            if row:
                self.node_id = row["value"]
            else:
                self.node_id = node_id or uuid.uuid4().hex[:12]
                self._conn.execute("INSERT INTO meta(key, value) VALUES('node_id', ?)", (self.node_id,))

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
            "created_at,supersedes,origin,run_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (claim.id, claim.type, canonical(claim.content), claim.source_service,
             json.dumps(list(claim.inputs)), claim.confidence, claim.created_at,
             claim.supersedes, claim.origin, run_id or claim.run_id),
        )
        return cur.rowcount == 1

    def get_claim(self, claim_id: str) -> Claim | None:
        row = self._one("SELECT * FROM claims WHERE id=?", (claim_id,))
        return _row_to_claim(row) if row else None

    def is_superseded(self, claim_id: str) -> bool:
        return self._one("SELECT 1 FROM claims WHERE supersedes=?", (claim_id,)) is not None

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
            sql += " AND NOT EXISTS (SELECT 1 FROM claims c2 WHERE c2.supersedes = claims.id)"
        if source_service is not None:
            sql += " AND source_service=?"
            params.append(source_service)
        if after_seq is not None:
            sql += " AND seq>?"
            params.append(after_seq)
        if exclude_local_service is not None:
            sql += " AND NOT (source_service=? AND origin=?)"
            params.extend([exclude_local_service, self.node_id])
        sql += " ORDER BY seq"
        claims = [_row_to_claim(r) for r in self._all(sql, tuple(params))]
        if types:
            claims = [c for c in claims if matches(types, c.type)]
        return claims[:limit] if limit else claims

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
                        "origin": c.origin})
            for i in c.inputs:
                queue.append((i, depth + 1, "input"))
            if c.supersedes:
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

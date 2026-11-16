"""Decentralised exchange of claims between independent nodes.

Claims are content-addressed, so a bundle can be verified for integrity by anyone
without a central authority. NOTE: integrity is not authenticity - there are no
signatures yet, so `source_service` and `origin` are self-asserted.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .errors import ClaimError
from . import schema as _schema
from .manifest import matches
from .store import Claim, Store, compute_claim_id, verify_wire_signature

BUNDLE_FORMAT = "oas-bundle/1"
_REQUIRED = ("id", "type", "content", "source_service", "inputs", "confidence",
             "created_at", "supersedes", "origin")


def export_bundle(store: Store, types: list[str] | None = None, since_seq: int = 0) -> dict[str, Any]:
    claims = store.query_claims(types, current_only=False, after_seq=since_seq)
    return {
        "format": BUNDLE_FORMAT,
        "node_id": store.node_id,
        "exported_at": store.clock(),
        "signer": store.public_key,
        "claims": [c.to_wire() for c in claims],
    }


def write_bundle(bundle: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")


def read_bundle(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate_shape(w: Any) -> None:
    if not isinstance(w, dict) or any(k not in w for k in _REQUIRED):
        raise ClaimError("claim is missing required fields")
    if not isinstance(w["inputs"], list) or not all(isinstance(i, str) for i in w["inputs"]):
        raise ClaimError("inputs must be a list of ids")
    if isinstance(w["confidence"], bool) or not isinstance(w["confidence"], (int, float)) \
            or not 0.0 <= w["confidence"] <= 1.0:
        raise ClaimError("confidence out of range")
    if not isinstance(w["created_at"], (int, float)):
        raise ClaimError("created_at must be a number")
    for k in ("id", "type", "source_service", "origin"):
        if not isinstance(w[k], str) or not w[k]:
            raise ClaimError(f"{k} must be a non-empty string")
    if w["supersedes"] is not None and not isinstance(w["supersedes"], str):
        raise ClaimError("supersedes must be a string or null")


def import_bundle(store: Store, bundle: dict[str, Any], *,
                  schema_lookup: Callable[[str], list[dict[str, Any]]] | None = None,
                  trusted: dict[str, dict[str, Any]] | None = None,
                  require_signatures: bool = False) -> dict[str, Any]:
    """Verify and merge a bundle. Idempotent. Returns counts plus per-claim rejection reasons.

    `schema_lookup(claim_type)` returns the schemas locally registered services declare for that type;
    claims whose content violates any of them are rejected.

    Signatures: a present signature must verify (covering the id and origin) or the claim is rejected. With
    `require_signatures`, unsigned claims are rejected. With `trusted` (pubkey -> {"services": [patterns]}), the
    signer must be listed and the claim's source_service must match one of its patterns.
    """
    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
        raise ClaimError(f"unsupported bundle format (expected {BUNDLE_FORMAT})")
    claims = bundle.get("claims")
    if not isinstance(claims, list):
        raise ClaimError("bundle has no claims list")
    imported = duplicates = 0
    rejected: list[dict[str, str]] = []
    with store.transaction():
        imported, duplicates = _import_claims(store, claims, rejected, schema_lookup, trusted, require_signatures)
    store.audit(None, "import_bundle",
                f"from {bundle.get('node_id')}: {imported} imported, {duplicates} duplicate, {len(rejected)} rejected",
                True)
    return {"imported": imported, "duplicates": duplicates, "rejected": rejected}


def _import_claims(store: Store, claims: list[Any], rejected: list[dict[str, str]],
                   schema_lookup, trusted, require_signatures) -> tuple[int, int]:
    imported = duplicates = 0
    for w in claims:
        cid = w.get("id", "?") if isinstance(w, dict) else "?"
        try:
            _validate_shape(w)
            expected = compute_claim_id(w["type"], w["content"], w["source_service"], w["inputs"],
                                        w["confidence"], w["created_at"], w["supersedes"])
            if expected != w["id"]:
                raise ClaimError("id does not match content (tampered or corrupted)")
            if store.get_claim(w["id"]) is not None:
                duplicates += 1
                continue
            status = verify_wire_signature(w)
            if status == "invalid":
                raise ClaimError("signature does not verify (forged or altered claim/origin)")
            if status == "unsigned" and (require_signatures or trusted is not None):
                raise ClaimError("claim is unsigned but signatures are required")
            if trusted is not None:
                entry = trusted.get(w["signer"])
                if entry is None:
                    raise ClaimError(f"signer {w['signer'][:12]} is not trusted")
                if not matches(entry["services"], w["source_service"]):
                    raise ClaimError(f"signer {w['signer'][:12]} is not trusted for service '{w['source_service']}'")
            if schema_lookup is not None:
                for sch in schema_lookup(w["type"]):
                    problems = _schema.validate(w["content"], sch)
                    if problems:
                        raise ClaimError(f"content violates local schema for '{w['type']}': {problems[0]}")
            if w["origin"] == store.node_id:
                raise ClaimError("claims claiming this node as origin must already exist locally")
            if w["supersedes"]:
                target = store.get_claim(w["supersedes"])
                if target is not None and (target.source_service != w["source_service"]
                                           or target.origin != w["origin"]):
                    raise ClaimError("cannot supersede a claim owned by a different service/origin")
                if target is not None and target.type != w["type"]:
                    raise ClaimError("a claim may only supersede a claim of the same type")
            claim = Claim(id=w["id"], type=w["type"], content=w["content"],
                          source_service=w["source_service"], inputs=tuple(w["inputs"]),
                          confidence=float(w["confidence"]), created_at=float(w["created_at"]),
                          supersedes=w["supersedes"], origin=w["origin"],
                          signer=w.get("signer"), signature=w.get("signature"))
            if store.insert_claim(claim):
                imported += 1
        except ClaimError as exc:
            rejected.append({"id": str(cid), "reason": str(exc)})
    return imported, duplicates

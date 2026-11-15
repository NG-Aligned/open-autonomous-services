"""Decentralised exchange of claims between independent nodes.

Claims are content-addressed, so a bundle can be verified for integrity by anyone
without a central authority. NOTE: integrity is not authenticity - there are no
signatures yet, so `source_service` and `origin` are self-asserted.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ClaimError
from .store import Claim, Store, compute_claim_id

BUNDLE_FORMAT = "oas-bundle/1"
_REQUIRED = ("id", "type", "content", "source_service", "inputs", "confidence",
             "created_at", "supersedes", "origin")


def export_bundle(store: Store, types: list[str] | None = None, since_seq: int = 0) -> dict[str, Any]:
    claims = store.query_claims(types, current_only=False, after_seq=since_seq)
    return {
        "format": BUNDLE_FORMAT,
        "node_id": store.node_id,
        "exported_at": store.clock(),
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


def import_bundle(store: Store, bundle: dict[str, Any]) -> dict[str, Any]:
    """Verify and merge a bundle. Idempotent. Returns counts plus per-claim rejection reasons."""
    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
        raise ClaimError(f"unsupported bundle format (expected {BUNDLE_FORMAT})")
    claims = bundle.get("claims")
    if not isinstance(claims, list):
        raise ClaimError("bundle has no claims list")
    imported = duplicates = 0
    rejected: list[dict[str, str]] = []
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
            if w["origin"] == store.node_id:
                raise ClaimError("claims claiming this node as origin must already exist locally")
            if w["supersedes"]:
                target = store.get_claim(w["supersedes"])
                if target is not None and (target.source_service != w["source_service"]
                                           or target.origin != w["origin"]):
                    raise ClaimError("cannot supersede a claim owned by a different service/origin")
            claim = Claim(id=w["id"], type=w["type"], content=w["content"],
                          source_service=w["source_service"], inputs=tuple(w["inputs"]),
                          confidence=float(w["confidence"]), created_at=float(w["created_at"]),
                          supersedes=w["supersedes"], origin=w["origin"])
            if store.insert_claim(claim):
                imported += 1
        except ClaimError as exc:
            rejected.append({"id": str(cid), "reason": str(exc)})
    store.audit(None, "import_bundle",
                f"from {bundle.get('node_id')}: {imported} imported, {duplicates} duplicate, {len(rejected)} rejected",
                True)
    return {"imported": imported, "duplicates": duplicates, "rejected": rejected}

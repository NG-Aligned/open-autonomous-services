"""Decentralised exchange of claims between independent nodes.

Claims are content-addressed, so a bundle can be verified for integrity by anyone
without a central authority. NOTE: integrity is not authenticity - there are no
signatures yet, so `source_service` and `origin` are self-asserted.
"""
from __future__ import annotations

import json
import math
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
    for k in ("confidence", "created_at"):
        v = w[k]
        # Ids hash the canonical JSON, where 1 and 1.0 differ; requiring floats keeps relayed claims verifiable.
        if isinstance(v, bool) or not isinstance(v, float) or not math.isfinite(v):
            raise ClaimError(f"{k} must be a finite JSON float (write 1.0, not 1)")
    if not 0.0 <= w["confidence"] <= 1.0:
        raise ClaimError("confidence out of range")
    for k in ("id", "type", "source_service", "origin"):
        if not isinstance(w[k], str) or not w[k]:
            raise ClaimError(f"{k} must be a non-empty string")
    for k in ("signer", "signature"):
        if w.get(k) is not None and not isinstance(w[k], str):
            raise ClaimError(f"{k} must be a string or null")
    if w["supersedes"] is not None and not isinstance(w["supersedes"], str):
        raise ClaimError("supersedes must be a string or null")
    for text in (w["id"], w["type"], w["source_service"], w["origin"], *w["inputs"], w["supersedes"] or "",
                 w.get("signer") or "", w.get("signature") or ""):
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise ClaimError("text fields must be valid Unicode (no lone surrogates)") from None
    if "\x00" in w["type"] or "\x00" in w["source_service"] or "\x00" in w["origin"]:
        raise ClaimError("type, source_service and origin must not contain NUL")


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
        imported, duplicates, upgraded = _import_claims(store, claims, rejected, schema_lookup, trusted,
                                                        require_signatures)
    store.audit(None, "import_bundle",
                f"from {bundle.get('node_id')}: {imported} imported, {duplicates} duplicate "
                f"({upgraded} upgraded to a signed copy), {len(rejected)} rejected", True)
    return {"imported": imported, "duplicates": duplicates, "upgraded": upgraded, "rejected": rejected}


def _check_signature_policy(store: Store, w: dict[str, Any], trusted: dict[str, dict[str, Any]] | None,
                            require_signatures: bool) -> str:
    """Signature, origin-binding and trusted-signer checks. Returns 'valid' or 'unsigned'; raises ClaimError."""
    status = verify_wire_signature(w)
    if status == "invalid":
        raise ClaimError("signature does not verify (forged or altered claim/origin)")
    if status == "unsigned":
        if require_signatures or trusted is not None:
            raise ClaimError("claim is unsigned but signatures are required")
        return status
    bound = store.origin_key(w["origin"])
    if bound is not None and bound != w["signer"]:
        raise ClaimError(f"origin '{w['origin']}' is bound to a different signing key")
    if trusted is not None:
        entry = trusted.get(w["signer"])
        if entry is None:
            raise ClaimError(f"signer {w['signer'][:12]} is not trusted")
        if not matches(entry["services"], w["source_service"]):
            raise ClaimError(f"signer {w['signer'][:12]} is not trusted for service '{w['source_service']}'")
    return status


def _import_claims(store: Store, claims: list[Any], rejected: list[dict[str, str]],
                   schema_lookup, trusted, require_signatures) -> tuple[int, int, int]:
    imported = duplicates = upgraded = 0
    for w in claims:
        cid = w.get("id", "?") if isinstance(w, dict) else "?"
        try:
            _validate_shape(w)
            expected = compute_claim_id(w["type"], w["content"], w["source_service"], w["inputs"],
                                        w["confidence"], w["created_at"], w["supersedes"])
            if expected != w["id"]:
                raise ClaimError("id does not match content (tampered or corrupted)")
            existing = store.get_claim(w["id"])
            if existing is not None:
                duplicates += 1
                # origin is not part of the id, so an unsigned copy with a forged origin could otherwise pin the
                # claim forever: a verified signed copy replaces the attribution of an unsigned one.
                if existing.signature is None and existing.origin != store.node_id and w.get("signature"):
                    try:
                        ok = _check_signature_policy(store, w, trusted, require_signatures) == "valid"
                    except ClaimError:
                        ok = False
                    if ok and store.upgrade_claim_attribution(w["id"], w["origin"], w["signer"], w["signature"]):
                        store.bind_origin(w["origin"], w["signer"])
                        upgraded += 1
                continue
            status = _check_signature_policy(store, w, trusted, require_signatures)
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
                if status == "valid":
                    store.bind_origin(w["origin"], w["signer"])
        except ClaimError as exc:
            rejected.append({"id": str(cid), "reason": str(exc)})
        except (OverflowError, UnicodeError, ValueError, TypeError, KeyError) as exc:   # hostile values: reject one claim
            rejected.append({"id": str(cid), "reason": f"malformed claim ({type(exc).__name__}: {exc})"})
    return imported, duplicates, upgraded

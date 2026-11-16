"""Service manifests: the declarative contract every service publishes."""
from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import cron as _cron
from .errors import ManifestError
from .schema import check_schema

ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
TRIGGER_KINDS = ("interval", "cron", "event", "dependency", "message", "manual")
CONTINUITY_MODES = ("persist", "ephemeral")
CONFIG_TYPES: dict[str, Any] = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}
ALLOWED_KEYS = {
    "id", "version", "purpose", "entrypoint", "config", "config_schema",
    "triggers", "reads", "writes", "can_message", "retry",
    "timeout_seconds", "continuity", "circuit_breaker", "claim_schemas", "isolation",
    "max_memory_mb",
}


def has_wildcard(value: str) -> bool:
    return any(c in value for c in "*?[")


def matches(patterns: Iterable[str], value: str) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


@dataclass
class Manifest:
    id: str
    version: str
    purpose: str
    entrypoint: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    config_schema: dict[str, Any] = field(default_factory=dict)
    triggers: list[dict[str, Any]] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    can_message: list[str] = field(default_factory=list)
    retry: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None
    continuity: str = "persist"
    circuit_breaker: int = 3
    claim_schemas: dict[str, Any] = field(default_factory=dict)
    isolation: str = "thread"
    max_memory_mb: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        if not isinstance(data, dict):
            raise ManifestError("manifest must be an object")
        unknown = set(data) - ALLOWED_KEYS
        if unknown:
            raise ManifestError(f"unknown manifest keys: {sorted(unknown)}")
        for key in ("id", "version", "purpose"):
            v = data.get(key)
            if not isinstance(v, str) or not v.strip():
                raise ManifestError(f"'{key}' is required and must be a non-empty string")
        m = cls(**data)
        m._validate()
        return m

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ------------------------------------------------------------------
    def _validate(self) -> None:
        if not ID_RE.match(self.id):
            raise ManifestError(f"invalid service id '{self.id}' (lowercase letters, digits, _ . -)")
        if self.entrypoint is not None and (
            not isinstance(self.entrypoint, str) or ":" not in self.entrypoint
        ):
            raise ManifestError("entrypoint must look like 'module:function'")
        for name in ("config", "config_schema", "retry", "claim_schemas"):
            if not isinstance(getattr(self, name), dict):
                raise ManifestError(f"'{name}' must be an object")
        for name in ("reads", "writes", "can_message", "triggers"):
            if not isinstance(getattr(self, name), list):
                raise ManifestError(f"'{name}' must be a list")
        for name in ("reads", "writes", "can_message"):
            for item in getattr(self, name):
                if not isinstance(item, str) or not item:
                    raise ManifestError(f"'{name}' entries must be non-empty strings")
        for item in self.writes:
            pass  # wildcard writes are allowed as a declared permission
        for t in self.triggers:
            self._validate_trigger(t)
        retry = {"max_attempts": 1, "backoff_seconds": 0.0}
        extra = set(self.retry) - set(retry)
        if extra:
            raise ManifestError(f"unknown retry keys: {sorted(extra)}")
        retry.update(self.retry)
        if not isinstance(retry["max_attempts"], int) or isinstance(retry["max_attempts"], bool) or retry["max_attempts"] < 1:
            raise ManifestError("retry.max_attempts must be an integer >= 1")
        if not _is_number(retry["backoff_seconds"]) or retry["backoff_seconds"] < 0:
            raise ManifestError("retry.backoff_seconds must be a number >= 0")
        self.retry = retry
        if self.timeout_seconds is not None and (
            not _is_number(self.timeout_seconds) or self.timeout_seconds <= 0
        ):
            raise ManifestError("timeout_seconds must be a positive number or null")
        if self.continuity not in CONTINUITY_MODES:
            raise ManifestError(f"continuity must be one of {CONTINUITY_MODES}")
        if not isinstance(self.circuit_breaker, int) or isinstance(self.circuit_breaker, bool) or self.circuit_breaker < 1:
            raise ManifestError("circuit_breaker must be an integer >= 1")
        if self.isolation not in ("thread", "process"):
            raise ManifestError("isolation must be 'thread' or 'process'")
        if self.isolation == "process" and not self.entrypoint:
            raise ManifestError("isolation 'process' requires an entrypoint ('module:function')")
        if self.max_memory_mb is not None and (not isinstance(self.max_memory_mb, int)
                                               or isinstance(self.max_memory_mb, bool) or self.max_memory_mb < 16):
            raise ManifestError("max_memory_mb must be an integer >= 16 or null")
        if self.max_memory_mb is not None and self.isolation != "process":
            raise ManifestError("max_memory_mb only applies with isolation 'process'")
        for ctype, schema in self.claim_schemas.items():
            if not isinstance(ctype, str) or not ctype:
                raise ManifestError("claim_schemas keys must be non-empty claim type strings")
            if not (matches(self.writes, ctype) or matches(self.reads, ctype)
                    or ctype in self.writes or ctype in self.reads):
                raise ManifestError(f"claim_schemas['{ctype}'] is for a type this service neither reads nor writes")
            problems = check_schema(schema)
            if problems:
                raise ManifestError(f"claim_schemas['{ctype}'] invalid: {problems[0]}")
        self.resolve_config()

    def schemas_for(self, claim_type: str) -> list[dict[str, Any]]:
        """Schemas declared by this manifest that apply to a literal claim type (exact key or wildcard key)."""
        return [sch for key, sch in self.claim_schemas.items()
                if key == claim_type or fnmatch.fnmatchcase(claim_type, key)]

    @staticmethod
    def _validate_trigger(t: Any) -> None:
        if not isinstance(t, dict) or t.get("kind") not in TRIGGER_KINDS:
            raise ManifestError(f"trigger must be an object with kind in {TRIGGER_KINDS}")
        kind = t["kind"]
        if kind == "interval":
            if not _is_number(t.get("seconds")) or t["seconds"] <= 0:
                raise ManifestError("interval trigger needs positive 'seconds'")
        elif kind == "cron":
            try:
                _cron.next_fire(t.get("expr"), 0.0)
            except ValueError as exc:
                raise ManifestError(f"cron trigger invalid: {exc}") from None
        elif kind == "event":
            if not isinstance(t.get("claim_type"), str) or not t["claim_type"]:
                raise ManifestError("event trigger needs 'claim_type'")
        elif kind == "dependency":
            if not isinstance(t.get("service"), str) or not t["service"]:
                raise ManifestError("dependency trigger needs 'service'")

    def resolve_config(self) -> dict[str, Any]:
        """Return config with schema defaults applied; raise on violations."""
        cfg = dict(self.config)
        schema = self.config_schema
        for key, spec in schema.items():
            if not isinstance(spec, dict) or spec.get("type") not in CONFIG_TYPES:
                raise ManifestError(f"config_schema['{key}'] needs a 'type' in {sorted(CONFIG_TYPES)}")
            if key not in cfg:
                if "default" in spec:
                    cfg[key] = spec["default"]
                elif spec.get("required"):
                    raise ManifestError(f"missing required config '{key}'")
                else:
                    continue
            value = cfg[key]
            expected = CONFIG_TYPES[spec["type"]]
            if spec["type"] in ("int", "float") and isinstance(value, bool):
                raise ManifestError(f"config '{key}' must be {spec['type']}, got bool")
            if not isinstance(value, expected):
                raise ManifestError(f"config '{key}' must be {spec['type']}, got {type(value).__name__}")
        if schema:
            unknown = set(cfg) - set(schema)
            if unknown:
                raise ManifestError(f"config keys not in config_schema: {sorted(unknown)}")
        return cfg


def load_manifest(path: str | Path) -> Manifest:
    p = Path(path)
    if p.suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
    elif p.suffix == ".toml":
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    else:
        raise ManifestError(f"unsupported manifest format: {p.suffix}")
    return Manifest.from_dict(data)

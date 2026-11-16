"""A small JSON-Schema subset validator (stdlib only) for claim content.

Supported keywords: type (string or list), enum, const, properties, required, additionalProperties (bool),
items, minItems, maxItems, minLength, maxLength, minimum, maximum. Unknown keywords are rejected when a schema
is checked, so a typo can never silently disable validation.
"""
from __future__ import annotations

from typing import Any

TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
KEYWORDS = {"type", "enum", "const", "properties", "required", "additionalProperties", "items",
            "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "description"}


def _is_type(value: Any, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def check_schema(schema: Any, path: str = "$") -> list[str]:
    """Return a list of problems with the schema itself (empty = well formed)."""
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    problems: list[str] = []
    for key in schema:
        if key not in KEYWORDS:
            problems.append(f"{path}: unsupported keyword '{key}'")
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not ts or any(x not in TYPES for x in ts):
            problems.append(f"{path}: type must be one or more of {sorted(TYPES)}")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        problems.append(f"{path}: enum must be a non-empty list")
    if "required" in schema and (not isinstance(schema["required"], list)
                                 or not all(isinstance(r, str) for r in schema["required"])):
        problems.append(f"{path}: required must be a list of strings")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        problems.append(f"{path}: additionalProperties must be a boolean")
    for k in ("minItems", "maxItems", "minLength", "maxLength"):
        if k in schema and (not isinstance(schema[k], int) or isinstance(schema[k], bool) or schema[k] < 0):
            problems.append(f"{path}: {k} must be a non-negative integer")
    for k in ("minimum", "maximum"):
        if k in schema and not _is_type(schema[k], "number"):
            problems.append(f"{path}: {k} must be a number")
    props = schema.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            problems.append(f"{path}: properties must be an object")
        else:
            for name, sub in props.items():
                problems += check_schema(sub, f"{path}.{name}")
    if "items" in schema:
        problems += check_schema(schema["items"], f"{path}[]")
    return problems


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of violations (empty = valid). The schema is assumed well formed."""
    errors: list[str] = []
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is_type(value, x) for x in ts):
            return [f"{path}: expected {'|'.join(ts)}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required property '{req}'")
        props = schema.get("properties", {})
        for name, sub in props.items():
            if name in value:
                errors += validate(value[name], sub, f"{path}.{name}")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    errors.append(f"{path}: unexpected property '{name}'")
    return errors

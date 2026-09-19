"""Strict bounded JSON transformation for private file-operation composition.

Returned bytes are proposed publication content, not evidence of a filesystem
change. ``None`` means skip-existing observed an existing destination. The
caller owns target observation, regular-file checks, and atomic publication.
"""

from __future__ import annotations

import json
import math
from typing import Literal, Never, cast

from agentworks.errors import StateError, ValidationError

type _JsonStrategy = Literal["replace", "merge-overwrite", "merge-preserve", "skip-existing"]
type _JsonValue = str | int | float | bool | None | list[_JsonValue] | dict[str, _JsonValue]
type _JsonObject = dict[str, _JsonValue]


def transform_json(
    source: bytes,
    existing: bytes | None,
    *,
    strategy: _JsonStrategy,
    create: bool,
    max_bytes: int,
    max_depth: int,
) -> bytes | None:
    """Validate and transform one JSON object without filesystem effects.

    ``existing=None`` denotes absence; empty bytes denote an existing file.
    Source validation always precedes destination-dependent behavior. Container
    depth starts at one for the root object; nested arrays and objects each add
    one, while scalar leaves do not.
    """
    _validate_limits(max_bytes=max_bytes, max_depth=max_depth)
    document = _parse_object(source, max_bytes=max_bytes, max_depth=max_depth)

    if existing is None:
        if not create:
            raise StateError("JSON destination is absent and creation is disabled")
        result = document
    elif strategy == "skip-existing":
        return None
    elif strategy == "replace":
        result = document
    else:
        current = _parse_object(existing, max_bytes=max_bytes, max_depth=max_depth)
        result = _merge(current, document) if strategy == "merge-overwrite" else _merge(document, current)

    return _serialize(result, max_bytes=max_bytes)


def _validate_limits(*, max_bytes: int, max_depth: int) -> None:
    """Require positive finite rejection thresholds before parsing content."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValidationError("JSON byte bound must be a positive integer")
    if type(max_depth) is not int or max_depth <= 0:
        raise ValidationError("JSON depth bound must be a positive integer")


def _parse_object(content: bytes, *, max_bytes: int, max_depth: int) -> _JsonObject:
    if len(content) > max_bytes:
        raise ValidationError("JSON input exceeds its byte bound")
    _validate_encoded_depth(content, max_depth=max_depth)

    try:
        text = content.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        pass
    else:
        if isinstance(value, dict):
            return cast("_JsonObject", value)

    # Raise outside the handler so decoder exceptions cannot retain content.
    raise ValidationError("JSON object is invalid or exceeds parser capacity")


def _validate_encoded_depth(content: bytes, *, max_depth: int) -> None:
    """Refuse excessive container nesting before the recursive stdlib parser."""
    depth = 0
    in_string = False
    escaped = False
    for byte in content:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # Backslash
                escaped = True
            elif byte == 0x22:  # Double quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # Opening bracket or brace
            depth += 1
            if depth > max_depth:
                raise ValidationError("JSON value exceeds its depth bound")
        elif byte in (0x5D, 0x7D) and depth:
            depth -= 1


def _unique_object(pairs: list[tuple[str, _JsonValue]]) -> _JsonObject:
    result: _JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _reject_constant(_: str) -> Never:
    raise ValueError


def _merge(losing: _JsonObject, winning: _JsonObject) -> _JsonObject:
    """Recursively combine objects while treating arrays and scalars atomically."""
    result = dict(losing)
    pending: list[tuple[_JsonObject, _JsonObject]] = [(result, winning)]
    while pending:
        target, additions = pending.pop()
        for key, value in additions.items():
            previous = target.get(key)
            if isinstance(previous, dict) and isinstance(value, dict):
                child = dict(previous)
                target[key] = child
                pending.append((child, value))
            else:
                target[key] = value
    return result


def _serialize(document: _JsonObject, *, max_bytes: int) -> bytes:
    encoder = json.JSONEncoder(indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
    content = bytearray()
    complete = True
    try:
        for chunk in encoder.iterencode(document):
            encoded = chunk.encode("utf-8")
            if len(encoded) > max_bytes - len(content):
                complete = False
                break
            content.extend(encoded)
    except (RecursionError, ValueError):
        pass
    else:
        if complete and len(content) < max_bytes:
            content.append(0x0A)
            return bytes(content)
    raise ValidationError("JSON result could not be encoded within the available limits")

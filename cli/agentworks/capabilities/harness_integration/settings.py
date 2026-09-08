"""Native settings snapshots and semantic merge policies.

The caller owns destination selection, file-kind checks, plugin conflict
planning, and atomic publication. These values are transient operation data;
settings content must not be copied into applied-state records or logs.
"""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Literal, cast

import tomli_w
from pydantic import field_validator

from agentworks.errors import ConfigError
from agentworks.schema import AgwModel
from agentworks.sources import SourceRefError, parse_source_ref, snapshot_workstation_file

type SettingsFormat = Literal["json", "toml"]
type SettingsStrategy = Literal["replace", "merge-overwrite", "merge-preserve", "skip-existing"]
type SettingsValue = (
    str | int | float | bool | None | datetime | date | time | list[SettingsValue] | dict[str, SettingsValue]
)
type SettingsObject = dict[str, SettingsValue]


class SettingsMapping(AgwModel):
    """Map one workstation document to an integration's native settings role."""

    source: str
    """Ordinary workstation file path or file:: reference, read during setup."""

    strategy: SettingsStrategy
    """Explicit collision policy for the live native settings document."""

    @field_validator("source")
    @classmethod
    def _local_source(cls, value: str) -> str:
        """Validate operator-authored source syntax without acquiring its content."""
        try:
            reference = parse_source_ref(value)
        except SourceRefError:
            raise ValueError("invalid local settings source reference") from None
        if reference.kind != "file":
            raise ValueError("settings sources must be workstation files")
        return value


@dataclass(frozen=True)
class SettingsResult:
    """Effective native bytes, including untouched bytes when mapping is skipped.

    ``changed`` compares bytes with the supplied destination snapshot, so a
    semantic reserialization may count as a change. ``skipped`` means no mapping
    ownership can be claimed. Hashes describe bytes, not semantic equivalence.
    """

    content: bytes = field(repr=False)
    changed: bool
    skipped: bool

    @property
    def sha256(self) -> str:
        """Hash of the effective bytes, including an unparsed skipped file."""
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class PreparedSettings:
    """A source validated before native writes, reusable without rereading it."""

    format: SettingsFormat
    strategy: SettingsStrategy
    source_sha256: str
    _document: SettingsObject = field(repr=False)

    @classmethod
    def from_bytes(cls, source: bytes, *, format: SettingsFormat, strategy: SettingsStrategy) -> PreparedSettings:
        """Parse captured source bytes, including for the skip-existing policy."""
        return cls(format, strategy, hashlib.sha256(source).hexdigest(), parse_settings(source, format=format))

    def apply(self, destination: bytes | None) -> SettingsResult:
        """Compute output; None denotes absence, while empty bytes denote a file.

        The caller must reject directories and unsuitable links before calling.
        Existing bytes are parsed only when the policy merges with them.
        """
        if self.strategy == "skip-existing" and destination is not None:
            return SettingsResult(destination, changed=False, skipped=True)
        document = self._document
        if destination is not None and self.strategy in ("merge-overwrite", "merge-preserve"):
            existing = parse_settings(destination, format=self.format)
            document = _merge(existing, document) if self.strategy == "merge-overwrite" else _merge(document, existing)
        content = serialize_settings(document, format=self.format)
        return SettingsResult(content, changed=content != destination, skipped=False)

    def contributions(self, destination: bytes | None) -> SettingsObject:
        """Source values surviving this policy, for native declaration conflicts.

        Retained destination values are not a new request from the source.
        Arrays and type collisions follow the same atomic policy as apply().
        """
        if destination is None or self.strategy in ("replace", "merge-overwrite"):
            return dict(self._document)
        if self.strategy == "skip-existing":
            return {}
        return _absent_source(self._document, parse_settings(destination, format=self.format))


def _absent_source(source: SettingsObject, existing: SettingsObject) -> SettingsObject:
    result: SettingsObject = {}
    for key, value in source.items():
        if key not in existing:
            result[key] = value
        elif isinstance(value, dict) and isinstance(existing[key], dict):
            children = _absent_source(value, cast("SettingsObject", existing[key]))
            if children:
                result[key] = children
    return result


def prepare_settings(mapping: SettingsMapping, *, format: SettingsFormat) -> PreparedSettings:
    """Snapshot and validate a workstation source before any native mutation."""
    return PreparedSettings.from_bytes(
        snapshot_workstation_file(mapping.source), format=format, strategy=mapping.strategy
    )


def parse_settings(content: bytes, *, format: SettingsFormat) -> SettingsObject:
    """Parse external native document bytes without reporting their values.

    JSON requires a root object, unique keys at every depth, and finite
    numbers. TOML's parser enforces unique keys and its native table semantics.
    """
    try:
        text = content.decode("utf-8")
        if format == "json":
            document = json.loads(
                text, object_pairs_hook=_unique_object, parse_float=_finite_float, parse_constant=_reject_constant
            )
        else:
            document = tomllib.loads(text)
        if not isinstance(document, dict):
            raise ValueError("settings require a root object")
    except (ValueError, RecursionError):
        raise ConfigError(f"invalid {format.upper()} settings document") from None
    return cast("SettingsObject", document)


def _unique_object(pairs: list[tuple[str, SettingsValue]]) -> SettingsObject:
    result: SettingsObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate settings key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _merge(losing: SettingsObject, winning: SettingsObject) -> SettingsObject:
    """Recursively combine tables; winning leaves replace arrays and scalars."""
    result = dict(losing)
    for key, value in winning.items():
        previous = result.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            result[key] = _merge(previous, value)
        else:
            result[key] = value
    return result


def serialize_settings(document: SettingsObject, *, format: SettingsFormat) -> bytes:
    """Serialize a parsed native document for transient publication."""
    if format == "json":
        return (json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    return tomli_w.dumps(document).encode("utf-8")

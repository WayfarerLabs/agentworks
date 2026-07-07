"""Envelope validation: ``apiVersion`` / ``kind`` / ``metadata`` / ``spec``.

The envelope is new surface (no TOML ancestor), so it is strict from day
one: unknown top-level keys, unknown metadata keys, wrong shapes, and
non-declarable kinds are all errors. Kind-specific field validation is
NOT here; that's ``decode.py``'s job (which reuses the TOML loaders).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError
from agentworks.resources import KIND_REGISTRY

if TYPE_CHECKING:
    from agentworks.source_location import SourceLocation

API_VERSION = "agentworks/v1"

_ENVELOPE_KEYS = {"apiVersion", "kind", "metadata", "spec"}
_METADATA_KEYS = {"name", "description"}

# Kinds whose Config-layer shape is a singleton; the envelope accepts
# only the reserved name until their plurification SDDs land.
_SINGLETON_KINDS = {"admin-template", "named-console-template"}


@dataclass(frozen=True)
class Document:
    """One validated envelope, ready for spec decode."""

    kind: str
    name: str
    description: str | None
    spec: dict[str, object]
    location: SourceLocation

    @property
    def where(self) -> str:
        return f"{self.location.file}:{self.location.line}"


def _err(location: SourceLocation, message: str, *, hint: str | None = None) -> ConfigError:
    return ConfigError(f"{location.file}:{location.line}: {message}", hint=hint)


def validate_envelope(raw: object, location: SourceLocation) -> Document:
    """Validate one YAML document's envelope; raise ``ConfigError`` on any
    violation. ``location`` is the document's start position.
    """
    if not isinstance(raw, dict):
        raise _err(location, "manifest document must be a mapping")

    unknown = set(raw) - _ENVELOPE_KEYS
    if unknown:
        raise _err(
            location,
            f"unknown manifest key(s) {sorted(unknown)}; "
            f"expected {sorted(_ENVELOPE_KEYS)}",
        )

    api_version = raw.get("apiVersion")
    if api_version != API_VERSION:
        raise _err(
            location,
            f'apiVersion must be "{API_VERSION}"; got {api_version!r}',
        )

    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind:
        raise _err(location, "kind is required and must be a string")
    handler = KIND_REGISTRY.get(kind)
    if handler is None:
        valid = sorted(
            k for k, h in KIND_REGISTRY.items() if h.category == "declarable"
        )
        hint = None
        kebab_guess = kind.replace("_", "-")
        if kebab_guess != kind and kebab_guess in KIND_REGISTRY:
            hint = f'kind identifiers are lower-kebab; did you mean "{kebab_guess}"?'
        raise _err(
            location,
            f"unknown kind {kind!r}; valid kinds: {', '.join(valid)}",
            hint=hint,
        )
    if handler.category != "declarable":
        raise _err(
            location,
            f"{kind} is provided by the app and cannot be declared in a manifest",
        )

    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        raise _err(location, "metadata is required and must be a mapping")
    unknown_meta = set(metadata) - _METADATA_KEYS
    if unknown_meta:
        raise _err(
            location,
            f"unknown metadata key(s) {sorted(unknown_meta)}; "
            f"expected {sorted(_METADATA_KEYS)}",
        )
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise _err(location, "metadata.name is required and must be a string")
    description = metadata.get("description")
    if description is not None and not isinstance(description, str):
        raise _err(location, "metadata.description must be a string")

    if kind in _SINGLETON_KINDS and name != "default":
        raise _err(
            location,
            f'{kind} accepts only metadata.name "default"; got {name!r}',
        )

    if "spec" not in raw:
        raise _err(location, "spec is required (an empty mapping {} is fine)")
    spec = raw.get("spec")
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise _err(location, "spec must be a mapping")

    return Document(
        kind=kind,
        name=name,
        description=description,
        spec=spec,
        location=location,
    )

"""Manifest loading: directory walk, YAML parse with positions, and the
operator publisher.

Loading is auto-load (conf.d semantics): every ``*.yaml`` / ``*.yml``
under the resources directory, walked in lexicographic relative-path
order, documents in file order. That order IS config-load order for the
framework (first-matching-reference origin attribution).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from agentworks.errors import ConfigError
from agentworks.manifests.decode import decode_document
from agentworks.manifests.envelope import validate_envelope
from agentworks.source_location import SourceLocation

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from agentworks.resources.registry import Registry

RESOURCES_DIRNAME = "resources"
"""The resources directory's name, sibling to config.toml."""

_MANIFEST_SUFFIXES = {".yaml", ".yml"}


@dataclass(frozen=True)
class ManifestEntry:
    """One decoded resource plus where it came from."""

    kind: str
    name: str
    resource: Any
    location: SourceLocation


@dataclass(frozen=True)
class ManifestSet:
    """All operator manifests, decoded, in config-load order.

    ``issues`` mirrors ``Config.config_issues``: spec-level warnings
    (unknown keys on warn-mode kinds, env hygiene) prefixed with the
    document's ``file:line``.
    """

    entries: tuple[ManifestEntry, ...]
    issues: tuple[str, ...]

    @classmethod
    def empty(cls) -> ManifestSet:
        return cls(entries=(), issues=())

    def publish_to(self, registry: Registry) -> None:
        """Publish every entry as an operator-declared Resource. Mirrors
        ``Config.publish_to``; the Registry's collision handling is the
        cross-source duplicate backstop.
        """
        from agentworks.resources import Origin

        for entry in self.entries:
            registry.add(
                entry.kind,
                entry.name,
                entry.resource,
                Origin.operator_declared(
                    file=entry.location.file, line=entry.location.line
                ),
            )


def _iter_manifest_files(resources_dir: Path) -> Iterator[Path]:
    if not resources_dir.is_dir():
        return
    for path in sorted(
        resources_dir.rglob("*"), key=lambda p: str(p.relative_to(resources_dir))
    ):
        if not path.is_file() or path.suffix not in _MANIFEST_SUFFIXES:
            continue
        rel = path.relative_to(resources_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        yield path


def _iter_documents(path: Path) -> Iterator[tuple[object, SourceLocation]]:
    """Yield ``(value, location)`` per YAML document in ``path``.

    ``yaml.compose_all`` yields one node per document carrying its start
    mark; values are constructed from the composed node with the safe
    constructor so per-document line numbers survive.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not valid UTF-8: {exc}") from exc
    try:
        for node in yaml.compose_all(text, Loader=yaml.SafeLoader):
            if node is None:
                continue
            location = SourceLocation(file=path, line=node.start_mark.line + 1)
            constructor = yaml.SafeLoader("")
            # construct_document is untyped in types-PyYAML; the call is
            # the documented node-to-value path for composed documents.
            value = constructor.construct_document(node)  # type: ignore[no-untyped-call]
            if value is None:
                # A document containing only whitespace/comments composes
                # to a null scalar; treat it like an absent document.
                continue
            yield value, location
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f":{mark.line + 1}" if mark is not None else ""
        raise ConfigError(f"{path}{line}: invalid YAML: {exc}") from exc


def load_manifests(resources_dir: Path) -> ManifestSet:
    """Load, validate, and decode every manifest under ``resources_dir``.

    A missing directory is valid (empty set). Raises ``ConfigError`` on
    envelope violations, spec-level validation errors, and duplicate
    ``(kind, name)`` declarations across the whole set.
    """
    entries: list[ManifestEntry] = []
    issues: list[str] = []
    seen: dict[tuple[str, str], SourceLocation] = {}

    for path in _iter_manifest_files(resources_dir):
        for value, location in _iter_documents(path):
            doc = validate_envelope(value, location)
            key = (doc.kind, doc.name)
            if key in seen:
                first = seen[key]
                raise ConfigError(
                    f"{location.file}:{location.line}: duplicate {doc.kind} "
                    f'"{doc.name}" (also declared at {first.file}:{first.line})',
                )
            seen[key] = location
            resource = decode_document(doc, issues)
            entries.append(
                ManifestEntry(
                    kind=doc.kind,
                    name=doc.name,
                    resource=resource,
                    location=location,
                )
            )

    return ManifestSet(entries=tuple(entries), issues=tuple(issues))

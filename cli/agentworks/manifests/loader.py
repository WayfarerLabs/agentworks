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
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from agentworks.resources.registry import Registry

RESOURCES_DIRNAME = "resources"
"""The resources directory's name, sibling to config.toml."""

_MANIFEST_SUFFIXES = {".yaml", ".yml"}


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys.

    tomllib errors on duplicate keys, so silent last-write-wins here
    would be a parity loosening on the new surface.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[object] = set()
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                # Deliberate rejection, not an accident: merge keys were
                # dropped by YAML 1.2 and (like Kubernetes manifests) we
                # keep documents literal. Repeat the fields instead.
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    "YAML merge keys (<<) are not supported in manifests; repeat the fields instead",
                    key_node.start_mark,
                )
            key = self.construct_object(key_node, deep=True)
            try:
                is_duplicate = key in seen
                if not is_duplicate:
                    seen.add(key)
            except TypeError:
                # Unhashable key (dict, list, !!set, ...): skip the
                # duplicate check; the base class raises its own clean
                # "unhashable key" ConstructorError below. (Both ops sit
                # in the try: set membership silently coerces a set key
                # to frozenset instead of raising, so add() is the one
                # that actually trips for !!set keys.)
                continue
            if is_duplicate:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"duplicate mapping key {key!r}",
                    key_node.start_mark,
                )
        return super().construct_mapping(node, deep=deep)


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

    ``issues`` mirrors ``Config.config_issues``: the load-time ADVISORIES
    a document earns (a non-conforming secret name, env hygiene), prefixed
    with the document's ``file:line``. Everything that used to warn about
    the SHAPE of a spec is a hard error now (FR12), so what rides this
    channel is only what an operator should act on without the config
    refusing to load.

    There is no deprecation channel here. The one manifest deprecation
    that ever rode one (the sibling capability-config shape) is a hard
    error now, so the channel would have been a field that is always
    empty, a warn loop that never fires, and a bundle gate that always
    passes. ``Config.deprecation_issues`` is a separate thing and stays:
    config.toml still carries a deprecated section to nudge about.
    """

    entries: tuple[ManifestEntry, ...]
    issues: tuple[str, ...]

    @classmethod
    def empty(cls) -> ManifestSet:
        return cls(entries=(), issues=())

    def publish_to(self, registry: Registry) -> None:
        """Publish every entry as an operator-declared Resource. Mirrors
        ``Config.publish_to``; the Registry's collision handling is the
        cross-source duplicate backstop, and each kind's
        ``builtin_override`` policy (enforced at ``Registry.add``) is
        what reserves built-in names -- this publisher knows nothing
        about kinds.
        """
        from agentworks.resources import Origin

        for entry in self.entries:
            registry.add(
                entry.kind,
                entry.name,
                entry.resource,
                Origin.operator_declared(file=entry.location.file, line=entry.location.line),
            )


def _iter_manifest_files(resources_dir: Path) -> Iterator[Path]:
    """Walk manifest files: per directory, files first (sorted by name),
    then subdirectories (sorted by name), recursively.

    A hand-rolled walk (rather than ``rglob``) so dot-directories are
    pruned without descending into them. Files-first-per-directory is
    the deliberate ordering contract: root manifests precede anything
    nested, and ``a/`` sorts before ``a-b/`` component-wise. This order
    IS config-load order for the framework (first-matching-reference
    origin attribution), so it must stay stable.
    """
    if not resources_dir.is_dir():
        return
    stack = [resources_dir]
    while stack:
        directory = stack.pop()
        subdirs: list[Path] = []
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                subdirs.append(child)
            elif child.is_file() and child.suffix in _MANIFEST_SUFFIXES:
                yield child
        stack.extend(reversed(subdirs))


def _iter_documents(path: Path, text: str | None = None) -> Iterator[tuple[object, SourceLocation]]:
    """Yield ``(value, location)`` per YAML document in ``path``.

    ``yaml.compose_all`` yields one node per document carrying its start
    mark; values are constructed from the composed node with the safe
    constructor so per-document line numbers survive.

    ``text`` supplies the content instead of reading ``path``, for a
    caller asking what a file it has NOT written would load as (see
    ``load_manifests``'s ``overlay``). ``path`` still frames every
    location, so errors name the file the operator will edit rather than
    wherever the text came from.
    """
    if text is None:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{path}: not valid UTF-8: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"{path}: cannot read manifest: {exc}") from exc
    try:
        for node in yaml.compose_all(text, Loader=_StrictLoader):
            if node is None:
                continue
            location = SourceLocation(file=path, line=node.start_mark.line + 1)
            constructor = _StrictLoader("")
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


@dataclass(frozen=True)
class LocateResult:
    """Outcome of the tolerant declaration scan (``locate_document``)."""

    location: SourceLocation | None
    unreadable: tuple[Path, ...]


def locate_document(resources_dir: Path, kind: str, name: str) -> LocateResult:
    """Best-effort ``(file, line)`` of the manifest document declaring
    ``kind``/``name`` -- WITHOUT validating anything.

    The fix-it path for ``agw resource edit``: when the config is
    failing validation, the strict registry lookup is unavailable, but
    the operator needs the declaring file MORE, not less. This scan
    reads raw envelopes only (``kind`` / ``metadata.name``), skips
    files that fail to parse (collected in ``unreadable`` so the caller
    can point at them), and never decodes specs.
    """
    unreadable: list[Path] = []
    for path in _iter_manifest_files(resources_dir):
        try:
            documents = list(_iter_documents(path))
        except ConfigError:
            unreadable.append(path)
            continue
        for value, location in documents:
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            doc_name = metadata.get("name") if isinstance(metadata, dict) else None
            if value.get("kind") == kind and doc_name == name:
                return LocateResult(location=location, unreadable=tuple(unreadable))
    return LocateResult(location=None, unreadable=tuple(unreadable))


def load_manifests(resources_dir: Path, *, overlay: Mapping[Path, str] | None = None) -> ManifestSet:
    """Load, validate, and decode every manifest under ``resources_dir``.

    A missing directory is valid (empty set). Raises ``ConfigError`` on
    envelope violations, spec-level validation errors, and duplicate
    ``(kind, name)`` declarations across the whole set.

    ``overlay`` maps a manifest path to the text to read INSTEAD of
    whatever is on disk, including for paths that do not exist yet. It
    answers "would this load?" for a tree a caller is about to write but
    has not: ``agw resource migrate`` uses it so a dry run refuses
    exactly what the real run refuses (migrate/preflight.py).

    One deliberate imprecision: an overlay path with no file on disk is
    loaded AFTER the walk rather than in the position it will occupy once
    it exists. Load order is a real contract (it is first-matching
    -reference attribution order), so this would matter to a caller
    reading the returned entries; it does not matter to one asking only
    whether the set loads, which is what an overlay is for.
    """
    entries: list[ManifestEntry] = []
    issues: list[str] = []
    seen: dict[tuple[str, str], SourceLocation] = {}

    paths = list(_iter_manifest_files(resources_dir))
    if overlay:
        paths.extend(sorted(set(overlay) - set(paths)))

    for path in paths:
        for value, location in _iter_documents(path, overlay.get(path) if overlay else None):
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

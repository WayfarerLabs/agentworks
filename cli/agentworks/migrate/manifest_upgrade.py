"""Manifest upgrade: rewrite YAML manifests still on a retired shape.

The migrator's other half moves TOML declarations into new YAML
documents. This half touches manifests that already exist: it rewrites
the ONE retired spelling operators can still have on disk, the legacy
sibling capability pair (``platform: lima`` plus ``platform_config:``,
and likewise ``provider``/``provider_config``), into the tagged table
(``platform: {name: lima, ...}``) that decode now requires.

Two properties shape everything here.

**Comments survive.** These are files the operator wrote and keeps
editing, so the rewrite round-trips through ``ruamel.yaml`` rather than
re-emitting from parsed values: comments, quote style, key order,
document separators, and unrelated keys all stay put. Only the affected
document's capability keys move.

**The upgrade is whole-tree, not selector-scoped.** The old shape does
not load at all now, so there is no valid partially-upgraded tree: one
document left behind leaves every command failing, and it would also
break this run's own verification, which rebuilds the registry from the
whole resources directory. So every run upgrades every legacy document it
finds, and selectors go on scoping the TOML units only.

The shape table below is hand-maintained rather than derived from the
capability-kind descriptors, matching ``migrate/toml_resources.py``: the
migrator is a deliberately independent oracle for shapes the framework no
longer speaks, and deriving from live wiring is exactly what it must not
do (the descriptor's deferred ``migration_participation`` field records
the same reasoning).
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
from typing import TYPE_CHECKING, Any, cast

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

_LEGACY_SIBLING_SHAPES: dict[str, tuple[str, str]] = {
    "vm-site": ("platform", "platform_config"),
    "git-credential": ("provider", "provider_config"),
}
"""Declarable kind -> (naming field, retired sibling config field).

Only the two surfaces that ever ACCEPTED the sibling pair. A session
template's ``harness_integration`` was born tagged (wave 1 renamed the
field and rejected the string form in the same release), so no manifest
on disk can carry ``harness_integration`` beside a
``harness_integration_config``.
"""


@dataclass(frozen=True)
class LegacyDocument:
    """One manifest document still spelling the retired sibling shape."""

    path: Path
    kind: str
    name: str

    @property
    def token(self) -> str:
        """The ``kind/name`` display token, matching every other surface."""
        return f"{self.kind}/{self.name}"


@dataclass(frozen=True)
class ManifestRewrite:
    """One comment-preserving replacement of an existing manifest file."""

    path: Path
    old_bytes: bytes
    old_digest: str
    new_text: str
    new_digest: str
    resources: tuple[str, ...]


def spec_is_legacy(kind: str, spec: object) -> bool:
    """Whether ``spec`` names its capability in the retired sibling shape.

    The naming field as a STRING is the whole test: a sibling
    ``*_config`` may or may not be present (``platform: lima`` alone is
    equally retired), and a tagged table beside a stray ``*_config`` is
    an error the operator must resolve by hand, not a mechanical fold.
    """
    pair = _LEGACY_SIBLING_SHAPES.get(kind)
    if pair is None or not isinstance(spec, dict):
        return False
    return isinstance(spec.get(pair[0]), str)


def discover_legacy_documents(resources_dir: Path) -> list[LegacyDocument]:
    """Every manifest document still on the retired sibling shape.

    Reads raw YAML rather than going through ``load_manifests``, which is
    load-bearing: the documents this looks for are exactly the ones that
    no longer decode, so the loader would raise on the first one instead
    of reporting all of them. Unparseable files are skipped for the same
    reason ``locate_document`` skips them: this is a best-effort scan
    whose failure mode must be "found nothing here", never "the whole
    remediation command is unavailable".
    """
    from agentworks.manifests.loader import _iter_manifest_files

    found: list[LegacyDocument] = []
    for path in _iter_manifest_files(resources_dir):
        for value in _load_documents(path):
            if not isinstance(value, dict):
                continue
            kind = value.get("kind")
            metadata = value.get("metadata")
            name = metadata.get("name") if isinstance(metadata, dict) else None
            if isinstance(kind, str) and isinstance(name, str) and spec_is_legacy(kind, value.get("spec")):
                found.append(LegacyDocument(path=path, kind=kind, name=name))
    return found


def plan_rewrites(documents: list[LegacyDocument]) -> list[ManifestRewrite]:
    """One replacement per FILE carrying legacy documents, in scan order.

    Grouped by file because a replacement is a whole-file write: two
    legacy documents in one file are one rewrite, and the digest guards
    in ``execute_plan`` are per file.
    """
    by_path: dict[Path, list[str]] = {}
    for document in documents:
        by_path.setdefault(document.path, []).append(document.name)

    rewrites: list[ManifestRewrite] = []
    for path, names in by_path.items():
        old_bytes = path.read_bytes()
        new_text, changed = _rewritten_text(old_bytes, set(names))
        if not changed:
            continue
        rewrites.append(
            ManifestRewrite(
                path=path,
                old_bytes=old_bytes,
                old_digest=sha256(old_bytes).hexdigest(),
                new_text=new_text,
                new_digest=sha256(new_text.encode()).hexdigest(),
                resources=tuple(changed),
            )
        )
    return rewrites


def legacy_pre_rows(documents: list[LegacyDocument]) -> dict[tuple[str, str], Any]:
    """The verification pre-side for the rewritten documents.

    Decodes each ORIGINAL document through the manifest decoder, after
    folding its legacy pair into the tagged table in a plain dict. The
    fold has to happen here rather than in decode because decode refuses
    the old shape outright now; this module is where knowledge of that
    shape lives.

    Not a tautology against the post-side: this reads parsed VALUES, while
    the post-side decodes the text ``ruamel`` emitted. A rewrite that
    dropped a key or changed a scalar shows up as a row difference.
    """
    from agentworks.manifests.decode import decode_document
    from agentworks.manifests.envelope import validate_envelope
    from agentworks.migrate.verify import strip_source_fields
    from agentworks.source_location import SourceLocation

    wanted: dict[Path, set[str]] = {}
    for legacy in documents:
        wanted.setdefault(legacy.path, set()).add(legacy.name)

    rows: dict[tuple[str, str], Any] = {}
    for path, names in wanted.items():
        # Line 0, like the emitted-key-set guard's: these rows exist only
        # to be compared, and ``strip_source_fields`` drops the source
        # location, so carrying a made-up line number would be a fiction
        # nothing reads.
        location = SourceLocation(file=path, line=0)
        for value in _load_documents(path):
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            name = metadata.get("name") if isinstance(metadata, dict) else None
            if name not in names or not spec_is_legacy(str(value.get("kind")), value.get("spec")):
                continue
            document = validate_envelope(_folded_document(value), location)
            rows[(document.kind, document.name)] = strip_source_fields(decode_document(document, []))
    return rows


def _round_trip() -> Any:
    """The one configured round-trip YAML for this module.

    ``preserve_quotes`` is not cosmetic here. An operator writing
    ``subscription_id: "0000"`` means the string; re-emitting it bare
    would make it the integer 0 on the next load. Verification would
    catch that, but only after the operator's file had been rewritten
    wrong, so the emitter is configured not to do it in the first place.
    """
    from ruamel.yaml import YAML

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    return yaml


def _load_documents(path: Path) -> list[Any]:
    """Every YAML document in ``path``, or none if it does not parse."""
    from ruamel.yaml.error import YAMLError

    try:
        text = path.read_text(encoding="utf-8")
        return list(_round_trip().load_all(text))
    except (OSError, UnicodeDecodeError, YAMLError):
        return []


def _folded_document(value: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``value`` with its spec's legacy pair folded.

    Plain-dict fold for the pre-row decode; the comment-preserving one
    below does the same thing to a round-trip node tree.
    """
    kind = str(value.get("kind"))
    field, config_field = _LEGACY_SIBLING_SHAPES[kind]
    spec = dict(cast("dict[str, Any]", value["spec"]))
    config = spec.pop(config_field, None)
    spec[field] = _tagged_table(kind, value, spec.pop(field), config)
    return {**value, "spec": spec}


def _tagged_table(kind: str, value: dict[str, Any], capability: str, config: object) -> dict[str, Any]:
    """``{name: <capability>, <config keys...>}``, refusing a name clash."""
    if isinstance(config, dict) and "name" in config:
        metadata = value.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else "?"
        raise ConfigError(
            f"cannot upgrade {kind}/{name}: its capability config carries a "
            f"'name' key, which collides with the tagged table's discriminator",
            hint="Fold this resource's capability table by hand.",
        )
    table: dict[str, Any] = {"name": capability}
    if isinstance(config, dict):
        table.update(config)
    return table


def _rewritten_text(old_bytes: bytes, names: set[str]) -> tuple[str, list[str]]:
    """Round-trip one file's text, folding the named documents' pairs.

    Returns the new text and the ``kind/name`` tokens actually changed.
    """
    yaml = _round_trip()
    old_text = old_bytes.decode("utf-8")
    preamble, trailing = _stream_comments_outside_markers(old_text)
    documents = list(yaml.load_all(old_text))
    changed: list[str] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        kind = document.get("kind")
        metadata = document.get("metadata")
        spec = document.get("spec")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if name not in names or not isinstance(kind, str) or not spec_is_legacy(kind, spec):
            continue
        _fold_in_place(kind, document, cast("Any", spec))
        changed.append(f"{kind}/{name}")
    if not changed:
        return old_text, []

    # ruamel stores comments inside documents but not each document's
    # start/end marker spelling. Emit one document at a time so a mixed
    # stream (implicit first document, explicit second; only one `...`;
    # marker comments) round-trips its own boundaries.
    markers = _document_markers(old_text, len(documents))
    parts: list[str] = []
    for document, (explicit_start, explicit_end, start_comment, end_comment) in zip(documents, markers, strict=True):
        yaml.explicit_start = explicit_start
        yaml.explicit_end = explicit_end
        stream = StringIO()
        yaml.dump(document, stream)
        parts.append(_restore_outer_marker_comments(stream.getvalue(), start_comment, end_comment))
    return preamble + "".join(parts) + trailing, changed


def _fold_in_place(kind: str, document: dict[str, Any], spec: Any) -> None:
    """Replace the spec's legacy pair with one tagged table, in place.

    Comment attachments follow their keys: the naming field's own
    comments stay on the (same-named) tagged table, each config key keeps
    its comments inside it, and a comment attached to the retired
    ``*_config`` key is merged onto the table rather than dropped with
    the key it was written beside.
    """
    from ruamel.yaml.comments import CommentedMap

    field, config_field = _LEGACY_SIBLING_SHAPES[kind]
    index = list(spec).index(field)
    comments = spec.ca.items.get(field)
    config = spec.get(config_field)
    config_comments = spec.ca.items.get(config_field)
    # Same refusal the plain-dict fold makes, before anything is mutated.
    _tagged_table(kind, document, spec[field], config)

    table = CommentedMap()
    table["name"] = spec[field]
    if isinstance(config, CommentedMap):
        # Assign the original nodes, then carry every comment attachment
        # (key, value, and nested map comments) into the tagged table.
        for key, item in config.items():
            table[key] = item
        for key, item in config.ca.items.items():
            table.ca.items[key] = item
    elif isinstance(config, dict):
        table.update(config)

    if config_field in spec:
        del spec[config_field]
    del spec[field]
    spec.insert(index, field, table)
    carried = _carried_comments(comments, config_comments)
    if carried is not None:
        spec.ca.items[field] = carried


def _carried_comments(naming: Any, config: Any) -> Any:
    """The comments both retired keys carried, moved onto the tagged table.

    ruamel attaches everything written after a key, up to the next one, as
    that key's post-VALUE comment, so each retired key can carry a run of
    comment lines. Both runs describe the capability and both survive,
    concatenated in source order onto the key that remains.

    They are also indented one level deeper, because what used to sit
    between two sibling spec keys now sits inside a nested table; left
    alone it would read as a comment on the spec rather than on the
    capability. A same-line trailing comment (``platform: lima  # local``)
    has no leading newline and is left exactly where it is.
    """
    naming_token = naming[2] if naming is not None and len(naming) > 2 else None
    config_token = config[2] if config is not None and len(config) > 2 else None
    tokens = [token for token in (naming_token, config_token) if token is not None]
    if not tokens:
        return naming if naming is not None else config
    carrier = tokens[0]
    carrier.value = _indented_comment("".join(token.value for token in tokens))
    slots = list(naming) if naming is not None else [None, None, None, None]
    slots[2] = carrier
    return slots


def _indented_comment(text: str) -> str:
    """Indent every own-line comment in ``text`` one level deeper."""
    head, newline, rest = text.partition("\n")
    if not newline:
        return text
    return head + newline + "\n".join(f"  {line}" if line.strip() else line for line in rest.split("\n"))


def _stream_comments_outside_markers(text: str) -> tuple[str, str]:
    """Extract comments ruamel cannot retain outside explicit stream markers."""
    lines = text.splitlines(keepends=True)
    non_comment = [index for index, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")]
    start = non_comment[0] if non_comment and lines[non_comment[0]].strip().startswith("---") else None
    end = non_comment[-1] if non_comment and lines[non_comment[-1]].strip().startswith("...") else None
    preamble = "".join(lines[:start]) if start is not None else ""
    trailing = "".join(lines[end + 1 :]) if end is not None else ""
    return preamble, trailing


def _restore_outer_marker_comments(text: str, opening: str | None, closing: str | None) -> str:
    """Reattach outer marker suffixes dropped by ruamel's emitter."""
    lines = text.splitlines(keepends=True)
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if opening is not None and nonempty and lines[nonempty[0]].strip() == "---":
        ending = "\n" if lines[nonempty[0]].endswith("\n") else ""
        lines[nonempty[0]] = f"---{opening}{ending}"
    if closing is not None and nonempty and lines[nonempty[-1]].strip() == "...":
        ending = "\n" if lines[nonempty[-1]].endswith("\n") else ""
        lines[nonempty[-1]] = f"...{closing}{ending}"
    return "".join(lines)


def _document_markers(text: str, count: int) -> list[tuple[bool, bool, str | None, str | None]]:
    """Capture explicit marker presence and inline comments per document."""
    result: list[list[object]] = [[False, False, None, None] for _ in range(count)]
    document = 0
    seen_content = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("---") and (stripped == "---" or stripped.startswith("--- #")):
            if seen_content:
                document += 1
            if document < count:
                result[document][0] = True
                result[document][2] = stripped[3:]
            continue
        if stripped.startswith("...") and (stripped == "..." or stripped.startswith("... #")):
            if document < count:
                result[document][1] = True
                result[document][3] = stripped[3:]
            continue
        seen_content = True
    return [
        (bool(a), bool(b), c if isinstance(c, str) else None, d if isinstance(d, str) else None)
        for a, b, c, d in result
    ]

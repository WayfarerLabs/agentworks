"""``agw resource sample``: the generated sample manifest.

A sample is RENDERED, live, from the same models the loader validates
against (``manifests/reference.py`` collects, ``manifests/skeleton.py``
presents). There are no bundled sample files, and that is the point: a
checked-in sample is a second description of a kind, and the moment a field
is added, renamed, or defaulted differently, it is a wrong one. A generated
one cannot drift, and it covers a capability a plugin contributed on the
same terms as a first-party kind, which no curated file ever did.

The output is fully commented, so a written sample is inert text the loader
ignores: ``--write`` can never create a duplicate or a live resource, and
running it twice just appends more comments. The uncomment rule is one
leading ``#`` per line, which turns document lines into YAML and prose
lines into ordinary YAML comments; ``manifests/skeleton.py`` owns it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.manifests.reference import kind_reference
from agentworks.manifests.skeleton import skeleton_text
from agentworks.manifests.spec_model import declarable_kinds
from agentworks.resources import KIND_REGISTRY

if TYPE_CHECKING:
    from pathlib import Path

_SUFFIXES = {".yaml", ".yml"}


def sample_text(kind: str | None = None, *, all_kinds: bool = False) -> str:
    """The rendered sample for ``kind``, or (with ``all_kinds``) every kind.

    Dumping every kind requires the explicit ``all_kinds`` opt-in
    (``--all``), mirroring ``agw resource migrate``: a bare invocation is an
    error, never a wall of thirteen samples by accident.
    """
    kinds = _validated_kinds(kind, all_kinds)
    parts = [skeleton_text(kind_reference(k)).rstrip("\n") for k in kinds]
    # A commented document separator between kinds, so the concatenation is
    # a real multi-document file once uncommented rather than a pile of
    # documents YAML would read as one.
    return "\n#---\n".join(parts) + "\n"


def write_sample(
    resources_dir: Path,
    filename: str,
    kind: str | None = None,
    *,
    all_kinds: bool = False,
) -> tuple[Path, bool]:
    """Write (or append) the sample under the resources directory.

    Returns ``(path, appended)``. The content is fully commented, so no
    document separator is involved: appending comments to an existing
    manifest file cannot change what it declares.

    A file this CREATES opens with the yaml-language-server modeline, and
    the schema set it refers to is written alongside, because a modeline
    pointing at a file that is not there is a red banner in the operator's
    editor rather than a missing feature.

    An append never INSERTS a modeline: one has to be at the top, so
    stamping a file that has none would shift every line number the
    operator already knows. It does RESTAMP one that is already there and
    has stopped being true, which costs no line numbers at all. A file
    created for one kind carries that kind's own schema, and appending a
    second kind's sample to it leaves the editor validating the new
    document against the first kind's shape: red underlines under
    configuration the loader accepts, which is worse than shipping no
    schema association at all. The file becomes a multi-kind file, so it
    gets the multi-kind (envelope) schema.

    The modeline is NOT part of :func:`sample_text`, which stays fully
    commented under its own uncomment rule. It is a file header, so the
    rule ("delete one leading ``#`` from the document lines") is still true
    of the body.
    """
    from agentworks.manifests.emit import SCHEMA_DIRNAME, modeline, write_schema_set

    target = _validated_target(resources_dir, filename)
    text = sample_text(kind, all_kinds=all_kinds)
    appended = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    if appended:
        existing = target.read_text(encoding="utf-8")
        separator = "" if existing.endswith("\n") or not existing else "\n"
        body, restamped = _restamped(
            f"{existing}{separator}\n{text}",
            target=target,
            resources_dir=resources_dir,
            kinds=_validated_kinds(kind, all_kinds),
        )
        target.write_text(body, encoding="utf-8")
        if restamped:
            # The line now names the envelope schema, so that file has to
            # be there for the same reason a created file's does.
            write_schema_set(resources_dir / SCHEMA_DIRNAME)
    else:
        header = modeline(manifest_path=target, resources_dir=resources_dir, kind=kind)
        target.write_text(f"{header}\n{text}", encoding="utf-8")
        write_schema_set(resources_dir / SCHEMA_DIRNAME)
    return target, appended


def _restamped(text: str, *, target: Path, resources_dir: Path, kinds: tuple[str, ...]) -> tuple[str, bool]:
    """``text`` with its modeline corrected for the kinds now in the file.

    Returns ``(text, changed)``. A file with no modeline is returned as it
    came: this only ever REPLACES a first line that is already one, never
    adds one, so no line number moves.

    The existing modeline is what says which kind the file was for, so
    nothing has to parse the body (which is mostly commented-out samples
    that no YAML parser would report a kind for anyway).
    """
    from agentworks.manifests.emit import MODELINE_PREFIX, modeline

    first, newline, rest = text.partition("\n")
    if not first.startswith(MODELINE_PREFIX):
        return text, False
    only = kinds[0] if len(kinds) == 1 else None
    if first == modeline(manifest_path=target, resources_dir=resources_dir, kind=only):
        return text, False
    envelope = modeline(manifest_path=target, resources_dir=resources_dir, kind=None)
    if first == envelope:
        return text, False
    return f"{envelope}{newline}{rest}", True


def _validated_kinds(kind: str | None, all_kinds: bool) -> tuple[str, ...]:
    # The kinds a document can exist for, which is the same derivation
    # emission uses for what it may describe.
    known_kinds = declarable_kinds()
    if all_kinds and kind is not None:
        raise ValidationError(
            "pass a kind or --all, not both",
            hint="A kind prints one sample; --all prints every kind's.",
        )
    if all_kinds:
        return known_kinds
    known = ", ".join(known_kinds)
    if kind is None:
        raise ValidationError(
            "indicate a kind to sample, or pass --all",
            hint=f"Example: `agw resource sample secret`. Kinds: {known}.",
        )
    if kind not in known_kinds:
        handler = KIND_REGISTRY.get(kind)
        if handler is not None and handler.category == "capability":
            # Capability kinds (harness-integration, secret-backend,
            # vm-platform, git-credential-provider) are code-backed and
            # carry no manifest of their own, so there is nothing to
            # sample. `resource kinds` lists them alongside the declarable
            # kinds, so a curious operator will ask for one here: name the
            # kind, point at the declarable set that does have samples, and
            # point at the surface that DOES document a capability's config.
            raise ValidationError(
                f"{kind!r} is a capability kind; it has no sample manifest",
                entity_kind="resource",
                entity_name=kind,
                hint=f"`agw resource describe-kind {kind}` documents it. Declarable kinds: {known}",
            )
        raise ValidationError(
            f"unknown kind {kind!r}",
            entity_kind="resource",
            entity_name=kind,
            hint=f"known kinds: {known}",
        )
    return (kind,)


def _validated_target(resources_dir: Path, filename: str) -> Path:
    from pathlib import PurePath

    rel = PurePath(filename)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValidationError(
            f"--write takes a path relative to the resources directory; got {filename!r}",
            hint=f"Files land under {resources_dir}.",
        )
    if rel.suffix not in _SUFFIXES:
        raise ValidationError(
            f"--write requires a .yaml or .yml filename; got {filename!r}",
            hint="The manifest loader only reads *.yaml / *.yml files.",
        )
    return resources_dir / rel

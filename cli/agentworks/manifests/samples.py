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

import os
import stat
import tempfile
from pathlib import Path
from typing import Literal

from agentworks.errors import ValidationError
from agentworks.manifests.reference import kind_reference
from agentworks.manifests.skeleton import skeleton_text
from agentworks.manifests.spec_model import declarable_kinds
from agentworks.path_rendering import format_host_path
from agentworks.resources import KIND_REGISTRY

_SUFFIXES = {".yaml", ".yml"}

SampleWriteOutcome = Literal["created", "appended", "filled"]
"""What ``write_sample`` did, at the granularity the CLI has to describe.

The three differ in exactly what ends up in the file that the operator
then has to act on: a ``created`` file gets a modeline and no separator,
an ``appended`` one gets a separator and keeps whatever modeline it had,
and a ``filled`` one (the file existed and was blank) gets neither.
"""

_SEPARATOR = "#---"
"""A YAML document separator, commented like every other document line.

It is a DOCUMENT line, not prose, so the skeleton's one rule ("delete one
leading ``#``") turns it into a real ``---`` at the same moment it turns
the lines around it into real YAML. Anything that concatenates a sample
onto content that is already there has to emit one: without it the
appended document's keys merge into the preceding document, and the load
fails on the duplicate ``apiVersion`` rather than on anything the operator
can see.
"""


def sample_text(kind: str | None = None, *, all_kinds: bool = False) -> str:
    """The rendered sample for ``kind``, or (with ``all_kinds``) every kind.

    Dumping every kind requires the explicit ``all_kinds`` opt-in
    (``--all``): a bare invocation is an error, never a wall of thirteen
    samples by accident. That is the CLI's standing shape for a bulk
    operation (see ``.claude/rules/cli-conventions.md``).
    """
    kinds = _validated_kinds(kind, all_kinds)
    parts = [skeleton_text(kind_reference(k)).rstrip("\n") for k in kinds]
    # A commented document separator between kinds, so the concatenation is
    # a real multi-document file once uncommented rather than a pile of
    # documents YAML would read as one.
    return f"\n{_SEPARATOR}\n".join(parts) + "\n"


def write_sample(
    resources_dir: Path,
    filename: str,
    kind: str | None = None,
    *,
    all_kinds: bool = False,
) -> tuple[Path, SampleWriteOutcome]:
    """Write (or append) the sample under the resources directory.

    Returns ``(path, outcome)``. THREE outcomes, not two, because the two
    things the caller has to tell an operator about (a separator above the
    new document, a modeline on the first line) are not present in the
    same cases:

    - ``"created"``: the file was not there. It opens with the modeline
      and carries no separator, having nothing to separate from.
    - ``"appended"``: the file held content. A commented ``---``
      (:data:`_SEPARATOR`) sits above the new document, and the modeline
      is restamped if one was already there.
    - ``"filled"``: the file was there and blank. Neither: ``_joined``
      writes no separator over nothing, and an append never INSERTS a
      modeline. Collapsing this into ``"appended"`` had the command point
      an operator at a ``#---`` that was not in their file.

    The separator changes nothing about the file handed back, since what
    this writes is inert either way. It matters at the operator's NEXT
    step, which is the whole point of writing the sample. Uncomment a
    sample appended without one and its keys land in the preceding
    document, which takes down a resource that was already working and
    reports it as a duplicate ``apiVersion`` rather than as a missing
    separator.

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
    from agentworks.manifests.emit import SCHEMA_DIRNAME, modeline, restamped_modeline, write_schema_set

    target = _validated_target(resources_dir, filename)
    text = sample_text(kind, all_kinds=all_kinds)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        outcome: SampleWriteOutcome = "appended" if existing.strip() else "filled"
        body, restamped = restamped_modeline(
            _joined(existing, text),
            manifest_path=target,
            resources_dir=resources_dir,
            kinds=_validated_kinds(kind, all_kinds),
        )
        _replace_atomically(target, body)
        if restamped:
            # The line now names the envelope schema, so that file has to
            # be there for the same reason a created file's does.
            write_schema_set(resources_dir / SCHEMA_DIRNAME)
        return target, outcome
    header = modeline(manifest_path=target, resources_dir=resources_dir, kind=kind)
    target.write_text(f"{header}\n{text}", encoding="utf-8")
    write_schema_set(resources_dir / SCHEMA_DIRNAME)
    return target, "created"


def _replace_atomically(target: Path, text: str) -> None:
    """Put ``text`` in ``target``, or leave ``target`` exactly as it was.

    An append is a read-then-write over a manifest the OPERATOR wrote, and
    a plain write truncates before it writes. An interrupt between the two
    (a Ctrl-C, a full disk, a killed terminal) leaves the operator with an
    empty or half-written file whose original content is now nowhere:
    this command read it into memory and is the only thing that still has
    it. So the new content goes to a temp file that is fully on disk
    before anything replaces the original, and the replacement itself is
    one rename.

    Three details carry the guarantee, and none of them is optional:

    - the temp file is made in the TARGET'S directory, because
      ``os.replace`` is only atomic within a filesystem, and a resources
      directory can be a mount of its own;
    - it is flushed AND fsynced, so the rename cannot expose a file whose
      bytes are still in the page cache after a power loss;
    - it is dot-prefixed, so the one residue a ``SIGKILL`` can leave (a
      temp file with no rename after it) is a name the manifest loader
      skips, exactly as it skips the generated schema directory. See
      :func:`_validated_target`.

    The target's permission bits are carried over, because ``mkstemp``
    creates 0600 and appending to a manifest must not quietly narrow a
    file the operator chose to share. Ownership is not carried over, and
    does not need to be: this writes under the operator's own resources
    directory, as the operator.

    Only the replacing path uses this. A file being CREATED has no
    content to lose, and routing it through here would hand it 0600
    rather than the mode the operator's umask asks for.
    """
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(stat.S_IMODE(target.stat().st_mode))
        os.replace(tmp, target)
    finally:
        # A no-op on the success path: the rename consumed the temp file.
        # On every failing one, including an interrupt, this is what keeps
        # the residue from accumulating in the operator's directory.
        tmp.unlink(missing_ok=True)


def _joined(existing: str, text: str) -> str:
    """``text`` appended to ``existing`` as a NEW document.

    Nothing here parses ``existing``, for the reason
    ``emit.restamped_modeline`` does not either: an append is a text
    operation on a file the operator owns. The separator is emitted
    whenever there is any content to separate FROM, which is the question
    a parse would answer anyway, and it is cheap to be wrong in the
    harmless direction. A file holding only a modeline (or only comments)
    gets one it did not strictly need, and a leading ``---`` is how a YAML
    stream may open, so it costs a line rather than a meaning.
    """
    if not existing.strip():
        return text
    newline = "" if existing.endswith("\n") else "\n"
    return f"{existing}{newline}\n{_SEPARATOR}\n{text}"


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

    from agentworks.manifests.emit import SCHEMA_DIRNAME

    rel = PurePath(filename)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValidationError(
            f"--write takes a path relative to the resources directory; got {filename!r}",
            hint=f"Files land under {format_host_path(resources_dir)}.",
        )
    if rel.suffix not in _SUFFIXES:
        raise ValidationError(
            f"--write requires a .yaml or .yml filename; got {filename!r}",
            hint="The manifest loader only reads *.yaml / *.yml files.",
        )
    if any(part.startswith(".") for part in rel.parts):
        # The same rule `loader._iter_manifest_files` walks by, stated
        # against the path rather than against `.schema/` by name: the
        # loader prunes every dot-prefixed file AND directory, so a
        # manifest written under any of them is unreachable, not just one
        # written under the generated-schema directory.
        raise ValidationError(
            f"--write cannot write into a dot-prefixed file or directory; got {filename!r}",
            hint=(
                "The manifest loader skips dot-prefixed names (that is what keeps the generated "
                f"{SCHEMA_DIRNAME}/ out of the walk), so a manifest written there could never be activated."
            ),
        )
    return resources_dir / rel

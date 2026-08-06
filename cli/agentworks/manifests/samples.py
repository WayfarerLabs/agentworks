"""Bundled sample manifests behind ``agw resource sample``.

One fully-commented-out sample file per manifest-declarable kind, in
``agentworks/manifests/samples/``. Fully commented means every line
starts with ``#``: document lines are ``#`` + the YAML line (uncomment
in place by deleting one ``#``), prose lines are ``## `` (stripping one
``#`` leaves them as ordinary YAML comments). Written samples are
therefore inert text the loader ignores: ``--write`` can never create a
duplicate or a live resource, and running it twice just appends more
comments. The loader guarantee stays real rather than vacuous -- the
test suite mechanically strips one ``#`` per line and loads the result
through the real loader, and the whole uncommented set builds a full
registry. One deliberate exception: the secret-backend sample is
prose-only (no document to uncomment) until a config-bearing provider
ships -- there is nothing real to declare yet, and an uncommentable
document would teach a lie.
"""

from __future__ import annotations

from importlib import resources as importlib_resources
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.resources import KIND_REGISTRY

if TYPE_CHECKING:
    from pathlib import Path

_SAMPLES_PACKAGE = "agentworks.manifests"
_SAMPLES_DIR = "samples"

# The declarable kinds, sorted, straight from the kind registry's
# per-kind category (ADR 0016). This is the single source of truth the
# capability guard in `_validated_kinds` also keys off, so the two can
# never disagree: a kind is sampleable exactly when its handler is
# declarable. Sorted for a stable order that matches `agw resource
# kinds`, insulated from KIND_REGISTRY's import-order churn. The
# samples-exist test pins that every declarable kind ships a bundled
# sample file as new kinds are added.
SAMPLE_KINDS: tuple[str, ...] = tuple(
    sorted(name for name, handler in KIND_REGISTRY.items() if handler.category == "declarable")
)

_SUFFIXES = {".yaml", ".yml"}


def sample_text(kind: str | None = None, *, all_kinds: bool = False) -> str:
    """The bundled sample for ``kind``, or (with ``all_kinds``) every
    kind concatenated.

    Dumping every kind requires the explicit ``all_kinds`` opt-in
    (``--all``), mirroring ``agw resource migrate``: a bare invocation
    is an error, never a wall of thirteen samples by accident.
    """
    kinds = _validated_kinds(kind, all_kinds)
    parts = [_read_sample(k) for k in kinds]
    return "\n".join(part.rstrip("\n") for part in parts) + "\n"


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
    editor rather than a missing feature. An append leaves the existing
    file's first line alone: a modeline has to be at the top, so stamping
    one means shifting every line number in a file the operator already
    knows.

    The modeline is NOT part of :func:`sample_text`, which stays fully
    commented under its own uncomment rule. It is a file header, so the
    rule ("delete one leading ``#`` from the document lines") is still
    true of the body.
    """
    from agentworks.manifests.emit import SCHEMA_DIRNAME, modeline, write_schema_set

    target = _validated_target(resources_dir, filename)
    text = sample_text(kind, all_kinds=all_kinds)
    appended = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    if appended:
        existing = target.read_bytes()
        prefix = "" if existing.endswith(b"\n") or not existing else "\n"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(prefix)
            handle.write("\n")
            handle.write(text)
    else:
        header = modeline(manifest_path=target, resources_dir=resources_dir, kind=kind)
        target.write_text(f"{header}\n{text}", encoding="utf-8")
        write_schema_set(resources_dir / SCHEMA_DIRNAME)
    return target, appended


def _validated_kinds(kind: str | None, all_kinds: bool) -> tuple[str, ...]:
    if all_kinds and kind is not None:
        raise ValidationError(
            "pass a kind or --all, not both",
            hint="A kind prints one sample; --all prints every kind's.",
        )
    if all_kinds:
        return SAMPLE_KINDS
    if kind is None:
        known = ", ".join(SAMPLE_KINDS)
        raise ValidationError(
            "indicate a kind to sample, or pass --all",
            hint=f"Example: `agw resource sample secret`. Kinds: {known}.",
        )
    if kind not in SAMPLE_KINDS:
        known = ", ".join(SAMPLE_KINDS)
        handler = KIND_REGISTRY.get(kind)
        if handler is not None and handler.category == "capability":
            # Capability kinds (harness-integration, secret-backend, vm-platform,
            # git-credential-provider) are code-backed and carry no
            # manifest, so there is nothing to sample. `resource kinds`
            # lists them alongside the declarable kinds, so a curious
            # operator will ask for one here; name the kind and point at
            # the declarable set that does have samples, matching --all.
            raise ValidationError(
                f"{kind!r} is a capability kind; it has no sample manifest",
                entity_kind="resource",
                entity_name=kind,
                hint=f"declarable kinds: {known}",
            )
        raise ValidationError(
            f"unknown kind {kind!r}",
            entity_kind="resource",
            entity_name=kind,
            hint=f"known kinds: {known}",
        )
    return (kind,)


def _read_sample(kind: str) -> str:
    bundle = importlib_resources.files(_SAMPLES_PACKAGE) / _SAMPLES_DIR / f"{kind}.yaml"
    return bundle.read_text(encoding="utf-8")


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

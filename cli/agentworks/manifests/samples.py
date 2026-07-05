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
through the real loader.
"""

from __future__ import annotations

from importlib import resources as importlib_resources
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.manifests.decode import KIND_SECTIONS

if TYPE_CHECKING:
    from pathlib import Path

_SAMPLES_PACKAGE = "agentworks.manifests"
_SAMPLES_DIR = "samples"

# Every kind in the decoder's table is manifest-declarable and has a
# bundled sample; the samples-exist test pins this stays true as kinds
# are added.
SAMPLE_KINDS: tuple[str, ...] = tuple(KIND_SECTIONS)

_SUFFIXES = {".yaml", ".yml"}


def sample_text(kind: str | None = None) -> str:
    """The bundled sample for ``kind``, or all kinds concatenated."""
    kinds = _validated_kinds(kind)
    parts = [_read_sample(k) for k in kinds]
    return "\n".join(part.rstrip("\n") for part in parts) + "\n"


def write_sample(resources_dir: Path, filename: str, kind: str | None = None) -> tuple[Path, bool]:
    """Write (or append) the sample under the resources directory.

    Returns ``(path, appended)``. The content is fully commented, so no
    document separator is involved: appending comments to an existing
    manifest file cannot change what it declares.
    """
    target = _validated_target(resources_dir, filename)
    text = sample_text(kind)
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
        target.write_text(text, encoding="utf-8")
    return target, appended


def _validated_kinds(kind: str | None) -> tuple[str, ...]:
    if kind is None:
        return SAMPLE_KINDS
    if kind not in SAMPLE_KINDS:
        known = ", ".join(SAMPLE_KINDS)
        raise ValidationError(
            f"unknown kind {kind!r}", hint=f"known kinds: {known}"
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
            f"--write takes a path relative to the resources directory; "
            f"got {filename!r}",
            hint=f"Files land under {resources_dir}.",
        )
    if rel.suffix not in _SUFFIXES:
        raise ValidationError(
            f"--write requires a .yaml or .yml filename; got {filename!r}",
            hint="The manifest loader only reads *.yaml / *.yml files.",
        )
    return resources_dir / rel

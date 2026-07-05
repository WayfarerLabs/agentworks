"""Tests for the bundled sample manifests and ``agw resource sample``."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.manifests.decode import KIND_SECTIONS
from agentworks.manifests.loader import load_manifests
from agentworks.manifests.samples import (
    SAMPLE_KINDS,
    sample_text,
    write_sample,
)


def _uncomment(text: str) -> str:
    """The documented uncomment rule: strip one leading ``#`` per line.

    Document lines become YAML; ``## `` prose lines become ordinary
    YAML comments.
    """
    lines = []
    for line in text.splitlines():
        lines.append(line[1:] if line.startswith("#") else line)
    return "\n".join(lines) + "\n"


def test_every_kind_has_a_sample() -> None:
    assert set(SAMPLE_KINDS) == set(KIND_SECTIONS)
    for kind in SAMPLE_KINDS:
        assert sample_text(kind).strip()


def test_all_kinds_concatenation_and_unknown_kind() -> None:
    everything = sample_text()
    for kind in SAMPLE_KINDS:
        assert f"kind: {kind}" in everything
    with pytest.raises(ValidationError, match="unknown kind"):
        sample_text("nope")


def test_samples_are_fully_commented() -> None:
    """Every non-blank line starts with ``#`` -- written samples are inert."""
    for kind in SAMPLE_KINDS:
        for line in sample_text(kind).splitlines():
            assert not line or line.startswith("#"), (kind, line)


def test_uncommented_samples_load_through_the_real_loader(tmp_path: Path) -> None:
    """The teaching surface must be true: stripping one ``#`` per line
    yields documents the real loader accepts, for every kind."""
    resources = tmp_path / "resources"
    resources.mkdir()
    for kind in SAMPLE_KINDS:
        (resources / f"{kind}.yaml").write_text(_uncomment(sample_text(kind)))
    manifests = load_manifests(resources)
    loaded_kinds = {entry.kind for entry in manifests.entries}
    assert loaded_kinds == set(SAMPLE_KINDS)
    assert not manifests.issues, manifests.issues


def test_commented_samples_are_inert_through_the_loader(tmp_path: Path) -> None:
    """As shipped (commented), a written sample declares nothing."""
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "all.yaml").write_text(sample_text())
    manifests = load_manifests(resources)
    assert not manifests.entries
    assert not manifests.issues


def test_write_sample_creates_and_appends(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    path, appended = write_sample(resources, "kinds/secret.yaml", "secret")
    assert not appended
    assert path == resources / "kinds" / "secret.yaml"
    first = path.read_text()

    path2, appended2 = write_sample(resources, "kinds/secret.yaml", "vm-template")
    assert appended2
    assert path2 == path
    text = path.read_text()
    assert text.startswith(first)
    assert "kind: vm-template" in text
    # Still inert after the append.
    manifests = load_manifests(resources)
    assert not manifests.entries


def test_write_sample_refuses_escapes_and_suffixes(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    with pytest.raises(ValidationError, match="relative to the resources"):
        write_sample(resources, "/abs/path.yaml")
    with pytest.raises(ValidationError, match="relative to the resources"):
        write_sample(resources, "../escape.yaml")
    with pytest.raises(ValidationError, match=".yaml or .yml"):
        write_sample(resources, "samples.txt")

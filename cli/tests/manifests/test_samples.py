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
    everything = sample_text(all_kinds=True)
    for kind in SAMPLE_KINDS:
        # Every sample opens with its prose header line -- checked
        # explicitly (not by document substring) because secret-backend
        # is prose-only and has no document to match.
        assert f"## kind: {kind} --" in everything
    with pytest.raises(ValidationError, match="unknown kind"):
        sample_text("nope")


def test_bare_sample_requires_kind_or_all() -> None:
    """Mirrors `resource migrate`: dumping every kind is an explicit
    opt-in, and mixing a kind with --all is an error."""
    with pytest.raises(ValidationError, match="indicate a kind"):
        sample_text()
    with pytest.raises(ValidationError, match="not both"):
        sample_text("secret", all_kinds=True)


def test_samples_are_fully_commented() -> None:
    """Every non-blank line starts with ``#`` -- written samples are inert."""
    for kind in SAMPLE_KINDS:
        for line in sample_text(kind).splitlines():
            assert not line or line.startswith("#"), (kind, line)


def test_uncommented_samples_load_through_the_real_loader(tmp_path: Path) -> None:
    """The teaching surface must be true: stripping one ``#`` per line
    yields documents the real loader accepts. Carve-out (maintainer
    ruling, 2026-07-05): the secret-backend sample is prose-only until
    a config-bearing provider ships, so uncommenting it yields zero
    documents by design."""
    resources = tmp_path / "resources"
    resources.mkdir()
    for kind in SAMPLE_KINDS:
        (resources / f"{kind}.yaml").write_text(_uncomment(sample_text(kind)))
    manifests = load_manifests(resources)
    loaded_kinds = {entry.kind for entry in manifests.entries}
    assert loaded_kinds == set(SAMPLE_KINDS) - {"secret-backend"}
    assert not manifests.issues, manifests.issues


def test_uncommented_samples_build_a_registry(tmp_path: Path) -> None:
    """Beyond the loader: the ENTIRE uncommented sample set builds a
    full registry -- its cross-references (admin-template ->
    git-credential github, apt-package -> apt-source my-repo, secrets
    auto-declare) resolve at finalize. No exclusions: the prose-only
    secret-backend sample contributes zero documents by design."""
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config

    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""\
[operator]
ssh_public_key = "{pub.as_posix()}"
ssh_private_key = "{priv.as_posix()}"
"""
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    for kind in SAMPLE_KINDS:
        (resources / f"{kind}.yaml").write_text(_uncomment(sample_text(kind)))
    config = load_config(cfg, warn_issues=False)
    build_registry(config)


def test_secret_backend_sample_is_prose_only() -> None:
    """The secret-backend sample has NOTHING to uncomment (maintainer
    ruling, 2026-07-05): no declarable backend can exist until a
    config-bearing provider ships, so shipping an uncommentable
    document would teach a lie. Every line is prose (``## ``); the
    day a real provider lands, this test flips and the sample gains a
    real document."""
    text = sample_text("secret-backend")
    for line in text.splitlines():
        assert not line or line.startswith("##"), line


def test_commented_samples_are_inert_through_the_loader(tmp_path: Path) -> None:
    """As shipped (commented), a written sample declares nothing."""
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "all.yaml").write_text(sample_text(all_kinds=True))
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

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
    # secret-backend is a capability descriptor post-collapse
    # (2026-07-07): in KIND_SECTIONS for the migrator's drop table, not
    # declarable, no sample.
    assert "secret-backend" not in SAMPLE_KINDS
    assert set(SAMPLE_KINDS) | {"secret-backend"} == set(KIND_SECTIONS)
    for kind in SAMPLE_KINDS:
        assert sample_text(kind).strip()


def test_sample_kinds_are_exactly_the_registry_declarable_kinds() -> None:
    """SAMPLE_KINDS (the set `resource sample --all` emits and the guard
    treats as sampleable) is derived from the kind registry's per-kind
    category, the same source of truth the capability guard keys off.
    Pin that they stay identical so a future capability kind can't slip
    into the sampleable set (and make `--all` crash on a missing sample
    file), and a declarable kind can't fall out of it."""
    from agentworks.resources import KIND_REGISTRY

    declarable = {name for name, handler in KIND_REGISTRY.items() if handler.category == "declarable"}
    assert set(SAMPLE_KINDS) == declarable
    # No capability kind is ever sampleable.
    assert set(SAMPLE_KINDS).isdisjoint(_capability_kinds())


def _capability_kinds() -> list[str]:
    """The code-backed kinds that carry no manifest, straight from the
    registry so this list can't drift from the kind inventory."""
    from agentworks.resources import KIND_REGISTRY

    return [name for name, handler in KIND_REGISTRY.items() if handler.category == "capability"]


def test_secret_backend_has_no_sample() -> None:
    """The declarable secret-backend kind died in the Phase 5.5
    collapse; it survives only as a capability descriptor, so sampling
    it reports the capability-kind error rather than crashing."""
    with pytest.raises(ValidationError, match="capability kind"):
        sample_text("secret-backend")


@pytest.mark.parametrize("kind", _capability_kinds())
def test_capability_kinds_report_no_sample(kind: str) -> None:
    """Every capability kind `resource kinds` lists (harness,
    secret-backend, vm-platform, git-credential-provider) is a valid
    click.Choice value, so it reaches the service layer instead of
    dying as a raw parse error (issue #276). The service layer rejects
    it with a typed domain error that names the kind and lists the
    declarable kinds that do have samples."""
    with pytest.raises(ValidationError) as excinfo:
        sample_text(kind)
    err = excinfo.value
    assert kind in str(err)
    assert "capability kind" in str(err)
    assert "no sample manifest" in str(err)
    # The declarable set is offered as remediation, matching --all.
    assert err.hint is not None
    for declarable in SAMPLE_KINDS:
        assert declarable in err.hint


def test_all_kinds_concatenation_and_unknown_kind() -> None:
    everything = sample_text(all_kinds=True)
    for kind in SAMPLE_KINDS:
        # Every sample opens with its prose header line.
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
    secret-backend sample contributes zero documents by design.

    The azure, aws, and proxmox plugins are enabled here so the vm-site
    sample's `platform_config` blocks reach their platform's `validate`
    (a disabled platform's site never does), which is what makes the
    sample's field names and shapes actually checked rather than merely
    parsed as YAML."""
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

[plugins]
system = ["azure", "aws", "proxmox"]
"""
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    for kind in SAMPLE_KINDS:
        (resources / f"{kind}.yaml").write_text(_uncomment(sample_text(kind)))
    config = load_config(cfg, warn_issues=False)
    build_registry(config)


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


def test_sample_capability_kind_is_a_clean_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end error contract for issue #276: `resource sample
    harness` exits non-zero with a single clean `Error:` line, no
    traceback, and (being a domain error, not an unexpected failure)
    leaves error.log untouched. Regression guard against the raw
    click.Choice traceback that used to escape the top-level handler."""
    from agentworks import cli as cli_mod

    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "resource", "sample", "harness"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "'harness' is a capability kind; it has no sample manifest" in err
    assert "Traceback" not in err
    assert "StopIteration" not in err
    # Domain errors are clean-line, not logged: error.log must not appear.
    assert not (tmp_path / "logs" / "error.log").exists()


def test_write_sample_refuses_escapes_and_suffixes(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    with pytest.raises(ValidationError, match="relative to the resources"):
        write_sample(resources, "/abs/path.yaml")
    with pytest.raises(ValidationError, match="relative to the resources"):
        write_sample(resources, "../escape.yaml")
    with pytest.raises(ValidationError, match=".yaml or .yml"):
        write_sample(resources, "samples.txt")

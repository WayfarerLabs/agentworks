"""``agw resource sample``: the CLI contract, and the rendered output.

The CLI-contract tests here predate the renderer (kind selection, ``--all``,
the capability-kind refusal, ``--write``'s create-then-append behavior) and
are carried through the swap unchanged: what the command DOES did not
change, only where its text comes from.

What is new is what replaced the bundled-file pins. The old suite proved a
curated corpus was honest by stripping one ``#`` per line and loading it;
these prove the same property of GENERATED text, which is a stronger claim,
because a curated file could be fixed by hand and a renderer cannot.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.manifests.decode import KIND_SECTIONS
from agentworks.manifests.loader import load_manifests
from agentworks.manifests.samples import sample_text, write_sample
from agentworks.manifests.spec_model import declarable_kinds


def _uncomment(text: str) -> str:
    """The documented uncomment rule: strip one leading ``#`` per line.

    Document lines become YAML; ``##`` prose lines become ordinary YAML
    comments.
    """
    lines = [line[1:] if line.startswith("#") else line for line in text.splitlines()]
    return "\n".join(lines) + "\n"


def _capability_kinds() -> list[str]:
    """The code-backed kinds that carry no manifest, straight from the
    registry so this list can't drift from the kind inventory."""
    from agentworks.resources import KIND_REGISTRY

    return [name for name, handler in KIND_REGISTRY.items() if handler.category == "capability"]


# --- what gets sampled ------------------------------------------------


def test_every_declarable_kind_renders() -> None:
    # secret-backend is a capability descriptor post-collapse
    # (2026-07-07): in KIND_SECTIONS for the migrator's drop table, not
    # declarable, no sample.
    assert "secret-backend" not in declarable_kinds()
    assert set(declarable_kinds()) | {"secret-backend"} == set(KIND_SECTIONS)
    for kind in declarable_kinds():
        assert sample_text(kind).strip()


def test_no_capability_kind_is_sampleable() -> None:
    """A kind is sampleable exactly when a document of it can exist, which
    is the registry's own per-kind category. Pin that the two stay
    identical so a future capability kind cannot slip into the set and make
    ``--all`` fail on a kind with no document."""
    assert set(declarable_kinds()).isdisjoint(_capability_kinds())


def test_secret_backend_has_no_sample() -> None:
    """The declarable secret-backend kind died in the Phase 5.5 collapse;
    it survives only as a capability, so sampling it reports the
    capability-kind error rather than crashing."""
    with pytest.raises(ValidationError, match="capability kind"):
        sample_text("secret-backend")


@pytest.mark.parametrize("kind", _capability_kinds())
def test_capability_kinds_report_no_sample(kind: str) -> None:
    """Every capability kind `resource kinds` lists is a valid argument
    value, so it reaches the service layer instead of dying as a raw parse
    error (issue #276). The service layer rejects it with a typed domain
    error that names the kind, offers the declarable set, and (new with the
    renderer) points at the surface that DOES document a capability."""
    with pytest.raises(ValidationError) as excinfo:
        sample_text(kind)
    err = excinfo.value
    assert kind in str(err)
    assert "capability kind" in str(err)
    assert "no sample manifest" in str(err)
    assert err.hint is not None
    assert f"describe-kind {kind}" in err.hint
    for declarable in declarable_kinds():
        assert declarable in err.hint


def test_all_kinds_concatenation_and_unknown_kind() -> None:
    everything = sample_text(all_kinds=True)
    for kind in declarable_kinds():
        # Every sample opens with a semantic kind header.
        assert re.search(rf"^## kind: {re.escape(kind)}(?=[:\s])", everything, re.MULTILINE)
    with pytest.raises(ValidationError, match="unknown kind"):
        sample_text("nope")


def test_bare_sample_requires_kind_or_all() -> None:
    """Mirrors `resource migrate`: dumping every kind is an explicit
    opt-in, and mixing a kind with --all is an error."""
    with pytest.raises(ValidationError, match="indicate a kind"):
        sample_text()
    with pytest.raises(ValidationError, match="not both"):
        sample_text("secret", all_kinds=True)


# --- the rendered text is inert, and true -----------------------------


def test_samples_are_fully_commented() -> None:
    """Every non-blank line starts with ``#``: written samples are inert."""
    for kind in declarable_kinds():
        for line in sample_text(kind).splitlines():
            assert not line or line.startswith("#"), (kind, line)


def test_commented_samples_are_inert_through_the_loader(tmp_path: Path) -> None:
    """As rendered, a written sample declares nothing."""
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "all.yaml").write_text(sample_text(all_kinds=True))
    manifests = load_manifests(resources)
    assert not manifests.entries
    assert not manifests.issues


def test_uncommented_samples_load_through_the_real_loader(tmp_path: Path) -> None:
    """The teaching surface must be true: stripping one ``#`` per line
    yields documents the real loader accepts.

    This is what makes "only required fields are live lines" a property
    rather than a preference. A renderer that emitted an optional field as
    a document line would have to invent a value for it, and this test is
    where an invented value fails.
    """
    resources = tmp_path / "resources"
    resources.mkdir()
    for kind in declarable_kinds():
        (resources / f"{kind}.yaml").write_text(_uncomment(sample_text(kind)))
    manifests = load_manifests(resources)

    assert {entry.kind for entry in manifests.entries} == set(declarable_kinds())
    assert not manifests.issues, manifests.issues


def test_the_whole_set_uncomments_as_one_file(tmp_path: Path) -> None:
    """``--all`` is a real multi-document file, not a pile of documents
    YAML reads as one: the renderer separates them with a commented
    ``---``, which the same one-``#`` strip turns into a separator. The
    per-file corpus this replaces never proved that."""
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "everything.yaml").write_text(_uncomment(sample_text(all_kinds=True)))
    manifests = load_manifests(resources)

    assert {entry.kind for entry in manifests.entries} == set(declarable_kinds())
    assert not manifests.issues, manifests.issues


def test_uncommented_samples_build_a_registry(tmp_path: Path) -> None:
    """Beyond the loader: the whole uncommented set builds a full registry,
    so every reference a rendered document makes resolves at finalize and
    every capability config block validates against its own model.

    The azure, aws, and proxmox plugins are enabled so a vm-site sample's
    platform block reaches its platform's validation (a disabled platform's
    site never does), which is what makes the rendered field names and
    shapes actually checked rather than merely parsed as YAML.
    """
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
    for kind in declarable_kinds():
        (resources / f"{kind}.yaml").write_text(_uncomment(sample_text(kind)))
    config = load_config(cfg, warn_issues=False)
    build_registry(config)


# --- --write ----------------------------------------------------------


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
    harness-integration` exits non-zero with a single clean `Error:` line, no
    traceback, and (being a domain error, not an unexpected failure)
    leaves error.log untouched. Regression guard against the raw
    click.Choice traceback that used to escape the top-level handler."""
    from agentworks import cli as cli_mod

    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "resource", "sample", "harness-integration"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "'harness-integration' is a capability kind; it has no sample manifest" in err
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

"""Tests for ``declared_at: SourceLocation`` capture on declared Resources.

config.toml is settings only now (ADR 0022): every operator-declared Resource
lives in a ``resources/*.yaml`` manifest, and its ``declared_at`` points at the
opening line of its YAML document (``file:line``), captured at manifest decode.
The former per-kind TOML-section line pinning is gone with the TOML resource
surface; the manifest ``declared_at`` is uniform across kinds (each decoder
stamps ``doc.location``), which the parametrized test below pins per kind.

The settings-side ``[secret_config]`` table stays in config.toml, so its
``declared_at`` is still captured from the TOML section header, and a config
that omits it still carries the ``line=0`` sentinel so downstream framework
code never faces a missing field.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from tests.conftest import ManifestDoc, write_manifests


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id_ed25519.pub"
    priv = tmp_path / "id_ed25519"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    return pub, priv


def _write_config(tmp_path: Path, body: str, ssh_keys: tuple[Path, Path]) -> Path:
    """Write a settings-only config.toml (``[operator]`` plus optional
    settings ``body``) and return its path. Resources go in manifests."""
    pub, priv = ssh_keys
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub.as_posix()}"
            ssh_private_key = "{priv.as_posix()}"

            """
        )
        + dedent(body)
    )
    return config_file


# ---------------------------------------------------------------------------
# Manifest-declared Resources carry a real declared_at (file:line)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "name", "spec"),
    [
        ("vm-template", "azure-prod", {"cpus": 4, "env": {"FOO": "bar"}}),
        ("admin-template", "default", {"shell": "zsh", "env": {"FOO": "bar"}}),
        ("named-console-template", "default", {"tmux_layout": "tiled"}),
        ("git-credential", "github-prod", {"provider": {"name": "github"}}),
        ("secret", "anthropic-api-key", {}),
        ("session-template", "dev", {"harness_integration": {"name": "shell", "command": "claude"}}),
        ("workspace-template", "gruntweave", {"repo": "https://example.com/org/repo.git"}),
        ("agent-template", "claude", {"shell": "zsh"}),
    ],
)
def test_manifest_resource_declared_at_points_at_source(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    kind: str,
    name: str,
    spec: dict[str, object],
) -> None:
    """Every declarable kind's manifest Resource carries a ``declared_at``
    pointing at its declaring YAML document (the ``resources/*.yaml`` file, at
    the document's opening line), not a synthesized sentinel. This is the
    manifest equivalent of the former per-kind TOML-section line capture, and
    it pins that each decoder stamps ``doc.location`` onto the Resource.
    """
    config_file = _write_config(tmp_path, "", ssh_keys)
    # ``secret`` requires a description; it is optional for the other kinds.
    description = "declared for the declared_at test" if kind == "secret" else None
    write_manifests(tmp_path, ManifestDoc(kind, name, spec, description=description))

    registry = build_registry(load_config(config_file, warn_issues=False))
    decl = registry.lookup(kind, name)
    # A single-document manifest: the document opens on line 1 of the file the
    # framework auto-loads operator manifests from.
    assert decl.declared_at.file.name == "resources.yaml"
    assert decl.declared_at.line == 1


# The former ``test_vm_template_declared_at_uses_subsection_when_only_env_present``
# was removed here: it pinned the TOML loader's implicit-parent behavior (writing
# only ``[vm_templates.x.env]`` produces ``vm_templates.x`` whose declared_at
# points at the env subsection header). A YAML manifest has no sub-section shape,
# so that TOML-loader-internal detail has no manifest equivalent (ADR 0022).


# ---------------------------------------------------------------------------
# [secret_config] stays a settings-side table: TOML header capture + sentinel
# ---------------------------------------------------------------------------


def test_secret_config_declared_at(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    config_file = _write_config(
        tmp_path,
        """\
        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )

    cfg = load_config(config_file, warn_issues=False)
    assert cfg.secret_config_data.declared_at.file == config_file
    assert cfg.secret_config_data.declared_at.line == 5


def test_secret_config_synthesized_when_omitted(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    config_file = _write_config(tmp_path, "", ssh_keys)

    cfg = load_config(config_file, warn_issues=False)
    assert cfg.secret_config_data.declared_at.file == config_file
    assert cfg.secret_config_data.declared_at.line == 0


# The former ``test_admin_config_synthesized_when_section_omitted`` and
# ``test_named_console_synthesized_when_omitted`` were removed here: both read
# the retired ``Config.admin`` / ``Config.named_console`` fields to assert the
# TOML loader published no synthesized placeholder for an omitted singleton.
# config.toml no longer carries any resource surface (ADR 0022), so those fields
# are gone; the omitted-singleton-auto-declares behavior is covered on the
# registry side by ``test_config.py::test_named_console_tmux_layout_default_when_section_missing``.

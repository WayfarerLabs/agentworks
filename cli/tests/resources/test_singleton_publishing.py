"""Tests for the singleton-backed kinds (``admin-template``,
``named-console-template``) ending up in the Registry as one-row entries
regardless of whether the operator declared the singleton's sections.

The Config layer always produces an instance (real-content if the operator
declared sections, empty-defaults otherwise); ``Config.publish_to`` always
publishes it; ``build_registry`` makes the result observable as a
finalized Registry entry.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.resources import ALWAYS_MATERIALIZE_SOURCE
from agentworks.vms.admin import AdminConfig
from tests.conftest import ManifestDoc, write_manifests


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    return pub, priv


def _write_cfg(tmp_path: Path, body: str, ssh_keys: tuple[Path, Path]) -> Path:
    pub, priv = ssh_keys
    p = tmp_path / "c.toml"
    p.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            """
        )
        + dedent(body)
    )
    return p


def test_admin_template_default_present_when_no_admin_sections(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    """The loader path end to end: no admin declaration anywhere, and the
    registry still holds exactly one ``admin-template:default`` carrying
    the framework's own provenance.

    Everything the row IS, asserted here rather than spread over three
    files that each built this same config: the type, the name, the
    auto-declared variant, the always-materialize source, and the count.
    """
    cfg = _write_cfg(tmp_path, "", ssh_keys)
    r = build_registry(load_config(cfg, warn_issues=False))

    admin = r.lookup("admin-template", "default")
    assert isinstance(admin, AdminConfig)
    assert admin.name == "default"
    assert admin.origin is not None
    # No [admin.*] sections -> nothing published from TOML; the
    # framework's always-materialize pre-step auto-declares the default,
    # exactly like vm-template/agent-template.
    assert admin.origin.variant == "auto-declared"
    assert admin.origin.source == ALWAYS_MATERIALIZE_SOURCE
    # And only ONE entry under admin-template kind.
    assert len(list(r.iter_kind("admin-template"))) == 1


def test_admin_template_default_present_with_admin_env_only(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    """An admin-template:default manifest carrying only an env block still
    yields one admin-template:default with env populated and an
    operator-declared origin pointing at the manifest document.
    """
    cfg = _write_cfg(tmp_path, "", ssh_keys)
    write_manifests(tmp_path, ManifestDoc("admin-template", "default", {"env": {"FOO": "bar"}}))
    r = build_registry(load_config(cfg, warn_issues=False))

    admin = r.lookup("admin-template", "default")
    assert admin.origin is not None
    assert admin.origin.variant == "operator-declared"
    # The origin points at the manifest document (config.toml no longer
    # declares resources, ADR 0022; there is no TOML header to resolve).
    assert admin.origin.file is not None
    assert admin.origin.file.name == "resources.yaml"
    # And the env block was carried through composition.
    assert admin.env["FOO"].value == "bar"


def test_named_console_template_default_always_present(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    cfg = _write_cfg(tmp_path, "", ssh_keys)
    r = build_registry(load_config(cfg, warn_issues=False))

    nc = r.lookup("named-console-template", "default")
    assert nc.origin is not None
    assert nc.origin.variant == "auto-declared"
    assert len(list(r.iter_kind("named-console-template"))) == 1


def test_named_console_template_default_with_real_section(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    cfg = _write_cfg(tmp_path, "", ssh_keys)
    write_manifests(tmp_path, ManifestDoc("named-console-template", "default", {"tmux_layout": "tiled"}))
    r = build_registry(load_config(cfg, warn_issues=False))

    nc = r.lookup("named-console-template", "default")
    assert nc.origin is not None
    assert nc.origin.file is not None
    assert nc.origin.file.name == "resources.yaml"
    assert nc.tmux_layout == "tiled"


def test_manifest_declared_default_needs_no_exemption(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> None:
    """The scenario the old synthesized-singleton collision exemption
    existed for: a manifest declares admin-template/default and the TOML
    has no [admin.*] sections. With no placeholder published, the
    manifest row is simply the only declaration -- operator-declared,
    exactly one row, no exemption machinery."""
    cfg = _write_cfg(tmp_path, "", ssh_keys)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "admin.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: admin-template
        metadata:
          name: default
        spec:
          shell: zsh
        """)
    )
    r = build_registry(load_config(cfg, warn_issues=False))
    admin = r.lookup("admin-template", "default")
    assert admin.origin is not None
    assert admin.origin.variant == "operator-declared"
    assert admin.origin.file == resources / "admin.yaml"
    assert admin.shell == "zsh"
    assert len(list(r.iter_kind("admin-template"))) == 1

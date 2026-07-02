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


def test_admin_template_default_present_when_no_admin_sections(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(tmp_path, "", ssh_keys)
    r = build_registry(load_config(cfg, warn_issues=False))

    admin = r.lookup("admin-template", "default")
    assert admin.origin is not None
    assert admin.origin.variant == "operator-declared"
    # The line is 0 (sentinel for synthesized-because-omitted), but file is
    # the real config path so error rendering can still cite the config.
    assert admin.origin.file == cfg
    assert admin.origin.line == 0
    # And only ONE entry under admin-template kind.
    assert len(list(r.iter_kind("admin-template"))) == 1


def test_admin_template_default_present_with_admin_env_only(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """The implicit-parent case at the singleton scope: ``[admin.env]``
    alone still yields one admin-template:default with env populated.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [admin.env]
        FOO = "bar"
        """,
        ssh_keys,
    )
    r = build_registry(load_config(cfg, warn_issues=False))

    admin = r.lookup("admin-template", "default")
    assert admin.origin is not None
    assert admin.origin.variant == "operator-declared"
    # declared_at picks the earliest [admin.*] header, which is [admin.env].
    assert admin.origin.line == 5  # operator header + blank + admin.env header
    # And the env block was carried through composition.
    assert admin.env["FOO"].value == "bar"


def test_named_console_template_default_always_present(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(tmp_path, "", ssh_keys)
    r = build_registry(load_config(cfg, warn_issues=False))

    nc = r.lookup("named-console-template", "default")
    assert nc.origin is not None
    assert nc.origin.variant == "operator-declared"
    assert nc.origin.file == cfg
    assert nc.origin.line == 0
    assert len(list(r.iter_kind("named-console-template"))) == 1


def test_named_console_template_default_with_real_section(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(
        tmp_path,
        """\
        [named_console]
        tmux_layout = "tiled"
        """,
        ssh_keys,
    )
    r = build_registry(load_config(cfg, warn_issues=False))

    nc = r.lookup("named-console-template", "default")
    assert nc.origin is not None
    assert nc.origin.line == 5  # [named_console] header line
    assert nc.tmux_layout == "tiled"

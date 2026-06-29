"""Tests for Phase 2b's catalog kinds: ``apt_package``,
``system_install_command``, ``user_install_command``.

Coverage:

- Each kind's shape (``miss_policy == "error"``, no auto-declare names).
- The error miss policy fires with the requirement's source on typo'd
  references from operator config.
- Known catalog names resolve.
- ``synthesize`` raises ``NoUnreferencedDefaultError`` per Phase 2a's
  empty-requirements contract (the kinds never auto-declare; the
  contract is still defined).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.resources import KIND_REGISTRY, NoUnreferencedDefaultError

CATALOG_KINDS = ("apt_package", "system_install_command", "user_install_command")


def _write_cfg(path: Path, body: str = "") -> Path:
    pub = path.parent / "id.pub"
    priv = path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(body),
    )
    return path


# -- Kind shape -------------------------------------------------------------


@pytest.mark.parametrize("kind_name", CATALOG_KINDS)
def test_catalog_kind_attributes(kind_name: str) -> None:
    kind = KIND_REGISTRY[kind_name]
    assert kind.kind == kind_name
    assert kind.miss_policy == "error"
    assert kind.auto_declare_names is None


@pytest.mark.parametrize("kind_name", CATALOG_KINDS)
def test_catalog_kind_synthesize_raises(kind_name: str) -> None:
    """Catalog kinds have ``miss_policy == "error"``; ``synthesize`` is
    never called by the framework in practice but the empty-requirements
    contract still applies (Phase 2a). Raises ``NoUnreferencedDefaultError``.
    """
    kind = KIND_REGISTRY[kind_name]
    with pytest.raises(NoUnreferencedDefaultError):
        kind.synthesize(())


# -- Framework miss-policy on typo'd references ---------------------------


def test_apt_package_typo_errors_with_source(tmp_path: Path) -> None:
    """A typo in ``vm_templates.*.apt_packages`` errors at
    ``build_registry`` time with the framework's miss-policy shape, citing
    the referencing template's ``(kind, name)`` source.
    """
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [vm_templates.default]
            apt_packages = ["nonexistent-pkg"]
            """,
        ),
        warn_issues=False,
    )
    with pytest.raises(ConfigError, match=r"references unknown apt_package 'nonexistent-pkg'"):
        build_registry(cfg)


def test_system_install_command_typo_errors_with_source(tmp_path: Path) -> None:
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [vm_templates.default]
            system_install_commands = ["totally-fake-cmd"]
            """,
        ),
        warn_issues=False,
    )
    with pytest.raises(ConfigError, match="references unknown system_install_command"):
        build_registry(cfg)


def test_user_install_command_typo_in_admin_errors(tmp_path: Path) -> None:
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [admin.config]
            user_install_commands = ["bogus-installer"]
            """,
        ),
        warn_issues=False,
    )
    with pytest.raises(ConfigError, match="references unknown user_install_command"):
        build_registry(cfg)


def test_user_install_command_typo_in_agent_errors(tmp_path: Path) -> None:
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [agent_templates.default]
            user_install_commands = ["bogus-installer"]
            """,
        ),
        warn_issues=False,
    )
    with pytest.raises(ConfigError, match="references unknown user_install_command"):
        build_registry(cfg)


# -- Known references resolve ----------------------------------------------


def test_known_apt_package_reference_resolves(tmp_path: Path) -> None:
    """A reference to a known built-in catalog entry (``gh`` in the
    built-in catalog today) finalizes cleanly.
    """
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [vm_templates.default]
            apt_packages = ["gh"]
            """,
        ),
        warn_issues=False,
    )
    registry = build_registry(cfg)
    gh = registry.lookup("apt_package", "gh")
    assert gh.name == "gh"
    # Cross-check: the catalog publisher attached code-declared origin
    # and the framework's finalize attached usage from the
    # vm_template:default reference.
    assert gh.origin.variant == "code-declared"
    assert any(
        u.source == ("vm_template", "default") for u in gh.usage
    ), "vm_template:default usage should be on the apt_package"

"""Tests for the apt / install-command kinds: ``apt-source``, ``apt-package``,
``system-install-command``, ``user-install-command``.

Coverage:

- Each kind's shape (``miss_policy == "error"``, no auto-declare names).
- The error miss policy fires with the reference's source on typo'd
  references from operator config.
- Known apt / install-command entries resolve.
- ``synthesize`` raises ``NoUnreferencedDefaultError`` per Phase 2a's
  empty-references contract (the kinds never auto-declare; the
  contract is still defined).
- ``apt-package -> apt-source`` edges: an unknown source name in a
  package's ``apt_sources`` field surfaces via the framework's miss
  policy; a known source shows up in the ``apt-source`` Resource's
  inbound ``references`` after finalize.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.resources import KIND_REGISTRY, NoUnreferencedDefaultError

APT_AND_INSTALL_KINDS = (
    "apt-source",
    "apt-package",
    "system-install-command",
    "user-install-command",
)


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


@pytest.mark.parametrize("kind_name", APT_AND_INSTALL_KINDS)
def test_install_resource_kind_attributes(kind_name: str) -> None:
    kind = KIND_REGISTRY[kind_name]
    assert kind.kind == kind_name
    assert kind.miss_policy == "error"
    assert kind.auto_declare_names is None


@pytest.mark.parametrize("kind_name", APT_AND_INSTALL_KINDS)
def test_install_resource_kind_synthesize_raises(kind_name: str) -> None:
    """These kinds have ``miss_policy == "error"``; ``synthesize`` is
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
    with pytest.raises(ConfigError, match=r"references unknown apt-package 'nonexistent-pkg'"):
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
    with pytest.raises(ConfigError, match="references unknown system-install-command"):
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
    with pytest.raises(ConfigError, match="references unknown user-install-command"):
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
    with pytest.raises(ConfigError, match="references unknown user-install-command"):
        build_registry(cfg)


# -- Known references resolve ----------------------------------------------


def test_known_apt_package_reference_resolves(tmp_path: Path) -> None:
    """A reference to a known built-in entry (``gh`` among the built-in
    apt-package entries today) finalizes cleanly.
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
    gh = registry.lookup("apt-package", "gh")
    assert gh.name == "gh"
    # Cross-check: the bundled-manifest publisher attached built-in origin
    # and the framework's finalize attached the inbound reference from
    # vm-template:default.
    assert gh.origin.variant == "built-in"
    assert any(
        u.source == ("vm-template", "default") for u in gh.references
    ), "vm-template:default reference should be on the apt_package"


# -- apt-package -> apt-source edges ---------------------------------------


def test_apt_source_kind_published_from_builtin_manifest(tmp_path: Path) -> None:
    """The bundled-manifest publisher emits ``apt-source`` Resources with
    ``built-in`` origin, parallel to ``apt-package`` / the
    install-command kinds. The built-in manifests ship at least one
    apt-source (``github`` today), so the registry has it after
    ``build_registry``.
    """
    cfg = load_config(
        _write_cfg(tmp_path / "config.toml"),
        warn_issues=False,
    )
    registry = build_registry(cfg)
    names = [name for name, _ in registry.iter_kind_items("apt-source")]
    assert names, "the built-in manifests should publish at least one apt_source"
    for name in names:
        src = registry.lookup("apt-source", name)
        assert src.origin.variant == "built-in"


def test_apt_package_references_flow_to_apt_source(tmp_path: Path) -> None:
    """``AptPackageEntry.referenced_resources()`` emits one
    ``ResourceReference(kind="apt-source", ...)`` per name in the
    package's ``apt_sources`` field. After finalize, the apt-source's
    ``references`` collection includes the referencing apt-package --
    the dependency graph that was previously implicit in
    ``_validate_references`` is now visible via
    ``agw resource describe apt-source/<name>``.
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
    # ``gh`` depends on the ``github-cli`` apt-source among the built-in
    # entries; check the inbound edge lands on the source.
    github = registry.lookup("apt-source", "github-cli")
    referencing_pkgs = [
        entry.source for entry in github.references
        if entry.source[0] == "apt-package"
    ]
    assert ("apt-package", "gh") in referencing_pkgs, (
        f"expected apt_package:gh to reference apt_source:github-cli; got "
        f"{referencing_pkgs}"
    )


def test_unknown_apt_source_reference_errors_via_framework(
    tmp_path: Path,
) -> None:
    """An apt_package pointing at a non-existent apt_source used to
    error at catalog load via ``_validate_references``. That validator
    was retired; the framework's ``AptSourceKind.miss_policy = "error"``
    now catches it at ``build_registry`` time. The error message names
    the missing source; the offending package's identity flows through
    the ``ResourceReference.source`` field on the reference.
    """
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [apt_packages.bad-pkg]
            description = "Package with an unknown source"
            apt = ["bad"]
            apt_sources = ["nonexistent-source"]
            """,
        ),
        warn_issues=False,
    )
    with pytest.raises(ConfigError, match="nonexistent-source"):
        build_registry(cfg)


def test_operator_declared_apt_source_layers_over_builtin(
    tmp_path: Path,
) -> None:
    """Operator-declared ``[apt_sources.<name>]`` in config.toml is
    parsed and published by ``apt.publish_to`` with ``operator-declared``
    origin. Publish order (the bundled manifests first, then
    ``apt.publish_to``) plus the kind's ``builtin_override = "allow"``
    policy is what lets the operator's declaration override the built-in
    for the same name. The same layering pattern that already covers
    apt_packages.
    """
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [apt_sources.custom-src]
            description = "Operator-defined source"
            key_url = "https://example.com/key.gpg"
            key_path = "/etc/apt/keyrings/custom-src.gpg"
            source = "deb [signed-by=/etc/apt/keyrings/custom-src.gpg] https://example.com/apt stable main"
            source_file = "custom-src.list"

            [apt_packages.custom-pkg]
            description = "Package using the operator source"
            apt = ["custom-pkg"]
            apt_sources = ["custom-src"]
            """,
        ),
        warn_issues=False,
    )
    registry = build_registry(cfg)

    custom_src = registry.lookup("apt-source", "custom-src")
    assert custom_src.origin.variant == "operator-declared"
    assert custom_src.name == "custom-src"
    # The referencing package shows up on the source's inbound edges.
    assert any(
        entry.source == ("apt-package", "custom-pkg")
        for entry in custom_src.references
    )

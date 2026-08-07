"""What a manifest document decodes to, and which spellings it refuses.

The spec shapes that are easy to get subtly wrong across kinds: the
git-credential provider vocabulary, the flattened admin-template spec, a
metadata field written inside ``spec``, the capability-config caps, and
the description handling every declarable kind shares. Per-kind field
coverage lives in the ``test_spec_*`` modules; this one is the cross-kind
decode behavior.

This file used to hold a parity suite comparing each shape against the
same resource declared as flat TOML, read by the migrator's frozen
pre-side oracle. Both sides of that comparison are gone with the migrator
(operator ruling, 2026-08-07): config.toml declares no resources, so
there is no pre-side left to compare against.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.manifests import ManifestSet, load_manifests

_BASE_TOML = """
[operator]
ssh_public_key = "{pub}"
ssh_private_key = "{priv}"
"""


def _config(tmp_path: Path, body: str = "") -> Any:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_BASE_TOML.format(pub=tmp_path / "k.pub", priv=tmp_path / "k") + dedent(body))
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA test")
    (tmp_path / "k").write_text("key")
    return load_config(cfg, warn_issues=False)


def _manifest(tmp_path: Path, text: str, rel: str = "res.yaml") -> None:
    path = tmp_path / "resources" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text))


def test_git_credential_type_key_rejected(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: github
        spec:
          type: github
        """,
    )
    # The hand-written "use provider, not type" steer is gone: the
    # unknown-key message names the one field this kind has, which says
    # the same thing without a second place to keep it.
    with pytest.raises(ConfigError, match="type: unknown field; expected one of: provider"):
        load_manifests(tmp_path / "resources")


def test_a_kind_owned_key_inside_the_provider_block_is_the_providers_to_refuse(
    tmp_path: Path,
) -> None:
    """decode used to guard this, because under the sibling shape a
    ``provider`` key inside ``provider_config`` could silently re-pick the
    capability. It cannot under one tagged table: ``name`` is the selector
    and it is a real field of the block, so a stray ``provider`` key is
    just config the provider does not accept, and the provider's own model
    is what says so at finalize."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: gh
        spec:
          provider:
            name: github
            provider: sneaky
        """,
    )
    manifests = load_manifests(tmp_path / "resources")
    with pytest.raises(ConfigError, match="provider: unknown field"):
        build_registry(_config(tmp_path), manifests)


def test_git_credential_token_in_provider_config(tmp_path: Path) -> None:
    """token lives inside the tagged provider table now; a top-level
    spec.token is rejected as an unknown field."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: gh
        spec:
          provider:
            name: github
          token: at-top-level
        """,
    )
    with pytest.raises(ConfigError, match="into\\s+the spec.provider table"):
        load_manifests(tmp_path / "resources")


def test_provider_must_be_a_tagged_table(tmp_path: Path) -> None:
    """A scalar that is not even a capability name (so not the retired
    string shape) is rejected as the wrong TYPE for the tagged table."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: gh
        spec:
          provider: 42
        """,
    )
    with pytest.raises(ConfigError, match="provider: must be a table"):
        load_manifests(tmp_path / "resources")


def test_git_credential_org_must_nest_under_provider_config(tmp_path: Path) -> None:
    """Provider-owned fields do not ride the spec top level in YAML: a
    stray ``org`` is an unknown key on a kind whose only field is
    ``provider``, which is the pointer."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: ado
        spec:
          provider:
            name: azdo
          org: my-org
        """,
    )
    with pytest.raises(ConfigError, match="org: unknown field; expected one of: provider"):
        load_manifests(tmp_path / "resources")


@pytest.mark.parametrize("field", ["description", "name"])
def test_a_metadata_field_written_in_spec_is_refused(tmp_path: Path, field: str) -> None:
    """The metadata fields ARE fields of the row, so ``extra="forbid"``
    would accept one written in ``spec`` and let it silently override the
    envelope. Parametrized over two of them because the guard is derived
    from the row base rather than naming ``description`` alone."""
    _manifest(
        tmp_path,
        f"""
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: s1
          description: d
        spec:
          {field}: also here
        """,
    )
    with pytest.raises(ConfigError, match=f"{field} belong\\(s\\) in metadata, not in spec"):
        load_manifests(tmp_path / "resources")


def test_secret_over_username_cap_decodes_and_registers(tmp_path: Path) -> None:
    """Issue #275: a >30 secret name decodes and lands in the registry; the
    raised cap applies to the secret kind."""
    long_name = "git-token-github-fg-wf-agw-tester"  # 33 chars
    assert len(long_name) > 30
    _manifest(
        tmp_path,
        f"""
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: {long_name}
          description: d
        spec: {{}}
        """,
    )
    manifests = load_manifests(tmp_path / "resources")
    assert [e.name for e in manifests.entries] == [long_name]
    registry = build_registry(_config(tmp_path))
    assert registry.lookup("secret", long_name).name == long_name


def test_vm_site_uses_freeform_cap(tmp_path: Path) -> None:
    """vm-site names hit no OS identifier limit (registry key + display only;
    they are NOT derived into hostnames or SSH aliases, VM names are), so they
    use the freeform cap (64), not the tighter VM-name cap. A 40-char name that
    the old 30-char cap rejected now decodes and registers."""
    from agentworks.naming import MAX_FREEFORM_NAME_LENGTH

    name = "a" * 40
    assert MAX_FREEFORM_NAME_LENGTH == 64 and len(name) > 30
    _manifest(
        tmp_path,
        f"""
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: {name}
        spec:
          platform:
            name: lima
        """,
    )
    manifests = load_manifests(tmp_path / "resources")
    assert [e.name for e in manifests.entries] == [name]


def test_vm_site_over_freeform_cap_rejected(tmp_path: Path) -> None:
    """A vm-site name past the freeform cap (64) is still rejected."""
    from agentworks.naming import MAX_FREEFORM_NAME_LENGTH

    _manifest(
        tmp_path,
        f"""
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: {"a" * (MAX_FREEFORM_NAME_LENGTH + 1)}
        spec:
          platform:
            name: lima
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_manifests(tmp_path / "resources")
    assert "is too long" in str(exc.value)
    assert f"max {MAX_FREEFORM_NAME_LENGTH}" in str(exc.value)


def test_description_stored_for_template_kind_without_warning(tmp_path: Path) -> None:
    """The formerly template-shaped kinds now store metadata.description
    like every other declarable kind: it round-trips onto the Resource
    and the retired "not yet stored" warning does not fire."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: vm-template
        metadata:
          name: dev
          description: a dev box
        spec: {}
        """,
    )
    manifests = load_manifests(tmp_path / "resources")
    assert not manifests.issues
    registry = build_registry(_config(tmp_path))
    assert registry.lookup("vm-template", "dev").description == "a dev box"


@pytest.mark.parametrize(
    ("kind", "spec_body"),
    [
        ("vm-template", "spec: {}"),
        ("agent-template", "spec: {}"),
        ("workspace-template", "spec: {}"),
        ("admin-template", "spec: {}"),
        ("named-console-template", "spec: {}"),
    ],
)
def test_description_never_warns_for_declarable_kind(tmp_path: Path, kind: str, spec_body: str) -> None:
    """No declarable kind emits the retired "not yet stored" warning:
    description is framework-uniform, so every kind stores it."""
    _manifest(
        tmp_path,
        f"""
        apiVersion: agentworks/v1
        kind: {kind}
        metadata:
          name: default
          description: uniform description
        {spec_body}
        """,
    )
    manifests = load_manifests(tmp_path / "resources")
    assert not manifests.issues
    assert manifests.entries[0].resource.description == "uniform description"


def test_install_command_kind_decode_error_carries_location(tmp_path: Path) -> None:
    """The install-command loader raises ConfigError on a bad spec; from a
    manifest it must surface with the document's file:line."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: system-install-command
        metadata:
          name: my-tool
        spec:
          command: install.sh
          test: my-tool
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_manifests(tmp_path / "resources")
    assert "res.yaml:2" in str(exc.value)
    assert "test" in str(exc.value)


def test_manifest_admin_default_is_only_row_when_toml_omits(tmp_path: Path) -> None:
    """A manifest-declared admin-template/default with no [admin.*] TOML
    sections is simply the only declaration: the TOML publisher no longer
    publishes placeholder rows for omitted sections, so no collision
    handling is involved."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: admin-template
        metadata:
          name: default
        spec:
          username: ops
        """,
    )
    registry = build_registry(_config(tmp_path))
    assert registry.lookup("admin-template", "default").username == "ops"


def test_manifest_named_admin_template_carries_its_name(tmp_path: Path) -> None:
    """A non-default admin-template manifest now decodes to an AdminConfig
    whose ``name`` is the document's ``metadata.name`` (previously the
    envelope rejected any name but ``default``). It coexists with the
    always-materialized ``default`` row."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: admin-template
        metadata:
          name: work
        spec:
          username: worker
        """,
    )
    registry = build_registry(_config(tmp_path))
    work = registry.lookup("admin-template", "work")
    assert work.name == "work"
    assert work.username == "worker"
    # The reserved default still materializes alongside the named row.
    assert registry.lookup("admin-template", "default").name == "default"


# The dual-window cross-source duplicate tests (a TOML [admin.config] /
# [apt_packages.*] / [secrets.*] colliding with a manifest of the same
# kind+name) were removed here: config.toml can no longer declare
# resources (ADR 0022), so a TOML-vs-manifest collision cannot occur. The
# surviving manifest-vs-manifest duplicate detection is covered in
# tests/manifests/test_loader_and_envelope.py (test_duplicate_across_files
# _cites_both_locations, test_duplicate_within_one_file_errors).


def test_manifest_overrides_builtin_apt_entry(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: apt-package
        metadata:
          name: gh
          description: overridden gh
        spec:
          apt: [gh-custom]
        """,
    )
    registry = build_registry(_config(tmp_path))
    row = registry.lookup("apt-package", "gh")
    assert row.apt == ["gh-custom"]
    assert row.origin.variant == "operator-declared"


def test_bootstrap_autoload_and_explicit_empty(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: from-manifest
          description: d
        spec: {}
        """,
    )
    auto = build_registry(config)
    assert auto.lookup("secret", "from-manifest").origin.variant == "operator-declared"

    explicit_empty = build_registry(config, ManifestSet.empty())
    with pytest.raises(KeyError):
        explicit_empty.lookup("secret", "from-manifest")

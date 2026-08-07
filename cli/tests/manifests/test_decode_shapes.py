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


def test_a_stray_top_level_key_names_the_one_field_this_kind_has(tmp_path: Path) -> None:
    """Both mistakes an operator brings from the flat TOML shape land on
    the same message, and that IS the design: the hand-written "use
    provider, not type" steer is gone, and provider-owned fields like
    ``org`` never rode the spec top level in YAML. Naming the kind's one
    field says both things without a second place to keep either.

    One loop, because landing on the same message is the claim: the
    retired selector and a provider-owned field, side by side.

    (``token`` is the exception that keeps its own steer, because the
    field list alone does not say WHERE the token goes; see
    ``test_spec_hosts.py::test_a_top_level_token_keeps_its_steer``.)
    """
    for key, value in (("type", "github"), ("org", "my-org")):
        _manifest(
            tmp_path,
            f"""
            apiVersion: agentworks/v1
            kind: git-credential
            metadata:
              name: github
            spec:
              provider:
                name: github
              {key}: {value}
            """,
        )
        with pytest.raises(ConfigError, match=f"{key}: unknown field; expected one of: provider"):
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


def test_a_metadata_field_written_in_spec_is_refused(tmp_path: Path) -> None:
    """The metadata fields ARE fields of the row, so ``extra="forbid"``
    would accept one written in ``spec`` and let it silently override the
    envelope. Over two of them because the guard is derived from the row
    base rather than naming ``description`` alone, and in one loop for the
    same reason: one derivation answers for both."""
    for field in ("description", "name"):
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


# The per-kind NAME CAPS were pinned here three times over and are not any
# more. Both the tightened-cap and the removed-cap mutations are caught by
# ``test_spec_hosts.py::test_a_site_name_takes_the_freeform_cap`` (vm-site,
# both directions, with the number in the message), by
# ``test_spec_secret.py::test_the_secret_cap_is_the_larger_one`` (the secret
# kind's larger cap, both directions), and through the whole loader by
# ``test_loader_and_envelope.py::test_long_secret_name_over_username_cap_loads``
# and ``::test_secret_name_over_secret_cap_errors``.


#: The five kinds that were template-shaped before description became
#: framework-uniform. Written out rather than derived from
#: ``declarable_kinds()``: the rest of the declarable set has required spec
#: fields, so ``spec: {}`` would not decode for them. The claim below is
#: therefore about these five, not about every declarable kind.
_FORMERLY_TEMPLATE_SHAPED = [
    "vm-template",
    "agent-template",
    "workspace-template",
    "admin-template",
    "named-console-template",
]


def test_a_description_is_stored_by_every_formerly_template_shaped_kind(tmp_path: Path) -> None:
    """description is framework-uniform now, so none of these kinds emits
    the retired "not yet stored" warning and every one of them keeps the
    value.

    All the way to the REGISTRY, not just onto the decoded row: what the
    retired warning said was that the value went nowhere, and a row that
    carries it into a registry nothing can look it up in would be the same
    defect one layer along. A separate vm-template-only test made that
    second half of the claim; it is the same claim for every kind here.

    One load over all five rather than a case each. Uniformity is the
    claim, so what a failure has to say is which kinds stopped being
    uniform, and this way the loader and the registry are each built once.
    """
    for kind in _FORMERLY_TEMPLATE_SHAPED:
        _manifest(
            tmp_path,
            f"""
            apiVersion: agentworks/v1
            kind: {kind}
            metadata:
              name: default
              description: uniform description
            spec: {{}}
            """,
            rel=f"{kind}.yaml",
        )
    manifests = load_manifests(tmp_path / "resources")
    registry = build_registry(_config(tmp_path))

    assert not manifests.issues
    decoded = {entry.kind: entry.resource.description for entry in manifests.entries}
    assert decoded == dict.fromkeys(_FORMERLY_TEMPLATE_SHAPED, "uniform description")
    stored = {kind: registry.lookup(kind, "default").description for kind in _FORMERLY_TEMPLATE_SHAPED}
    assert stored == dict.fromkeys(_FORMERLY_TEMPLATE_SHAPED, "uniform description")


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

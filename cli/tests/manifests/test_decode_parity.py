"""Decode parity: the same resource declared as flat TOML and as a tagged
manifest must produce the same Resource.

config.toml no longer loads resources on the normal path (ADR 0022), so
the two sides fork: the flat TOML is read by the migrator's pre-side
oracle (``agentworks.migrate.toml_resources``, where those loaders now
live), the tagged YAML by the manifest decoders. Comparing the two is a
real test of the emission mapping rather than a tautology, and pins the
metadata.description mapping, the git-credential provider vocabulary, and
the admin flattening. Source-dependent fields are normalized with the
migrator's own ``strip_source_fields`` so the two cannot drift.
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
from agentworks.migrate.toml_resources import toml_resource_rows

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


def _strip(resource: Any) -> Any:
    """Drop the source-dependent fields so TOML- and manifest-decoded
    Resources compare equal. Shared with the migrate tool's per-run
    registry-equivalence verification so the two cannot drift."""
    from agentworks.migrate.verify import strip_source_fields

    return strip_source_fields(resource)


@pytest.mark.parametrize(
    ("kind", "name", "toml_body", "manifest_doc"),
    [
        (
            "secret",
            "npm-token",
            """
            [secrets.npm-token]
            description = "npm registry token"
            hint = "generate at npmjs.com"
            backend_mappings.env-var = "NPM_TOKEN"
            """,
            """
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: npm-token
              description: npm registry token
            spec:
              hint: generate at npmjs.com
              backend_mappings:
                env-var: NPM_TOKEN
            """,
        ),
        (
            "vm-template",
            "dev",
            """
            [vm_templates.dev]
            description = "dev box"
            cpus = 8
            apt = ["zsh"]
            apt_packages = ["gh"]

            [vm_templates.dev.env]
            HTTP_PROXY = "http://proxy:3128"
            NPM_TOKEN = { secret = "npm-token" }
            """,
            """
            apiVersion: agentworks/v1
            kind: vm-template
            metadata:
              name: dev
              description: dev box
            spec:
              cpus: 8
              apt: [zsh]
              apt_packages: [gh]
              env:
                HTTP_PROXY: http://proxy:3128
                NPM_TOKEN: {secret: npm-token}
            """,
        ),
        (
            "agent-template",
            "dev",
            """
            [agent_templates.dev]
            description = "dev agent"
            shell = "zsh"
            """,
            """
            apiVersion: agentworks/v1
            kind: agent-template
            metadata:
              name: dev
              description: dev agent
            spec:
              shell: zsh
            """,
        ),
        (
            # Flat TOML command fields and the canonical tagged YAML
            # harness-integration table decode to the same row.
            "session-template",
            "claude",
            """
            [session_templates.claude]
            command = "claude"
            description = "Claude session"
            required_commands = ["claude"]
            """,
            """
            apiVersion: agentworks/v1
            kind: session-template
            metadata:
              name: claude
              description: Claude session
            spec:
              harness_integration:
                name: shell
                command: claude
                required_commands: [claude]
            """,
        ),
        (
            "workspace-template",
            "proj",
            """
            [workspace_templates.proj]
            description = "the proj workspace"
            repo = "https://github.com/org/proj.git"
            tmuxinator = false
            """,
            """
            apiVersion: agentworks/v1
            kind: workspace-template
            metadata:
              name: proj
              description: the proj workspace
            spec:
              repo: https://github.com/org/proj.git
              tmuxinator: false
            """,
        ),
        (
            "named-console-template",
            "default",
            """
            [named_console]
            description = "the default console"
            tmux_layout = "aw-session-vertical"
            """,
            """
            apiVersion: agentworks/v1
            kind: named-console-template
            metadata:
              name: default
              description: the default console
            spec:
              tmux_layout: aw-session-vertical
            """,
        ),
        (
            "git-credential",
            "github",
            """
            [git_credentials.github]
            type = "github"
            description = "gh access"
            """,
            """
            apiVersion: agentworks/v1
            kind: git-credential
            metadata:
              name: github
              description: gh access
            spec:
              provider:
                name: github
            """,
        ),
        (
            # The deliberate shape divergence: flat TOML (org top-level)
            # and nested YAML (org under provider_config) decode to the
            # same row -- provider-owned config nests in manifests.
            "git-credential",
            "ado",
            """
            [git_credentials.ado]
            type = "azdo"
            org = "my-org"
            token = "git-token-ado"
            """,
            """
            apiVersion: agentworks/v1
            kind: git-credential
            metadata:
              name: ado
            spec:
              provider:
                name: azdo
                org: my-org
                token: git-token-ado
            """,
        ),
        (
            "apt-package",
            "my-tool",
            """
            [apt_packages.my-tool]
            description = "my tool"
            apt = ["my-tool"]
            """,
            """
            apiVersion: agentworks/v1
            kind: apt-package
            metadata:
              name: my-tool
              description: my tool
            spec:
              apt: [my-tool]
            """,
        ),
    ],
)
def test_round_trip_parity(tmp_path: Path, kind: str, name: str, toml_body: str, manifest_doc: str) -> None:
    # Pre-side oracle: the flat TOML declaration, read by the migrator's
    # TOML reader (config.toml no longer loads resources on the normal
    # path, ADR 0022; that reader now lives in the oracle).
    toml_cfg = tmp_path / "source.toml"
    toml_cfg.write_text(dedent(toml_body))
    oracle_row = toml_resource_rows(toml_cfg)[(kind, name)]

    # Post-side: the tagged YAML manifest, decoded through build_registry.
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    _manifest(manifest_dir, manifest_doc)
    manifest_row = build_registry(_config(manifest_dir)).lookup(kind, name)

    assert _strip(oracle_row) == _strip(manifest_row)


def test_admin_template_flat_spec(tmp_path: Path) -> None:
    # Pre-side oracle: the flat [admin.config] + [admin.env] TOML.
    toml_cfg = tmp_path / "source.toml"
    toml_cfg.write_text(
        dedent(
            """
            [admin.config]
            description = "the admin user"
            username = "ops"
            shell = "zsh"
            git_credentials = ["github"]

            [admin.env]
            EDITOR = "nvim"
            """
        )
    )
    oracle_admin = toml_resource_rows(toml_cfg)[("admin-template", "default")]

    # Post-side: the flattened admin manifest (plus the git-credential its
    # git_credentials list references, so the finalize walk resolves).
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    _manifest(
        manifest_dir,
        """
        apiVersion: agentworks/v1
        kind: admin-template
        metadata:
          name: default
          description: the admin user
        spec:
          username: ops
          shell: zsh
          git_credentials: [github]
          env:
            EDITOR: nvim
        ---
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: github
        spec:
          provider:
            name: github
        """,
    )
    manifest_admin = build_registry(_config(manifest_dir)).lookup("admin-template", "default")

    assert _strip(oracle_admin) == _strip(manifest_admin)


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
    """token lives under provider_config now; a top-level spec.token is
    rejected with a migration hint."""
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

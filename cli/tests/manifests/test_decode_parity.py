"""Decode parity: the same resource declared via TOML and via a manifest
must produce the same Resource (resource-manifests SDD, Phase 2).

Parity is structural because the decoders literally call the TOML
loaders; these tests pin that wiring (and the metadata.description
mapping, the git-credential provider vocabulary, and the admin
flattening) end to end through ``build_registry``.
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
    cfg.write_text(
        _BASE_TOML.format(pub=tmp_path / "k.pub", priv=tmp_path / "k")
        + dedent(body)
    )
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
            # The deliberate shape divergence for the harness pair: flat
            # TOML (command/required_commands top-level) and clean YAML
            # (nested under harness_config on the shell harness) decode to
            # the same row -- the loader hoists, manifests nest.
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
              harness: shell
              harness_config:
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
              provider: github
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
              provider: azdo
              provider_config:
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
def test_round_trip_parity(
    tmp_path: Path, kind: str, name: str, toml_body: str, manifest_doc: str
) -> None:
    toml_dir = tmp_path / "toml"
    toml_dir.mkdir()
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()

    toml_registry = build_registry(
        _config(toml_dir, toml_body), ManifestSet.empty()
    )
    _manifest(manifest_dir, manifest_doc)
    manifest_registry = build_registry(_config(manifest_dir))

    assert _strip(toml_registry.lookup(kind, name)) == _strip(
        manifest_registry.lookup(kind, name)
    )


def test_admin_template_flat_spec(tmp_path: Path) -> None:
    toml_dir = tmp_path / "toml"
    toml_dir.mkdir()
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()

    toml_registry = build_registry(
        _config(
            toml_dir,
            """
            [admin.config]
            description = "the admin user"
            username = "ops"
            shell = "zsh"
            git_credentials = ["github"]

            [admin.env]
            EDITOR = "nvim"

            [git_credentials.github]
            type = "github"
            """,
        ),
        ManifestSet.empty(),
    )
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
          provider: github
        """,
    )
    manifest_registry = build_registry(_config(manifest_dir))

    assert _strip(toml_registry.lookup("admin-template", "default")) == _strip(
        manifest_registry.lookup("admin-template", "default")
    )


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
    with pytest.raises(ConfigError, match='use "provider", not "type"'):
        load_manifests(tmp_path / "resources")


def test_git_credential_provider_config_rejects_kind_owned_fields(
    tmp_path: Path,
) -> None:
    """The blob may not shadow the kind-owned surface (type/provider/
    description). token is NOT kind-owned any more; it is provider
    config, so it is tested separately (test_git_credential_token_in
    _provider_config)."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: gh
        spec:
          provider: github
          provider_config:
            provider: sneaky
        """,
    )
    with pytest.raises(
        ConfigError, match="may not contain kind-owned field"
    ):
        load_manifests(tmp_path / "resources")


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
          provider: github
          token: at-top-level
        """,
    )
    with pytest.raises(ConfigError, match="under spec.provider_config"):
        load_manifests(tmp_path / "resources")


def test_provider_config_must_be_a_mapping(tmp_path: Path) -> None:
    """A non-mapping provider_config blob is rejected. (Post-collapse,
    git-credential is the one kind carrying the blob.)"""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: gh
        spec:
          provider: github
          provider_config: nope
        """,
    )
    with pytest.raises(
        ConfigError, match="provider_config must be a mapping"
    ):
        load_manifests(tmp_path / "resources")


def test_git_credential_org_must_nest_under_provider_config(tmp_path: Path) -> None:
    """Provider-owned fields do not ride the spec top level in YAML:
    a stray `org` errors with a pointer at the nesting rule."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: ado
        spec:
          provider: azdo
          org: my-org
        """,
    )
    with pytest.raises(ConfigError, match="goes under\\s+spec.provider_config"):
        load_manifests(tmp_path / "resources")


def test_description_in_spec_rejected(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: s1
          description: d
        spec:
          description: also here
        """,
    )
    with pytest.raises(ConfigError, match="metadata.description"):
        load_manifests(tmp_path / "resources")


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
def test_description_never_warns_for_declarable_kind(
    tmp_path: Path, kind: str, spec_body: str
) -> None:
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


def test_catalog_kind_decode_error_carries_location(tmp_path: Path) -> None:
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


def test_manifest_admin_collides_with_declared_toml_admin(tmp_path: Path) -> None:
    """Dual-window semantics: a real [admin.config] in TOML plus an
    admin manifest is a duplicate."""
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
    config = _config(
        tmp_path,
        """
        [admin.config]
        username = "other"
        """,
    )
    with pytest.raises(ConfigError, match="duplicate admin-template"):
        build_registry(config)


def test_toml_catalog_extension_vs_manifest_is_duplicate(tmp_path: Path) -> None:
    """The line-0 exemption is singleton-only: a TOML catalog extension
    colliding with a manifest errors like any operator duplicate."""
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: apt-package
        metadata:
          name: my-tool
          description: from manifest
        spec:
          apt: [my-tool]
        """,
    )
    config = _config(
        tmp_path,
        """
        [apt_packages.my-tool]
        description = "from TOML"
        apt = ["my-tool"]
        """,
    )
    with pytest.raises(ConfigError, match="duplicate apt-package"):
        build_registry(config)


def test_cross_source_duplicate_errors_at_build(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "from TOML"
        """,
    )
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: npm-token
          description: from manifest
        spec: {}
        """,
    )
    with pytest.raises(ConfigError, match="duplicate secret"):
        build_registry(config)


def test_manifest_overrides_builtin_catalog_entry(tmp_path: Path) -> None:
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

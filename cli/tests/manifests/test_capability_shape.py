"""The tagged capability-config shape (declarative-schema pre-support).

The two stable hosting surfaces (vm-site's platform and git-credential's
provider) accept the capability as ONE tagged table whose ``name`` key
selects it and whose remaining keys are its config. Session templates use
their distinct ``harness_integration`` selector. Old forms still load
identically but record deprecation facts; mixing old and canonical forms
on one resource is a hard error.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests
from agentworks.migrate.verify import strip_source_fields

_OLD_NEW_PAIRS = [
    (
        "vm-site",
        "gpu-box",
        """
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: gpu-box
        spec:
          platform: lima
          platform_config:
            vm_host: me@gpu-box
        """,
        """
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: gpu-box
        spec:
          platform:
            name: lima
            vm_host: me@gpu-box
        """,
    ),
    (
        "git-credential",
        "ado",
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
        "session-template",
        "htop",
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: htop
        spec:
          harness: shell
          harness_config:
            command: htop
            required_commands: [htop]
          env:
            TERM: xterm-256color
        """,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: htop
        spec:
          harness_integration:
            name: shell
            command: htop
            required_commands: [htop]
          env:
            TERM: xterm-256color
        """,
    ),
]


def _load_one(tmp_path: Path, name: str, text: str):  # noqa: ANN202 - test helper
    resources = tmp_path / name / "resources"
    resources.mkdir(parents=True)
    (resources / "res.yaml").write_text(dedent(text))
    return load_manifests(resources)


@pytest.mark.parametrize(("kind", "name", "old_doc", "new_doc"), _OLD_NEW_PAIRS)
def test_tagged_shape_decodes_identically_to_sibling_shape(
    tmp_path: Path, kind: str, name: str, old_doc: str, new_doc: str
) -> None:
    """Both shapes normalize to the same internal fields, so the config
    reaches the finalize capability validation identically."""
    old = _load_one(tmp_path, "old", old_doc)
    new = _load_one(tmp_path, "new", new_doc)
    assert [e.kind for e in new.entries] == [kind]
    assert strip_source_fields(old.entries[0].resource) == strip_source_fields(new.entries[0].resource)


@pytest.mark.parametrize(("kind", "name", "old_doc", "new_doc"), _OLD_NEW_PAIRS)
def test_old_shape_warns_and_new_shape_does_not(
    tmp_path: Path, kind: str, name: str, old_doc: str, new_doc: str
) -> None:
    old = _load_one(tmp_path, "old", old_doc)
    if kind == "session-template":
        assert old.deprecated_harness_selectors == (f"{kind}/{name}",)
        assert not old.deprecation_issues
    else:
        assert old.deprecated_shape_resources == (f"{kind}/{name}",)
        assert len(old.deprecation_issues) == 1
    # Deprecation rides its own channel, not the issue channel (which
    # bundles treat as fatal and doctor renders as generic warnings).
    assert not old.issues

    new = _load_one(tmp_path, "new", new_doc)
    assert not new.deprecation_issues
    assert not new.deprecated_shape_resources
    assert not new.deprecated_harness_selectors
    assert not new.issues


def test_old_shape_warning_aggregates_once_and_names_resources(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    docs = "\n---\n".join(dedent(doc) for _kind, _name, doc, _new in _OLD_NEW_PAIRS)
    (resources / "res.yaml").write_text(docs)
    manifests = load_manifests(resources)
    assert manifests.deprecated_shape_resources == (
        "vm-site/gpu-box",
        "git-credential/ado",
    )
    assert manifests.deprecated_harness_selectors == ("session-template/htop",)
    (message,) = manifests.deprecation_issues
    for token in manifests.deprecated_shape_resources:
        assert token in message
    assert "deprecated" in message
    assert "will be removed" in message
    assert "platform: {name: <capability>, <config keys...>}" in message


def test_old_string_without_sibling_config_counts_as_old_shape(tmp_path: Path) -> None:
    """`platform: lima` alone is still the old shape; the rewrite is
    `platform: {name: lima}`."""
    manifests = _load_one(
        tmp_path,
        "old",
        """
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: bare
        spec:
          platform: lima
        """,
    )
    assert manifests.deprecated_shape_resources == ("vm-site/bare",)


def test_session_template_canonical_selector_is_not_a_capability_shape_deprecation(tmp_path: Path) -> None:
    manifests = _load_one(
        tmp_path,
        "canonical",
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: htop
        spec:
          harness_integration:
            name: shell
            command: htop
        """,
    )
    (entry,) = manifests.entries
    assert entry.resource.harness == "shell"
    assert entry.resource.harness_config == {"command": "htop"}
    assert not manifests.deprecated_harness_selectors
    assert not manifests.deprecation_issues


def test_session_template_old_and_canonical_selectors_cannot_mix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="old and new harness selector/config fields cannot be mixed"):
        _load_one(
            tmp_path,
            "mixed-selector",
            """
            apiVersion: agentworks/v1
            kind: session-template
            metadata:
              name: htop
            spec:
              harness: shell
              harness_integration:
                name: shell
            """,
        )


@pytest.mark.parametrize(
    "spec",
    ["env:\n    TERM: xterm-256color", "inherits: parent"],
)
def test_session_template_without_selector_remains_a_valid_default_or_inheriting_template(
    tmp_path: Path, spec: str
) -> None:
    manifests = _load_one(
        tmp_path,
        "no-selector",
        "\n".join(
            (
                "apiVersion: agentworks/v1",
                "kind: session-template",
                "metadata:",
                "  name: child",
                "spec:",
                *(f"  {line}" for line in spec.splitlines()),
            )
        ),
    )
    (entry,) = manifests.entries
    assert entry.resource.harness is None
    assert entry.resource.harness_config is None
    assert not manifests.deprecated_harness_selectors


@pytest.mark.parametrize(
    ("doc", "field"),
    [
        (
            """
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: gpu-box
            spec:
              platform:
                name: lima
                vm_host: me@gpu-box
              platform_config:
                vm_host: elsewhere
            """,
            "platform",
        ),
        (
            """
            apiVersion: agentworks/v1
            kind: git-credential
            metadata:
              name: ado
            spec:
              provider:
                name: azdo
              provider_config:
                org: my-org
            """,
            "provider",
        ),
    ],
)
def test_mixed_shape_is_a_hard_error(tmp_path: Path, doc: str, field: str) -> None:
    with pytest.raises(ConfigError, match=f"spec.{field} is a tagged table"):
        _load_one(tmp_path, "mixed", doc)


@pytest.mark.parametrize(
    "name_line",
    [
        "vm_host: me@gpu-box",  # no name key at all
        "name: 123",  # non-string name
        "name: [lima]",  # non-string name (sequence)
        "name: ''",  # empty-string name
    ],
)
def test_tagged_table_requires_a_string_name_key(tmp_path: Path, name_line: str) -> None:
    with pytest.raises(ConfigError, match="requires a 'name' key"):
        _load_one(
            tmp_path,
            "nameless",
            f"""
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: gpu-box
            spec:
              platform:
                {name_line}
            """,
        )


def test_migrator_tagged_table_refuses_name_config_key() -> None:
    """The migrator's emission guard: a config key literally named
    'name' would collide with the tagged table's discriminator. Known
    capabilities reject it via pre-write validation; this guard is the
    backstop for capabilities the run cannot validate, so it is pinned
    directly."""
    from agentworks.migrate.planning import _tagged_capability_table

    with pytest.raises(ConfigError, match="collides with the tagged table's discriminator"):
        _tagged_capability_table("vm-site", "weird", "future-platform", {"name": "sneaky"})
    # Without the collision the table folds name-first.
    table = _tagged_capability_table("vm-site", "ok", "lima", {"vm_host": "me@box"})
    assert table == {"name": "lima", "vm_host": "me@box"}


def test_tagged_shape_reaches_finalize_validation(tmp_path: Path) -> None:
    """The tagged config keys land on the row's config mapping, so the
    finalize capability validation sees them exactly as with the old
    shape (an unknown key still errors at finalize)."""
    manifests = _load_one(
        tmp_path,
        "new",
        """
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: gpu-box
        spec:
          platform:
            name: lima
            vm_host: me@gpu-box
        """,
    )
    (entry,) = manifests.entries
    assert entry.resource.platform == "lima"
    assert entry.resource.platform_config == {"vm_host": "me@gpu-box"}


def test_tagged_table_still_hits_reserved_field_check(tmp_path: Path) -> None:
    """The vm-site kind-owned shadow check keeps firing for the tagged
    shape: a `platform` key inside the table would silently re-pick the
    capability."""
    with pytest.raises(ConfigError, match="may not contain kind-owned field"):
        _load_one(
            tmp_path,
            "shadow",
            """
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: gpu-box
            spec:
              platform:
                name: lima
                platform: wsl2
            """,
        )


def _write_config(tmp_path: Path) -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
    )
    return cfg


_OLD_SHAPE_MANIFEST = """
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: htop
spec:
  harness: shell
  harness_config:
    command: htop
"""


def test_cli_warns_ambiently_and_no_deprecations_silences(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The aggregated warning rides the ambient deprecation channel:
    printed by the registry auto-load, silenced by --no-deprecations."""
    from typer.testing import CliRunner

    from agentworks import output
    from agentworks.cli import app

    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "res.yaml").write_text(dedent(_OLD_SHAPE_MANIFEST))
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setattr(output, "_suppress_deprecations", False)

    with_warning = CliRunner().invoke(app, ["resource", "list", "--names-only"])
    assert with_warning.exit_code == 0, with_warning.output
    assert "deprecated session-template selector in: session-template/htop" in with_warning.output

    silenced = CliRunner().invoke(app, ["--no-deprecations", "resource", "list", "--names-only"])
    assert silenced.exit_code == 0, silenced.output
    assert "deprecated session-template selector" not in silenced.output


def test_cli_aggregates_toml_and_yaml_old_selectors_into_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warning is a request fact, not one emission per input source."""
    from typer.testing import CliRunner

    from agentworks import output
    from agentworks.cli import app

    cfg = _write_config(tmp_path)
    with cfg.open("a", encoding="utf-8") as handle:
        handle.write('\n[session_templates.toml-old]\nharness = "shell"\n')
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "res.yaml").write_text(dedent(_OLD_SHAPE_MANIFEST))
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setattr(output, "_suppress_deprecations", False)

    result = CliRunner().invoke(app, ["resource", "list", "--names-only"])
    assert result.exit_code == 0, result.output
    assert result.output.count("deprecated session-template selector in:") == 1
    assert "session-template/toml-old" in result.output
    assert "session-template/htop" in result.output


def test_cli_new_shape_does_not_warn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from agentworks import output
    from agentworks.cli import app

    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "res.yaml").write_text(
        dedent("""
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: htop
        spec:
          harness_integration:
            name: shell
            command: htop
        """)
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setattr(output, "_suppress_deprecations", False)

    result = CliRunner().invoke(app, ["resource", "list", "--names-only"])
    assert result.exit_code == 0, result.output
    assert "deprecated session-template selector" not in result.output


def test_doctor_surfaces_deprecated_shape_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor renders the fact as a tidy one-liner naming the affected
    resources with the rewrite pattern as the one next step."""
    from agentworks.doctor import Status, _check_config

    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "res.yaml").write_text(dedent(_OLD_SHAPE_MANIFEST))
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)

    g, _, _ = _check_config()
    warns = [(c.name, c.message or "") for c in g.checks if c.status == Status.WARN]
    ((name, message),) = [w for w in warns if "deprecated harness selector" in w[0]]
    assert name == "Session templates use the deprecated harness selector"
    assert "session-template/htop" in message
    assert "harness_integration" in message


def test_builtin_bundle_publishes_cleanly() -> None:
    """The shipped built-in bundle spells the tagged shape, so it clears
    the deprecated-shape gate."""
    from agentworks.manifests.package import publish_manifest_package
    from agentworks.resources import Origin, Registry

    publish_manifest_package(
        Registry.empty(),
        anchor="agentworks.manifests",
        subdir="builtin",
        origin_for=lambda name: Origin.built_in(source=name),
    )


def test_bundled_manifests_reject_deprecated_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shipped bundles are the pattern book: a bundle spelling the old
    shape fails loudly instead of teaching it."""
    from agentworks.manifests import ManifestSet
    from agentworks.manifests.package import publish_manifest_package
    from agentworks.resources import Origin, Registry

    dirty = ManifestSet(entries=(), issues=(), deprecation_issues=("deprecated capability config shape in: ...",))
    monkeypatch.setattr("agentworks.manifests.package.load_manifests", lambda _dir: dirty)
    with pytest.raises(ConfigError, match="must not use deprecated shapes"):
        publish_manifest_package(
            Registry.empty(),
            anchor="agentworks.manifests",
            subdir="builtin",
            origin_for=lambda name: Origin.built_in(source=name),
        )

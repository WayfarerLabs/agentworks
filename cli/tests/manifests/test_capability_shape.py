"""The tagged capability-config shape: the ONE way to name a capability.

Every hosting surface (vm-site's platform, git-credential's provider,
session-template's harness_integration) takes the capability as one
tagged table whose ``name`` key selects it and whose remaining keys are
its config. The legacy sibling shape (a naming string plus a ``*_config``
table) was accepted with a deprecation warning through 0.14 and is a hard
error now, naming the exact rewrite; ``agw resource migrate`` is the
remediation.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests

_SURFACES = [
    (
        "vm-site",
        "platform",
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
        "platform: {name: lima, vm_host: ...}",
    ),
    (
        "git-credential",
        "provider",
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
        "provider: {name: azdo, org: ..., token: ...}",
    ),
    (
        "session-template",
        "harness_integration",
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: htop
        spec:
          harness_integration: shell
          harness_integration_config:
            command: htop
        """,
        "harness_integration: {name: shell, command: ...}",
    ),
]
"""Per host surface: kind, naming field, a document in the retired sibling
shape, and the exact rewrite its error must print."""


def _load_one(tmp_path: Path, name: str, text: str):  # noqa: ANN202 - test helper
    resources = tmp_path / name / "resources"
    resources.mkdir(parents=True)
    (resources / "res.yaml").write_text(dedent(text))
    return load_manifests(resources)


@pytest.mark.parametrize(("kind", "field", "old_doc", "rewrite"), _SURFACES)
def test_sibling_shape_is_rejected_with_the_exact_rewrite(
    tmp_path: Path, kind: str, field: str, old_doc: str, rewrite: str
) -> None:
    """The error shows the operator THEIR resource in the shape it needs:
    the capability they named, plus the config keys they wrote, folded into
    one table. A generic "use a tagged table" would leave them to work out
    the fold themselves.

    Every surface answers the same way. Session templates hardened a
    release earlier than their siblings and kept a fold of their own
    through that window; there is one fold now, so this is one table-driven
    expectation rather than an asymmetry to describe.
    """
    with pytest.raises(ConfigError) as excinfo:
        _load_one(tmp_path, "old", old_doc)
    assert f"spec.{field} names the capability as a string" in str(excinfo.value)
    assert rewrite in str(excinfo.value)
    assert excinfo.value.hint == "`agw resource migrate --all` rewrites your manifests in place."


def test_bare_naming_string_is_rejected_with_a_one_key_rewrite(tmp_path: Path) -> None:
    """`platform: lima` alone is the old shape too; the rewrite is
    `platform: {name: lima}` with nothing to fold in."""
    with pytest.raises(ConfigError, match=r"platform: \{name: lima\}"):
        _load_one(
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


@pytest.mark.parametrize(("kind", "field", "old_doc", "rewrite"), _SURFACES)
def test_sibling_config_alone_names_the_unsupported_field(
    tmp_path: Path, kind: str, field: str, old_doc: str, rewrite: str
) -> None:
    """A ``*_config`` table with no naming field beside it: the message
    names the field that is not supported rather than reporting the
    capability as missing, which is what the kind's own required-field
    error would say."""
    config_field = f"{field}_config"
    document = "\n".join(line for line in dedent(old_doc).splitlines() if not line.strip().startswith(f"{field}:"))
    with pytest.raises(ConfigError, match=f"spec.{config_field} is not a supported YAML field"):
        _load_one(tmp_path, "ownerless", document)


def test_session_template_canonical_selector_decodes_to_the_internal_pair(tmp_path: Path) -> None:
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
    assert entry.resource.harness_integration == "shell"
    assert entry.resource.harness_integration_config == {"command": "htop"}
    assert not manifests.issues


def test_legacy_session_harness_config_without_selector_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError,
        match=r"unexpected keys in \[session_templates.htop\]: harness_config",
    ):
        _load_one(
            tmp_path,
            "ownerless-config",
            """
            apiVersion: agentworks/v1
            kind: session-template
            metadata:
              name: htop
            spec:
              harness_config:
                command: htop
            """,
        )


def test_legacy_session_harness_empty_scalar_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unexpected keys.*harness"):
        _load_one(
            tmp_path,
            "empty-selector",
            """
            apiVersion: agentworks/v1
            kind: session-template
            metadata:
              name: htop
            spec:
              harness: ""
            """,
        )


def test_legacy_session_harness_non_string_scalar_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unexpected keys.*harness"):
        _load_one(
            tmp_path,
            "non-string-selector",
            """
            apiVersion: agentworks/v1
            kind: session-template
            metadata:
              name: htop
            spec:
              harness: 42
            """,
        )


def test_legacy_session_harness_is_rejected_with_location(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError,
        match=r"res.yaml:2:.*unexpected keys.*harness",
    ):
        _load_one(
            tmp_path,
            "valid-selector",
            """
            apiVersion: agentworks/v1
            kind: session-template
            metadata:
              name: htop
            spec:
              harness: shell
            """,
        )


def test_session_template_old_and_canonical_selectors_cannot_mix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unexpected keys.*harness"):
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
    assert entry.resource.harness_integration is None
    assert entry.resource.harness_integration_config is None


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
        (
            """
            apiVersion: agentworks/v1
            kind: session-template
            metadata:
              name: htop
            spec:
              harness_integration:
                name: shell
              harness_integration_config:
                command: htop
            """,
            "harness_integration",
        ),
    ],
)
def test_mixed_shape_is_a_hard_error(tmp_path: Path, doc: str, field: str) -> None:
    """A tagged table beside a sibling ``*_config``: the message names the
    field that is not supported, so the operator's next move is to fold
    those keys in rather than to guess which half won."""
    with pytest.raises(ConfigError, match=f"spec.{field}_config is not a supported YAML field"):
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
kind: vm-site
metadata:
  name: gpu-box
spec:
  platform: lima
  platform_config:
    vm_host: me@gpu-box
"""


def test_cli_fails_on_the_old_shape_and_no_deprecations_does_not_silence_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through a real command: the old shape stops the run and
    prints the rewrite plus the remediation command.

    ``--no-deprecations`` silences ambient nudges. This is not one any
    more, so the flag must not hide it: a silenced load error would leave
    the operator with a command that fails for no visible reason.
    """
    from typer.testing import CliRunner

    from agentworks import output
    from agentworks.cli import app

    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "res.yaml").write_text(dedent(_OLD_SHAPE_MANIFEST))
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setattr(output, "_suppress_deprecations", False)

    for argv in (["resource", "list"], ["--no-deprecations", "resource", "list"]):
        result = CliRunner().invoke(app, argv)
        assert result.exit_code != 0, result.output
        assert isinstance(result.exception, ConfigError)
        assert "res.yaml:2:" in str(result.exception)
        assert "platform: {name: lima, vm_host: ...}" in str(result.exception)
        assert result.exception.hint == "`agw resource migrate --all` rewrites your manifests in place."


def test_cli_tagged_shape_loads_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert "session-template/htop" in result.output


def test_doctor_reports_the_old_shape_as_a_manifest_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor carried a tidy WARN row for the old shape while it still
    loaded. It is a load failure now, so the row it gets is the Manifest
    FAIL row every unloadable manifest gets, carrying the same rewrite."""
    from agentworks.doctor import Status, _check_config

    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "res.yaml").write_text(dedent(_OLD_SHAPE_MANIFEST))
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)

    g, _, _ = _check_config()
    fails = [(c.name, c.message or "") for c in g.checks if c.status == Status.FAIL]
    ((name, message),) = [f for f in fails if "platform" in f[1]]
    assert name == "Manifest"
    assert "platform: {name: lima, vm_host: ...}" in message
    assert not [c for c in g.checks if c.status == Status.WARN and "capability config shape" in c.name]


def test_builtin_bundle_publishes_cleanly() -> None:
    """The shipped built-in bundle spells the tagged shape.

    It needs no bundle-specific gate to prove it: a first-party bundle
    loads through ``load_manifests`` like any other manifest source, so
    the old shape would fail this publish outright.
    """
    from agentworks.manifests.package import publish_manifest_package
    from agentworks.resources import Origin, Registry

    publish_manifest_package(
        Registry.empty(),
        anchor="agentworks.manifests",
        subdir="builtin",
        origin_for=lambda name: Origin.built_in(source=name),
    )

"""The tagged capability-config shape: the ONE way to name a capability.

Every hosting surface (vm-site's platform, git-credential's provider,
session-template's harness_integration) takes the capability as one
tagged table whose ``name`` key selects it and whose remaining keys are
its config. The legacy sibling shape (a naming string plus a ``*_config``
table) was accepted with a deprecation warning through 0.14 and is a hard
error now, naming the exact rewrite the operator applies by hand.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests

_REWRITE_HINT = (
    "Apply the rewrite above; `agw resource describe-kind <kind>` documents the field, "
    "and `agw resource sample <kind>` prints it as a document to edit. "
    'See "The retired sibling capability shape" in docs/guides/upgrading-to-0.14.md.'
)
"""The operator-facing text, spelled out rather than imported.

The remediation is the operator's own edit now (operator ruling,
2026-08-07), so what the hint SAYS is the whole remedy rather than a
pointer to a command that would do it. Importing the constant would assert
it equals itself; this is the representative-mistakes corpus, and the text
is what it is pinning."""

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

    **This is the representative-mistakes corpus's old-sibling-shape
    entry**, and it is deliberately end to end (a manifest on disk, not a
    model and a blob) rather than living beside the other four in
    ``tests/schema/test_errors.py``. Under the kind spec models this input
    is just two problems the model layer has no reason to connect, an
    unknown ``platform_config`` key and a ``platform`` that is not a table,
    which is exactly the generic pair this sentence exists to beat. Pinning
    it at the surface an operator actually types is what stops the swap
    from quietly degrading it.
    """
    with pytest.raises(ConfigError) as excinfo:
        _load_one(tmp_path, "old", old_doc)
    assert f"spec.{field} names the capability as a string" in str(excinfo.value)
    assert rewrite in str(excinfo.value)
    assert excinfo.value.hint == _REWRITE_HINT


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
    assert entry.resource.harness_integration.name == "shell"
    assert entry.resource.harness_integration.config == {"command": "htop"}
    assert not manifests.issues


def test_legacy_session_harness_config_without_selector_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError,
        match="harness_config: unknown field; expected one of: ",
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
    with pytest.raises(ConfigError, match="harness: unknown field; expected one of: "):
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
    with pytest.raises(ConfigError, match="harness: unknown field; expected one of: "):
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
        match=r"res.yaml:2:.*harness: unknown field; expected one of: ",
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
    with pytest.raises(ConfigError, match="harness: unknown field; expected one of: "):
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
    ["env:\n    TERM: xterm-256color", "inherits: [parent]"],
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
def test_mixed_shape_is_a_hard_error_with_no_rewrite_hint(tmp_path: Path, doc: str, field: str) -> None:
    """A tagged table beside a sibling ``*_config``: the message names the
    field that is not supported, so the operator's next move is to fold
    those keys in rather than to guess which half won.

    And it carries NO hint at all. The hint reads "apply the rewrite
    above", and no rewrite is printed here: which half of a mixed document
    wins is the operator's call, so any rewrite would be a guess. The
    sample hint every model-layer error carries is not reached either,
    because this refusal runs ahead of validation.
    """
    with pytest.raises(ConfigError) as excinfo:
        _load_one(tmp_path, "mixed", doc)
    assert f"spec.{field}_config is not a supported YAML field" in str(excinfo.value)
    assert excinfo.value.hint is None


@pytest.mark.parametrize(
    ("name_line", "message"),
    [
        ("vm_host: me@gpu-box", "platform.name: is required"),
        ("name: 123", "platform.name: must be a string"),
        ("name: [lima]", "platform.name: must be a string"),
        ("name: ''", "platform.name: must not be empty"),
    ],
)
def test_tagged_table_requires_a_string_name_key(tmp_path: Path, name_line: str, message: str) -> None:
    """The tag is a field of the block now, so each way of getting it
    wrong reads as that field's own problem rather than as one
    hand-written sentence covering four cases."""
    with pytest.raises(ConfigError, match=message):
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
    assert entry.resource.platform.name == "lima"
    assert entry.resource.platform.config == {"vm_host": "me@gpu-box"}


def test_a_kind_owned_key_inside_the_table_is_the_platforms_to_refuse(tmp_path: Path) -> None:
    """decode carried a shadow check while the sibling pair existed,
    because a ``platform`` key inside ``platform_config`` could silently
    re-pick the capability. It cannot inside ONE tagged table: ``name`` is
    the selector and it is a real field of the block, so a stray
    ``platform`` key is config the platform does not accept, and the
    platform's own model says so at finalize rather than decode
    duplicating the rule."""
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config

    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "res.yaml").write_text(
        dedent("""
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: gpu-box
        spec:
          platform:
            name: lima
            vm_host: me@gpu-box
            platform: wsl2
        """)
    )

    with pytest.raises(ConfigError, match="platform: unknown field"):
        build_registry(load_config(cfg, warn_issues=False))


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
        assert result.exception.hint == _REWRITE_HINT


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


# -- The rewrite is only printed when it would be an honest one ---------------


def test_a_sibling_carrying_its_own_name_gets_no_rewrite_and_no_hint(tmp_path: Path) -> None:
    """Folding this document would emit ``platform: {name: a, name: b}``,
    which is not valid YAML and hides that two keys claim to select the
    capability. Which one wins is the operator's call, so no rewrite is
    printed, and with no rewrite on screen the hint that says to apply it
    would be pointing at nothing.
    """
    with pytest.raises(ConfigError) as excinfo:
        _load_one(
            tmp_path,
            "collision",
            """
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: gpu-box
            spec:
              platform: future-platform
              platform_config:
                name: sneaky
            """,
        )

    message = str(excinfo.value)
    assert "carries its own 'name' ('sneaky')" in message
    assert "merge them by hand" in message
    assert "{name:" not in message, "an unusable rewrite is worse than none"
    assert excinfo.value.hint is None


def test_a_non_table_sibling_names_the_value_it_cannot_fold(tmp_path: Path) -> None:
    """There are no keys to fold, so printing the tag alone would quietly
    discard what the operator wrote."""
    with pytest.raises(ConfigError) as excinfo:
        _load_one(
            tmp_path,
            "scalar-sibling",
            """
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: gpu-box
            spec:
              platform: lima
              platform_config: oops
            """,
        )

    message = str(excinfo.value)
    assert "spec.platform_config is 'oops' rather than a table" in message
    assert excinfo.value.hint is None


@pytest.mark.parametrize("spelling", ["", " null", " ~"])
def test_an_empty_sibling_is_shown_the_rewrite(tmp_path: Path, spelling: str) -> None:
    """An empty sibling holds nothing, so the rewrite is printable, so it
    is printed.

    The refusal above prints none, because folding a non-table value would
    discard what the operator wrote. That reasoning does not reach a null:
    there are no keys to lose. Withholding the rewrite here told an
    operator to put a value where it belongs when they had written no
    value at all.

    The extra instruction over the absent-sibling case is real work: the
    empty key still has to go, or the next load answers with the ORPHAN
    refusal instead.
    """
    with pytest.raises(ConfigError) as excinfo:
        _load_one(
            tmp_path,
            f"empty-sibling{len(spelling)}",
            f"""
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: gpu-box
            spec:
              platform: lima
              platform_config:{spelling}
            """,
        )

    message = str(excinfo.value)
    assert "spec.platform_config is empty, so there are no keys to fold" in message
    assert "platform: {name: lima}" in message, "the rewrite is printable here, unlike a non-table value"
    assert "remove it" in message
    assert excinfo.value.hint == _REWRITE_HINT


def test_decode_refuses_exactly_the_three_retired_sibling_shapes() -> None:
    """The retired shapes, pinned by hand against the derived table.

    Decode refuses the sibling pair from ONE generic guard driven by
    ``HostSurface``, so what it refuses is whatever the descriptor table
    says: nothing in the refusal path would notice a renamed
    ``config_field``, because both halves would move together. That field
    exists only so the refusal can name a spelling operators have already
    typed, and renaming it would leave the guard looking for a key nobody
    ever wrote.

    Hand-written expectations for that reason, field names included. A
    fourth hosting kind lands here as a failure, which is the point: it
    inherits the refusal and the hint automatically, and this is where
    someone decides whether the sibling pair was ever a spelling for it.
    """
    from agentworks.manifests.decode import _hosting_descriptors

    core = {
        kind: (descriptor.manifest_section.naming_field, descriptor.manifest_section.config_field)
        for kind, descriptor in _hosting_descriptors().items()
        if descriptor.manifest_section is not None
    }

    assert core == {
        "vm-site": ("platform", "platform_config"),
        "git-credential": ("provider", "provider_config"),
        "session-template": ("harness_integration", "harness_integration_config"),
    }

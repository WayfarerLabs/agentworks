"""The tagged capability-config shape: the ONE way to name a capability.

Every hosting surface (vm-site's platform, git-credential's provider,
session-template's harness_integration) takes the capability as one
tagged table whose ``name`` key selects it and whose remaining keys are
its config.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests


def _load_one(tmp_path: Path, name: str, text: str):  # noqa: ANN202 - test helper
    resources = tmp_path / name / "resources"
    resources.mkdir(parents=True)
    (resources / "res.yaml").write_text(dedent(text))
    return load_manifests(resources)


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


#: The two retired session-template selector keys, and the spellings that
#: used to be pinned one test each. The VALUE cannot matter: both keys are
#: gone from the row, so ``extra="forbid"`` refuses the key before anything
#: looks at what is under it. Four separate tests spelled `harness` as an
#: empty string, a number, a valid name, and a valid name beside the
#: canonical selector; re-admitting `harness` as a field is the only
#: mutation any of them catches, and it reddens all four at once. So the
#: spellings that differ only in the unread value are gone, and what is
#: left is one case per distinct claim: each retired KEY, and the mixture
#: that might have been thought to rescue one.
_RETIRED_SELECTORS = [
    ("config-without-a-selector", "harness_config:\n    command: htop", "harness_config"),
    ("a-name-that-used-to-work", "harness: shell", "harness"),
    # A canonical selector beside it does not rescue the retired one.
    ("old-and-canonical-mixed", "harness: shell\n  harness_integration:\n    name: shell", "harness"),
]


def test_a_retired_session_selector_is_an_unknown_field_at_its_own_location(tmp_path: Path) -> None:
    """The retired keys read as what they are, with the document's
    ``file:line`` on the front so an operator with several templates knows
    which one to open.

    One loop, because the note above is the whole reason these are
    together: they all land on ``extra="forbid"``, so re-admitting a key
    reddens the lot. The label is carried into the failure so the spelling
    that stopped being refused still names itself.
    """
    for label, spec_body, key in _RETIRED_SELECTORS:
        # The literal keeps its original indentation: ``dedent`` strips the
        # COMMON prefix, and ``spec_body``'s own continuation lines carry
        # only their nesting, so indenting the block further would leave
        # dedent nothing to strip and change the document.
        document = f"""
            apiVersion: agentworks/v1
            kind: session-template
            metadata:
              name: htop
            spec:
              {spec_body}
            """
        with pytest.raises(ConfigError, match=rf"res\.yaml:2:.*{key}: unknown field; expected one of: "):
            _load_one(tmp_path, f"retired-selector-{label}", document)


def test_session_template_without_selector_remains_a_valid_default_or_inheriting_template(tmp_path: Path) -> None:
    """A template that names no integration loads, whether it is the
    default one an operator configures or a child that inherits its
    parent's. Both spellings make the one claim, so they are one loop."""
    for label, spec in (("a-default", "env:\n    TERM: xterm-256color"), ("an-inheritor", "inherits: [parent]")):
        manifests = _load_one(
            tmp_path,
            f"no-selector-{label}",
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
        assert entry.resource.harness_integration is None, label


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
            placement: { mode: ssh, host: me@gpu-box }
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

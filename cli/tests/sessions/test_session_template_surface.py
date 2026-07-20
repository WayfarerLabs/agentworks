"""The Phase 4 template surface: the ``(harness, harness_config)`` pair,
the TOML hoist and its two conflict errors, the manifest flat-field
rejection, the pair-inheritance rules (FRD R5, including the multi-parent
divergence), and the harness reference / describe surfaces.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.harness import HARNESS_REGISTRY, Harness
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests
from agentworks.resources.inspect import describe_resource
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import resolve_from_dict

# -- a second registered harness, for the cross-harness R5 case --------------


class _FakeHarness(Harness):
    """A minimal second harness so the 'different harness' inheritance
    case (which needs two registered names) can be exercised without
    ``claude-code`` (unregistered until Phase 2)."""

    name = "fake"
    description = "test double harness"

    @classmethod
    def validate_config(cls, owner, config):  # type: ignore[no-untyped-def]
        return ()

    def start(self, ctx):  # type: ignore[no-untyped-def]
        return ""

    def restart(self, ctx):  # type: ignore[no-untyped-def]
        return ""

    def _probe_target(self, transport):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture()
def fake_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(HARNESS_REGISTRY, "fake", _FakeHarness)


def _config(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
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
        + dedent(body)
    )
    return load_config(cfg, warn_issues=False)


def _templates(config) -> dict[str, SessionTemplate]:  # type: ignore[no-untyped-def]
    from agentworks.resources.access import kind_dict

    return kind_dict(build_registry(config), "session-template")


# -- TOML hoist + the two conflict errors (FRD R6) ---------------------------


def test_flat_toml_hoists_to_the_shell_pair(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [session_templates.claude]
        command = "claude"
        restart_command = "claude --resume"
        required_commands = ["claude"]
        """,
    )
    tmpl = _templates(config)["claude"]
    assert tmpl.harness == "shell"
    assert tmpl.harness_config == {
        "command": "claude",
        "restart_command": "claude --resume",
        "required_commands": ["claude"],
    }


def test_nested_toml_harness_config_passes_through(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [session_templates.htop]
        harness = "shell"
        [session_templates.htop.harness_config]
        command = "htop"
        required_commands = ["htop"]
        """,
    )
    tmpl = _templates(config)["htop"]
    assert tmpl.harness == "shell"
    assert tmpl.harness_config == {"command": "htop", "required_commands": ["htop"]}


def test_undeclared_template_leaves_the_pair_none(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [session_templates.plain]
        description = "just a login shell"
        """,
    )
    tmpl = _templates(config)["plain"]
    assert tmpl.harness is None
    assert tmpl.harness_config is None


def test_flat_fields_with_non_shell_harness_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot combine with harness"):
        _config(
            tmp_path,
            """
            [session_templates.bad]
            harness = "claude-code"
            command = "claude"
            """,
        )


def test_flat_fields_with_explicit_harness_config_is_an_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="cannot combine with an explicit"):
        _config(
            tmp_path,
            """
            [session_templates.bad]
            command = "claude"
            [session_templates.bad.harness_config]
            command = "claude"
            """,
        )


def test_harness_config_without_harness_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="harness_config needs a harness"):
        _config(
            tmp_path,
            """
            [session_templates.bad]
            [session_templates.bad.harness_config]
            command = "claude"
            """,
        )


def test_unknown_shell_field_errors_at_load(tmp_path: Path) -> None:
    """The declared blob is shape-validated at load in TOML vocabulary."""
    with pytest.raises(ConfigError, match="unknown shell harness field"):
        _config(
            tmp_path,
            """
            [session_templates.bad]
            harness = "shell"
            [session_templates.bad.harness_config]
            nope = "x"
            """,
        )


# -- manifest flat-field rejection + unknown-name miss policy (FRD R2) -------


def _manifest(tmp_path: Path, text: str) -> Path:
    root = tmp_path / "resources"
    root.mkdir(parents=True, exist_ok=True)
    (root / "res.yaml").write_text(dedent(text))
    return root


def test_manifest_flat_field_is_rejected(tmp_path: Path) -> None:
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: claude
        spec:
          command: claude
        """,
    )
    with pytest.raises(ConfigError, match="move them under spec.harness_config"):
        load_manifests(root)


def test_manifest_unknown_harness_name_errors_at_finalize(tmp_path: Path) -> None:
    """A typo'd (or not-yet-registered, e.g. claude-code) harness name is
    a valid reference shape at load; the kind's error miss policy reports
    it at finalize, naming the template."""
    cfg = tmp_path / "config.toml"
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg.write_text(
        f'[operator]\nssh_public_key = "{pub.as_posix()}"\n'
        f'ssh_private_key = "{priv.as_posix()}"\n'
    )
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: typo
        spec:
          harness: shel
        """,
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(
        ConfigError, match="'typo' references unknown harness 'shel'"
    ):
        build_registry(config)


# -- pair inheritance (FRD R5) -----------------------------------------------


def test_child_same_harness_merges_child_wins_and_unions_required() -> None:
    templates = {
        "base": SessionTemplate(
            name="base",
            harness="shell",
            harness_config={"command": "claude", "required_commands": ["claude"]},
        ),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            harness="shell",
            harness_config={
                "command": "claude --resume",
                "required_commands": ["rg"],
            },
        ),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness == "shell"
    assert resolved.harness_config["command"] == "claude --resume"  # child wins
    assert resolved.harness_config["required_commands"] == ["claude", "rg"]  # union


def test_child_silent_inherits_the_pair_unchanged() -> None:
    templates = {
        "base": SessionTemplate(
            name="base", harness="shell", harness_config={"command": "claude"}
        ),
        "child": SessionTemplate(name="child", inherits=["base"]),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness == "shell"
    assert resolved.harness_config == {"command": "claude"}


def test_child_different_harness_starts_fresh(fake_harness: None) -> None:
    """A child naming a DIFFERENT harness starts from an empty blob; the
    parent's blob was addressed to the wrong capability and never leaks."""
    templates = {
        "base": SessionTemplate(
            name="base", harness="shell", harness_config={"command": "sh-cmd"}
        ),
        "child": SessionTemplate(
            name="child", inherits=["base"], harness="fake",
            harness_config={"k": "v"},
        ),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness == "fake"
    assert resolved.harness_config == {"k": "v"}  # no leak of the shell blob


def test_multi_parent_silent_parent_does_not_wipe(tmp_path: Path) -> None:
    """The pinned divergence from today's multi-parent semantics (FRD
    R5): a later harness-silent parent no longer wipes an earlier
    parent's command. Under the old flat-scalar merge, ``env-only``
    would have reset the command to empty."""
    config = _config(
        tmp_path,
        """
        [session_templates.has-command]
        command = "run-me"

        [session_templates.env-only]
        [session_templates.env-only.env]
        FOO = "bar"

        [session_templates.child]
        inherits = ["has-command", "env-only"]
        """,
    )
    from agentworks.sessions.templates import resolve_template

    resolved = resolve_template(build_registry(config), "child")
    assert resolved.harness == "shell"
    assert resolved.harness_config == {"command": "run-me"}
    assert resolved.env["FOO"].value == "bar"


def test_undeclared_default_resolves_to_shell_empty() -> None:
    resolved = resolve_from_dict({}, None)
    assert resolved.name == "default"
    assert resolved.harness == "shell"
    assert resolved.harness_config == {}


# -- describe / reference surfaces (FRD R2, R8) ------------------------------


def test_declared_harness_emits_a_reference() -> None:
    tmpl = SessionTemplate(
        name="claude", harness="shell", harness_config={"command": "claude"}
    )
    refs = tmpl.referenced_resources()
    harness_refs = [r for r in refs if r.kind == "harness"]
    assert len(harness_refs) == 1
    assert harness_refs[0].name == "shell"
    assert harness_refs[0].usage == "the session harness"


def test_undeclared_harness_emits_no_reference() -> None:
    tmpl = SessionTemplate(name="plain")
    assert [r for r in tmpl.referenced_resources() if r.kind == "harness"] == []


def test_harness_row_lists_its_declaring_template(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [session_templates.htop]
        harness = "shell"
        [session_templates.htop.harness_config]
        command = "htop"
        """,
    )
    registry = build_registry(config)
    desc = describe_resource(registry, "harness", "shell")
    sources = {entry.source for entry in desc.references}
    assert ("session-template", "htop") in sources

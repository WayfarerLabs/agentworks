"""The template surface: the internal
``(harness_integration, harness_integration_config)`` pair,
the TOML hoist and its two conflict errors, the manifest flat-field
rejection, the pair-inheritance rules (FRD R5, including the multi-parent
divergence), and the harness-integration reference / describe surfaces.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY, HarnessIntegration
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests
from agentworks.resources.graph import BuildContext
from agentworks.resources.inspect import describe_resource
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import resolve_from_dict

# -- a second registered harness integration, for the cross-integration R5 case --


class _FakeHarnessIntegration(HarnessIntegration):
    """A minimal second harness integration so the 'different integration' inheritance
    case (which needs two registered names) can be exercised without
    ``claude-code`` (unregistered until Phase 2)."""

    name = "fake"
    description = "test double harness"

    @classmethod
    def dependencies(cls, owner, config):  # type: ignore[no-untyped-def]
        return ()

    @classmethod
    def validate(cls, owner, config):  # type: ignore[no-untyped-def]
        return None

    def start(self, ctx):  # type: ignore[no-untyped-def]
        return ""

    def resume(self, ctx):  # type: ignore[no-untyped-def]
        return ""

    def _probe_target(self, transport):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture()
def fake_harness_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(HARNESS_INTEGRATION_REGISTRY, "fake", _FakeHarnessIntegration)


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


# The legacy flat-TOML session-template hoist and its conflict errors are no
# longer reachable on the normal load path: config.toml is settings-only (ADR
# 0022), so a ``[session_templates.*]`` section hard-errors as a resource
# section. That reader (``_session_harness_integration_pair`` and the
# ``_load_session_templates`` normalization around it) relocated verbatim to
# the migrator's pre-side oracle, so the hoist / conflict pins re-point there,
# testing the same code that used to run at load. The restart_command
# deprecation those tests also asserted now surfaces on the manifest channel
# (``test_yaml_restart_command_warns_and_normalizes``) and the migrator
# rewrite (``test_resource_migrate.py``); the row's ``restart_command_compat``
# flag preserves the "this used the deprecated spelling" pin here.


def _oracle_row(tmp_path: Path, body: str, name: str) -> SessionTemplate:
    """The flat-TOML session template ``name``, read through the migrator's
    oracle."""
    from typing import cast

    from agentworks.migrate.toml_resources import toml_resource_rows

    cfg = tmp_path / "legacy.toml"
    cfg.write_text(dedent(body))
    return cast("SessionTemplate", toml_resource_rows(cfg)[("session-template", name)])


def _oracle_rows(tmp_path: Path, body: str) -> object:
    """Run the migrator oracle over ``body`` (the raising path for the
    conflict pins)."""
    from agentworks.migrate.toml_resources import toml_resource_rows

    cfg = tmp_path / "legacy.toml"
    cfg.write_text(dedent(body))
    return toml_resource_rows(cfg)


def _pair(body: str, name: str) -> tuple[str | None, dict[str, object] | None, bool]:
    """The raw ``(harness_integration, harness_integration_config,
    used_old_selector)`` triple for one flat-TOML template, before the
    ``_load_session_templates`` restart_command normalization."""
    import tomllib

    from agentworks.migrate.toml_resources import _session_harness_integration_pair

    data = tomllib.loads(dedent(body))
    return _session_harness_integration_pair(name, data["session_templates"][name])  # type: ignore[index]


# -- TOML hoist + the two conflict errors (FRD R6) ---------------------------


def test_flat_toml_hoists_to_the_shell_pair(tmp_path: Path) -> None:
    tmpl = _oracle_row(
        tmp_path,
        """
        [session_templates.claude]
        command = "claude"
        restart_command = "claude --resume"
        required_commands = ["claude"]
        """,
        "claude",
    )
    assert tmpl.harness_integration == "shell"
    assert tmpl.harness_integration_config == {
        "command": "claude",
        "resume_command": "claude --resume",
        "required_commands": ["claude"],
    }
    # The deprecated restart_command spelling was hoisted and normalized to
    # resume_command; the compat flag records that it was used.
    assert tmpl.restart_command_compat is True


def test_flat_toml_resume_command_is_canonical(tmp_path: Path) -> None:
    tmpl = _oracle_row(
        tmp_path,
        """
        [session_templates.claude]
        command = "claude"
        resume_command = "claude --resume"
        """,
        "claude",
    )
    assert tmpl.harness_integration_config == {
        "command": "claude",
        "resume_command": "claude --resume",
    }
    assert tmpl.restart_command_compat is False


def test_local_resume_and_restart_command_conflict(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="resume_command and restart_command cannot be combined"):
        _oracle_rows(
            tmp_path,
            """
            [session_templates.bad]
            resume_command = "new"
            restart_command = "old"
            """,
        )


def test_nested_toml_resume_and_restart_command_conflict(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="resume_command and restart_command cannot be combined"):
        _oracle_rows(
            tmp_path,
            """
            [session_templates.bad]
            harness_integration = "shell"
            [session_templates.bad.harness_integration_config]
            resume_command = "new"
            restart_command = "old"
            """,
        )


def test_nested_toml_harness_config_passes_through(tmp_path: Path) -> None:
    tmpl = _oracle_row(
        tmp_path,
        """
        [session_templates.htop]
        harness_integration = "shell"
        [session_templates.htop.harness_integration_config]
        command = "htop"
        required_commands = ["htop"]
        """,
        "htop",
    )
    assert tmpl.harness_integration == "shell"
    assert tmpl.harness_integration_config == {"command": "htop", "required_commands": ["htop"]}


def test_canonical_toml_harness_integration_pair_normalizes_to_internal_pair(tmp_path: Path) -> None:
    integration, config, used_old_selector = _pair(
        """
        [session_templates.htop]
        harness_integration = "shell"
        [session_templates.htop.harness_integration_config]
        command = "htop"
        required_commands = ["htop"]
        """,
        "htop",
    )
    assert integration == "shell"
    assert config == {"command": "htop", "required_commands": ["htop"]}
    # The canonical spelling is not flagged as a deprecated selector.
    assert used_old_selector is False


def test_toml_harness_old_and_canonical_pairs_cannot_mix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="old and new harness integration selector/config fields cannot be mixed"):
        _oracle_rows(
            tmp_path,
            """
            [session_templates.bad]
            harness = "shell"
            harness_integration = "shell"
            """,
        )


def test_undeclared_template_leaves_the_pair_none(tmp_path: Path) -> None:
    tmpl = _oracle_row(
        tmp_path,
        """
        [session_templates.plain]
        description = "just a login shell"
        """,
        "plain",
    )
    assert tmpl.harness_integration is None
    assert tmpl.harness_integration_config is None


def test_flat_fields_with_non_shell_harness_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot combine with harness"):
        _oracle_rows(
            tmp_path,
            """
            [session_templates.bad]
            harness_integration = "claude-code"
            command = "claude"
            """,
        )


def test_flat_fields_with_explicit_harness_config_is_an_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="cannot combine with an explicit"):
        _oracle_rows(
            tmp_path,
            """
            [session_templates.bad]
            command = "claude"
            [session_templates.bad.harness_config]
            command = "claude"
            """,
        )


def test_harness_config_without_harness_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="harness_config needs a selector"):
        _oracle_rows(
            tmp_path,
            """
            [session_templates.bad]
            [session_templates.bad.harness_config]
            command = "claude"
            """,
        )


def test_unknown_shell_field_errors_at_build(tmp_path: Path) -> None:
    """The declared blob is shape-validated by the finalize ``validate``
    pass (R3), so a malformed shell block fails at build_registry, not at
    load. The error keeps the harness-integration vocabulary and gains the source
    location (re-attached from the resource origin, the manifest file now)."""
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: bad
        spec:
          harness_integration:
            name: shell
            nope: x
        """,
    )
    config = _config(tmp_path, "")
    with pytest.raises(ConfigError, match="unknown shell harness integration field") as exc:
        build_registry(config, load_manifests(root))
    assert "res.yaml" in str(exc.value)


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
    with pytest.raises(ConfigError, match="move them into a spec.harness_integration tagged table"):
        load_manifests(root)


def test_manifest_unknown_harness_name_errors_at_finalize(tmp_path: Path) -> None:
    """A typo'd (or not-yet-registered, e.g. claude-code) harness integration name is
    a valid reference shape at load; the kind's error miss policy reports
    it at finalize, naming the template."""
    cfg = tmp_path / "config.toml"
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg.write_text(f'[operator]\nssh_public_key = "{pub.as_posix()}"\nssh_private_key = "{priv.as_posix()}"\n')
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
    with pytest.raises(ConfigError, match="'typo' references unknown harness-integration 'shel'"):
        build_registry(config)


# -- pair inheritance (FRD R5) -----------------------------------------------


def test_child_same_harness_integration_merges_child_wins_and_unions_required() -> None:
    templates = {
        "base": SessionTemplate(
            name="base",
            harness_integration="shell",
            harness_integration_config={"command": "claude", "required_commands": ["claude"]},
        ),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            harness_integration="shell",
            harness_integration_config={
                "command": "claude --resume",
                "required_commands": ["rg"],
            },
        ),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config["command"] == "claude --resume"  # child wins
    assert resolved.harness_integration_config["required_commands"] == ["claude", "rg"]  # union


def test_child_silent_inherits_the_pair_unchanged() -> None:
    templates = {
        "base": SessionTemplate(
            name="base", harness_integration="shell", harness_integration_config={"command": "claude"}
        ),
        "child": SessionTemplate(name="child", inherits=["base"]),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {"command": "claude"}


@pytest.mark.parametrize(("parent_old", "child_old"), [(True, False), (False, True)])
def test_inheritance_rejects_mixed_resume_spellings(parent_old: bool, child_old: bool) -> None:
    templates = {
        "base": SessionTemplate(
            name="base",
            harness_integration="shell",
            harness_integration_config={"resume_command": "parent"},
            restart_command_compat=parent_old,
        ),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            harness_integration="shell",
            harness_integration_config={"resume_command": "child"},
            restart_command_compat=child_old,
        ),
    }
    with pytest.raises(ConfigError, match="inheritance cannot combine"):
        resolve_from_dict(templates, "child")


def test_old_parent_with_unrelated_child_override_normalizes() -> None:
    templates = {
        "base": SessionTemplate(
            name="base",
            harness_integration="shell",
            harness_integration_config={"resume_command": "parent"},
            restart_command_compat=True,
        ),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            harness_integration="shell",
            harness_integration_config={"command": "child"},
        ),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration_config == {
        "command": "child",
        "resume_command": "parent",
    }


def test_yaml_restart_command_warns_and_normalizes(tmp_path: Path) -> None:
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: old-shell
        spec:
          harness_integration:
            name: shell
            restart_command: old-resume
        """,
    )
    manifests = load_manifests(root)
    assert len(manifests.deprecation_issues) == 1
    assert "restart_command is deprecated; use resume_command instead" in manifests.deprecation_issues[0]
    template = manifests.entries[0].resource
    assert template.harness_integration_config == {"resume_command": "old-resume"}


def test_tagged_yaml_resume_and_restart_command_conflict(tmp_path: Path) -> None:
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: bad
        spec:
          harness_integration:
            name: shell
            resume_command: new
            restart_command: old
        """,
    )
    with pytest.raises(ConfigError, match="resume_command and restart_command cannot be combined"):
        load_manifests(root)


# The flat-TOML twin of ``test_yaml_inheritance_through_loader_and_registry``
# is gone: config.toml no longer loads ``[session_templates.*]`` (ADR 0022), so
# a TOML-declared template can never reach build_registry / resolve_template.
# The manifest sibling below covers the identical restart/resume/command
# inheritance matrix, and the deprecated-spelling inheritance conflict itself is
# pinned by ``test_inheritance_rejects_mixed_resume_spellings``.


@pytest.mark.parametrize(
    ("parent_field", "child_field", "errors"),
    [
        ("restart_command", "resume_command", True),
        ("resume_command", "restart_command", True),
        ("restart_command", "command", False),
    ],
)
def test_yaml_inheritance_through_loader_and_registry(
    tmp_path: Path,
    parent_field: str,
    child_field: str,
    errors: bool,
) -> None:
    root = _manifest(
        tmp_path,
        f"""
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: parent
        spec:
          harness_integration:
            name: shell
            {parent_field}: parent
        ---
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: child
        spec:
          inherits: [parent]
          harness_integration:
            name: shell
            {child_field}: child
        """,
    )
    config = _config(tmp_path, "")
    registry = build_registry(config, load_manifests(root))
    from agentworks.sessions.templates import resolve_template

    if errors:
        with pytest.raises(ConfigError, match="inheritance cannot combine"):
            resolve_template(registry, "child")
    else:
        resolved = resolve_template(registry, "child")
        assert resolved.harness_integration_config == {
            "command": "child",
            "resume_command": "parent",
        }


def test_child_different_harness_integration_starts_fresh(fake_harness_integration: None) -> None:
    """A child naming a DIFFERENT harness integration starts from an empty blob; the
    parent's blob was addressed to the wrong capability and never leaks."""
    templates = {
        "base": SessionTemplate(
            name="base", harness_integration="shell", harness_integration_config={"command": "sh-cmd"}
        ),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            harness_integration="fake",
            harness_integration_config={"k": "v"},
        ),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration == "fake"
    assert resolved.harness_integration_config == {"k": "v"}  # no leak of the shell blob


def test_multi_parent_silent_parent_does_not_wipe(tmp_path: Path) -> None:
    """The pinned divergence from today's multi-parent semantics (FRD
    R5): a later harness-integration-silent parent no longer wipes an earlier
    parent's command. Under the old flat-scalar merge, ``env-only``
    would have reset the command to empty."""
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: has-command
        spec:
          harness_integration:
            name: shell
            command: run-me
        ---
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: env-only
        spec:
          env:
            FOO: bar
        ---
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: child
        spec:
          inherits: [has-command, env-only]
        """,
    )
    config = _config(tmp_path, "")
    from agentworks.sessions.templates import resolve_template

    resolved = resolve_template(build_registry(config, load_manifests(root)), "child")
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {"command": "run-me"}
    assert resolved.env["FOO"].value == "bar"


def test_undeclared_default_resolves_to_shell_empty() -> None:
    resolved = resolve_from_dict({}, None)
    assert resolved.name == "default"
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {}


# -- describe / reference surfaces (FRD R2, R8) ------------------------------


def test_declared_harness_integration_emits_a_reference() -> None:
    tmpl = SessionTemplate(name="claude", harness_integration="shell", harness_integration_config={"command": "claude"})
    refs = tmpl.dependencies(BuildContext())
    harness_refs = [r for r in refs if r.kind == "harness-integration"]
    assert len(harness_refs) == 1
    assert harness_refs[0].name == "shell"
    assert harness_refs[0].usage == "the session harness integration"


def test_undeclared_harness_integration_emits_no_reference() -> None:
    tmpl = SessionTemplate(name="plain")
    assert [r for r in tmpl.dependencies(BuildContext()) if r.kind == "harness-integration"] == []


def test_harness_integration_row_lists_its_declaring_template(tmp_path: Path) -> None:
    root = _manifest(
        tmp_path,
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
    config = _config(tmp_path, "")
    registry = build_registry(config, load_manifests(root))
    desc = describe_resource(registry, "harness-integration", "shell")
    sources = {entry.source for entry in desc.references}
    assert ("session-template", "htop") in sources

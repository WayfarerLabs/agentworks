"""Native carriers retain artifact semantics and refuse ambiguous delivery."""

from __future__ import annotations

import hashlib
import json
import shlex
import tomllib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.artifacts.application import ArtifactApplication, OwnedArtifactFile, SessionArtifactContext
from agentworks.artifacts.model import (
    ArtifactContent,
    ArtifactInput,
    ArtifactMember,
    ArtifactOrigin,
    ArtifactProvenance,
    ArtifactType,
)
from agentworks.artifacts.native.common import native_home, validate_ancestor_names
from agentworks.artifacts.native.probe import probe_native
from agentworks.artifacts.native.shell import shell_artifacts
from agentworks.artifacts.publication import validate_application
from agentworks.artifacts.session import validate_session_application
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.plugins.codex import artifacts as codex
from agentworks.plugins.codex.harness_integration import CodexIntegration
from agentworks.plugins.grok import artifacts as grok
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from tests.artifacts._fixtures import received


def artifact(
    type: ArtifactType, *, name: str = "review", owner: str = "s1", options: dict | None = None
) -> ArtifactInput:
    members: tuple[ArtifactMember, ...] = ()
    if type is ArtifactType.SKILL:
        members = (
            ArtifactMember("SKILL.md", b"---\nname: review\ndescription: Review changes\n---\nUse this skill.\n"),
            ArtifactMember("scripts/check.sh", b"#!/bin/sh\nexit 0\n", executable=True),
            ArtifactMember("data/logo.bin", bytes(range(256))),
        )
    return ArtifactInput(
        ArtifactContent(
            type,
            name,
            description="Review changes",
            text="before {{session_name}} after\n",
            members=members,
            native_options_json=json.dumps(options or {}),
        ),
        ArtifactProvenance(),
        ArtifactOrigin("session", "session", owner, bundle="team", entry=name),
    )


def context(*inputs: ArtifactInput, ancestors: tuple[OwnedArtifactFile, ...] = ()) -> SessionArtifactContext:
    return SessionArtifactContext(
        inputs=received(*inputs),
        home="/home/alice",
        directory="/home/alice/.agentworks-artifacts/session/u/r",
        session_uuid="u",
        run_id="r",
        ancestor_files=ancestors,
    )


def owned(file, *, origins: tuple[str, ...] | None = None) -> OwnedArtifactFile:
    return OwnedArtifactFile(
        path=file.path,
        sha256=hashlib.sha256(file.data).hexdigest(),
        origins=origins or file.origins,
        native_identity=file.native_identity,
    )


@pytest.mark.parametrize("render", [claude.outer_artifacts, grok.outer_artifacts])
def test_outer_skills_preserve_complete_package_bytes_and_executable_intent(render):
    item = artifact(ArtifactType.SKILL)
    result = render(received(item), "/home/alice/.native")
    assert [(file.path.rsplit("/review/", 1)[1], file.data, file.executable) for file in result.files] == [
        (member.path, member.data, member.executable) for member in item.content.members
    ]
    assert {file.native_identity for file in result.files} == {"skill:review"}
    assert not result.deferred


@pytest.mark.parametrize("render", [claude.outer_artifacts, grok.outer_artifacts])
def test_rules_are_unconditional_and_hints_do_not_collide_with_named_rule(render):
    hint = artifact(ArtifactType.HINT)
    rule = artifact(ArtifactType.RULE, name="hints")
    result = render(received(hint, rule), "/native")
    assert len({file.path for file in result.files}) == 2
    assert all(file.data.endswith(hint.content.text.encode()) for file in result.files)
    assert all(file.path.count("/") == 3 for file in result.files)


def test_shell_index_has_only_this_run_inputs_and_private_relative_paths():
    item = artifact(ArtifactType.SKILL)
    result = shell_artifacts(received(item), "/private/run", session=True)
    index = json.loads(next(file.data for file in result.files if file.path.endswith("/index.json")))
    assert index["groups"][0]["skills"]["review"]["files"] == [
        f"scopes/session/s1/skills/review/{member.path}" for member in item.content.members
    ]
    assert index["groups"][0]["skills"]["review"]["origin"]["resource_name"] == "s1"
    assert result.artifacts_dir == "/private/run"
    assert shell_artifacts(received(item), "/outer").artifacts_dir is None
    assert shell_artifacts(received(), "/private/run", session=True) == ArtifactApplication()


@pytest.mark.parametrize(
    "directory", [42, (), "", "relative/run", "/private/../run", "/private//run", "/private/\x00run"]
)
def test_plugin_artifact_directory_requires_an_absolute_normalized_path(directory):
    with pytest.raises(StateError):
        validate_application(ArtifactApplication(artifacts_dir=directory), received(), "session", integration="fixture")


@pytest.mark.parametrize("facet", ["vm", "user", "workspace"])
def test_outer_facet_cannot_supply_a_session_artifact_directory(facet):
    with pytest.raises(StateError):
        validate_application(
            ArtifactApplication(artifacts_dir="/private/run"), received(), facet, integration="fixture"
        )


def test_session_artifact_directory_is_bound_to_the_prepared_run():
    prepared = context(artifact(ArtifactType.HINT))
    application = shell_artifacts(prepared.inputs, prepared.directory, session=True)
    assert validate_session_application(application, prepared, integration="shell") is application
    for directory in ("/outside", prepared.directory + "/nested", prepared.directory + "-other"):
        with pytest.raises(StateError):
            validate_session_application(replace(application, artifacts_dir=directory), prepared, integration="shell")


def test_codex_outer_routes_context_and_installs_standard_skills_and_personas():
    items = tuple(artifact(type, name=type.value) for type in ArtifactType)
    result = codex.outer_artifacts(
        received(*items), skills_root="/home/a/.agents/skills", agents_root="/home/a/.codex/agents"
    )
    assert {entry.input_id for entry in result.deferred} == {item.identity for item in items[:2]}
    assert {entry.destination for entry in result.deferred} == {"session"}
    role = tomllib.loads(next(file.data.decode() for file in result.files if file.path.endswith(".toml")))
    assert role["developer_instructions"] == items[-1].content.text
    assert role["name"] == "agent"


@pytest.mark.parametrize(
    "render,option",
    [
        (claude.session_artifacts, "--append-system-prompt-file"),
        (grok.session_artifacts, "--rules"),
        (codex.session_artifacts, "-c"),
    ],
)
def test_session_guidance_adds_to_configured_text_without_making_it_an_initial_prompt(render, option):
    item = artifact(ArtifactType.RULE)
    result = render(context(item), configured="existing setup", extra_args=[])
    assert option in result.argv
    expected = "existing setup\n\n" + item.content.text
    if render is codex.session_artifacts:
        value = result.argv[result.argv.index("-c") + 1]
        assert tomllib.loads(value)["developer_instructions"] == expected
        assert result.application.files == ()
    elif render is grok.session_artifacts:
        assert result.argv[result.argv.index("--rules") + 1] == expected
        assert result.application.files == ()
    else:
        assert result.application.files[0].data.decode() == expected
        assert result.argv[result.argv.index("--append-system-prompt-file") + 1] == result.application.files[0].path
        assert result.argv[-2:] == ("--system-prompt-snapshot", "off")


def test_claude_session_plugin_uses_native_namespace_and_private_directory():
    item = artifact(ArtifactType.SKILL)
    result = claude.session_artifacts(context(item), configured=None, extra_args=[])
    plugin_path = result.argv[result.argv.index("--plugin-dir") + 1]
    manifest = json.loads(next(file.data for file in result.application.files if file.path.endswith("plugin.json")))
    assert manifest["name"] == "agentworks-artifacts"
    assert all(file.path.startswith(plugin_path + "/") for file in result.application.files)
    assert {file.native_identity for file in result.application.files if file.native_identity} == {
        "skill:agentworks-artifacts:review"
    }


@pytest.mark.parametrize("render", [codex.session_artifacts, grok.session_artifacts])
def test_private_skills_stay_unhandled_when_native_cli_cannot_discover_them(render):
    item = artifact(ArtifactType.SKILL)
    result = render(context(item), configured=None, extra_args=[])
    assert result.application.files == ()
    assert result.application.deferred[0].input_id == item.identity
    assert result.application.deferred[0].destination == "session"
    assert result.argv == ()


@pytest.mark.parametrize("render", [claude.session_artifacts, codex.session_artifacts, grok.session_artifacts])
def test_native_persona_options_do_not_enable_hooks_or_unknown_native_fields(render):
    name = {
        claude.session_artifacts: "claude-code",
        codex.session_artifacts: "codex",
        grok.session_artifacts: "grok-build",
    }[render]
    item = artifact(ArtifactType.AGENT, options={name: {"hooks": {}}})
    with pytest.raises(ConfigError):
        render(context(item), configured=None, extra_args=[])


@pytest.mark.parametrize(
    "render,args",
    [
        (claude.session_artifacts, ["--append-system-prompt=override"]),
        (claude.session_artifacts, ["--disable-slash-commands"]),
        (codex.session_artifacts, ['-cdeveloper_instructions="override"']),
        (codex.session_artifacts, ["--config", 'agents.review.config_file="/other"']),
        (grok.session_artifacts, ["--append-system-prompt", "override"]),
        (grok.session_artifacts, ["--no-subagents"]),
    ],
)
def test_raw_native_overrides_cannot_replace_artifact_carrier(render, args):
    with pytest.raises(ConfigError):
        render(context(artifact(ArtifactType.RULE)), configured=None, extra_args=args)


def test_native_name_collision_does_not_silently_shadow_another_scope():
    item = artifact(ArtifactType.AGENT)
    other = replace(item, origin=ArtifactOrigin("agent", "agent", "alice", bundle="another", entry="review"))
    earlier = claude.outer_artifacts(received(other), "/home/alice/.claude")
    with pytest.raises(ConfigError):
        claude.session_artifacts(
            context(item, ancestors=tuple(owned(file) for file in earlier.files)), configured=None, extra_args=[]
        )
    # Multiple members of the same skill are a single native claim.
    skill = claude.outer_artifacts(received(artifact(ArtifactType.SKILL)), "/home/alice/.claude")
    validate_ancestor_names(context(ancestors=tuple(owned(file) for file in skill.files)), ArtifactApplication())


def test_ancestor_guidance_also_disables_claude_saved_prompt_on_resume():
    files = claude.outer_artifacts(received(artifact(ArtifactType.RULE)), "/home/alice/.claude").files
    result = claude.session_artifacts(
        context(ancestors=tuple(owned(file) for file in files)), configured=None, extra_args=[]
    )
    assert result.application.files == ()
    assert result.argv == ("--system-prompt-snapshot", "off")


@pytest.mark.parametrize("root", ["relative", "/home/a/../b", "/home//a", "/home/a/"])
def test_native_home_override_must_be_normalized(root):
    with pytest.raises(ConfigError):
        native_home("/home/a", {"NATIVE_HOME": root}, "NATIVE_HOME", ".native")


def test_native_home_override_is_used_instead_of_default():
    assert native_home("/home/a", {"NATIVE_HOME": "/home/a/custom"}, "NATIVE_HOME", ".native") == "/home/a/custom"


def test_native_probe_passes_environment_separately_and_rejects_policy_failures():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="AGW_ARTIFACT_PROBE="
            + json.dumps(
                {"native_home": "/home/a/.claude", "problems": ["native-discovery-exclusions"], "inventory": []}
            ),
        )

    with pytest.raises(StateError):
        probe_native(
            SimpleNamespace(run=run), tool="claude", home="/home/a", environment={"API_TOKEN": "a-secret-value"}
        )
    assert "a-secret-value" not in calls[0][0]
    assert calls[0][1]["env"]["API_TOKEN"] == "a-secret-value"


@pytest.mark.parametrize(
    "implementation", [ShellIntegration, ClaudeCodeIntegration, CodexIntegration, GrokBuildIntegration]
)
def test_vm_deferral_preserves_input_identity_without_descendant_knowledge(implementation):
    integration = implementation.for_setup(owner_name="box", owner_kind="vm", facet="vm", config={})
    item = artifact(ArtifactType.RULE)
    result = integration.vm_init(SimpleNamespace(artifacts=received(item)))
    assert result.files == ()
    assert result.deferred[0].input_id == item.identity
    assert result.deferred[0].destination == ("session" if implementation is ShellIntegration else "user")


@pytest.mark.parametrize(
    "implementation,configured",
    [
        (ClaudeCodeIntegration, {"append_system_prompt": "configured guidance"}),
        (GrokBuildIntegration, {"rules": "configured guidance"}),
    ],
)
@pytest.mark.parametrize("resume", [False, True])
def test_native_launch_carries_literal_artifact_guidance_on_new_and_resumed_threads(
    monkeypatch, implementation, configured, resume
):
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from tests.conftest import _FakeTarget

    module = __import__(implementation.__module__, fromlist=["probe_native"])
    monkeypatch.setattr(module, "probe_native", lambda *args, **kwargs: "/home/alice/.native")
    item = artifact(ArtifactType.RULE)
    integration = implementation(
        "native",
        configured,
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={"session_id": "939b1597-7c61-5ace-80f4-14617b7b4257"},
        artifact_context=context(item),
    )
    method = "_transcript_exists" if implementation is ClaudeCodeIntegration else "_session_exists"
    monkeypatch.setattr(integration, method, lambda *_args: resume)
    result = integration.start(
        RunContext(admin_target=_FakeTarget()),
        intent=HarnessLaunchIntent.RESUME_OR_NEW if resume else HarnessLaunchIntent.CREATE,
    )
    assert "{{" not in result.command
    argv = shlex.split(shlex.split(result.command)[2])
    assert ("--resume" in argv) is resume
    if implementation is GrokBuildIntegration:
        assert argv[argv.index("--rules") + 1] == "configured guidance\n\n" + item.content.text
    else:
        assert "--append-system-prompt" not in argv
        assert argv[argv.index("--append-system-prompt-file") + 1] == result.artifacts.files[0].path
        assert result.artifacts.files[0].data.decode() == "configured guidance\n\n" + item.content.text


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("name", ["reviewer", "review-2", "123", "r" * 64])
def test_codex_literal_role_and_guidance_overrides_apply_on_resume_and_new(fresh, name):
    item = artifact(ArtifactType.RULE)
    persona = artifact(ArtifactType.AGENT, name=name)
    persona = replace(persona, content=replace(persona.content, description='Review "x=y".\n{{session_name}} café'))
    integration = CodexIntegration(
        "codex",
        {"developer_instructions": "configured guidance"},
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=context(item, persona),
    )
    integration._artifact_plan = codex.session_artifacts(
        context(item, persona), configured="configured guidance", extra_args=[]
    )
    argv_text = integration._codex_argv(() if fresh else ("resume", "thread-id"), fresh=fresh)
    assert "{{" not in argv_text
    argv = shlex.split(argv_text)
    values = [argv[index + 1] for index, token in enumerate(argv) if token == "-c"]
    # Codex splits the assignment at the first '=' and parses only its value as TOML:
    # https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/utils/cli/src/config_override.rs
    combined = {}
    for value in values:
        key, raw_value = value.split("=", 1)
        combined[key.strip()] = tomllib.loads("value=" + raw_value.strip())["value"]
    assert combined["developer_instructions"] == "configured guidance\n\n" + item.content.text
    assert {key for key in combined if key.startswith("agents.")} == {
        f"agents.{name}.config_file",
        f"agents.{name}.description",
    }
    assert combined[f"agents.{name}.description"] == persona.content.description
    path = combined[f"agents.{name}.config_file"]
    role = tomllib.loads(
        next(file.data.decode() for file in integration._artifact_plan.application.files if file.path == path)
    )
    assert role["developer_instructions"] == persona.content.text
    assert "name" not in role and "description" not in role


@pytest.mark.parametrize(
    "name",
    [
        "review.part",
        'review"er',
        "review=er",
        "review er",
        "Review",
        "review_er",
        "review--er",
        "-review",
        "review-",
        "r" * 65,
    ],
)
def test_codex_session_rejects_persona_names_that_cannot_use_portable_override_keys(name):
    with pytest.raises(ConfigError):
        codex.session_artifacts(context(artifact(ArtifactType.AGENT, name=name)), configured=None, extra_args=[])


@pytest.mark.parametrize(
    "render,type",
    [
        (codex.session_artifacts, ArtifactType.RULE),
        (grok.session_artifacts, ArtifactType.RULE),
        (claude.session_artifacts, ArtifactType.AGENT),
    ],
)
def test_large_native_argument_artifacts_fail_before_publication(render, type):
    item = artifact(type)
    item = replace(item, content=replace(item.content, text="x" * (128 * 1024)))
    with pytest.raises(ConfigError):
        render(context(item), configured=None, extra_args=[])


def test_large_claude_context_uses_private_file_without_an_argument_size_penalty():
    item = artifact(ArtifactType.RULE)
    item = replace(item, content=replace(item.content, text="x" * (128 * 1024)))
    result = claude.session_artifacts(context(item), configured=None, extra_args=[])
    assert len(result.application.files[0].data) == 128 * 1024
    assert sum(len(token) for token in result.argv) < 1024


def test_quoted_native_command_limit_accounts_for_shell_expansion():
    from agentworks.artifacts.native.common import validate_native_command
    from agentworks.capabilities.harness_integration import quote_literal_argv

    text = "'" * (16 * 1024)
    with pytest.raises(ConfigError):
        validate_native_command(quote_literal_argv(text))


@pytest.mark.parametrize(
    "implementation,module",
    [
        (ClaudeCodeIntegration, "agentworks.plugins.claude.harness_integration"),
        (CodexIntegration, "agentworks.plugins.codex.harness_integration"),
        (GrokBuildIntegration, "agentworks.plugins.grok.harness_integration"),
    ],
)
@pytest.mark.parametrize("outside_home", [False, True])
def test_user_native_home_boundary_precedes_other_setup(db, monkeypatch, implementation, module, outside_home):
    from importlib import import_module

    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation

    plugin = import_module(module)
    setup_calls = []
    if hasattr(plugin, "setup_user"):
        monkeypatch.setattr(plugin, "setup_user", lambda *args: setup_calls.append(args))
    root = "/mnt/native" if outside_home else "/home/alice/custom-native"
    monkeypatch.setattr(plugin, "probe_native", lambda *args, **kwargs: root)
    integration = implementation.for_setup(owner_kind="agent", owner_name="alice", facet="user", config={})
    invocation = UserSetupInvocation(
        vm=db.insert_vm("vm", "lima", "vm"),
        runner=SimpleNamespace(),
        prior=None,
        checkpoint=lambda claims: None,
        username="alice",
        home="/home/alice",
        artifacts=received(artifact(ArtifactType.AGENT)),
    )
    if outside_home:
        with pytest.raises(ConfigError):
            integration.user_init(invocation)
        assert setup_calls == []
    else:
        result = integration.user_init(invocation)
        assert result.files and all(file.path.startswith(root + "/") for file in result.files)
        assert len(setup_calls) == (0 if implementation is GrokBuildIntegration else 1)


def test_codex_external_home_does_not_block_home_owned_skills(db, monkeypatch):
    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
    from agentworks.plugins.codex import harness_integration as plugin

    monkeypatch.setattr(plugin, "setup_user", lambda *args: None)
    monkeypatch.setattr(plugin, "probe_native", lambda *args, **kwargs: "/mnt/native")
    integration = CodexIntegration.for_setup(owner_kind="agent", owner_name="alice", facet="user", config={})
    result = integration.user_init(
        UserSetupInvocation(
            vm=db.insert_vm("vm", "lima", "vm"),
            runner=SimpleNamespace(),
            prior=None,
            checkpoint=lambda claims: None,
            username="alice",
            home="/home/alice",
            artifacts=received(artifact(ArtifactType.SKILL), artifact(ArtifactType.HINT, name="setup")),
        )
    )
    assert all(file.path.startswith("/home/alice/.agents/skills/") for file in result.files)
    assert len(result.deferred) == 1

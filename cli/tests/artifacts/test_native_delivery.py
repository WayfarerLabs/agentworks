"""Native carriers retain artifact semantics and refuse ambiguous delivery."""

from __future__ import annotations

import hashlib
import json
import shlex
import tomllib
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import get_args

import pytest

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication, OwnedArtifactFile, SessionArtifactContext
from agentworks.artifacts.model import (
    ArtifactContent,
    ArtifactInput,
    ArtifactMember,
    ArtifactOrigin,
    ArtifactProvenance,
    ArtifactType,
)
from agentworks.artifacts.native.common import NativeSessionArtifacts, native_home, validate_ancestor_names
from agentworks.artifacts.native.probe import probe_native
from agentworks.artifacts.native.shell import shell_artifacts
from agentworks.artifacts.publication import validate_application
from agentworks.artifacts.session import validate_session_application
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.harness_integration import HarnessStart
from agentworks.capabilities.harness_integration.shell import ShellIntegration
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.plugins.codex import artifacts as codex
from agentworks.plugins.codex.harness_integration import CodexIntegration
from agentworks.plugins.grok import artifacts as grok
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from tests.artifacts._fixtures import received


def enabled(render: Callable[..., NativeSessionArtifacts]) -> list[str]:
    return {
        claude.session_artifacts: ["session-prompt", "session-skill-plugin", "session-agent-definitions"],
        codex.session_artifacts: ["session-developer-instructions", "session-agent-config"],
        grok.session_artifacts: ["session-rules", "session-agent-definitions"],
    }[render]


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
        package_root=file.package_root,
    )


@pytest.mark.parametrize("render", [claude.outer_artifacts, grok.outer_artifacts])
def test_outer_skills_preserve_complete_package_bytes_and_executable_intent(render):
    item = artifact(ArtifactType.SKILL)
    result = render(received(item), "/home/alice/.native")
    assert [(file.path.rsplit("/review/", 1)[1], file.data, file.executable) for file in result.files] == [
        (member.path, member.data, member.executable) for member in item.content.members
    ]
    assert {file.native_identity for file in result.files} == {"skill:review"}
    assert {file.package_root for file in result.files} == {"/home/alice/.native/skills/review"}
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


def test_codex_outer_installs_context_and_standard_skills_and_personas():
    items = tuple(artifact(type, name=type.value) for type in ArtifactType)
    result = codex.outer_artifacts(
        received(*items),
        skills_root="/home/a/.agents/skills",
        agents_root="/home/a/.codex/agents",
        instructions_path="/home/a/.codex/AGENTS.md",
    )
    assert result.deferred == ()
    guidance = next(file for file in result.files if file.path.endswith("/AGENTS.md"))
    assert guidance.generated_section
    assert guidance.origins == tuple(item.origin_identity for item in items[:2])
    assert all(item.content.text.encode() in guidance.data for item in items[:2])
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
    result = render(
        enabled_workarounds=enabled(render), context=context(item), configured="existing setup", extra_args=[]
    )
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
        assert "--system-prompt-snapshot" not in result.argv


def test_claude_session_plugin_uses_native_namespace_and_private_directory():
    item = artifact(ArtifactType.SKILL)
    result = claude.session_artifacts(
        enabled_workarounds=enabled(claude.session_artifacts), context=context(item), configured=None, extra_args=[]
    )
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
    result = render(enabled_workarounds=enabled(render), context=context(item), configured=None, extra_args=[])
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
        render(enabled_workarounds=enabled(render), context=context(item), configured=None, extra_args=[])


@pytest.mark.parametrize(
    "render,args",
    [
        (claude.session_artifacts, ["--append-system-prompt=override"]),
        (codex.session_artifacts, ['-cdeveloper_instructions="override"']),
        (grok.session_artifacts, ["--append-system-prompt", "override"]),
    ],
)
def test_raw_native_overrides_cannot_replace_artifact_carrier(render, args):
    with pytest.raises(ConfigError):
        render(
            enabled_workarounds=enabled(render),
            context=context(artifact(ArtifactType.RULE)),
            configured=None,
            extra_args=args,
        )


def test_native_name_collision_does_not_silently_shadow_another_scope():
    item = artifact(ArtifactType.AGENT)
    other = replace(item, origin=ArtifactOrigin("agent", "agent", "alice", bundle="another", entry="review"))
    earlier = claude.outer_artifacts(received(other), "/home/alice/.claude")
    with pytest.raises(ConfigError):
        claude.session_artifacts(
            enabled_workarounds=enabled(claude.session_artifacts),
            context=context(item, ancestors=tuple(owned(file) for file in earlier.files)),
            configured=None,
            extra_args=[],
        )
    # Multiple members of the same skill are a single native claim.
    skill = claude.outer_artifacts(received(artifact(ArtifactType.SKILL)), "/home/alice/.claude")
    validate_ancestor_names(context(ancestors=tuple(owned(file) for file in skill.files)), ArtifactApplication())


def test_ancestor_guidance_does_not_change_claude_saved_prompt_on_resume():
    files = claude.outer_artifacts(received(artifact(ArtifactType.RULE)), "/home/alice/.claude").files
    result = claude.session_artifacts(
        enabled_workarounds=enabled(claude.session_artifacts),
        context=context(ancestors=tuple(owned(file) for file in files)),
        configured=None,
        extra_args=[],
    )
    assert result.application.files == ()
    assert result.argv == ()


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
            ok=True,
            stderr="",
            stdout="AGW_ARTIFACT_PROBE="
            + json.dumps(
                {"native_home": "/home/a/.claude", "problems": ["native-discovery-exclusions"], "inventory": []}
            ),
        )

    with pytest.raises(StateError):
        probe_native(
            SimpleNamespace(run=run), tool="claude", home="/home/a", environment={"API_TOKEN": "a-secret-value"}
        )
    assert "a-secret-value" not in calls[-1][0]
    assert calls[-1][1]["env"]["API_TOKEN"] == "a-secret-value"


@pytest.mark.parametrize("implementation", [CodexIntegration, GrokBuildIntegration])
def test_vm_deferral_preserves_input_identity_without_descendant_knowledge(implementation):
    integration = implementation.for_setup(owner_name="box", owner_kind="vm", facet="vm", config={})
    item = artifact(ArtifactType.RULE)
    result = integration.vm_init(SimpleNamespace(artifacts=received(item)))
    assert result.files == ()
    assert result.deferred[0].input_id == item.identity
    assert result.deferred[0].destination == "user"


@pytest.mark.parametrize(
    "implementation,configured",
    [
        (ClaudeCodeIntegration, {"append_system_prompt": "configured guidance"}),
        (GrokBuildIntegration, {"rules": "configured guidance"}),
    ],
)
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("opted_in", [False, True])
def test_native_launch_carries_literal_artifact_guidance_on_new_and_resumed_threads(
    monkeypatch, implementation, configured, resume, opted_in
):
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from tests.conftest import _FakeTarget

    module = __import__(implementation.__module__, fromlist=["probe_native"])
    monkeypatch.setattr(module, "probe_native", lambda *args, **kwargs: "/home/alice/.native")
    item = artifact(ArtifactType.RULE)
    integration = implementation(
        "native",
        {
            **configured,
            "enabled_workarounds": (
                ["session-prompt" if implementation is ClaudeCodeIntegration else "session-rules"] if opted_in else []
            ),
        },
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
    if not opted_in:
        flag = "--rules" if implementation is GrokBuildIntegration else "--append-system-prompt"
        assert argv[argv.index(flag) + 1] == "configured guidance"
        assert result.artifacts.files == ()
        assert len(result.artifacts.deferred) == 1
    elif implementation is GrokBuildIntegration:
        assert argv[argv.index("--rules") + 1] == "configured guidance\n\n" + item.content.text
    else:
        assert "--append-system-prompt" not in argv
        assert argv[argv.index("--append-system-prompt-file") + 1] == result.artifacts.files[0].path
        assert result.artifacts.files[0].data.decode() == "configured guidance\n\n" + item.content.text


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("name", ["reviewer", "review-2", "123", "r" * 64])
@pytest.mark.parametrize("include_rule", [False, True])
def test_codex_literal_role_and_guidance_overrides_apply_on_resume_and_new(fresh, name, include_rule):
    item = artifact(ArtifactType.RULE)
    persona = artifact(ArtifactType.AGENT, name=name)
    persona = replace(
        persona,
        content=replace(persona.content, description='Review "developer_instructions=x".\n{{session_name}} café'),
    )
    artifacts = context(item, persona) if include_rule else context(persona)
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
        artifact_context=artifacts,
    )
    integration._artifact_plan = codex.session_artifacts(
        enabled_workarounds=enabled(codex.session_artifacts) if include_rule else ["session-agent-config"],
        context=artifacts,
        configured="configured guidance",
        extra_args=[],
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
    assert combined["developer_instructions"] == "configured guidance" + (
        "\n\n" + item.content.text if include_rule else ""
    )
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
        "/",
        "",
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
@pytest.mark.parametrize("kind", list(ArtifactType))
def test_normalized_artifact_names_reject_noncanonical_names_before_native_delivery(name, kind):
    with pytest.raises(ValueError):
        artifact(kind, name=name)


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
        render(enabled_workarounds=enabled(render), context=context(item), configured=None, extra_args=[])


def test_large_claude_context_uses_private_file_without_an_argument_size_penalty():
    item = artifact(ArtifactType.RULE)
    item = replace(item, content=replace(item.content, text="x" * (128 * 1024)))
    result = claude.session_artifacts(
        enabled_workarounds=enabled(claude.session_artifacts), context=context(item), configured=None, extra_args=[]
    )
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
            artifacts=received(artifact(ArtifactType.SKILL)),
        )
    )
    assert all(file.path.startswith("/home/alice/.agents/skills/") for file in result.files)
    assert result.deferred == ()


def test_codex_persona_serialization_rejects_output_beyond_inventory_bound(monkeypatch):
    item = artifact(ArtifactType.AGENT)
    rendered = codex.outer_artifacts(
        received(item), skills_root="/skills", agents_root="/agents", instructions_path="/AGENTS.md"
    )
    limit = len(rendered.files[0].data)
    monkeypatch.setattr(codex, "MAX_CODEX_PERSONA_BYTES", limit)
    assert (
        codex.outer_artifacts(
            received(item), skills_root="/skills", agents_root="/agents", instructions_path="/AGENTS.md"
        )
        == rendered
    )
    monkeypatch.setattr(codex, "MAX_CODEX_PERSONA_BYTES", limit - 1)
    with pytest.raises(ConfigError):
        codex.outer_artifacts(
            received(item), skills_root="/skills", agents_root="/agents", instructions_path="/AGENTS.md"
        )


def test_shell_index_preserves_declared_key_order_with_fixed_type_order():
    inputs = received(
        artifact(ArtifactType.RULE, name="zebra"),
        artifact(ArtifactType.RULE, name="alpha"),
        artifact(ArtifactType.HINT, name="zebra"),
        artifact(ArtifactType.HINT, name="alpha"),
    )
    application = shell_artifacts(inputs, "/private", session=True)
    index = json.loads(next(file.data for file in application.files if file.path.endswith("/index.json")))
    owner = index["groups"][0]
    assert list(owner) == ["owner", "hints", "rules", "skills", "agents"]
    assert list(owner["hints"]) == list(owner["rules"]) == ["zebra", "alpha"]


@pytest.mark.parametrize("render", [claude.session_artifacts, codex.session_artifacts, grok.session_artifacts])
def test_session_delivery_defaults_to_unhandled_before_native_validation(render):
    items = tuple(
        artifact(type, options={name: {"hooks": {}} for name in ("claude-code", "codex", "grok-build")})
        for type in ArtifactType
    )
    duplicate = replace(items[-1], origin=ArtifactOrigin("agent", "agent", "worker", entry="review"))
    result = render(context(duplicate, *items), configured=None, extra_args=["--"])
    assert result.argv == ()
    assert result.application.files == ()
    assert {item.input_id for item in result.application.deferred} == {item.identity for item in (*items, duplicate)}
    assert {item.destination for item in result.application.deferred} == {"session"}


@pytest.mark.parametrize(
    "integration,workarounds",
    [
        (ClaudeCodeIntegration, claude._SESSION_WORKAROUNDS),
        (CodexIntegration, codex._SESSION_WORKAROUNDS),
        (GrokBuildIntegration, grok._SESSION_WORKAROUNDS),
    ],
)
def test_config_workaround_names_match_native_delivery(integration, workarounds):
    annotation = integration.config_model.model_fields["enabled_workarounds"].annotation
    names = get_args(get_args(annotation)[0])
    assert set(names) == set(workarounds.values())


@pytest.mark.parametrize("type", list(ArtifactType))
def test_shell_delivers_session_artifacts_without_workarounds(type):
    item = artifact(type)
    prepared = context(item)
    integration = ShellIntegration(
        "shell",
        {},
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=prepared,
    )
    result = integration.start(RunContext())
    assert isinstance(result, HarnessStart)
    assert result.artifacts.files
    assert result.artifacts.deferred == ()
    assert result.artifacts.artifacts_dir == prepared.directory
    assert any(
        file.origins == (item.origin_identity,) and file.path != f"{prepared.directory}/index.json"
        for file in result.artifacts.files
    )


@pytest.mark.parametrize(
    "render,workaround,selected,flag",
    [
        (
            claude.session_artifacts,
            "session-prompt",
            {ArtifactType.HINT, ArtifactType.RULE},
            "--append-system-prompt-file",
        ),
        (claude.session_artifacts, "session-skill-plugin", {ArtifactType.SKILL}, "--plugin-dir"),
        (claude.session_artifacts, "session-agent-definitions", {ArtifactType.AGENT}, "--agents"),
        (codex.session_artifacts, "session-developer-instructions", {ArtifactType.HINT, ArtifactType.RULE}, "-c"),
        (codex.session_artifacts, "session-agent-config", {ArtifactType.AGENT}, "-c"),
        (grok.session_artifacts, "session-rules", {ArtifactType.HINT, ArtifactType.RULE}, "--rules"),
        (grok.session_artifacts, "session-agent-definitions", {ArtifactType.AGENT}, "--agents"),
    ],
)
def test_workaround_selects_only_its_types_including_deferred_ancestors(render, workaround, selected, flag):
    items = tuple(artifact(type) for type in ArtifactType)
    items = tuple(
        replace(item, origin=ArtifactOrigin("agent", "agent", "worker", entry="review"))
        if item.content.type is ArtifactType.RULE
        else item
        for item in items
    )
    items = tuple(sorted(items, key=lambda item: item.origin.component == "session"))
    received_context = context(*items)
    assert received_context.inputs.deferred
    result = render(received_context, configured=None, extra_args=[], enabled_workarounds=[workaround])
    assert flag in result.argv
    assert {item.input_id for item in result.application.deferred} == {
        item.identity for item in items if item.content.type not in selected
    }


@pytest.mark.parametrize(
    "render,workaround,args",
    [
        (claude.session_artifacts, "session-agent-definitions", ["--append-system-prompt", "configured"]),
        (codex.session_artifacts, "session-agent-config", ["-c", 'developer_instructions="configured"']),
        (grok.session_artifacts, "session-agent-definitions", ["--rules", "configured"]),
    ],
)
def test_unselected_guidance_does_not_restrict_native_guidance_flags(render, workaround, args):
    result = render(
        context(artifact(ArtifactType.RULE), artifact(ArtifactType.AGENT)),
        configured=None,
        extra_args=args,
        enabled_workarounds=[workaround],
    )
    assert result.argv
    assert len(result.application.deferred) == 1


@pytest.mark.parametrize("implementation", [ClaudeCodeIntegration, CodexIntegration, GrokBuildIntegration])
def test_skipped_inputs_do_not_probe_or_require_launch_target(monkeypatch, implementation):
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent

    module = __import__(implementation.__module__, fromlist=["probe_native"])
    monkeypatch.setattr(module, "probe_native", lambda *args, **kwargs: pytest.fail("unexpected artifact probe"))
    integration = implementation(
        "native",
        {},
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=context(artifact(ArtifactType.RULE)),
    )
    # Isolate launch selection from independent transcript / identity discovery.
    monkeypatch.setattr(integration, "_resume_or_launch", lambda *args, **kwargs: "native-command")
    if implementation is CodexIntegration:
        monkeypatch.setattr(integration, "_start_fresh", lambda *args, **kwargs: "native-command")
    result = integration.start(RunContext(), intent=HarnessLaunchIntent.CREATE)
    assert result.artifacts.files == ()
    assert len(result.artifacts.deferred) == 1


@pytest.mark.parametrize(
    "version,returncode,snapshot,warn",
    [
        ("2.0.64 (Claude Code)", 0, False, False),
        ("2.1.264 (Claude Code)", 0, False, False),
        ("2.1.265 (Claude Code)", 0, True, False),
        ("2.2.0 (Claude Code)", 0, True, False),
        ("node 22.18.0\n2.1.264 (Claude Code)\n", 0, False, False),
        ("startup helper 1.0.0\n2.1.265 (Claude Code)\n", 0, True, False),
        ("2.1.265", 0, False, True),
        ("unrecognized", 0, False, True),
        ("2.1.265", 1, False, True),
    ],
)
def test_claude_snapshot_adjustment_is_narrow_and_unknown_versions_warn(
    monkeypatch, version, returncode, snapshot, warn
):
    from unittest.mock import Mock

    from agentworks.ssh import SSHResult
    from agentworks.transports import Transport

    target = Mock(spec=Transport)
    target.run.return_value = SSHResult(returncode, version, "")
    warning = Mock()
    monkeypatch.setattr(output, "warn", warning)
    result = claude.session_prompt_snapshot(target, {})
    assert result == (("--system-prompt-snapshot", "off") if snapshot else ())
    assert warning.called is warn
    assert target.run.call_count == 1


@pytest.mark.parametrize(
    "config,workaround",
    [
        (ClaudeCodeIntegration.config_model, "session-prompt"),
        (CodexIntegration.config_model, "session-agent-config"),
        (GrokBuildIntegration.config_model, "session-rules"),
    ],
)
def test_workaround_config_replaces_inherited_optins_and_refuses_unknown_names(config, workaround):
    from pydantic import ValidationError

    from agentworks.schema import merge_model

    assert config().enabled_workarounds == []
    value, _ = merge_model(config, {"enabled_workarounds": [workaround]}, {"enabled_workarounds": []})
    assert config.model_validate(value).enabled_workarounds == []
    with pytest.raises(ValidationError):
        config.model_validate({"enabled_workarounds": ["unknown-workaround"]})


def test_claude_snapshot_observation_timeout_warns_and_continues(monkeypatch):
    from unittest.mock import Mock

    from agentworks.ssh import SSHError
    from agentworks.transports import Transport

    target = Mock(spec=Transport)
    target.run.side_effect = SSHError("fixture timed out")
    warning = Mock()
    monkeypatch.setattr(output, "warn", warning)
    assert claude.session_prompt_snapshot(target, {"TOKEN": "fixture-secret"}) == ()
    warning.assert_called_once()


@pytest.mark.parametrize("workaround", [None, "session-skill-plugin", "session-agent-definitions", "session-prompt"])
def test_claude_observes_version_only_for_delivered_session_prompt(monkeypatch, workaround):
    from unittest.mock import Mock

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from agentworks.plugins.claude import harness_integration as module
    from tests.conftest import _FakeTarget

    snapshot = Mock(return_value=("--system-prompt-snapshot", "off"))
    monkeypatch.setattr(module, "session_prompt_snapshot", snapshot)
    monkeypatch.setattr(module, "probe_native", lambda *args, **kwargs: "/home/alice/.claude")
    ancestor = claude.outer_artifacts(received(artifact(ArtifactType.RULE)), "/home/alice/.claude")
    integration = ClaudeCodeIntegration(
        "claude-code",
        {"enabled_workarounds": [workaround] if workaround else []},
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=context(
            *(artifact(type) for type in ArtifactType), ancestors=tuple(owned(file) for file in ancestor.files)
        ),
    )
    monkeypatch.setattr(integration, "_resume_or_launch", lambda *args, **kwargs: "native-command")
    integration.start(RunContext(admin_target=_FakeTarget()), intent=HarnessLaunchIntent.CREATE)
    assert snapshot.called is (workaround == "session-prompt")
    assert ("--system-prompt-snapshot" in integration._artifact_plan.argv) is (workaround == "session-prompt")

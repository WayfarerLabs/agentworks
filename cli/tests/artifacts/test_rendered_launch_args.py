"""Native artifact guards and commands consume the same rendered arguments."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Mapping
from importlib import import_module
from typing import Any

import pytest

from agentworks.artifacts.application import OwnedArtifactFile, SessionArtifactContext
from agentworks.artifacts.model import ArtifactType
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.harness_integration import HarnessLaunchIntent
from agentworks.errors import ConfigError
from tests.artifacts.test_native_delivery import artifact, context
from tests.conftest import requires_posix_shell
from tests.vm_access_support import Admin, RecordingVMAccess

TOOLS = [
    ("claude", "ClaudeCodeIntegration", "session-skill-plugin"),
    ("codex", "CodexIntegration", "session-home-skills"),
    ("grok", "GrokBuildIntegration", "session-home-skills"),
]


def integration(
    tool: str, class_name: str, config: Mapping[str, object], *, artifacts: SessionArtifactContext | None = None
) -> Any:
    implementation = getattr(import_module(f"agentworks.plugins.{tool}.harness_integration"), class_name)
    return implementation(
        implementation.name,
        config,
        session_name="s1",
        vm_name="vm",
        workspace_name="workspace",
        workspace_path="/workspace",
        target=None,
        admin=True,
        state={},
        artifact_context=artifacts,
    )


@pytest.mark.parametrize(
    "tool,class_name,workaround,args,value",
    [
        (*TOOLS[0], ["--${agw:workspace_name}"], "disable-slash-commands"),
        (*TOOLS[1], ["--${agw:workspace_name}", "custom"], "profile"),
        (*TOOLS[1], ["--disable", "${agw:workspace_name}"], "skills"),
        (*TOOLS[1], ["--config", "${agw:workspace_name}=false"], "features.skills"),
        (*TOOLS[2], ["--${agw:workspace_name}"], "disable-skills"),
    ],
)
@pytest.mark.parametrize("templated", [False, True])
def test_start_refuses_effective_carrier_overrides_before_identity_probes(
    monkeypatch, tool, class_name, workaround, args, value, templated
):
    authored = args if templated else [token.replace("${agw:workspace_name}", value) for token in args]
    selected = integration(
        tool,
        class_name,
        {"extra_args": authored, "enabled_workarounds": [workaround]},
        artifacts=context(artifact(ArtifactType.RULE)),
    )
    selected.prepare_context({"workspace_name": value}, intent=HarnessLaunchIntent.CREATE)

    # Native policy is independent of argv validation; any attempted probe is a
    # failure of this rejection boundary, before identity or command creation.
    def forbid_probe(*args, **kwargs):
        pytest.fail("effective forbidden arguments reached native probing")

    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", forbid_probe)
    access = RecordingVMAccess([], Admin())
    with pytest.raises(ConfigError):
        selected.start(RunContext(admin_access=access), intent=HarnessLaunchIntent.CREATE)
    assert access.requests() == [] and selected._state == {}


@pytest.mark.windows
@pytest.mark.parametrize("tool,class_name,workaround", TOOLS)
def test_start_emits_guarded_tokens_once_and_rebinds_next_launch(monkeypatch, tool, class_name, workaround):
    module = import_module(f"agentworks.plugins.{tool}.harness_integration")
    render = module.session_artifacts
    observed = []

    def inspect(prepared, *, extra_args, **kwargs):
        observed.append(tuple(extra_args))
        return render(prepared, extra_args=extra_args, **kwargs)

    monkeypatch.setattr(module, "session_artifacts", inspect)
    authored = ["--fixture", "${agw:workspace_name}", "$${agw:workspace_name}"]
    selected = integration(tool, class_name, {"extra_args": authored})
    access = RecordingVMAccess([], Admin())
    for value in ("one ; $(echo unsafe) ${agw:session_name}", "two ' quoted ${agw:session_name}"):
        selected.prepare_context(
            {"workspace_name": value, "session_name": "must-remain-unexpanded"},
            intent=HarnessLaunchIntent.CREATE,
        )
        result = selected.start(RunContext(admin_access=access), intent=HarnessLaunchIntent.CREATE)
        tokens = shlex.split(shlex.split(result.command)[2])
        emitted = tokens[tokens.index(tool) + 1 :]
        expected = ("--fixture", value, "${agw:workspace_name}")
        assert observed[-1] == expected
        assert tuple(emitted[-3:]) == expected
        assert selected.config.extra_args == authored
    assert len(observed) == 2 and observed[0] != observed[1]


@pytest.mark.parametrize("tool,class_name,workaround", TOOLS)
def test_disabled_session_delivery_keeps_effective_native_override_available(tool, class_name, workaround):
    flags = {"claude": "disable-slash-commands", "codex": "profile", "grok": "disable-skills"}
    selected = integration(
        tool,
        class_name,
        {"extra_args": ["--${agw:workspace_name}", "custom"]},
        artifacts=context(artifact(ArtifactType.RULE)),
    )
    selected.prepare_context({"workspace_name": flags[tool]}, intent=HarnessLaunchIntent.CREATE)
    result = selected.start(RunContext(admin_access=RecordingVMAccess([], Admin())), intent=HarnessLaunchIntent.CREATE)
    assert not result.artifacts.files and len(result.artifacts.deferred) == 1
    tokens = shlex.split(shlex.split(result.command)[2])
    assert tokens[-2:] == ["--" + flags[tool], "custom"]


@pytest.mark.parametrize("tool,class_name,workaround", TOOLS)
def test_ancestor_only_override_is_advisory_after_rendering(monkeypatch, captured_output, tool, class_name, workaround):
    native = {"claude": "/home/alice/.claude", "codex": "/home/alice/.codex", "grok": "/home/alice/.grok"}
    skills = "/home/alice/.agents" if tool == "codex" else native[tool]
    ancestor = OwnedArtifactFile(
        path=skills + "/skills/existing/SKILL.md",
        sha256="a" * 64,
        origins=("b" * 64,),
        native_identity="skill:existing",
    )
    flags = {"claude": "disable-slash-commands", "codex": "profile", "grok": "disable-skills"}
    selected = integration(
        tool, class_name, {"extra_args": ["--${agw:workspace_name}"]}, artifacts=context(ancestors=(ancestor,))
    )
    selected.prepare_context({"workspace_name": flags[tool]}, intent=HarnessLaunchIntent.CREATE)
    monkeypatch.setattr("agentworks.artifacts.native.probe.probe_native", lambda *args, **kwargs: native[tool])
    result = selected.start(RunContext(admin_access=RecordingVMAccess([], Admin())), intent=HarnessLaunchIntent.CREATE)
    assert captured_output.warnings
    assert not result.artifacts.files and not result.artifacts.deferred
    assert shlex.split(shlex.split(result.command)[2])[-1] == "--" + flags[tool]


@requires_posix_shell
@pytest.mark.parametrize("tool,class_name,workaround", TOOLS)
def test_rendered_values_reach_the_process_as_literal_single_tokens(tmp_path, tool, class_name, workaround):
    home = tmp_path / "home"
    home.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    capture = tmp_path / "argv.json"
    sentinel = tmp_path / "injected"
    executable = binaries / tool
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['ARGV_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    payload = f"' ; $(touch {sentinel}) ${{agw:session_name}}"
    selected = integration(tool, class_name, {"extra_args": ["--fixture", "${agw:workspace_name}"]})
    selected.prepare_context(
        {"workspace_name": payload, "session_name": "must-remain-unexpanded"}, intent=HarnessLaunchIntent.CREATE
    )
    result = selected.start(RunContext(admin_access=RecordingVMAccess([], Admin())), intent=HarnessLaunchIntent.CREATE)
    completed = subprocess.run(
        ["/bin/sh", "-c", result.command],
        env={"HOME": str(home), "PATH": str(binaries) + ":/usr/bin:/bin", "ARGV_CAPTURE": str(capture)},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert completed.returncode == 0
    assert json.loads(capture.read_text())[-2:] == ["--fixture", payload]
    assert not sentinel.exists()

"""Grok Build config, UUID lifecycle, argv construction, and readiness."""

from __future__ import annotations

import shlex
import subprocess
import uuid
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.capabilities.config import capability_config_references, validate_capability_config
from agentworks.capabilities.harness_integration import HarnessLaunchIntent, HarnessStart
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.grok.harness_integration import GrokBuildConfig, GrokBuildIntegration
from agentworks.schema import RefOwner, merge_model
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_SID = "939b1597-7c61-5ace-80f4-14617b7b4257"


def _integration(
    config: Mapping[str, object] | None = None,
    *,
    state: dict[str, object] | None = None,
    session_name: str = "s1",
    admin: bool = True,
) -> GrokBuildIntegration:
    return GrokBuildIntegration(
        "grok",
        config or {},
        session_name=session_name,
        vm_name="box",
        workspace_name="ws1",
        workspace_path="/srv/ws1",
        target=None,
        admin=admin,
        state={"session_id": _SID} if state is None else state,
    )


def _op_ctx(target: _FakeTarget) -> RunContext:
    return RunContext(admin_target=target)


def _scope() -> OperationScope:
    return OperationScope(
        level=ScopeLevel.SESSION,
        vm="box",
        workspace="ws1",
        session="s1",
        agent=None,
        admin=True,
    )


def _validate(blob: dict[str, object]) -> None:
    validate_capability_config(
        kind="harness-integration",
        config={"name": "grok-build", **blob},
        owner=RefOwner(kind="session-template", name="grok"),
    )


def _grok_argv(command: str | HarnessStart) -> list[str]:
    if isinstance(command, HarnessStart):
        command = command.command
    outer = shlex.split(command)
    assert outer[:2] == ["sh", "-c"]
    inner = shlex.split(outer[2])
    grok = inner.index("grok")
    return inner[grok + 1 :]


def test_config_implies_no_resource_references() -> None:
    owner = RefOwner(kind="session-template", name="grok")
    assert (
        capability_config_references(
            kind="harness-integration",
            config={"name": "grok-build", "model": "grok-4.6"},
            owner=owner,
        )
        == ()
    )
    assert (
        capability_config_references(
            kind="harness-integration",
            config={"name": "grok-build", "model": 3, "typo": True},
            owner=owner,
        )
        == ()
    )


def test_validation_accepts_open_string_choices_and_empty_config() -> None:
    _validate(
        {
            "permission_mode": "future-mode",
            "model": "future-model",
            "reasoning_effort": "future-effort",
            "sandbox": "future-sandbox",
            "goal": "Finish the migration",
            "initial_prompt": "Start with the failing tests",
            "agent": "reviewer",
            "rules": "Keep changes focused",
            "extra_args": ["--future-flag"],
        }
    )
    _validate({})


def test_validation_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError):
        _validate({"permision_mode": "typo"})


@pytest.mark.parametrize(
    "field",
    [
        "permission_mode",
        "model",
        "reasoning_effort",
        "sandbox",
        "goal",
        "initial_prompt",
        "agent",
        "rules",
    ],
)
def test_validation_rejects_non_string_choices(field: str) -> None:
    with pytest.raises(ConfigError):
        _validate({field: 3})


def test_validation_rejects_non_list_extra_args() -> None:
    with pytest.raises(ConfigError, match="extra_args: must be a list"):
        _validate({"extra_args": "--flag"})


def test_model_merge_replaces_extra_args_including_with_an_empty_list() -> None:
    raw, _ = merge_model(
        GrokBuildConfig,
        {"extra_args": ["--parent"]},
        {"extra_args": []},
    )
    assert cast("dict[str, object]", raw)["extra_args"] == []


def test_construct_revalidates_config() -> None:
    with pytest.raises(ConfigError):
        _integration({"nope": True})


def test_persisted_summary_resumes() -> None:
    target = _FakeTarget({"summary.json": _FakeResult(0)})
    command = _integration().start(_op_ctx(target))
    assert _grok_argv(command)[:2] == ["--resume", _SID]


def test_absent_summary_starts_fresh() -> None:
    target = _FakeTarget({"summary.json": _FakeResult(1)})
    command = _integration().start(_op_ctx(target))
    assert _grok_argv(command)[:2] == ["--session-id", _SID]


def test_resume_only_resumes_an_existing_summary() -> None:
    target = _FakeTarget({"summary.json": _FakeResult(0)})
    result = _integration(state={"session_id": _SID}).start(_op_ctx(target), intent=HarnessLaunchIntent.RESUME_ONLY)
    assert _grok_argv(result)[:2] == ["--resume", _SID]


@pytest.mark.parametrize(
    ("state", "target", "expected_probe_count"),
    [
        pytest.param({}, _FakeTarget(), 0, id="no-stored-id"),
        pytest.param({"session_id": _SID}, _FakeTarget({"summary.json": _FakeResult(1)}), 1, id="no-summary"),
        pytest.param({"session_id": _SID}, _FakeTarget({"summary.json": _FakeResult(255)}), 1, id="probe-failed"),
    ],
)
def test_resume_only_refuses_without_resumable_state_and_preserves_integration_state(
    state: dict[str, object],
    target: _FakeTarget,
    expected_probe_count: int,
) -> None:
    before = state.copy()
    with pytest.raises(StateError):
        _integration(state=state).start(_op_ctx(target), intent=HarnessLaunchIntent.RESUME_ONLY)
    assert state == before
    assert len(target.commands) == expected_probe_count


def test_resume_only_refuses_a_missing_target_without_state_mutation() -> None:
    state: dict[str, object] = {"session_id": _SID}
    with pytest.raises(StateError):
        _integration(state=state).start(RunContext(), intent=HarnessLaunchIntent.RESUME_ONLY)
    assert state == {"session_id": _SID}


def test_create_rotates_the_id_without_probing_resumable_state(monkeypatch: pytest.MonkeyPatch) -> None:
    replacement_sid = "0a18a04b-d083-4dc8-961a-07f2151c9b35"
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(replacement_sid))
    state: dict[str, object] = {"session_id": _SID}
    target = _FakeTarget({"summary.json": _FakeResult(0)})

    command = _integration(state=state).start(_op_ctx(target), intent=HarnessLaunchIntent.CREATE)

    assert _grok_argv(command)[:2] == ["--session-id", replacement_sid]
    assert state == {"session_id": replacement_sid}
    assert target.commands == []


def test_launch_intents_report_distinct_decisions() -> None:
    target = _FakeTarget({"summary.json": _FakeResult(1)})
    missing = _integration().start(_op_ctx(target))
    created = _integration().start(_op_ctx(target), intent=HarnessLaunchIntent.CREATE)
    forced = _integration().start(_op_ctx(target), intent=HarnessLaunchIntent.FORCE_NEW)

    assert None not in {missing.note, created.note, forced.note}
    assert len({missing.note, created.note, forced.note}) == 3


def test_repeated_start_uses_the_same_state_based_decision() -> None:
    target = _FakeTarget({"summary.json": _FakeResult(0)})
    integration = _integration()
    assert integration.start(_op_ctx(target)) == integration.start(_op_ctx(target))


def test_probe_uses_grok_home_and_the_persisted_summary_boundary() -> None:
    target = _FakeTarget({"summary.json": _FakeResult(0)})
    _integration().start(_op_ctx(target))
    (probe,) = target.commands
    assert "GROK_HOME" in probe
    assert f"{_SID}/summary.json" in probe
    assert "-mindepth 3 -maxdepth 3" in probe
    assert "-type f" in probe


def test_missing_session_root_is_clean_absence_but_find_failure_is_not() -> None:
    target = _FakeTarget({"summary.json": _FakeResult(0)})
    _integration().start(_op_ctx(target))
    (probe,) = target.commands
    assert "[ -d " in probe
    assert "exit 6" in probe

    with pytest.raises(StateError):
        _integration().start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(6)})))


def test_transport_failure_refuses_to_guess() -> None:
    with pytest.raises(StateError) as exc:
        _integration().start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(255)})))
    assert exc.value.entity_name == "s1"


def test_first_start_mints_and_records_a_uuid() -> None:
    state: dict[str, object] = {}
    command = _integration(state=state).start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(1)}))).command
    sid = state["session_id"]
    assert isinstance(sid, str) and len(sid) == 36
    assert sid in command


def test_wrong_typed_session_id_is_replaced_with_a_canonical_uuid() -> None:
    state: dict[str, object] = {"session_id": 7}
    _integration(state=state).start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(1)})))
    sid = state["session_id"]
    assert isinstance(sid, str)
    assert str(uuid.UUID(sid)) == sid


def test_existing_session_id_is_reused_verbatim() -> None:
    state: dict[str, object] = {"session_id": _SID}
    command = _integration(state=state).start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(0)})))
    assert _grok_argv(command)[:2] == ["--resume", _SID]
    assert state == {"session_id": _SID}


@pytest.mark.parametrize("sid", ["not-a-uuid", _SID.replace("-", ""), f"{{{_SID}}}"])
def test_invalid_persisted_session_id_is_rejected_before_probe(sid: str) -> None:
    target = _FakeTarget({"summary.json": _FakeResult(0)})
    with pytest.raises(StateError) as exc:
        _integration(state={"session_id": sid}).start(_op_ctx(target))
    assert exc.value.entity_name == "s1"
    assert target.commands == []


class _LocalProbeTarget:
    def __init__(self, grok_home: Path) -> None:
        self._grok_home = grok_home

    def run(self, command: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            shell=True,
            executable="/bin/sh",
            env={"GROK_HOME": str(self._grok_home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/sh"},
            capture_output=True,
            text=True,
            check=False,
        )


def test_probe_executes_against_groks_real_filesystem_boundary(tmp_path: Path) -> None:
    grok_home = tmp_path / "grok-home"
    target = _LocalProbeTarget(grok_home)

    absent = _integration().start(_op_ctx(target))  # type: ignore[arg-type]
    assert _grok_argv(absent)[:2] == ["--session-id", _SID]

    stub = grok_home / "sessions" / "encoded-cwd" / _SID
    stub.mkdir(parents=True)
    incomplete = _integration().start(_op_ctx(target))  # type: ignore[arg-type]
    assert _grok_argv(incomplete)[:2] == ["--session-id", _SID]

    (stub / "summary.json").write_text("{}")
    persisted = _integration().start(_op_ctx(target))  # type: ignore[arg-type]
    assert _grok_argv(persisted)[:2] == ["--resume", _SID]


def test_managed_fields_map_to_canonical_flags() -> None:
    command = _integration(
        {
            "permission_mode": "acceptEdits",
            "model": "grok-4.6",
            "reasoning_effort": "future-effort",
            "sandbox": "workspace-write",
        }
    ).start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(1)})))
    argv = _grok_argv(command)
    assert argv == [
        "--session-id",
        _SID,
        "--permission-mode",
        "acceptEdits",
        "--model",
        "grok-4.6",
        "--reasoning-effort",
        "future-effort",
        "--sandbox",
        "workspace-write",
    ]


def test_empty_config_emits_only_the_fresh_session_identity() -> None:
    command = _integration().start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(1)})))
    assert _grok_argv(command) == ["--session-id", _SID]


def test_fresh_workload_uses_native_controls_and_one_positional_prompt() -> None:
    goal = "Finish safely; printf 'done'"
    initial = "Begin with the failing test"
    command = _integration(
        {
            "goal": goal,
            "initial_prompt": initial,
            "agent": "reviewer",
            "rules": "Keep the diff focused",
            "extra_args": ["--future-flag"],
        }
    ).start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(1)})))
    argv = _grok_argv(command)

    assert argv[argv.index("--agent") + 1] == "reviewer"
    assert argv[argv.index("--rules") + 1] == "Keep the diff focused"
    assert "--prompt" not in argv
    assert "--agent-profile" not in argv
    assert argv[-3] == "--future-flag"
    assert argv[-2] == "--"
    prompt = argv[-1]
    assert prompt.index(goal) < prompt.index(initial)


@pytest.mark.parametrize("initial_prompt", ["--version", "agent"])
def test_fresh_initial_prompt_uses_a_positional_parser_boundary(initial_prompt: str) -> None:
    argv = _grok_argv(
        _integration({"initial_prompt": initial_prompt, "extra_args": ["--future-flag"]}).start(
            _op_ctx(_FakeTarget({"summary.json": _FakeResult(1)}))
        )
    )

    assert argv[-3:] == ["--future-flag", "--", initial_prompt]
    assert "--prompt" not in argv


def test_resume_reapplies_process_controls_but_not_fresh_conversation_content() -> None:
    command = _integration(
        {
            "goal": "Fresh goal",
            "initial_prompt": "Fresh prompt",
            "agent": "reviewer",
            "rules": "Fresh rules",
        }
    ).start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(0)})))
    argv = _grok_argv(command)
    assert argv[argv.index("--agent") + 1] == "reviewer"
    assert argv[argv.index("--rules") + 1] == "Fresh rules"
    assert all("Fresh goal" not in token and "Fresh prompt" not in token for token in argv)
    assert "--" not in argv


def test_extra_args_are_quoted_and_appended_last() -> None:
    payload = "a'; touch /tmp/pwned #"
    command = _integration({"model": "managed", "extra_args": ["--future-flag", payload]}).start(
        _op_ctx(_FakeTarget({"summary.json": _FakeResult(1)}))
    )
    argv = _grok_argv(command)
    assert argv[-2:] == ["--future-flag", payload]
    assert argv.index("managed") < argv.index("--future-flag")
    assert "touch" not in shlex.split(shlex.split(command.command)[2])


def test_readiness_requires_grok() -> None:
    target = _FakeTarget({"command -v grok": _FakeResult(20)})
    with pytest.raises(StateError):
        _integration().preflight(RunContext(operation_scope=_scope(), admin_target=target))


def test_readiness_accepts_installed_grok() -> None:
    target = _FakeTarget({"command -v grok": _FakeResult(0)})
    _integration().preflight(RunContext(operation_scope=_scope(), admin_target=target))
    assert any("command -v grok" in command for command in target.commands)

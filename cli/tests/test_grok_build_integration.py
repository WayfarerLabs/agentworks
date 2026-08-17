"""Grok Build config, UUID lifecycle, argv construction, and readiness."""

from __future__ import annotations

import shlex
import subprocess
import uuid
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.capabilities.config import capability_config_references, validate_capability_config
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.grok.harness_integration import GrokBuildIntegration
from agentworks.schema import RefOwner
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


def _grok_argv(command: str) -> list[str]:
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
            "extra_args": ["--future-flag"],
        }
    )
    _validate({})


def test_validation_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError):
        _validate({"permision_mode": "typo"})


@pytest.mark.parametrize("field", ["permission_mode", "model", "reasoning_effort", "sandbox"])
def test_validation_rejects_non_string_choices(field: str) -> None:
    with pytest.raises(ConfigError):
        _validate({field: 3})


def test_validation_rejects_non_list_extra_args() -> None:
    with pytest.raises(ConfigError, match="extra_args: must be a list"):
        _validate({"extra_args": "--flag"})


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


def test_start_and_resume_use_the_same_state_based_decision() -> None:
    target = _FakeTarget({"summary.json": _FakeResult(0)})
    integration = _integration()
    assert integration.start(_op_ctx(target)) == integration.resume(_op_ctx(target))


def test_launch_note_reports_the_selected_branch() -> None:
    integration = _integration()
    assert integration.launch_note() is None
    integration.start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(0)})))
    assert integration.launch_note() is not None

    integration = _integration()
    integration.start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(1)})))
    assert integration.launch_note() is not None


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
    command = _integration(state=state).start(_op_ctx(_FakeTarget({"summary.json": _FakeResult(1)})))
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
    command = _integration(state=state).resume(_op_ctx(_FakeTarget({"summary.json": _FakeResult(0)})))
    assert _grok_argv(command)[:2] == ["--resume", _SID]
    assert state == {"session_id": _SID}


@pytest.mark.parametrize("sid", ["not-a-uuid", _SID.replace("-", ""), f"{{{_SID}}}"])
def test_invalid_persisted_session_id_is_rejected_before_probe(sid: str) -> None:
    target = _FakeTarget({"summary.json": _FakeResult(0)})
    with pytest.raises(StateError) as exc:
        _integration(state={"session_id": sid}).resume(_op_ctx(target))
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
    incomplete = _integration().resume(_op_ctx(target))  # type: ignore[arg-type]
    assert _grok_argv(incomplete)[:2] == ["--session-id", _SID]

    (stub / "summary.json").write_text("{}")
    persisted = _integration().resume(_op_ctx(target))  # type: ignore[arg-type]
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


def test_extra_args_are_quoted_and_appended_last() -> None:
    payload = "a'; touch /tmp/pwned #"
    command = _integration({"model": "managed", "extra_args": ["--rules", payload]}).start(
        _op_ctx(_FakeTarget({"summary.json": _FakeResult(1)}))
    )
    argv = _grok_argv(command)
    assert argv[-2:] == ["--rules", payload]
    assert argv.index("managed") < argv.index("--rules")
    assert "touch" not in shlex.split(shlex.split(command)[2])


def test_readiness_requires_grok() -> None:
    target = _FakeTarget({"command -v grok": _FakeResult(1)})
    with pytest.raises(StateError):
        _integration().preflight(RunContext(operation_scope=_scope(), admin_target=target))


def test_readiness_accepts_installed_grok() -> None:
    target = _FakeTarget({"command -v grok": _FakeResult(0)})
    _integration().preflight(RunContext(operation_scope=_scope(), admin_target=target))
    assert any("command -v grok" in command for command in target.commands)

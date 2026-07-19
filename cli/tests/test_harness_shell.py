"""The ``shell`` harness and the shared ``Harness`` readiness base.

Covers the config vocabulary (validate/merge), the ops (start/restart
pane strings), the relocated required-commands probe, the SESSION-level
identity guard, and the layering rule that the capability package
imports neither ``sessions`` nor ``orchestration``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.capabilities.harness import ShellHarness
from agentworks.errors import ConfigError, StateError

if TYPE_CHECKING:
    from collections.abc import Mapping


class _Probe:
    """Recording transport double for the required-commands probe."""

    def __init__(self, missing: set[str] | None = None) -> None:
        self._missing = missing or set()
        self.commands: list[str] = []

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        ok = not any(f"command -v {m} " in cmd for m in self._missing)
        return SimpleNamespace(ok=ok)


def _harness(
    config: Mapping[str, object] | None = None,
    *,
    session_name: str = "s1",
    vm_name: str = "box",
    workspace_name: str = "ws1",
    target: object | None = None,
    admin: bool = True,
    state: dict[str, object] | None = None,
) -> ShellHarness:
    return ShellHarness(
        "claude",
        config or {},
        session_name=session_name,
        vm_name=vm_name,
        workspace_name=workspace_name,
        target=target,  # type: ignore[arg-type]
        admin=admin,
        state={} if state is None else state,
    )


def _session_scope(
    *,
    vm: str = "box",
    workspace: str = "ws1",
    session: str = "s1",
    agent: str | None = None,
    admin: bool = True,
) -> OperationScope:
    return OperationScope(
        level=ScopeLevel.SESSION,
        vm=vm,
        workspace=workspace,
        session=session,
        agent=agent,
        admin=admin,
    )


# -- config vocabulary: validate_config --------------------------------------


def test_validate_accepts_the_known_fields_and_implies_no_reference() -> None:
    refs = ShellHarness.validate_config(
        "session-template/claude",
        {
            "command": "claude",
            "restart_command": "claude --resume",
            "required_commands": ["claude", "rg"],
        },
    )
    assert refs == ()


def test_validate_accepts_empty_config() -> None:
    assert ShellHarness.validate_config("session-template/claude", {}) == ()


def test_validate_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError, match="unknown shell harness field"):
        ShellHarness.validate_config(
            "session-template/claude", {"commnad": "typo"}
        )


def test_validate_rejects_non_string_command() -> None:
    with pytest.raises(ConfigError, match="command must be a string"):
        ShellHarness.validate_config(
            "session-template/claude", {"command": 3}
        )


def test_validate_rejects_non_string_required_commands() -> None:
    with pytest.raises(ConfigError, match="required_commands must be a list"):
        ShellHarness.validate_config(
            "session-template/claude", {"required_commands": [1, 2]}
        )


def test_construct_revalidates_config() -> None:
    """A shape error dies at construction (the base re-runs
    validate_config)."""
    with pytest.raises(ConfigError, match="unknown shell harness field"):
        _harness({"nope": 1})


# -- config vocabulary: merge_config -----------------------------------------


def test_merge_child_wins_the_scalars() -> None:
    merged = ShellHarness.merge_config(
        {"command": "parent", "restart_command": "parent-r"},
        {"command": "child"},
    )
    assert merged["command"] == "child"
    assert merged["restart_command"] == "parent-r"  # untouched by the child


def test_merge_unions_required_commands_append_dedupe() -> None:
    merged = ShellHarness.merge_config(
        {"required_commands": ["claude", "rg"]},
        {"required_commands": ["rg", "fd"]},
    )
    assert merged["required_commands"] == ["claude", "rg", "fd"]


def test_merge_child_overriding_only_command_keeps_parent_required() -> None:
    """The reason for the union override: a child that overrides only
    ``command`` must not silently drop the parent's required commands."""
    merged = ShellHarness.merge_config(
        {"command": "parent", "required_commands": ["claude"]},
        {"command": "child"},
    )
    assert merged["command"] == "child"
    assert merged["required_commands"] == ["claude"]


def test_merge_default_shape_when_neither_declares_required() -> None:
    merged = ShellHarness.merge_config({"command": "a"}, {"command": "b"})
    assert "required_commands" not in merged


# -- the ops: start / restart pane strings -----------------------------------


def test_start_returns_the_command() -> None:
    assert _harness({"command": "claude"}).start(RunContext()) == "claude"


def test_start_empty_config_is_a_login_shell() -> None:
    assert _harness({}).start(RunContext()) == ""


def test_restart_prefers_restart_command() -> None:
    harness = _harness({"command": "claude", "restart_command": "claude --resume"})
    assert harness.restart(RunContext()) == "claude --resume"


def test_restart_falls_back_to_command() -> None:
    assert _harness({"command": "claude"}).restart(RunContext()) == "claude"


def test_restart_empty_config_is_a_login_shell() -> None:
    assert _harness({}).restart(RunContext()) == ""


def test_shell_leaves_the_state_blob_untouched() -> None:
    """``shell`` keeps no per-session state: the blob it is handed stays
    ``{}`` across both ops, so the manager persists nothing for it."""
    state: dict[str, object] = {}
    harness = _harness({"command": "claude"}, state=state)
    harness.start(RunContext())
    harness.restart(RunContext())
    assert state == {}
    assert harness.state == {}


# -- the readiness probe (shared require_commands) ---------------------------


def test_probe_fires_once_and_checks_every_required_command() -> None:
    harness = _harness({"required_commands": ["claude", "rg"]})
    probe = _Probe()
    scope = _session_scope()
    ctx = RunContext(operation_scope=scope, admin_target=probe)

    harness.preflight(ctx)
    assert len(probe.commands) == 2  # one probe per required command
    harness.runup(ctx)
    assert len(probe.commands) == 2  # single-fire guard: not re-probed


def test_missing_command_is_a_typed_error_naming_the_vm() -> None:
    harness = _harness({"required_commands": ["claude", "rg"]})
    probe = _Probe(missing={"rg"})
    ctx = RunContext(operation_scope=_session_scope(), admin_target=probe)

    with pytest.raises(StateError, match="requires 'rg'") as exc:
        harness.preflight(ctx)
    assert "for VM 'box'." in str(exc.value)
    assert "--template" in (exc.value.hint or "")


def test_agent_mode_defers_pending_target_then_probes_after_flip() -> None:
    target = SimpleNamespace(name="dev", realized=False)
    harness = _harness(
        {"required_commands": ["claude"]},
        target=target,
        admin=False,
    )
    probe = _Probe()
    scope = _session_scope(agent="dev", admin=False)
    ctx = RunContext(operation_scope=scope, agent_target=probe)

    harness.preflight(ctx)
    assert probe.commands == []  # pending target: deferred

    target.realized = True
    harness.runup(ctx)
    assert len(probe.commands) == 1  # probed once, post-flip


def test_agent_mode_missing_command_names_the_agent() -> None:
    target = SimpleNamespace(name="dev", realized=True)
    harness = _harness(
        {"required_commands": ["claude"]}, target=target, admin=False
    )
    probe = _Probe(missing={"claude"})
    ctx = RunContext(
        operation_scope=_session_scope(agent="dev", admin=False),
        agent_target=probe,
    )
    with pytest.raises(StateError, match="requires 'claude'") as exc:
        harness.preflight(ctx)
    assert "agent 'dev'" in str(exc.value)


# -- the readiness fork edges ------------------------------------------------


def test_system_level_scan_skips() -> None:
    """Out of scope for the level: no probe, no raise, even with no
    target at all."""
    harness = _harness({"required_commands": ["claude"]})
    harness.preflight(RunContext(operation_scope=OperationScope(level=ScopeLevel.SYSTEM)))
    harness.runup(RunContext(operation_scope=OperationScope(level=ScopeLevel.SYSTEM)))


def test_scope_less_context_is_a_loud_error() -> None:
    harness = _harness({"required_commands": ["claude"]})
    with pytest.raises(StateError, match="no operation scope"):
        harness.preflight(RunContext())


def test_agent_mode_absent_target_is_a_loud_error() -> None:
    """Anti-silent-skip: agent mode with no target is a selection bug,
    never a skip. A valid SESSION scope always names an agent, so the
    identity guard (null-safe on ``self._target``) catches the mis-wiring
    first; step 6's own ``refusing to skip`` branch is the same-intent
    backstop for a target that goes absent behind a matching scope."""
    harness = _harness(
        {"required_commands": ["claude"]}, target=None, admin=False
    )
    ctx = RunContext(operation_scope=_session_scope(agent="dev", admin=False))
    with pytest.raises(StateError, match="runs as agent None"):
        harness.preflight(ctx)


def test_missing_transport_defers_at_preflight_and_is_loud_at_runup() -> None:
    harness = _harness({"required_commands": ["claude"]})
    ctx = RunContext(operation_scope=_session_scope())  # no admin_target
    harness.preflight(ctx)  # deferred, no raise
    with pytest.raises(StateError, match="op-start context"):
        harness.runup(ctx)


# -- the SESSION-level identity guard ----------------------------------------


def test_identity_guard_raises_on_vm_mismatch() -> None:
    harness = _harness({"required_commands": ["claude"]}, vm_name="box")
    probe = _Probe()
    ctx = RunContext(
        operation_scope=_session_scope(vm="other-box"), admin_target=probe
    )
    with pytest.raises(StateError, match="wired for VM 'box'") as exc:
        harness.preflight(ctx)
    assert exc.value.entity_name == "s1"
    assert probe.commands == []  # never reached the probe


def test_identity_guard_raises_on_agent_mismatch() -> None:
    target = SimpleNamespace(name="dev", realized=True)
    harness = _harness(
        {"required_commands": ["claude"]}, target=target, admin=False
    )
    ctx = RunContext(
        operation_scope=_session_scope(agent="someone-else", admin=False),
        agent_target=_Probe(),
    )
    with pytest.raises(StateError, match="runs as agent 'dev'"):
        harness.preflight(ctx)


def test_identity_guard_raises_on_mode_mismatch() -> None:
    """Admin-wired harness handed an agent-mode scope."""
    harness = _harness({"required_commands": ["claude"]}, admin=True)
    ctx = RunContext(
        operation_scope=_session_scope(agent="dev", admin=False),
        admin_target=_Probe(),
    )
    with pytest.raises(StateError, match="admin"):
        harness.preflight(ctx)


def test_identity_guard_passes_the_matching_scope() -> None:
    harness = _harness({"required_commands": ["claude"]})
    probe = _Probe()
    ctx = RunContext(operation_scope=_session_scope(), admin_target=probe)
    harness.preflight(ctx)  # matching identity: no raise
    assert len(probe.commands) == 1


# -- the layering rule (FRD R1) ----------------------------------------------


def test_capability_imports_neither_sessions_nor_orchestration() -> None:
    """The capability layer depends only on the framework: importing the
    harness package must pull in neither its consuming domain
    (``sessions``) nor the orchestration layer.

    Runs in a fresh subprocess so the check sees a clean ``sys.modules``
    (this test session has already imported both packages) without
    mutating the shared interpreter state, which would corrupt module
    identity for other tests."""
    import subprocess
    import sys

    probe = (
        "import agentworks.capabilities.harness\n"
        "import sys\n"
        "leaked = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'agentworks.sessions'\n"
        "    or m.startswith('agentworks.sessions.')\n"
        "    or m == 'agentworks.orchestration'\n"
        "    or m.startswith('agentworks.orchestration.')\n"
        ")\n"
        "assert not leaked, 'harness leaked forbidden imports: ' + repr(leaked)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

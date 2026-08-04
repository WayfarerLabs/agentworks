"""The ``shell`` harness integration and the shared ``HarnessIntegration`` readiness base.

Covers the config vocabulary (validate/merge), the ops (start/resume
pane strings), the relocated required-commands probe, the SESSION-level
identity guard, and the layering rule that the capability package
imports neither ``sessions`` nor ``orchestration``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.capabilities.harness_integration import ShellIntegration
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


def _harness_integration(
    config: Mapping[str, object] | None = None,
    *,
    session_name: str = "s1",
    vm_name: str = "box",
    workspace_name: str = "ws1",
    target: object | None = None,
    admin: bool = True,
    state: dict[str, object] | None = None,
) -> ShellIntegration:
    return ShellIntegration(
        "claude",
        config or {},
        session_name=session_name,
        vm_name=vm_name,
        workspace_name=workspace_name,
        workspace_path="/srv/ws1",
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


# -- config vocabulary: dependencies + validate ------------------------------


def test_dependencies_imply_no_reference() -> None:
    """``shell`` implies no edge, and ``dependencies`` is total: it
    returns ``()`` for the known fields and even for a malformed blob."""
    assert (
        ShellIntegration.dependencies(
            "session-template/claude",
            {
                "command": "claude",
                "resume_command": "claude --resume",
                "required_commands": ["claude", "rg"],
            },
        )
        == ()
    )
    # Never raises, even on config that ``validate`` would reject.
    assert ShellIntegration.dependencies("session-template/claude", {"commnad": "typo", "command": 3}) == ()


def test_validate_accepts_the_known_fields_and_empty_config() -> None:
    assert (
        ShellIntegration.validate(
            "session-template/claude",
            {
                "command": "claude",
                "resume_command": "claude --resume",
                "required_commands": ["claude", "rg"],
            },
        )
        is None
    )
    assert ShellIntegration.validate("session-template/claude", {}) is None


def test_shell_launch_note_is_silent() -> None:
    # shell has no resume-vs-new notion, so it adds no op-output note.
    assert _harness_integration().launch_note() is None


def test_validate_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError, match="unknown shell harness integration field"):
        ShellIntegration.validate("session-template/claude", {"commnad": "typo"})


def test_validate_rejects_deprecated_runtime_field() -> None:
    with pytest.raises(ConfigError, match="unknown shell harness integration field.*restart_command"):
        ShellIntegration.validate("session-template/claude", {"restart_command": "old"})


def test_validate_rejects_non_string_command() -> None:
    with pytest.raises(ConfigError, match="command must be a string"):
        ShellIntegration.validate("session-template/claude", {"command": 3})


def test_validate_rejects_non_string_required_commands() -> None:
    with pytest.raises(ConfigError, match="required_commands must be a list"):
        ShellIntegration.validate("session-template/claude", {"required_commands": [1, 2]})


def test_construct_revalidates_config() -> None:
    """A shape error dies at construction (the base re-runs
    validate)."""
    with pytest.raises(ConfigError, match="unknown shell harness integration field"):
        _harness_integration({"nope": 1})


# -- config vocabulary: merge_config -----------------------------------------


def test_merge_child_wins_the_scalars() -> None:
    merged = ShellIntegration.merge_config(
        {"command": "parent", "resume_command": "parent-r"},
        {"command": "child"},
    )
    assert merged["command"] == "child"
    assert merged["resume_command"] == "parent-r"  # untouched by the child


def test_merge_unions_required_commands_append_dedupe() -> None:
    merged = ShellIntegration.merge_config(
        {"required_commands": ["claude", "rg"]},
        {"required_commands": ["rg", "fd"]},
    )
    assert merged["required_commands"] == ["claude", "rg", "fd"]


def test_merge_never_launders_an_invalid_required_commands_entry() -> None:
    """merge_config runs on RAW declared blobs (the resolver merges before
    the final validate), so a mixed valid/invalid list must survive the
    merge un-filtered for validate to reject; silently dropping the bad
    entry would produce a valid-looking blob that validate passes."""
    merged = ShellIntegration.merge_config({}, {"required_commands": ["rg", 5]})
    assert merged["required_commands"] == ["rg", 5]
    with pytest.raises(ConfigError, match="required_commands"):
        ShellIntegration.validate("session-template/t", merged)


def test_merge_child_overriding_only_command_keeps_parent_required() -> None:
    """The reason for the union override: a child that overrides only
    ``command`` must not silently drop the parent's required commands."""
    merged = ShellIntegration.merge_config(
        {"command": "parent", "required_commands": ["claude"]},
        {"command": "child"},
    )
    assert merged["command"] == "child"
    assert merged["required_commands"] == ["claude"]


def test_merge_default_shape_when_neither_declares_required() -> None:
    merged = ShellIntegration.merge_config({"command": "a"}, {"command": "b"})
    assert "required_commands" not in merged


# -- the ops: start / restart pane strings -----------------------------------


def test_start_returns_the_command() -> None:
    assert _harness_integration({"command": "claude"}).start(RunContext()) == "claude"


def test_start_empty_config_is_a_login_shell() -> None:
    assert _harness_integration({}).start(RunContext()) == ""


def test_resume_prefers_resume_command() -> None:
    harness_integration = _harness_integration({"command": "claude", "resume_command": "claude --resume"})
    assert harness_integration.resume(RunContext()) == "claude --resume"


def test_resume_falls_back_to_command() -> None:
    assert _harness_integration({"command": "claude"}).resume(RunContext()) == "claude"


def test_resume_empty_config_is_a_login_shell() -> None:
    assert _harness_integration({}).resume(RunContext()) == ""


def test_shell_leaves_the_state_blob_untouched() -> None:
    """``shell`` keeps no per-session state: the blob it is handed stays
    ``{}`` across both ops, so the manager persists nothing for it."""
    state: dict[str, object] = {}
    harness_integration = _harness_integration({"command": "claude"}, state=state)
    harness_integration.start(RunContext())
    harness_integration.resume(RunContext())
    assert state == {}
    assert harness_integration.state == {}


# -- the readiness probe (shared require_commands) ---------------------------


def test_probe_fires_once_and_checks_every_required_command() -> None:
    harness_integration = _harness_integration({"required_commands": ["claude", "rg"]})
    probe = _Probe()
    scope = _session_scope()
    ctx = RunContext(operation_scope=scope, admin_target=probe)

    harness_integration.preflight(ctx)
    assert len(probe.commands) == 2  # one probe per required command
    harness_integration.runup(ctx)
    assert len(probe.commands) == 2  # single-fire guard: not re-probed


def test_missing_command_is_a_typed_error_naming_the_vm() -> None:
    harness_integration = _harness_integration({"required_commands": ["claude", "rg"]})
    probe = _Probe(missing={"rg"})
    ctx = RunContext(operation_scope=_session_scope(), admin_target=probe)

    with pytest.raises(StateError, match="requires 'rg'") as exc:
        harness_integration.preflight(ctx)
    assert "for VM 'box'." in str(exc.value)
    assert "--template" in (exc.value.hint or "")


def test_agent_mode_defers_pending_target_then_probes_after_flip() -> None:
    target = SimpleNamespace(name="dev", realized=False)
    harness_integration = _harness_integration(
        {"required_commands": ["claude"]},
        target=target,
        admin=False,
    )
    probe = _Probe()
    scope = _session_scope(agent="dev", admin=False)
    ctx = RunContext(operation_scope=scope, agent_target=probe)

    harness_integration.preflight(ctx)
    assert probe.commands == []  # pending target: deferred

    target.realized = True
    harness_integration.runup(ctx)
    assert len(probe.commands) == 1  # probed once, post-flip


def test_agent_mode_missing_command_names_the_agent() -> None:
    target = SimpleNamespace(name="dev", realized=True)
    harness_integration = _harness_integration({"required_commands": ["claude"]}, target=target, admin=False)
    probe = _Probe(missing={"claude"})
    ctx = RunContext(
        operation_scope=_session_scope(agent="dev", admin=False),
        agent_target=probe,
    )
    with pytest.raises(StateError, match="requires 'claude'") as exc:
        harness_integration.preflight(ctx)
    assert "agent 'dev'" in str(exc.value)


# -- the readiness fork edges ------------------------------------------------


def test_system_level_scan_skips() -> None:
    """Out of scope for the level: no probe, no raise, even with no
    target at all."""
    harness_integration = _harness_integration({"required_commands": ["claude"]})
    harness_integration.preflight(RunContext(operation_scope=OperationScope(level=ScopeLevel.SYSTEM)))
    harness_integration.runup(RunContext(operation_scope=OperationScope(level=ScopeLevel.SYSTEM)))


def test_scope_less_context_is_a_loud_error() -> None:
    harness_integration = _harness_integration({"required_commands": ["claude"]})
    with pytest.raises(StateError, match="no operation scope"):
        harness_integration.preflight(RunContext())


def test_agent_mode_absent_target_is_a_loud_error() -> None:
    """Anti-silent-skip: agent mode with no target is a selection bug,
    never a skip. A valid SESSION scope always names an agent, so the
    identity guard (null-safe on ``self._target``) catches the mis-wiring
    first; step 6's own ``refusing to skip`` branch is the same-intent
    backstop for a target that goes absent behind a matching scope."""
    harness_integration = _harness_integration({"required_commands": ["claude"]}, target=None, admin=False)
    ctx = RunContext(operation_scope=_session_scope(agent="dev", admin=False))
    with pytest.raises(StateError, match="runs as agent None"):
        harness_integration.preflight(ctx)


def test_missing_transport_defers_at_preflight_and_is_loud_at_runup() -> None:
    harness_integration = _harness_integration({"required_commands": ["claude"]})
    ctx = RunContext(operation_scope=_session_scope())  # no admin_target
    harness_integration.preflight(ctx)  # deferred, no raise
    with pytest.raises(StateError, match="op-start context"):
        harness_integration.runup(ctx)


# -- the SESSION-level identity guard ----------------------------------------


def test_identity_guard_raises_on_vm_mismatch() -> None:
    harness_integration = _harness_integration({"required_commands": ["claude"]}, vm_name="box")
    probe = _Probe()
    ctx = RunContext(operation_scope=_session_scope(vm="other-box"), admin_target=probe)
    with pytest.raises(StateError, match="wired for VM 'box'") as exc:
        harness_integration.preflight(ctx)
    assert exc.value.entity_name == "s1"
    assert probe.commands == []  # never reached the probe


def test_identity_guard_raises_on_agent_mismatch() -> None:
    target = SimpleNamespace(name="dev", realized=True)
    harness_integration = _harness_integration({"required_commands": ["claude"]}, target=target, admin=False)
    ctx = RunContext(
        operation_scope=_session_scope(agent="someone-else", admin=False),
        agent_target=_Probe(),
    )
    with pytest.raises(StateError, match="runs as agent 'dev'"):
        harness_integration.preflight(ctx)


def test_identity_guard_raises_on_mode_mismatch() -> None:
    """Admin-wired harness integration handed an agent-mode scope."""
    harness_integration = _harness_integration({"required_commands": ["claude"]}, admin=True)
    ctx = RunContext(
        operation_scope=_session_scope(agent="dev", admin=False),
        admin_target=_Probe(),
    )
    with pytest.raises(StateError, match="admin"):
        harness_integration.preflight(ctx)


def test_identity_guard_passes_the_matching_scope() -> None:
    harness_integration = _harness_integration({"required_commands": ["claude"]})
    probe = _Probe()
    ctx = RunContext(operation_scope=_session_scope(), admin_target=probe)
    harness_integration.preflight(ctx)  # matching identity: no raise
    assert len(probe.commands) == 1


# -- the layering rule (FRD R1) ----------------------------------------------


def test_capability_imports_neither_sessions_nor_orchestration() -> None:
    """The capability layer depends only on the framework: importing the
    harness-integration package must pull in neither its consuming domain
    (``sessions``) nor the orchestration layer.

    Runs in a fresh subprocess so the check sees a clean ``sys.modules``
    (this test session has already imported both packages) without
    mutating the shared interpreter state, which would corrupt module
    identity for other tests."""
    import subprocess
    import sys

    probe = (
        "import agentworks.capabilities.harness_integration\n"
        "import sys\n"
        "leaked = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'agentworks.sessions'\n"
        "    or m.startswith('agentworks.sessions.')\n"
        "    or m == 'agentworks.orchestration'\n"
        "    or m.startswith('agentworks.orchestration.')\n"
        ")\n"
        "assert not leaked, 'harness integration leaked forbidden imports: ' + repr(leaked)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

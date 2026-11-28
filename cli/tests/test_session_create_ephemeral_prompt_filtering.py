"""Prompt-list filtering by a known VM anchor for
``sessions.manager.create_session``.

Split out of ``test_session_create_ephemeral.py`` (see
``_session_ephemeral_support.py`` for the full background on issue #124's
guarantees). This file covers the "Prompt filtering by known VM anchors"
slice: once a VM is pinned (via ``--vm``, an existing workspace, or an
existing agent), the workspace/mode choosers only offer options on that
VM, and the operator is told why the list was narrowed. Also covers the
ordering between the mode prompt and the VM prompt when neither has fired
yet.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks import output
from agentworks.errors import ValidationError
from agentworks.secrets.policy import TtyInteractionPolicy

from ._session_ephemeral_support import (
    _non_interactive,
    _seed_two_vms,
    _stub_build_registry,
    _stub_for_post_prompt_flow,
)

__all__ = ["_non_interactive", "_stub_build_registry"]


def test_workspace_prompt_filters_by_vm_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``--vm vm-a`` set, the workspace chooser only shows
    workspaces on vm-a. Workspaces on other VMs are filtered out so the
    operator can't pick one that the cross-check would reject."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)  # vm-a has ws-a; vm-b has ws-b
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    captured_choose: list[tuple[str, list[str]]] = []

    def _choose_spy(msg: str, opts: list[str]) -> int:
        captured_choose.append((msg, list(opts)))
        return 0  # pick the (one) filtered workspace

    info_messages: list[str] = []
    monkeypatch.setattr(output, "info", lambda m: info_messages.append(m))
    monkeypatch.setattr(output, "choose", _choose_spy)

    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            vm_name="vm-a",  # pins VM upfront
            admin=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    # Exactly one choose call: the workspace prompt.
    assert len(captured_choose) == 1
    _msg, opts = captured_choose[0]
    # The chooser saw only ws-a + [Create new], not ws-b.
    assert any("ws-a" in o for o in opts)
    assert not any("ws-b" in o for o in opts)
    assert opts[-1] == "[Create new workspace]"
    # And the operator got told why the list is short.
    assert any("Only showing workspaces on VM 'vm-a'" in m for m in info_messages)
    db.close()


def test_workspace_prompt_filters_by_existing_agent_vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``--agent agt-a`` (on vm-a) set, the workspace chooser
    filters to workspaces on vm-a even when ``--vm`` was not passed.
    The agent's VM is the anchor."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    captured_choose: list[list[str]] = []

    def _choose_spy(msg: str, opts: list[str]) -> int:
        captured_choose.append(list(opts))
        return 0

    info_messages: list[str] = []
    monkeypatch.setattr(output, "info", lambda m: info_messages.append(m))
    monkeypatch.setattr(output, "choose", _choose_spy)

    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            agent="agt-a",  # agt-a lives on vm-a
            interaction=TtyInteractionPolicy.REFUSE,
        )
    opts = captured_choose[0]
    assert any("ws-a" in o for o in opts)
    assert not any("ws-b" in o for o in opts)
    assert any("Only showing workspaces on VM 'vm-a'" in m for m in info_messages)
    db.close()


def test_mode_prompt_filters_by_resolved_vm_with_info_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mode prompt lists only agents on the resolved VM, and prints the
    'Only showing agents on VM X' info line when other-VM agents are
    being omitted."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)  # agt-a on vm-a, agt-b on vm-b
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    captured_choose: list[list[str]] = []
    info_messages: list[str] = []

    def _choose_spy(msg: str, opts: list[str]) -> int:
        captured_choose.append(list(opts))
        return 0  # admin

    monkeypatch.setattr(output, "info", lambda m: info_messages.append(m))
    monkeypatch.setattr(output, "choose", _choose_spy)
    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws-a",  # pins VM to vm-a
            # No mode flag → mode prompt fires.
            interaction=TtyInteractionPolicy.REFUSE,
        )
    opts = captured_choose[0]
    assert opts[0] == "admin"
    assert any("agt-a" in o for o in opts)
    assert not any("agt-b" in o for o in opts)
    assert opts[-1] == "[Create new agent]"
    assert any("Only showing agents on VM 'vm-a'" in m for m in info_messages)
    db.close()


def test_vm_and_existing_agent_mismatch_fails_before_workspace_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--vm vm-a --agent agt-b`` (where agt-b lives on vm-b) is
    internally inconsistent; the service has to catch this before any
    prompt fires (no point asking for a workspace when we already know
    the command is impossible)."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    # If a prompt fires, this raises: proves the validation came first.
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(
        output, "choose", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt should fire"))
    )

    with pytest.raises(ValidationError, match="VM mismatch"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            vm_name="vm-a",
            agent="agt-b",  # lives on vm-b
            interaction=TtyInteractionPolicy.REFUSE,
        )
    db.close()


def test_mode_prompt_picks_existing_agent_pins_vm_no_vm_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--new-workspace`` + no ``--vm`` + no mode flag: workspace
    doesn't pin a VM, so the mode prompt lists agents across all VMs.
    Picking an existing agent pins the VM via that agent -- the VM
    prompt should NOT fire afterwards."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)  # agt-a on vm-a, agt-b on vm-b
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    captured_choose: list[tuple[str, list[str]]] = []

    def _choose_spy(msg: str, opts: list[str]) -> int:
        captured_choose.append((msg, list(opts)))
        # The only chooser call should be the mode prompt; pick agt-a
        # (index 1: 0=admin, 1=agt-a, 2=agt-b, 3=[Create new agent]).
        return 1

    monkeypatch.setattr(output, "choose", _choose_spy)

    # If _prompt_vm fires, fail loudly -- it should not, because the
    # mode prompt's pick already pinned the VM.
    def _vm_prompt_should_not_fire(*a: object, **k: object) -> object:
        raise AssertionError("VM prompt fired but mode prompt should have pinned the VM")

    monkeypatch.setattr(session_manager, "_prompt_vm", _vm_prompt_should_not_fire)

    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            # No --vm, no mode flag.
            interaction=TtyInteractionPolicy.REFUSE,
        )
    # Exactly one chooser call -- the mode prompt, listing agents across
    # both VMs (with VM labels).
    assert len(captured_choose) == 1
    msg, opts = captured_choose[0]
    assert msg == "Run session as:"
    # The agent labels should include the VM since the prompt was
    # cross-VM.
    assert any("agt-a" in o and "vm: vm-a" in o for o in opts)
    assert any("agt-b" in o and "vm: vm-b" in o for o in opts)
    db.close()


def test_mode_prompt_picks_admin_then_vm_prompt_fires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--new-workspace`` + no ``--vm`` + no mode flag: if the mode
    prompt picks ``admin``, the VM is still unresolved, so the VM
    prompt MUST fire afterwards."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    # First chooser call: mode prompt → admin (index 0).
    # Second chooser call: VM prompt → pick vm-a (index 0).
    call_count = [0]

    def _choose_spy(msg: str, opts: list[str]) -> int:
        call_count[0] += 1
        if call_count[0] == 1:
            assert msg == "Run session as:"
            return 0  # admin
        if call_count[0] == 2:
            assert msg == "Select a VM:"
            return 0  # vm-a
        raise AssertionError(f"unexpected third chooser call: {msg}")

    monkeypatch.setattr(output, "choose", _choose_spy)
    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    assert call_count[0] == 2  # both prompts fired in order
    db.close()

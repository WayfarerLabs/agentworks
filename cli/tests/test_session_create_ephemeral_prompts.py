"""Prompt behavior for ``sessions.manager.create_session``: non-interactive
guards and interactive workspace/mode prompt outcomes.

Split out of ``test_session_create_ephemeral.py`` (see
``_session_ephemeral_support.py`` for the full background on issue #124's
guarantees). This file covers the "Prompt behavior" and "Interactive
prompt behavior" slices: mode/workspace being required in non-interactive
mode, and what each prompt choice (existing workspace, create-new
workspace, admin, existing agent, create-new agent) drives downstream.
Prompt-list *filtering* by a known VM anchor lives in the sibling
``test_session_create_ephemeral_prompt_filtering.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks import output
from agentworks.errors import ValidationError

from ._session_ephemeral_support import (
    _install_session_prep_stubs,
    _non_interactive,
    _seed_one_vm,
    _stub_build_registry,
    _stub_for_post_prompt_flow,
)
from .conftest import empty_secret_target, stub_vm_gates

__all__ = ["_non_interactive", "_stub_build_registry"]


# ---------------------------------------------------------------------------
# Prompt behavior
# ---------------------------------------------------------------------------


def test_admin_non_interactive_on_vm_with_agents_does_not_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``admin=True`` must bypass the mode prompt even when the
    target VM has agents that would otherwise be offered as options.
    Regression: an earlier shape erased ``admin`` during canonicalization,
    and the mode-prompt gate couldn't distinguish "operator chose admin"
    from "operator didn't say", causing a spurious ValidationError in
    non-interactive mode."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    # vm1 with ws1 AND an existing agent on it.
    db = _seed_one_vm(tmp_path)
    db.insert_agent("agt1", "vm1", "aw-agt1")
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    # Stub template resolution so the SimpleNamespace doesn't need
    # session_templates; the call we want to land at is ensure_active.
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)
    # The pre-create SecretTarget joins the resolver's boundary
    # registration and reads template env; the stub above keeps the
    # SimpleNamespace config out of template resolution.
    monkeypatch.setattr(
        session_manager,
        "_session_secret_target_pre_create",
        lambda *a, **k: empty_secret_target(),
    )

    called: list[str] = []

    def _spy(*args: object, **kwargs: object) -> None:
        called.append("build_graph")
        raise RuntimeError("stop after mode-prompt gate")

    stub_vm_gates(monkeypatch)
    monkeypatch.setattr("agentworks.vms.nodes.live_vm_node", _spy)

    # If admin is honored, we reach ensure_active. If admin is
    # erased and the prompt fires, the autouse non-interactive fixture
    # makes it raise ValidationError first.
    with pytest.raises(RuntimeError, match="stop after mode-prompt gate"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            admin=True,
        )
    assert called == ["build_graph"]
    db.close()


def test_mode_required_in_non_interactive(tmp_path: Path) -> None:
    """admin and agent mode are materially different (different Linux
    user, env, security boundary). The service prompts in interactive
    mode (with ``admin`` / existing agents / ``[Create new agent]`` as
    options) but never auto-selects -- omitting the flag in
    non-interactive mode raises."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1, no agents
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="session mode is required in non-interactive mode"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            # No --admin, no --agent, no --new-agent.
        )
    db.close()


def test_workspace_required_in_non_interactive(tmp_path: Path) -> None:
    """Workspace is part of the session's identity; sessions don't
    auto-select it even when there's exactly one workspace on disk.
    The service prompts in interactive mode (with existing workspaces
    + ``[Create new workspace]`` as options) but raises in
    non-interactive mode."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1, exactly one workspace
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="workspace is required in non-interactive mode"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            admin=True,  # mode is specified; workspace is what's under test
        )
    db.close()


# ---------------------------------------------------------------------------
# Interactive prompt behavior
# ---------------------------------------------------------------------------


def test_workspace_prompt_picks_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In interactive mode, the workspace prompt offers existing
    workspaces + ``[Create new]``. Picking an existing one continues
    the flow with that workspace as the anchor."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "choose", lambda msg, opts: 0)  # ws1
    called = _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            admin=True,
        )
    assert called == ["build_graph"]
    db.close()


def test_workspace_prompt_picks_create_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Picking the ``[Create new workspace]`` option (last in the list)
    sets ``new_workspace=True``, so interactive mode is functionally
    equivalent to passing ``--new-workspace`` on the CLI."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    # One workspace + [Create new] = 2 options; index 1 is [Create new].
    monkeypatch.setattr(output, "choose", lambda msg, opts: len(opts) - 1)

    # Stub the path up through eager-resolve so we land on the
    # ephemeral-realize step cleanly.
    _install_session_prep_stubs(monkeypatch)

    realize_workspace_calls: list[dict[str, object]] = []

    def _ws_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        realize_workspace_calls.append(dict(kwargs))
        raise RuntimeError("stop after workspace realize")

    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", _ws_spy)

    with pytest.raises(RuntimeError, match="stop after workspace realize"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            admin=True,
        )
    # The flow reached the workspace realization body, which means
    # new_workspace=True was set by the prompt-driven path.
    (call,) = realize_workspace_calls
    assert call["name"] == "s1"
    assert call["vm"].name == "vm1"  # type: ignore[attr-defined]
    assert call["template"].name == "default"  # type: ignore[attr-defined]
    db.close()


def test_mode_prompt_picks_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In interactive mode, the mode prompt offers ``admin`` + existing
    agents on the resolved VM + ``[Create new agent]``. Picking
    ``admin`` continues in admin mode."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    db.insert_agent("agt1", "vm1", "aw-agt1")
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "choose", lambda msg, opts: 0)  # admin
    called = _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
        )
    assert called == ["build_graph"]
    db.close()


def test_mode_prompt_picks_existing_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Picking an existing agent (option > 0, not [Create new]) sets
    ``agent_name`` to that agent."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    db.insert_agent("agt1", "vm1", "aw-agt1")
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "choose", lambda msg, opts: 1)  # agt1
    called = _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
        )
    assert called == ["build_graph"]
    db.close()


def test_mode_prompt_picks_create_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Picking the ``[Create new agent]`` option (last in the list)
    sets ``new_agent=True`` AND defaults ``agent_name`` to the session
    name. Regression: a prior shape sat the mode prompt after the
    agent-default/validation/existence block, so ``[Create new agent]``
    landed at the ephemeral-create step with ``agent_name=None`` and
    crashed on an internal assertion."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1, no agents
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    # No agents on VM: options are [admin, [Create new agent]] = 2 entries.
    # Last index is [Create new agent].
    monkeypatch.setattr(output, "choose", lambda msg, opts: len(opts) - 1)

    # Stub the path so we land on the agent realization body cleanly.
    _install_session_prep_stubs(monkeypatch)

    realize_agent_calls: list[dict[str, object]] = []

    def _ag_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        realize_agent_calls.append(dict(kwargs))
        raise RuntimeError("stop after agent realize")

    monkeypatch.setattr("agentworks.agents.realize.realize_agent", _ag_spy)

    with pytest.raises(RuntimeError, match="stop after agent realize"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
        )
    # The flow reached the agent realization body with the session name
    # defaulted in.
    (call,) = realize_agent_calls
    assert call["name"] == "s1"
    assert call["vm"].name == "vm1"  # type: ignore[attr-defined]
    db.close()

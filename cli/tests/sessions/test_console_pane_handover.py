"""Every console path that creates a shell pane parks a fresh handover, and no value reaches a command line.

The pane's environment is in the request its identity's own access parks; the
admin's tmux lines carry only the handover's token. This covers console build
(start), add-shell and both restore paths, the window rebuild being the
recovery after a killed attach pane.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import SessionMode
from agentworks.execution.access import Admin, Agent
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import multi_console as mc
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database
    from agentworks.execution.access import Identity
    from tests.conftest import _ScriptedGuest

CON = "aw-console-con"
MARKER = "sekrit-pane-6f2b"
BOT = Agent("bot", "bot-user")


@pytest.fixture(autouse=True)
def _marked_environments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every pane an environment holding a value that must reach no command line."""

    def pane_env(*_args: object, **kwargs: object) -> dict[str, str]:
        return {"TOKEN": f"{MARKER}:{kwargs['pane_user']}", "SPACED": "a 'quoted' value"}

    monkeypatch.setattr(mc, "_resolve_pane_env", pane_env)
    monkeypatch.setattr(mc, "_resolve_admin_env", lambda *_a, **_k: {"TOKEN": f"{MARKER}:standalone"})


def _seed(db: Database, specs: list[str], *, admin_shell: bool = False) -> None:
    """VM, an admin session ``a``, an agent session ``s`` of ``bot``, and console ``con`` with ``specs``."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    db.insert_agent("bot", "vm1", linux_user="bot-user")
    db.insert_session(
        "s",
        "ws-vm1",
        "default",
        SessionMode.AGENT,
        agent_name="bot",
        socket_path="/run/agentworks/agent-tmux-sockets/bot-user/s.sock",
    )
    create_console_record(db, name="con", vm_name="vm1", session_specs=specs, add_admin_shell=admin_shell)


def _parks(guest: _ScriptedGuest) -> list[tuple[Identity, str]]:
    return [(call.identity, call.request.env["TOKEN"]) for call in guest.parks()]


def _assert_no_value_left_the_parked_requests(guest: _ScriptedGuest) -> None:
    assert guest.recorder is not None
    assert not [line for line in guest.commands if MARKER in line or "quoted" in line]
    assert not [call for call in guest.recorder.calls if call.operation == "run" and call.request.env]


def test_a_console_build_parks_every_shell_on_its_own_identity(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest]
) -> None:
    _seed(db, ["a+1", "s+1"], admin_shell=True)
    db.update_console_shells("con", "s", [{"cwd": None, "admin": False}, {"cwd": None, "admin": True}])
    guest = console_guest_factory(TmuxModel())

    mc.start_console(db, _StubConfig(), name="con", interaction=TtyInteractionPolicy.REFUSE)

    assert _parks(guest) == [
        (Admin(), f"{MARKER}:standalone"),
        (Admin(), f"{MARKER}:admin"),
        (BOT, f"{MARKER}:bot-user"),
        (Admin(), f"{MARKER}:admin"),
    ]
    _assert_no_value_left_the_parked_requests(guest)


def test_add_shell_parks_the_new_shell_on_its_own_identity(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest]
) -> None:
    _seed(db, ["s"])
    model = TmuxModel()
    model.seed_session(CON, "s")
    guest = console_guest_factory(model)

    mc.add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    assert _parks(guest) == [(BOT, f"{MARKER}:bot-user")]
    _assert_no_value_left_the_parked_requests(guest)


def test_restoring_a_missing_window_parks_every_configured_shell(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest]
) -> None:
    """A killed attach pane takes its window with it when it was the last pane; restore rebuilds it whole."""
    _seed(db, ["a", "s+2"])
    db.update_console_shells("con", "s", [{"cwd": None, "admin": False}, {"cwd": None, "admin": True}])
    model = TmuxModel()
    model.seed_session(CON, "a")
    guest = console_guest_factory(model)

    mc.restore_session(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    assert model.window_names(CON) == ["a", "s"]
    assert _parks(guest) == [(BOT, f"{MARKER}:bot-user"), (Admin(), f"{MARKER}:admin")]
    _assert_no_value_left_the_parked_requests(guest)


def test_restoring_missing_panes_parks_only_the_missing_shells(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest]
) -> None:
    _seed(db, ["s+2"])
    db.update_console_shells("con", "s", [{"cwd": None, "admin": True}, {"cwd": None, "admin": False}])
    model = TmuxModel()
    model.seed_session(CON, "s", pane_tags=(None, 0))
    guest = console_guest_factory(model)

    mc.restore_session(db, _StubConfig(), console_name="con", session_name="s", interaction=TtyInteractionPolicy.REFUSE)

    assert [tag for _pid, _pidx, tag in model.pane_rows(CON, "s") or []] == [None, 0, 1]
    assert _parks(guest) == [(BOT, f"{MARKER}:bot-user")]
    _assert_no_value_left_the_parked_requests(guest)

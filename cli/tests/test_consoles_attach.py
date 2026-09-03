"""Named-console attach behavior after explicit lifecycle cutover."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import Database
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.multi_console import _attach_loop_wrapper, attach_console
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import _FakeTarget


def test_attach_loop_uses_exact_session_targets() -> None:
    wrapper = _attach_loop_wrapper("backend", "/tmp/backend.sock")
    assert "tmux -S /tmp/backend.sock has-session -t '=backend'" in wrapper
    assert "tmux -S /tmp/backend.sock attach -t '=backend'" in wrapper


def test_attach_joins_existing_canonical_runtime_without_building(
    db: Database,
    console_target_factory: Callable[[TmuxModel], _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    model = TmuxModel()
    model.seed_session("aw-console-con", "alpha")
    target = console_target_factory(model)
    interactive: list[str] = []
    target.interactive = lambda command, **kwargs: interactive.append(command) or 0  # type: ignore[attr-defined]

    assert (
        attach_console(
            db,
            _StubConfig(),
            name="con",
            allow_nesting=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
        == 0
    )
    assert interactive == ["tmux attach -t '=aw-console-con'"]
    assert not any("new-session" in command for command in target.commands)

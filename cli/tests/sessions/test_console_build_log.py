"""A console build writes the admin's requests to its ``console-build`` log, with the build's values redacted."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.bootstrap as bootstrap
from agentworks.execution.access import Admin
from agentworks.sessions import multi_console as mc
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.conftest import _FakeResult
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from agentworks.db import Database
    from agentworks.execution.results import Result
    from tests.conftest import _ScriptedGuest
    from tests.vm_access_support import Call

VALUE = "s3cret-pane-value"


def test_the_build_logs_each_admin_request_with_its_values_redacted(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    console_guest_factory: Callable[..., _ScriptedGuest],
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a+1"])
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    monkeypatch.setattr("agentworks.execution.oplog.LOG_DIR", tmp_path)
    model = TmuxModel()
    # A guest that prints a value back: the layout step's output is the one place it can reach the log.
    guest = console_guest_factory(model, responses={"select-layout": _FakeResult(stdout=f"{VALUE}\n")})
    assert guest.recorder is not None
    recorder = guest.recorder
    answer = guest.answer
    logged: list[Call] = []

    def answered_and_logged(call: Call) -> Result | None:
        """Answer ``call``, then write it to the access's log, as a real access writes each request it runs."""
        result = answer(call)
        log = recorder.logs[-1]
        if result is not None and log is not None and call.identity == Admin():
            log(call.request, result)
            logged.append(call)
        return result

    monkeypatch.setattr(guest, "answer", answered_and_logged)
    vm = db.get_vm("vm1")
    console = db.get_console("con")
    assert vm is not None and console is not None

    mc._build_console_tmux(guest.console_access(vm), db, registry, console, values={"PANE": VALUE}, layout="tiled")

    assert model.has_session("aw-console-con")
    [path] = tmp_path.glob("vm1-*-console-build.log")
    text = path.read_text()
    assert logged
    assert text.count("request (") == len(logged)
    assert VALUE not in text

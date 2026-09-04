"""Runtime observation tests for named consoles."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from agentworks.db import ConsoleRow
from agentworks.errors import ConnectivityError, NotFoundError
from agentworks.sessions.multi_console._status import (
    ConsoleStatus,
    _enumerate_tmux_sessions,
    classify_console_status,
    observe_console_statuses,
)
from agentworks.sessions.multi_console.attach import ConsoleListing, console_listing, render_console_listing


@dataclass
class _Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class _Target:
    def __init__(self, result: _Result) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def run(self, command: str, **kwargs: object) -> _Result:
        self.calls.append({"command": command, **kwargs})
        return self.result


@pytest.mark.parametrize(
    ("canonical", "staging", "expected"),
    [
        (True, False, ConsoleStatus.RUNNING),
        (False, False, ConsoleStatus.STOPPED),
        (True, True, ConsoleStatus.RESIDUAL),
        (False, True, ConsoleStatus.RESIDUAL),
    ],
)
def test_console_status_classifier(
    canonical: bool,
    staging: bool,
    expected: ConsoleStatus,
) -> None:
    assert classify_console_status(canonical_present=canonical, staging_present=staging) is expected


def test_console_enumeration_is_bounded_and_noninteractive() -> None:
    target = _Target(_Result(0, stdout="aw-console-alpha\noperator-session\n"))

    assert _enumerate_tmux_sessions(target) == {"aw-console-alpha", "operator-session"}  # type: ignore[arg-type]
    assert target.calls == [
        {
            "command": "tmux list-sessions -F '#{session_name}'",
            "check": False,
            "tty": False,
            "timeout": 10,
            "retries": 1,
        }
    ]


def test_console_enumeration_accepts_only_authoritative_absence() -> None:
    missing = _Target(_Result(1, stderr="no server running on /tmp/tmux-1000/default"))
    mixed = _Target(_Result(1, stdout="no server running on /tmp/tmux-1000/default", stderr="extra"))

    assert _enumerate_tmux_sessions(missing) == set()  # type: ignore[arg-type]
    assert _enumerate_tmux_sessions(mixed) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("stdout", ["aw-console-alpha\n\n", "aw-console-alpha\x00tail\n", "aw-console-alpha\rjunk\n"])
def test_console_success_stream_must_be_well_formed(stdout: str) -> None:
    assert _enumerate_tmux_sessions(_Target(_Result(0, stdout=stdout))) is None  # type: ignore[arg-type]


def test_console_observer_isolates_unreachable_vm_and_uses_exact_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consoles = [
        ConsoleRow("alpha", "vm-a", False, "", ""),
        ConsoleRow("alphabet", "vm-a", False, "", ""),
        ConsoleRow("beta", "vm-b", False, "", ""),
    ]
    vms = {name: object() for name in ("vm-a", "vm-b")}

    class _DB:
        def get_vm(self, name: str) -> object:
            return vms[name]

    def require(_db: object, _config: object, vm: object) -> None:
        if vm is vms["vm-b"]:
            raise ConnectivityError("offline")

    target = _Target(_Result(0, stdout="aw-console-alpha\naw-console-build+alphabet\n"))
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", require)
    monkeypatch.setattr("agentworks.transports.transport", lambda *_args, **_kwargs: target)

    result = observe_console_statuses(_DB(), object(), consoles)  # type: ignore[arg-type]

    assert result == {
        "alpha": ConsoleStatus.RUNNING,
        "alphabet": ConsoleStatus.RESIDUAL,
        "beta": ConsoleStatus.UNKNOWN,
    }
    assert len(target.calls) == 1


def test_console_observer_preserves_missing_vm_as_structural_failure() -> None:
    class _DB:
        def get_vm(self, _name: str) -> None:
            return None

    with pytest.raises(NotFoundError) as caught:
        observe_console_statuses(
            _DB(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            [ConsoleRow("alpha", "missing", False, "", "")],
        )
    assert caught.value.entity_kind == "vm"
    assert caught.value.entity_name == "missing"


def test_console_observer_preserves_corrupt_ssh_applied_state(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import StateError

    db.insert_vm("box", site="site", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_console("alpha", "box")
    console = db.get_console("alpha")
    assert console is not None
    structural = StateError("corrupt SSH applied state", entity_kind="vm", entity_name="box")
    monkeypatch.setattr(
        "agentworks.vms.manager.require_vm_ssh_boundary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(structural),
    )
    monkeypatch.setattr(
        "agentworks.transports.transport",
        lambda *_args, **_kwargs: pytest.fail("transport constructed after structural failure"),
    )

    with pytest.raises(StateError) as caught:
        observe_console_statuses(db, object(), [console])  # type: ignore[arg-type]

    assert caught.value is structural


def test_console_observer_uses_only_identity_and_transport_read_seams(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    db.insert_console("alpha", "box")
    console = db.get_console("alpha")
    assert console is not None
    target = _Target(_Result(0, stdout="aw-console-alpha\n"))
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agentworks.transports.transport", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(
        "agentworks.orchestration.activation.activation_gate",
        lambda *_args, **_kwargs: pytest.fail("console observation activated a VM"),
    )
    monkeypatch.setattr(
        "agentworks.secrets.resolver.Resolver.resolve",
        lambda *_args, **_kwargs: pytest.fail("console observation resolved secrets"),
    )
    changes_before = db._conn.total_changes  # noqa: SLF001

    result = observe_console_statuses(db, object(), [console])  # type: ignore[arg-type]

    assert result == {"alpha": ConsoleStatus.RUNNING}
    assert db._conn.total_changes == changes_before  # noqa: SLF001


def test_console_listing_is_local_until_status_is_requested(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    db.insert_console("alpha", "box")
    calls: list[tuple[str, ...]] = []

    def observe(_db: object, _config: object, consoles: list[ConsoleRow]):
        calls.append(tuple(console.name for console in consoles))
        return {"alpha": ConsoleStatus.RUNNING}

    monkeypatch.setattr("agentworks.sessions.multi_console.observe_console_statuses", observe)

    plain = console_listing(db)
    observed = console_listing(
        db,
        object(),  # type: ignore[arg-type]
        include_status=True,
    )

    assert plain.consoles[0].status == "unavailable"
    assert observed.consoles[0].status == "running"
    assert calls == [("alpha",)]


def test_console_listing_status_column_follows_explicit_render_request(
    db,  # noqa: ANN001
    captured_output,  # noqa: ANN001
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    db.insert_console("alpha", "box")
    listing = console_listing(db)

    render_console_listing(listing)
    assert "STATUS" not in captured_output.info[0]

    captured_output.info.clear()
    observed = ConsoleListing(
        consoles=(replace(listing.consoles[0], status="running"),),
    )
    render_console_listing(observed, include_status=True)
    assert "STATUS" in captured_output.info[0]
    assert captured_output.info[2].index("running") == captured_output.info[0].index("STATUS")

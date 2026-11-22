"""Runtime observation tests for named consoles."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.db import ConsoleRow
from agentworks.errors import ConnectivityError, NotFoundError
from agentworks.execution.errors import TrustMissing
from agentworks.execution.requests import Script, Shell
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.sessions.multi_console._status import (
    ConsoleStatus,
    _enumerate_tmux_sessions,
    classify_console_status,
    observe_console_statuses,
)
from agentworks.sessions.multi_console.attach import (
    ConsoleListing,
    console_description,
    console_listing,
    render_console_listing,
)
from tests.vm_access_support import AccessRecorder, Admin, RecordingVMAccess, install_access_recorder


def _admin(returncode: int, stdout: str = "", stderr: str = "") -> RecordingVMAccess:
    """An admin access whose every request reports ``returncode`` with these streams."""
    access = RecordingVMAccess([], Admin())
    access.default_outcome = Result(Completed(returncode), stdout.encode(), stderr.encode())
    return access


def _recorder(monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str = "") -> AccessRecorder:
    """Route ``execution.vm_access`` to recording accesses that each report ``returncode`` and ``stdout``."""
    recorder = install_access_recorder(monkeypatch)
    recorder.answer = lambda _call: Result(Completed(returncode), stdout.encode())
    return recorder


def _vm(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


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


def test_console_enumeration_is_one_bounded_request_as_the_admin() -> None:
    admin = _admin(0, stdout="aw-console-alpha\noperator-session\n")

    assert _enumerate_tmux_sessions(admin) == {"aw-console-alpha", "operator-session"}
    assert admin.requests() == [Script("tmux list-sessions -F '#{session_name}'", Shell.SH, deadline=10)]


def test_console_enumeration_accepts_only_authoritative_absence() -> None:
    missing = _admin(1, stderr="no server running on /tmp/tmux-1000/default")
    mixed = _admin(1, stdout="no server running on /tmp/tmux-1000/default", stderr="extra")

    assert _enumerate_tmux_sessions(missing) == set()
    assert _enumerate_tmux_sessions(mixed) is None


@pytest.mark.parametrize("stdout", ["aw-console-alpha\n\n", "aw-console-alpha\x00tail\n", "aw-console-alpha\rjunk\n"])
def test_console_success_stream_must_be_well_formed(stdout: str) -> None:
    assert _enumerate_tmux_sessions(_admin(0, stdout=stdout)) is None


def test_console_observer_isolates_unreachable_vm_and_uses_exact_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consoles = [
        ConsoleRow("alpha", "vm-a", False, "", ""),
        ConsoleRow("alphabet", "vm-a", False, "", ""),
        ConsoleRow("beta", "vm-b", False, "", ""),
    ]
    vms = {name: _vm(name) for name in ("vm-a", "vm-b")}

    class _DB:
        def get_vm(self, name: str) -> object:
            return vms[name]

    def require(_db: object, _config: object, vm: object) -> None:
        if vm is vms["vm-b"]:
            raise ConnectivityError("offline")

    recorder = _recorder(monkeypatch, 0, stdout="aw-console-alpha\naw-console-build+alphabet\n")
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", require)

    result = observe_console_statuses(_DB(), object(), consoles)  # type: ignore[arg-type]

    assert result == {
        "alpha": ConsoleStatus.RUNNING,
        "alphabet": ConsoleStatus.RESIDUAL,
        "beta": ConsoleStatus.UNKNOWN,
    }
    assert recorder.operations == [("admin", "run")]


def test_console_observer_leaves_only_an_unreachable_vms_consoles_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A VM that passes the SSH boundary but does not answer costs only its own consoles their status.

    Building an access is passive, so the failure arrives with the VM's one
    request, inside its own observation.
    """
    consoles = [ConsoleRow("alpha", "vm-a", False, "", ""), ConsoleRow("beta", "vm-b", False, "", "")]
    vms = {name: _vm(name) for name in ("vm-a", "vm-b")}

    class _DB:
        def get_vm(self, name: str) -> object:
            return vms[name]

    answers = {
        "vm-a": Result(Completed(0), b"aw-console-alpha\n"),
        "vm-b": Result(Unknown(UnknownReason.CONNECTION)),
    }

    def access(vm: SimpleNamespace, _config: object, identity: Admin, **_kwargs: object) -> RecordingVMAccess:
        built = RecordingVMAccess([], identity, vm_name=vm.name)
        built.default_outcome = answers[vm.name]
        return built

    monkeypatch.setattr("agentworks.execution.vm_access", access)
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args: None)

    result = observe_console_statuses(_DB(), object(), consoles)  # type: ignore[arg-type]

    assert result == {"alpha": ConsoleStatus.RUNNING, "beta": ConsoleStatus.UNKNOWN}


def test_console_observer_reports_a_vm_without_a_host_key_record_and_observes_the_others(
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    consoles = [ConsoleRow("alpha", "vm-a", False, "", ""), ConsoleRow("beta", "vm-b", False, "", "")]
    vms = {name: _vm(name) for name in ("vm-a", "vm-b")}

    class _DB:
        def get_vm(self, name: str) -> object:
            return vms[name]

    def require(_db: object, _config: object, vm: object) -> None:
        if vm is vms["vm-b"]:
            raise TrustMissing("vm-b")

    _recorder(monkeypatch, 0, stdout="aw-console-alpha\n")
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", require)

    result = observe_console_statuses(_DB(), object(), consoles)  # type: ignore[arg-type]

    assert result == {"alpha": ConsoleStatus.RUNNING, "beta": ConsoleStatus.UNKNOWN}
    assert len(captured_output.warnings) == 1
    assert "vm-b" in captured_output.warnings[0]


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
    recorder = install_access_recorder(monkeypatch)

    with pytest.raises(StateError) as caught:
        observe_console_statuses(db, object(), [console])  # type: ignore[arg-type]

    assert caught.value is structural
    assert recorder.calls == []


def test_console_observer_uses_only_identity_and_access_read_seams(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    db.insert_console("alpha", "box")
    console = db.get_console("alpha")
    assert console is not None
    _recorder(monkeypatch, 0, stdout="aw-console-alpha\n")
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args, **_kwargs: None)
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

    def observe(_db: object, _config: object, consoles: list[ConsoleRow]) -> dict[str, ConsoleStatus]:
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


def test_plain_console_listing_preserves_orphaned_inventory(
    db,  # noqa: ANN001
) -> None:
    db.insert_vm("removed-vm", site="site", hostname="removed-vm")
    db.insert_console("orphan", "removed-vm")
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'removed-vm'")
    db._conn.commit()
    db._conn.execute("PRAGMA foreign_keys = ON")

    listing = console_listing(db)

    assert [(row.name, row.vm_name, row.status) for row in listing.consoles] == [
        ("orphan", "removed-vm", "unavailable")
    ]


def test_live_console_listing_isolates_orphaned_inventory(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    db.insert_console("healthy", "box")
    db.insert_vm("removed-vm", site="site", hostname="removed-vm")
    db.insert_console("orphan", "removed-vm")
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'removed-vm'")
    db._conn.commit()
    db._conn.execute("PRAGMA foreign_keys = ON")
    observed: list[tuple[str, ...]] = []

    def observe(_db: object, _config: object, consoles: list[ConsoleRow]):
        observed.append(tuple(console.name for console in consoles))
        return {"healthy": ConsoleStatus.RUNNING}

    monkeypatch.setattr("agentworks.sessions.multi_console.observe_console_statuses", observe)
    changes_before = db._conn.total_changes

    listing = console_listing(db, object(), include_status=True)  # type: ignore[arg-type]

    assert db._conn.total_changes == changes_before
    assert [(row.name, row.status) for row in listing.consoles] == [
        ("healthy", "running"),
        ("orphan", "unknown"),
    ]
    assert observed == [("healthy",)]


def test_console_description_rejects_orphaned_inventory(
    db,  # noqa: ANN001
) -> None:
    db.insert_vm("removed-vm", site="site", hostname="removed-vm")
    db.insert_console("orphan", "removed-vm")
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'removed-vm'")
    db._conn.commit()
    db._conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(NotFoundError) as caught:
        console_description(db, object(), name="orphan")  # type: ignore[arg-type]

    assert caught.value.entity_kind == "vm"
    assert caught.value.entity_name == "removed-vm"


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
    assert captured_output.info[2].rindex("running") == captured_output.info[0].index("STATUS")

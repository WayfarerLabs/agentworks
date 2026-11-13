"""``workspace rehome`` through the orchestrated model.

Its pre-gate refusals (a rejected policy, overlapping or unaddressable paths,
a target that is not an absolute directory), its inherently post-gate
directory checks, path resolution and confirm (they need the VM), the
canonical ACL it applies, and the first repair after it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateError, UserAbort, ValidationError
from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Completed, Result
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces import manager as workspace_manager
from agentworks.workspaces.acls import workspace_acl_commands
from agentworks.workspaces.guest import open_parent_traversal
from tests.vm_access_support import Admin, RecordingVMAccess
from tests.workspaces._lifecycle_support import (
    ConvergingGuest,
    Guest,
    RehomeGuest,
    arguments,
    no_gate,
    reachable,
    route,
    seed,
    seed_live_session,
    starts,
    stop_the_vm,
)
from tests.workspaces._lifecycle_support import guest as guest
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.conftest import CapturedOutput

COPY = starts("rsync")


def _rehome(db: Database, config: object, target_path: str = "/dst/ws1", **options: object) -> None:
    workspace_manager.rehome_workspace(
        db,
        config,  # type: ignore[arg-type]
        "ws1",
        target_path=target_path,
        interaction=TtyInteractionPolicy.REFUSE,
        **options,  # type: ignore[arg-type]
    )


# -- validation stays pre-gate ------------------------------------------------


def test_rehome_non_enum_policy_raises_before_any_session_probe(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session-status guard reaches the VM and writes repaired PIDs, and
    the boundary that would otherwise reject the policy sits past all of it.
    The check at the top of ``rehome_workspace`` is what keeps a rejected
    rehome from touching the VM first."""
    from agentworks.sessions import manager as sessions_manager

    config = make_config()
    seed(db)
    seed_live_session(db, name="s1", ws="ws1")
    no_gate(monkeypatch)

    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("sessions must not be probed for a rejected policy")

    monkeypatch.setattr(sessions_manager, "ensure_pids_batch", _boom)
    monkeypatch.setattr(sessions_manager, "observe_session_statuses", _boom)

    with pytest.raises(StateError):
        workspace_manager.rehome_workspace(
            db,
            config,
            "ws1",
            target_path="/dst/ws1",
            interaction="refuse",  # type: ignore[arg-type]
        )

    assert resolve_counter == []
    assert guest.requests == []


@pytest.mark.parametrize(
    ("stored", "target_path"),
    [
        ("/srv/ws1", "/srv/ws1/nested"),
        ("/opt/agentworks/workspaces//ws1", "/opt/agentworks/workspaces/ws1/inner"),
    ],
    ids=["nested", "stored-double-slash"],
)
def test_rehome_overlapping_paths_fail_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    stored: str,
    target_path: str,
) -> None:
    config = make_config()
    seed(db)
    db._conn.execute("UPDATE workspaces SET workspace_path = ? WHERE name = 'ws1'", (stored,))
    db._conn.commit()
    no_gate(monkeypatch)

    with pytest.raises(ValidationError):
        _rehome(db, config, target_path)

    assert resolve_counter == []
    assert guest.requests == []


def test_rehome_refuses_a_stored_path_that_is_not_absolute_before_anything_runs(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A '~' row, which the state migration leaves relative, names no one directory on the VM."""
    config = make_config()
    seed(db)
    db._conn.execute("UPDATE workspaces SET workspace_path = '~/ws/ws1' WHERE name = 'ws1'")
    db._conn.commit()
    reachable(monkeypatch, True)

    with pytest.raises(ValidationError):
        _rehome(db, config, yes=True)

    assert resolve_counter == []
    assert guest.requests == []
    assert db.get_workspace("ws1") is not None


@pytest.mark.parametrize("target_path", ["dst/ws1", "host:/dst/ws1", "/", "/dst/../ws1", "/dst//ws1", "/dst/./ws1"])
def test_rehome_refuses_a_target_that_is_not_an_absolute_directory_before_anything_runs(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    target_path: str,
) -> None:
    config = make_config()
    seed(db)
    no_gate(monkeypatch)

    with pytest.raises(ValidationError):
        _rehome(db, config, target_path)

    assert resolve_counter == []
    assert guest.requests == []


# -- the directory checks, the resolution and the confirm --------------------


def test_rehome_of_a_stored_path_with_a_dotdot_stores_the_target(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Rehome is how such a row is repaired: the guest resolves the source, and the new row has no '..'."""
    config = make_config()
    seed(db)
    db._conn.execute("UPDATE workspaces SET workspace_path = '/home/admin/../srv/ws1' WHERE name = 'ws1'")
    db._conn.commit()
    reachable(monkeypatch, True)
    fake = RehomeGuest()
    route(monkeypatch, fake)

    _rehome(db, config, yes=True)

    ws = db.get_workspace("ws1")
    assert ws is not None and ws.workspace_path == "/dst/ws1"
    assert ("rsync", "-a", "--", "/home/admin/../srv/ws1/", "/dst/ws1/") in fake.argvs()


def test_rehome_refuses_a_target_inside_the_source_through_a_link(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spellings differ, but the guest resolves the target into the source: nothing is copied or removed."""
    config = make_config()
    seed(db)
    db._conn.execute("UPDATE workspaces SET workspace_path = '/data/ws/ws1' WHERE name = 'ws1'")
    db._conn.commit()
    reachable(monkeypatch, True)
    fake = Guest(fails=(("test", "-d", "/mnt/x/ws/ws1/inner"),))
    fake.links = {"/data/ws": "/mnt/x/ws"}
    route(monkeypatch, fake)

    with pytest.raises(ValidationError):
        _rehome(db, config, "/mnt/x/ws/ws1/inner", remove_old=True, yes=True)

    # The two directory checks, then the resolution of both paths; nothing after.
    assert [request.argv[:2] if isinstance(request, Command) else "script" for request in fake.requests] == [
        ("test", "-d"),
        ("test", "-d"),
        "script",
    ]
    row = db.get_workspace("ws1")
    assert row is not None and row.workspace_path == "/data/ws/ws1"


def test_rehome_resolves_both_paths_as_root() -> None:
    """As the copy runs: an unsearchable component would otherwise come back unresolved."""
    from agentworks.workspaces.manager.rehome import _resolved_paths

    access = RecordingVMAccess([], Admin())
    access.default_outcome = Result(Completed(0), b"/mnt/x/ws1\0/dst/ws1\0")

    assert _resolved_paths(access, "/data/ws1", "/dst/ws1") == ("/mnt/x/ws1", "/dst/ws1")
    [request] = access.requests()
    assert request.elevated


@pytest.mark.parametrize(
    "stdout",
    [b"/mnt/x/ws1\0", b"/mnt/x/ws1\0/dst/ws1\0/third\0", b"mnt/x/ws1\0/dst/ws1\0", b"/mnt/x/ws1\0/dst/ws1\0trailing"],
    ids=["one-path", "three-paths", "not-absolute", "trailing-data"],
)
def test_rehome_refuses_a_resolution_that_is_not_two_absolute_paths(stdout: bytes) -> None:
    from agentworks.workspaces.manager.rehome import _resolved_paths

    access = RecordingVMAccess([], Admin())
    access.default_outcome = Result(Completed(0), stdout)

    with pytest.raises(StateError):
        _resolved_paths(access, "/data/ws1", "/dst/ws1")


def test_rehome_reads_a_target_with_a_trailing_slash_as_the_directory(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    seed(db)
    reachable(monkeypatch, True)
    fake = RehomeGuest()
    route(monkeypatch, fake)

    _rehome(db, config, "/dst/ws1//", yes=True)

    ws = db.get_workspace("ws1")
    assert ws is not None and ws.workspace_path == "/dst/ws1"
    assert ("test", "-d", "/dst/ws1") in fake.argvs()
    assert not any("/dst/ws1//" in argument for request in fake.requests for argument in arguments(request))


def test_rehome_confirm_sits_inside_the_span_after_the_dir_checks(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The order pin, which doubles as rehome's gate-prompt parity
    carry: on a stopped VM the gate's just-in-time token resolve is
    the ONLY backend pass (the boundary is fully seeded), then the
    gate events, then the directory checks and the resolution of both
    paths, then the confirm (which renders their results). A declined
    confirm raises UserAbort and leaves the DB path unchanged; by then
    the gate has already run, because the checks the prompt reports on
    need the VM (inherently post-gate)."""
    from agentworks import output as output_mod

    config = make_config()
    seed(db)
    events: list[str] = []
    stop_the_vm(monkeypatch, events)
    fake = Guest(events=events, fails=(("test", "-d", "/dst/ws1"),))
    route(monkeypatch, fake)

    def _decline(message: str, default: bool = False) -> bool:
        events.append("confirm")
        return False

    monkeypatch.setattr(output_mod, "confirm", _decline)

    with pytest.raises(UserAbort, match="rehome cancelled"):
        _rehome(db, config)

    assert events == [
        "status",
        "status",
        "start",
        "tailscale",
        "run:test -d /srv/ws1",
        "run:test -d /dst/ws1",
        "run:script /srv/ws1 /dst/ws1",
        "confirm",
    ]
    assert isinstance(fake.requests[-1], Script) and fake.requests[-1].elevated
    assert resolve_counter == [["proxmox-token"]]
    ws = db.get_workspace("ws1")
    assert ws is not None and ws.workspace_path == "/srv/ws1"


# -- the canonical ACL, the tmuxinator config and the first repair after ------


def test_rehome_routes_through_the_canonical_acl_helper(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Rehome applies the ACL through ``apply_workspace_acls`` (issue #263),
    after the copy and before the ancestor traversal is re-granted."""
    config = make_config()
    seed(db)
    reachable(monkeypatch, True)
    fake = RehomeGuest()
    route(monkeypatch, fake)

    _rehome(db, config, yes=True)

    acls = workspace_acl_commands("/dst/ws1")
    assert all(command in fake.requests for command in acls)
    assert fake.index(COPY) < fake.requests.index(acls[0]) < fake.requests.index(open_parent_traversal("/dst/ws1"))
    row = db.get_workspace("ws1")
    assert row is not None and row.workspace_path == "/dst/ws1"


def test_rehome_removes_tmuxinator_projections_when_live_overlay_disables_them(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.instance_specs import parse_instance_spec

    config = make_config()
    seed(db)
    db._conn.execute("UPDATE workspaces SET template = 'copied' WHERE name = 'ws1'")
    db._conn.commit()
    overlay = parse_instance_spec("workspace", '{"tmuxinator":false}')
    db.instance_state.put_desired_overlay("workspace", "ws1", overlay.payload)
    reachable(monkeypatch, True)
    fake = RehomeGuest()
    route(monkeypatch, fake)

    _rehome(db, config, yes=True)

    assert not any(path.endswith("/.tmuxinator.yml") for path in fake.written)
    assert any(
        command[:2] == ("rm", "-f") and any(argument.endswith(".tmuxinator.yml") for argument in command)
        for command in fake.argvs()
    )


def test_first_repair_after_rehome_is_a_noop(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A rehomed workspace lands in the canonical ACL state because rehome
    applies the IDENTICAL spec through the shared ``apply_workspace_acls``
    helper (issue #263), so its first ``workspace repair`` is a true ACL
    no-op: OK: ACLs and No issues found."""
    fake = ConvergingGuest(path="/dst/ws1", rehome_target="/dst/ws1")
    route(monkeypatch, fake)
    config = make_config()
    seed(db)
    reachable(monkeypatch, True)

    _rehome(db, config, yes=True)
    assert fake.acl_canonical

    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert "OK: ACLs" in captured_output.detail
    assert not any(line.startswith("Fixed:") for line in captured_output.detail)
    assert "\nNo issues found" in captured_output.info

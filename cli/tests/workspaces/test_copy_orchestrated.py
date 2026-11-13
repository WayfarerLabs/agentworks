"""``workspace copy`` through the orchestrated model.

Its sequential two-boundary composition (one per VM, nested holds; exactly
one on the same-VM path), the source tree streamed into the destination with
nothing staged, its pre-gate refusals, the grant-all materialization onto the
destination VM (issue #321), the canonical ACL it applies, and the first
repair after it.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.agents import grants as agent_grants
from agentworks.db import InitStatus
from agentworks.errors import NotFoundError
from agentworks.execution.requests import Command, FromFile, ToFile
from agentworks.execution.results import Completed, Result
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces import manager as workspace_manager
from agentworks.workspaces.acls import workspace_acl_commands
from tests.vm_access_support import install_access_recorder
from tests.workspaces._lifecycle_support import (
    ConvergingGuest,
    Guest,
    arguments,
    is_occupancy_probe,
    no_gate,
    reachable,
    route,
    seed,
    seed_workspace,
    starts,
)
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database
    from tests.conftest import CapturedOutput
    from tests.vm_access_support import Call

PACK = starts("tar", "czf")
UNPACK = starts("tar", "xzf")


class CopyGuest(Guest):
    """A guest for a copy: the source read records ``pack`` in ``events``, and the destination is free."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(events=events)
        self._pack_events = events

    def respond(self, request: object) -> Result | None:
        if PACK(request):
            self._pack_events.append("pack")
        if is_occupancy_probe(request):
            return Result(Completed(1))
        return super().respond(request)


def _wire_copy(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> CopyGuest:
    """The copy command's guest, and hold-span recording on the platform's ``vm_active``."""
    fake = CopyGuest(events)
    route(monkeypatch, fake)
    real_vm_active = ProxmoxPlatform.vm_active

    @contextlib.contextmanager
    def _recording_hold(self: ProxmoxPlatform, row, *, config=None):  # noqa: ANN001, ANN202
        events.append(f"hold-enter:{row.name}")
        with real_vm_active(self, row, config=config):
            yield
        events.append(f"hold-exit:{row.name}")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold)
    return fake


def _second_vm(db: Database) -> None:
    db.insert_vm("box2", site="proxmox", hostname="box2")
    db.update_vm_tailscale("box2", "100.64.0.10")
    db.update_vm_init_status("box2", InitStatus.COMPLETE)


def _copy(db: Database, config: object, *, vm_name: str = "box", source: str = "ws1") -> None:
    workspace_manager.copy_workspace(
        db,
        config,  # type: ignore[arg-type]
        source,
        dest_name="ws2",
        vm_name=vm_name,
        interaction=TtyInteractionPolicy.REFUSE,
    )


def test_copy_cross_vm_runs_two_sequential_boundaries_with_nested_holds(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Cross-VM copy: TWO boundary bursts in order (source first, then
    dest; one shared site config here, so both bursts name the site's
    secret), the dest boundary only entered after the pack (the dest VM is
    resolved mid-command), and BOTH holds open concurrently (the dest span
    nests inside the source span)."""
    config = make_config()
    seed(db)
    _second_vm(db)
    reachable(monkeypatch, True)
    events: list[str] = []
    fake = _wire_copy(monkeypatch, events)

    _copy(db, config, vm_name="box2")

    assert resolve_counter == [["proxmox-token"], ["proxmox-token"]]
    assert events.index("hold-enter:box") < events.index("pack")
    assert events.index("pack") < events.index("hold-enter:box2")
    assert events.index("hold-exit:box2") < events.index("hold-exit:box")
    row = db.get_workspace("ws2")
    assert row is not None and row.vm_name == "box2" and row.template == "copied"
    assert any(UNPACK(request) for request in fake.requests)
    assert "Workspace 'ws1' copied to 'ws2'" in captured_output.info


def test_copy_streams_the_source_tree_into_the_destination_with_nothing_staged(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The source's tar stream lands in a new workstation file and is the
    destination extraction's input, root's and without the source's owners;
    nothing is staged on the destination, and the local file is gone after."""
    from agentworks.workspaces.backends.vm import default_workspace_path

    config = make_config()
    seed(db)
    _second_vm(db)
    reachable(monkeypatch, True)
    fake = _wire_copy(monkeypatch, [])
    archive = b"\x1f\x8b fixture archive \r\n\x00"
    delivered: list[tuple[Path, bytes]] = []
    source = Command(("tar", "czf", "-", "-C", "/srv/ws1", "."))

    def answer(call: Call) -> Result | None:
        request = call.request
        if request == source:
            assert isinstance(call.options["stdout"], ToFile)
            return Result(Completed(0), archive)
        if isinstance(request, Command) and isinstance(request.stdin, FromFile):
            delivered.append((request.stdin.path, request.stdin.path.read_bytes()))
            assert request == Command(
                ("tar", "xzf", "-", "--no-same-owner", "-C", default_workspace_path(config, "ws2")),
                elevated=True,
                stdin=FromFile(request.stdin.path),
            )
        return fake.answer(call)

    install_access_recorder(monkeypatch).answer = answer

    _copy(db, config, vm_name="box2")

    [(path, content)] = delivered
    assert content == archive
    assert not path.exists() and not path.parent.exists()
    named = [argument for request in fake.requests for argument in arguments(request)]
    assert not any("/var/tmp" in argument or "mktemp" in argument for argument in named)


def test_copy_same_vm_reuses_the_source_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Same-VM copy: exactly ONE boundary (no second resolve, no second
    hold); the source composition already gates and holds the one VM."""
    config = make_config()
    seed(db)
    reachable(monkeypatch, True)
    events: list[str] = []
    _wire_copy(monkeypatch, events)

    _copy(db, config)

    assert resolve_counter == [["proxmox-token"]]
    assert events.count("hold-enter:box") == 1
    row = db.get_workspace("ws2")
    assert row is not None and row.vm_name == "box"
    assert "Workspace 'ws1' copied to 'ws2'" in captured_output.info


def test_copy_refusals_fail_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copy's cheap row refusals stay pre-everything: an unknown source
    workspace and an already-existing destination both fail before any
    prompt, resolve, or gate event."""
    from agentworks.errors import AlreadyExistsError

    config = make_config()
    seed(db)
    no_gate(monkeypatch)

    with pytest.raises(NotFoundError, match="workspace 'nope' not found"):
        _copy(db, config, source="nope")

    seed_workspace(db, vm_name="box", name="ws2")
    with pytest.raises(AlreadyExistsError, match="workspace 'ws2' already exists"):
        _copy(db, config)

    assert resolve_counter == []


# -- copy materializes grant-all agents on the dest VM (issue #321) -----------


def test_copy_materializes_grant_all_agents_on_the_dest_vm(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Copy inserts a workspace row, so it runs the same grant-all
    materialization pass as workspace create's realization body: one
    explicit grant row plus the on-VM group add per grant_all agent on the
    DESTINATION VM (issue #321). The source VM's grant_all agent gains
    nothing."""
    config = make_config()
    seed(db)
    _second_vm(db)
    db.insert_agent("dev", "box2", "agt-dev", template="default", grant_all=True)
    db.insert_agent("src", "box", "agt-src", template="default", grant_all=True)
    db.insert_agent_grant("src", "ws1", "explicit")  # src's invariant pre-copy
    reachable(monkeypatch, True)
    _wire_copy(monkeypatch, [])
    group_adds: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_grants,
        "add_to_workspace_group",
        lambda vm, config_, db_, linux_user, ws, **k: group_adds.append((linux_user, ws)),
    )

    _copy(db, config, vm_name="box2")

    assert group_adds == [("agt-dev", "ws2")]
    assert {(g.workspace_name, g.grant_type) for g in db.list_agent_grants("dev")} == {("ws2", "explicit")}
    assert {g.workspace_name for g in db.list_agent_grants("src")} == {"ws1"}
    assert "Added 1 grant-all agent(s) to workspace" in captured_output.detail


def test_copy_without_grant_all_agents_makes_no_grant_calls(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A dest VM whose agents carry no grant_all: the materialization pass
    is a no-op: no group calls, no grant rows, no summary line."""
    config = make_config()
    seed(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    reachable(monkeypatch, True)
    _wire_copy(monkeypatch, [])
    calls: list[object] = []
    monkeypatch.setattr(agent_grants, "add_to_workspace_group", lambda *a, **k: calls.append(a))

    _copy(db, config)

    assert calls == []
    assert db.list_agent_grants("dev") == []
    assert not any("grant-all" in d for d in captured_output.detail)


def test_copy_closes_the_operation_logs_exactly_once_on_cancellation(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The destination-VM mutation span holds an open operation log, and
    ``materialize_grant_all_agents``'s sanctioned SIGINT arm re-raises
    ``KeyboardInterrupt`` through it. A cancellation mid-span still lands
    the footer and exception section in the per-VM log, exactly once:
    ``close()`` is not idempotent, each call appends a footer. The source
    read's own log closed before."""
    from agentworks.ssh import SSHLogger

    config = make_config()
    seed(db)
    reachable(monkeypatch, True)
    _wire_copy(monkeypatch, [])
    closes: list[str] = []
    real_close = SSHLogger.close

    def _spying_close(self: SSHLogger) -> None:
        closes.append(str(self.path))
        real_close(self)

    monkeypatch.setattr(SSHLogger, "close", _spying_close)

    def _cancelled(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_grants, "materialize_grant_all_agents", _cancelled)

    with pytest.raises(KeyboardInterrupt):
        _copy(db, config)

    # The source read's log, then the destination's, each closed once.
    assert len(closes) == len(set(closes)) == 2


# -- #263: copy routes through the shared canonical ACL helper ----------------


def test_copy_applies_canonical_acl_after_unpack_and_chown(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Copy routes its ACL through ``apply_workspace_acls`` (issue #263),
    AFTER the tar unpack, the recursive chown and the SGID pass, so every
    unpacked entry is covered and future entries inherit."""
    from agentworks.workspaces.backends.vm import default_workspace_path

    config = make_config()
    seed(db)
    reachable(monkeypatch, True)
    fake = _wire_copy(monkeypatch, [])

    _copy(db, config)

    acls = workspace_acl_commands(default_workspace_path(config, "ws2"))
    assert all(command in fake.requests for command in acls)
    applied = fake.requests.index(acls[0])
    assert applied > fake.index(UNPACK)
    assert applied > fake.index(starts("chown", "-R"))
    assert applied > fake.index(lambda request: starts("find")(request) and "g+s" in arguments(request))


def test_first_repair_after_copy_is_a_noop(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A copied workspace lands in the canonical ACL state because copy
    applies the IDENTICAL spec through the shared ``apply_workspace_acls``
    helper (issue #263), so its first ``workspace repair`` is a true ACL
    no-op: OK: ACLs and No issues found, exactly like a freshly created
    workspace."""
    from agentworks.workspaces.backends.vm import default_workspace_path

    config = make_config()
    fake = ConvergingGuest(path=default_workspace_path(config, "ws2"), group="ws-ws2")
    route(monkeypatch, fake)
    seed(db)
    reachable(monkeypatch, True)

    _copy(db, config)
    assert fake.acl_canonical

    workspace_manager.repair_workspace(db, config, "ws2", interaction=TtyInteractionPolicy.REFUSE)

    assert "OK: ACLs" in captured_output.detail
    assert not any(line.startswith("Fixed:") for line in captured_output.detail)
    assert "\nNo issues found" in captured_output.info

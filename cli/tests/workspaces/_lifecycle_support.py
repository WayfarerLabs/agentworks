"""Fixtures, seeds and the guest double shared by the workspace lifecycle command tests.

``workspace repair``, ``rehome``, ``delete`` and ``copy`` run through the
orchestrated model with a real config, registry, resolver and backend loop
(env-var backend). The platform's power operations, the reachability probe and
the admin's access are the fakes: :class:`Guest` answers the access's typed
requests, and each test asserts on the requests it recorded.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.db import InitStatus, SessionMode, VMStatus
from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import Command, PythonProgram, Script
from agentworks.execution.results import Completed, Result
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.vms import manager as vm_manager
from agentworks.workspaces.acls import workspace_acl_commands
from agentworks.workspaces.guest import open_parent_traversal, path_occupied
from tests.conftest import ManifestDoc, seed_vm_trust, stub_vm_ssh_identity
from tests.vm_access_support import AccessRecorder, Call, install_access_recorder

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database

HEALTHY_ACL = "user::rwx\ngroup::rwx\nmask::rwx\nother::---"
DAMAGED_ACL = "user::rwx\ngroup::r-x\nother::---"  # narrowed group, mask stripped


@pytest.fixture(autouse=True)
def stub_ssh_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_vm_ssh_identity(monkeypatch)


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The shared ``make_config`` shape, plus a tmp ``[paths]`` section
    so delete's ``.code-workspace`` unlink and copy's VS Code stub
    never touch the operator's real directories."""
    from tests.orchestrated_fixtures import PLUGINS_ENABLED, proxmox_site, write_operator_config

    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    paths_section = f'[paths]\nvscode_workspaces = "{(tmp_path / "vscode").as_posix()}"\n'

    def _make(extra: str = "", *, manifests: Sequence[ManifestDoc | str] = ()):  # noqa: ANN202
        return write_operator_config(
            tmp_path, PLUGINS_ENABLED + paths_section + extra, manifests=[proxmox_site(), *manifests]
        )

    return _make


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch) -> Guest:
    """One healthy guest behind the admin's access."""
    fake = Guest()
    route(monkeypatch, fake)
    return fake


def seed(db: Database, *, ws: str = "ws1") -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    seed_vm_trust("box")
    # rehome and copy guard on init status pre-gate; the seeded row
    # must be COMPLETE for them (repair and delete never guarded).
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    seed_workspace(db, vm_name="box", name=ws)


def seed_workspace(db: Database, *, vm_name: str, name: str) -> None:
    db.insert_workspace(
        name,
        vm_name=vm_name,
        workspace_path=f"/srv/{name}",
        template="default",
        linux_group=f"ws-{name}",
    )


def seed_vm_only(db: Database) -> None:
    """A COMPLETE, reachable-host VM row with no workspace, for the create path to make one."""
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    seed_vm_trust("box")
    db.update_vm_init_status("box", InitStatus.COMPLETE)


def seed_live_session(db: Database, *, name: str, ws: str) -> None:
    """A session row that reads as alive (complete fingerprint + socket), so
    delete's status-aware kill loop probes and kills it."""
    db.insert_session(name, ws, "default", SessionMode.ADMIN)
    db.update_session_runtime(
        name,
        socket_path=f"/run/agentworks/admin-tmux-sockets/agentworks/{name}.sock",
        pid=4242,
        boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        tmux_server_start_ticks=77,
    )


def reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def stop_the_vm(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr("agentworks.vms.manager._await_tailnet", lambda *a, **k: events.append("tailscale"))


def no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _status_must_not_run(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for a command that must fail pre-gate")

    monkeypatch.setattr(ProxmoxPlatform, "status", _status_must_not_run)
    reachable(monkeypatch, False)


def node_holding(db: Database, config: object, platform: object, *, vm_name: str = "box"):  # noqa: ANN201
    """A live VM node for ``vm_name`` whose site holds the given platform: the
    shape a nested teardown hands ``delete_workspace`` (it re-enters the hold
    through ``vm_node.site.platform``)."""
    from agentworks.vms.nodes import LiveVMNode, VMSiteNode

    row = db.get_vm(vm_name)
    assert row is not None
    site = VMSiteNode("proxmox", platform, (), object())  # type: ignore[arg-type]
    return LiveVMNode(db, config, object(), row, site)  # type: ignore[arg-type]


def acl_block(path: str, entries: str) -> str:
    """One ``getfacl -R -n`` record: the ``# file:`` header, the owner/group
    comment lines the parser must ignore, then the ACL entries."""
    return f"# file: {path}\n# owner: 1000\n# group: 1000\n{entries}\n"


def argv(request: object) -> tuple[str, ...]:
    """A ``Command``'s argv, or nothing for any other request."""
    return request.argv if isinstance(request, Command) else ()


def script_args(request: object) -> tuple[str, ...]:
    """The positional parameters a ``workspaces.guest.script`` carries, or nothing for any other request."""
    if not isinstance(request, Script) or not request.source.startswith("set -- "):
        return ()
    return tuple(shlex.split(request.source.split("\n", 1)[0])[2:])


def arguments(request: object) -> tuple[str, ...]:
    """Every argument a request names: a command's argv or a script's positional parameters."""
    return argv(request) or script_args(request)


def is_occupancy_probe(request: object) -> bool:
    """Whether ``request`` is ``path_occupied``'s probe, of any path."""
    args = script_args(request)
    return len(args) == 1 and request == path_occupied(args[0])


Failing = Script | Command | tuple[str, ...] | Callable[[object], bool]
"""A request that exits 1: equal to it, a ``Command`` whose argv starts with the tuple, or one the callable names."""


class Guest:
    """The VM as the admin's access reaches it.

    Records each request in ``requests``, in order, and answers it as a
    healthy, converged guest would: exit 0 and no output. A request ``fails``
    names exits 1 instead. A file operation is recorded by its path in
    ``written`` and succeeds. ``links`` maps each directory link to its
    target, which rehome's path resolution follows. A subclass models more
    of the guest in :meth:`respond`, which may also raise. A non-zero exit
    under ``check`` raises ``CommandFailed``, as the access does. ``events``,
    when given, gets ``run:`` and each request's :func:`label`.
    """

    def __init__(self, *, events: list[str] | None = None, fails: Sequence[Failing] = ()) -> None:
        self.requests: list[Any] = []
        self.written: list[str] = []
        self.links: dict[str, str] = {}
        self._events = events
        self._fails = tuple(fails)

    def answer(self, call: Call) -> Result:
        request = call.request
        if isinstance(request, PythonProgram):
            if request.args[:1] == ("write",):
                self.written.append(json.loads(request.args[1])["path"])
            return Result(Completed(0))
        self.requests.append(request)
        if self._events is not None:
            self._events.append(f"run:{label(request)}")
        result = self.respond(request)
        if result is None:
            result = Result(Completed(1 if self.fails(request) else 0))
        status = result.completion.status if isinstance(result.completion, Completed) else 0
        if call.options["check"] and status:
            raise CommandFailed("box", status, result, streams_logged=True)
        return result

    def fails(self, request: object) -> bool:
        return any(
            failing(request)
            if callable(failing)
            else argv(request)[: len(failing)] == failing
            if isinstance(failing, tuple)
            else request == failing
            for failing in self._fails
        )

    def respond(self, request: object) -> Result | None:
        """This guest's answer to ``request``, or ``None`` for the default.

        The default guest resolves rehome's two paths (a root ``realpath``
        script) through ``links``.
        """
        args = script_args(request)
        if isinstance(request, Script) and request.elevated and "realpath" in request.source and len(args) == 2:
            if self.fails(request):
                return Result(Completed(1))
            return Result(Completed(0), b"".join(self._resolved(path).encode() + b"\0" for path in args))
        return None

    def argvs(self) -> list[tuple[str, ...]]:
        """Every command's argv, in order."""
        return [argv(request) for request in self.requests if isinstance(request, Command)]

    def index(self, matches: Callable[[object], bool]) -> int:
        """The position of the first request ``matches`` names."""
        return next(position for position, request in enumerate(self.requests) if matches(request))

    def _resolved(self, path: str) -> str:
        for link, target in self.links.items():
            if path == link or path.startswith(link + "/"):
                return target + path.removeprefix(link)
        return path


def label(request: object) -> str:
    """A request as a line: a command's argv joined, or ``script`` and its positional parameters."""
    if isinstance(request, Command):
        return shlex.join(request.argv)
    return shlex.join(("script", *script_args(request)))


def starts(*prefix: str) -> Callable[[object], bool]:
    """Names a ``Command`` whose argv starts with ``prefix``."""
    return lambda request: argv(request)[: len(prefix)] == prefix


def route(monkeypatch: pytest.MonkeyPatch, guest: Guest) -> AccessRecorder:
    """Route the admin's access to ``guest``."""
    recorder = install_access_recorder(monkeypatch)
    recorder.answer = guest.answer
    return recorder


class RepairProbeGuest(Guest):
    """A guest with chosen live-state divergences, so the apply-and-observe
    repair steps exercise their Fixed-vs-OK detection.

    ``damaged`` names the categories whose converging request reports a real
    change: 'owner'/'mode'/'sgid' (``chown -c`` / ``chmod -c`` print a change
    line), 'acl' (the before/after getfacl snapshots differ on a persisting
    path), 'traversal' (the parent walk prints a change line). Every other
    request answers with the silent output a healthy workspace gives, so the
    admin's membership probe sees itself already in the group and the getfacl
    snapshots match when 'acl' is absent.

    ``acl_churn`` models ordinary filesystem churn: the two getfacl snapshots
    share one persisting path (identical ACL) but each also carries a path the
    other lacks (a temp file created then renamed away). The intersect-compare
    must ignore those and report OK, not a spurious Fixed.
    """

    def __init__(
        self,
        *,
        damaged: frozenset[str] = frozenset(),
        acl_churn: bool = False,
        getfacl_ok: bool = True,
        path: str = "/srv/ws1",
        group: str = "ws-ws1",
    ) -> None:
        super().__init__()
        self._damaged = damaged
        self._acl_churn = acl_churn
        self._getfacl_ok = getfacl_ok
        self._path = path
        self._group = group
        self._getfacl_calls = 0

    def _getfacl_snapshot(self) -> str:
        self._getfacl_calls += 1
        first = self._getfacl_calls == 1
        record = self._path.lstrip("/")
        if "acl" in self._damaged and first:
            # Before: the persisting path's ACL is narrowed / mask-stripped.
            return acl_block(record, DAMAGED_ACL)
        if self._acl_churn:
            # A persisting path with identical ACL in both snapshots, plus a
            # path that exists in only one (churn the compare must ignore).
            churn = f"{record}/tmp-before" if first else f"{record}/tmp-after"
            return acl_block(record, HEALTHY_ACL) + "\n" + acl_block(churn, HEALTHY_ACL)
        return acl_block(record, HEALTHY_ACL)

    def respond(self, request: object) -> Result | None:
        command = argv(request)
        if command[:1] == ("getfacl",):
            if not self._getfacl_ok:
                # A failed snapshot: the ACL step has no change data to compare.
                return Result(Completed(1), b"", b"getfacl: error")
            return Result(Completed(0), self._getfacl_snapshot().encode())
        changed = ""
        if command[:3] == ("chown", "-R", "-c") and "owner" in self._damaged:
            changed = f"changed ownership of '{self._path}/f'\n"
        elif command == ("chmod", "-c", "2770", self._path) and "mode" in self._damaged:
            changed = f"mode of '{self._path}' changed from 0700 to 2770\n"
        elif "g+s" in command and command[:1] == ("find",) and "sgid" in self._damaged:
            changed = f"mode of '{self._path}/d' changed from 0770 to 2770\n"
        elif request == open_parent_traversal(self._path) and "traversal" in self._damaged:
            changed = "mode of '/srv' changed from 0700 to 0711\n"
        elif command[:2] == ("id", "-nG"):
            # The admin membership probe: already in the group, so that
            # detection-based step is a no-op in these scenarios.
            changed = f"admin {self._group}"
        return Result(Completed(0), changed.encode()) if changed else super().respond(request)


class ConvergingGuest(RepairProbeGuest):
    """A guest whose workspace ACL is damaged until a command applies the canonical one.

    For the first repair after a create, copy or rehome: that command applies
    the canonical ACL through ``apply_workspace_acls``, and the repair's
    before and after snapshots then both read it, so the ACL step is a true
    no-op. The workspace's path is free for create and copy, and rehome's
    target is absent until its copy has run.
    """

    def __init__(self, *, path: str = "/srv/ws1", group: str = "ws-ws1", rehome_target: str | None = None) -> None:
        super().__init__(path=path, group=group)
        self.acl_canonical = False
        self._rehome_target = rehome_target
        self._copied = False

    def _getfacl_snapshot(self) -> str:
        return acl_block(self._path.lstrip("/"), HEALTHY_ACL if self.acl_canonical else DAMAGED_ACL)

    def respond(self, request: object) -> Result | None:
        command = argv(request)
        if is_occupancy_probe(request):
            return Result(Completed(1))
        if command[:1] == ("rsync",):
            self._copied = True
        elif self._rehome_target is not None and command == ("test", "-d", self._rehome_target):
            return Result(Completed(0 if self._copied else 1))
        elif request in workspace_acl_commands(self._path):
            self.acl_canonical = True
        return super().respond(request)


class RehomeGuest(Guest):
    """A guest for a rehome to ``/dst/ws1``: the target is free until the
    copy has run, then a directory, so rehome's pre-copy occupancy check and
    its post-copy verify both pass."""

    def __init__(self, *, fails: Sequence[Failing] = ()) -> None:
        super().__init__(fails=fails)
        self._copied = False

    def respond(self, request: object) -> Result | None:
        if starts("rsync")(request):
            self._copied = True
        if request == path_occupied("/dst/ws1"):
            return Result(Completed(0 if self._copied else 1))
        if argv(request) == ("test", "-d", "/dst/ws1"):
            return Result(Completed(0 if self._copied else 1))
        return super().respond(request)


class FailingGuest(Guest):
    """A guest whose chosen commands exit 1 with a stderr, and whose occupancy probe finds the path free.

    ``failures`` maps a command's argv prefix to the stderr its exit carries.
    """

    def __init__(self, failures: dict[tuple[str, ...], str] | None = None) -> None:
        super().__init__()
        self.failures = failures or {}

    def respond(self, request: object) -> Result | None:
        stderr = next((text for prefix, text in self.failures.items() if starts(*prefix)(request)), None)
        if stderr is not None:
            return Result(Completed(1), b"", stderr.encode())
        if is_occupancy_probe(request):
            return Result(Completed(1))
        return super().respond(request)

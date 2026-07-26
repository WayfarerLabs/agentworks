"""``workspace repair`` / ``workspace rehome`` / ``workspace delete`` /
``workspace copy`` through the orchestrated model: the shared derived
graph (the live VM alone; deliberately NO live workspace node, the
workspace has no capability instances and nothing realization-shaped),
the gate-prompt parity carries (all four DO open the activation gate,
at WORKSPACE scope), the pre-gate validation pins (refusals cost zero
prompts, zero resolves, zero gate events), rehome's inherently
post-gate directory checks and confirm (they need SSH), delete's two
paths (its own ``gated_vm_boundary`` composition when standalone; the
caller's bound platform held verbatim on the nested-teardown path; no
boundary at all without a VM row), and copy's sequential two-boundary
composition (one per VM, nested holds; exactly one on the same-VM
path).

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, the admin
SSH transport, and (for copy's pack step) ``subprocess.run`` are the
fakes.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.db import InitStatus, VMStatus
from agentworks.errors import (
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.vms import manager as vm_manager
from agentworks.workspaces import manager as workspace_manager

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database
    from tests.conftest import CapturedOutput


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The shared ``make_config`` shape, plus a tmp ``[paths]`` section
    so delete's ``.code-workspace`` unlink and copy's VS Code stub
    never touch the operator's real directories."""
    from tests.orchestrated_fixtures import PROXMOX_SECTION, write_operator_config

    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    paths_section = f'[paths]\nvscode_workspaces = "{tmp_path / "vscode"}"\n'

    def _make(extra: str = ""):  # noqa: ANN202
        return write_operator_config(tmp_path, PROXMOX_SECTION + paths_section + extra)

    return _make


def _seed(db: Database, *, ws: str = "ws1") -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    # rehome and copy guard on init status pre-gate; the seeded row
    # must be COMPLETE for them (repair and delete never guarded).
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    _seed_workspace(db, vm_name="box", name=ws)


def _seed_workspace(db: Database, *, vm_name: str, name: str) -> None:
    db.insert_workspace(
        name,
        vm_name=vm_name,
        workspace_path=f"/srv/{name}",
        template="default",
        linux_group=f"ws-{name}",
    )


def _seed_live_session(db: Database, *, name: str, ws: str) -> None:
    """A session row that reads as alive (pid + boot_id + socket), so
    delete's status-aware kill loop probes and kills it."""
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, "
        "socket_path, pid, boot_id) VALUES (?, ?, 'default', 'admin', "
        "?, 4242, 'boot-1')",
        (name, ws, f"/tmp/{name}.sock"),
    )
    db._conn.commit()


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _stop_the_vm(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale"))


def _no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_status(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for a command that must fail pre-gate")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)
    _reachable(monkeypatch, False)


def _node_holding(db: Database, config: object, platform: object, *, vm_name: str = "box"):  # noqa: ANN202
    """A live VM node for ``vm_name`` (default 'box') whose site holds
    the given platform: the shape a nested teardown hands
    ``delete_workspace`` (it re-enters the hold through
    ``vm_node.site.platform``)."""
    from agentworks.vms.nodes import LiveVMNode, VMSiteNode

    row = db.get_vm(vm_name)
    assert row is not None
    site = VMSiteNode("proxmox", platform, (), object())  # type: ignore[arg-type]
    return LiveVMNode(db, config, object(), row, site)  # type: ignore[arg-type]


class _FakeAdminTarget:
    """Admin transport double: every command is recorded (optionally
    into a shared event log) and answers ok unless a substring matches
    the per-test response map."""

    def __init__(
        self,
        *,
        events: list[str] | None = None,
        failing: tuple[str, ...] = (),
    ) -> None:
        self.commands: list[str] = []
        self.written: list[tuple[str, str]] = []
        self._events = events
        self._failing = failing

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        if self._events is not None:
            self._events.append(f"run:{cmd}")
        ok = not any(needle in cmd for needle in self._failing)
        return SimpleNamespace(ok=ok, returncode=0 if ok else 1, stdout="", stderr="")

    def write_file(self, remote_path: str, content: str, **kwargs: object) -> None:
        self.written.append((remote_path, content))

    def copy_to(self, local_path: object, remote_path: str, **kwargs: object) -> None:
        self.commands.append(f"copy_to:{remote_path}")


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _FakeAdminTarget:
    """One recording admin target behind the canonical transport
    factory (the lifecycle bodies import ``transport``
    function-locally) AND the workspace VM backend's eager module
    import (pre-imported before patching so the module can never
    first-import mid-patch and capture the fake as its original)."""
    import agentworks.workspaces.backends.vm  # noqa: F401

    fake = _FakeAdminTarget()
    factory = lambda vm, config, **kwargs: fake  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", factory)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.transport", factory)
    return fake


# -- the derived graph (stated once for the lifecycle ops) --------------------


def test_graph_is_the_live_vm_alone_no_workspace_node(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """repair / rehome / delete / copy share one graph per VM: the live
    VM from its row (vm-site + vm), union = the site's config secret
    only. Deliberately NO live workspace node: the workspace here has
    no capability instances, no secret refs, no readiness, and nothing
    realization-shaped (delete unwinds nothing, repair converges,
    rehome / copy mutate through the VM transport), so introducing one
    would be over-orchestration."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry)

    nodes = walk(live_vm_node(db, config, registry, vm))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)

    for name in secret_union(nodes):
        resolver.register_name(name)
    resolver.resolve()
    assert set(resolver.values) == {"proxmox-token"}


# -- gate-prompt parity (the per-command carries) -----------------------------


def test_repair_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert resolve_counter == [["proxmox-token"]]
    assert any("chmod -c 2770 /srv/ws1" in c for c in target.commands)
    assert "Repairing workspace 'ws1' on VM 'box'..." in captured_output.info


def test_repair_stopped_vm_gate_burst_seeds_the_whole_union(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """No env targets, so the gate's just-in-time resolve covers the
    entire union: one burst, nothing twice, nothing after."""
    config = make_config()
    _seed(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"]]
    assert any("chmod -c 2770 /srv/ws1" in c for c in target.commands)


def test_repair_converges_git_identity_on_the_checkout(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A git identity declared on the template is stamped into the
    checkout's repo-local config on repair (the fake answers the rev-parse
    repo probe ok, and the config --get probe empty, so both fields apply)."""
    config = make_config(
        '[workspace_templates.default]\ngit_user_name = "Ada Lovelace"\ngit_user_email = "ada@example.com"\n'
    )
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert any("git -C /srv/ws1 config --local user.name 'Ada Lovelace'" in c for c in target.commands)
    assert any("git -C /srv/ws1 config --local user.email ada@example.com" in c for c in target.commands)


class _RevParseFailingTarget(_FakeAdminTarget):
    """Admin target whose `git rev-parse` fails with a chosen stderr, so
    the repair identity probe can exercise its no-repo vs error branches."""

    def __init__(self, *, rev_parse_stderr: str) -> None:
        super().__init__()
        self._rev_parse_stderr = rev_parse_stderr

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        if "rev-parse" in cmd:
            return SimpleNamespace(ok=False, returncode=128, stdout="", stderr=self._rev_parse_stderr)
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")


def _wire_target(monkeypatch: pytest.MonkeyPatch, fake: _FakeAdminTarget) -> None:
    import agentworks.workspaces.backends.vm  # noqa: F401

    factory = lambda vm, config, **kwargs: fake  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", factory)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.transport", factory)


def test_repair_skips_git_identity_when_not_a_repo(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Identity declared on a workspace that is not a git checkout is a
    quiet no-op: the rev-parse probe reports 'not a git repository', so no
    git config runs and no warning fires."""
    fake = _RevParseFailingTarget(
        rev_parse_stderr="fatal: not a git repository (or any of the parent directories): .git"
    )
    _wire_target(monkeypatch, fake)

    config = make_config('[workspace_templates.default]\ngit_user_name = "Ada Lovelace"\n')
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert not any("config" in c for c in fake.commands)
    assert not any("git identity" in w for w in captured_output.warnings)


def test_repair_git_identity_warns_on_unexpected_probe_failure(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A probe failure that is NOT 'not a git repository' (git missing, a
    broken checkout) warns rather than reporting a misleading OK, and still
    skips the config write."""
    fake = _RevParseFailingTarget(rev_parse_stderr="git: command not found")
    _wire_target(monkeypatch, fake)

    config = make_config('[workspace_templates.default]\ngit_user_name = "Ada Lovelace"\n')
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert not any("config" in c for c in fake.commands)
    assert any("git identity skipped" in w for w in captured_output.warnings)


def test_repair_default_template_stamps_no_identity(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """No identity declared (the bare default template): repair emits no
    git commands at all, not even the repo probe."""
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert not any(c.startswith("git ") for c in target.commands)


_HEALTHY_ACL = "user::rwx\ngroup::rwx\nmask::rwx\nother::---"
_DAMAGED_ACL = "user::rwx\ngroup::r-x\nother::---"  # narrowed group, mask stripped


def _acl_block(path: str, entries: str) -> str:
    """One ``getfacl -R -n`` record: the ``# file:`` header, the owner/group
    comment lines the parser must ignore, then the ACL entries."""
    return f"# file: {path}\n# owner: 1000\n# group: 1000\n{entries}\n"


class _RepairProbeTarget(_FakeAdminTarget):
    """Admin target that simulates specific live-state divergences so the
    apply-and-observe repair steps exercise their Fixed-vs-OK detection.

    ``damaged`` names the categories whose canonical command should signal a
    real change: 'owner'/'mode'/'sgid' (chown/chmod -c print a change line),
    'acl' (the before/after getfacl snapshots differ on a persisting path),
    'traversal' (a parent chmod -c prints a change line). Every other command
    answers ok with the silent/unchanged output a healthy workspace would
    produce, so the admin membership probe sees itself already in the group
    and the getfacl snapshots match when 'acl' is absent.

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
        group: str = "ws-ws1",
    ) -> None:
        super().__init__()
        self._damaged = damaged
        self._acl_churn = acl_churn
        self._getfacl_ok = getfacl_ok
        self._group = group
        self._getfacl_calls = 0

    def _getfacl_snapshot(self) -> str:
        self._getfacl_calls += 1
        first = self._getfacl_calls == 1
        if "acl" in self._damaged and first:
            # Before: the persisting path's ACL is narrowed / mask-stripped.
            return _acl_block("srv/ws1", _DAMAGED_ACL)
        if self._acl_churn:
            # A persisting path with identical ACL in both snapshots, plus a
            # path that exists in only one (churn the compare must ignore).
            churn_path = "srv/ws1/tmp-before" if first else "srv/ws1/tmp-after"
            return _acl_block("srv/ws1", _HEALTHY_ACL) + "\n" + _acl_block(churn_path, _HEALTHY_ACL)
        return _acl_block("srv/ws1", _HEALTHY_ACL)

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        stdout = ""
        if "getfacl" in cmd and not self._getfacl_ok:
            # Simulate a transient getfacl failure: the snapshot is missing,
            # so the ACL step has no change data to compare.
            return SimpleNamespace(ok=False, returncode=1, stdout="", stderr="getfacl: error")
        if "chown -R -c" in cmd and "owner" in self._damaged:
            stdout = "changed ownership of '/srv/ws1/f'\n"
        elif "chmod -c 2770" in cmd and "mode" in self._damaged:
            stdout = "mode of '/srv/ws1' changed from 0700 to 2770\n"
        elif "chmod -c g+s" in cmd and "sgid" in self._damaged:
            stdout = "mode of '/srv/ws1/d' changed from 0770 to 2770\n"
        elif "getfacl" in cmd:
            stdout = self._getfacl_snapshot()
        elif "chmod -c a+x" in cmd and "traversal" in self._damaged:
            stdout = "mode of '/srv' changed from 0700 to 0711\n"
        elif "id -nG" in cmd:
            # The admin membership probe: report admin already in the group
            # so that detection-based step is a no-op in these scenarios.
            stdout = f"admin {self._group}"
        return SimpleNamespace(ok=True, returncode=0, stdout=stdout, stderr="")


def test_repair_healthy_workspace_reports_ok_for_every_step(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A workspace whose live state already matches: every apply-and-observe
    step reports OK (no false Fixed), the closing line is the truthful
    'No issues found', and convergence still runs (the canonical commands
    execute even when they are no-ops)."""
    fake = _RepairProbeTarget()
    _wire_target(monkeypatch, fake)
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert "OK: directory ownership and permissions" in captured_output.detail
    assert "OK: ACLs" in captured_output.detail
    assert "OK: parent traversal" in captured_output.detail
    assert not any(line.startswith("Fixed:") for line in captured_output.detail)
    assert "\nNo issues found" in captured_output.info
    # Convergence is unconditional: the canonical commands still ran.
    assert any("chmod -c 2770 /srv/ws1" in c for c in fake.commands)
    assert any("setfacl -R -m g::rwx" in c for c in fake.commands)


def test_repair_fully_damaged_workspace_reports_fixed_per_step(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Wrong owner/mode, missing SGID, a stripped ACL, and a missing parent
    traversal bit: each apply-and-observe step reports Fixed, the count is
    the truthful 'Repaired 3 issue(s)' (ownership/permissions/SGID collapse
    to one category), and convergence still runs."""
    fake = _RepairProbeTarget(damaged=frozenset({"owner", "mode", "sgid", "acl", "traversal"}))
    _wire_target(monkeypatch, fake)
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert "Fixed: directory ownership and permissions" in captured_output.detail
    assert "Fixed: ACLs" in captured_output.detail
    assert "Fixed: parent traversal" in captured_output.detail
    assert "\nRepaired 3 issue(s)" in captured_output.info
    assert "\nNo issues found" not in captured_output.info
    assert any("chmod -c 2770 /srv/ws1" in c for c in fake.commands)


def test_repair_partial_damage_reports_only_the_diverged_step(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Only the ACL diverges: ACLs report Fixed while ownership/permissions
    and parent traversal report OK, and the count is the truthful 'Repaired
    1 issue(s)' (no over-counting of the still-correct steps)."""
    fake = _RepairProbeTarget(damaged=frozenset({"acl"}))
    _wire_target(monkeypatch, fake)
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert "Fixed: ACLs" in captured_output.detail
    assert "OK: directory ownership and permissions" in captured_output.detail
    assert "OK: parent traversal" in captured_output.detail
    assert "\nRepaired 1 issue(s)" in captured_output.info


def test_repair_sgid_only_damage_reports_permissions_fixed(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """SGID diverged while owner and mode are already correct: only the SGID
    command signals a change, so the permissions category still reports Fixed
    and counts once. This pins the OR-collapse of owner/mode/sgid: under an
    AND collapse the two silent (healthy) commands would force OK and this
    would fail."""
    fake = _RepairProbeTarget(damaged=frozenset({"sgid"}))
    _wire_target(monkeypatch, fake)
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert "Fixed: directory ownership and permissions" in captured_output.detail
    assert "OK: ACLs" in captured_output.detail
    assert "OK: parent traversal" in captured_output.detail
    assert "\nRepaired 1 issue(s)" in captured_output.info


def test_repair_mode_only_damage_reports_permissions_fixed(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Mode diverged while owner and SGID are already correct: a second
    isolated case pinning the OR-collapse from a different member (again
    fails under an AND collapse, where owner's silence would force OK)."""
    fake = _RepairProbeTarget(damaged=frozenset({"mode"}))
    _wire_target(monkeypatch, fake)
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert "Fixed: directory ownership and permissions" in captured_output.detail
    assert "\nRepaired 1 issue(s)" in captured_output.info


def test_repair_owner_only_damage_reports_permissions_fixed(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Owner diverged while mode and SGID are already correct: the third
    isolated case, pinning the last member of the owner/mode/sgid `or`
    collapse (fails under an AND collapse, where mode/sgid silence forces
    OK). Together the three isolated tests pin every sub-signal."""
    fake = _RepairProbeTarget(damaged=frozenset({"owner"}))
    _wire_target(monkeypatch, fake)
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert "Fixed: directory ownership and permissions" in captured_output.detail
    assert "\nRepaired 1 issue(s)" in captured_output.info


def test_repair_acl_churn_does_not_report_false_fixed(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A healthy-but-busy workspace: a file is created/renamed between the two
    getfacl snapshots (so the snapshots' path sets differ) but every persisting
    path's ACL is unchanged. The intersect-compare must report OK, not a
    spurious Fixed, keeping 'No issues found' truthful. A whole-output
    byte-compare would fail here."""
    fake = _RepairProbeTarget(acl_churn=True)
    _wire_target(monkeypatch, fake)
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert "OK: ACLs" in captured_output.detail
    assert not any(line.startswith("Fixed:") for line in captured_output.detail)
    assert "\nNo issues found" in captured_output.info


def test_repair_acls_indeterminate_when_getfacl_fails(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Both getfacl snapshots fail (transient SSH error): the ACL step has no
    change data, so it must NOT claim OK (or count a fix), it must warn that
    the state is indeterminate. Convergence still ran (apply_workspace_acls),
    only the verification is missing. Fails before the fix, where empty-vs-empty
    snapshots compared equal and printed a confirmed OK: ACLs."""
    fake = _RepairProbeTarget(getfacl_ok=False)
    _wire_target(monkeypatch, fake)
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    assert "OK: ACLs" not in captured_output.detail
    assert not any(line.startswith("Fixed: ACLs") for line in captured_output.detail)
    assert any("indeterminate" in w and "could not run" in w for w in captured_output.warnings)
    # apply_workspace_acls still ran: convergence is unaffected by the probe.
    assert any("setfacl -R -m g::rwx" in c for c in fake.commands)


def test_repair_parent_traversal_quotes_a_spaced_path(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Regression for the traversal quoting fix: a workspace_path with a space
    and a shell metacharacter is passed as the positional `$1` (shlex-quoted),
    never interpolated raw into the sh -c script. Guards against a re-introduced
    raw interpolation, which would split the path on the space and execute the
    metacharacter."""
    import shlex

    from agentworks.db import InitStatus

    spaced_path = "/srv/my ws;danger"
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    db.insert_workspace(
        "ws1",
        vm_name="box",
        workspace_path=spaced_path,
        template="default",
        linux_group="ws-ws1",
    )
    fake = _RepairProbeTarget()
    _wire_target(monkeypatch, fake)
    config = make_config()
    _reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1")

    traversal = next(c for c in fake.commands if "chmod -c a+x" in c)
    # The path rides in as the shlex-quoted positional arg after `_`, and the
    # loop reads it from "$1", so the raw path never touches the script body.
    assert traversal.endswith(f"_ {shlex.quote(spaced_path)}")
    assert 'p="$1"' in traversal
    assert f"p={spaced_path}" not in traversal


# -- the ACL intersect-compare helper (churn-robust change detection) ---------


def test_acls_changed_identical_snapshots_is_no_change() -> None:
    from agentworks.workspaces.manager.repair import _acls_changed

    snap = _acl_block("srv/ws1", _HEALTHY_ACL)
    assert _acls_changed(snap, snap) is False


def test_acls_changed_ignores_added_and_removed_paths() -> None:
    """Churn only: the shared path is unchanged; a path appears in one
    snapshot and not the other. Not a repair."""
    from agentworks.workspaces.manager.repair import _acls_changed

    before = _acl_block("srv/ws1", _HEALTHY_ACL) + "\n" + _acl_block("srv/ws1/gone", _HEALTHY_ACL)
    after = _acl_block("srv/ws1", _HEALTHY_ACL) + "\n" + _acl_block("srv/ws1/new", _HEALTHY_ACL)
    assert _acls_changed(before, after) is False


def test_acls_changed_detects_change_on_a_persisting_path() -> None:
    from agentworks.workspaces.manager.repair import _acls_changed

    before = _acl_block("srv/ws1", _DAMAGED_ACL)
    after = _acl_block("srv/ws1", _HEALTHY_ACL)
    assert _acls_changed(before, after) is True


def test_acls_changed_detects_real_change_even_amid_churn() -> None:
    """The coordinator's explicit case: a persisting path is genuinely
    re-ACLed WHILE other paths churn. The real change is still detected."""
    from agentworks.workspaces.manager.repair import _acls_changed

    before = _acl_block("srv/ws1", _DAMAGED_ACL) + "\n" + _acl_block("srv/ws1/gone", _HEALTHY_ACL)
    after = _acl_block("srv/ws1", _HEALTHY_ACL) + "\n" + _acl_block("srv/ws1/new", _HEALTHY_ACL)
    assert _acls_changed(before, after) is True


def test_acls_changed_ownership_comment_lines_are_ignored() -> None:
    """The ``# owner:`` / ``# group:`` header lines are not ACL entries: a
    workspace whose owner changed (step 3) between snapshots but whose ACL
    entries match is not an ACL change."""
    from agentworks.workspaces.manager.repair import _acls_changed

    before = f"# file: srv/ws1\n# owner: 0\n# group: 0\n{_HEALTHY_ACL}\n"
    after = f"# file: srv/ws1\n# owner: 1000\n# group: 1000\n{_HEALTHY_ACL}\n"
    assert _acls_changed(before, after) is False


# -- create and repair share one canonical ACL (first repair is a no-op) ------


class _CreateThenRepairTarget(_FakeAdminTarget):
    """Stateful fake spanning a create then a repair on one workspace.

    The canonical ACL that create applies (via ``apply_workspace_acls``)
    persists as ``_acl_canonical``, so the subsequent repair's before/after
    getfacl snapshots both reflect it and the ACL step is a true no-op. Every
    other repair probe reports already-converged, so the first repair of a
    freshly created workspace finds nothing to fix.
    """

    def __init__(self) -> None:
        super().__init__()
        self._acl_canonical = False

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        stdout = ""
        ok = True
        if cmd.startswith("test -d"):
            ok = False  # create's existence precheck must see "absent" to proceed
        elif "setfacl -R -m g::rwx" in cmd:
            self._acl_canonical = True  # the recursive access apply converges the tree
        elif "getfacl" in cmd:
            entries = _HEALTHY_ACL if self._acl_canonical else _DAMAGED_ACL
            stdout = _acl_block("srv/ws1", entries)
        elif "id -nG" in cmd:
            stdout = "admin ws-ws1"  # admin already in the group
        return SimpleNamespace(ok=ok, returncode=0 if ok else 1, stdout=stdout, stderr="")


def test_first_repair_after_create_is_a_noop(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """create and repair now apply the IDENTICAL canonical ACL through the
    shared ``apply_workspace_acls`` helper, so a freshly created workspace is
    already in repair's canonical state: the first repair reports OK: ACLs and
    the truthful No issues found. Create applies the recursive spec (default
    ACL on dirs + recursive access), not the old top-dir-only form."""
    from agentworks.workspaces.backends.vm import create_vm_workspace
    from agentworks.workspaces.templates import ResolvedTemplate

    fake = _CreateThenRepairTarget()
    _wire_target(monkeypatch, fake)
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    vm = db.get_vm("box")
    assert vm is not None
    create_vm_workspace(vm, config, "ws1", ResolvedTemplate(name="default", repo=None))

    # Create applied the recursive canonical ACL (the same spec repair uses),
    # not a top-dir-only default. Assert on shape, path-agnostically.
    create_acls = [c for c in fake.commands if "setfacl" in c]
    assert any(c.startswith("find ") and "setfacl -d -m g::rwx -m m::rwx" in c for c in create_acls)
    assert any("setfacl -R -m g::rwx -m m::rwx" in c for c in create_acls)
    assert fake._acl_canonical

    workspace_manager.repair_workspace(db, config, "ws1")

    assert "OK: ACLs" in captured_output.detail
    assert not any(line.startswith("Fixed:") for line in captured_output.detail)
    assert "\nNo issues found" in captured_output.info


def test_delete_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True)

    assert resolve_counter == [["proxmox-token"]]
    assert any("rm -rf /srv/ws1" in c for c in target.commands)
    assert db.get_workspace("ws1") is None
    assert "Workspace 'ws1' deleted" in captured_output.info


def test_delete_stopped_vm_gate_burst_seeds_the_whole_union(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True)

    assert events == ["status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_workspace("ws1") is None


# -- the operation scope reaches readiness ------------------------------------


def test_workspace_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    workspace_manager.repair_workspace(db, config, "ws1")

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.WORKSPACE
    assert scope.vm == "box" and scope.workspace == "ws1"
    assert scope.agent is None and scope.session is None


# -- validation stays pre-gate ------------------------------------------------


def test_delete_sessions_guard_refuses_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _seed_live_session(db, name="s1", ws="ws1")
    _no_gate(monkeypatch)

    with pytest.raises(StateError, match="has 1 session"):
        workspace_manager.delete_workspace(db, config, "ws1")

    assert resolve_counter == []
    assert target.commands == []
    assert db.get_workspace("ws1") is not None


def test_delete_declined_confirm_aborts_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)
    captured_output.confirm_response = False

    with pytest.raises(UserAbort, match="delete cancelled"):
        workspace_manager.delete_workspace(db, config, "ws1")

    assert resolve_counter == []
    assert target.commands == []
    assert db.get_workspace("ws1") is not None


def test_rehome_overlapping_paths_fail_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)

    with pytest.raises(ValidationError, match="paths overlap"):
        workspace_manager.rehome_workspace(db, config, "ws1", target_path="/srv/ws1/nested")

    assert resolve_counter == []
    assert target.commands == []


def test_repair_unknown_workspace_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)

    with pytest.raises(NotFoundError, match="workspace 'ghost' not found"):
        workspace_manager.repair_workspace(db, config, "ghost")

    assert resolve_counter == []
    assert target.commands == []


# -- delete's two paths -------------------------------------------------------


def test_delete_nested_platform_path_reuses_the_callers_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The nested-teardown seam: the caller hands its already-held VM
    NODE, whose gate has converged and holds the VM, so delete performs
    ZERO additional resolves and composes no second boundary (a status
    probe would be one); it trusts that gate and re-enters only the
    keepalive hold, reaching the platform through the node's site edge."""

    class _BoundPlatformStub:
        name = "proxmox"

        def __init__(self) -> None:
            self.holds = 0

        def vm_active(self, row: object, *, config: object | None = None) -> contextlib.AbstractContextManager[None]:
            self.holds += 1
            return contextlib.nullcontext()

        def status(self, row: object, ctx: object) -> VMStatus:
            raise AssertionError("nested delete must not probe status")

    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)  # any boundary composition would probe status and fail
    _reachable(monkeypatch, True)
    bound = _BoundPlatformStub()
    vm_node = _node_holding(db, config, bound)

    workspace_manager.delete_workspace(
        db,
        config,
        "ws1",
        force=True,
        yes=True,
        vm_node=vm_node,
    )

    assert resolve_counter == []  # nothing resolved beyond the caller's pass
    assert bound.holds == 1  # the hold was re-entered, nothing else
    assert db.get_workspace("ws1") is None
    assert any("rm -rf /srv/ws1" in c for c in target.commands)


def test_delete_nested_rejects_a_mismatched_vm_node(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforce-invariants pin: a teardown must hand ``delete_workspace``
    the workspace's OWN vm node. A node for a different VM would hold
    that VM active while the delete body issues SSH + DB work against
    the workspace's real VM, a silent footgun; the guard raises a typed
    ``StateError`` before the hold is ever entered."""
    config = make_config()
    _seed(db)  # ws1 on 'box'
    # A live node for a DIFFERENT VM than ws1's ('box').
    db.insert_vm("other", site="proxmox", hostname="other")
    db.update_vm_tailscale("other", "100.64.0.10")
    _no_gate(monkeypatch)  # nothing may probe status or hold the VM
    vm_node = _node_holding(db, config, object(), vm_name="other")

    with pytest.raises(StateError, match="teardown-wiring bug"):
        workspace_manager.delete_workspace(
            db,
            config,
            "ws1",
            force=True,
            yes=True,
            vm_node=vm_node,
        )

    assert resolve_counter == []  # refused before any resolve
    assert db.get_workspace("ws1") is not None  # nothing was deleted


def test_delete_without_vm_row_is_db_only_with_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The imperative special case, preserved: a workspace whose VM row
    is gone (drift) deletes DB-side only, with no boundary, no gate,
    and no SSH."""
    config = make_config()
    _seed(db)
    # Fabricate the drift the special case defends against: drop the VM
    # row out from under the workspace (FKs off for the surgery only).
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'box'")
    db._conn.execute("PRAGMA foreign_keys = ON")
    db._conn.commit()
    _no_gate(monkeypatch)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True)

    assert resolve_counter == []
    assert target.commands == []
    assert db.get_workspace("ws1") is None


# -- rehome: the confirm is inherently post-gate ------------------------------


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
    gate events, then the SSH directory existence checks, then the
    confirm (which renders their results). A declined confirm raises
    UserAbort and leaves the DB path unchanged; by then the gate has
    already run, because the checks the prompt reports on need SSH
    (inherently post-gate)."""
    from agentworks import output as output_mod

    config = make_config()
    _seed(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)
    fake = _FakeAdminTarget(events=events, failing=("test -d /dst/ws1",))
    monkeypatch.setattr("agentworks.transports.transport", lambda vm, config_, **kwargs: fake)

    def _decline(message: str, default: bool = False) -> bool:
        events.append("confirm")
        return False

    monkeypatch.setattr(output_mod, "confirm", _decline)

    with pytest.raises(UserAbort, match="rehome cancelled"):
        workspace_manager.rehome_workspace(db, config, "ws1", target_path="/dst/ws1")

    assert events == [
        "status",
        "start",
        "tailscale",
        "run:test -d /srv/ws1",
        "run:test -d /dst/ws1",
        "confirm",
    ]
    assert resolve_counter == [["proxmox-token"]]
    ws = db.get_workspace("ws1")
    assert ws is not None and ws.workspace_path == "/srv/ws1"


# -- copy: the sequential two-boundary composition ----------------------------


def _wire_copy_fakes(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> _FakeAdminTarget:
    """The copy command's fakes: a transport double that IS an
    SSHTransport (the pack step asserts the concrete type to read the
    raw ssh argv off it), a recording ``subprocess.run`` for the tar
    pipe, and hold-span recording on the platform's ``vm_active``."""
    import subprocess as subprocess_mod

    from agentworks.transports import SSHTransport

    # The fake FIRST in the MRO so its recording run / write_file /
    # copy_to win; SSHTransport supplies the concrete type (and the
    # host / user / identity_file attributes the pack step reads).
    class _FakeSSHTarget(_FakeAdminTarget, SSHTransport):  # type: ignore[misc]  # the fake's recording run deliberately shadows the real signature
        def __init__(self) -> None:
            SSHTransport.__init__(self, "100.64.0.9", user="admin")
            _FakeAdminTarget.__init__(self, events=events)

    fake = _FakeSSHTarget()
    monkeypatch.setattr("agentworks.transports.transport", lambda vm, config, **kwargs: fake)

    def _fake_pack(args: object, **kwargs: object) -> SimpleNamespace:
        events.append("pack")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(subprocess_mod, "run", _fake_pack)

    real_vm_active = ProxmoxPlatform.vm_active

    @contextlib.contextmanager
    def _recording_hold(self: ProxmoxPlatform, row, *, config=None):  # noqa: ANN001, ANN202
        events.append(f"hold-enter:{row.name}")
        with real_vm_active(self, row, config=config):
            yield
        events.append(f"hold-exit:{row.name}")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold)
    return fake


def test_copy_cross_vm_runs_two_sequential_boundaries_with_nested_holds(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Cross-VM copy: TWO boundary bursts in order (source first, then
    dest; one shared site config here, so both bursts name the site's
    secret), the dest boundary only entered after the pack (the dest
    VM is resolved mid-command, as at HEAD), and BOTH holds open
    concurrently (the dest span nests inside the source span)."""
    config = make_config()
    _seed(db)
    db.insert_vm("box2", site="proxmox", hostname="box2")
    db.update_vm_tailscale("box2", "100.64.0.10")
    db.update_vm_init_status("box2", InitStatus.COMPLETE)
    _reachable(monkeypatch, True)
    events: list[str] = []
    _wire_copy_fakes(monkeypatch, events)

    workspace_manager.copy_workspace(db, config, "ws1", dest_name="ws2", vm_name="box2")

    # Two sequential compositions, one boundary resolve each.
    assert resolve_counter == [["proxmox-token"], ["proxmox-token"]]
    # Source held before the pack; dest boundary only after it; the
    # dest hold exits before the source hold (nested spans, both open
    # across the unpack).
    assert events.index("hold-enter:box") < events.index("pack")
    assert events.index("pack") < events.index("hold-enter:box2")
    assert events.index("hold-exit:box2") < events.index("hold-exit:box")
    row = db.get_workspace("ws2")
    assert row is not None and row.vm_name == "box2" and row.template == "copied"
    assert any("tar xzf" in e for e in events if e.startswith("run:"))
    assert "Workspace 'ws1' copied to 'ws2'" in captured_output.info


def test_copy_same_vm_reuses_the_source_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Same-VM copy: exactly ONE boundary (no second resolve, no
    second hold); the source composition already gates and holds the
    one VM."""
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)
    events: list[str] = []
    _wire_copy_fakes(monkeypatch, events)

    workspace_manager.copy_workspace(db, config, "ws1", dest_name="ws2", vm_name="box")

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
    """Copy's cheap row refusals stay pre-everything: an unknown
    source workspace and an already-existing destination both fail
    before any prompt, resolve, or gate event."""
    from agentworks.errors import AlreadyExistsError

    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)

    with pytest.raises(NotFoundError, match="workspace 'nope' not found"):
        workspace_manager.copy_workspace(db, config, "nope", dest_name="ws2", vm_name="box")

    _seed_workspace(db, vm_name="box", name="ws2")
    with pytest.raises(AlreadyExistsError, match="workspace 'ws2' already exists"):
        workspace_manager.copy_workspace(db, config, "ws1", dest_name="ws2", vm_name="box")

    assert resolve_counter == []

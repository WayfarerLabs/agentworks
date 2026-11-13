"""``workspace repair`` through the orchestrated model.

The derived graph the lifecycle commands share (the live VM alone, no
workspace node), repair's gate-prompt parity, its pre-gate refusal, the
workspace scope reaching readiness, and its apply-and-observe steps over a
guest that reports what each converging request changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import NotFoundError
from agentworks.execution.results import Completed, Result
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces import manager as workspace_manager
from agentworks.workspaces.acls import workspace_acl_commands
from agentworks.workspaces.guest import open_parent_traversal
from tests.conftest import ManifestDoc, seed_vm_trust
from tests.workspaces._lifecycle_support import (
    DAMAGED_ACL,
    HEALTHY_ACL,
    ConvergingGuest,
    Guest,
    RepairProbeGuest,
    acl_block,
    argv,
    no_gate,
    reachable,
    route,
    seed,
    stop_the_vm,
)
from tests.workspaces._lifecycle_support import guest as guest
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database
    from tests.conftest import CapturedOutput

CANONICAL_MODE = ("chmod", "-c", "2770", "/srv/ws1")


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
    rehome / copy mutate through the admin's access), so introducing one
    would be over-orchestration."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    seed(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry, interaction=TtyInteractionPolicy.REFUSE)

    nodes = walk(live_vm_node(db, config, registry, vm))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)

    for name in secret_union(nodes):
        resolver.register_name(name)
    resolver.resolve()
    assert set(resolver.values) == {"proxmox-token"}


# -- gate-prompt parity -------------------------------------------------------


def test_repair_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    seed(db)
    reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert CANONICAL_MODE in guest.argvs()
    assert "Repairing workspace 'ws1' on VM 'box'..." in captured_output.info


def test_repair_stopped_vm_gate_burst_seeds_the_whole_union(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """No env targets, so the gate's just-in-time resolve covers the
    entire union: one burst, nothing twice, nothing after."""
    config = make_config()
    seed(db)
    events: list[str] = []
    stop_the_vm(monkeypatch, events)

    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["status", "status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"]]
    assert CANONICAL_MODE in guest.argvs()


def test_repair_unknown_workspace_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    seed(db)
    no_gate(monkeypatch)

    with pytest.raises(NotFoundError, match="workspace 'ghost' not found"):
        workspace_manager.repair_workspace(db, config, "ghost", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert guest.requests == []


def test_workspace_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    seed(db)
    reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.WORKSPACE
    assert scope.vm == "box" and scope.workspace == "ws1"
    assert scope.agent is None and scope.session is None


# -- git identity -------------------------------------------------------------


def _git(*args: str) -> tuple[str, ...]:
    return ("git", "-C", "/srv/ws1", *args)


def _git_configs(guest: Guest) -> list[tuple[str, ...]]:
    return [command for command in guest.argvs() if command[:4] == _git("config")]


class _RevParseFailingGuest(Guest):
    """A guest whose ``git rev-parse`` fails with a chosen stderr, so the
    repair identity probe can exercise its no-repo vs error branches."""

    def __init__(self, *, rev_parse_stderr: str) -> None:
        super().__init__()
        self._rev_parse_stderr = rev_parse_stderr

    def respond(self, request: object) -> Result | None:
        if argv(request) == _git("rev-parse", "--git-dir"):
            return Result(Completed(128), b"", self._rev_parse_stderr.encode())
        return super().respond(request)


def test_repair_converges_git_identity_on_the_checkout(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A git identity declared on the template is stamped into the
    checkout's repo-local config on repair (the guest answers the rev-parse
    repo probe ok, and the config --get probe empty, so both fields apply),
    each value one argument."""
    config = make_config(
        manifests=[
            ManifestDoc(
                "workspace-template",
                "default",
                {"git_user_name": "Ada Lovelace", "git_user_email": "ada@example.com"},
            )
        ]
    )
    seed(db)
    reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert _git("config", "--local", "user.name", "Ada Lovelace") in guest.argvs()
    assert _git("config", "--local", "user.email", "ada@example.com") in guest.argvs()


def test_repair_skips_git_identity_when_not_a_repo(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Identity declared on a workspace that is not a git checkout is a
    quiet no-op: the rev-parse probe reports 'not a git repository', so no
    git config runs and no warning fires."""
    fake = _RevParseFailingGuest(
        rev_parse_stderr="fatal: not a git repository (or any of the parent directories): .git"
    )
    route(monkeypatch, fake)

    config = make_config(manifests=[ManifestDoc("workspace-template", "default", {"git_user_name": "Ada Lovelace"})])
    seed(db)
    reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert _git_configs(fake) == []
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
    fake = _RevParseFailingGuest(rev_parse_stderr="git: command not found")
    route(monkeypatch, fake)

    config = make_config(manifests=[ManifestDoc("workspace-template", "default", {"git_user_name": "Ada Lovelace"})])
    seed(db)
    reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert _git_configs(fake) == []
    assert any("git identity skipped" in w for w in captured_output.warnings)


def test_repair_default_template_stamps_no_identity(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """No identity declared (the bare default template): repair emits no
    git commands at all, not even the repo probe."""
    config = make_config()
    seed(db)
    reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert not any(command[:1] == ("git",) for command in guest.argvs())


# -- apply-and-observe steps ----------------------------------------------------


def _repair(db: Database, config: object, guest: Guest, monkeypatch: pytest.MonkeyPatch) -> None:
    route(monkeypatch, guest)
    seed(db)
    reachable(monkeypatch, True)
    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


def test_repair_healthy_workspace_reports_ok_for_every_step(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A workspace whose live state already matches: every apply-and-observe
    step reports OK (no false Fixed), the closing line is the truthful
    'No issues found', and convergence still runs (the canonical requests
    are sent even when they are no-ops)."""
    fake = RepairProbeGuest()

    _repair(db, make_config(), fake, monkeypatch)

    assert "OK: directory ownership and permissions" in captured_output.detail
    assert "OK: ACLs" in captured_output.detail
    assert "OK: parent traversal" in captured_output.detail
    assert not any(line.startswith("Fixed:") for line in captured_output.detail)
    assert "\nNo issues found" in captured_output.info
    assert CANONICAL_MODE in fake.argvs()
    assert all(command in fake.requests for command in workspace_acl_commands("/srv/ws1"))


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
    fake = RepairProbeGuest(damaged=frozenset({"owner", "mode", "sgid", "acl", "traversal"}))

    _repair(db, make_config(), fake, monkeypatch)

    assert "Fixed: directory ownership and permissions" in captured_output.detail
    assert "Fixed: ACLs" in captured_output.detail
    assert "Fixed: parent traversal" in captured_output.detail
    assert "\nRepaired 3 issue(s)" in captured_output.info
    assert "\nNo issues found" not in captured_output.info
    assert CANONICAL_MODE in fake.argvs()


@pytest.mark.parametrize("damaged", ["acl", "sgid", "mode", "owner"])
def test_repair_one_diverged_step_reports_only_that_step_fixed(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    damaged: str,
) -> None:
    """One category diverges: only its step reports Fixed, and the count is
    the truthful 'Repaired 1 issue(s)'. For SGID, mode and owner alone this
    pins the OR-collapse of the three into one category: under an AND
    collapse the two silent (healthy) requests would force OK."""
    _repair(db, make_config(), RepairProbeGuest(damaged=frozenset({damaged})), monkeypatch)

    fixed = "Fixed: ACLs" if damaged == "acl" else "Fixed: directory ownership and permissions"
    assert fixed in captured_output.detail
    assert "OK: parent traversal" in captured_output.detail
    assert [line for line in captured_output.detail if line.startswith("Fixed:")] == [fixed]
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
    _repair(db, make_config(), RepairProbeGuest(acl_churn=True), monkeypatch)

    assert "OK: ACLs" in captured_output.detail
    assert not any(line.startswith("Fixed:") for line in captured_output.detail)
    assert "\nNo issues found" in captured_output.info


def test_repair_acls_indeterminate_when_getfacl_fails(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Both getfacl snapshots fail: the ACL step has no change data, so it
    must NOT claim OK (or count a fix), it must warn that the state is
    indeterminate. Convergence still ran (apply_workspace_acls), only the
    verification is missing."""
    fake = RepairProbeGuest(getfacl_ok=False)

    _repair(db, make_config(), fake, monkeypatch)

    assert "OK: ACLs" not in captured_output.detail
    assert not any(line.startswith("Fixed: ACLs") for line in captured_output.detail)
    assert any("indeterminate" in w and "could not run" in w for w in captured_output.warnings)
    # The closing line must agree with the warning: not a clean "No issues
    # found", but qualified with the unverified-step count.
    assert "\nNo issues found (1 step(s) could not be verified)" in captured_output.info
    assert all(command in fake.requests for command in workspace_acl_commands("/srv/ws1"))


def test_repair_opens_traversal_above_a_spaced_path(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A workspace path with a space and a shell metacharacter reaches the
    traversal walk as its argument (``test_guest.py`` proves the walk treats
    it as one path and starts at its parent)."""
    from agentworks.db import InitStatus

    spaced_path = "/srv/my ws;danger"
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    seed_vm_trust("box")
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    db.insert_workspace(
        "ws1",
        vm_name="box",
        workspace_path=spaced_path,
        template="default",
        linux_group="ws-ws1",
    )
    fake = RepairProbeGuest(path=spaced_path)
    route(monkeypatch, fake)
    reachable(monkeypatch, True)

    workspace_manager.repair_workspace(db, make_config(), "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert open_parent_traversal(spaced_path) in fake.requests


# -- the ACL intersect-compare helper (churn-robust change detection) ---------


def test_acls_changed_identical_snapshots_is_no_change() -> None:
    from agentworks.workspaces.manager.repair import _acls_changed

    snap = acl_block("srv/ws1", HEALTHY_ACL)
    assert _acls_changed(snap, snap) is False


def test_acls_changed_ignores_added_and_removed_paths() -> None:
    """Churn only: the shared path is unchanged; a path appears in one
    snapshot and not the other. Not a repair."""
    from agentworks.workspaces.manager.repair import _acls_changed

    before = acl_block("srv/ws1", HEALTHY_ACL) + "\n" + acl_block("srv/ws1/gone", HEALTHY_ACL)
    after = acl_block("srv/ws1", HEALTHY_ACL) + "\n" + acl_block("srv/ws1/new", HEALTHY_ACL)
    assert _acls_changed(before, after) is False


def test_acls_changed_detects_change_on_a_persisting_path() -> None:
    from agentworks.workspaces.manager.repair import _acls_changed

    before = acl_block("srv/ws1", DAMAGED_ACL)
    after = acl_block("srv/ws1", HEALTHY_ACL)
    assert _acls_changed(before, after) is True


def test_acls_changed_detects_real_change_even_amid_churn() -> None:
    """A persisting path is genuinely re-ACLed WHILE other paths churn. The
    real change is still detected."""
    from agentworks.workspaces.manager.repair import _acls_changed

    before = acl_block("srv/ws1", DAMAGED_ACL) + "\n" + acl_block("srv/ws1/gone", HEALTHY_ACL)
    after = acl_block("srv/ws1", HEALTHY_ACL) + "\n" + acl_block("srv/ws1/new", HEALTHY_ACL)
    assert _acls_changed(before, after) is True


def test_acls_changed_ownership_comment_lines_are_ignored() -> None:
    """The ``# owner:`` / ``# group:`` header lines are not ACL entries: a
    workspace whose owner changed (step 3) between snapshots but whose ACL
    entries match is not an ACL change."""
    from agentworks.workspaces.manager.repair import _acls_changed

    before = f"# file: srv/ws1\n# owner: 0\n# group: 0\n{HEALTHY_ACL}\n"
    after = f"# file: srv/ws1\n# owner: 1000\n# group: 1000\n{HEALTHY_ACL}\n"
    assert _acls_changed(before, after) is False


# -- create and repair share one canonical ACL (first repair is a no-op) ------


def test_first_repair_after_create_is_a_noop(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """create and repair apply the IDENTICAL canonical ACL through the
    shared ``apply_workspace_acls`` helper, so a freshly created workspace is
    already in repair's canonical state: the first repair reports OK: ACLs and
    the truthful No issues found."""
    from agentworks.workspaces.backends.vm import create_vm_workspace, default_workspace_path
    from agentworks.workspaces.templates import ResolvedTemplate

    config = make_config()
    path = default_workspace_path(config, "ws1")
    fake = ConvergingGuest(path=path)
    route(monkeypatch, fake)
    seed(db)
    db._conn.execute("UPDATE workspaces SET workspace_path = ? WHERE name = 'ws1'", (path,))
    db._conn.commit()
    reachable(monkeypatch, True)
    vm = db.get_vm("box")
    assert vm is not None

    create_vm_workspace(vm, config, "ws1", ResolvedTemplate(name="default", repo=None))

    assert fake.acl_canonical

    workspace_manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert "OK: ACLs" in captured_output.detail
    assert not any(line.startswith("Fixed:") for line in captured_output.detail)
    assert "\nNo issues found" in captured_output.info


# -- #254: repair hardens a workspace created under the old default:other -----


_PRE254_ACL = (
    "user::rwx\ngroup::rwx\nmask::rwx\nother::r-x\n"
    "default:user::rwx\ndefault:group::rwx\ndefault:mask::rwx\ndefault:other::r-x"
)
_HARDENED_ACL = (
    "user::rwx\ngroup::rwx\nmask::rwx\nother::---\n"
    "default:user::rwx\ndefault:group::rwx\ndefault:mask::rwx\ndefault:other::---"
)


class _OtherStillGrantedGuest(RepairProbeGuest):
    """A workspace created before #254: its ACL still grants ``other`` r-x on
    both the access entry AND the default entry, so new files inherited
    world-read/traverse. The canonical ACL passes deny ``other``, which flips
    the persisting path from the pre-#254 ACL to the hardened one between the
    before/after getfacl snapshots, so ``_acls_changed`` detects a real change
    and the ACL step reports ``Fixed: ACLs``. Every other probe reports
    already-converged."""

    def __init__(self) -> None:
        super().__init__()
        self._hardened = False

    def _getfacl_snapshot(self) -> str:
        return acl_block("srv/ws1", _HARDENED_ACL if self._hardened else _PRE254_ACL)

    def respond(self, request: object) -> Result | None:
        if request in workspace_acl_commands("/srv/ws1"):
            self._hardened = True
        return super().respond(request)


def test_repair_hardens_other_bits_on_a_pre254_workspace(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A workspace whose live ACL still carries ``default:other::r-x`` (created
    before #254): repair applies the canonical passes, the before/after
    snapshots differ ONLY on the other entries, so the ACL step legitimately
    reports ``Fixed: ACLs`` while every other step reports OK."""
    fake = _OtherStillGrantedGuest()

    _repair(db, make_config(), fake, monkeypatch)

    assert "Fixed: ACLs" in captured_output.detail
    assert "OK: directory ownership and permissions" in captured_output.detail
    assert "OK: parent traversal" in captured_output.detail
    assert "\nRepaired 1 issue(s)" in captured_output.info
    assert all(command in fake.requests for command in workspace_acl_commands("/srv/ws1"))

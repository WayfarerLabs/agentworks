"""``vm create`` / ``vm reinit`` through the orchestrated model: the
derived graph, the unwind parity oracle (``create_vm``'s rollback), and
the reinit gate.

Real config, registry, resolver, and backend loop; the platform's
backend ops, the initializer, and the transports are the fakes, same
surfaces the imperative oracle tests use.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform import ProvisionResult
from agentworks.config import load_config
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.output import Role
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database

PROXMOX_SECTION = """
[proxmox]
api_url = "https://pve:8006"
node = "pve1"
token_id = "agw@pam!agw"
template_vmid = 9000
"""

GIT_CRED_SECTION = """
[git_credentials.gh]
provider = "github"
"""


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public ssh key")
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-test")
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "ghtok")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _make(extra: str = ""):
        path = tmp_path / "config.toml"
        path.write_text(f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n' + extra)
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


@pytest.fixture(autouse=True)
def _no_tailscale_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)


# -- vm create: the derived graph --------------------------------------------


def test_create_graph_derives_from_declared_resources(make_config, db: Database) -> None:
    """The pending VM's graph: its edges are the resolved template, the
    chosen site, and the admin template's declared credentials, all
    real declared resources; the union is exactly the imperative
    boundary set (tailscale key, git token, site config secret), with
    the template's env-block secrets EXCLUDED (hermetic provisioning).
    """
    from agentworks.bootstrap import build_registry
    from agentworks.git_credentials.nodes import git_credential_node
    from agentworks.orchestration.node import CreatableNode
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.resources.access import admin_template
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import (
        pending_vm_node,
        vm_site_node,
        vm_template_node,
    )
    from agentworks.vms.templates import resolve_template

    config = make_config(
        PROXMOX_SECTION
        + GIT_CRED_SECTION
        + '[admin.config]\ngit_credentials = ["gh"]\n'
        + '[vm_templates.default.env]\nAPI_KEY = { secret = "api-key" }\n'
        + '[secrets.api-key]\ndescription = "runtime only"\n'
    )
    registry = build_registry(config)
    resolver = Resolver(config, registry)
    admin = admin_template(registry)
    assert admin.git_credentials == ["gh"]

    creds = tuple(git_credential_node(registry, name) for name in admin.git_credentials)
    template = vm_template_node(resolve_template(registry, None), resolver)
    site = vm_site_node(registry, "proxmox")
    pending = pending_vm_node(db, "nvm", template, site, creds)
    nodes = walk(pending)

    assert [n.key for n in nodes] == [
        "vm-template/default",
        "vm-site/proxmox",
        "git-credential/gh",
        "vm/nvm",
    ]
    assert isinstance(pending, CreatableNode)
    assert secret_union(nodes) == (
        "tailscale-auth-key",
        "proxmox-token",
        "git-token-gh",
    )
    # The runtime-only env secret stays out of the provisioning union.
    assert "api-key" not in secret_union(nodes)


# -- vm create: unwind parity ------------------------------------------------


def test_create_rollback_on_keyboard_interrupt_unwinds_the_row(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """The unwind oracle, interrupt flavor: a cancel during
    provisioning deletes the row (the realized set, reverse order,
    which for create is exactly the one VM node) and re-raises."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    def _interrupt(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(LimaPlatform, "create", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        vm_manager.create_vm(db, make_config(), name="ivm")
    assert db.get_vm("ivm") is None
    assert any("rolling back" in w for w in captured_output.warnings)


def test_create_rollback_on_user_abort_unwinds_the_row(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """The unwind oracle, abort flavor: an operator abort during
    provisioning deletes the row and re-raises as itself, never
    downgraded to a ProvisioningError."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.errors import UserAbort

    def _abort(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        raise UserAbort("operator said stop")

    monkeypatch.setattr(LimaPlatform, "create", _abort)
    with pytest.raises(UserAbort):
        vm_manager.create_vm(db, make_config(), name="avm")
    assert db.get_vm("avm") is None


def test_create_rollback_failure_warns_and_never_masks(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """Best-effort unwind: a teardown failure (DB trouble) warns,
    NAMING the artifact left standing per the teardown contract, and
    the original provisioning error still propagates."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import Database as _Db
    from agentworks.errors import ProvisioningError

    def _boom(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(LimaPlatform, "create", _boom)
    monkeypatch.setattr(_Db, "delete_vm", lambda self, name: (_ for _ in ()).throw(RuntimeError("db locked")))
    with pytest.raises(ProvisioningError, match="backend exploded"):
        vm_manager.create_vm(db, make_config(), name="wvm")
    (warning,) = [w for w in captured_output.warnings if "rollback" in w]
    assert warning.startswith("rollback: teardown of vm/wvm failed:")
    assert "the DB record for VM 'wvm'" in warning  # names what survived
    assert "db locked" in warning  # chains the cause


@contextlib.contextmanager
def _noop_hold(self: object, vm: object, *, config: object | None = None):
    """Stand-in for ``platform.vm_active``: create_vm enters this into its
    ExitStack across both init phases; the lima default is already a
    nullcontext, but pinning it keeps these tests off any real hold."""
    yield


def test_create_init_failure_keeps_the_row(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """The non-rollbackable window: once provisioning (platform create +
    Phase A) succeeded, a Phase B initialization failure keeps the VM
    (debuggable, reinit-able) and maps to an ExternalError with reinit
    guidance."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.errors import ExternalError

    def _fake_create(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        return ProvisionResult(
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={},
            bootstrap_complete=True,
            tailscale_ip="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)
    # Phase A succeeds (its steps are exercised elsewhere); Phase B explodes.
    monkeypatch.setattr(
        vm_manager,
        "bootstrap_vm",
        lambda *a, **k: (SimpleNamespace(), SimpleNamespace(), "/home/agentworks"),
    )

    def _init_boom(*a: object, **k: object) -> None:
        raise RuntimeError("init exploded")

    monkeypatch.setattr(vm_manager, "run_initialization", _init_boom)
    with pytest.raises(ExternalError, match="init exploded"):
        vm_manager.create_vm(db, make_config(), name="kvm")
    assert db.get_vm("kvm") is not None


def test_create_phase_a_failure_maps_to_provisioning_error(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """A Phase A (provisioning bootstrap/connectivity) failure marks the VM
    provisioning 'failed', maps to a ProvisioningError with delete guidance,
    keeps the row (past the platform-create unwind window), and never reaches
    Phase B."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import ProvisioningStatus
    from agentworks.errors import ProvisioningError

    def _fake_create(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        return ProvisionResult(
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={},
            bootstrap_complete=True,
            tailscale_ip="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)

    def _boom_bootstrap(db_: Database, config_: object, tmpl: object, vm_name: str, *a: object, **k: object) -> None:
        # Mirror the real bootstrap_vm's fatal path: mark provisioning
        # failed, then raise for create_vm's mapping to pick up.
        db_.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
        raise RuntimeError("bootstrap exploded")

    monkeypatch.setattr(vm_manager, "bootstrap_vm", _boom_bootstrap)

    def _no_phase_b(*a: object, **k: object) -> None:
        raise AssertionError("Phase B ran despite a Phase A failure")

    monkeypatch.setattr(vm_manager, "run_initialization", _no_phase_b)

    with pytest.raises(ProvisioningError, match="bootstrap exploded") as exc:
        vm_manager.create_vm(db, make_config(), name="fvm")
    assert "vm delete fvm" in (exc.value.hint or "")
    row = db.get_vm("fvm")
    assert row is not None  # kept: past the unwind window
    assert row.provisioning_status == ProvisioningStatus.FAILED.value


def test_create_phase_a_sync_failure_is_non_fatal(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_output,
) -> None:
    """A local SSH-config write failure at the end of Phase A is non-fatal:
    the bootstrapped VM is reachable, so it is NOT marked FAILED (it stays
    reinit-able), Phase B still runs, and create completes with a warning
    rather than raising. Only the bootstrap/verify is fatal to provisioning."""
    import agentworks.ssh_config as ssh_config_mod
    import agentworks.vms.initializer.driver as driver
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import ProvisioningStatus

    config = make_config(f'ssh_config = "{tmp_path / "ssh_config"}"\n')

    def _fake_create(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        return ProvisionResult(
            native_transport=SimpleNamespace(describe=lambda: "lima:vm", logger=None),  # type: ignore[arg-type]
            platform_metadata={},
            bootstrap_complete=True,
            tailscale_ip="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)

    class _FakeTS:
        def __init__(self, **kwargs: object) -> None:
            self.host = kwargs.get("host")
            self.logger = kwargs.get("logger")

        def run(self, cmd: str, timeout: int | None = None) -> object:
            return SimpleNamespace(ok=True, stdout="ok", returncode=0)

    monkeypatch.setattr(driver, "SSHTransport", _FakeTS)

    # The SSH-config sync fails (e.g. read-only home) at the end of Phase A
    # (and again on the post-init re-sync); both sites handle it non-fatally.
    def _boom_sync(*a: object, **k: object) -> None:
        raise RuntimeError("read-only home")

    monkeypatch.setattr(ssh_config_mod, "sync_ssh_config", _boom_sync)

    phase_b_ran: list[bool] = []
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *a, **k: phase_b_ran.append(True))

    # Does not raise: the sync failure is non-fatal.
    vm_manager.create_vm(db, config, name="svm")

    row = db.get_vm("svm")
    assert row is not None
    # Reachable VM stays COMPLETE (not FAILED), so `vm reinit` remains open.
    assert row.provisioning_status == ProvisioningStatus.COMPLETE.value
    assert phase_b_ran == [True]  # Phase B ran despite the sync failure
    assert any("SSH config sync failed" in w for w in captured_output.warnings)


def test_create_provisioning_section_ends_with_ssh_config_synced(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_output,
) -> None:
    """The Provisioning section now spans platform create + Phase A
    bootstrap/connectivity, and its last body line is the announced
    'SSH config synced' (emitted exactly once, from Phase A's real sync; the
    post-init re-sync is silent). Phase A runs for real here with the
    Tailscale transport faked; Phase B is a no-op."""
    import agentworks.vms.initializer.driver as driver
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    # Contain the real SSH-config write inside the test's tmp dir.
    config = make_config(f'ssh_config = "{tmp_path / "ssh_config"}"\n')

    def _fake_create(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        return ProvisionResult(
            native_transport=SimpleNamespace(describe=lambda: "lima:vm", logger=None),  # type: ignore[arg-type]
            platform_metadata={},
            bootstrap_complete=True,
            tailscale_ip="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)

    class _FakeTS:
        """Stand-in for the Tailscale SSHTransport: the verify and the
        reconnect wait both call ``run`` and it just succeeds."""

        def __init__(self, **kwargs: object) -> None:
            self.host = kwargs.get("host")
            self.logger = kwargs.get("logger")

        def run(self, cmd: str, timeout: int | None = None) -> object:
            return SimpleNamespace(ok=True, stdout="ok", returncode=0)

    monkeypatch.setattr(driver, "SSHTransport", _FakeTS)
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *a, **k: None)

    vm_manager.create_vm(db, config, name="pvm")

    # Provisioning is a real level-0 section.
    assert (Role.HEADER, 0, "Provisioning") in captured_output.lines
    # 'SSH config synced' is a level-1 body line, emitted exactly once.
    synced = [ln for ln in captured_output.lines if ln == (Role.BODY, 1, "SSH config synced")]
    assert len(synced) == 1
    # It is the LAST level-1 body line of the Provisioning section: after the
    # header, the section's body lines end with the sync (Phase B is faked and
    # the post-init re-sync is silent, so nothing else at level 1 follows).
    prov_idx = captured_output.lines.index((Role.HEADER, 0, "Provisioning"))
    body_l1 = [msg for (role, lvl, msg) in captured_output.lines[prov_idx + 1 :] if role is Role.BODY and lvl == 1]
    assert body_l1[-1] == "SSH config synced"
    # The connectivity step reads as a primary (info) step at the section body
    # level, not a de-emphasized detail.
    assert "Verifying Tailscale SSH..." in body_l1


# -- vm reinit: the orchestrated path ----------------------------------------


def _seed_provisioned_vm(db: Database) -> None:
    from agentworks.db import ProvisioningStatus

    db.insert_vm("rvm", site="lima-local", hostname="rvm")
    db.update_vm_tailscale("rvm", "100.64.0.9")
    db.update_vm_provisioning_status("rvm", ProvisioningStatus.COMPLETE)


def test_reinit_runs_initialization_through_the_gate(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """Reinit end to end on a reachable VM: one boundary pass covering
    the union (git token; the lima site has no config secrets), scoped
    tokens handed to the initializer, and the whole init held inside
    the activation span."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config(GIT_CRED_SECTION + '[admin.config]\ngit_credentials = ["gh"]\n')
    _seed_provisioned_vm(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    holds: list[str] = []

    import contextlib as _contextlib

    @_contextlib.contextmanager
    def _hold(self: LimaPlatform, vm: object, *, config: object | None = None):
        holds.append("open")
        try:
            yield
        finally:
            holds.append("close")

    monkeypatch.setattr(LimaPlatform, "vm_active", _hold)
    captured: dict[str, object] = {}

    def _fake_init(*args: object, **kwargs: object) -> None:
        captured["git_tokens"] = kwargs["git_tokens"]
        captured["providers"] = args[7]
        captured["held"] = list(holds)

    monkeypatch.setattr(vm_manager, "run_initialization", _fake_init)
    import agentworks.transports as transports

    monkeypatch.setattr(transports, "transport", lambda vm, config, **kw: SimpleNamespace())

    vm_manager.reinit_vm(db, config, "rvm")

    assert captured["git_tokens"] == {"gh": "ghtok"}
    assert list(captured["providers"]) == ["gh"]  # type: ignore[call-overload]
    assert captured["held"] == ["open"]  # init ran inside the span
    assert holds == ["open", "close"]  # span closed at the end
    assert any("reinitialized successfully" in m for m in captured_output.info)
    # Preflight is a real section (header at level 0), and the terminal
    # outcome routes through result() (RESULT role at level 0).
    assert (Role.HEADER, 0, "Preflight") in captured_output.lines
    assert (
        Role.RESULT,
        0,
        "VM 'rvm' reinitialized successfully!",
    ) in captured_output.lines


def test_reinit_resolves_the_stored_admin_template(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_output,
) -> None:
    """Reinit reads the VM's stored admin-template column, not always
    ``default``: a VM created on the ``work`` admin-template (whose only
    git credential is ``gh``) reinitializes with that credential. The
    default admin-template declares none, so seeing ``gh`` proves the
    column, not the default, drove resolution."""
    from textwrap import dedent

    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import ProvisioningStatus

    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "admin.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: admin-template
        metadata:
          name: work
        spec:
          git_credentials: ["gh"]
        """)
    )
    config = make_config(GIT_CRED_SECTION)
    db.insert_vm("rvm", site="lima-local", hostname="rvm", admin_template="work")
    db.update_vm_tailscale("rvm", "100.64.0.9")
    db.update_vm_provisioning_status("rvm", ProvisioningStatus.COMPLETE)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)

    import contextlib as _contextlib

    @_contextlib.contextmanager
    def _hold(self: LimaPlatform, vm: object, *, config: object | None = None):
        yield

    monkeypatch.setattr(LimaPlatform, "vm_active", _hold)
    captured: dict[str, object] = {}

    def _fake_init(*args: object, **kwargs: object) -> None:
        captured["git_tokens"] = kwargs["git_tokens"]
        captured["providers"] = args[7]

    monkeypatch.setattr(vm_manager, "run_initialization", _fake_init)
    import agentworks.transports as transports

    monkeypatch.setattr(transports, "transport", lambda vm, config, **kw: SimpleNamespace())

    vm_manager.reinit_vm(db, config, "rvm")

    # The work admin-template's git credential flowed through: reinit
    # resolved ``work``, not the credential-less ``default``.
    assert captured["git_tokens"] == {"gh": "ghtok"}
    assert list(captured["providers"]) == ["gh"]  # type: ignore[call-overload]


def test_reinit_errors_cleanly_when_the_stored_admin_template_is_gone(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """A VM whose stored admin-template was since removed from config
    reinitializes into a clean typed error naming the selector, not a raw
    ``KeyError`` traceback (parity with create's unknown-template error).
    The error fires before any initialization work."""
    from agentworks.db import ProvisioningStatus
    from agentworks.errors import NotFoundError

    # No admin manifest declares ``work``; the column points at a name the
    # registry no longer knows.
    config = make_config()
    db.insert_vm("rvm", site="lima-local", hostname="rvm", admin_template="work")
    db.update_vm_tailscale("rvm", "100.64.0.9")
    db.update_vm_provisioning_status("rvm", ProvisioningStatus.COMPLETE)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)

    called = False

    def _fake_init(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(vm_manager, "run_initialization", _fake_init)

    with pytest.raises(NotFoundError, match="work"):
        vm_manager.reinit_vm(db, config, "rvm")
    assert not called  # errored before initialization


def test_reinit_refuses_an_operator_stopped_vm_at_the_gate(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """The gate's refusal reaches reinit: a manually stopped VM refuses
    with the explicit-start hint before any init work."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config()
    _seed_provisioned_vm(db)
    db.set_operator_stopped("rvm", True)
    monkeypatch.setattr(LimaPlatform, "status", lambda self, vm, ctx: VMStatus.STOPPED)

    def _no_init(*a: object, **k: object) -> None:
        raise AssertionError("init ran despite the refusal")

    monkeypatch.setattr(vm_manager, "run_initialization", _no_init)
    with pytest.raises(StateError, match="manually stopped") as exc:
        vm_manager.reinit_vm(db, config, "rvm")
    assert "agw vm start rvm" in (exc.value.hint or "")

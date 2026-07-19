"""``vm create`` / ``vm reinit`` through the orchestrated model: the
derived graph, the unwind parity oracle (``create_vm``'s rollback), and
the reinit gate.

Real config, registry, resolver, and backend loop; the platform's
backend ops, the initializer, and the transports are the fakes, same
surfaces the imperative oracle tests use.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform import ProvisionResult
from agentworks.config import load_config
from agentworks.db import VMStatus
from agentworks.errors import StateError
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
        path.write_text(
            f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
            + extra
        )
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


@pytest.fixture(autouse=True)
def _no_tailscale_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)


# -- vm create: the derived graph --------------------------------------------


def test_create_graph_derives_from_declared_resources(
    make_config, db: Database
) -> None:
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
        + "[admin.config]\ngit_credentials = [\"gh\"]\n"
        + "[vm_templates.default.env]\nAPI_KEY = { secret = \"api-key\" }\n"
        + "[secrets.api-key]\ndescription = \"runtime only\"\n"
    )
    registry = build_registry(config)
    resolver = Resolver(config, registry)
    admin = admin_template(registry)
    assert admin.git_credentials == ["gh"]

    creds = tuple(
        git_credential_node(registry, name)
        for name in admin.git_credentials
    )
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
    monkeypatch.setattr(
        _Db, "delete_vm", lambda self, name: (_ for _ in ()).throw(RuntimeError("db locked"))
    )
    with pytest.raises(ProvisioningError, match="backend exploded"):
        vm_manager.create_vm(db, make_config(), name="wvm")
    (warning,) = [w for w in captured_output.warnings if "rollback" in w]
    assert warning.startswith("rollback: teardown of vm/wvm failed:")
    assert "the DB record for VM 'wvm'" in warning  # names what survived
    assert "db locked" in warning  # chains the cause


def test_create_init_failure_keeps_the_row(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """The non-rollbackable window: once provisioning succeeded, an
    initialization failure keeps the VM (debuggable, reinit-able),
    exactly as before."""
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

    def _init_boom(*a: object, **k: object) -> None:
        raise RuntimeError("init exploded")

    monkeypatch.setattr(vm_manager, "initialize_vm", _init_boom)
    with pytest.raises(ExternalError, match="init exploded"):
        vm_manager.create_vm(db, make_config(), name="kvm")
    assert db.get_vm("kvm") is not None


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

    config = make_config(GIT_CRED_SECTION + "[admin.config]\ngit_credentials = [\"gh\"]\n")
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

    monkeypatch.setattr(
        transports, "transport", lambda vm, config, **kw: SimpleNamespace()
    )

    vm_manager.reinit_vm(db, config, "rvm")

    assert captured["git_tokens"] == {"gh": "ghtok"}
    assert list(captured["providers"]) == ["gh"]  # type: ignore[call-overload]
    assert captured["held"] == ["open"]  # init ran inside the span
    assert holds == ["open", "close"]  # span closed at the end
    assert any("reinitialized successfully" in m for m in captured_output.info)


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
    db.insert_vm(
        "rvm", site="lima-local", hostname="rvm", admin_template="work"
    )
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

    monkeypatch.setattr(
        transports, "transport", lambda vm, config, **kw: SimpleNamespace()
    )

    vm_manager.reinit_vm(db, config, "rvm")

    # The work admin-template's git credential flowed through: reinit
    # resolved ``work``, not the credential-less ``default``.
    assert captured["git_tokens"] == {"gh": "ghtok"}
    assert list(captured["providers"]) == ["gh"]  # type: ignore[call-overload]


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
    monkeypatch.setattr(
        LimaPlatform, "status", lambda self, vm, ctx: VMStatus.STOPPED
    )

    def _no_init(*a: object, **k: object) -> None:
        raise AssertionError("init ran despite the refusal")

    monkeypatch.setattr(vm_manager, "run_initialization", _no_init)
    with pytest.raises(StateError, match="manually stopped") as exc:
        vm_manager.reinit_vm(db, config, "rvm")
    assert "agw vm start rvm" in (exc.value.hint or "")

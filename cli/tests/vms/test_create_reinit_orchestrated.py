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
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.git_credential.base import StoredCredential
from agentworks.capabilities.vm_platform import ProvisionResult
from agentworks.config import load_config
from agentworks.db import VersionedPayload, VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import ConfigError, ExternalError, StateError, UserAbort, ValidationError
from agentworks.execution.requests import Script
from agentworks.execution.results import Completed, NotDelivered, NotDeliveredReason, Result
from agentworks.output import Role
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from agentworks.vms.admin import AdminConfig
from agentworks.vms.contract import ACCOUNTS_VARIABLE
from agentworks.vms.templates import ResolvedVMTemplate
from tests.conftest import ManifestDoc, pin_wsl2_unsupported, seed_vm_trust, write_manifests
from tests.orchestrated_fixtures import proxmox_site
from tests.ssh_fixtures import write_test_ssh_keypair
from tests.vm_access_support import AccessRecorder, Admin

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentworks.db import Database
    from agentworks.git_credentials import CredentialRequest

# Proxmox ships in the opt-in ``proxmox`` system plugin since Phase 10 (R11),
# so a config that uses the proxmox site enables the plugin, exactly as a real
# proxmox operator would. The plugin opt-in is a settings section; the site
# itself is declared as a ``vm-site`` manifest (ADR 0022).
PLUGINS_ENABLED = """
[plugins]
system = ["proxmox"]
"""

GIT_CRED_GH = ManifestDoc("git-credential", "gh", {"provider": {"name": "github", "source": {"mode": "secret"}}})


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "id_ed25519"
    write_test_ssh_keypair(key)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-test")
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "ghtok")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    pin_wsl2_unsupported(monkeypatch)

    def _make(extra: str = "", *, manifests: Sequence[ManifestDoc | str] = ()):
        path = tmp_path / "config.toml"
        path.write_text(
            f'[operator]\nssh_public_key = "{key.as_posix()}.pub"\nssh_private_key = "{key.as_posix()}"\n' + extra
        )
        if manifests:
            write_manifests(tmp_path, *manifests)
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


@pytest.fixture(autouse=True)
def _no_tailscale_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)


def _completed_phase_a(
    db_: Database, config_: object, vm_name: str, *args: object, **kwargs: object
) -> tuple[object, str, DebianRelease]:
    """Stands in for a Phase A that passed: provisioning complete and the requested release attested."""
    from agentworks.db import ProvisioningStatus

    db_.update_vm_provisioning_status(vm_name, ProvisioningStatus.COMPLETE)
    release = cast("DebianRelease", kwargs["expected_release"])
    return SimpleNamespace(), "/home/agentworks", release


# -- vm create: the derived graph --------------------------------------------


def test_create_unknown_template_precedes_unrelated_unsupported_live_overlay(
    make_config,
    db: Database,
) -> None:
    from agentworks.errors import NotFoundError

    db.insert_vm("unrelated", site="lima-local", hostname="unrelated")
    db.instance_state.put_desired_overlay("vm", "unrelated", VersionedPayload(2, {"future": True}))

    with pytest.raises(NotFoundError) as caught:
        vm_manager.create_vm(
            db,
            make_config(),
            name="new-vm",
            template="missing",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert (caught.value.entity_kind, caught.value.entity_name) == ("vm-template", "missing")


def test_create_refuses_invalid_provider_input_before_db_or_platform_mutation(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config(
        manifests=[
            GIT_CRED_GH,
            ManifestDoc("admin-template", "default", {"git_credentials": ["gh"]}),
        ]
    )
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "invalid\nvalue")
    create = MagicMock()
    initialize = MagicMock()
    monkeypatch.setattr(LimaPlatform, "create", create)
    monkeypatch.setattr(vm_manager, "run_initialization", initialize)

    with pytest.raises(ValidationError):
        vm_manager.create_vm(db, config, name="invalid-git", interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_vm("invalid-git") is None
    create.assert_not_called()
    initialize.assert_not_called()


def test_create_refuses_duplicate_static_scopes_before_db_or_platform_mutation(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    credentials = [
        ManifestDoc(
            "git-credential",
            name,
            {"provider": {"name": "github", "source": {"mode": "secret"}}},
        )
        for name in ("first", "second")
    ]
    config = make_config(
        manifests=[
            *credentials,
            ManifestDoc(
                "admin-template",
                "default",
                {"git_credentials": ["first", "second"]},
            ),
        ]
    )
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_FIRST", "first-token")
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_SECOND", "second-token")
    create = MagicMock()
    monkeypatch.setattr(LimaPlatform, "create", create)

    with pytest.raises(ConfigError):
        vm_manager.create_vm(db, config, name="duplicate-git", interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_vm("duplicate-git") is None
    create.assert_not_called()


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
    from agentworks.vms.nodes import (
        pending_vm_node,
        vm_site_node,
        vm_template_node,
    )
    from agentworks.vms.templates import resolve_template

    config = make_config(
        PLUGINS_ENABLED,
        manifests=[
            proxmox_site(),
            GIT_CRED_GH,
            ManifestDoc("admin-template", "default", {"git_credentials": ["gh"]}),
            ManifestDoc("vm-template", "default", {"env": {"API_KEY": {"secret": "api-key"}}}),
            ManifestDoc("secret", "api-key", description="runtime only"),
        ],
    )
    registry = build_registry(config)
    admin = admin_template(registry)
    assert admin.git_credentials == ["gh"]

    creds = tuple(git_credential_node(registry, name) for name in admin.git_credentials)
    template = vm_template_node(resolve_template(registry, None))
    site = vm_site_node(registry, "proxmox")
    pending = pending_vm_node(db, "nvm", DebianRelease.TRIXIE, template, site, creds)
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


def test_create_admin_spec_credential_joins_graph_and_logger_redactions(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """An admin final layer contributes to the create graph and its log."""
    from agentworks.capabilities.vm_platform import ProvisionRequest
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config(manifests=[GIT_CRED_GH])
    events: list[str] = []
    captured_redactions: list[tuple[str, ...]] = []
    loggers: list[object] = []

    class _LoggerSpy:
        display_path = "~/.config/agentworks/logs/logvm-vm-create.log"
        has_warnings = False
        warnings: list[str] = []

        def __init__(self, vm_name: str, command_stem: str, *, redactions: tuple[str, ...] = ()) -> None:
            assert (vm_name, command_stem) == ("logvm", "vm-create")
            events.append("logger")
            captured_redactions.append(redactions)
            loggers.append(self)

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr("agentworks.ssh.SSHLogger", _LoggerSpy)

    def _create(self: LimaPlatform, request: ProvisionRequest, ctx: object) -> ProvisionResult:
        events.append("create")
        assert request.progress is loggers[0]
        return ProvisionResult(
            tailnet_address="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _create)

    def _phase_a(*args: object, **kwargs: object) -> tuple[object, str, DebianRelease]:
        events.append("phase-a")
        assert args[5] is loggers[0]
        return _completed_phase_a(*args, **kwargs)

    monkeypatch.setattr(vm_manager, "bootstrap_vm", _phase_a)

    def _phase_b(*args: object, **kwargs: object) -> None:
        events.append("phase-b")
        assert args[10] is loggers[0]
        providers = cast("tuple[CredentialRequest, ...]", args[7])
        assert [request.name for request in providers] == ["gh"]
        request = providers[0]
        payload = request.provider.credential_material(request.context())
        assert isinstance(payload, StoredCredential)
        assert payload.password == "ghtok"

    monkeypatch.setattr(vm_manager, "run_initialization", _phase_b)

    vm_manager.create_vm(
        db,
        config,
        name="logvm",
        admin_spec='{"git_credentials":["gh"]}',
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert captured_redactions == [("tskey-test", "ghtok")]
    assert events == ["logger", "create", "phase-a", "phase-b", "close"]


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("log exploded"), KeyboardInterrupt("stop")],
    ids=("ordinary", "interrupt"),
)
def test_logger_construction_failure_never_dispatches_and_unwinds_row(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.errors import ProvisioningError

    def _logger_failure(*args: object, **kwargs: object) -> None:
        raise failure

    create = MagicMock()
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _logger_failure)
    monkeypatch.setattr(LimaPlatform, "create", create)

    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt) as interrupt_caught:
            vm_manager.create_vm(db, make_config(), name="nologvm", interaction=TtyInteractionPolicy.REFUSE)
        assert interrupt_caught.value is failure
    else:
        with pytest.raises(ProvisioningError) as provisioning_caught:
            vm_manager.create_vm(db, make_config(), name="nologvm", interaction=TtyInteractionPolicy.REFUSE)
        assert provisioning_caught.value.__cause__ is failure

    create.assert_not_called()
    assert db.get_vm("nologvm") is None


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("create exploded"), KeyboardInterrupt("stop")],
    ids=("ordinary", "interrupt"),
)
def test_create_failure_closes_logger_once_without_replacing_primary(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
    failure: BaseException,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.errors import ProvisioningError

    closes: list[str] = []
    close_interrupt = KeyboardInterrupt("close interrupted")

    class _LoggerSpy:
        display_path = "~/.config/agentworks/logs/failvm-vm-create.log"
        has_warnings = False
        warnings: list[str] = []

        def __init__(self, vm_name: str, command_stem: str, *, redactions: tuple[str, ...] = ()) -> None:
            pass

        def close(self) -> None:
            closes.append("close")
            raise close_interrupt

    monkeypatch.setattr("agentworks.ssh.SSHLogger", _LoggerSpy)

    def _fail(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        raise failure

    monkeypatch.setattr(LimaPlatform, "create", _fail)

    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt) as interrupt_caught:
            vm_manager.create_vm(db, make_config(), name="failvm", interaction=TtyInteractionPolicy.REFUSE)
        assert interrupt_caught.value is failure
        assert any(
            "Log: ~/.config/agentworks/logs/failvm-vm-create.log" in warning for warning in captured_output.warnings
        )
    else:
        with pytest.raises(ProvisioningError) as provisioning_caught:
            vm_manager.create_vm(db, make_config(), name="failvm", interaction=TtyInteractionPolicy.REFUSE)
        assert provisioning_caught.value.__cause__ is failure
        assert "Details: ~/.config/agentworks/logs/failvm-vm-create.log" in str(provisioning_caught.value)

    assert closes == ["close"]
    assert db.get_vm("failvm") is None
    assert any(
        "could not close provisioning log ~/.config/agentworks/logs/failvm-vm-create.log: close interrupted" in warning
        for warning in captured_output.warnings
    )


def test_successful_create_propagates_fresh_logger_close_interrupt(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    close_interrupt = KeyboardInterrupt("fresh close interrupt")
    closes: list[str] = []

    class _LoggerSpy:
        display_path = "~/.config/agentworks/logs/freshvm-vm-create.log"
        has_warnings = False
        warnings: list[str] = []

        def __init__(self, vm_name: str, command_stem: str, *, redactions: tuple[str, ...] = ()) -> None:
            pass

        def close(self) -> None:
            closes.append("close")
            raise close_interrupt

    monkeypatch.setattr("agentworks.ssh.SSHLogger", _LoggerSpy)
    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda self, request, ctx: ProvisionResult(  # noqa: ARG005
            tailnet_address="100.64.0.7",
        ),
    )
    monkeypatch.setattr(
        vm_manager,
        "bootstrap_vm",
        _completed_phase_a,
    )
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *args, **kwargs: None)

    with pytest.raises(KeyboardInterrupt) as caught:
        vm_manager.create_vm(db, make_config(), name="freshvm", interaction=TtyInteractionPolicy.REFUSE)

    assert caught.value is close_interrupt
    assert closes == ["close"]
    assert db.get_vm("freshvm") is not None
    assert not any("could not close provisioning log" in warning for warning in captured_output.warnings)


def test_logger_close_warning_failure_does_not_replace_primary(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.errors import ProvisioningError

    primary = RuntimeError("create exploded")
    close_interrupt = KeyboardInterrupt("close interrupted")
    warning_failure = SystemExit("warning failed")
    closes: list[str] = []
    warning_messages: list[str] = []

    class _LoggerSpy:
        display_path = "~/.config/agentworks/logs/warnvm-vm-create.log"
        has_warnings = False
        warnings: list[str] = []

        def __init__(self, vm_name: str, command_stem: str, *, redactions: tuple[str, ...] = ()) -> None:
            pass

        def close(self) -> None:
            closes.append("close")
            raise close_interrupt

    monkeypatch.setattr("agentworks.ssh.SSHLogger", _LoggerSpy)

    def _fail_create(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        raise primary

    def _fail_warning(message: str) -> None:
        warning_messages.append(message)
        raise warning_failure

    monkeypatch.setattr(LimaPlatform, "create", _fail_create)
    monkeypatch.setattr("agentworks.vms.manager.lifecycle.output.warn", _fail_warning)

    with pytest.raises(ProvisioningError) as caught:
        vm_manager.create_vm(db, make_config(), name="warnvm", interaction=TtyInteractionPolicy.REFUSE)

    assert caught.value.__cause__ is primary
    assert closes == ["close"]
    assert warning_messages == [
        "could not close provisioning log ~/.config/agentworks/logs/warnvm-vm-create.log: close interrupted"
    ]
    assert db.get_vm("warnvm") is None


# -- vm create: unwind parity ------------------------------------------------


def test_create_rollback_on_keyboard_interrupt_unwinds_the_row(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    """The unwind oracle, interrupt flavor: a cancel during
    provisioning deletes the row (the realized set, reverse order,
    which for create is exactly the one VM node) and re-raises. The
    row deletion is safe because the platform's create owns rolling
    back its own partial backend resources before the interrupt
    reaches this handler (the create contract; #338, exercised in
    test_azure_create_interrupt.py)."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    def _interrupt(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(LimaPlatform, "create", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        vm_manager.create_vm(db, make_config(), name="ivm", interaction=TtyInteractionPolicy.REFUSE)
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
        vm_manager.create_vm(db, make_config(), name="avm", interaction=TtyInteractionPolicy.REFUSE)
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
        vm_manager.create_vm(db, make_config(), name="wvm", interaction=TtyInteractionPolicy.REFUSE)
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

    closes: list[str] = []

    class _LoggerSpy:
        display_path = "~/.config/agentworks/logs/kvm-vm-create.log"
        has_warnings = False
        warnings: list[str] = []

        def __init__(
            self,
            vm_name: str,
            command_stem: str,
            *,
            redactions: tuple[str, ...] = (),
        ) -> None:
            pass

        def close(self) -> None:
            closes.append("close")

    monkeypatch.setattr("agentworks.ssh.SSHLogger", _LoggerSpy)

    def _fake_create(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        return ProvisionResult(
            platform_metadata={},
            tailnet_address="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)
    # Phase A succeeds (its steps are exercised elsewhere); Phase B explodes.
    monkeypatch.setattr(vm_manager, "bootstrap_vm", _completed_phase_a)

    def _init_boom(*a: object, **k: object) -> None:
        raise RuntimeError("init exploded")

    monkeypatch.setattr(vm_manager, "run_initialization", _init_boom)
    with pytest.raises(ExternalError, match="init exploded"):
        vm_manager.create_vm(db, make_config(), name="kvm", interaction=TtyInteractionPolicy.REFUSE)
    assert db.get_vm("kvm") is not None
    assert closes == ["close"]


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
            platform_metadata={},
            tailnet_address="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)

    def _boom_bootstrap(db_: Database, config_: object, vm_name: str, *a: object, **k: object) -> None:
        # Mirror the real bootstrap_vm's fatal path: mark provisioning
        # failed, then raise for create_vm's mapping to pick up.
        db_.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
        raise RuntimeError("bootstrap exploded")

    monkeypatch.setattr(vm_manager, "bootstrap_vm", _boom_bootstrap)

    def _no_phase_b(*a: object, **k: object) -> None:
        raise AssertionError("Phase B ran despite a Phase A failure")

    monkeypatch.setattr(vm_manager, "run_initialization", _no_phase_b)

    with pytest.raises(ProvisioningError, match="bootstrap exploded") as exc:
        vm_manager.create_vm(db, make_config(), name="fvm", interaction=TtyInteractionPolicy.REFUSE)
    assert "vm delete fvm" in (exc.value.hint or "")
    row = db.get_vm("fvm")
    assert row is not None  # kept: past the unwind window
    assert row.provisioning_status == ProvisioningStatus.FAILED.value


@pytest.mark.parametrize(
    ("failure", "raised"),
    [
        (KeyboardInterrupt(), KeyboardInterrupt),
        (UserAbort("stop"), UserAbort),
        (RuntimeError("hold failed"), ExternalError),
    ],
    ids=["interrupt", "user-abort", "failure"],
)
def test_create_retains_a_vm_failed_whenever_phase_a_did_not_complete(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
    failure: BaseException,
    raised: type[BaseException],
) -> None:
    """The availability hold is entered before Phase A, with the row still pending: a VM stopped
    there was never attested, so it is retained failed, out of the ordinary commands' gates."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import ProvisioningStatus

    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda self, request, ctx: ProvisionResult(  # noqa: ARG005
            platform_metadata={},
            tailnet_address="100.64.0.7",
        ),
    )

    @contextlib.contextmanager
    def _hold_fails(self: LimaPlatform, vm: object, *, config: object | None = None):
        raise failure
        yield

    monkeypatch.setattr(LimaPlatform, "vm_active", _hold_fails)
    monkeypatch.setattr(vm_manager, "bootstrap_vm", lambda *a, **k: pytest.fail("Phase A reached"))

    with pytest.raises(raised) as caught:
        vm_manager.create_vm(db, make_config(), name="hvm", interaction=TtyInteractionPolicy.REFUSE)

    row = db.get_vm("hvm")
    assert row is not None
    assert row.provisioning_status == ProvisioningStatus.FAILED.value
    if isinstance(caught.value, ExternalError):
        assert "vm delete hvm" in (caught.value.hint or "")


def test_an_interrupt_after_phase_a_completed_leaves_the_vm_complete(
    make_config, db: Database, monkeypatch: pytest.MonkeyPatch, captured_output
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import ProvisioningStatus

    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda self, request, ctx: ProvisionResult(  # noqa: ARG005
            platform_metadata={},
            tailnet_address="100.64.0.7",
        ),
    )
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)
    monkeypatch.setattr(vm_manager, "bootstrap_vm", _completed_phase_a)

    def _interrupt(*a: object, **k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(vm_manager, "run_initialization", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        vm_manager.create_vm(db, make_config(), name="cvm", interaction=TtyInteractionPolicy.REFUSE)

    row = db.get_vm("cvm")
    assert row is not None
    assert row.provisioning_status == ProvisioningStatus.COMPLETE.value


def _stub_create_with_trust_probes(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
    seen: dict[str, object],
    *,
    bootstrap_fails: bool = False,
) -> None:
    """Fake the platform's create and Phase A, recording what the host-key
    store and the provisioning file look like at each point."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.execution import trust

    def _fake_create(self: LimaPlatform, request: Any, ctx: object) -> ProvisionResult:
        binding = request.provisioning_trust
        seen["binding"] = binding
        seen["row_at_create"] = db.get_vm(request.vm_name) is not None
        seen["record_at_create"] = trust.has_record(request.vm_name)
        seen["file_at_create"] = binding.provisioning_file.is_file()
        return ProvisionResult(platform_metadata={}, tailnet_address="100.64.0.7")

    def _bootstrap(
        db_: Database, config_: object, vm_name: str, *a: object, **k: object
    ) -> tuple[object, str, DebianRelease]:
        # Nothing after the platform's create reaches the VM at its public address.
        seen["file_at_bootstrap"] = seen["binding"].provisioning_file.is_file()  # type: ignore[attr-defined]
        if bootstrap_fails:
            raise RuntimeError("bootstrap exploded")
        return _completed_phase_a(db_, config_, vm_name, *a, **k)

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)
    monkeypatch.setattr(vm_manager, "bootstrap_vm", _bootstrap)
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)


def test_create_resets_the_store_once_the_row_exists_and_hands_cloud_create_an_unverified_file(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    from agentworks.execution import trust

    seed_vm_trust("nvm")  # a stale record from an earlier VM of the same name
    seen: dict[str, object] = {}
    _stub_create_with_trust_probes(monkeypatch, db, seen)

    vm_manager.create_vm(db, make_config(), name="nvm", interaction=TtyInteractionPolicy.REFUSE)

    assert seen["row_at_create"] is True
    assert seen["record_at_create"] is False
    binding = seen["binding"]
    assert isinstance(binding, trust.TrustBinding)
    assert binding.mode is trust.TrustMode.UNVERIFIED
    assert seen["file_at_create"] is True
    assert seen["file_at_bootstrap"] is False
    assert binding.provisioning_file is not None
    assert not binding.provisioning_file.exists()


def test_create_removes_the_provisioning_file_when_a_later_step_fails(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    seen: dict[str, object] = {}
    _stub_create_with_trust_probes(monkeypatch, db, seen, bootstrap_fails=True)

    with pytest.raises(ExternalError, match="bootstrap exploded"):
        vm_manager.create_vm(db, make_config(), name="fvm", interaction=TtyInteractionPolicy.REFUSE)

    binding = seen["binding"]
    assert binding.provisioning_file is not None  # type: ignore[attr-defined]
    assert not binding.provisioning_file.exists()  # type: ignore[attr-defined]


def test_create_that_cannot_reset_the_store_unwinds_the_row_before_the_platform_runs(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.execution import trust

    def _refuse(vm_name: str) -> None:
        raise StateError("cannot reset the host-key store")

    def _platform_must_not_run(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        raise AssertionError("the platform ran without a reset store")

    monkeypatch.setattr(trust, "begin_enrollment", _refuse)
    monkeypatch.setattr(LimaPlatform, "create", _platform_must_not_run)

    with pytest.raises(StateError):
        vm_manager.create_vm(db, make_config(), name="uvm", interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_vm("uvm") is None


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

    config = make_config(f'ssh_config = "{(tmp_path / "ssh_config").as_posix()}"\n')

    def _fake_create(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        return ProvisionResult(
            platform_metadata={},
            tailnet_address="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)

    # Phase A runs for real over the recording access (the module's contract fixture);
    # Phase B's legacy target is only built, since the initializer is faked.
    monkeypatch.setattr(driver, "SSHTransport", lambda **kwargs: SimpleNamespace(**kwargs))

    # The SSH-config sync fails (e.g. read-only home) at the end of Phase A
    # (and again on the post-init re-sync); both sites handle it non-fatally.
    def _boom_sync(*a: object, **k: object) -> None:
        raise RuntimeError("read-only home")

    monkeypatch.setattr(ssh_config_mod, "sync_ssh_config", _boom_sync)

    phase_b_ran: list[bool] = []
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *a, **k: phase_b_ran.append(True))

    # Does not raise: the sync failure is non-fatal.
    vm_manager.create_vm(db, config, name="svm", interaction=TtyInteractionPolicy.REFUSE)

    row = db.get_vm("svm")
    assert row is not None
    # Reachable VM stays COMPLETE (not FAILED), so `vm reinit` remains open.
    assert row.provisioning_status == ProvisioningStatus.COMPLETE.value
    assert phase_b_ran == [True]  # Phase B ran despite the sync failure
    assert any("SSH config sync failed" in w for w in captured_output.warnings)


def test_create_provisioning_section_has_explicit_closing_body_line(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_output,
) -> None:
    """Provisioning closes explicitly after Phase A and before Phase B."""
    import agentworks.vms.initializer.driver as driver
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    # Contain the real SSH-config write inside the test's tmp dir.
    config = make_config(f'ssh_config = "{(tmp_path / "ssh_config").as_posix()}"\n')

    def _fake_create(self: LimaPlatform, request: object, ctx: object) -> ProvisionResult:
        return ProvisionResult(
            platform_metadata={},
            tailnet_address="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)

    # Phase A runs for real over the recording access (the module's contract fixture);
    # Phase B's legacy target is only built, since the initializer is faked.
    monkeypatch.setattr(driver, "SSHTransport", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *a, **k: None)

    vm_manager.create_vm(db, config, name="pvm", interaction=TtyInteractionPolicy.REFUSE)

    headers = [
        index
        for index, (role, level, _message) in enumerate(captured_output.lines)
        if role is Role.HEADER and level == 0
    ]
    assert len(headers) >= 3
    provisioning = captured_output.lines[headers[2] + 1 :]
    body_shape = [(role, level) for role, level, _message in provisioning if role is Role.BODY]
    # Phase A's final announcement is followed by the manager's explicit
    # section-closing announcement, both at the section body level.
    assert body_shape[-2:] == [(Role.BODY, 1), (Role.BODY, 1)]


# -- vm reinit: the orchestrated path ----------------------------------------


def _seed_provisioned_vm(db: Database) -> None:
    from agentworks.db import ProvisioningStatus

    db.insert_vm("rvm", site="lima-local", hostname="rvm")
    db.update_vm_tailscale("rvm", "100.64.0.9")
    db.update_vm_provisioning_status("rvm", ProvisioningStatus.COMPLETE)


def _reinit_transport() -> SimpleNamespace:
    """Phase B's legacy target, which these tests' faked initializer never uses."""
    return SimpleNamespace()


TRIXIE_OS_RELEASE = b'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n'
BOOKWORM_OS_RELEASE = b'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'
CLEAN_SCRIPT = b"AGW-CHECK-END\n"


def _contract_outcomes(
    *, os_release: bytes = TRIXIE_OS_RELEASE, script: bytes = CLEAN_SCRIPT, sudo: bytes = b"0\n"
) -> list[Result | BaseException]:
    """What the VM answers to the base checks: the release, the script, then the elevated sudo check."""
    return [Result(Completed(0), os_release), Result(Completed(0), script), Result(Completed(0), sudo)]


@pytest.fixture(autouse=True)
def _contract_passes(vm_access_recorder: AccessRecorder) -> AccessRecorder:
    """Reinit's base checks run over the recording access, and pass unless a test scripts otherwise."""
    seed_vm_trust("rvm")
    vm_access_recorder.access(vm_name="rvm").run_outcomes = _contract_outcomes()
    return vm_access_recorder


def test_reinit_runs_initialization_through_the_gate(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
    _contract_passes: AccessRecorder,
) -> None:
    """Reinit end to end on a reachable VM: one boundary pass covering
    the union (git token; the lima site has no config secrets), scoped
    tokens handed to the initializer, and the whole init held inside
    the activation span."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config(manifests=[GIT_CRED_GH, ManifestDoc("admin-template", "default", {"git_credentials": ["gh"]})])
    _seed_provisioned_vm(db)
    legacy_checked: list[str] = []
    monkeypatch.setattr(
        "agentworks.vms.manager.lifecycle._warn_legacy_release",
        lambda vm: legacy_checked.append(vm.name),
    )
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
    logger_redactions: list[tuple[str, ...]] = []

    class _LoggerSpy:
        path = "/dev/null"
        warnings: list[str] = []

        def __init__(self, vm_name: str, command_stem: str, *, redactions: tuple[str, ...] = ()) -> None:
            logger_redactions.append(redactions)

        def step(self, name: str) -> None:
            pass

        def record(self, request: object, result: object) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("agentworks.ssh.SSHLogger", _LoggerSpy)
    _contract_passes.access(vm_name="rvm").run_outcomes = _contract_outcomes(os_release=BOOKWORM_OS_RELEASE)

    def _fake_init(*args: object, **kwargs: object) -> None:
        captured["providers"] = args[7]
        captured["held"] = list(holds)
        captured["debian_release"] = kwargs["debian_release"]

    monkeypatch.setattr(vm_manager, "run_initialization", _fake_init)
    import agentworks.transports as transports

    monkeypatch.setattr(transports, "transport", lambda vm, config, **kw: _reinit_transport())

    vm_manager.reinit_vm(db, config, "rvm", interaction=TtyInteractionPolicy.REFUSE)

    assert captured["debian_release"] is DebianRelease.BOOKWORM
    # The base checks, release first, over the admin's access, and nothing else on the new stack.
    assert _contract_passes.operations == [("admin", "run")] * 3
    assert legacy_checked == ["rvm"]
    assert logger_redactions == [("ghtok",)]
    requests = cast("tuple[CredentialRequest, ...]", captured["providers"])
    assert [request.name for request in requests] == ["gh"]
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


def test_reinit_refuses_invalid_configured_identity_before_activation_or_transport(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_provisioned_vm(db)
    failure = ConfigError("invalid test identity")
    prepare = MagicMock(side_effect=failure)
    activate = MagicMock()
    make_transport = MagicMock()
    monkeypatch.setattr(
        "agentworks.vms.applied_state.prepare_configured_ssh_identity",
        prepare,
    )
    monkeypatch.setattr("agentworks.orchestration.activation.activation_gate", activate)
    monkeypatch.setattr("agentworks.transports.transport", make_transport)

    with pytest.raises(ConfigError) as caught:
        vm_manager.reinit_vm(db, config, "rvm", interaction=TtyInteractionPolicy.REFUSE)

    assert caught.value is failure
    prepare.assert_called_once_with(
        config.operator.ssh_public_key,
        config.operator.ssh_private_key,
    )
    activate.assert_not_called()
    make_transport.assert_not_called()


def test_reinit_resolves_the_stored_admin_template(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_output,
) -> None:
    """Reinit reapplies the stored admin layer after its selected template."""
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
          shell: bash
        """)
    )
    config = make_config(manifests=[GIT_CRED_GH])
    db.insert_vm("rvm", site="lima-local", hostname="rvm", admin_template="work")
    db.update_vm_tailscale("rvm", "100.64.0.9")
    db.update_vm_provisioning_status("rvm", ProvisioningStatus.COMPLETE)
    from agentworks.instance_specs import parse_vm_instance_specs

    overlays = parse_vm_instance_specs(None, '{"git_credentials":["gh"],"shell":"zsh"}')
    assert overlays is not None
    db.instance_state.put_desired_overlay("vm", "rvm", overlays.payload)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)

    import contextlib as _contextlib

    @_contextlib.contextmanager
    def _hold(self: LimaPlatform, vm: object, *, config: object | None = None):
        yield

    monkeypatch.setattr(LimaPlatform, "vm_active", _hold)
    captured: dict[str, object] = {}

    def _fake_init(*args: object, **kwargs: object) -> None:
        captured["providers"] = args[7]
        captured["admin"] = args[4]

    monkeypatch.setattr(vm_manager, "run_initialization", _fake_init)
    import agentworks.transports as transports

    monkeypatch.setattr(transports, "transport", lambda vm, config, **kw: _reinit_transport())

    vm_manager.reinit_vm(db, config, "rvm", interaction=TtyInteractionPolicy.REFUSE)

    requests = cast("tuple[CredentialRequest, ...]", captured["providers"])
    assert [request.name for request in requests] == ["gh"]
    assert isinstance(captured["admin"], AdminConfig)
    assert captured["admin"].shell == "zsh"


def test_reinit_applies_a_legacy_flat_vm_payload(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VM desired layer written before paired admin layers still reapplies."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config()
    _seed_provisioned_vm(db)
    db.instance_state.put_desired_overlay("vm", "rvm", VersionedPayload(1, {"cpus": 11}))
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)

    @contextlib.contextmanager
    def _hold(self: LimaPlatform, vm: object, *, config: object | None = None):
        yield

    monkeypatch.setattr(LimaPlatform, "vm_active", _hold)
    captured: dict[str, object] = {}

    def _fake_init(*args: object, **kwargs: object) -> None:
        captured["vm_template"] = args[3]

    monkeypatch.setattr(vm_manager, "run_initialization", _fake_init)
    monkeypatch.setattr("agentworks.transports.transport", lambda vm, config, **kw: _reinit_transport())

    vm_manager.reinit_vm(db, config, "rvm", interaction=TtyInteractionPolicy.REFUSE)

    vm_template = captured["vm_template"]
    assert isinstance(vm_template, ResolvedVMTemplate)
    assert vm_template.cpus == 11


def _reinit_with_faked_initializer(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    recorder: AccessRecorder,
    *,
    outcomes: list[Result | BaseException],
    initialized: list[bool] | None = None,
) -> list[bool]:
    """Reinit ``rvm`` with the base checks answered by ``outcomes``; ``initialized`` records each initialization."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config()
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)

    @contextlib.contextmanager
    def _hold(self: LimaPlatform, vm: object, *, config: object | None = None):
        yield

    monkeypatch.setattr(LimaPlatform, "vm_active", _hold)
    ran = [] if initialized is None else initialized
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *a, **k: ran.append(True))
    monkeypatch.setattr("agentworks.transports.transport", lambda vm, config, **kw: _reinit_transport())
    recorder.access(vm_name="rvm").run_outcomes = outcomes
    vm_manager.reinit_vm(db, config, "rvm", interaction=TtyInteractionPolicy.REFUSE)
    return ran


@pytest.mark.parametrize(
    "script",
    [b"AGW-CHECK pam-env /etc/environment\nAGW-CHECK-END\n", b"AGW-CHECK login-shells agentworks\nAGW-CHECK-END\n"],
    ids=["pam-env", "admin-login-shell"],
)
def test_reinit_warns_for_a_contract_warning_and_still_initializes(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
    _contract_passes: AccessRecorder,
    script: bytes,
) -> None:
    _seed_provisioned_vm(db)

    initialized = _reinit_with_faked_initializer(
        make_config, db, monkeypatch, _contract_passes, outcomes=_contract_outcomes(script=script)
    )

    assert initialized == [True]
    assert len(captured_output.warnings) == 1


@pytest.mark.parametrize(
    "outcomes",
    [
        _contract_outcomes(script=b"AGW-CHECK base-tools flock\nAGW-CHECK-END\n"),
        _contract_outcomes(script=b"AGW-CHECK pam-env /etc/environment\n"),
        _contract_outcomes(sudo=b""),
        [Result(NotDelivered(NotDeliveredReason.PYTHON_MISSING))],
    ],
    ids=["base-tools", "script-incomplete", "sudo", "no-python"],
)
def test_reinit_stops_before_initialization_when_the_contract_fails(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    _contract_passes: AccessRecorder,
    outcomes: list[Result | BaseException],
) -> None:
    _seed_provisioned_vm(db)
    initialized: list[bool] = []

    with pytest.raises(StateError):
        _reinit_with_faked_initializer(
            make_config, db, monkeypatch, _contract_passes, outcomes=outcomes, initialized=initialized
        )

    assert initialized == []


def test_reinit_refuses_live_release_drift_before_initialization(
    make_config, db: Database, monkeypatch: pytest.MonkeyPatch, _contract_passes: AccessRecorder
) -> None:
    _seed_provisioned_vm(db)
    db.update_vm_debian_release("rvm", DebianRelease.BOOKWORM)
    enforced: list[object] = []
    monkeypatch.setattr("agentworks.vms.contract.enforce", lambda report, name: enforced.append(report))

    with pytest.raises(StateError) as caught:
        _reinit_with_faked_initializer(
            make_config,
            db,
            monkeypatch,
            _contract_passes,
            outcomes=_contract_outcomes(script=b"AGW-CHECK pam-env /etc/environment\nAGW-CHECK-END\n"),
        )

    assert "confirm-release" in (caught.value.hint or "")
    # Drift refuses before any other finding is shown.
    assert enforced == []
    row = db.get_vm("rvm")
    assert row is not None
    assert row.debian_release is DebianRelease.BOOKWORM


def test_reinit_checks_the_admin_and_every_agent_on_the_vm(
    make_config, db: Database, monkeypatch: pytest.MonkeyPatch, _contract_passes: AccessRecorder
) -> None:
    _seed_provisioned_vm(db)
    db.insert_agent("worker", "rvm", "agt-worker")

    _reinit_with_faked_initializer(make_config, db, monkeypatch, _contract_passes, outcomes=_contract_outcomes())

    script = _contract_passes.requests("run")[1]
    assert isinstance(script, Script)
    assert script.env[ACCOUNTS_VARIABLE].split() == ["agentworks", "agt-worker"]
    assert [identity for _vm, identity in _contract_passes.built] == [Admin()]
    assert _contract_passes.enrolled == []  # reinit never enrolls a host key


@pytest.mark.parametrize("admin_template", ["", "work"])
def test_reinit_errors_cleanly_for_an_unresolved_stored_admin_template(
    make_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,
    admin_template: str,
) -> None:
    """An unresolved stored selector produces a typed error before work."""
    from agentworks.db import ProvisioningStatus
    from agentworks.errors import NotFoundError

    config = make_config()
    db.insert_vm("rvm", site="lima-local", hostname="rvm", admin_template=admin_template)
    db.update_vm_tailscale("rvm", "100.64.0.9")
    db.update_vm_provisioning_status("rvm", ProvisioningStatus.COMPLETE)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)

    called = False

    def _fake_init(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(vm_manager, "run_initialization", _fake_init)

    with pytest.raises(NotFoundError) as caught:
        vm_manager.reinit_vm(db, config, "rvm", interaction=TtyInteractionPolicy.REFUSE)
    assert caught.value.entity_kind == "admin-template"
    assert caught.value.entity_name == admin_template
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
        vm_manager.reinit_vm(db, config, "rvm", interaction=TtyInteractionPolicy.REFUSE)
    assert "agw vm start rvm" in (exc.value.hint or "")

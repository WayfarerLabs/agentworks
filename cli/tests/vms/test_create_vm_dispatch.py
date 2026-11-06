"""``create_vm`` through the composition root: the ProvisionRequest
shape handed to the bound platform, the persisted row, and the proxmox
config-secret resolve pass end to end (no env-read shadow path).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, ProvisionResult, RetainedProvisioningError
from agentworks.config import load_config
from agentworks.debian import DebianRelease
from agentworks.errors import (
    AuthorizationError,
    ConfigError,
    ConnectivityError,
    NotFoundError,
    ProvisioningError,
    StateError,
    TokenRejectedError,
    UserAbort,
    ValidationError,
)
from agentworks.execution import trust
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests.conftest import CapturedOutput, ManifestDoc, pin_wsl2_unsupported, write_manifests
from tests.orchestrated_fixtures import proxmox_site
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY, write_test_ssh_keypair

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.db import Database

# Proxmox ships in the opt-in ``proxmox`` system plugin since Phase 10 (R11),
# so a config that uses the proxmox site enables the plugin, exactly as a real
# proxmox operator would. The plugin opt-in is a settings section; the site
# itself is declared as a ``vm-site`` manifest (ADR 0022).
PLUGINS_ENABLED = """
[plugins]
system = ["proxmox"]
"""


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "id_ed25519"
    write_test_ssh_keypair(key)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-test")
    # Deterministic platform preflights: lima checks for limactl
    # locally; pretend the tool exists regardless of the host. wsl2 is
    # host-ready on a Windows test host but not on Linux, so pin it
    # unsupported to keep exactly one site (lima-local) ready on any host.
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


def test_create_vm_request_shape_and_row(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """The bound lima platform receives the provision request (bare-name
    hostname, null slug pre-Phase-4) and the returned platform_metadata
    persists verbatim. The request's cpus comes from the typed final
    instance layer, which also persists with the owner."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.instance_specs import OverlayDisposition, OverlayOutcome

    config = make_config(manifests=[ManifestDoc("vm-template", "default", {"cpus": 2})])
    captured_request: list[ProvisionRequest] = []
    captured_platform: list[LimaPlatform] = []
    initialized_release: list[DebianRelease] = []
    attested_release: list[DebianRelease] = []
    outcomes: list[OverlayOutcome] = []
    start_events: list[str] = []
    monkeypatch.setattr("agentworks.instance_specs.render_overlay_outcome", outcomes.append)

    def _fake_create(self: LimaPlatform, request: ProvisionRequest, ctx: object) -> ProvisionResult:
        start_events.append("provider-create")
        captured_platform.append(self)
        captured_request.append(request)
        return ProvisionResult(
            platform_metadata={"instance_name": "dvm"},
            tailnet_address="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    record_vm_started = db.record_vm_started

    def _record_started(name: str) -> None:
        start_events.append("record-start")
        record_vm_started(name)

    monkeypatch.setattr(db, "record_vm_started", _record_started)

    # Phase A is faked here: it attests and records the release core asked
    # for, which Phase B then receives. This suite pins the create() request
    # shape and the persisted row, not the init sequence.
    def _attest(*args: object, expected_release: DebianRelease, tailnet_address: str, **kwargs: object):
        start_events.append("attest")
        attested_release.append(expected_release)
        db.update_vm_debian_release("dvm", expected_release)
        return SimpleNamespace(), "/home/agentworks", expected_release

    monkeypatch.setattr(vm_manager, "bootstrap_vm", _attest)

    def _fake_init(*args: object, **kwargs: object) -> None:
        del args
        initialized_release.append(kwargs["debian_release"])  # type: ignore[arg-type]
        observed = db.get_vm("dvm")
        assert observed is not None
        assert observed.debian_release is DebianRelease.TRIXIE

    monkeypatch.setattr(vm_manager, "run_initialization", _fake_init)

    vm_manager.create_vm(
        db,
        config,
        name="dvm",
        spec='{"cpus":6}',
        admin_spec='{"username":"operator","shell":"zsh"}',
        interaction=TtyInteractionPolicy.REFUSE,
    )

    (request,) = captured_request
    assert request.vm_name == "dvm"
    assert request.debian_release is DebianRelease.TRIXIE
    assert request.hostname == "dvm"  # no slug: the bare name
    assert request.system_slug is None
    assert request.cpus == 6
    assert request.admin_username == "operator"
    assert request.ssh_public_key == TEST_SSH_PUBLIC_KEY
    (bound,) = captured_platform
    assert bound.site_name == "lima-local"

    vm = db.get_vm("dvm")
    assert vm is not None
    assert vm.site == "lima-local"
    assert vm.hostname == "dvm"
    assert vm.platform_metadata == {"instance_name": "dvm"}
    assert vm.debian_release is DebianRelease.TRIXIE
    assert vm.debian_release_observed_at is not None
    assert vm.operator_stopped is False
    assert vm.last_started_at is not None
    assert attested_release == [DebianRelease.TRIXIE]
    assert initialized_release == [DebianRelease.TRIXIE]
    assert start_events == ["provider-create", "record-start", "attest"]
    stored = db.instance_state.get_desired_overlay("vm", "dvm")
    assert stored is not None and stored.payload.value == {
        "vm": {"cpus": 6},
        "admin": {"shell": "zsh", "username": "operator"},
    }
    assert [(outcome.disposition, outcome.fields) for outcome in outcomes] == [
        (OverlayDisposition.SET, ("admin.shell", "admin.username", "vm.cpus"))
    ]


def test_create_vm_request_build_failure_unwinds_owner_and_overlay_without_outcome(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.instance_specs import OverlayOutcome

    config = make_config()
    config.operator.ssh_public_key.unlink()
    outcomes: list[OverlayOutcome] = []
    monkeypatch.setattr("agentworks.instance_specs.render_overlay_outcome", outcomes.append)
    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *args, **kwargs: pytest.fail("platform create reached after request construction failed"),
    )

    with pytest.raises(ConfigError):
        vm_manager.create_vm(
            db,
            config,
            name="request-fail",
            spec='{"cpus":6}',
            admin_spec='{"shell":"zsh"}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert db.get_vm("request-fail") is None
    assert db.instance_state.get_desired_overlay("vm", "request-fail") is None
    assert outcomes == []


def test_create_vm_retains_platform_metadata_when_rollback_is_unconfirmed(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import ProvisioningStatus

    failure = RetainedProvisioningError(
        "backend cleanup was not confirmed",
        platform_metadata={"instance_name": "retained-backend", "create_incomplete": "true"},
        entity_name="retained",
        hint="delete the retained VM",
    )
    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(RetainedProvisioningError) as caught:
        vm_manager.create_vm(
            db,
            make_config(),
            name="retained",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert caught.value is failure
    row = db.get_vm("retained")
    assert row is not None
    assert row.platform_metadata == {"instance_name": "retained-backend", "create_incomplete": "true"}
    assert row.provisioning_status == ProvisioningStatus.FAILED.value
    # A retained row keeps its store file, which delete removes with it.
    assert trust.store_file("retained").exists()
    assert any(
        "retained-" in message and message.endswith("-vm-create.log")
        for _role, _level, message in captured_output.lines
    )


def test_create_vm_metadata_failure_retains_owner_and_reports_once(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.instance_specs import OverlayDisposition, OverlayOutcome

    config = make_config()
    outcomes: list[OverlayOutcome] = []
    monkeypatch.setattr("agentworks.instance_specs.render_overlay_outcome", outcomes.append)
    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *args, **kwargs: ProvisionResult(
            platform_metadata={"instance_name": "metadata-fail"},
            tailnet_address="100.64.0.7",
        ),
    )
    monkeypatch.setattr(
        db,
        "update_vm_platform_metadata",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("metadata write failed")),
    )

    with pytest.raises(RuntimeError, match="metadata write failed"):
        vm_manager.create_vm(
            db,
            config,
            name="metadata-fail",
            spec='{"cpus":6}',
            admin_spec='{"shell":"zsh"}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert db.get_vm("metadata-fail") is not None
    assert db.instance_state.get_desired_overlay("vm", "metadata-fail") is not None
    assert [(outcome.disposition, outcome.fields) for outcome in outcomes] == [
        (OverlayDisposition.SET, ("admin.shell", "vm.cpus"))
    ]


@pytest.mark.parametrize(
    "failure",
    [
        ConfigError("site has no Trixie template", hint="set template_vmids.trixie"),
        StateError("plugin has no Trixie selector", hint="update the plugin"),
        AuthorizationError("provider denied create", hint="grant the required action"),
        TokenRejectedError("provider rejected the credential", hint="check the credential"),
    ],
    ids=("operator-map", "code-map", "authorization", "credential"),
)
def test_create_vm_preserves_typed_platform_failure(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
    failure: ConfigError | StateError | AuthorizationError | TokenRejectedError,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(type(failure)) as caught:
        vm_manager.create_vm(
            db,
            make_config(),
            name="map-fail",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert caught.value is failure
    assert db.get_vm("map-fail") is None
    # The store file create reset for the name goes with the row.
    assert not trust.store_file("map-fail").exists()


@pytest.mark.parametrize(
    "failure",
    [ConnectivityError("node did not report its address", hint="log in again"), RuntimeError("no hint here")],
    ids=("agentworks-error", "other"),
)
def test_create_vm_wraps_an_untyped_platform_failure_with_its_hint(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
    failure: Exception,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    monkeypatch.setattr(LimaPlatform, "create", lambda *args, **kwargs: (_ for _ in ()).throw(failure))

    with pytest.raises(ProvisioningError) as caught:
        vm_manager.create_vm(db, make_config(), name="wrap-fail", interaction=TtyInteractionPolicy.REFUSE)

    assert caught.value.__cause__ is failure
    assert caught.value.hint == getattr(failure, "hint", None)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(StateError("guest release mismatch"), id="mismatch"),
        pytest.param(RuntimeError("private-transport-sentinel"), id="phase-a-failure"),
        pytest.param(KeyboardInterrupt("stop"), id="interrupt"),
        pytest.param(UserAbort("stop"), id="user-abort"),
    ],
)
def test_create_vm_attests_the_core_release_and_keeps_an_unattested_vm_gated(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
    failure: BaseException,
) -> None:
    """Phase A attests the release core asked for, whatever the platform did to the request, and a VM
    whose Phase A did not complete is kept for delete, refused by every ordinary command, with no release recorded."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import InitStatus, ProvisioningStatus

    def _create_with_mutated_request(
        _self: LimaPlatform,
        request: ProvisionRequest,
        _ctx: object,
    ) -> ProvisionResult:
        request.debian_release = DebianRelease.BOOKWORM
        return ProvisionResult(
            platform_metadata={"instance_name": "unverified"},
            tailnet_address="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _create_with_mutated_request)

    def _failing_phase_a(*args: object, expected_release: DebianRelease, **kwargs: object) -> None:
        assert expected_release is DebianRelease.TRIXIE
        raise failure

    monkeypatch.setattr(vm_manager, "bootstrap_vm", _failing_phase_a)
    monkeypatch.setattr(
        vm_manager, "run_initialization", lambda *a, **k: pytest.fail("Phase B reached after Phase A failed")
    )

    with pytest.raises((ProvisioningError, KeyboardInterrupt, UserAbort)) as caught:
        vm_manager.create_vm(db, make_config(), name="unverified", interaction=TtyInteractionPolicy.REFUSE)

    if isinstance(failure, Exception) and not isinstance(failure, UserAbort):
        assert isinstance(caught.value, ProvisioningError)
        assert caught.value.__cause__ is failure
    else:
        assert caught.value is failure
    row = db.get_vm("unverified")
    assert row is not None
    assert row.platform_metadata == {"instance_name": "unverified"}
    assert row.provisioning_status != ProvisioningStatus.COMPLETE.value
    assert row.init_status == InitStatus.PENDING.value
    assert row.debian_release is None
    with pytest.raises(StateError):
        vm_manager._guard_incomplete_vm(row)


def test_an_interrupt_keeps_its_place_when_the_cancel_handler_cannot_read_the_row(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """The cancel handler reads the row to choose its guidance; a failed read gives the delete guidance."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.vms.manager import lifecycle

    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *_a, **_k: ProvisionResult(platform_metadata={"instance_name": "box"}, tailnet_address="100.64.0.7"),
    )
    interrupt = KeyboardInterrupt("stop")
    read = db.get_vm
    broken = False

    def _get_vm(name: str) -> object:
        if broken:
            raise sqlite3.OperationalError("database is locked")
        return read(name)

    def _interrupted_phase_a(*_args: object, **_kwargs: object) -> None:
        nonlocal broken
        broken = True
        raise interrupt

    monkeypatch.setattr(db, "get_vm", _get_vm)
    monkeypatch.setattr(vm_manager, "bootstrap_vm", _interrupted_phase_a)
    monkeypatch.setattr(lifecycle, "_warn_init_cancel", lambda *_a: pytest.fail("reinit guidance for an unread row"))

    with pytest.raises(KeyboardInterrupt) as caught:
        vm_manager.create_vm(db, make_config(), name="box", interaction=TtyInteractionPolicy.REFUSE)

    assert caught.value is interrupt
    assert len(captured_output.warnings) == 1


def test_create_vm_logger_close_failure_retains_owner_and_reports_once(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.instance_specs import OverlayDisposition, OverlayOutcome
    from agentworks.ssh import SSHLogger

    config = make_config()
    outcomes: list[OverlayOutcome] = []
    monkeypatch.setattr("agentworks.instance_specs.render_overlay_outcome", outcomes.append)
    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *args, **kwargs: ProvisionResult(
            platform_metadata={"instance_name": "close-fail"},
            tailnet_address="100.64.0.7",
        ),
    )
    monkeypatch.setattr(
        vm_manager,
        "bootstrap_vm",
        lambda *args, **kwargs: (SimpleNamespace(), "/home/agentworks", DebianRelease.TRIXIE),
    )
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *args, **kwargs: None)
    monkeypatch.setattr(SSHLogger, "close", lambda self: (_ for _ in ()).throw(RuntimeError("close failed")))

    with pytest.raises(RuntimeError, match="close failed"):
        vm_manager.create_vm(
            db,
            config,
            name="close-fail",
            spec='{"cpus":6}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert db.get_vm("close-fail") is not None
    assert db.instance_state.get_desired_overlay("vm", "close-fail") is not None
    assert [(outcome.disposition, outcome.fields) for outcome in outcomes] == [(OverlayDisposition.SET, ("vm.cpus",))]


def test_create_vm_stores_and_provisions_selected_admin_template(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_output: object,
) -> None:
    """An admin final layer composes with an explicit non-default template."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "admin.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: admin-template
        metadata:
          name: work
        spec:
          username: worker
        """)
    )
    config = make_config()
    captured: list[ProvisionRequest] = []

    def _fake_create(self: LimaPlatform, request: ProvisionRequest, ctx: object) -> ProvisionResult:
        captured.append(request)
        return ProvisionResult(
            platform_metadata={"instance_name": "wvm"},
            tailnet_address="100.64.0.9",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    # Phase A / Phase B are faked here: this suite pins the create()
    # request shape and the persisted row, not the init sequence.
    monkeypatch.setattr(
        vm_manager,
        "bootstrap_vm",
        lambda *a, **k: (SimpleNamespace(), "/home/agentworks", DebianRelease.TRIXIE),
    )
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *a, **k: None)

    vm_manager.create_vm(
        db,
        config,
        name="wvm",
        admin_template="work",
        admin_spec='{"username":"instance-worker"}',
        interaction=TtyInteractionPolicy.REFUSE,
    )

    (request,) = captured
    assert request.admin_username == "instance-worker"
    vm = db.get_vm("wvm")
    assert vm is not None
    assert vm.admin_template == "work"
    assert vm.admin_username == "instance-worker"


@pytest.mark.parametrize("admin_template", ["", "ghost"])
def test_unknown_admin_template_errors_before_any_work(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
    admin_template: str,
) -> None:
    """An undeclared ``--admin-template`` name fails with the typed
    unknown-template error before any slug prompt, secret resolve, DB row,
    or platform create() runs."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config()

    def _no_create(self: LimaPlatform, request: object, ctx: object) -> object:
        raise AssertionError("provisioning reached for an unknown admin-template")

    def _no_slug(db_: object) -> None:
        raise AssertionError("slug prompt reached for an unknown admin-template")

    monkeypatch.setattr(LimaPlatform, "create", _no_create)
    monkeypatch.setattr(vm_manager, "_resolve_system_slug", _no_slug)

    with pytest.raises(NotFoundError) as exc:
        vm_manager.create_vm(
            db,
            config,
            name="nvm",
            admin_template=admin_template,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    assert exc.value.entity_kind == "admin-template"
    assert exc.value.entity_name == admin_template
    assert db.get_vm("nvm") is None


def test_admin_spec_unknown_reference_errors_before_lifecycle(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: pytest.fail("lifecycle reached"))

    with pytest.raises(ConfigError):
        vm_manager.create_vm(
            db,
            make_config(),
            name="unknown-admin-ref",
            admin_spec='{"user_install_commands":["missing-command"]}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert db.get_vm("unknown-admin-ref") is None


def test_admin_spec_disabled_reference_errors_before_lifecycle(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: pytest.fail("lifecycle reached"))

    with pytest.raises(StateError) as caught:
        vm_manager.create_vm(
            db,
            make_config(),
            name="disabled-admin-ref",
            admin_spec='{"user_install_commands":["claude"]}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert caught.value.entity_kind == "user-install-command"
    assert caught.value.entity_name == "claude"
    assert db.get_vm("disabled-admin-ref") is None


def test_not_ready_site_errors_before_tailscale_and_slug_prompt(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """An explicit --site naming a not-ready site errors UP FRONT: the
    operator never answers the system-slug prompt (and no Tailscale
    probe runs) for an op the site already sank, the same
    no-work-before-the-fatal-check discipline as the preflight
    boundary, one tier earlier."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.errors import StateError

    config = make_config()
    from agentworks.resources.graph import Readiness

    monkeypatch.setattr(
        LimaPlatform, "not_ready", classmethod(lambda cls, config: Readiness.blocked("limactl not installed"))
    )

    def _no_tailscale() -> None:
        raise AssertionError("tailscale probed for a not-ready site")

    def _no_slug(db_: object) -> None:
        raise AssertionError("slug prompt reached for a not-ready site")

    monkeypatch.setattr(vm_manager, "verify_tailscale_available", _no_tailscale)
    monkeypatch.setattr(vm_manager, "_resolve_system_slug", _no_slug)

    with pytest.raises(StateError, match="not ready on this host") as exc:
        vm_manager.create_vm(db, config, name="dvm", site="lima-local", interaction=TtyInteractionPolicy.REFUSE)
    assert "limactl" in str(exc.value)


def test_create_vm_composes_r11_hostname_with_slug(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """With a slug set, the hostname is {slug}-{name} and the slug
    rides the ProvisionRequest (no first-create prompt fires: the
    settings row exists)."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config()
    db.set_setting("system_slug", "team-a")
    captured: list[ProvisionRequest] = []

    def _fake_create(self: LimaPlatform, request: ProvisionRequest, ctx: object) -> ProvisionResult:
        captured.append(request)
        return ProvisionResult(
            platform_metadata={"instance_name": "team-a-svm"},
            tailnet_address="100.64.0.8",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    # Phase A / Phase B are faked here: this suite pins the create()
    # request shape and the persisted row, not the init sequence.
    monkeypatch.setattr(
        vm_manager,
        "bootstrap_vm",
        lambda *a, **k: (SimpleNamespace(), "/home/agentworks", DebianRelease.TRIXIE),
    )
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *a, **k: None)

    vm_manager.create_vm(db, config, name="svm", interaction=TtyInteractionPolicy.REFUSE)

    (request,) = captured
    assert request.hostname == "team-a-svm"
    assert request.system_slug == "team-a"
    vm = db.get_vm("svm")
    assert vm is not None
    assert vm.hostname == "team-a-svm"


def test_slug_resolution_precedes_secrets_and_insert(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """Ordering: the slug prompt runs before the boundary resolve pass
    and before the DB row exists, so an aborted slug entry leaves
    nothing behind."""
    from agentworks.secrets.resolver import Resolver

    order: list[str] = []

    def _slug_spy(db_: object) -> None:
        order.append("slug")
        return None

    class _Stop(Exception):
        pass

    def _resolve_spy(self: Resolver) -> None:
        order.append("secrets")
        raise _Stop

    monkeypatch.setattr(vm_manager, "_resolve_system_slug", _slug_spy)
    monkeypatch.setattr(Resolver, "resolve", _resolve_spy)

    with pytest.raises(_Stop):
        vm_manager.create_vm(db, make_config(), name="ovm", interaction=TtyInteractionPolicy.REFUSE)

    assert order == ["slug", "secrets"]
    assert db.get_vm("ovm") is None  # insert happens after the resolve


@pytest.mark.parametrize(
    "auth_key", ["tskey-prefix\r\ntskey-suffix\r\n", "", " \t "], ids=["multiline", "empty", "whitespace"]
)
def test_create_rejects_an_unusable_tailscale_key_before_runup_db_or_platform(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
    auth_key: str,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", auth_key)
    monkeypatch.setattr(
        LimaPlatform,
        "runup",
        lambda *args, **kwargs: pytest.fail("platform runup reached with a line-unsafe Tailscale key"),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *args, **kwargs: pytest.fail("platform create reached with a line-unsafe Tailscale key"),
    )

    with pytest.raises(ValidationError) as caught:
        vm_manager.create_vm(db, make_config(), name="unsafe-ts", interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_vm("unsafe-ts") is None
    assert not auth_key.strip() or auth_key not in repr((caught.value.args, vars(caught.value)))


def test_r11_hostname_and_vnet_bound_by_construction() -> None:
    """The VM-name cap is the MIN over two composed sinks. At slug max 20 and
    name max 38 (MAX_VM_NAME_LENGTH): the {slug}-{name} hostname is 59 chars
    (inside the 63-char DNS-label limit), and the tighter {slug}-{name}-vnet
    Azure virtual-network name is exactly 64 (its cap). The vnet sink is what
    binds the cap at 38."""
    from agentworks.naming import AZURE_VNET_NAME_MAX_LENGTH, DNS_LABEL_MAX_LENGTH, MAX_VM_NAME_LENGTH, validate_name
    from agentworks.plugins.azure.network import VNET_NAME_SUFFIX

    slug = "a" * 20
    vm_manager.validate_slug(slug)
    name = "b" * MAX_VM_NAME_LENGTH
    validate_name(name, max_length=MAX_VM_NAME_LENGTH)
    hostname = f"{slug}-{name}"
    assert len(hostname) == 59
    assert len(hostname) <= DNS_LABEL_MAX_LENGTH == 63
    vnet_name = f"{slug}-{name}{VNET_NAME_SUFFIX}"
    assert len(vnet_name) == AZURE_VNET_NAME_MAX_LENGTH == 64


def test_proxmox_token_resolves_end_to_end(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """The site's token secret joins create_vm's single boundary resolve
    pass (env-var backend under the AW_SECRET_ convention) and ops read
    it from the resolver's cache; there is no raw
    PROXMOX_TOKEN_SECRET env fallback."""
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    config = make_config(PLUGINS_ENABLED, manifests=[proxmox_site()])
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token-value")
    # The deleted legacy shadow path: setting the OLD raw variable to a
    # different value proves nothing reads it.
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "must-not-be-read")

    captured: dict[str, object] = {}

    def _fake_create(self: ProxmoxPlatform, request: object, ctx: RunContext) -> ProvisionResult:
        captured["token"] = ctx.secret("proxmox-token")
        raise RuntimeError("halt after binding")

    monkeypatch.setattr(ProxmoxPlatform, "create", _fake_create)

    with pytest.raises(ProvisioningError, match="halt after binding"):
        vm_manager.create_vm(db, config, name="pvm", site="proxmox", interaction=TtyInteractionPolicy.REFUSE)

    assert captured["token"] == "pve-token-value"
    # Rollback removed the row after the failed provisioning.
    assert db.get_vm("pvm") is None


def test_proxmox_missing_create_release_fails_before_resolve_and_runup(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    resolve_counter: list[list[str]],
) -> None:
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    site = proxmox_site()
    platform_config = site.spec["platform"]
    assert isinstance(platform_config, dict)
    platform_config.pop("template_vmids")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    config = make_config(PLUGINS_ENABLED, manifests=[site])

    monkeypatch.setattr(
        ProxmoxPlatform,
        "runup",
        lambda *args, **kwargs: pytest.fail("runup reached with a missing create release"),
    )
    monkeypatch.setattr(
        ProxmoxPlatform,
        "create",
        lambda *args, **kwargs: pytest.fail("create reached with a missing create release"),
    )

    with pytest.raises(ConfigError) as caught:
        vm_manager.create_vm(
            db,
            config,
            name="missing-release",
            site="proxmox",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert caught.value.entity_kind == "vm-site"
    assert caught.value.entity_name == "proxmox"
    assert resolve_counter == []
    assert db.get_vm("missing-release") is None


def test_an_unsupported_client_refuses_create_before_any_platform_secret_or_row(
    db: Database, make_config, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.execution.errors import ClientUnsupported
    from agentworks.secrets.resolver import Resolver

    def _unsupported() -> object:
        raise ClientUnsupported("no supported OpenSSH client")

    monkeypatch.setattr("agentworks.execution.resolve_ssh", _unsupported)
    for method in ("runup", "create"):
        monkeypatch.setattr(LimaPlatform, method, lambda *a, **k: pytest.fail("the platform was reached"))
    monkeypatch.setattr(Resolver, "resolve", lambda self: pytest.fail("a secret was resolved"))

    with pytest.raises(ClientUnsupported):
        vm_manager.create_vm(db, make_config(), name="noclient", interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_vm("noclient") is None

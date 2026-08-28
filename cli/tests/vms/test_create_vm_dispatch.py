"""``create_vm`` through the composition root: the ProvisionRequest
shape handed to the bound platform, the persisted row, and the proxmox
config-secret resolve pass end to end (no env-read shadow path).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionResult
from agentworks.config import load_config
from agentworks.errors import ConfigError, NotFoundError, ProvisioningError, StateError, ValidationError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests.conftest import ManifestDoc, write_manifests
from tests.orchestrated_fixtures import proxmox_site

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
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public ssh key")
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-test")
    # Deterministic platform preflights: lima checks for limactl
    # locally; pretend the tool exists regardless of the host.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _make(extra: str = "", *, manifests: Sequence[ManifestDoc | str] = ()):
        path = tmp_path / "config.toml"
        path.write_text(f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n' + extra)
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
    from agentworks.capabilities.vm_platform import ProvisionRequest
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.instance_specs import OverlayDisposition, OverlayOutcome

    config = make_config(manifests=[ManifestDoc("vm-template", "default", {"cpus": 2})])
    captured_request: list[ProvisionRequest] = []
    captured_platform: list[LimaPlatform] = []
    outcomes: list[OverlayOutcome] = []
    monkeypatch.setattr("agentworks.instance_specs.render_overlay_outcome", outcomes.append)

    def _fake_create(self: LimaPlatform, request: ProvisionRequest, ctx: object) -> ProvisionResult:
        captured_platform.append(self)
        captured_request.append(request)
        return ProvisionResult(
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={"instance_name": "dvm"},
            tailscale_ip="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    # Phase A / Phase B are faked here: this suite pins the create()
    # request shape and the persisted row, not the init sequence.
    monkeypatch.setattr(
        vm_manager,
        "bootstrap_vm",
        lambda *a, **k: (SimpleNamespace(), "/home/agentworks"),
    )
    monkeypatch.setattr(vm_manager, "run_initialization", lambda *a, **k: None)

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
    assert request.hostname == "dvm"  # no slug: the bare name
    assert request.system_slug is None
    assert request.cpus == 6
    assert request.admin_username == "operator"
    assert request.ssh_public_key == "public ssh key"
    (bound,) = captured_platform
    assert bound.site_name == "lima-local"

    vm = db.get_vm("dvm")
    assert vm is not None
    assert vm.site == "lima-local"
    assert vm.hostname == "dvm"
    assert vm.platform_metadata == {"instance_name": "dvm"}
    assert vm.operator_stopped is False
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

    with pytest.raises(FileNotFoundError):
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
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={"instance_name": "metadata-fail"},
            tailscale_ip="100.64.0.7",
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
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={"instance_name": "close-fail"},
            tailscale_ip="100.64.0.7",
        ),
    )
    monkeypatch.setattr(
        vm_manager,
        "bootstrap_vm",
        lambda *args, **kwargs: (SimpleNamespace(), "/home/agentworks"),
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
    from agentworks.capabilities.vm_platform import ProvisionRequest
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
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={"instance_name": "wvm"},
            tailscale_ip="100.64.0.9",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    # Phase A / Phase B are faked here: this suite pins the create()
    # request shape and the persisted row, not the init sequence.
    monkeypatch.setattr(
        vm_manager,
        "bootstrap_vm",
        lambda *a, **k: (SimpleNamespace(), "/home/agentworks"),
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
    from agentworks.capabilities.vm_platform import ProvisionRequest
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config()
    db.set_setting("system_slug", "team-a")
    captured: list[ProvisionRequest] = []

    def _fake_create(self: LimaPlatform, request: ProvisionRequest, ctx: object) -> ProvisionResult:
        captured.append(request)
        return ProvisionResult(
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={"instance_name": "team-a-svm"},
            tailscale_ip="100.64.0.8",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    # Phase A / Phase B are faked here: this suite pins the create()
    # request shape and the persisted row, not the init sequence.
    monkeypatch.setattr(
        vm_manager,
        "bootstrap_vm",
        lambda *a, **k: (SimpleNamespace(), "/home/agentworks"),
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


def test_create_rejects_multiline_tailscale_key_before_runup_db_or_platform(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    auth_key = "tskey-prefix\r\ntskey-suffix\r\n"
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
    assert auth_key not in repr((caught.value.args, vars(caught.value)))


def test_create_rejects_multiline_git_token_before_runup_db_or_platform(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    token = "ghp-prefix\nghp-suffix\n"
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GITHUB", token)
    config = make_config(
        manifests=[
            ManifestDoc(
                "admin-template",
                "default",
                {"shell": "zsh", "git_credentials": ["github"]},
            ),
            ManifestDoc("git-credential", "github", {"provider": {"name": "github"}}),
        ]
    )
    monkeypatch.setattr(
        LimaPlatform,
        "runup",
        lambda *args, **kwargs: pytest.fail("platform runup reached with a line-unsafe Git token"),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *args, **kwargs: pytest.fail("platform create reached with a line-unsafe Git token"),
    )

    with pytest.raises(ValidationError) as caught:
        vm_manager.create_vm(db, config, name="unsafe-git", interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_vm("unsafe-git") is None
    assert token not in repr((caught.value.args, vars(caught.value)))


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

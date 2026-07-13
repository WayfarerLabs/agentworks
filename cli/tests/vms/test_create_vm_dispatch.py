"""``create_vm`` through the composition root: the ProvisionRequest
shape handed to the bound platform, the persisted row, and the proxmox
config-secret resolve pass end to end (no env-read shadow path).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform import ProvisionResult
from agentworks.config import load_config
from agentworks.errors import ProvisioningError
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.db import Database

PROXMOX_SECTION = """
[proxmox]
api_url = "https://pve:8006"
node = "pve1"
token_id = "agw@pam!agw"
template_vmid = 9000
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


def test_create_vm_request_shape_and_row(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """The bound lima platform receives the provision request (bare-name
    hostname, null slug pre-Phase-4) and the returned platform_metadata
    persists verbatim."""
    from agentworks.capabilities.vm_platform import ProvisionRequest
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    config = make_config()
    captured_request: list[ProvisionRequest] = []
    captured_platform: list[LimaPlatform] = []

    def _fake_create(self: LimaPlatform, request: ProvisionRequest) -> ProvisionResult:
        captured_platform.append(self)
        captured_request.append(request)
        return ProvisionResult(
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={"instance_name": "dvm"},
            bootstrap_complete=True,
            tailscale_ip="100.64.0.7",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(vm_manager, "initialize_vm", lambda *a, **k: None)

    vm_manager.create_vm(db, config, name="dvm", cpus=2)

    (request,) = captured_request
    assert request.vm_name == "dvm"
    assert request.hostname == "dvm"  # no slug: the bare name
    assert request.system_slug is None
    assert request.cpus == 2
    assert request.ssh_public_key == "public ssh key"
    (bound,) = captured_platform
    assert bound.site_name == "lima-local"

    vm = db.get_vm("dvm")
    assert vm is not None
    assert vm.site == "lima-local"
    assert vm.hostname == "dvm"
    assert vm.platform_metadata == {"instance_name": "dvm"}
    assert vm.operator_stopped is False


def test_disabled_site_errors_before_tailscale_and_slug_prompt(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """An explicit --site naming a disabled site errors UP FRONT: the
    operator never answers the system-slug prompt (and no Tailscale
    probe runs) for an op the site already sank, the same
    no-work-before-the-fatal-check discipline as the preflight
    boundary, one tier earlier."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.errors import StateError

    config = make_config()
    monkeypatch.setattr(
        LimaPlatform, "disabled_reason", lambda self: "limactl not installed"
    )

    def _no_tailscale() -> None:
        raise AssertionError("tailscale probed for a disabled site")

    def _no_slug(db_: object) -> None:
        raise AssertionError("slug prompt reached for a disabled site")

    monkeypatch.setattr(vm_manager, "verify_tailscale_available", _no_tailscale)
    monkeypatch.setattr(vm_manager, "_resolve_system_slug", _no_slug)

    with pytest.raises(StateError, match="disabled on this host") as exc:
        vm_manager.create_vm(db, config, name="dvm", site="lima-local")
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

    def _fake_create(self: LimaPlatform, request: ProvisionRequest) -> ProvisionResult:
        captured.append(request)
        return ProvisionResult(
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={"instance_name": "team-a-svm"},
            bootstrap_complete=True,
            tailscale_ip="100.64.0.8",
        )

    monkeypatch.setattr(LimaPlatform, "create", _fake_create)
    monkeypatch.setattr(vm_manager, "initialize_vm", lambda *a, **k: None)

    vm_manager.create_vm(db, config, name="svm")

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
        vm_manager.create_vm(db, make_config(), name="ovm")

    assert order == ["slug", "secrets"]
    assert db.get_vm("ovm") is None  # insert happens after the resolve


def test_r11_hostname_bound_by_construction() -> None:
    """Slug max 20 + dash + name max 30 = 51 chars, inside the 63-char
    hostname-label and Azure 64-char computer-name limits."""
    from agentworks.config import MAX_NAME_LENGTH, validate_name

    slug = "a" * 20
    vm_manager.validate_slug(slug)
    name = "b" * MAX_NAME_LENGTH
    validate_name(name)
    hostname = f"{slug}-{name}"
    assert len(hostname) == 51
    assert len(hostname) <= 63


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
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    config = make_config(PROXMOX_SECTION)
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token-value")
    # The deleted legacy shadow path: setting the OLD raw variable to a
    # different value proves nothing reads it.
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "must-not-be-read")

    captured: dict[str, object] = {}

    def _fake_create(self: ProxmoxPlatform, request: object) -> ProvisionResult:
        assert self.resolver is not None
        captured["token"] = self.resolver.get("proxmox-token")
        raise RuntimeError("halt after binding")

    monkeypatch.setattr(ProxmoxPlatform, "create", _fake_create)

    with pytest.raises(ProvisioningError, match="halt after binding"):
        vm_manager.create_vm(db, config, name="pvm", site="proxmox")

    assert captured["token"] == "pve-token-value"
    # Rollback removed the row after the failed provisioning.
    assert db.get_vm("pvm") is None

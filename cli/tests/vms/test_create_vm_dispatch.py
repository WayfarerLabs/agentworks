"""``create_vm`` through the composition root: the ProvisionRequest
shape handed to the bound platform, the persisted row, and the proxmox
config-secret resolve pass end to end (no env-read shadow path).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.errors import ProvisioningError
from agentworks.vms import manager as vm_manager
from agentworks.vms.base import ProvisionResult

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
    """The bound lima platform receives the R11 request (bare-name
    hostname, null slug pre-Phase-4) and the returned platform_metadata
    persists verbatim."""
    from agentworks.vms.base import ProvisionRequest
    from agentworks.vms.platforms.lima import LimaPlatform

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
    assert request.hostname == "dvm"  # R11 with no slug: the bare name
    assert request.system_slug is None
    assert request.cpus == 2
    assert request.ssh_public_key == "public ssh key"
    (bound,) = captured_platform
    assert bound.site_name == "lima"

    vm = db.get_vm("dvm")
    assert vm is not None
    assert vm.site == "lima"
    assert vm.hostname == "dvm"
    assert vm.platform_metadata == {"instance_name": "dvm"}
    assert vm.operator_stopped is False


def test_create_vm_composes_r11_hostname_with_slug(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """With a slug set, the hostname is {slug}-{name} and the slug
    rides the ProvisionRequest (no first-create prompt fires: the
    settings row exists)."""
    from agentworks.vms.base import ProvisionRequest
    from agentworks.vms.platforms.lima import LimaPlatform

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
    """R4 ordering: the slug prompt runs before the secret resolve pass
    and before the DB row exists, so an aborted slug entry leaves
    nothing behind."""
    order: list[str] = []

    def _slug_spy(db_: object) -> tuple[None, bool]:
        order.append("slug")
        return None, False

    class _Stop(Exception):
        pass

    def _secrets_spy(*a: object, **k: object) -> tuple[str, dict, dict]:
        order.append("secrets")
        raise _Stop

    monkeypatch.setattr(vm_manager, "_resolve_system_slug", _slug_spy)
    monkeypatch.setattr(vm_manager, "_collect_secrets", _secrets_spy)

    with pytest.raises(_Stop):
        vm_manager.create_vm(db, make_config(), name="ovm")

    assert order == ["slug", "secrets"]
    assert db.get_vm("ovm") is None  # insert happens after the resolve


def test_nudge_skipped_when_prompt_just_declined(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """First-ever create on a shared-backend site: declining the full
    prompt must not trigger the nudge in the same create."""
    monkeypatch.setattr(
        vm_manager, "_resolve_system_slug", lambda db_: (None, True)
    )
    monkeypatch.setattr(
        vm_manager,
        "_nudge_shared_backend_slug",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("nudge must not fire in the same create")
        ),
    )

    class _Stop(Exception):
        pass

    monkeypatch.setattr(
        vm_manager, "_collect_secrets",
        lambda *a, **k: (_ for _ in ()).throw(_Stop()),
    )

    with pytest.raises(_Stop):
        vm_manager.create_vm(db, make_config(), name="nvm")


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
    """The site's token secret joins create_vm's single resolve pass
    (env-var backend under the AW_SECRET_ convention) and reaches the
    bound platform via secret_values -- there is no raw
    PROXMOX_TOKEN_SECRET env fallback."""
    from agentworks.vms.platforms.proxmox import ProxmoxPlatform

    config = make_config(PROXMOX_SECTION)
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN_SECRET", "pve-token-value")
    # The deleted legacy shadow path: setting the OLD raw variable to a
    # different value proves nothing reads it.
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "must-not-be-read")

    captured: dict[str, object] = {}

    def _fake_create(self: ProxmoxPlatform, request: object) -> ProvisionResult:
        captured["secret_values"] = dict(self.secret_values)
        captured["token"] = self.secret_values.get("proxmox-token-secret")
        raise RuntimeError("halt after binding")

    monkeypatch.setattr(ProxmoxPlatform, "create", _fake_create)

    with pytest.raises(ProvisioningError, match="halt after binding"):
        vm_manager.create_vm(db, config, name="pvm", platform="proxmox")

    assert captured["token"] == "pve-token-value"
    # Rollback removed the row after the failed provisioning.
    assert db.get_vm("pvm") is None

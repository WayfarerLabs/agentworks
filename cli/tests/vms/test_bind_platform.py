"""``bind_platform`` / ``bind_platforms``: the composition-root helper's
resolve-pass discipline (one pass per site, none without site secrets,
prompt-once by construction) and the lazy registry build.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.config import load_config
from agentworks.vms import manager as vm_manager

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
    (tmp_path / "id_ed25519.pub").write_text("public")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN_SECRET", "pve-token")

    def _make(extra: str = ""):
        path = tmp_path / "config.toml"
        path.write_text(
            f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
            + extra
        )
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


@pytest.fixture
def resolve_counter(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Count resolve passes through the real resolver."""
    from agentworks.secrets import orchestration

    calls: list[object] = []
    real = orchestration.resolve_for_command

    def _counting(*args: object, **kwargs: object) -> dict[str, str]:
        calls.append(kwargs.get("extra_decls"))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestration, "resolve_for_command", _counting)
    return calls


def _vm(name: str, site: str) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(name=name, site=site)


def test_no_site_secrets_skips_the_resolve_pass(
    make_config, resolve_counter: list[object]
) -> None:
    config = make_config()
    platform = vm_manager.bind_platform(config, _vm("v1", "lima"))  # type: ignore[arg-type]
    assert platform.name == "lima"
    assert resolve_counter == []


def test_secret_bearing_site_resolves_exactly_once(
    make_config, resolve_counter: list[object]
) -> None:
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    config = make_config(PROXMOX_SECTION)
    platform = vm_manager.bind_platform(config, _vm("v1", "proxmox"))  # type: ignore[arg-type]
    assert isinstance(platform, ProxmoxPlatform)
    assert platform.secret_values.get("proxmox-token-secret") == "pve-token"
    assert len(resolve_counter) == 1


def test_bind_platforms_one_resolve_and_one_instance_per_site(
    make_config, resolve_counter: list[object]
) -> None:
    """Two VMs at the same secret-bearing site share one bound platform
    and one resolve pass (prompt-once across a batch command)."""
    config = make_config(PROXMOX_SECTION)
    vms = [_vm("v1", "proxmox"), _vm("v2", "proxmox"), _vm("v1", "proxmox")]
    pairs = vm_manager.bind_platforms(config, vms)  # type: ignore[arg-type]

    assert [vm.name for vm, _ in pairs] == ["v1", "v2"]  # name dedup
    assert pairs[0][1] is pairs[1][1]  # shared instance per site
    assert len(resolve_counter) == 1


def test_bind_platforms_empty_set_builds_no_registry(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch commands with an empty VM set (e.g. `session stop --all`
    matching nothing) must stay a complete no-op."""
    import agentworks.bootstrap as bootstrap

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("build_registry must not run for an empty VM set")

    monkeypatch.setattr(bootstrap, "build_registry", _boom)
    assert vm_manager.bind_platforms(make_config(), []) == []

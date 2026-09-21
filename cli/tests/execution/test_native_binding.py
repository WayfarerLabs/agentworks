"""Passive platform composition of independent native carriers."""

from __future__ import annotations

import builtins
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import VMRow
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carriers.proxmox import ProxmoxCarrier
from agentworks.execution.carriers.wsl2 import WSL2Carrier
from agentworks.plugins.proxmox.platform import ProxmoxPlatform

_SECRET = "native-binding-secret-canary"
_PROXMOX_CONFIG: dict[str, object] = {
    "api_url": "https://pve.example:8006",
    "node": "configured-node",
    "token_id": "agentworks@pam!token",
    "token_secret": "native-binding-token",
    "template_vmids": {"trixie": 9001},
}


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.requests: list[str] = []

    def get(self, name: str) -> str:
        self.requests.append(name)
        return self.values[name]


def _vm(*, metadata: dict[str, str], admin_username: str = "operator") -> VMRow:
    return cast(
        "VMRow",
        SimpleNamespace(
            name="vm-one",
            admin_username=admin_username,
            platform_metadata=metadata,
        ),
    )


def _block_legacy_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "agentworks.transports" or name.startswith("agentworks.transports."):
            raise AssertionError(f"native binding imported retired execution: {name}")
        if name == "agentworks.plugins.proxmox.transport":
            raise AssertionError(f"native binding imported retired execution: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.delitem(sys.modules, "agentworks.plugins.proxmox.transport", raising=False)


def test_wsl2_binding_is_passive_and_uses_recorded_distribution_and_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("process launched"))
    _block_legacy_imports(monkeypatch)

    binding = WSL2Platform("wsl2", {}).native_execution_binding(
        _vm(metadata={"distro_name": "recorded-distro"}, admin_username="delivery-user"),
        RunContext(),
    )

    assert isinstance(binding.carrier, WSL2Carrier)
    connection = binding.carrier._connection
    assert (connection.distribution, connection.user, connection.wsl_executable) == (
        "recorded-distro",
        "delivery-user",
        "wsl",
    )
    assert binding.delivery_account == "delivery-user"
    assert binding.runtime_selection == RuntimeSelection(RuntimeTargetOS.LINUX)


@pytest.mark.parametrize(
    ("metadata", "expected_node"),
    [
        ({"vmid": "101", "node": "recorded-node"}, "recorded-node"),
        ({"vmid": "101"}, "configured-node"),
    ],
)
def test_proxmox_binding_uses_scoped_secret_platform_metadata_and_verified_connection(
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, str],
    expected_node: str,
) -> None:
    secrets = _Secrets({"native-binding-token": _SECRET})
    platform = ProxmoxPlatform("pve-site", {**_PROXMOX_CONFIG, "ca_bundle": "/trust/cluster-ca.pem"})
    monkeypatch.setattr(platform, "_api", lambda _ctx: pytest.fail("legacy API constructed"))
    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("process launched"))
    _block_legacy_imports(monkeypatch)

    binding = platform.native_execution_binding(_vm(metadata=metadata), RunContext(secrets=secrets))

    assert secrets.requests == ["native-binding-token"]
    assert isinstance(binding.carrier, ProxmoxCarrier)
    connection = binding.carrier._wire._connection
    assert connection.api_url == "https://pve.example:8006"
    assert connection.node == expected_node
    assert connection.vmid == 101
    assert connection.token_id == "agentworks@pam!token"
    assert connection.token_secret == _SECRET
    assert connection.ca_bundle == Path("/trust/cluster-ca.pem")
    assert binding.delivery_account == "root"
    assert binding.runtime_selection == RuntimeSelection(RuntimeTargetOS.LINUX)


def test_proxmox_binding_uses_system_trust_when_ca_bundle_is_omitted() -> None:
    secrets = _Secrets({"native-binding-token": _SECRET})
    binding = ProxmoxPlatform("pve-site", _PROXMOX_CONFIG).native_execution_binding(
        _vm(metadata={"vmid": "101"}),
        RunContext(secrets=secrets),
    )

    assert cast("ProxmoxCarrier", binding.carrier)._wire._connection.ca_bundle is None


def test_proxmox_binding_rejects_legacy_tls_bypass_before_secret_delivery() -> None:
    secrets = _Secrets({"native-binding-token": _SECRET})
    platform = ProxmoxPlatform("pve-site", {**_PROXMOX_CONFIG, "verify_ssl": False})

    with pytest.raises(ConfigError) as raised:
        platform.native_execution_binding(
            _vm(metadata={"vmid": "101"}),
            RunContext(secrets=secrets),
        )

    assert raised.value.entity_kind == "vm-site"
    assert raised.value.entity_name == "pve-site"
    assert _SECRET not in repr(raised.value)
    assert secrets.requests == []


def test_proxmox_binding_rejects_unsafe_secret_without_retaining_its_value() -> None:
    secret = _SECRET + "\n"
    platform = ProxmoxPlatform("pve-site", _PROXMOX_CONFIG)

    with pytest.raises(ValidationError) as raised:
        platform.native_execution_binding(
            _vm(metadata={"vmid": "101"}),
            RunContext(secrets=_Secrets({"native-binding-token": secret})),
        )

    assert "native-binding-token" in str(raised.value)
    assert secret not in repr(raised.value)


def test_proxmox_api_ca_load_failure_is_scoped_and_secret_free(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = ProxmoxPlatform("pve-site", {**_PROXMOX_CONFIG, "ca_bundle": "/missing/cluster-ca.pem"})
    monkeypatch.setattr(ssl, "create_default_context", MagicMock(side_effect=OSError(_SECRET)))

    with pytest.raises(ConfigError) as raised:
        platform._build_api(_SECRET)

    assert "pve-site" in str(raised.value)
    assert raised.value.hint is not None
    assert "/missing/cluster-ca.pem" in raised.value.hint
    assert _SECRET not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.windows
@pytest.mark.parametrize("path", ["binding", "api"])
def test_proxmox_ca_expansion_failure_is_scoped_without_loading_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    configured = "~nonexistent-user/cluster-ca.pem"
    platform = ProxmoxPlatform("pve-site", {**_PROXMOX_CONFIG, "ca_bundle": configured})
    secrets = _Secrets({"native-binding-token": _SECRET})
    load = MagicMock()
    monkeypatch.setattr(Path, "expanduser", MagicMock(side_effect=RuntimeError(_SECRET)))
    monkeypatch.setattr(ssl, "create_default_context", load)
    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("process launched"))

    with pytest.raises(ConfigError) as raised:
        if path == "binding":
            platform.native_execution_binding(
                _vm(metadata={"vmid": "101"}),
                RunContext(secrets=secrets),
            )
        else:
            platform._build_api(_SECRET)

    assert raised.value.entity_kind == "vm-site"
    assert raised.value.entity_name == "pve-site"
    assert raised.value.hint is not None
    assert configured in raised.value.hint
    assert _SECRET not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    load.assert_not_called()


def test_unimplemented_platform_hook_fails_without_making_old_subclass_abstract() -> None:
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})

    with pytest.raises(StateError) as raised:
        platform.native_execution_binding(_vm(metadata={"instance_name": "vm-one"}), RunContext())

    assert raised.value.entity_kind == "vm-platform"
    assert raised.value.entity_name == "lima"

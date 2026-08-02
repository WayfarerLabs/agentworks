"""``AzureVMPlatform._vm_exists`` is the create-path collision probe, and
it fails CLOSED (#364): only a genuine not-found answers "no VM here".

Before #364 it caught a bare ``Exception`` and returned ``False`` on ANY
error, so an auth blip or a throttled probe was masqueraded as "does not
exist" and the create proceeded, only to fail less cleanly at the first
ARM mutation. These pins hold the narrowed contract: not-found -> False,
any other SDK error -> a wrapped ``AzureError`` surfaced right here, in
line with the fails-closed probes on the other platforms (aws
``_backend_name_in_use``, lima ``_instance_exists``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.plugins.azure.network import AzureError
from agentworks.plugins.azure.platform import AzureVMPlatform


def _compute(get: Any) -> Any:
    """A compute-client stub whose ``virtual_machines.get`` is ``get``."""
    return SimpleNamespace(virtual_machines=SimpleNamespace(get=get))


def test_vm_exists_true_when_the_vm_is_found() -> None:
    compute = _compute(lambda rg, name, **_kw: SimpleNamespace(id="/vm/id"))
    assert AzureVMPlatform._vm_exists(compute, "rg", "vm") is True


def test_vm_exists_false_only_on_a_genuine_not_found() -> None:
    from azure.core.exceptions import ResourceNotFoundError

    def get(_rg: str, _name: str, **_kw: object) -> Any:
        raise ResourceNotFoundError("no such VM")

    assert AzureVMPlatform._vm_exists(_compute(get), "rg", "vm") is False


def test_vm_exists_wraps_and_raises_on_a_non_not_found_error() -> None:
    from azure.core.exceptions import ClientAuthenticationError

    def get(_rg: str, _name: str, **_kw: object) -> Any:
        raise ClientAuthenticationError("token expired")

    # Fails CLOSED: the probe surfaces the real error (wrapped) rather
    # than reporting "does not exist" and letting create march on.
    with pytest.raises(AzureError):
        AzureVMPlatform._vm_exists(_compute(get), "rg", "vm")

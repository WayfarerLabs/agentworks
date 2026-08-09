"""The secret-backend capability has one permanent package address."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    (
        "agentworks.secrets.backends",
        "agentworks.secrets.env_var",
        "agentworks.secrets.prompt",
    ),
)
def test_old_secret_backend_module_addresses_do_not_exist(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


@pytest.mark.parametrize(
    "module",
    (
        "agentworks.capabilities.secret_backend.base",
        "agentworks.capabilities.secret_backend.client",
        "agentworks.capabilities.secret_backend.conformance",
        "agentworks.capabilities.secret_backend.env_var",
        "agentworks.capabilities.secret_backend.kinds",
        "agentworks.capabilities.secret_backend.prompt",
    ),
)
def test_permanent_secret_backend_module_addresses_import(module: str) -> None:
    assert importlib.import_module(module).__name__ == module


def test_the_secrets_package_does_not_alias_capability_ownership() -> None:
    import agentworks.secrets as secrets

    assert not hasattr(secrets, "SECRET_BACKEND_REGISTRY")
    assert not hasattr(secrets, "SecretBackend")
    assert not hasattr(secrets, "EnvVarBackend")
    assert not hasattr(secrets, "PromptBackend")

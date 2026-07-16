"""Source-level tripwires that Phase 1d's ``obtain_token`` removal
stayed removed.

These read the source rather than calling the code; behavior tests
live in ``test_git_credentials_token_resolve.py``. The source-level
checks protect against accidental reintroduction of the env-var
fallback / inline prompt during refactors.
"""

from __future__ import annotations

import inspect
from pathlib import Path


def _read_module_source(name: str) -> str:
    import importlib

    mod = importlib.import_module(name)
    src_path = inspect.getfile(mod)
    return Path(src_path).read_text()


def test_obtain_token_method_gone_from_base() -> None:
    src = _read_module_source("agentworks.capabilities.git_credential.base")
    assert "obtain_token" not in src, (
        "Phase 1d removed obtain_token from GitCredentialProvider; "
        "reintroducing it would bypass the framework's token resolution path"
    )
    assert "env_var_for_credential" not in src, (
        "Phase 1d removed env_var_for_credential; the framework's "
        "AW_SECRET_<NAME> convention applies via the secret kind"
    )
    assert "legacy_env_var_for_credential" not in src
    assert "_prompt_token" not in src


def test_obtain_token_method_gone_from_concrete_providers() -> None:
    for module_name in (
        "agentworks.capabilities.git_credential.github",
        "agentworks.capabilities.git_credential.azdo",
    ):
        src = _read_module_source(module_name)
        assert "obtain_token" not in src, (
            f"Phase 1d removed obtain_token; {module_name} should only "
            f"format credential_lines + pre-flight auth"
        )
        assert "_prompt_token" not in src, (
            f"Phase 1d removed _prompt_token from {module_name}; tokens "
            f"resolve via the framework, not via provider-side prompts"
        )


def test_manager_modules_have_no_obtain_token_calls() -> None:
    """The three manager-side modules that previously called
    ``provider.obtain_token(...)`` (vms/manager.py, agents/manager.py,
    vms/initializer.py) must use the framework instead.
    """
    for module_name in (
        "agentworks.vms.manager",
        "agentworks.agents.manager",
        "agentworks.vms.initializer",
    ):
        src = _read_module_source(module_name)
        assert "obtain_token" not in src, (
            f"found legacy obtain_token call in {module_name}; tokens "
            f"must resolve through the framework's git-token resolve pass"
        )


def test_legacy_env_var_names_gone_from_git_credentials_module() -> None:
    """The legacy AW_GIT_CREDENTIALS_<NAME> / GIT_CREDENTIALS_<NAME>
    env var names are gone from the provider package.
    """
    for module_name in (
        "agentworks.capabilities.git_credential.base",
        "agentworks.capabilities.git_credential.github",
        "agentworks.capabilities.git_credential.azdo",
    ):
        src = _read_module_source(module_name)
        assert "AW_GIT_CREDENTIALS_" not in src
        # GIT_CREDENTIALS_ (the deprecated name) appeared only in helpers
        # we removed.
        assert "GIT_CREDENTIALS_" not in src

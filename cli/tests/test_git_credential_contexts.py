"""Graph derivation and scoped delivery for provider-owned credential inputs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal
from unittest.mock import MagicMock

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.git_credential.base import HttpsCredentialScope, StoredCredential
from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.git_credentials import credential_requests, materialize_credential_state
from agentworks.git_credentials.nodes import git_credential_node
from agentworks.orchestration.secrets import ScopedSecrets, secret_union
from tests.conftest import ManifestDoc, write_manifests
from tests.vm_access_support import Admin, RecordingVMAccess

if TYPE_CHECKING:
    pass


def _config(tmp_path: Path, *documents: ManifestDoc):  # noqa: ANN202
    public = tmp_path / "id.pub"
    private = tmp_path / "id"
    public.write_text("ssh-ed25519 AAAA test")
    private.write_text("private")
    path = tmp_path / "config.toml"
    path.write_text(
        f'[operator]\nssh_public_key = "{public.as_posix()}"\nssh_private_key = "{private.as_posix()}"\n'
        '\n[plugins]\nsystem = ["azure"]\n'
    )
    write_manifests(tmp_path, *documents)
    return load_config(path, warn_issues=False)


def test_secret_source_default_and_named_refs_are_structurally_derived(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        ManifestDoc("git-credential", "default", {"provider": {"name": "github", "source": {"mode": "secret"}}}),
        ManifestDoc(
            "git-credential",
            "named",
            {"provider": {"name": "github", "source": {"mode": "secret", "secret": "custom-input"}}},
        ),
    )
    registry = build_registry(config)
    default = git_credential_node(registry, "default")
    named = git_credential_node(registry, "named")
    assert default.secret_refs() == ("git-token-default",)
    assert named.secret_refs() == ("custom-input",)
    assert secret_union((default, named)) == ("git-token-default", "custom-input")


@pytest.mark.parametrize(
    ("name", "provider"),
    [
        ("github-cli", {"name": "github", "source": {"mode": "gh-cli"}}),
        ("azdo-cli", {"name": "azdo", "org": "acme", "source": {"mode": "az-cli"}}),
    ],
)
def test_cli_sources_declare_no_secret_edge(tmp_path: Path, name: str, provider: dict[str, object]) -> None:
    config = _config(tmp_path, ManifestDoc("git-credential", name, {"provider": provider}))
    registry = build_registry(config)
    node = git_credential_node(registry, name)
    assert node.secret_refs() == ()
    assert secret_union((node,)) == ()


def test_request_context_is_scoped_to_each_provider_declaration(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        ManifestDoc(
            "git-credential",
            "first",
            {
                "provider": {
                    "name": "github",
                    "owner": "first",
                    "source": {"mode": "secret", "secret": "first-input"},
                }
            },
        ),
        ManifestDoc(
            "git-credential",
            "second",
            {
                "provider": {
                    "name": "github",
                    "owner": "second",
                    "source": {"mode": "secret", "secret": "second-input"},
                }
            },
        ),
    )
    registry = build_registry(config)
    nodes = (git_credential_node(registry, "first"), git_credential_node(registry, "second"))
    values = {"first-input": "one", "second-input": "two"}

    contexts: list[RunContext] = []

    def scoped(
        names: tuple[str, ...],
        *,
        admin_access=None,  # noqa: ANN001
        agent_access=None,  # noqa: ANN001
    ) -> RunContext:
        context = RunContext(
            admin_access=admin_access,
            agent_access=agent_access,
            secrets=ScopedSecrets(values, names),
        )
        contexts.append(context)
        return context

    first, second = credential_requests(nodes, scoped)
    assert all(context.admin_access() is None and context.agent_access() is None for context in contexts)
    first_context = first.context()
    second_context = second.context()
    first_material = first.provider.credential_material(first_context)
    second_material = second.provider.credential_material(second_context)
    assert isinstance(first_material, StoredCredential)
    assert isinstance(second_material, StoredCredential)
    assert first_material.password == "one"
    assert second_material.password == "two"
    with pytest.raises(StateError):
        first_context.secret("second-input")
    assert len({id(context) for context in contexts}) == 4
    assert first_context.admin_access() is second_context.admin_access() is None
    assert first_context.agent_access() is second_context.agent_access() is None


@pytest.mark.parametrize("target_role", ["admin", "agent"])
def test_materialization_reassembles_current_target_context(
    target_role: Literal["admin", "agent"],
) -> None:
    provider = MagicMock()
    provider.credential_scopes.return_value = (HttpsCredentialScope("example.com"),)
    provider.credential_material.return_value = StoredCredential("user", "credential")
    node = MagicMock()
    node.name = "synthetic"
    node.provider = provider
    node.secret_refs.return_value = ("input",)
    contexts: list[RunContext] = []
    config = MagicMock()
    scope = MagicMock()
    config.defaults.runup_git_credentials = True

    def scoped(
        names: tuple[str, ...],
        *,
        admin_access=None,  # noqa: ANN001
        agent_access=None,  # noqa: ANN001
    ) -> RunContext:
        context = RunContext(
            config=config,
            operation_scope=scope,
            admin_access=admin_access,
            agent_access=agent_access,
            secrets=ScopedSecrets({"input": "value"}, names),
        )
        contexts.append(context)
        return context

    requests = credential_requests((node,), scoped)
    access = RecordingVMAccess([], Admin())
    state = materialize_credential_state(
        requests,
        access,
        target_role,
        config,
    )

    assert state.has_credentials
    assert len(contexts) == 3
    validation_context, runup_context, material_context = contexts
    assert len({id(context) for context in contexts}) == 3
    assert validation_context.admin_access() is validation_context.agent_access() is None
    assert runup_context.admin_access() is (access if target_role == "admin" else None)
    assert runup_context.agent_access() is (access if target_role == "agent" else None)
    assert material_context.admin_access() is material_context.agent_access() is None
    assert validation_context.config is runup_context.config is material_context.config is config
    assert (
        validation_context.operation_scope is runup_context.operation_scope is material_context.operation_scope is scope
    )
    assert (
        validation_context.secret("input")
        == runup_context.secret("input")
        == material_context.secret("input")
        == "value"
    )
    provider.validate_inputs.assert_called_once_with(validation_context)
    node.runup.assert_called_once_with(runup_context)
    provider.credential_material.assert_called_once_with(material_context)

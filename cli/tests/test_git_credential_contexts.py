"""Graph derivation and scoped delivery for provider-owned credential inputs."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.git_credential.base import StoredCredential
from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.git_credentials import credential_requests
from agentworks.git_credentials.nodes import git_credential_node
from agentworks.orchestration.secrets import ScopedSecrets, secret_union
from tests.conftest import ManifestDoc, write_manifests


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

    def scoped(names: tuple[str, ...]) -> RunContext:
        return RunContext(secrets=ScopedSecrets(values, names))

    first, second = credential_requests(nodes, scoped)
    first_material = first.provider.credential_material(first.context)
    second_material = second.provider.credential_material(second.context)
    assert isinstance(first_material, StoredCredential)
    assert isinstance(second_material, StoredCredential)
    assert first_material.password == "one"
    assert second_material.password == "two"
    with pytest.raises(StateError):
        first.context.secret("second-input")

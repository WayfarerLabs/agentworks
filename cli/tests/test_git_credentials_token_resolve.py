"""Tests for Phase 1d's git-credential token-resolution path.

The framework resolves each git-credential's ``token`` field through
the source chain; the resolved value is written into
``~/.git-credentials`` via ``credential_lines``. No
``AW_GIT_CREDENTIALS_<NAME>`` lookup, no ``provider.obtain_token``
fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.secrets.policy import InteractionPolicy
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _resolve_tokens(config: object, registry: object, names: list[str]) -> dict[str, str]:
    """Resolve git tokens for the named credentials the way the
    orchestrated commands do: construct the credential nodes, register
    the walk-derived union on the operation's resolver, run the one
    boundary pass, and read each token through the node's SCOPED
    delivery."""
    from agentworks.git_credentials.nodes import git_credential_node
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.secrets.resolver import Resolver

    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)  # type: ignore[arg-type]
    nodes = [git_credential_node(registry, n) for n in names]
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    resolver.resolve()
    tokens = {
        node.provider.owner_name: ScopedSecrets(resolver.values, node.secret_refs()).get(node.provider.secret_name)
        for node in nodes
    }
    from agentworks.git_credentials import validate_git_tokens

    return validate_git_tokens({node.provider.owner_name: node.provider for node in nodes}, tokens)


def _write_cfg(
    tmp_path: Path,
    *,
    settings: str = "",
    manifests: Sequence[ManifestDoc | str] = (),
) -> Path:
    """``write_cfg`` under this file's keyword spelling."""
    return write_cfg(tmp_path, *manifests, settings=settings, filename="c.toml")


def test_collect_git_tokens_resolves_default_secret_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``token = "git-token-<name>"`` resolves via the framework's
    ``AW_SECRET_GIT_TOKEN_<NAME>`` env-var convention.
    """
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("git-credential", "github", {"provider": {"name": "github"}})],
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GITHUB", "ghp_abc")

    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    tokens = _resolve_tokens(config, registry, ["github"])
    assert tokens == {"github": "ghp_abc"}


def test_collect_git_tokens_resolves_custom_secret_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-typed ``token = "custom-tok"`` targets the custom secret
    name; the framework's env-var backend reads ``AW_SECRET_CUSTOM_TOK``.
    """
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("git-credential", "github", {"provider": {"name": "github", "token": "custom-tok"}})],
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_CUSTOM_TOK", "ghp_custom")

    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    tokens = _resolve_tokens(config, registry, ["github"])
    assert tokens["github"] == "ghp_custom"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("ghp_line-one\nline-two\n", id="lf"),
        pytest.param("ghp_line-one\r\nline-two\r\n", id="crlf"),
    ],
)
def test_collect_git_tokens_rejects_multiline_after_real_operation_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    from agentworks.bootstrap import build_registry
    from agentworks.errors import ValidationError

    cfg = _write_cfg(
        tmp_path,
        settings='[secret_config]\nsources = ["env-var"]\n',
        manifests=[ManifestDoc("git-credential", "github", {"provider": {"name": "github"}})],
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GITHUB", value)

    with pytest.raises(ValidationError) as caught:
        _resolve_tokens(config, build_registry(config), ["github"])

    assert value not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_collect_git_tokens_batches_multiple_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple credentials resolve in one batched call; each gets its
    own value.
    """
    cfg = _write_cfg(
        tmp_path,
        # ``azdo`` ships in the opt-in ``azure`` system plugin; enable it so the
        # azdo credential is ready and its token resolves.
        settings="""
        [plugins]
        system = ["azure"]

        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[
            ManifestDoc("git-credential", "github", {"provider": {"name": "github"}}),
            ManifestDoc("git-credential", "azdo", {"provider": {"name": "azdo", "org": "my-org"}}),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GITHUB", "ghp_aaa")
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_AZDO", "azdo_bbb")

    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    tokens = _resolve_tokens(config, registry, ["github", "azdo"])
    assert tokens == {"github": "ghp_aaa", "azdo": "azdo_bbb"}


def test_collect_git_tokens_empty_list_returns_empty_dict(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path)
    config = load_config(cfg, warn_issues=False)

    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    assert _resolve_tokens(config, registry, []) == {}


def test_manifest_declared_credential_resolves_through_the_fold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A YAML-manifest-declared credential (here a scoped fine-grained
    PAT) resolves through the same node fold as the TOML-declared ones:
    both declaration surfaces feed the graph identically."""
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[
            ManifestDoc("git-credential", "widgets-bot", {"provider": {"name": "github", "repos": ["acme/widgets"]}})
        ],
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_WIDGETS_BOT", "tok123")

    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    tokens = _resolve_tokens(config, registry, ["widgets-bot"])
    assert tokens == {"widgets-bot": "tok123"}


def test_collect_git_tokens_credential_lines_use_resolved_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token value flows into ``provider.credential_lines(token)``
    -- the format that lands in ``~/.git-credentials`` on the VM.
    """
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("git-credential", "github", {"provider": {"name": "github"}})],
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GITHUB", "ghp_xyz")

    from agentworks.bootstrap import build_registry
    from agentworks.capabilities.git_credential.github import GitHubCredentialProvider

    registry = build_registry(config)
    tokens = _resolve_tokens(config, registry, ["github"])

    provider = GitHubCredentialProvider("github")
    lines = provider.credential_lines(tokens["github"])
    assert lines == ["https://x-access-token:ghp_xyz@github.com"]


def test_secret_name_equals_graph_secret_edge_single_derivation(
    tmp_path: Path,
) -> None:
    """LLD d single-derivation invariant: a git-credential provider's op-time
    ``secret_name`` (read from the construct-time ``_secret_refs`` cache, itself
    sourced from ``dependencies(config)``) equals the secret edge the graph
    froze for the same resource. Build-time edges and op-time refs agree by
    construction (one derivation, not two), so no graph-threading to op time is
    needed."""
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("git-credential", "github", {"provider": {"name": "github", "token": "custom-tok"}})],
    )
    config = load_config(cfg, warn_issues=False)

    from agentworks.bootstrap import build_registry
    from agentworks.git_credentials.nodes import git_credential_node

    registry = build_registry(config)
    node = git_credential_node(registry, "github")
    graph_secret_edges = tuple(
        ref.name for ref in registry.graph.edges_of("git-credential", "github") if ref.kind == "secret"
    )
    assert node.provider.secret_name == "custom-tok"
    assert graph_secret_edges == ("custom-tok",)
    assert node.provider.secret_name in graph_secret_edges

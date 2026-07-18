"""Tests for Phase 1d's git-credential token-resolution path.

The framework resolves each git-credential's ``token`` field through
the backend chain; the resolved value is written into
``~/.git-credentials`` via ``credential_lines``. No
``AW_GIT_CREDENTIALS_<NAME>`` lookup, no ``provider.obtain_token``
fallback.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import load_config


def _resolve_tokens(config: object, registry: object, names: list[str]) -> dict[str, str]:
    """Resolve git tokens for the named credentials the way the
    orchestrated commands do: construct the credential nodes, register
    the walk-derived union on the operation's resolver, run the one
    boundary pass, and read each token through the node's SCOPED
    delivery."""
    from agentworks.git_credentials.nodes import git_credential_node
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.secrets.resolver import Resolver

    resolver = Resolver(config, registry)  # type: ignore[arg-type]
    nodes = [git_credential_node(registry, n, resolver) for n in names]  # type: ignore[arg-type]
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    resolver.resolve()
    return {
        node.provider.owner_name: ScopedSecrets(
            resolver.values, node.secret_refs()
        ).get(node.provider.secret_name)
        for node in nodes
    }


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    return pub, priv


def _write_cfg(tmp_path: Path, body: str, ssh_keys: tuple[Path, Path]) -> Path:
    pub, priv = ssh_keys
    p = tmp_path / "c.toml"
    p.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            """
        )
        + dedent(body)
    )
    return p


def test_collect_git_tokens_resolves_default_secret_name(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``token = "git-token-<name>"`` resolves via the framework's
    ``AW_SECRET_GIT_TOKEN_<NAME>`` env-var convention.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"

        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GITHUB", "ghp_abc")

    from agentworks.bootstrap import build_registry


    registry = build_registry(config)
    tokens = _resolve_tokens(config, registry, ["github"])
    assert tokens == {"github": "ghp_abc"}


def test_collect_git_tokens_resolves_custom_secret_name(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-typed ``token = "custom-tok"`` targets the custom secret
    name; the framework's env-var backend reads ``AW_SECRET_CUSTOM_TOK``.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"
        token = "custom-tok"

        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_CUSTOM_TOK", "ghp_custom")

    from agentworks.bootstrap import build_registry


    registry = build_registry(config)
    tokens = _resolve_tokens(config, registry, ["github"])
    assert tokens["github"] == "ghp_custom"


def test_collect_git_tokens_batches_multiple_credentials(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple credentials resolve in one batched call; each gets its
    own value.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"

        [git_credentials.azdo]
        type = "azdo"
        org = "my-org"

        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GITHUB", "ghp_aaa")
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_AZDO", "azdo_bbb")

    from agentworks.bootstrap import build_registry


    registry = build_registry(config)
    tokens = _resolve_tokens(config, registry, ["github", "azdo"])
    assert tokens == {"github": "ghp_aaa", "azdo": "azdo_bbb"}


def test_collect_git_tokens_empty_list_returns_empty_dict(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(tmp_path, "", ssh_keys)
    config = load_config(cfg, warn_issues=False)

    from agentworks.bootstrap import build_registry


    registry = build_registry(config)
    assert _resolve_tokens(config, registry, []) == {}


def test_collect_git_tokens_credential_lines_use_resolved_value(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token value flows into ``provider.credential_lines(token)``
    -- the format that lands in ``~/.git-credentials`` on the VM.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"

        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
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

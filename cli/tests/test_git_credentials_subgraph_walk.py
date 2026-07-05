"""Tests for the transitive requirement walk: admin (or agent template)
-> git_credentials -> secret. Phase 1d's framework wiring makes this
graph reachable through the registry's finalize pass; once finalized,
each token secret is auto-declared with the right ``Origin.source``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config


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


def test_admin_to_git_credentials_to_secret_walk(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """admin emits requirement for git_credentials:github;
    git_credentials:github emits requirement for secret:git-token-github;
    finalize walks the whole chain and auto-declares the secret with
    the right source (the git-credential, not admin) per the
    first-matching-requirement rule.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"

        [admin.config]
        git_credentials = ["github"]
        claude_marketplaces = []
        claude_plugins = []
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    # The intermediate Resource is in the registry.
    cred = registry.lookup("git-credential", "github")
    assert cred.token == "git-token-github"

    # The downstream secret is auto-declared; its source is the
    # git_credentials Resource that emitted the requirement, NOT
    # admin (admin doesn't emit a secret requirement directly).
    decl = registry.lookup("secret", "git-token-github")
    assert decl.origin is not None
    assert decl.origin.variant == "auto-declared"
    assert decl.origin.source == ("git-credential", "github")


def test_agent_template_to_git_credentials_to_secret_walk(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.azdo]
        type = "azdo"
        org = "my-org"

        [agent_templates.claude]
        git_credentials = ["azdo"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    cred = registry.lookup("git-credential", "azdo")
    assert cred.provider_config == {"org": "my-org"}

    decl = registry.lookup("secret", "git-token-azdo")
    assert decl.origin is not None
    assert decl.origin.source == ("git-credential", "azdo")


def test_collect_secrets_for_walks_admin_subgraph(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """The ``collect_secrets_for`` helper walks the admin-template
    subgraph transitively (admin -> git_credentials -> secret) and
    returns the SecretDecls reachable along the way. Each token
    secret shows up in the walk's result; no duplicates.
    """
    from agentworks.resources import collect_secrets_for

    cfg = _write_cfg(
        tmp_path,
        """\
        [git_credentials.github]
        type = "github"

        [git_credentials.azdo]
        type = "azdo"
        org = "my-org"

        [admin.config]
        git_credentials = ["github", "azdo"]
        claude_marketplaces = []
        claude_plugins = []
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    secrets = collect_secrets_for(registry, ("admin-template", "default"))
    names = sorted(d.name for d in secrets)
    assert names == ["git-token-azdo", "git-token-github"]

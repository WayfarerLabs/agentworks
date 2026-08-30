"""Tests for the transitive requirement walk: admin (or agent template)
-> git_credentials -> secret. Phase 1d's framework wiring makes this
graph reachable through the registry's finalize pass; once finalized,
each token secret is auto-declared with the right ``Origin.source``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _write_cfg(
    tmp_path: Path,
    *,
    settings: str = "",
    manifests: Sequence[ManifestDoc | str] = (),
) -> Path:
    """``write_cfg`` under this file's keyword spelling."""
    return write_cfg(tmp_path, *manifests, settings=settings, filename="c.toml")


def test_admin_to_git_credentials_to_secret_walk(tmp_path: Path) -> None:
    """admin emits requirement for git_credentials:github;
    git_credentials:github emits requirement for secret:git-token-github;
    finalize walks the whole chain and auto-declares the secret with
    the right source (the git-credential, not admin) per the
    first-matching-requirement rule.
    """
    cfg = _write_cfg(
        tmp_path,
        manifests=[
            ManifestDoc(
                "git-credential",
                "github",
                {"provider": {"name": "github", "source": {"mode": "secret"}}},
            ),
            ManifestDoc(
                "admin-template",
                "default",
                {"git_credentials": ["github"], "claude_marketplaces": [], "claude_plugins": []},
            ),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    # The intermediate Resource is in the registry; token defaults via
    # the provider now, so the auto-declared secret below is the
    # observable proof of the walk.
    assert registry.lookup("git-credential", "github") is not None

    # The downstream secret is auto-declared; its source is the
    # git_credentials Resource that emitted the requirement, NOT
    # admin (admin doesn't emit a secret requirement directly).
    decl = registry.lookup("secret", "git-token-github")
    assert decl.origin is not None
    assert decl.origin.variant == "auto-declared"
    assert decl.origin.source == ("git-credential", "github")


def test_agent_template_to_git_credentials_to_secret_walk(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        # ``azdo`` ships in the opt-in ``azure`` system plugin; enable it so the
        # azdo credential is ready and the subgraph walk reaches its secret.
        settings="""
        [plugins]
        system = ["azure"]
        """,
        manifests=[
            ManifestDoc(
                "git-credential",
                "azdo",
                {"provider": {"name": "azdo", "org": "my-org", "source": {"mode": "secret"}}},
            ),
            ManifestDoc("agent-template", "claude", {"git_credentials": ["azdo"]}),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    cred = registry.lookup("git-credential", "azdo")
    assert cred.provider.config == {"org": "my-org", "source": {"mode": "secret"}}

    decl = registry.lookup("secret", "git-token-azdo")
    assert decl.origin is not None
    assert decl.origin.source == ("git-credential", "azdo")


def test_collect_secrets_for_walks_admin_subgraph(tmp_path: Path) -> None:
    """The ``collect_secrets_for`` helper walks the admin-template
    subgraph transitively (admin -> git_credentials -> secret) and
    returns the SecretDecls reachable along the way. Each token
    secret shows up in the walk's result; no duplicates.
    """
    from agentworks.resources import collect_secrets_for

    cfg = _write_cfg(
        tmp_path,
        # ``azdo`` ships in the opt-in ``azure`` system plugin; enable it so the
        # azdo credential is ready and the admin subgraph walk reaches its secret.
        settings="""
        [plugins]
        system = ["azure"]
        """,
        manifests=[
            ManifestDoc(
                "git-credential",
                "github",
                {"provider": {"name": "github", "source": {"mode": "secret"}}},
            ),
            ManifestDoc(
                "git-credential",
                "azdo",
                {"provider": {"name": "azdo", "org": "my-org", "source": {"mode": "secret"}}},
            ),
            ManifestDoc(
                "admin-template",
                "default",
                {"git_credentials": ["github", "azdo"], "claude_marketplaces": [], "claude_plugins": []},
            ),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    secrets = collect_secrets_for(registry, ("admin-template", "default"))
    names = sorted(d.name for d in secrets)
    assert names == ["git-token-azdo", "git-token-github"]

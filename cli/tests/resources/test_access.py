"""Contract tests for ``agentworks.resources.access``.

The accessor layer's miss semantics are load-bearing: ``git_credential``
must return None on a miss (callers raise their own typed errors for
operator-typed names), while the singleton accessors rely on the
always-materialize guarantee. A regression here surfaced as dead
``if cred is None`` guards and raw ``KeyError`` tracebacks reaching
operators (Phase 1 review finding).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.resources.access import (
    admin_template,
    git_credential,
    kind_dict,
    named_console_template,
    secret_decls,
)


def _registry(tmp_path: Path, body: str = ""):  # noqa: ANN202 - test helper
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            """
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"
            """
        ).format(pub=tmp_path / "k.pub", priv=tmp_path / "k")
        + dedent(body)
    )
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA test")
    (tmp_path / "k").write_text("key")
    return build_registry(load_config(cfg, warn_issues=False))


def test_git_credential_miss_returns_none(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert git_credential(registry, "does-not-exist") is None


def test_git_credential_hit_returns_entry(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        """
        [git_credentials.github]
        type = "github"
        description = "gh"
        """,
    )
    cred = git_credential(registry, "github")
    assert cred is not None
    assert cred.type == "github"


def test_singleton_accessors_always_present(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert admin_template(registry) is not None
    assert named_console_template(registry) is not None


def test_kind_dict_unknown_kind_is_empty(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert kind_dict(registry, "no-such-kind") == {}


def test_secret_decls_includes_auto_declared(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    # The always-materialized vm-template default references the
    # Tailscale auth key, so at least that auto-declared row exists.
    assert "tailscale-auth-key" in secret_decls(registry)

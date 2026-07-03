"""Tests for Phase 2b.2's ``secret-backend`` kind.

The kind makes per-backend config a framework citizen and lets
operator-declared ``[secret_backends.<kind>]`` blocks land as overrides
on top of the built-in known-backend rows. Partial migration: the
``[secret_config].backends`` active-chain validation is a bespoke check
at resolver assembly (``agentworks.secrets.providers.resolver_for``)
because ``SecretConfig`` isn't a framework Resource today (deferred).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.resources import KIND_REGISTRY, NoUnreferencedDefaultError
from agentworks.secrets import KNOWN_BACKEND_KINDS


def _write_cfg(path: Path, body: str = "") -> Path:
    pub = path.parent / "id.pub"
    priv = path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(body),
    )
    return path


def test_kind_attributes() -> None:
    kind = KIND_REGISTRY["secret-backend"]
    assert kind.kind == "secret-backend"
    assert kind.miss_policy == "error"
    assert kind.auto_declare_names is None


def test_synthesize_raises() -> None:
    kind = KIND_REGISTRY["secret-backend"]
    with pytest.raises(NoUnreferencedDefaultError):
        kind.synthesize(())


def test_known_backends_published(tmp_path: Path) -> None:
    """The bundled manifests seed ``SecretBackendDecl`` rows for every
    built-in backend (``env-var``, ``prompt``); operator config doesn't
    have to declare ``[secret_backends.*]`` for them to exist. Phase 3
    of the resource-manifests SDD moved these rows from the code
    publisher to ``manifests/builtin/secret-backends.yaml``.
    """
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    for backend_name in KNOWN_BACKEND_KINDS:
        row = registry.lookup("secret-backend", backend_name)
        assert row.name == backend_name
        assert row.provider == backend_name
        assert row.origin.variant == "built-in"
        assert row.origin.source == (
            "agentworks.manifests.builtin/secret-backends.yaml"
        )


def test_secret_providers_published(tmp_path: Path) -> None:
    """Every registered provider gets a read-only descriptor row that
    backend ``provider`` references resolve against."""
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    for provider_name in KNOWN_BACKEND_KINDS:
        row = registry.lookup("secret-provider", provider_name)
        assert row.name == provider_name
        assert row.origin.variant == "built-in"


def test_operator_declared_backend_overrides_built_in(tmp_path: Path) -> None:
    """An operator who writes ``[secret_backends.env-var]`` re-publishes
    the row with operator-declared origin via Config.publish_to (which
    runs after the secrets publisher). Same publish-order pattern as
    the catalog kinds.
    """
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [secret_backends.env-var]
            """,
        ),
        warn_issues=False,
    )
    registry = build_registry(cfg)
    env_var = registry.lookup("secret-backend", "env-var")
    assert env_var.origin.variant == "operator-declared"

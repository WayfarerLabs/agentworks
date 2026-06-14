"""Tests for eager-prompting orchestration (Phase 6).

Pins:
- compute_needed_secrets unions across targets and dedupes by name
- extra_decls extends the union without target env-table membership
- resolve_for_command issues one batched resolve_all per command
- resolve_for_command on an empty union does not call the resolver
- SecretTarget.label is excluded from identity (compare=False)
- Admin and agent scopes don't both flow through a single target
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.config import load_config
from agentworks.env import EnvEntry
from agentworks.secrets import (
    SecretDecl,
    SecretTarget,
    compute_needed_secrets,
    resolve_for_command,
)

# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, *, extras: str = "") -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""\
[operator]
ssh_public_key = "{pub.as_posix()}"
ssh_private_key = "{priv.as_posix()}"

[vm_templates.default]

[admin.config]
shell = "zsh"

[defaults]
{extras}
"""
    )
    return cfg


# ---------------------------------------------------------------------------
# compute_needed_secrets
# ---------------------------------------------------------------------------


def test_returns_empty_for_no_targets(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    assert compute_needed_secrets([], config) == []


def test_unions_single_target_env_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AW_SECRET_API_KEY", "x")  # silence prompt fallback
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.api-key]
description = "shared API key"

[secret_config]
backends = ["env_var"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    vm_env = {"API_KEY": EnvEntry(key="API_KEY", secret="api-key")}

    decls = compute_needed_secrets([SecretTarget(vm=vm_env)], config)
    assert [d.name for d in decls] == ["api-key"]


def test_unions_across_multiple_targets_dedup_by_name(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.shared]
description = "shared"

[secrets.unique-a]
description = "unique to target a"

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)

    t_a = SecretTarget(
        vm={
            "KEY1": EnvEntry(key="KEY1", secret="shared"),
            "KEY2": EnvEntry(key="KEY2", secret="unique-a"),
        }
    )
    t_b = SecretTarget(
        vm={
            "KEY3": EnvEntry(key="KEY3", secret="shared"),
        }
    )

    decls = compute_needed_secrets([t_a, t_b], config)
    names = [d.name for d in decls]
    # 'shared' should appear once. 'unique-a' from t_a is included.
    assert sorted(names) == ["shared", "unique-a"]
    # First-encounter ordering: 'shared' came before 'unique-a' in t_a's env
    # (KEY1 < KEY2 lexicographically; effective_env preserves dict order from
    # input which is insertion order in Python 3.7+).
    assert names[0] == "shared"


def test_walks_all_scopes_in_target(tmp_path: Path) -> None:
    """All five scope dicts in a SecretTarget feed into the union."""
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.vm-secret]
description = "vm"
[secrets.ws-secret]
description = "ws"
[secrets.admin-secret]
description = "admin"
[secrets.session-secret]
description = "session"

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)

    target = SecretTarget(
        vm={"V": EnvEntry(key="V", secret="vm-secret")},
        workspace={"W": EnvEntry(key="W", secret="ws-secret")},
        admin={"A": EnvEntry(key="A", secret="admin-secret")},
        session={"S": EnvEntry(key="S", secret="session-secret")},
    )
    decls = compute_needed_secrets([target], config)
    assert sorted(d.name for d in decls) == [
        "admin-secret", "session-secret", "vm-secret", "ws-secret",
    ]


def test_extra_decls_extend_union(tmp_path: Path) -> None:
    """extra_decls includes secrets that aren't in any target's env chain
    -- the hook for legacy tailscale / git-cred migration."""
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.from-env]
description = "in env table"
[secrets.external]
description = "not in any env table"

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)

    target = SecretTarget(vm={"K": EnvEntry(key="K", secret="from-env")})
    external_decl = config.secrets["external"]
    decls = compute_needed_secrets([target], config, extra_decls=[external_decl])
    assert sorted(d.name for d in decls) == ["external", "from-env"]


def test_extra_decls_dedupe_against_target_decls(tmp_path: Path) -> None:
    """An extra_decl that's ALSO referenced by a target's env chain
    appears once, not twice."""
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.shared]
description = "shared"

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)

    target = SecretTarget(vm={"K": EnvEntry(key="K", secret="shared")})
    shared = config.secrets["shared"]
    decls = compute_needed_secrets([target], config, extra_decls=[shared])
    assert [d.name for d in decls] == ["shared"]


# ---------------------------------------------------------------------------
# resolve_for_command
# ---------------------------------------------------------------------------


def test_resolve_for_command_calls_backend_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured backend's batch_get fires exactly once per command:
    once at the eager-resolve call, then never again because subsequent
    compose_env / resolver.render calls hit the resolver cache."""
    monkeypatch.setenv("AW_SECRET_API_KEY", "from-env")
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.api-key]
description = "api"

[secret_config]
backends = ["env_var"]
""",
    )
    config = load_config(cfg, warn_issues=False)

    # The env_var source is the only active backend; spy its batch_get to
    # count how many times the chain actually consults a backend.
    env_var_source = config.secret_resolver._sources[0]
    assert env_var_source.kind == "env_var"
    calls: list[list[str]] = []
    original = env_var_source.batch_get

    def _spy(decls: list[SecretDecl]) -> dict[str, str]:
        calls.append([d.name for d in decls])
        return original(decls)

    monkeypatch.setattr(env_var_source, "batch_get", _spy)

    target = SecretTarget(vm={"K": EnvEntry(key="K", secret="api-key")})
    resolve_for_command([target], config)

    assert calls == [["api-key"]]

    # Subsequent renders hit the resolver cache: backend is never consulted again.
    config.secret_resolver.render({"K": EnvEntry(key="K", secret="api-key")})
    config.secret_resolver.render({"K": EnvEntry(key="K", secret="api-key")})
    assert calls == [["api-key"]], (
        "backend.batch_get must not fire again once the cache is warm"
    )


def test_resolve_for_command_skips_resolver_when_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty target list (or targets whose envs reference no secrets)
    must not call resolve_all -- avoids spinning up prompt machinery for
    commands that need nothing."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)

    called = {"count": 0}

    def _spy(decls: list[SecretDecl]) -> dict[str, str]:
        called["count"] += 1
        return {}

    monkeypatch.setattr(config.secret_resolver, "resolve_all", _spy)

    resolve_for_command([], config)
    assert called["count"] == 0

    plaintext_target = SecretTarget(vm={"K": EnvEntry(key="K", value="plain")})
    resolve_for_command([plaintext_target], config)
    assert called["count"] == 0


def test_resolve_for_command_passes_extra_decls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extra_decls reach resolve_all even when no target references any
    secret -- pinning the legacy-migration hook."""
    monkeypatch.setenv("AW_SECRET_EXTERNAL", "x")
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.external]
description = "external"

[secret_config]
backends = ["env_var"]
""",
    )
    config = load_config(cfg, warn_issues=False)

    calls: list[list[str]] = []
    original = config.secret_resolver.resolve_all

    def _spy(decls: list[SecretDecl]) -> dict[str, str]:
        calls.append([d.name for d in decls])
        return original(decls)

    monkeypatch.setattr(config.secret_resolver, "resolve_all", _spy)

    resolve_for_command(
        [], config, extra_decls=[config.secrets["external"]]
    )
    assert calls == [["external"]]


# ---------------------------------------------------------------------------
# SecretTarget shape
# ---------------------------------------------------------------------------


def test_label_excluded_from_equality() -> None:
    """label is diagnostic-only; targets with the same envs but different
    labels are equal."""
    env = {"K": EnvEntry(key="K", value="v")}
    a = SecretTarget(vm=env, label="provisioning")
    b = SecretTarget(vm=env, label="session-create")
    assert a == b


def test_label_round_trips() -> None:
    target = SecretTarget(
        vm={"K": EnvEntry(key="K", value="v")},
        label="agent-bootstrap",
    )
    assert target.label == "agent-bootstrap"

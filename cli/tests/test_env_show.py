"""Tests for ``agw env show`` (the service-layer ``agentworks.env.show.show_env``).

Pins:
- context-required validation (raises ValidationError on no flags)
- auto-resolution from --session / --workspace / --agent down to the VM
- precedence-sorted, scope-annotated rendering
- secret redaction by default; --reveal-secrets resolves via the resolver
- per-context identity vars overlay user env (identity wins per FRD R1)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.config import load_config
from agentworks.db import Database
from agentworks.env.show import ResolvedEnvRow, show_env
from agentworks.errors import ValidationError

# ---------------------------------------------------------------------------
# Test config fixture
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, *, extras: str = "") -> Path:
    """Write a minimal config.toml + return its path."""
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


@pytest.fixture(autouse=True)
def _all_platforms_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests resolve VM rows against real registries; make the
    bundled sites publish regardless of the test host's tooling."""
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)


def _seed_db(
    db: Database, *, with_workspace: bool = True, with_agent: bool = False, with_session: bool = False
) -> None:
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host) "
        "VALUES ('vm-1', 'lima-local', 'h', 'agentworks', '100.64.0.5')"
    )
    if with_workspace:
        db._conn.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES ('ws-a', 'vm-1', '/home/agentworks/ws-a', 'ws-ws-a')"
        )
    if with_agent:
        db.insert_agent("claude", "vm-1", "aw-claude")
    if with_session:
        db._conn.execute(
            "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
            "VALUES ('s1', 'ws-a', 'default', 'agent', 'claude', "
            "'/run/agentworks/agent-tmux-sockets/aw-claude/s1.sock')"
        )
    db._conn.commit()


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


def test_no_flags_raises_validation_error(db: Database, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)
    with pytest.raises(ValidationError, match="requires a context"):
        show_env(db, config)


def test_unknown_vm_raises_validation_error(db: Database, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)
    with pytest.raises(ValidationError, match="VM 'nope' not found"):
        show_env(db, config, vm_name="nope")


def test_session_flag_auto_resolves_workspace_agent_vm(
    db: Database,
    tmp_path: Path,
) -> None:
    """--session s1 should infer workspace, agent, and vm from the session
    row. The dynamic identity vars (AGENTWORKS_SESSION, AGENTWORKS_WORKSPACE)
    show up in env-show output because they're per-context. AGENTWORKS_AGENT
    does NOT -- it's per-user-static under the new identity taxonomy and
    lives in the agent's on-disk profile fragment, not in inline env."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_workspace=True, with_agent=True, with_session=True)

    rows = show_env(db, config, session_name="s1")
    # Per-context dynamic identity vars are surfaced.
    keys = {r.key: r for r in rows}
    assert keys["AGENTWORKS_SESSION"].rendered_value == "s1"
    assert keys["AGENTWORKS_WORKSPACE"].rendered_value == "ws-a"
    # Per-user static identity (AGENTWORKS_AGENT) is NOT in env-show
    # output: it comes from the on-disk profile fragment, same shape as
    # the VM-stable vars (AGENTWORKS_VM etc.).
    assert "AGENTWORKS_AGENT" not in keys


def test_workspace_flag_auto_resolves_vm(db: Database, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    rows = show_env(db, config, workspace_name="ws-a")
    keys = {r.key: r for r in rows}
    assert "AGENTWORKS_WORKSPACE" in keys
    assert keys["AGENTWORKS_WORKSPACE"].rendered_value == "ws-a"
    # No agent / session vars because no agent context.
    assert "AGENTWORKS_AGENT" not in keys
    assert "AGENTWORKS_SESSION" not in keys


# ---------------------------------------------------------------------------
# Scope precedence + provenance
# ---------------------------------------------------------------------------


def test_session_scope_wins_over_vm_for_same_key(
    db: Database,
    tmp_path: Path,
) -> None:
    """When the same key is set at both vm and session scope, the session
    value wins AND the row's scope label is 'session'."""
    cfg = _write_config(
        tmp_path,
        extras="""
[vm_templates.default.env]
EDITOR = "vim"

[session_templates.shell.env]
EDITOR = "nvim"
""",
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_workspace=True, with_agent=True, with_session=True)
    # Session was created with template='default'; rewrite to 'shell' for this test.
    db._conn.execute("UPDATE sessions SET template = 'shell' WHERE name = 's1'")
    db._conn.commit()

    rows = show_env(db, config, session_name="s1")
    editor = next(r for r in rows if r.key == "EDITOR")
    assert editor.rendered_value == "nvim"
    assert editor.scope == "session"


def test_admin_env_appears_only_when_no_agent_context(
    db: Database,
    tmp_path: Path,
) -> None:
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
HTTP_PROXY = "http://proxy:3128"
""",
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_agent=True)

    # --vm only: admin scope applies (no agent context).
    rows_admin = show_env(db, config, vm_name="vm-1")
    assert any(r.key == "HTTP_PROXY" and r.scope == "admin" for r in rows_admin)

    # --agent: admin scope does NOT apply (agent context excludes it).
    rows_agent = show_env(db, config, agent_name="claude")
    assert not any(r.key == "HTTP_PROXY" for r in rows_agent)


# ---------------------------------------------------------------------------
# Secret rendering
# ---------------------------------------------------------------------------


def test_secret_redacted_by_default(db: Database, tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
API_KEY = { secret = "shared-token" }

[secrets.shared-token]
description = "shared API token"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    rows = show_env(db, config, vm_name="vm-1")
    api = next(r for r in rows if r.key == "API_KEY")
    assert api.is_secret
    assert api.rendered_value == "<from secret: shared-token>"


def test_secret_revealed_with_flag(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--reveal-secrets resolves through the active backend chain."""
    monkeypatch.setenv("AW_SECRET_SHARED_TOKEN", "from-operator-env")
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
API_KEY = { secret = "shared-token" }

[secrets.shared-token]
description = "shared API token"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    rows = show_env(db, config, vm_name="vm-1", reveal_secrets=True)
    api = next(r for r in rows if r.key == "API_KEY")
    assert api.is_secret
    assert api.rendered_value == "from-operator-env"


# ---------------------------------------------------------------------------
# Identity overlay
# ---------------------------------------------------------------------------


def test_identity_var_overlays_user_env(
    db: Database,
    tmp_path: Path,
) -> None:
    """User env that tries to set AGENTWORKS_SESSION gets the identity value
    at render time (per FRD R1; the operator's value is replaced)."""
    cfg = _write_config(
        tmp_path,
        extras="""
[session_templates.shell.env]
AGENTWORKS_SESSION = "operator-override"
""",
    )
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_workspace=True, with_agent=True, with_session=True)
    db._conn.execute("UPDATE sessions SET template = 'shell' WHERE name = 's1'")
    db._conn.commit()

    rows = show_env(db, config, session_name="s1")
    session_row = next(r for r in rows if r.key == "AGENTWORKS_SESSION")
    assert session_row.rendered_value == "s1"  # identity wins
    assert session_row.scope == "identity"


def test_identity_subset_skips_vm_stable_vars(
    db: Database,
    tmp_path: Path,
) -> None:
    """The inline (env show) identity output mirrors the inline prelude
    subset: VM-stable vars (AGENTWORKS_VM / _VM_HOST / _PLATFORM) come from
    VM-side profile fragments (Phase 4) and don't appear in the env-show
    output."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db, with_workspace=True, with_agent=True, with_session=True)

    rows = show_env(db, config, session_name="s1")
    keys = {r.key for r in rows}
    for excluded in ("AGENTWORKS_VM", "AGENTWORKS_VM_HOST", "AGENTWORKS_PLATFORM"):
        assert excluded not in keys, f"{excluded} should not appear in env show output"


# ---------------------------------------------------------------------------
# Shape of the return value
# ---------------------------------------------------------------------------


def test_return_type_is_list_of_resolved_env_rows(
    db: Database,
    tmp_path: Path,
) -> None:
    """``show_env`` returns the structured rows in addition to printing,
    so tests can pin contracts without parsing formatted output."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    _seed_db(db)

    rows = show_env(db, config, vm_name="vm-1")
    assert isinstance(rows, list)
    assert all(isinstance(r, ResolvedEnvRow) for r in rows)

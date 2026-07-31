"""SecretTarget builder parity for ``sessions.manager.create_session``.

Split out of ``test_session_create_ephemeral.py`` (see
``_session_ephemeral_support.py`` for the full background on issue #124's
guarantees). This file pins parity between the two SecretTarget builders
so the pre-create helper can't silently diverge from the existing
post-create one for the inputs they both handle (existing workspace +
existing agent or admin mode).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.db import Database

from ._session_ephemeral_support import _non_interactive, _stub_build_registry

__all__ = ["_non_interactive", "_stub_build_registry"]


def _write_parity_config(tmp_path: Path) -> Path:
    """Config with secrets referenced at every env scope so the parity
    test exercises vm / workspace / admin / agent / session scopes."""
    from textwrap import dedent

    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [vm_templates.default]
        env = {{ VM_TOKEN = {{ secret = "vm-secret" }} }}

        [workspace_templates.default]
        env = {{ WS_TOKEN = {{ secret = "ws-secret" }} }}

        [agent_templates.default]
        env = {{ AGENT_TOKEN = {{ secret = "agent-secret" }} }}

        [admin.config]
        shell = "zsh"

        [admin.env]
        ADMIN_TOKEN = {{ secret = "admin-secret" }}

        [session_templates.default]
        env = {{ SESSION_TOKEN = {{ secret = "session-secret" }} }}

        [secrets.vm-secret]
        description = "vm-scope secret"
        [secrets.ws-secret]
        description = "workspace-scope secret"
        [secrets.agent-secret]
        description = "agent-scope secret"
        [secrets.admin-secret]
        description = "admin-scope secret"
        [secrets.session-secret]
        description = "session-scope secret"
        """)
    )
    return cfg


def _seed_parity_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "parity.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, template) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5', 'default')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group, template) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1', 'default')"
    )
    db._conn.commit()
    db.insert_agent("agt1", "vm1", "aw-agt1", template="default")
    return db


@pytest.mark.parametrize("mode", ["admin", "agent"])
def test_secret_target_pre_create_parity_with_session_secret_target(tmp_path: Path, mode: str) -> None:
    """For existing workspace + (existing agent | admin mode), the two
    SecretTarget builders must produce equal targets so
    ``compute_needed_secrets`` is invariant across the two helpers."""
    from agentworks.config import load_config
    from agentworks.db import SessionMode
    from agentworks.secrets import compute_needed_secrets
    from agentworks.sessions.manager import (
        _resolve_template,
        _session_secret_target,
        _session_secret_target_pre_create,
    )

    config = load_config(_write_parity_config(tmp_path), warn_issues=False)
    db = _seed_parity_db(tmp_path)
    vm = db.get_vm("vm1")
    ws = db.get_workspace("ws1")
    assert vm is not None
    assert ws is not None
    agent = db.get_agent("agt1") if mode == "agent" else None
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    session_template = _resolve_template(registry, None)

    post = _session_secret_target(
        registry,
        db=db,
        vm=vm,
        ws=ws,
        session_name="s1",
        session_template=session_template,
        mode=SessionMode.AGENT if mode == "agent" else SessionMode.ADMIN,
        agent_name="agt1" if mode == "agent" else None,
    )
    pre = _session_secret_target_pre_create(
        registry,
        name="s1",
        workspace_name="ws1",
        vm=vm,
        session_template=session_template,
        new_workspace=False,
        workspace_template=None,
        existing_workspace=ws,
        new_agent=False,
        agent_template=None,
        existing_agent=agent,
        is_admin_mode=(mode == "admin"),
    )

    assert pre == post
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    assert compute_needed_secrets([pre], registry) == compute_needed_secrets([post], registry)

    db.close()

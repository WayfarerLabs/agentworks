"""The claude-code OAuth-token wiring driven through the real
orchestrator (issue #220): the carry the harness unit test cannot prove.

- ``session create`` and ``session restart`` both merge the harness's
  ``CLAUDE_CODE_OAUTH_TOKEN`` contribution over the composed session env,
  with the value resolved by the real graph boundary pass (env-var
  backend);
- a collision with an operator env directive of the same name warns and
  the harness value wins;
- the token value never rides the pane command string;
- an unmapped token secret fails at preflight for free (the declared ref
  joins the graph union, so central resolvability prediction covers it),
  with nothing killed or created.

Real config / registry / resolver / env-var backend; the transports and
the tmux launch are the fakes. No test spawns a real ``claude`` binary:
the readiness ``command -v claude`` probe and the ``<sid>.jsonl`` find
probe are answered by the transport double.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, SessionMode, SessionStatus
from agentworks.errors import ConfigError

from ..orchestrated_fixtures import write_operator_config

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.ssh import SSHLogger

_TOKEN_VALUE = "sk-oauth-abc123"


class _Result:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = ""
        self.stderr = ""


class _ClaudeTarget:
    """Transport double: answers the readiness ``command -v claude`` probe
    and the ``<sid>.jsonl`` find probe (transcript always present, so the
    op resumes). ``received_logger`` records the op logger the manager
    handed the transport factory, so the redaction tests can assert
    against the logger the LAUNCH transport actually carries, not just
    any logger the operation constructed."""

    received_logger: SSHLogger | None = None

    def run(self, cmd: str, **kwargs: object) -> _Result:
        return _Result(ok=True)


def _seed_lima_vm(db: Database) -> None:
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, "
        "init_status) VALUES ('vm1', 'lima-local', 'h', 'admin', '100.64.0.5', "
        "'complete')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()


def _make_config(tmp_path: Path, template_body: str) -> Config:
    return write_operator_config(tmp_path, template_body)


# A claude-code session template with OAuth passing enabled.
_CC_TEMPLATE = """
[session_templates.claude]
harness = "claude-code"

[session_templates.claude.harness_config]
pass_oauth_token = true
"""

# Same, plus an operator env directive that collides with the token var.
_CC_TEMPLATE_WITH_COLLISION = """
[session_templates.claude]
harness = "claude-code"

[session_templates.claude.harness_config]
pass_oauth_token = true

[session_templates.claude.env]
CLAUDE_CODE_OAUTH_TOKEN = "operator-placeholder"
"""


def _patch_transports(monkeypatch: pytest.MonkeyPatch) -> _ClaudeTarget:
    target = _ClaudeTarget()

    def admin_factory(
        *args: object, logger: SSHLogger | None = None, **kwargs: object
    ) -> _ClaudeTarget:
        target.received_logger = logger
        return target

    monkeypatch.setattr("agentworks.transports.transport", admin_factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", admin_factory)
    return target


def _capture_launch(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
) -> None:
    from agentworks.sessions import tmux as tmux_mod

    def _capture(
        name: str,
        ws_path: str,
        command: str,
        linux_user: str,
        *,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> tuple[str, int]:
        captured["command"] = command
        captured["env"] = dict(env or {})
        return ("/tmp/s1.sock", 4243)

    monkeypatch.setattr(tmux_mod, "create_session", _capture)


def _common_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.sessions import console as console_mod
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod
    from tests.conftest import stub_vm_gates

    stub_vm_gates(monkeypatch)
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *a, **k: None)
    monkeypatch.setattr(session_manager, "_get_boot_id", lambda *a, **k: "boot-x")
    monkeypatch.setattr(session_manager, "_regenerate_tmuxinator", lambda *a, **k: None)
    monkeypatch.setattr(console_mod, "add_session_to_console", lambda *a, **k: None)


# -- create: the token reaches the session env -------------------------------


def test_create_merges_the_token_into_the_session_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.sessions.manager import create_session

    monkeypatch.setenv("AW_SECRET_CLAUDE_CODE_OAUTH_TOKEN", _TOKEN_VALUE)
    db = Database(tmp_path / "test.db")
    _seed_lima_vm(db)
    config = _make_config(tmp_path, _CC_TEMPLATE)
    _patch_transports(monkeypatch)
    _common_stubs(monkeypatch)
    captured: dict[str, object] = {}
    _capture_launch(monkeypatch, captured)

    create_session(db, config, name="s1", workspace="ws1", admin=True, template_name="claude")

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN_VALUE
    # The token value NEVER rides the pane command string.
    assert _TOKEN_VALUE not in captured["command"]  # type: ignore[operator]
    db.close()


# -- restart: same merge at the restart launch site --------------------------


def test_restart_merges_the_token_into_the_session_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import restart_session

    monkeypatch.setenv("AW_SECRET_CLAUDE_CODE_OAUTH_TOKEN", _TOKEN_VALUE)
    db = Database(tmp_path / "test.db")
    _seed_lima_vm(db)
    db.insert_session(
        "s1",
        "ws1",
        "claude",
        SessionMode.ADMIN,
        harness_state={"session_id": "939b1597-7c61-5ace-80f4-14617b7b4257"},
    )
    db.update_session_pid("s1", 4242, boot_id="boot-x")
    config = _make_config(tmp_path, _CC_TEMPLATE)
    _patch_transports(monkeypatch)
    _common_stubs(monkeypatch)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.OK
    )
    monkeypatch.setattr(session_manager, "_kill_session", lambda *a, **k: True)
    captured: dict[str, object] = {}
    _capture_launch(monkeypatch, captured)

    restart_session(db, config, name="s1", yes=True)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN_VALUE
    assert _TOKEN_VALUE not in captured["command"]  # type: ignore[operator]
    db.close()


# -- collision: harness wins over an operator env directive, and warns -------


def test_harness_value_wins_over_operator_env_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    from agentworks.sessions.manager import create_session

    monkeypatch.setenv("AW_SECRET_CLAUDE_CODE_OAUTH_TOKEN", _TOKEN_VALUE)
    db = Database(tmp_path / "test.db")
    _seed_lima_vm(db)
    config = _make_config(tmp_path, _CC_TEMPLATE_WITH_COLLISION)
    _patch_transports(monkeypatch)
    _common_stubs(monkeypatch)
    captured: dict[str, object] = {}
    _capture_launch(monkeypatch, captured)

    create_session(db, config, name="s1", workspace="ws1", admin=True, template_name="claude")

    env = captured["env"]
    assert isinstance(env, dict)
    # The harness value wins the collision (not the operator placeholder).
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN_VALUE
    warnings = captured_output.warnings  # type: ignore[attr-defined]
    assert any(
        "CLAUDE_CODE_OAUTH_TOKEN" in w and "claude-code" in w for w in warnings
    ), warnings
    db.close()


def test_restart_harness_value_wins_over_operator_env_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """The restart-path mirror of the create-path collision test: both
    launch sites route through the one ``_merge_harness_env`` helper, and
    this pins the restart wiring end to end."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import restart_session

    monkeypatch.setenv("AW_SECRET_CLAUDE_CODE_OAUTH_TOKEN", _TOKEN_VALUE)
    db = Database(tmp_path / "test.db")
    _seed_lima_vm(db)
    db.insert_session(
        "s1",
        "ws1",
        "claude",
        SessionMode.ADMIN,
        harness_state={"session_id": "939b1597-7c61-5ace-80f4-14617b7b4257"},
    )
    db.update_session_pid("s1", 4242, boot_id="boot-x")
    config = _make_config(tmp_path, _CC_TEMPLATE_WITH_COLLISION)
    _patch_transports(monkeypatch)
    _common_stubs(monkeypatch)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.OK
    )
    monkeypatch.setattr(session_manager, "_kill_session", lambda *a, **k: True)
    captured: dict[str, object] = {}
    _capture_launch(monkeypatch, captured)

    restart_session(db, config, name="s1", yes=True)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN_VALUE
    warnings = captured_output.warnings  # type: ignore[attr-defined]
    assert any(
        "CLAUDE_CODE_OAUTH_TOKEN" in w and "claude-code" in w for w in warnings
    ), warnings
    db.close()


# -- the resolved token is registered for redaction on the op logger ----------


def test_create_registers_the_token_for_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The create path registers every resolved secret value on the
    logger carried by the LAUNCH transport (the one the manager handed
    the transport factory), so a command carrying the token (the tmux
    ``-e`` flags) is redacted from the op log and from raised SSHError
    text (asserted through the public ``sanitize`` surface)."""
    from agentworks.sessions.manager import create_session

    monkeypatch.setenv("AW_SECRET_CLAUDE_CODE_OAUTH_TOKEN", _TOKEN_VALUE)
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    db = Database(tmp_path / "test.db")
    _seed_lima_vm(db)
    config = _make_config(tmp_path, _CC_TEMPLATE)
    target = _patch_transports(monkeypatch)
    _common_stubs(monkeypatch)
    _capture_launch(monkeypatch, {})

    create_session(db, config, name="s1", workspace="ws1", admin=True, template_name="claude")

    assert target.received_logger is not None
    assert target.received_logger.sanitize(_TOKEN_VALUE) == "[REDACTED]"
    db.close()


def test_restart_registers_the_token_for_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart path registers the values from BOTH resolve passes
    (the graph union carries the token) on the launch transport's
    logger."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import restart_session

    monkeypatch.setenv("AW_SECRET_CLAUDE_CODE_OAUTH_TOKEN", _TOKEN_VALUE)
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    db = Database(tmp_path / "test.db")
    _seed_lima_vm(db)
    db.insert_session(
        "s1",
        "ws1",
        "claude",
        SessionMode.ADMIN,
        harness_state={"session_id": "939b1597-7c61-5ace-80f4-14617b7b4257"},
    )
    db.update_session_pid("s1", 4242, boot_id="boot-x")
    config = _make_config(tmp_path, _CC_TEMPLATE)
    target = _patch_transports(monkeypatch)
    _common_stubs(monkeypatch)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.OK
    )
    monkeypatch.setattr(session_manager, "_kill_session", lambda *a, **k: True)
    _capture_launch(monkeypatch, {})

    restart_session(db, config, name="s1", yes=True)

    assert target.received_logger is not None
    assert target.received_logger.sanitize(_TOKEN_VALUE) == "[REDACTED]"
    db.close()


# -- an unmapped token fails at preflight, for free --------------------------


def test_unmapped_token_fails_at_preflight_before_any_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``AW_SECRET_CLAUDE_CODE_OAUTH_TOKEN`` in the env and a non-
    interactive run: the declared token ref joins the graph union, so the
    central resolvability prediction fails it at preflight, before any
    tmux launch. No new manager code proves this: it is the #202/#215
    plumbing covering the new ref."""
    from agentworks.sessions.manager import create_session

    monkeypatch.delenv("AW_SECRET_CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    db = Database(tmp_path / "test.db")
    _seed_lima_vm(db)
    # Restrict the chain to env-var only (no prompt) so an unset token is
    # DEFINITIVELY unreachable, and central prediction fails it at
    # preflight rather than the resolve boundary.
    config = _make_config(
        tmp_path,
        _CC_TEMPLATE + '\n[secret_config]\nbackends = ["env-var"]\n',
    )
    _patch_transports(monkeypatch)
    _common_stubs(monkeypatch)
    captured: dict[str, object] = {}
    _capture_launch(monkeypatch, captured)

    with pytest.raises(ConfigError, match="not resolvable by any active backend"):
        create_session(
            db, config, name="s1", workspace="ws1", admin=True, template_name="claude"
        )

    assert "command" not in captured  # nothing launched
    assert db.get_session("s1") is None
    db.close()

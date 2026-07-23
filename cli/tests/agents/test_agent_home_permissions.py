"""Agent home-directory isolation: the two enforcement points added for
issue #228 (a world-readable ``$HOME`` let any other agent user on the
VM read another agent's scratch, logs, and caches).

Both changes live on ``create_agent_on_vm``, the single path shared by
``agent create`` and ``agent reinit``, so exercising that function
directly with recording transports proves create AND reinit apply them:

1. an admin-side ``chmod 0750`` of the agent's home, and
2. a ``umask 027`` line in the managed ``~/.agentworks-profile.sh``.

The transports are fakes that record every ``run`` / ``write_file``;
the config, registry, and resolved template are real (a minimal default
template: no git credentials, install commands, dotfiles, or mise, so
the setup body reduces to its identity/permission steps).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents import initializer as agent_initializer
from tests.orchestrated_fixtures import PROXMOX_SECTION, write_operator_config

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database

LINUX_USER = "agt-dev"
HOME = f"/home/{LINUX_USER}"
PROFILE_BASENAME = ".agentworks-profile.sh"


class _Result:
    """Minimal stand-in for a transport command result."""

    def __init__(self, ok: bool = True, returncode: int = 0, stdout: str = "") -> None:
        self.ok = ok
        self.returncode = returncode
        self.stdout = stdout


class _RecordingTransport:
    """Records ``run`` / ``write_file`` calls and returns benign results.

    ``user_exists`` selects the ``id <user>`` branch: False drives the
    ``useradd`` (create) path, True the ``usermod`` (reinit) path.
    """

    def __init__(self, *, user_exists: bool = False) -> None:
        self.runs: list[tuple[str, bool]] = []
        self.writes: list[tuple[str, str, str | None]] = []
        self._user_exists = user_exists

    def run(self, cmd: str, *, sudo: bool = False, check: bool = True, timeout: int | None = None) -> _Result:
        self.runs.append((cmd, sudo))
        if cmd.startswith(f"id {LINUX_USER}"):
            return _Result(ok=self._user_exists, returncode=0 if self._user_exists else 1)
        if cmd.startswith("mktemp"):
            # _reconcile_authorized_keys stages into a mktemp path and
            # raises on an empty one, so hand back a plausible path.
            return _Result(stdout="/tmp/agw-ak.abcdef")
        return _Result()

    def write_file(self, path: str, content: str, *, mode: str | None = None) -> _Result:
        self.writes.append((path, content, mode))
        return _Result()


def _run_create_on_vm(
    db: Database,
    config: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_exists: bool,
) -> tuple[_RecordingTransport, _RecordingTransport]:
    """Drive ``create_agent_on_vm`` with recording transports and return
    them (admin, agent)."""
    from agentworks.agents.templates import resolve_template
    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHLogger

    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    template = resolve_template(registry, None)

    admin = _RecordingTransport(user_exists=user_exists)
    agent = _RecordingTransport(user_exists=user_exists)
    monkeypatch.setattr(agent_initializer, "transport", lambda *a, **k: admin)
    monkeypatch.setattr("agentworks.transports.transport_for_user", lambda *a, **k: agent)

    agent_initializer.create_agent_on_vm(
        vm,
        config,
        registry,
        template,
        LINUX_USER,
        agent_name="dev",
        git_tokens={},
        logger=SSHLogger("box", "test-home-perms"),
    )
    return admin, agent


def _profile_writes(agent: _RecordingTransport) -> list[str]:
    """Contents of every write to the managed profile fragment."""
    return [content for path, content, _mode in agent.writes if path.endswith(PROFILE_BASENAME)]


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    return write_operator_config(tmp_path, PROXMOX_SECTION)


def test_create_chmods_home_0750_via_admin(db: Database, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """create runs an idempotent ``chmod 0750`` of the agent's home on the
    admin transport, with sudo (the agent can't chmod its home before its
    own SSH path exists, and the home is world-readable otherwise)."""
    admin, _agent = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    chmod_runs = [(cmd, sudo) for cmd, sudo in admin.runs if cmd.startswith("chmod 0750")]
    assert (f"chmod 0750 {HOME}", True) in chmod_runs


def test_create_writes_umask_027_into_profile(db: Database, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The managed profile fragment carries ``umask 027`` so files the
    agent writes outside a workspace default to owner-only. Written by
    ``_write_agent_profile`` itself, so both of its writes (identity-only,
    then with PATH) keep the line."""
    _admin, agent = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    writes = _profile_writes(agent)
    assert writes, "expected at least one write to the managed profile fragment"
    assert all("umask 027" in content for content in writes)


def test_reinit_reapplies_both_enforcements(db: Database, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reinit (existing user: the ``usermod`` branch) re-applies both the
    ``chmod 0750`` and the ``umask 027``, so a pre-existing world-readable
    agent home is repaired on the next reinit."""
    admin, agent = _run_create_on_vm(db, config, monkeypatch, user_exists=True)

    # Took the reinit branch (usermod, not useradd), yet still chmods.
    assert any(cmd.startswith("usermod -s") for cmd, _ in admin.runs)
    assert not any(cmd.startswith("useradd") for cmd, _ in admin.runs)
    assert (f"chmod 0750 {HOME}", True) in admin.runs

    writes = _profile_writes(agent)
    assert writes and all("umask 027" in content for content in writes)

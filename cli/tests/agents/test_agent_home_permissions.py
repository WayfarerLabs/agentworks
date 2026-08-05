"""Agent home-directory isolation and honest agent-user reporting: the
enforcement points on ``create_agent_on_vm``, the single path shared by
``agent create`` and ``agent reinit``. Exercising that function directly
with recording transports proves create AND reinit behave correctly.

Issue #228 (home isolation) added:

1. an admin-side ``chmod 0750`` of the agent's home, and
2. a ``umask 027`` line in the managed ``~/.agentworks-profile.sh``.

Issue #252 (verifying agent-user step) made the user-setup report the
DETECTED outcome instead of an unconditional "Creating user..." line:
created when the user was absent, already-exists when it converged as a
no-op, and shell-corrected when an existing user's login shell diverged
from the template. Because the message is driven by state, not by the
caller, ``reinit`` of a truly-gone agent user honestly reports a create.

The transports are fakes that record every ``run`` / ``write_file``;
the config, registry, and resolved template are real (a minimal default
template: no git credentials, install commands, dotfiles, or mise, so
the setup body reduces to its identity/permission steps).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents import initializer as agent_initializer
from tests.orchestrated_fixtures import proxmox_site, write_operator_config

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

    ``user_exists`` selects the ``getent passwd <user>`` branch: False
    drives the ``useradd`` (create) path, True the existing-user path.
    ``shell`` is the login shell that ``getent passwd`` reports for an
    existing user (field 7); it defaults to a value that DIFFERS from the
    default template's ``/bin/bash`` so the existing-user path takes the
    ``usermod`` shell-correction branch, matching the pre-#252 behavior
    where reinit always ran ``usermod``. A test that wants the converged
    no-op path passes ``shell="/bin/bash"``.
    ``primary_group`` is what ``id -gn <user>`` reports; it defaults to
    the username (a private per-user group, the healthy case), and a test
    overrides it to a shared group to exercise the post-condition guard.
    """

    def __init__(
        self,
        *,
        user_exists: bool = False,
        primary_group: str | None = None,
        shell: str = "/bin/sh",
    ) -> None:
        self.runs: list[tuple[str, bool]] = []
        self.writes: list[tuple[str, str, str | None]] = []
        self._user_exists = user_exists
        self._primary_group = primary_group or LINUX_USER
        self._shell = shell

    def run(self, cmd: str, *, sudo: bool = False, check: bool = True, timeout: int | None = None) -> _Result:
        self.runs.append((cmd, sudo))
        if cmd.startswith(f"id -gn {LINUX_USER}"):
            return _Result(stdout=self._primary_group)
        if cmd.startswith(f"getent passwd {LINUX_USER}"):
            if not self._user_exists:
                return _Result(ok=False, returncode=2)
            return _Result(stdout=f"{LINUX_USER}:x:1001:1001::{HOME}:{self._shell}")
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
    primary_group: str | None = None,
    shell: str = "/bin/sh",
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

    admin = _RecordingTransport(user_exists=user_exists, primary_group=primary_group, shell=shell)
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


def _first_run_index(admin: _RecordingTransport, predicate) -> int:  # noqa: ANN001
    """Index of the first recorded admin ``run`` whose command matches."""
    for i, (cmd, _sudo) in enumerate(admin.runs):
        if predicate(cmd):
            return i
    return -1


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    return write_operator_config(tmp_path, manifests=[proxmox_site()])


def test_create_useradd_forces_private_group(db: Database, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The create-path ``useradd`` carries ``-U`` so the agent's primary
    group is a per-user private group regardless of the image's
    ``USERGROUPS_ENAB``; without it, 0750 could leak the home to a shared
    primary group."""
    admin, _agent = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    useradd = [cmd for cmd, _ in admin.runs if cmd.startswith("useradd")]
    assert useradd, "expected a useradd on the create path"
    assert all(" -U " in f" {cmd} " for cmd in useradd)


def test_create_chmods_home_0750_via_admin(db: Database, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """create runs an idempotent ``chmod 0750`` of the agent's home on the
    admin transport, with sudo (the agent can't chmod its home before its
    own SSH path exists, and the home is world-readable otherwise)."""
    admin, _agent = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    chmod_runs = [(cmd, sudo) for cmd, sudo in admin.runs if cmd.startswith("chmod 0750")]
    assert (f"chmod 0750 {HOME}", True) in chmod_runs


def test_chmod_ordered_after_user_creation_and_before_ssh_stage(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``chmod 0750`` must land AFTER the useradd/usermod (the home
    must exist) and BEFORE the authorized_keys stage-and-install writes
    into ``~/.ssh`` (the stated phase-1 placement). Pins order so a
    reorder that moved the chmod ahead of user creation, a real runtime
    failure, cannot pass on presence alone."""
    admin, _agent = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    user_create = _first_run_index(admin, lambda c: c.startswith(("useradd", "usermod -s")))
    chmod = _first_run_index(admin, lambda c: c.startswith(f"chmod 0750 {HOME}"))
    ssh_stage = _first_run_index(admin, lambda c: f"{HOME}/.ssh" in c or c.startswith("mktemp"))

    assert user_create != -1 and chmod != -1 and ssh_stage != -1
    assert user_create < chmod < ssh_stage


def test_shared_primary_group_warns(
    db: Database,
    config: Any,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: Any,
) -> None:
    """When the agent's primary group is shared (``id -gn`` != username),
    the post-condition guard surfaces a warning rather than silently
    leaving a 0750 home that other group members can read."""
    _admin, _agent = _run_create_on_vm(db, config, monkeypatch, user_exists=True, primary_group="users")

    assert any("primary group is 'users'" in w for w in captured_output.warnings)
    assert any("cannot be made private" in w for w in captured_output.warnings)


def test_private_primary_group_does_not_warn(
    db: Database,
    config: Any,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: Any,
) -> None:
    """The healthy case (``id -gn`` == username) raises no group warning."""
    _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    assert not any("primary group" in w for w in captured_output.warnings)


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
    """Reinit (existing user with a diverged shell: the ``usermod``
    branch) re-applies both the ``chmod 0750`` and the ``umask 027``, so a
    pre-existing world-readable agent home is repaired on the next reinit."""
    admin, agent = _run_create_on_vm(db, config, monkeypatch, user_exists=True)

    # Took the reinit branch (usermod, not useradd), yet still chmods.
    assert any(cmd.startswith("usermod -s") for cmd, _ in admin.runs)
    assert not any(cmd.startswith("useradd") for cmd, _ in admin.runs)
    assert (f"chmod 0750 {HOME}", True) in admin.runs

    writes = _profile_writes(agent)
    assert writes and all("umask 027" in content for content in writes)


# -- honest agent-user reporting (issue #252) ---------------------------------


def test_create_absent_user_reports_created_not_creating(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, captured_output: Any
) -> None:
    """First-time create (user absent): ``useradd`` runs and the step
    reports the user was CREATED. The old unconditional "Creating
    user..." wording is gone."""
    admin, _agent = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    assert any(cmd.startswith("useradd") for cmd, _ in admin.runs)
    assert any(f"Created agent user '{LINUX_USER}'" in m for m in captured_output.info)
    assert not any("Creating user" in m for m in captured_output.info)
    assert not any("already exists" in m for m in captured_output.info)


def test_reinit_gone_user_reports_created_not_silently_reinitialized(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, captured_output: Any
) -> None:
    """The critical case: a truly-gone agent user on reinit is recreated
    and reported as CREATED, not silently converged. Because the message
    is driven by detected state (user absent) rather than the caller,
    reinit and create report the recreation identically."""
    admin, _agent = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    assert any(cmd.startswith("useradd") for cmd, _ in admin.runs)
    assert any(f"Created agent user '{LINUX_USER}'" in m for m in captured_output.info)
    assert not any("Creating user" in m for m in captured_output.info)


def test_reinit_converged_user_reports_already_exists_and_skips_usermod(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, captured_output: Any
) -> None:
    """Reinit of a healthy agent whose shell already matches the template
    (``/bin/bash``): a converging no-op. The step reports the user already
    exists, does NOT say "Creating user", and runs no ``usermod`` (nothing
    to converge), matching ``workspace repair``'s "OK" no-op reporting."""
    admin, _agent = _run_create_on_vm(db, config, monkeypatch, user_exists=True, shell="/bin/bash")

    assert not any(cmd.startswith("useradd") for cmd, _ in admin.runs)
    assert not any(cmd.startswith("usermod -s") for cmd, _ in admin.runs)
    assert any(f"Agent user '{LINUX_USER}' already exists" in m for m in captured_output.info)
    assert not any("Creating user" in m for m in captured_output.info)
    assert not any("Created agent user" in m for m in captured_output.info)


def test_reinit_diverged_shell_reports_fixed_and_runs_usermod(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, captured_output: Any
) -> None:
    """Reinit of an existing user whose login shell diverges from the
    template (``/bin/zsh`` on the VM vs. the default template's
    ``/bin/bash``): ``usermod -s`` converges it and the step reports the
    correction with the before/after shells."""
    admin, _agent = _run_create_on_vm(db, config, monkeypatch, user_exists=True, shell="/bin/zsh")

    assert any(cmd.startswith("usermod -s /bin/bash") for cmd, _ in admin.runs)
    assert not any(cmd.startswith("useradd") for cmd, _ in admin.runs)
    assert any(f"Fixed agent user '{LINUX_USER}'" in m and "/bin/zsh -> /bin/bash" in m for m in captured_output.info)
    assert not any("Creating user" in m for m in captured_output.info)
    assert not any("already exists" in m for m in captured_output.info)

"""Admin home-directory isolation: the admin-user follow-up (issue #231) to the
agent home hardening of issue #228. A world-readable admin ``$HOME`` let any
agent user on the VM read the admin's git credentials, shell history, tool
caches, and dotfiles.

Three enforcement points, mirroring the agent side:

1. an admin-side ``chmod 0750`` of the admin home, applied by
   ``_harden_admin_home`` on every ``_phase_b_setup`` (initial provision AND
   reinit), with an ``id -gn`` post-condition guard that warns on a shared
   primary group;
2. a ``umask 027`` line in the admin's managed ``~/.agentworks-profile.sh``,
   emitted by ``_write_agentworks_profile`` itself so it survives reinit
   rewrites; and
3. ``useradd -m -U`` in the create-time bootstrap script so the admin's primary
   group is a private per-user group.

The transports are ``Transport``-shaped fakes that record every ``run`` /
``write_file``; the bootstrap check is a pure string assertion on the generated
script.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.vms.initializer import (
    AGENTWORKS_PROFILE,
    _harden_admin_home,
    _write_agentworks_profile,
)

ADMIN = "agentworks"
HOME = f"/home/{ADMIN}"


@dataclass
class _SpyResult:
    ok: bool = True
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _SpyTarget:
    """Records ``run`` / ``write_file`` calls and returns benign results.

    ``primary_group`` is what ``id -gn <admin>`` reports; it defaults to the
    admin username (a private per-user group, the healthy case). A test
    overrides it to a shared group to exercise the post-condition guard.
    """

    def __init__(self, *, primary_group: str | None = None) -> None:
        self.runs: list[str] = []
        self.writes: list[tuple[str, str]] = []
        self._primary_group = primary_group or ADMIN

    def run(self, command: str, **kwargs: object) -> _SpyResult:  # noqa: ARG002
        self.runs.append(command)
        if command.startswith(f"id -gn {ADMIN}"):
            return _SpyResult(stdout=self._primary_group)
        return _SpyResult()

    def write_file(self, path: str, content: str, mode: str = "0644") -> None:  # noqa: ARG002
        self.writes.append((path, content))


class _SpyLogger:
    def __init__(self) -> None:
        self.steps: list[str] = []
        self.warnings: list[str] = []

    def step(self, name: str) -> None:
        self.steps.append(name)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)


# ---------------------------------------------------------------------------
# chmod 0750 + private-group guard
# ---------------------------------------------------------------------------


def test_harden_admin_home_chmods_0750() -> None:
    """The helper runs ``chmod 0750`` on the admin home. No sudo prefix: the
    admin owns its own home (contrast the agent path, where the admin transport
    chmods a different user's home and needs sudo)."""
    target = _SpyTarget()
    _harden_admin_home(target, home=HOME, admin_username=ADMIN, logger=_SpyLogger())

    assert f"chmod 0750 {HOME}" in target.runs
    assert not any(cmd.startswith("sudo") and "chmod 0750" in cmd for cmd in target.runs)


def test_harden_admin_home_chmod_precedes_group_check() -> None:
    """The ``chmod`` lands before the ``id -gn`` guard: the guard only reports
    drift, it does not gate the tightening."""
    target = _SpyTarget()
    _harden_admin_home(target, home=HOME, admin_username=ADMIN, logger=_SpyLogger())

    chmod_idx = next(i for i, c in enumerate(target.runs) if c.startswith(f"chmod 0750 {HOME}"))
    guard_idx = next(i for i, c in enumerate(target.runs) if c.startswith(f"id -gn {ADMIN}"))
    assert chmod_idx < guard_idx


def test_harden_admin_home_shared_primary_group_warns() -> None:
    """When the admin's primary group is shared (``id -gn`` != username), the
    guard surfaces a warning rather than silently leaving a 0750 home that other
    group members can read."""
    target = _SpyTarget(primary_group="users")
    logger = _SpyLogger()
    _harden_admin_home(target, home=HOME, admin_username=ADMIN, logger=logger)

    assert any("primary group is 'users'" in w for w in logger.warnings)
    assert any("cannot be made private" in w for w in logger.warnings)


def test_harden_admin_home_private_primary_group_is_quiet() -> None:
    """The healthy case (``id -gn`` == username) raises no group warning."""
    target = _SpyTarget()
    logger = _SpyLogger()
    _harden_admin_home(target, home=HOME, admin_username=ADMIN, logger=logger)

    assert not any("primary group" in w for w in logger.warnings)


# ---------------------------------------------------------------------------
# umask 027 in the admin profile fragment
# ---------------------------------------------------------------------------


def test_admin_profile_carries_umask_027() -> None:
    """The managed profile fragment carries ``umask 027`` so files the admin
    writes outside a workspace default to owner-only. Emitted by
    ``_write_agentworks_profile`` itself, so it survives the reinit rewrite that
    clears/rebuilds the PATH exports."""
    target = _SpyTarget()
    _write_agentworks_profile(target, path_additions=["/opt/bin"], logger=_SpyLogger())

    bodies = [content for path, content in target.writes if AGENTWORKS_PROFILE in path]
    assert bodies, "expected a write to the managed profile fragment"
    assert all("umask 027" in body for body in bodies)


def test_admin_profile_umask_present_even_with_no_paths() -> None:
    """Reinit safety: the umask line is present even when there are no PATH
    additions (an operator who removed all install commands still gets it)."""
    target = _SpyTarget()
    _write_agentworks_profile(target, path_additions=[], logger=_SpyLogger())

    bodies = [content for path, content in target.writes if AGENTWORKS_PROFILE in path]
    assert bodies and all("umask 027" in body for body in bodies)


# ---------------------------------------------------------------------------
# bootstrap useradd -U (create-time private primary group)
# ---------------------------------------------------------------------------


def test_bootstrap_useradd_forces_private_group() -> None:
    """The create-time bootstrap ``useradd`` carries ``-U`` so the admin's
    primary group is a per-user private group regardless of the image's
    ``USERGROUPS_ENAB``; without it, the 0750 home could leak to a shared
    primary group."""
    script = generate_bootstrap_script(
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA testkey",
        provisioning_packages=["curl"],
        tailscale_auth_key="tskey-auth-test123",
        hostname="lima--myvm",
    )

    assert 'useradd -m -U -s /bin/bash "$VM_USER"' in script

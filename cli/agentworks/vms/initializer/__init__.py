"""VM lifecycle: provisioning (one-time) and initialization (repeatable).

Two phases:
  A. Provisioning (over provisioning transport): bootstrap, SSH key, Tailscale join.
     One-time, platform-specific, pass/fail. Tracked via provisioning_status.
  B. Initialization (over Tailscale SSH): packages, install commands, git credentials,
     dotfiles. Repeatable via `vm reinit`. Tracked via init_status.

Phase A steps are fatal: if they fail, the VM is unreachable and useless.
Phase B steps are non-fatal: failures produce warnings and a 'partial' status.

This package preserves the flat ``agentworks.vms.initializer`` import
surface that predates the split into submodules (``shell_env``, ``mise``,
``ssh_keys``, ``packages``, ``credentials``, ``driver``): every name below
is re-exported here so ``from agentworks.vms.initializer import
bootstrap_vm`` (and the many ``agentworks.vms.initializer.<name>``
attribute references across the codebase and test suite) keep working
unchanged. Unlike the sibling ``vms.manager`` package, no cross-submodule
call here needs package-object indirection: the internal names that ARE
monkeypatched (e.g. ``bootstrap_vm``, ``run_initialization``) are only
ever patched on the ``vms.manager`` namespace (its reinit path reads them
via that package object), never on ``vms.initializer`` itself, so plain
cross-submodule imports (``from .shell_env import ...`` etc, inside
``driver.py``) are sufficient. If a future test needs to patch a name on
``agentworks.vms.initializer`` and have an internal caller observe it,
that caller must switch to package-object indirection.
"""

from __future__ import annotations

from .credentials import (
    _configure_git_credentials,
    _join_tailscale,
    announce_git_credentials,
    rejoin_tailscale,
    resolve_git_credential_providers,
    verify_tailscale_available,
)
from .driver import (
    _phase_a_bootstrap,
    _phase_b_setup,
    _run_bootstrap_script,
    bootstrap_vm,
    install_claude_plugins,
    run_initialization,
)
from .mise import (
    MISE_ACTIVATE_LINES,
    MISE_GPG_KEY_PATH,
    MISE_GPG_KEY_URL,
    MISE_SOURCE_FILE,
    MISE_SOURCE_LINE,
)
from .packages import (
    _configure_apt_sources,
    _install_apt_packages,
    _run_install_commands,
)
from .shell_env import (
    AGENTWORKS_IDENTITY_PROFILE_PATH,
    AGENTWORKS_PROFILE,
    AGENTWORKS_RC,
    AGENTWORKS_SSHD_ACCEPT_ENV_PATH,
    AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH,
    AGENTWORKS_SUDOERS_ENV_KEEP_PATH,
    AGENTWORKS_SUDOERS_ENV_KEEP_PATTERNS,
    AGENTWORKS_ZPROFILE_PATH,
    SKEL_BASHRC_PATH,
    SKEL_ZSHRC_PATH,
    _ensure_agentworks_files_sourced,
    _harden_admin_home,
    _write_agentworks_identity_profile,
    _write_agentworks_profile,
    _write_agentworks_rc,
    _write_skel_seeds,
    _write_sshd_accept_env,
    _write_sudoers_console_setenv,
    _write_sudoers_env_keep,
)
from .ssh_keys import AUTHORIZED_KEYS_HEADER, _apply_sve_mask, _preserve_ssh_host_keys, _reconcile_authorized_keys

__all__ = [
    "AGENTWORKS_IDENTITY_PROFILE_PATH",
    "AGENTWORKS_PROFILE",
    "AGENTWORKS_RC",
    "AGENTWORKS_SSHD_ACCEPT_ENV_PATH",
    "AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH",
    "AGENTWORKS_SUDOERS_ENV_KEEP_PATH",
    "AGENTWORKS_SUDOERS_ENV_KEEP_PATTERNS",
    "AGENTWORKS_ZPROFILE_PATH",
    "AUTHORIZED_KEYS_HEADER",
    "MISE_ACTIVATE_LINES",
    "MISE_GPG_KEY_PATH",
    "MISE_GPG_KEY_URL",
    "MISE_SOURCE_FILE",
    "MISE_SOURCE_LINE",
    "SKEL_BASHRC_PATH",
    "SKEL_ZSHRC_PATH",
    "_apply_sve_mask",
    "_configure_apt_sources",
    "_configure_git_credentials",
    "_ensure_agentworks_files_sourced",
    "_harden_admin_home",
    "_install_apt_packages",
    "_join_tailscale",
    "_phase_a_bootstrap",
    "_phase_b_setup",
    "_preserve_ssh_host_keys",
    "_reconcile_authorized_keys",
    "_run_bootstrap_script",
    "_run_install_commands",
    "_write_agentworks_identity_profile",
    "_write_agentworks_profile",
    "_write_agentworks_rc",
    "_write_skel_seeds",
    "_write_sshd_accept_env",
    "_write_sudoers_console_setenv",
    "_write_sudoers_env_keep",
    "announce_git_credentials",
    "bootstrap_vm",
    "install_claude_plugins",
    "rejoin_tailscale",
    "resolve_git_credential_providers",
    "run_initialization",
    "verify_tailscale_available",
]

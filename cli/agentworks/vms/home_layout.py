"""What Agentworks keeps in an account's home, the admin's or an agent's, as paths relative to it.

A dotfiles destination may not be, hold, or lie inside any path in
:data:`MANAGED` (``sources.normalize_destination``), since replacing it would
replace what Agentworks put there.
"""

AGENTWORKS_PROFILE = ".agentworks-profile.sh"
"""The login profile fragment (``vms.initializer.shell_env``)."""

AGENTWORKS_RC = ".agentworks-rc.sh"
"""The interactive rc fragment (``vms.initializer.shell_env``)."""

SSH_DIR = ".ssh"
"""The directory of ``authorized_keys`` (``vms.initializer.ssh_keys``)."""

MISE_CONFIG_DIR = ".config/mise"
"""The directory of the mise config and lockfile (``vms.initializer.mise``).

Not in :data:`MANAGED`: dotfiles may own ``~/.config``, and initialization
writes the mise config after the dotfiles sync, so the order settles it.
"""

GIT_CREDENTIAL_STATE_DIR = ".agentworks"
"""The directory of the git credential state; ``git_credentials``' scripts name it as ``$HOME/.agentworks``."""

AGENTWORKS_ARTIFACTS = ".agentworks-artifacts"
"""The directory of user and session artifacts (``artifacts.session``, ``harness_integration.shell``)."""

MANAGED = (AGENTWORKS_PROFILE, AGENTWORKS_RC, SSH_DIR, GIT_CREDENTIAL_STATE_DIR, AGENTWORKS_ARTIFACTS)
"""The paths a dotfiles destination may not be, hold, or lie inside."""

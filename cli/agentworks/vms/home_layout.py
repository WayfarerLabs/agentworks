"""What Agentworks keeps in an account's home, the admin's or an agent's, as paths relative to it.

A dotfiles destination may be none of these and contain none of them
(``sources.normalize_destination``), since replacing it would replace what
Agentworks put there.
"""

AGENTWORKS_PROFILE = ".agentworks-profile.sh"
"""The login profile fragment (``vms.initializer.shell_env``)."""

AGENTWORKS_RC = ".agentworks-rc.sh"
"""The interactive rc fragment (``vms.initializer.shell_env``)."""

SSH_DIR = ".ssh"
"""The directory of ``authorized_keys`` (``vms.initializer.ssh_keys``)."""

MISE_CONFIG_DIR = ".config/mise"
"""The directory of the mise config and lockfile (``vms.initializer.mise``)."""

GIT_CREDENTIAL_STATE_DIR = ".agentworks"
"""The directory of the git credential state; ``git_credentials``' scripts name it as ``$HOME/.agentworks``."""

MANAGED = (AGENTWORKS_PROFILE, AGENTWORKS_RC, SSH_DIR, MISE_CONFIG_DIR, GIT_CREDENTIAL_STATE_DIR)
"""Every path above."""

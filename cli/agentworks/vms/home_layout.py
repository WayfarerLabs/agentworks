"""What Agentworks keeps in an account's home, the admin's or an agent's, as paths relative to it.

A dotfiles destination may not be, hold, or lie inside any path in its
account's set, :data:`MANAGED` for an agent and :data:`ADMIN_MANAGED` for the
admin (``sources.normalize_destination``), since replacing it would replace
what Agentworks put there.
"""

AGENTWORKS_PROFILE = ".agentworks-profile.sh"
"""The login profile fragment (``vms.initializer.shell_env``)."""

AGENTWORKS_RC = ".agentworks-rc.sh"
"""The interactive rc fragment (``vms.initializer.shell_env``)."""

SSH_DIR = ".ssh"
"""The directory of ``authorized_keys`` (``vms.initializer.ssh_keys``)."""

AGENTWORKS_STATE_DIR = ".agentworks"
"""Agentworks' own state directory: the git credential state and the codex recorder live in it."""

GIT_CREDENTIAL_STATE_DIR = AGENTWORKS_STATE_DIR
"""The directory of the git credential state (``git_credentials``), rendered into its scripts and config.

Its include path is deployed into every account's ``~/.gitconfig``, so
changing it means retiring the old include as well, as
``git_credentials.reconcile`` retires the paths of earlier layouts.
"""

AGENTWORKS_ARTIFACTS = ".agentworks-artifacts"
"""The directory of user and session artifacts (``artifacts.session``, ``harness_integration.shell``)."""

TMUXINATOR_CONFIG_DIR = ".config/tmuxinator"
"""Where each workspace's tmuxinator config is linked by console session name (``workspaces``).

The links live in the admin's home only, so the directory is in
:data:`ADMIN_MANAGED` and not in :data:`MANAGED`.
"""

MISE_CONFIG_DIR = ".config/mise"
"""The directory of the mise config and lockfile (``vms.initializer.mise``).

Not in :data:`MANAGED`: dotfiles may own or override the mise config. For
the admin and every agent alike, initialization writes the config before the
dotfiles sync and fetches the lockfile and installs after it
(``docs/guides/mise.md``).
"""

MANAGED = (AGENTWORKS_PROFILE, AGENTWORKS_RC, SSH_DIR, AGENTWORKS_STATE_DIR, AGENTWORKS_ARTIFACTS)
"""The paths every account's dotfiles destination may not be, hold, or lie inside: an agent's set."""

ADMIN_MANAGED = (*MANAGED, TMUXINATOR_CONFIG_DIR)
"""The admin's set: :data:`MANAGED` and the workspace tmuxinator links."""

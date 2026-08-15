The `install-command` system plugin owns Agentworks' shipped optional user install-command catalog.
Core still owns the `user-install-command` kind, validation, idempotent execution, and admin or
agent initialization.

The plugin is installed but disabled by default. Enable it only when an admin or agent template
selects a shipped command. Define a YAML resource yourself when you need a command the catalog does
not own.

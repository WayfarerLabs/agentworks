---
description: Understand and optionally enable the shipped user install-command catalog.
---

# Optional install-command catalog

The `install-command` system plugin owns Agentworks' optional user install-command catalog. Core
owns the `user-install-command` kind, validation, idempotent execution, and admin or agent
initialization. Define an operator-owned YAML resource when the catalog does not own the command.

Use `agw resource list --kind user-install-command --include-disabled --output json` to inspect its
rows. Prefer apt, snap, or mise fields when they fit. Otherwise, select only a command whose check
or operation is repeat-safe. If a selected template needs a shipped command, state that enabling the
plugin edits only `[plugins].system` in the chosen config and preserves existing entries. If
authorized, add `install-command`, then rerun the inventory. If declined, leave the config unchanged
and use an operator-owned resource or omit the dependency.

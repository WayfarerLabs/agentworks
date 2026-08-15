Use the System plugins roster to discover the installed `install-command` plugin and its configured
state. The resource inventory and resource descriptions show its catalog rows, including disabled
rows when you ask for them. After authorization, enable it exactly by adding `install-command` to
`[plugins].system` while preserving existing entries, then use the separate verification action.

These commands run for the user selected by the admin or agent template and can run again during
reinitialization. Prefer apt, snap, or mise fields when they fit. Otherwise, select only a command
whose completion check or command itself is repeat-safe. The next safe action is to review the
listed catalog and choose the enablement action only for a command your template needs.

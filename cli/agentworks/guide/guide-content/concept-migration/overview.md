Agentworks 0.14 moved resource declarations out of `config.toml` and into YAML manifests. This topic
covers that exceptional resource-model migration. It is not a general upgrade checklist, and it does
not provide a migrator or a frozen copy of an older schema.

This resource rewrite is distinct from the automatic SQLite state-schema migration. A normal command
announces stale state and offers or automatically creates a pre-migration database snapshot before
changing it; `agw doctor` only inspects. Restore a schema-compatible snapshot before a downgrade,
and refresh generated shell code with `agw completion install` after upgrading.

Work from the installed model. `agw resource sample KIND` provides a declarable manifest shape, and
`agw resource describe-kind KIND` or `agw resource describe-kind KIND/NAME` provides the current
field reference. Keep an untouched backup and the expected resource identities until the normal
inventory and doctor both prove the cutover.

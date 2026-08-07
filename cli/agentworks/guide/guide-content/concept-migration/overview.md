Agentworks 0.14 moved resource declarations out of `config.toml` and into YAML manifests. This topic
covers that exceptional resource-model migration. It is not a general upgrade checklist, and it does
not provide a migrator or a frozen copy of an older schema.

Work from the installed model. A bare kind topic, such as `agw guide vm-template`, includes its live
sample and field reference. A capability implementation topic, such as `agw guide vm-platform/lima`,
describes that implementation's tagged configuration. Keep an untouched backup and the expected
resource names until the normal inventory and doctor both prove the cutover.

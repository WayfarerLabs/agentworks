# The capability model

This design was promoted to a permanent home once a second capability validated it:

**[`cli/agentworks/capabilities/README.md`](../../../cli/agentworks/capabilities/README.md)**

That is the contract every capability implements (the lifecycle: `validate_config`, construct,
`preflight`, `verify`, ops; the `disabled_reason` tier; where capabilities live). The `vm-platform`
/ `vm-site` pair drove its first implementation in this SDD; `git-credential-provider` validated it
as the second capability, which triggered the promotion. See the permanent doc for the current
contract; this pointer is all that remains here.

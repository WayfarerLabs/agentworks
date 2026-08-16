Use `agw resource kinds --output json` for the installed kind vocabulary. Use
`agw resource list --kind KIND --include-disabled --output json` for current registered members,
origins, enablement, and readiness. Use the applicable operational list or describe command for
stored VM, workspace, Agentworks-managed agent, session, console, and secret facts.

Use live JSON facts for current state: `agw resource list --output json`,
`agw graph show KIND/NAME --output json`, and the applicable VM, workspace, Agentworks-managed
agent, session, console, or secret list and describe command. Use `agw GROUP --help` for the current
group surface and `agw GROUP COMMAND --help` for exact operation syntax. The stable built-in groups
are `config`, `resource`, `vm`, `workspace`, `agent`, `session`, `console`, and `secret`. Their
Typer help is the command authority; this topic does not copy a command registry or recipe catalog.

Create and change declarable resources through their owning CLI commands or canonical manifests,
then read the matching command-owned JSON facts to confirm the projected state. Discover a
capability in the resource list before adopting it. Disabled and not-ready implementations are
facts, not automatic enablement instructions. Configuration and VM or session operation are one
assistance surface: choose the smallest current CLI operation that satisfies the operator's goal and
verify its result.

After an upgrade, resolve emitted deprecation instructions before changing unrelated state. For a
failure, begin with the framed error and projected readiness. Run `agw doctor --output json` when
workstation examination is covered by the current envelope. Doctor is evidence, not authorization
for a repair. Use `concept-migration` only for exceptional breaking-input conversion.

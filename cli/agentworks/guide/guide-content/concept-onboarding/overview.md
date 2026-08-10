Agentworks separates declared resources, capability implementations, and live instances. This topic
is the repeatable first-setup and current-adoption view. Begin with the live inventory below, report
what is already done, and choose the smallest action that advances the operator's goal. Use
`concept-release-notes` instead for changes between versions; current facts are not a historical
delta.

## Security disclosure

The Agentworks assistant agent runs on the intended workstation and may inspect files and execute
commands with the workstation account's permissions. That is not root access; privilege elevation is
separate. It can also reach Agentworks-managed resources, secret references, and SSH destinations
reachable from the workstation. Use the strictest practical harness approval, visibility, and
sandbox posture that preserves the requested workstation access. State this disclosure once at
assistance startup, then use the durable authorization envelope described above.

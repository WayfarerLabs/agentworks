---
description: Narrow an Agentworks failure to configuration, readiness, state, or connectivity.
index-order: 40
---

# Troubleshooting

Start with the framed error from the command that failed. It should identify the affected operation
and provide a useful next step without exposing secrets.

Use `agw doctor` for a broad workstation, configuration, dependency, and database check. Then move
to the smallest owning surface:

- `agw resource list --kind KIND --include-disabled` for enablement and readiness;
- `agw resource explain KIND/NAME` for one capability's requirements;
- `agw GROUP describe NAME` for recorded instance state;
- `agw vm verify-connection NAME` for an explicit VM connectivity check; and
- `agw GROUP COMMAND --help` for the current repair or retry options.

Change one thing at a time and rerun the narrow check that motivated it. Preserve the original error
and a redacted reproduction if the problem needs to be reported through
`agw guide show concept-reporting-bugs`.

<!-- agw:agent-only -->

Summarize the evidence and propose the smallest relevant repair. Keep the proposal tied to the
failure being investigated rather than treating diagnostics as a general setup checklist.

<!-- /agw:agent-only -->

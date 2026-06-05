# VM-Scoped Agents -- Lockfile

## 2026-03-31

All artifacts in this feature directory (FRD, HLA, plan) were implemented and verified as of this
date. The implementation matches the specs with the following notable design decisions made during
development:

- Agent usernames use `agt--<name>` (double hyphen prefix) rather than the original
  `<workspace>--<agent>` convention from the earlier workspace-scoped model.
- Workspace groups use `ws--<name>` (double hyphen) for consistency with the agent naming.
- Workspace directories live under `/opt/agentworks/workspaces/` (configurable) rather than the
  admin user's home directory, with mode 2770 and default ACLs for group-writable files.
- The grant/deny CLI was implemented as a `workspace-grants` subcommand group
  (`agent workspace-grants grant/deny/list`) rather than top-level `agent grant-workspaces` and
  `agent deny-workspaces` commands.
- `workspace repair` was added to reconcile infrastructure (group naming, permissions, ACLs, agent
  group membership) for existing workspaces created before the VM-scoped model.
- ADR-0006 (workspace-scoped agents) was superseded by ADR-0010 (VM-scoped agents with workspace
  grants).

These specs are accurate as of this date but are now locked and will not be updated to reflect
further changes to the implementation.

## 2026-06-01

The grant/deny CLI shape described above was flattened. The `agent workspace-grants` subgroup is
gone; commands now sit directly on `agent` as `agent grant-workspace` and `agent revoke-workspace`,
matching the resource/verb-object pattern used elsewhere in the CLI. The `list` subcommand was
dropped since `agent describe` already shows grants. Service-layer rename: `deny_workspaces` is now
`revoke_workspaces`. See PR #74 and the `.rulesync/rules/cli-conventions.md` rule it introduced.

The `workspace repair` command described above was renamed to `workspace reinit` to match the
existing `vm reinit` / `agent reinit` shape. The behavior is unchanged (idempotent forward-only
reconciliation of live VM state against the DB); the rename reflects that this is the same
declarative-reinit semantic the rest of the platform already uses. Service-layer rename:
`repair_workspace` is now `reinit_workspace`.

## 2026-06-03

The grant/revoke verbs were pluralized back to `agent grant-workspaces` and
`agent revoke-workspaces`. The earlier 2026-06-01 rename to singular was justified by "matching the
resource/verb-object pattern used elsewhere," and that elsewhere-pattern (`console add-session`,
`console remove-session`) has since been flipped to plural to honestly reflect their variadic shape.
The cli-conventions rule now codifies "pluralize when variadic, singular when single object," and
these two commands are variadic. Service-layer function names are unchanged (`grant_workspaces` /
`revoke_workspaces` were always plural).

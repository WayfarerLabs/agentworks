---
description: Adopt desired runtime intent after upgrade and migrate retired resource settings.
index-order: 60
---

# Migration

## Adopt desired runtime intent

Schema migration 39 preserves the old VM policy: manually stopped becomes stopped, and permitted
automatic activation becomes on-demand. Existing sessions and named consoles have no target until
the operator adopts them; old PIDs, timestamps, or current absence do not establish intent. New
creates record running, and explicit VM start now records running even when already live. Use
`agw vm set-desired-state NAME on-demand` when returning to permission-only activation is intended.

Inspect local inventory, establish each requested target from known operator intent, then use the
owning `set-desired-state` command. Preview the authorized recovery scope with
`agw vm reconcile NAME --dry-run`; execute it only when runtime changes for that scope are intended.
If intent is unknown, leave targets unset and report what is missing. Session and console start/stop
can also adopt accepted targets, including ordinary batch no-ops. Runtime exit never clears a
target.

**Breaking batch behavior:** session start/restart now gathers all eligible workload secrets before
changing any selected runtime. One required workload secret failure prevents the entire deferred
batch instead of leaving earlier sessions restarted. Required VM activation may already have
happened and is not rolled back. An infrastructure credential failure blocks only dependent families
unless an eligible workload also requires that credential. Accepted targets survive later failure or
cancellation.

On WSL2, live stopped-intent guests receive no idle protection during access or management commands
and may stop mid-command. Use `vm set-desired-state NAME on-demand` or `running` only when both
normal holds and future activation are intended. This is a policy change, not a keepalive-only
switch.

Keep the normal pre-migration database snapshot. An older binary refuses the newer schema; rollback
requires a full compatible pre-migration backup and the matching release. Restore does not undo
external runtime changes. Use `agw database --help` for backup and restore operations and
`agw guide show concept-management` for recovery signposts. Refresh completions after upgrading.

## Retired resource model

Agentworks 0.14 moved resource declarations out of `config.toml` and into YAML manifests. This is an
exceptional resource-model migration, not a general upgrade checklist or an automated migrator. Work
from the installed model: `agw resource sample KIND` provides a manifest shape, and
`agw resource explain KIND` or `agw resource explain KIND/NAME` provides its field reference.

## Keep state-database recovery separate

The resource-model procedure below backs up config and resource manifests. It does not replace the
automatic snapshot offered before a SQLite state-schema migration. A normal command owns that
migration and its recovery instructions; `agw doctor` only inspects. Restore a schema-compatible
snapshot before a downgrade, and refresh generated completions with `agw completion install` after
upgrading.

## Inventory and preserve inputs

Before editing, read only the selected `config.toml` and existing resources directory. Record each
pre-existing manifest and each manifest-producing retired section as canonical `kind/name`,
`operator-declared`, and its existing or operator-selected manifest path. Collapse nested tables
into their parent and exclude `[secret_backends.*]`, which has no one-for-one manifest. Freeze that
complete identity set before backup.

Before editing, make separate untouched backups of the config and resource directory at fresh
operator-selected destinations outside both active trees. Record every source and destination. If
the resources directory is absent, record that baseline instead of creating one. If the operator
declines either backup, or if one cannot be verified byte-for-byte, stop before editing and leave
the active files unchanged.

## Rewrite one resource at a time

Use these release-history mappings only to find the current command-owned model:

| Retired section                    | Manifest kind                            |
| ---------------------------------- | ---------------------------------------- |
| `[secrets.*]`                      | `secret`                                 |
| `[vm_templates.*]`                 | `vm-template`                            |
| `[agent_templates.*]`              | `agent-template`                         |
| `[workspace_templates.*]`          | `workspace-template`                     |
| `[session_templates.*]`            | `session-template`                       |
| `[git_credentials.*]`              | `git-credential`                         |
| `[admin.config]` and `[admin.env]` | `admin-template` named `default`         |
| `[named_console]`                  | `named-console-template` named `default` |
| `[azure]` and `[proxmox]`          | `vm-site`                                |
| `[apt_sources.*]`                  | `apt-source`                             |
| `[apt_packages.*]`                 | `apt-package`                            |
| `[system_install_commands.*]`      | `system-install-command`                 |
| `[user_install_commands.*]`        | `user-install-command`                   |
| `[secret_backends.*]`              | No one-for-one manifest                  |

For an ordinary named section, the suffix becomes `metadata.name`, `description` moves to
`metadata.description`, and remaining values move under `spec`. Tagged capabilities must follow
their live `resource explain` output.

Edit only one pre-recorded manifest path at a time and do not change the frozen identity set. Record
the file and expected resulting identity before each edit. Preserve the last verified files if the
operator declines an edit. The current field reference owns details, including authentication,
placement, and git-token shapes; do not infer them from this migration summary.

## Cut over and verify

Non-default secret backends become operator-named `secret-source` manifests selected through
`[secret_config].sources`; the synthesized `env-var` and `prompt` sources need no manifest. Draft
and review every manifest before removing retired sections from `config.toml` in one explicit edit.
If that cutover is declined, preserve the old config and backups and stop.

After cutover, run `agw doctor --output json`. Require JSON contract version 1, command `doctor`, a
valid configuration and manifest set, zero failures, and a successful database schema check. Then
run `agw resource list --origin operator --output json` and compare kind, name, origin variant, and
manifest path with the frozen identity set. Ignore source-line movement. Any missing, extra, or
wrongly originated row stops completion and returns to the backups for investigation. Do not retry a
mutation automatically.

Continue with `concept-management` after the exact inventory and final doctor result pass.

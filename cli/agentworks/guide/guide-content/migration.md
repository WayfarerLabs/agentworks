---
description: Migrate retired Agentworks resource settings into current declarative manifests.
index-order: 60
---

# Resource model migration

Agentworks 0.14 moved resource declarations out of `config.toml` and into YAML manifests. This is an
exceptional resource-model migration, not a general upgrade checklist or an automated migrator. Work
from the installed model: `agw resource sample KIND` provides a manifest shape, and
`agw resource explain KIND` or `agw resource explain KIND/NAME` provides its field reference.

## Keep state-database recovery separate

This procedure backs up config and resource manifests. It does not replace the automatic snapshot
offered before a SQLite state-schema migration. A normal command owns that migration and its
recovery instructions; `agw doctor` only inspects. Restore a schema-compatible snapshot before a
downgrade, and refresh generated completions with `agw completion install` after upgrading.

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

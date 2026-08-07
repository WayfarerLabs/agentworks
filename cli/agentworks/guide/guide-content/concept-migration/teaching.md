## Preserve the migration evidence

Before editing, preserve untouched copies of the selected `config.toml` and resources directory.
Read the precise load error, inventory every retired TOML resource section it names, and record the
expected resource names from those untouched sections. An inventory captured before upgrading can be
useful additional evidence, but the procedure does not assume one exists.

## Rewrite one resource at a time

Map each retired section family to the kind named by the load error. Use `agw resource sample KIND`
or `agw guide KIND` for a declarable manifest shape. Use `agw resource describe-kind KIND/NAME` or
`agw guide KIND/NAME` for the tagged configuration of a capability implementation. The installed
schema is authoritative. This topic deliberately does not copy its fields.

The release-history mapping is:

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
| `[secret_backends.*]`              | No manifest                              |

For an ordinary named section, the section suffix becomes `metadata.name`, `description` moves to
`metadata.description`, and the remaining values move under `spec`. Tagged capabilities need the
live implementation reference: `[azure]` selects `spec.platform.name: azure-vm`, `[proxmox]` selects
`spec.platform.name: proxmox`, and a legacy git credential provider moves under the tagged
`spec.provider` table.

Write one manifest at a time while leaving every retired TOML section in place. Run `agw doctor`
after each edit. Its degraded configuration path validates the growing manifest set while continuing
to report the retired-section failure. Fix closed-world fields, strict types, non-nullable nulls,
and retired sibling capability shapes from that precise error and the live field reference.

## Review changed secret references

Inspect the site manifests for `token_secret`, `service_principal.secret`, and
`credentials.access_key_secret`. For all three fields, omission and explicit null both select the
default secret name. A custom string selects that named secret.

Azure and AWS also support ambient authentication: remove the enclosing `service_principal` or
`credentials` block, respectively. Proxmox has no no-secret mode, so its token reference must use
the default name or a custom name.

## Cut over once

`[secret_backends.*]` is the one retired section family with no manifest replacement. Delete those
empty declarations during the final TOML cutover, and activate desired backends through
`[secret_config].backends`.

After every new manifest has passed its per-manifest doctor loop, remove all retired resource
sections from `config.toml` in one edit. Run `agw resource list --origin operator --names-only` and
compare the result with the preserved names. Any missing or extra resource restores the untouched
backup before further work. Finish only when a final `agw doctor` reports zero failures.

## Inventory and preserve the migration evidence

Before any backup or edit, read only the selected `config.toml` and the resources directory when it
exists. Record every pre-existing manifest as canonical `kind/name`, `operator-declared` origin
variant, and manifest file path. For each manifest-producing retired section, record its canonical
`kind/name`, the same origin variant, and an operator-chosen intended manifest file. Collapse nested
tables into their parent resource. Exclude `[secret_backends.*]`, which produces no manifest. Omit
source lines because edits to a multi-document file can shift them without changing origin. The
caller owns this complete union as immutable expected identities. An inventory captured before
upgrading can be useful additional evidence, but the procedure does not assume one exists.

Next, preserve the selected `config.toml` and resources directory as separate untouched backups at
fresh operator-selected destinations. Each destination must be distinct from its source and outside
the active config and resources trees. Treat each copy as its own mutation boundary. A pure-TOML
installation can have no resources directory; record an explicit absent resources baseline without
creating a directory in that case.

Before editing, use a separate read boundary to verify that the config backup matches its source
byte for byte and that the resources backup contains exactly the same paths and file bytes as its
source, or that both sides match the explicit absent baseline. Validate the complete expected
identities against those untouched sources and the pre-recorded intended TOML paths. Verification
does not add, remove, or change an entry.

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

Write one manifest at a time at its pre-recorded intended path while leaving every retired TOML
section in place. The edit must consume its existing expected identity without changing the
baseline. Use the manifest kind's sample and field reference. When the manifest contains tagged
capability configuration, use that implementation's separate `kind/name` field reference. Run
`agw doctor` after each edit. Its degraded configuration path validates the growing manifest set
while continuing to report the retired-section failure. Fix closed-world fields, strict types,
non-nullable nulls, and retired sibling capability shapes from that precise error and the live field
reference.

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
sections from `config.toml` in one edit. Run `agw resource list --origin operator` and compare the
result with the caller-owned expected identities by `kind/name`, operator-declared origin variant,
and intended manifest file path, ignoring source line. Any missing, extra, or wrongly originated
resource returns to the untouched backups for investigation. Finish only when a final `agw doctor`
reports zero failures.

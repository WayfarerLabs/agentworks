# Native harness setup

Activated Claude Code and Codex user facets configure that actual user's marketplaces, plugins, and
settings. The same integration activation model serves administrators and agents. Activated
workspace facets map project settings. They do not install workspace plugins. Integration activation
must be explicit, including when all configuration fields use defaults.

Native setup requires `python3` on the VM for guarded file access and atomic publication. Add
`python3` to the VM template's `apt_packages` and run `vm reinit` before user or workspace setup. A
missing interpreter is reported before staging or native mutation. The harness CLI itself must also
be installed through the existing package or install-command configuration.

## Settings mappings

A `settings` mapping requires a workstation `source` and a `strategy`. The source is a local file
path, optionally prefixed with `file::`; relative paths and `~` resolve on the workstation running
Agentworks. The integration snapshots and validates its bytes before writing native settings or
installing plugins. Later source changes cannot alter that operation's captured input. Reinit takes
a fresh snapshot; session launch does not need the workstation source.

| Integration | User role                 | Workspace role          | Format      |
| ----------- | ------------------------- | ----------------------- | ----------- |
| Claude Code | `~/.claude/settings.json` | `.claude/settings.json` | JSON object |
| Codex       | `~/.codex/config.toml`    | `.codex/config.toml`    | TOML table  |

User roles respect `CLAUDE_CONFIG_DIR` and `CODEX_HOME` in the prepared setup environment. Overrides
must be absolute guest paths without `.` or `..` components. Workspace destinations stay under the
workspace root regardless of user overrides. These are fixed settings roles, not arbitrary guest
destinations. Source paths embedded inside the document are copied as values without rewriting.

| Strategy          | Existing destination                                                  |
| ----------------- | --------------------------------------------------------------------- |
| `replace`         | Replace the complete document; remove destination-only keys.          |
| `merge-overwrite` | Recursively merge tables and objects; source values win collisions.   |
| `merge-preserve`  | Recursively merge tables and objects; existing values win collisions. |
| `skip-existing`   | Leave the existing file unchanged.                                    |

All strategies create a missing destination from the source. Arrays are atomic; scalar/table type
collisions take the strategy's winning value. Source syntax must be valid even when skipping an
existing file. Merge also requires valid destination syntax. Settings-only replacement can repair an
invalid destination; plugin reconciliation requires trustworthy native inventory before any write
and refuses malformed existing settings. Repair those settings separately first.

Serialization preserves values, not comments or formatting. JSON duplicate keys and non-finite
numbers are rejected. Destinations must be regular files reached without traversing symbolic links.
Publication uses a private sibling temporary file, checks the observed destination hash, and renames
atomically. Traversal respects search-only ancestors. If the settings change during plugin work
outside the planned native keys, mapping publication refuses and retains the recorded plugin prefix
for retry. User files are private; workspace files are writable by the workspace group. Permission
failures do not trigger elevation.

Use non-secret settings sources. Authentication files are not supported settings roles. Captured
contents and native command output are transferred privately and are not stored in receipts or logs.
Removing a mapping retains the native file and its current values, reports that retention, and
relinquishes the mapping's claims. It does not restore overwritten values. Workspace mapping changes
follow workspace creation; applying a new mapping requires workspace recreation.

## Marketplaces and plugins

User configuration has `marketplaces` and `plugins` lists, both empty by default. Marketplace
strings use the installed harness's native source grammar. Local marketplace paths refer to the
guest filesystem; relative paths resolve against the actual user's home. Plugin names resolve
against the native catalog; qualify ambiguous names as `plugin@marketplace`.

Tool discovery reads the actual user's login-shell PATH once per setup invocation. An explicit PATH
in the prepared setup environment takes precedence. Native commands use that resolved executable and
keep the prepared environment, so login profiles cannot replace setup identities or explicit values
during execution. Marketplace discovery reuses the same executable under its private HOME and native
config directories. A launcher that depends on the real HOME may fail there; use a standalone CLI
installation or a launcher compatible with an isolated HOME. Agentworks does not bypass isolation or
guess package-manager cache paths to make such a launcher work.

A newly declared marketplace is first resolved in a private temporary native home. This discovers
its native name and validates conflicts before registration in the real user home. Native source
acquisition may therefore happen twice, including two Git fetches. The temporary home is cleaned on
success or handled failure; its contents never count as completed user setup.

Agentworks reconciles explicit plugin settings and mapped settings as one plan. A surviving source
value that disables an explicitly requested plugin or changes its marketplace identity is a config
error before writes. Source values discarded by `merge-preserve` or `skip-existing` create no such
conflict. Plugin commands run first; the final mapping preserves their controlled native keys.
`skip-existing` skips only the mapping, not separately requested plugin work.

Completed native mutations are observed and recorded individually. Repeat setup leaves matching
owned installations in place and enables a requested owned plugin if it is disabled. A native
registration or plugin without an Agentworks ownership receipt is not adopted, even when it matches
the request. Remove the conflicting association with the native CLI before retrying. Source identity
drift also requires native remediation before setup can continue. Unrelated built-in or remote
entries whose source cannot be established are preserved; Agentworks cannot claim or remove those
entries without observable source identity.

Removing desired associations removes owned plugins before owned marketplaces. The integration
refuses marketplace cleanup when it would affect an unowned or project plugin. Claude's native
marketplace removal can cascade into project installations, so this check matters even though
Agentworks installs only user plugins. Claude uninstall retains persistent plugin data; its native
garbage collection owns orphaned cache cleanup. Agentworks does not delete guessed cache paths.

An independently removed marketplace can hide a surviving plugin from native inventory. When the
remaining evidence cannot prove the plugin's ownership, cleanup stays pending with its prior
receipt. Repair or remove the association using the native CLI, then retry. A failure after a native
mutation but before its checkpoint may likewise require native remediation; an unrecorded write is
never silently adopted. Settings retention is intentional and is separate from pending plugin
cleanup.

## Native compatibility evidence

The local fixture suite exercises the actual setup helpers with Codex CLI 0.153.4 and Claude Code
2.1.263. It covers installation, repeat setup, retirement, interrupted checkpoints, settings
conflicts, project dependencies, disabled plugins, source capture, and guarded file publication.
Each fixture uses a dedicated home and empty local marketplace without authentication or model
requests. Native CLI tests skip when the required executable is absent. These fixtures validate the
native boundary; they do not replace tests of Agentworks lifecycle orchestration and remote
transports against supported backends.

Codex inventory handling follows its tagged
[plugin commands](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/cli/src/plugin_cmd.rs)
and
[marketplace commands](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/cli/src/marketplace_cmd.rs).
Native command success is always followed by a fresh observation; unexpected identity payloads
refuse mutation instead of being interpreted as empty inventory.

## Deleting owning resources

Agent and workspace deletion first retires recorded native claims with no desired setup config. It
does not reapply current template settings, resolve setup secrets, or install missing plugins.
Settings mappings relinquish their claims while retaining the document; successful deletion of the
user home or workspace directory subsequently removes that document with its owner.

Unavailable integrations, changed destinations, interrupted cleanup, and remaining owned claims
prevent owner deletion. Agentworks keeps the owner and the latest confirmed claim prefix for retry.
An orphaned workspace with native receipts likewise requires recovery of its VM before deletion.
Native user or directory removal failures propagate instead of discarding the corresponding database
row. A missing agent account is treated as removed only when its home is also absent.

A successful platform VM deletion removes guest-native effects with the VM and clears its family's
instance records in the existing database cascade. If platform binding or deletion fails, recorded
native evidence keeps the VM and child owners available for recovery. Ordinary cleanup of a broken
VM with no native setup evidence retains its existing behavior.

VM, agent, and workspace deletion use the same VM-family mutation guard as setup. A competing
mutation refuses immediately with retry guidance, and nested removal can share its caller's held
guard. This also covers workspace cleanup when the VM row is missing.

Rehome changes a workspace's directory on its current VM and holds that VM's guard across the move.
It refuses a workspace with native setup receipts before probing sessions or changing files;
relocating receipts has no supported contract. Preserve the contents, delete the old workspace with
its integration cleanup, and recreate at the new location. Workspaces without native receipts retain
the existing rehome behavior.

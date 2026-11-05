# Native harness setup

Activated Claude Code and Codex user facets configure that actual user's marketplaces, plugins, and
settings. The same integration activation model serves administrators and agents. Activated
workspace facets map project settings. They do not install workspace plugins. Integration activation
must be explicit, including when all configuration fields use defaults.

Agentworks installs `python3` as a required system package on every VM during create and reinit.
Native setup uses its standard library for guarded file access, atomic publication and artifact
discovery; `python3-venv` and `python3-pip` are not required. Run `agw vm reinit <vm-name>` to
repair an older VM missing the interpreter. A missing interpreter is reported before staging or
native mutation. The harness CLI itself must also be installed through the existing package or
install-command configuration.

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
atomically. Traversal respects search-only ancestors. After plugin commands finish, merge policies
use the current settings and preserve the native associations. Publication refuses if the file
changes again between that read and the atomic write. Publication sets user files to mode `0600` and
workspace files to mode `0660` with the workspace group. Unchanged or skipped files retain their
existing permissions. Permission failures do not trigger elevation.

Use non-secret settings sources. Authentication files are not supported settings roles. Captured
contents and native command output are transferred privately and are not stored in applied-state
records or logs. Removing a mapping retains the native file and its current values. Settings
mappings do not create ownership claims; the setup record tracks completion and the requested
policy. It does not restore overwritten values. Workspace mapping changes follow workspace creation;
applying a new mapping requires workspace recreation. Correct the source before recreating and
preserve work outside the workspace first; see the
[workspace recovery guidance](harness-facets.md#inspecting-and-reconciling-setup).

## Marketplaces and plugins

User configuration has `marketplaces` and `plugins` lists, both empty by default. Marketplace
strings use the installed harness's native source grammar. Local marketplace paths refer to the
guest filesystem; relative paths resolve against the actual user's home. Plugin names resolve
against the native catalog; qualify ambiguous names as `plugin@marketplace`. An ambiguous
unqualified selection is retained and reported without retiring its prior associations. A name
absent from the catalog is a configuration error.

Claude marketplace paths can select a directory or its `marketplace.json` file. A named entry with a
source form Agentworks cannot establish is preserved and reported locally rather than invalidating
unrelated marketplace entries. Malformed overall inventory still fails that facet.

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
registration or user plugin whose observable identity and source match the request is adopted,
recorded and reported, including after an interrupted setup. Differing unowned entries or ambiguous
native drift are preserved and reported individually. Independent entries and later integrations
continue, and the affected integration's setup record remains incomplete until retry succeeds.
Adoption makes the matching entry managed: removing its declaration can remove it under the normal
safe-cleanup rules. Confirmed registrations, installs, removals and replacements report progress.
Descendant session launches still proceed and can repeat the incomplete-setup warning until the
owning setup resolves the retained conflict. Resolve a conflicting association with the native CLI
before retrying its owning setup. Unrelated built-in or remote entries whose source cannot be
established are preserved; Agentworks cannot claim or remove those entries without observable source
identity.

Matching uses the native tool's reported association, including the full marketplace source and any
ref or sparse-path options. Agentworks does not authenticate the historical origin of cached plugin
files. An installed version can change through the native tool without changing the owned plugin
association. A differing unowned source or association is preserved with a warning; resolve that
native conflict before retrying setup.

Claude's git and GitHub list entries do not expose every source option. A missing or malformed user
source declaration, or one inconsistent with native inventory, makes that entry unobservable.
Agentworks retains and names affected selected or owned entries while independent entries continue.
Restore a valid native association before retrying setup. Marketplace retirement or replacement also
retains dependent plugins when the user declaration needed for removal is absent. Agentworks does
not broaden user-scoped removal to all scopes or write a guessed source. Explicitly protected named
operator entries are retained through unrelated native rewrites and mapped-settings publication;
other incomplete placeholders can follow the native CLI's own normalization. Entry preservation
respects native registrations and removals confirmed earlier in the same operation. For example, an
ambiguous plugin selection does not undo a newly registered marketplace; qualify the plugin name
before retrying.

Current-attempt incomplete reasons are retained with setup evidence. `agw artifact show` and managed
session-start warnings identify the retained entry and its reason. Resolve that native or file
conflict before running the owning repair command; repeating setup alone cannot resolve a differing
operator-owned entry. A successful retry clears the previous incomplete reasons.

Removing desired associations removes owned plugins before owned marketplaces. The integration
refuses marketplace cleanup when it would affect an unowned or project plugin. Claude's native
marketplace removal can cascade into project installations, so this check matters even though
Agentworks installs only user plugins. Claude uninstall retains persistent plugin data; its native
garbage collection owns orphaned cache cleanup. Agentworks does not delete guessed cache paths.

An independently removed marketplace can hide a surviving plugin from native inventory. When the
remaining evidence cannot prove the plugin's ownership, cleanup stays pending with its prior
applied-state record. Repair or remove the association using the native CLI, then retry. A failure
after a native mutation but before its checkpoint heals on retry when the resulting native entry
matches the declaration; adoption is recorded and reported. Settings retention is intentional and is
separate from pending plugin cleanup.

A native setup error stops that facet while independent facets and the owner's remaining setup
continue. The command reports the collected failures at the end and returns a failing exit status;
completed core setup and confirmed native claims remain recorded. Shared persistence failures and
cancellation still abort immediately. Inspect the recorded reason before retrying the owning
operation. Workspace creation has no reinit command; changing its setup requires explicit recreation
through the ordinary workspace lifecycle.

## Native compatibility evidence

Native CLI fixture tests require Codex and Claude executables on the test runner's PATH. The
simplification round was validated locally with Codex CLI 0.154.0 and Claude Code 2.1.269. These are
tested versions, not an enforced minimum. Fixtures use isolated temporary homes and do not require
provider credentials. Runs without the required executable skip the corresponding native tests.

Codex inventory handling follows its tagged
[plugin commands](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/cli/src/plugin_cmd.rs)
and
[marketplace commands](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/cli/src/marketplace_cmd.rs).
Native command success is always followed by a fresh observation; unexpected identity payloads
refuse mutation instead of being interpreted as empty inventory.

## Deleting owning resources

VM, agent, and workspace deletion follow their existing lifecycle policies. Removing a VM removes
its contained native files; removing an agent home or workspace directory removes the settings and
plugins inside it. There is no separate native plugin retirement step before parent deletion. Setup
applied-state records, including unreadable or unknown versions, do not add deletion prerequisites.
The existing database deletion removes the owner's records and any affected child records together.

Backend binding failures retain the existing warning-and-local-cleanup behavior of VM deletion. An
actual platform deletion failure still aborts as before. Agent and workspace remote cleanup retains
its existing best-effort warnings. Files deliberately configured outside the removed parent remain
outside that filesystem cleanup; setup does not introduce a new forget command or force option.

Setup and parent deletion share a VM-family mutation guard so they cannot race. This serializes
mutating operations on the same VM, including owners without harness activations; contention refuses
with retry guidance. It does not require native applied-state record discovery before parent
deletion.

Rehome moves project settings with the workspace and holds the same guard across the move. Setup
records do not block rehome. Their old destination makes readiness stale until owning setup runs
successfully at the new destination; rehome itself does not run harness setup.

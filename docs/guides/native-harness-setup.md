# Native harness setup

Activated Claude Code and Codex user facets configure that actual user's marketplaces, plugins, and
settings. Claude Code, Codex and Grok also register declared MCP server artifacts at user and
workspace scope. The same integration activation model serves administrators and agents. Activated
workspace facets map project settings where supported. They do not install workspace plugins.
Integration activation must be explicit, including when all configuration fields use defaults.

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

User roles respect `CLAUDE_CONFIG_DIR` and `CODEX_HOME` in the actual user's login environment,
including exports from guest startup. The selected native home applies consistently to settings,
plugins and artifacts; adding or removing MCP does not select another settings destination.
Overrides must be absolute guest paths without `.` or `..` components. Workspace destinations stay
under the workspace root regardless of user overrides. These are fixed settings roles, not arbitrary
guest destinations. Source paths embedded inside the document are copied as values without
rewriting.

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

## MCP registrations

Select connections through an artifact bundle's `mcp_servers` map and the owner's
`artifacts.bundles` block. Explicit integration activation is still required, including default
configuration. See the
[artifact format and examples](../../cli/agentworks/artifacts/README.md#mcp-servers) for the strict
stdio/Streamable HTTP profile, workstation/Git sources and unresolved env references. MCP
definitions register connections; they do not install executables or dependencies, authenticate, run
a server, connect to an endpoint or grant tool permissions. Native clients keep their own trust,
approval and runtime authentication behavior.

| Integration | Default user MCP role                   | Workspace MCP role               |
| ----------- | --------------------------------------- | -------------------------------- |
| Claude Code | `mcpServers` in `~/.claude.json`        | `<workspace>/.mcp.json`          |
| Codex       | `mcp_servers` in `~/.codex/config.toml` | `<workspace>/.codex/config.toml` |
| Grok Build  | `mcp_servers` in `~/.grok/config.toml`  | `<workspace>/.grok/config.toml`  |

These are fixed native roles. User roles follow actual native-home overrides beneath the user's
home. An explicit `CLAUDE_CONFIG_DIR` selects `<CLAUDE_CONFIG_DIR>/.claude.json`; without that
override, Claude's role is `~/.claude.json`, not `~/.claude/settings.json`. Codex and Grok use their
`CODEX_HOME` and `GROK_HOME` config directories. Workspace roles stay within the workspace. Native
VM facets defer MCP to user; Shell publishes canonical JSON and an index at each scope without
running clients or servers.

The declared artifact key is the native server name, unchanged: `github` registers as `github`.
Original-owner hashes remain internal provenance and do not rename the server. Separate native
user/project configurations retain the harness's normal loading and precedence rules; use distinct
names to address separate connections. Within one destination, equal same-name inputs share one
entry; different definitions are all deferred with a warning. Use `agw artifact show` for recorded
names and destinations without querying a native MCP inventory or exposing connection values.

Application owns an entry, not the whole shared file. Missing entries are created; identical unowned
entries can be adopted and become managed. Matching the recorded entry fingerprint permits an owned
update. Different unowned or externally modified entries are retained and deferred individually.
Removing a declaration or activation retires unchanged owned entries wherever safely possible;
modified obsolete entries keep their claims and warn. Unrelated entries and settings remain unless
an explicit settings mapping changes them. JSON/TOML serialization preserves values, not comments or
formatting. Concurrent changes refuse publication rather than replacing them from a saved document
snapshot.

When upgrading from generated MCP names, the next owning setup retires unchanged recorded aliases
and creates or adopts the declared name. Modified aliases remain recorded with a warning; unclaimed
entries remain foreign regardless of their spelling. A conflicting literal entry stays in place and
leaves the requested artifact unhandled, while an unchanged obsolete alias still retires. Native
credentials are retained, although native clients can bind credentials or approvals to a server name
and require operator review after a rename. Private session carriers change on the next managed
start/restart. This adds no automatic ancestor repair or workspace reinit operation.

Codex settings mappings share `config.toml` with MCP. Setup computes safe obsolete-entry removal,
the selected mapping policy and desired MCP entries in one document plan. An actual surviving mapped
value that differs at a declared server name leaves that MCP artifact unhandled. Equal values can be
adopted. If an unchanged MCP entry is withdrawn but the source mapping still supplies that key,
`replace`, `merge-overwrite` and `merge-preserve` leave the source value and release the MCP claim
in that same application. For `skip-existing`, the original file-existence decision still governs
the mapping: an existing file skips the source while the withdrawn entry retires; a missing file is
populated from the source. `skip-existing` does not disable separately declared MCP work. Removing
only a settings mapping retains its current values as usual.

Untracked shared MCP config documents receive exact local Git exclusions. The exclusion covers the
entire file, including unrelated values, because Git cannot hide individual entries. Already-tracked
files warn and remain tracked; Agentworks changes neither the Git index nor repository `.gitignore`.
Exclusion cleanup removes only unchanged owned marker/pattern pairs and leaves the native document
intact. Workspace rehome updates managed MCP destination metadata after a successful copy; server
names and entry fingerprints follow the active workspace unchanged. A deliberately retained old copy
is unmanaged and can keep its native entries.

### Session opt-ins

Claude `session-mcp-config` passes a private session/run JSON file through `--mcp-config` without
enabling strict mode or suppressing other native sources. Codex `session-mcp-overrides` uses
invocation-only configuration overrides without writing user/project config. Both are explicitly
selected through the session facet's `enabled_workarounds`. They preserve env references and native
consent/authentication. A differing conflicting native entry remains unhandled rather than being
silently combined with another connection definition.

Grok Build 1.0.77 rejects the proposed private-plugin `--plugin-dir` carrier, so no Grok session MCP
workaround is advertised. Those session inputs warn and stay unhandled. Shell exposes canonical
files and its private session index without an opt-in. No native session MCP method is enabled by
default; terminal unhandled artifacts warn and permit launch.

### Credentials after removal

Removing an Agentworks registration names the native server and warns that any OAuth credentials
remain. This is conditional guidance, not a claim that credentials exist: setup and inspection do
not read native credential caches or log out automatically. OAuth-specific artifact configuration
and an explicit authentication workflow are planned for phase 2; they are not part of this initial
registration support. Native authentication remains separate.

If removing the server's stored credentials is authorized, use the actual workload user and native
config context before withdrawing its artifact definition. For Codex, use
`codex mcp logout <native-name>` as documented in the
[native CLI reference](https://learn.chatgpt.com/docs/cli/reference#codex-mcp). Claude documents
`claude mcp logout <native-name>` and its interactive `/mcp` authentication controls in its
[MCP authentication guide](https://code.claude.com/docs/en/mcp#authenticate-from-the-command-line).
Use the derived name from Agentworks inspection and check the installed native CLI's supported
interface. Credential deletion and provider-side revocation are different operations; follow the
provider's revocation process when necessary. There is no verified Grok logout command claimed here.

Some native logout commands need the server definition still present. If it was already removed,
temporarily restore the same native server name and matching definition in the original native
user/project config context through its supported configuration workflow, perform logout, then
remove the temporary registration. Keep the intended Agentworks declaration consistent so later
setup does not reintroduce it. Existing workspaces have no reinit; native credential cleanup does
not require recreating a workspace. Native client removal and Agentworks entry removal can have
different credential behavior. If logout is outside the requested work, leave credentials untouched
and report the remaining native authentication task. Reading this guide authorizes neither logout
nor temporary reprovisioning.

## Marketplaces and plugins

User configuration has `marketplaces` and `plugins` lists, both empty by default. Marketplace
strings use the installed harness's native source grammar. Local marketplace paths refer to the
guest filesystem; relative paths resolve against the actual user's home. Plugin names resolve
against the native catalog; qualify ambiguous names as `plugin@marketplace`. An ambiguous
unqualified selection is retained and reported without retiring its prior associations. A name
absent from the catalog is a configuration error.

Claude marketplace paths can select a directory or its `marketplace.json` file. A named entry with a
source form Agentworks cannot establish is preserved and reported locally rather than invalidating
unrelated marketplace entries when the native CLI can read the inventory. If the CLI itself refuses
the inventory, that facet fails without rewriting the configuration. The diagnostic names the entry
and durable native configuration root; repair the unsupported declaration with the native tool or
configuration before retrying. Malformed overall inventory still fails that facet.

Tool discovery reads the actual user's login-shell PATH once per setup invocation. An explicit PATH
in the prepared setup environment takes precedence. Native commands use that resolved executable and
bind the selected native home and remaining prepared values explicitly during execution. Marketplace
discovery reuses the same executable under its private HOME and native config directories. A
launcher that depends on the real HOME may fail there; use a standalone CLI installation or a
launcher compatible with an isolated HOME. Agentworks does not bypass isolation or guess
package-manager cache paths to make such a launcher work.

A newly declared marketplace is first resolved in a private temporary native home. This discovers
its native name and validates conflicts before registration in the real user home. Native source
acquisition may therefore happen twice, including two Git fetches. The temporary home is cleaned on
success or handled failure; its contents never count as completed user setup.

Agentworks reconciles explicit plugin settings and mapped settings as one plan. A surviving source
value that disables an explicitly requested plugin or changes its marketplace identity is a config
error before writes. Source values discarded by `merge-preserve` or `skip-existing` create no such
conflict. Plugin commands run first; the final mapping preserves their controlled native keys.
`skip-existing` skips only the mapping, not separately requested plugin work. Native associations
retained by an unresolved managed claim or ambiguous selection keep their existing overlay when the
mapping omits them; omission does not bypass safe native retirement. Explicitly supplied mapped
entries follow the selected settings policy.

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
identity. An unrelated built-in marketplace without an observable source does not make Agentworks
setup incomplete. Only a declared or previously managed association needs to converge or retire.

Matching uses the native tool's reported association, including the full marketplace source and any
ref or sparse-path options. Agentworks does not authenticate the historical origin of cached plugin
files. An installed version can change through the native tool without changing the owned plugin
association. A differing unowned source or association is preserved with a warning; resolve that
native conflict before retrying setup.

Claude's git and GitHub list entries do not expose every source option. A missing or malformed user
source declaration, or one inconsistent with native inventory, makes that entry unobservable.
Agentworks retains and names all affected entries, including unrelated operator entries, while
independent entries continue where the native CLI permits. Restore a valid native association before
retrying setup. Marketplace retirement or replacement also retains dependent plugins when the user
declaration needed for removal is absent. Agentworks does not broaden user-scoped removal to all
scopes or write a guessed source. When Agentworks cannot verify preservation of a listed settings
entry, it holds all native CLI mutations that can rewrite the whole settings document. An
unrecognized source form, including a native inline `settings` source, can be valid native
configuration; this guard does not establish that it is malformed or that the native command would
lose it. Independent file/settings work and adoption of matching reported associations continue.
Review the native CLI operation separately, then retry setup to adopt matching reported
associations, or explicitly map the entry under the intended settings policy. An explicitly mapped
settings contribution follows its selected strategy and can replace that entry; no initial snapshot
is restored over the mapping, later operator edits or confirmed removals. A settings mapping change
can therefore require a second reinit for the previously skipped native command.

Native commands and Agentworks ownership checkpoints are separate operations. Abrupt termination can
leave a native mutation without a checkpoint; retry observes and adopts a matching association.
Agentworks does not promise transactional rollback of the native executable's work.

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
applied-state record. Inspect the installed plugin and native registration first. Restore the
matching original marketplace registration with the native CLI when it is still intended, or
explicitly remove the remaining plugin using its native scope after confirming that removal is
appropriate, then retry the owning setup. Reinit does not reinstall an externally removed
registration solely because an old owned record exists, and cannot prove a hidden plugin was
removed. A failure after a native mutation but before its checkpoint heals on retry when the
resulting native entry matches the declaration; adoption is recorded and reported. Settings
retention is intentional and is separate from pending plugin cleanup.

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
Workspace deletion attempts to retire exact owned artifact exclusions because shared Git metadata
can survive the workspace directory. Unresolved cleanup warns and preserves foreign or ambiguous
bytes without preventing the normal deletion. A warning does not certify that shared exclusions were
removed.

Backend binding failures warn and continue local cleanup without attempting remote deletion. An
exception raised by a bound platform's delete operation aborts local removal. Agent and workspace
remote cleanup retains its existing best-effort warnings. Files deliberately configured outside the
removed parent remain outside that filesystem cleanup; setup does not introduce a new forget command
or force option.

Whole-VM deletion does not first log out of Tailscale or stop individual guest sessions. An external
tailnet entry can remain after the guest is gone; remove a retired node from the Tailscale admin
console separately. A binding-failure warning means remote deletion was not attempted, so inspect
and clean up the provider resource as well. Platform-specific best-effort deletion may also leave
remote resources; a removed local row alone does not prove provider removal.

Setup and parent deletion share a VM-family mutation guard so they cannot race. This serializes
mutating operations on the same VM, including owners without harness activations; contention refuses
with retry guidance. Unavailable native applied-state evidence does not prevent parent deletion.

Rehome moves project settings with the workspace and holds the same guard across the move. It uses
the known old/new owner paths to reconcile artifact exclusions where safe, warning about unresolved
shared metadata. Setup records do not block rehome. Every readable workspace setup record follows
the verified copy: owner identity and known file, package, skipped-path and MCP destination metadata
move to the new root, preserving fingerprints and unresolved status. MCP server names stay stable;
unknown native claims remain unchanged. A retained old copy is unmanaged. Rehome does not rerun
facets or rewrite literal absolute paths inside copied files or native values, which can remain
path-dependent. Existing workspaces have no harness reinit operation, and session restart does not
repair that workspace setup.

The VM description's initialization field records the last initialization run. It is separate from
per-integration lifecycle evidence: a run can finish while a native entry is skipped. Read the
integration's incomplete reasons in that description or `agw artifact show` before concluding that
its requested artifacts or native associations were applied.

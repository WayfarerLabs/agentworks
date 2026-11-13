# Harness integrations

## Scopes and facets

Harness integrations configure native tooling at its owning resource and launch one workload per
session. A **scope** identifies the resource and context; a **facet** is a scoped part of a
capability, pairing its operations and configuration. Facets are not specific to harness
integrations. Harness integrations use four facets: `vm`, `user`, `workspace`, and `session`.

| Owning scope | Facet       | When it runs                       | Native ownership                                         |
| ------------ | ----------- | ---------------------------------- | -------------------------------------------------------- |
| VM           | `vm`        | VM create and reinit               | VM-wide setup and artifact routing                       |
| Admin        | `user`      | VM create and reinit               | The VM administrator's actual user and home              |
| Agent        | `user`      | Agent create and reinit            | That agent's actual user and home                        |
| Workspace    | `workspace` | Workspace creation                 | Project settings and artifacts shared by workspace users |
| Session      | `session`   | Session create, start, and restart | Workload launch and conversation state                   |

The admin and agent cases use the same user facet and config model. Configuring the administrator
does not configure agent users. Workspace setup likewise has no particular session user; the shipped
workspace facets apply project settings and artifacts shared by workspace users. They do not install
user-owned project plugins.

## Explicit integration activations

Availability and activation are independent. Plugin-provided integrations first need their system
plugin enabled in Agentworks config; the built-in shell integration needs no plugin entry. Then
activate the integration's facet on each resource that should use it. Enable the shipped Claude and
Codex capabilities with this config fragment:

```toml
[plugins]
system = ["claude", "codex"]
```

VM, admin, agent, and workspace templates accept a `harness_integrations` map keyed by integration
name. Every entry is an **integration activation**: its value is the facet configuration, with no
inner `name` field. An empty entry such as `codex: {}` activates a supported facet with defaults.
Multiple supported integrations can coexist, each with its own config. Merely enabling the system
plugin does not run setup anywhere. Activating an unimplemented facet fails during owning setup with
an error identifying the integration and facet. With no prior owned effects to retire, leaving it
inactive skips invocation and creates no successful setup record. An implemented facet can succeed
without changes when its defaults request no work or its desired state already exists.

Activation describes the resource's effective declaration. It does not prove that setup has run or
succeeded; readiness uses setup evidence separately. This is distinct from VM activation, which
starts and holds a VM active for an operation.

This example activates both user integrations and selects Codex for a session. Install the native
harness CLIs through the existing package or user install-command configuration before running
setup, and provide `python3` on the VM as described in
[native harness setup](native-harness-setup.md).

```yaml
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: dual-harness-user
spec:
  harness_integrations:
    claude-code: {}
    codex: {}
---
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: codex-work
spec:
  harness_integration:
    name: codex
```

A session template selects exactly one integration through singular `harness_integration`. Its
effective template must select one or inherit a selection. The built-in `default` session template
explicitly selects `shell`; an unrelated template with no selection is invalid.

Integration activation maps merge by key:

- Omit `harness_integrations` or supply `{}` to inherit the effective map.
- Adding a key activates that integration and preserves the parent's other integrations.
- Restating a key merges its configuration according to the owning facet's schema. Claude and Codex
  user facets replace an inherited `plugins` or `marketplaces` list when that field is supplied;
  include every desired entry, or use `[]` to clear the list. Omitted fields inherit.
- An empty entry activates defaults when new and preserves inherited configuration when already
  selected.
- A `null` entry disables that integration, including an inherited activation. For example,
  `harness_integrations: {codex: null}` leaves other integrations active. A later layer can
  reactivate Codex with `{}` or explicit config; the disabled config is not restored.

For example, this child sets Claude's marketplace and plugin lists, replacing any inherited values
for those fields. It retains the parent's Codex activation and Claude settings:

```yaml
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: review-user
spec:
  inherits: [dual-harness-user]
  harness_integrations:
    claude-code:
      marketplaces: [example-org/team]
      plugins: [reviewer@team]
```

To replace Claude's whole inherited configuration while retaining other integrations, use two
layers: disable Claude in an intermediate template, then reactivate it in the child. A single map
entry has no whole-config replacement switch.

```yaml
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: without-claude
spec:
  inherits: [dual-harness-user]
  harness_integrations:
    claude-code: null
---
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: fresh-claude
spec:
  inherits: [without-claude]
  harness_integrations:
    claude-code:
      marketplaces: [example-org/team]
      plugins: [reviewer@team]
```

Session selection uses a tagged table: `harness_integration: {name: codex}`. Restating the same
`name` merges session config according to its model; selecting a different name replaces the
previous selection and starts that integration's config anew. Omission inherits the selection, but
an effective session without a selection is invalid. The setup map's `null` opt-out does not apply
to the session selector. Config for integration activations at setup scopes never rolls into a
session's config, even when both name the same integration. Each host validates its own facet.
Claude and Codex user facets accept settings, marketplaces, plugins, and artifacts; their workspace
facets accept settings and artifacts. All shipped integrations implement VM, user, workspace, and
session facets. VM facets apply artifacts at machine scope where supported and route unsupported
types to a later facet. Shell publishes files directly at every activated facet, including
`/opt/agentworks/artifacts` at VM scope. An empty config schema does not itself imply facet support
for another integration.

For separate project settings, select both workspace facets and give each its own workstation
source. Create these source documents before workspace creation; this declaration does not change
the corresponding user configuration:

```yaml
apiVersion: agentworks/v1
kind: workspace-template
metadata:
  name: dual-harness-project
spec:
  harness_integrations:
    claude-code:
      settings:
        source: ~/.config/agentworks/harness/claude-project.json
        strategy: merge-preserve
    codex:
      settings:
        source: ~/.config/agentworks/harness/codex-project.toml
        strategy: merge-overwrite
```

Use `agw resource explain harness-integration/codex` for all facet answers and
`agw resource explain agent-template` for the exact user host shape. The generated reference owns
field definitions and defaults. [Native harness setup](native-harness-setup.md) explains workstation
settings sources, merge strategies, native placement, and plugin ownership. Existing Claude
declarations and stored overlays have a dedicated [migration guide](migrating-claude-setup.md).

## Core setup and the environment

At each owner, core prepares environment and captures declared artifact bundles before harness
integration setup. The integration receives the applicable inputs and applies native configuration
and artifacts or defers artifacts to a later facet. VM/admin setup completes before the final
authorized-key and successful VM initialization checkpoint. Agent setup follows core user
initialization; workspace setup follows directory and repository creation. Creating pending
resources during session creation runs their owning setup before the session launches.

Active setup receives environment from the scopes that already exist for that owner:

| Invocation | Environment layers, from outer to inner |
| ---------- | --------------------------------------- |
| VM         | VM                                      |
| Admin user | VM, admin                               |
| Agent user | VM, agent                               |
| Workspace  | VM, workspace                           |

Inner values win collisions under the normal env merge rules. Core protects `AGENTWORKS_*` identity
values. Setup environment and declared config secrets join the operation's eager resolution before
remote mutation and logger construction. Each integration receives only its declared config secrets;
environment arrives separately as the invocation's `environment`, and `request_environment(local)`
builds a request's `env` from it with the integration's own variables added, never an `AGENTWORKS_*`
one. With neither selected activations nor prior applied-state records, this setup path does not
resolve otherwise unused environment secrets. Existing install commands keep their own execution
behavior.

Session environment retains its existing precedence: session over actual user, over workspace, over
VM. It is not produced by merging setup configs. Environment values follow their own merge rules;
artifact handling does not consume or reroute those values. Launched workloads also receive
`AGENTWORKS_SESSION_UUID` and `AGENTWORKS_RUN_ID`, identifying their managed logical session and
current run independently of the reusable session display name and native conversation ID. The
pipeline is core environment and artifacts, then harness integration. MCP credential references stay
unresolved for the native runtime. This prepared env map is not proof that a name is absent from the
later login-shell environment; artifact handling does not add a credential-presence probe. Features
and automatic hint emission are future work.

## Artifact handling across facets

An owner selects reusable bundles through `artifacts.bundles`, independently of integration
activation. Core captures those inputs even when no integration is activated there. A later facet
reads the capture; it does not reopen workstation files or fetch Git sources for an ancestor.
Bundles contain `hints`, `rules`, `skills`, `agents` and `mcp_servers` maps. MCP entries describe
connections, not running services; registration never proves authentication, connectivity, tool
discovery or model use. Select the consuming user's env/secret declarations separately.

Core routes an inactive VM facet's inputs to the user facet by default. An activated VM facet can
choose user, workspace, or session for each deferred input. These routes do not depend on which
downstream resources exist or which integrations they activate. Each input follows one route,
avoiding duplicate delivery through the user/workspace diamond.

An inactive user or workspace facet passes its inputs onward to the session. An activated facet
applies what it can and defers the remainder. Handled inputs stop at that facet. The session joins
its actual user and workspace results with anything routed directly from the VM. Remaining inputs
that the session integration cannot handle produce warnings with their original source and reason.
Native harness session delivery requires explicit `enabled_workarounds` in the selected
integration's session config. The default empty list leaves all remaining inputs unhandled. Each
named workaround enables only its documented delivery method; `[]` replaces inherited opt-ins. See
`agw guide show concept-agent-artifacts` for the supported methods and their limitations. Shell
publishes directly as files at every activated facet, including the private session directory.

For example, VM-declared skills can reach native user placement by activating only the user facet.
The same captured VM inputs remain available to every actual user; one user's handling does not
consume another's inputs. Admin and agent users remain separate. With neither VM nor user
activation, those inputs reach the session through user passthrough.

Claude applies VM guidance skills, skills and agents in its managed locations. Codex applies VM
skills and guidance skills, and defers personas to user. Native VM facets defer MCP to user. Grok
defers VM inputs to user. Shell applies all VM types under `/opt/agentworks/artifacts`.
Workspace-owned bundles remain useful independently: their workspace facet handles them in project
locations or passes them to each consuming session. Workspace placement is shared with other users
of that workspace. Session-only content stays under the actual user's home: private session/run
files or an explicitly enabled best-effort native skill workaround. Home-skill catalogs can be
shared by that user's sessions; metadata states intended applicability. The
[two concurrent Shell sessions example](../../cli/agentworks/artifacts/README.md#two-concurrent-sessions)
uses an existing workspace and distinct private run paths, with inspection and teardown commands.

Applying files at a facet updates its native locations during that setup. Whether a running workload
reloads them depends on the harness. Deferring inputs updates only the reusable result; existing
descendants and running sessions keep their prior delivery until the receiving user's setup or
managed session start/restart runs. Missing, stale or incomplete ancestor state is reported with its
owner and supported repair operation; it does not block descendant setup or session launch. Runtime
routing uses available captures and explicit known deferrals without guessing unknown handling or
refreshing ancestors.

A routing result is usable only when its recorded input identities cover the applicable current
inputs, including incoming routes whose payload is unavailable. An old empty table cannot confirm
new or changed inputs. A confirmed empty set of deferrals remains usable when the ancestor has an
unrelated unfinished native entry. It does not hold a descendant's own updates or removals. An
unknown applicable input set is reported by artifact inspection and session routing, separately from
the receiving facet's completed local setup. It does not permanently fail a fresh workspace merely
because its VM was temporarily unavailable. Incoming route health is recalculated from the available
ancestor evidence; local completion acknowledges only the known inputs actually supplied to that
invocation. An unrelated ancestor failure with a fully covered routing table is reported separately.
Saved interrupted evidence that cannot establish whether local routing finished remains unknown; use
the reported owning operation to confirm it.

A missing, unreadable or stale ancestor capture does not authorize retirement of its existing
placements. Independently attributable local files can still update or retire. A shared generated
file that needs unknown ancestor content remains held. Required/recommended prerequisites retain
their native setup contract; optional incoming artifact health is advisory.

Workspace setup runs only at creation. There is no workspace reinit, and workspace repair does not
refresh artifacts. If a VM recovers and confirms that nothing further needs workspace placement,
artifact inspection clears the temporary upstream warning without recreating the workspace. An
existing workspace cannot adopt genuinely new workspace-routed inputs in place; recreation is a
separate lifecycle decision. Setup warns about deferred application without scanning or changing
descendants. Removal has the same timing boundary: ancestor refresh does not remove files previously
applied by a descendant.

See `agw guide show concept-agent-artifacts` for bundle examples, source capture, native support,
and inspection. `agw artifact show` includes upstream handling even when handled payloads no longer
flow to a descendant.

Claude Code, Codex and Grok register MCP at user/workspace scope in their actual native config
roles, preserving unrelated settings and entries unless an explicit mapping changes them. Managed
names include the original owner, so equal keys at different scopes do not shadow one another. An
identical unowned entry can be adopted; conflicting entries are retained and deferred individually.
The optional session carriers are Claude `session-mcp-config` and Codex `session-mcp-overrides`.
Grok has no supported session MCP carrier; unhandled MCP warns and launch continues. Shell publishes
canonical files at every scope without running servers. See
[MCP setup](native-harness-setup.md#mcp-registrations) for paths, merge interaction and the separate
native authentication/credential-removal workflow. OAuth configuration is planned for a later phase;
the initial artifact profile rejects OAuth and tool-filter fields explicitly.

## Session prerequisites

An integration can require or recommend prior setup through its session `check_setup` hook. Core
provides evidence for the session's actual VM, user, and workspace, and enforces the severity the
integration returns: a required gap refuses launch before runtime replacement; a recommended gap
warns and permits launch. Integrations supply the reason, and core adds the owning remediation.
These are integration policies, not a generic operator-authored list of required facets. A required
gap means that the consuming session needs a specific prerequisite to function; it does not mean
that all upstream artifact state must match its latest declarations.

The shipped integrations currently declare no ancestor requirement, so session-only use is valid
when the tool is otherwise installed and ready and any supplied artifacts can be delivered there.
Artifact delivery uses available captures, known recorded routes and existing placement. Stale or
incomplete ancestor evidence is reported without blocking launch; unsupported final-session delivery
warns and continues without claiming those inputs were handled. Selecting a user activation does not
select a session integration, and selecting a session integration does not implicitly enable or run
user setup.

Evidence can be absent, incomplete, stale, unavailable, or current. Current means a completed
applied-state record matches the effective declaration and destination identity. It does not mean
every native byte was revalidated; an integration can add read-only native probes when its
prerequisite needs them. Prerequisite checks neither reopen workstation settings files nor acquire
new setup secrets.

## Inspecting and reconciling setup

Owning setup records confirmed native claims through instance state. Applied-state records track
integration and component identity, completion, pending cleanup, native ownership metadata and
current-attempt incomplete reasons. Resolved secrets and settings contents are not stored. A failure
keeps the last confirmed mutation prefix so a retry can reconcile what actually completed. Fresh
agent and workspace applied-state records commit with their owner rows.

If a valid setup facet fails, initialization stops that facet, records its failure, and continues
other independent facets and the remaining owner setup. At the end the command lists the failed
owner, facet and integration with repair guidance and exits unsuccessfully. A newly created agent or
workspace remains available with its partial setup; it is not reported as fully initialized.
Explicit required prerequisites still block their consumer, while recommended prerequisites warn.
Cancellation, shared state failures, invalid activation preflight and unexpected internal errors
still abort immediately. A failed facet is distinct from an entry skipped with a warning. Creation
messages confirm that the core owner exists; inspect completion evidence for harness setup health.

A non-zero session-create result can leave a running session when only ancestor facet failures were
collected. Inspect the session before retrying. An explicit required prerequisite can instead leave
no session while keeping the newly created ancestors. Reuse those owners with `--agent <name>` or
`--workspace <name>`, rather than repeating `--new-agent` or `--new-workspace`. Remove the
corresponding creation-only `--agent-name`, `--agent-template`, `--agent-spec`, `--workspace-name`,
`--workspace-template` and `--workspace-spec` options when switching to an existing owner. Keep
session-level template/spec selection as appropriate. Reuse does not repair an incomplete owner;
repair the required prerequisite before retrying. Agent and VM setup can be retried with their
reinit commands. Workspaces have no integration retry command: `workspace repair` handles access and
Git identity only. Before destructive recreation, preserve tracked edits, untracked files, commits
not yet pushed and stashes outside the workspace. Correct the configured source first: push needed
repository changes, fix the workstation settings source, or choose the intended template as
applicable. Editing only the guest checkout does not change what a fresh clone will receive.

Confirmed deferrals survive failed setup attempts. If a recorded input is no longer readable from
its owner's capture, owning setup reports the unknown route and preserves affected prior placements,
including shared generated sections and related packages. Independent native setup and new
independent paths can still apply. Repair the source owner's capture/setup before retrying the
affected descendant; unknown content is never substituted with a current revision or treated as an
empty declaration authorizing cleanup.

At session launch, content that cannot be reconstructed from the available captures is explicitly
unhandled. A known route reports its recorded input identity and owner; unknown routing without an
input identity reports the owner without inventing content. Launch continues with available inputs,
while the session's artifact evidence remains incomplete. A later restart after the owning repair
can resolve that evidence. New-run preparation leaves the old runtime and its files intact until
teardown; obsolete private-run files retire after the old runtime is removed.

Harness setup and artifact-delivery diagnostics redact known secrets and remove guest terminal
controls. Each diagnostic reason or hint is bounded to one line of at most 2 KiB; output can add
owner and retry context. Human inspection also bounds older saved diagnostics without rewriting the
recorded state.

Use the existing owning inspection commands, such as `agw agent describe worker`,
`agw workspace describe project`, or `agw vm describe dev`, to inspect the `harness-native-setup`
lifecycle evidence. The summary includes completion, pending cleanup, and claim counts, without
native I/O or settings values. Use `agw artifact show` to inspect artifact capture, handling,
deferral and the full recorded incomplete reasons along the actual lineage. Only the selected
integration's ancestor status is relevant to its session; a shell session does not report unrelated
Claude or Codex setup. Doctor also checks stored evidence; it does not complete setup or repair
native drift.

After editing VM/admin or agent activations, run `agw vm reinit <name>` or
`agw agent reinit <name>`. Removing a declaration, or disabling an inherited activation with `null`,
becomes native cleanup during the owning operation, using its previous applied-state records.
Workspace mappings require explicit recreation to apply changes; `workspace repair` only repairs its
existing access and Git identity responsibilities.

Cleanup only removes effects whose ownership can be established. Removed settings mappings retain
the document; removed owned plugin associations and unchanged MCP entries are reconciled where safe.
MCP removal leaves native OAuth credentials, if any, untouched and reports that distinction.
Untracked shared native MCP files receive exact local Git exclusions covering the entire file;
tracked files warn and remain tracked. Cleanup removes owned exclusions without deleting the shared
document or changing the Git index. Obsolete artifact files are removed when their recorded
ownership and contents still match. Modified files are retained and diagnosed instead of being
silently overwritten or deleted. See
[native cleanup rules](native-harness-setup.md#deleting-owning-resources) before deleting an owner.
Parent deletion follows the existing VM, agent, or workspace lifecycle. It removes the contained
native files with their parent and clears setup records through the existing database deletion. It
does not first uninstall plugins or require readable applied-state records.

Setup, deletion, and workspace rehome share a VM-family mutation guard. A competing mutation refuses
with retry guidance. Rehome moves project settings with the workspace. Existing applied-state
records do not block the move. After a verified copy, every readable workspace record updates owner
identity and known file, package, skipped-path and MCP destination metadata, preserving fingerprints
and unresolved status. MCP server names stay stable; unknown native claims remain unchanged. A
deliberately retained old copy is unmanaged. Rehome does not rerun facets or rewrite literal
absolute paths inside copied files or native values, which can remain path-dependent.
[Idempotency](idempotency.md#harness-setup-reconciliation) describes retry guarantees and limits.

Before changing setup, confirm authorization for the owning operation and inspect its effects.
Reinit can change native files and plugin registrations; workspace recreation replaces a shared
resource. If that work is not authorized, use the read-only inspection commands and report the
required owning operation. Reading this guide does not authorize mutation.

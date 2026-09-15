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
    codex: {}
```

A session template selects exactly one integration through singular `harness_integration`. Its
effective template must select one or inherit a selection. The built-in `default` session template
explicitly selects `shell`; an unrelated template with no selection is invalid.

Integration activation maps merge by key:

- Omit `harness_integrations` or supply `{}` to inherit the effective map.
- Adding a key activates that integration and preserves the parent's other integrations.
- Restating a key merges its configuration according to the owning facet's schema. Lists append and
  deduplicate by default; fields can declare another merge policy.
- An empty entry activates defaults when new and preserves inherited configuration when already
  selected.

For example, this child adds a Claude plugin while retaining the parent's Codex activation and any
inherited Claude configuration:

```yaml
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: review-user
spec:
  inherits: [dual-harness-user]
  harness_integrations:
    claude-code:
      plugins: [reviewer@team]
```

Session selection is a map with exactly one integration key. Restating the same key merges session
config according to its model; selecting a different key replaces the previous selection and starts
that integration's config anew. An omitted or empty map inherits the selection, but an effective
session without a selection is invalid. Config for integration activations at setup scopes never
rolls into a session's config, even when both name the same integration. Each host validates its own
facet. Claude and Codex user facets accept settings, marketplaces, plugins, and artifacts; their
workspace facets accept settings and artifacts. All shipped integrations implement VM, user,
workspace, and session facets. VM facets route artifact inputs to a later facet; shell and Grok
outer facets provide artifact handling. An empty config schema does not itself imply facet support
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
environment arrives separately through the prepared runner. With neither selected activations nor
prior applied-state records, this setup path does not resolve otherwise unused environment secrets.
Existing install commands keep their own execution behavior.

Session environment retains its existing precedence: session over actual user, over workspace, over
VM. It is not produced by merging setup configs. Environment values follow their own merge rules;
artifact handling does not consume or reroute those values. The pipeline is core environment and
artifacts, then harness integration. Features and automatic hint emission are future work.

## Artifact handling across facets

An owner selects reusable bundles through `artifacts.bundles`, independently of integration
activation. Core captures those inputs even when no integration is activated there. A later facet
reads the capture; it does not reopen workstation files or fetch Git sources for an ancestor.

Core routes an inactive VM facet's inputs to the user facet by default. An activated VM facet can
choose user, workspace, or session for each deferred input. These routes do not depend on which
downstream resources exist or which integrations they activate. Each input follows one route,
avoiding duplicate delivery through the user/workspace diamond.

An inactive user or workspace facet passes its inputs onward to the session. An activated facet
applies what it can and defers the remainder. Handled inputs stop at that facet. The session joins
its actual user and workspace results with anything routed directly from the VM. Remaining inputs
that the session integration cannot handle refuse launch with their original source and reason.

For example, VM-declared skills can reach native user placement by activating only the user facet.
The same captured VM inputs remain available to every actual user; one user's handling does not
consume another's inputs. Admin and agent users remain separate. With neither VM nor user
activation, those inputs reach the session through user passthrough.

Shipped Claude, Codex, and Grok VM facets route to user; the shell VM facet routes to session.
Workspace-owned bundles remain useful independently: their workspace facet handles them in project
locations or passes them to each consuming session. Workspace placement is shared with other users
of that workspace, so session-only content belongs in the private session directory under the actual
user's home.

Applying files at a facet updates its native locations during that setup. Whether a running workload
reloads them depends on the harness. Deferring inputs updates only the reusable result; existing
descendants and running sessions keep their prior delivery until the receiving user's setup or
managed session start/restart runs. Stale ancestors must be refreshed in their own lifecycle order.

Workspace setup runs only at creation. There is no workspace reinit, and workspace repair does not
refresh artifacts. An existing workspace cannot adopt changed workspace-routed inputs in place;
recreation is a separate lifecycle decision. Setup warns about deferred application without scanning
or changing descendants. Removal has the same timing boundary: ancestor refresh does not remove
files previously applied by a descendant.

See `agw guide show concept-agent-artifacts` for bundle examples, source capture, native support,
and inspection. `agw artifact show` includes upstream handling even when handled payloads no longer
flow to a descendant.

## Session prerequisites

An integration can require or recommend prior setup through its session `check_setup` hook. Core
provides evidence for the session's actual VM, user, and workspace, and enforces the severity the
integration returns: a required gap refuses launch before runtime replacement; a recommended gap
warns and permits launch. Integrations supply the reason, and core adds the owning remediation.
These are integration policies, not a generic operator-authored list of required facets.

The shipped integrations currently declare no ancestor requirement, so session-only use is valid
when the tool is otherwise installed and ready and any supplied artifacts can be delivered there.
Artifact delivery additionally requires current ancestor capture and handling evidence; unsupported
final-session delivery refuses launch. Selecting a user activation does not select a session
integration, and selecting a session integration does not implicitly enable or run user setup.

Evidence can be absent, incomplete, stale, unavailable, or current. Current means a completed
applied-state record matches the effective declaration and destination identity. It does not mean
every native byte was revalidated; an integration can add read-only native probes when its
prerequisite needs them. Prerequisite checks neither reopen workstation settings files nor acquire
new setup secrets.

## Inspecting and reconciling setup

Owning setup records confirmed native claims through instance state. Applied-state records track
integration and component identity, completion, pending cleanup, and native ownership metadata.
Resolved secrets and settings contents are not stored. A failure keeps the last confirmed mutation
prefix so a retry can reconcile what actually completed. Fresh agent and workspace applied-state
records commit with their owner rows.

Use the existing owning inspection commands, such as `agw agent describe worker`,
`agw workspace describe project`, or `agw vm describe dev`, to inspect the `harness-native-setup`
lifecycle evidence. The summary includes completion, pending cleanup, and claim counts, without
native I/O or settings values. Use `agw artifact show` to inspect artifact capture, handling, and
deferral along the actual lineage. Doctor also checks stored evidence; it does not complete setup or
repair native drift.

After editing VM/admin or agent activations, run `agw vm reinit <name>` or
`agw agent reinit <name>`. Removing a declaration becomes native cleanup during the owning
operation, using its previous applied-state records. Workspace mappings require explicit recreation
to apply changes; `workspace repair` only repairs its existing access and Git identity
responsibilities.

Cleanup only removes effects whose ownership can be established. Removed settings mappings retain
the document; removed owned plugin associations are reconciled where safe. Obsolete artifact files
are removed when their recorded ownership and contents still match. Modified files are retained and
diagnosed instead of being silently overwritten or deleted. See
[native cleanup rules](native-harness-setup.md#deleting-owning-resources) before deleting an owner.
Parent deletion follows the existing VM, agent, or workspace lifecycle. It removes the contained
native files with their parent and clears setup records through the existing database deletion. It
does not first uninstall plugins or require readable applied-state records.

Setup, deletion, and workspace rehome share a VM-family mutation guard. A competing mutation refuses
with retry guidance. Rehome moves project settings with the workspace. Existing applied-state
records do not block the move; readiness reports stale setup when its recorded destination no longer
matches. [Idempotency](idempotency.md#harness-setup-reconciliation) describes retry guarantees and
limits.

Before changing setup, confirm authorization for the owning operation and inspect its effects.
Reinit can change native files and plugin registrations; workspace recreation replaces a shared
resource. If that work is not authorized, use the read-only inspection commands and report the
required owning operation. Reading this guide does not authorize mutation.

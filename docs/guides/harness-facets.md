# Harness facets

Harness integrations configure native tooling at its owning resource and launch one workload per
session. A **scope** identifies the resource and context; a **facet** is a scoped part of a
capability, pairing its operations and configuration. Facets are not specific to harness
integrations. Harness integrations use four facets: `vm`, `user`, `workspace`, and `session`.

| Owning scope | Facet       | When it runs                       | Native ownership                                      |
| ------------ | ----------- | ---------------------------------- | ----------------------------------------------------- |
| VM           | `vm`        | VM create and reinit               | VM tooling setup                                      |
| Admin        | `user`      | VM create and reinit               | The VM administrator's actual user and home           |
| Agent        | `user`      | Agent create and reinit            | That agent's actual user and home                     |
| Workspace    | `workspace` | Workspace creation                 | Project settings shared by sessions in that workspace |
| Session      | `session`   | Session create, start, and restart | Workload launch and conversation state                |

The admin and agent cases use the same user facet and config model. Configuring the administrator
does not configure agent users. Workspace setup likewise has no particular session user; the shipped
workspace facets map project settings and do not install user-owned project plugins.

## Explicit integration activations

There are two independent choices: enable a capability's system plugin in Agentworks config, then
activate that integration's facet on the resource that should consume it. Enable the shipped Claude
and Codex capabilities with this config fragment:

```toml
[plugins]
system = ["claude", "codex"]
```

VM, admin, agent, and workspace templates accept an ordered `harness_integrations` list. Every entry
is an **integration activation**: a `name` tag followed by any explicit facet configuration.
Name-only entries activate the facet with defaults. Multiple supported integrations can coexist,
each with its own config. Merely enabling the system plugin does not run setup anywhere.

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
    - name: claude-code
    - name: codex
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

Setup activation lists use complete replacement:

- Omit `harness_integrations` to inherit the effective list.
- Supply a list to replace the complete inherited list, including each entry's config.
- Supply `harness_integrations: []` to select none.
- Duplicate integration names are invalid.

For example, this child keeps only Claude with its default user config. It does not retain the
parent's Codex activation or merge a previous Claude settings mapping:

```yaml
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: claude-user
spec:
  inherits: [dual-harness-user]
  harness_integrations:
    - name: claude-code
```

Session selection has its existing tagged-object merge semantics: restating the same name merges
session config according to its model; selecting a different name starts that integration's config
anew. Setup activation config never rolls into a session's config, even when both name the same
integration. Each host validates its own facet. Claude and Codex user facets accept settings,
marketplaces, and plugins; their workspace facets accept settings. VM facets and the shell/Grok
setup facets currently accept name-only entries and perform no native writes.

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
    - name: claude-code
      settings:
        source: ~/.config/agentworks/harness/claude-project.json
        strategy: merge-preserve
    - name: codex
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

At each owner, core provisioning runs before harness integration setup. VM/admin setup completes
before the final authorized-key and successful VM initialization checkpoint. Agent setup follows
core user initialization; workspace setup follows directory and repository creation. Creating
pending resources during session creation runs their owning setup before the session launches.

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
prior receipts, this setup path does not resolve otherwise unused environment secrets. Existing
install commands keep their own execution behavior.

Session environment retains its existing precedence: session over actual user, over workspace, over
VM. It is not produced by merging setup configs. The current pipeline has no feature-emission stage
or artifact block, bundle ingestion, propagation, or deferral mechanism.

## Session prerequisites

An integration can require or recommend prior setup through its session `check_setup` hook. Core
provides evidence for the session's actual VM, user, and workspace, and enforces the severity the
integration returns: a required gap refuses launch before runtime replacement; a recommended gap
warns and permits launch. Integrations supply the reason, and core adds the owning remediation.
These are integration policies, not a generic operator-authored list of required facets.

The shipped integrations currently declare no ancestor requirement, so session-only use is valid
when the tool is otherwise installed and ready. Selecting a user activation does not select a
session integration, and selecting a session integration does not implicitly enable or run user
setup.

Evidence can be absent, incomplete, stale, unavailable, or current. Current means a completed
receipt matches the effective declaration and destination identity. It does not mean every native
byte was revalidated; an integration can add read-only native probes when its prerequisite needs
them. Prerequisite checks neither reopen workstation settings files nor acquire new setup secrets.

## Inspecting and reconciling setup

Owning setup records confirmed native claims through instance state. Receipts track integration and
component identity, completion, pending cleanup, and native ownership metadata. Resolved secrets and
settings contents are not stored. A failure keeps the last confirmed mutation prefix so a retry can
reconcile what actually completed. Fresh agent and workspace receipts commit with their owner rows.

Use the existing owning inspection commands, such as `agw agent describe worker`,
`agw workspace describe project`, or `agw vm describe dev`, to inspect the `harness-native-setup`
lifecycle evidence. The summary includes completion, pending cleanup, and claim counts, without
native I/O or settings values. Doctor also checks stored evidence; it does not complete setup or
repair native drift.

After editing VM/admin or agent activations, run `agw vm reinit <name>` or
`agw agent reinit <name>`. Removing a declaration becomes native cleanup during the owning
operation, using its previous receipts. Workspace mappings require explicit recreation to apply
changes; `workspace repair` only repairs its existing access and Git identity responsibilities.

Cleanup only removes effects whose ownership can be established. Removed settings mappings retain
the document and relinquish claims; removed owned plugin associations are reconciled where safe. See
[native cleanup rules](native-harness-setup.md#deleting-owning-resources) before deleting an owner.
Agent and workspace deletion stop and retain evidence when retirement cannot complete. Successful
platform VM deletion removes guest-native effects with the VM and clears its family's records.

Setup, deletion, and workspace rehome share a VM-family mutation guard. A competing mutation refuses
with retry guidance. Rehome refuses a workspace carrying native receipts because receipt relocation
is unsupported; preserve its contents, delete with integration cleanup, and recreate at the new
location. [Idempotency](idempotency.md#harness-setup-reconciliation) describes retry guarantees and
limits.

# Harness Integration Scope Perspective

- Status: Initial perspective
- Date: 2026-08-04
- Baseline: Agentworks 0.13.0 (`v0.13.0`)

## Purpose

This document records a perspective on expanding harness integrations beyond their current
session-only contract. It is an input to later requirements and architecture work in this SDD, not a
functional specification or implementation plan.

The central recommendation is to retain one harness-integration identity while giving it distinct,
explicitly selected contributions at the VM, agent, workspace, and session levels. Each contribution
runs only in its owning resource's lifecycle. Session operations may verify broader prerequisites,
but must never repair them implicitly.

The design must also leave room for first-class agentic artifacts. Rules, skills, hooks, and future
artifact kinds may be declared directly or emitted by scoped features, then rendered into the
locations and formats required by a selected harness integration.

## Executive Assessment

The current harness-integration API is a good session workload abstraction, but it cannot safely
perform the wider setup real harnesses need. Authentication, executable installation, user plugins,
and workspace artifacts commonly affect resources that outlive or are shared by one session. Doing
that work from `session create` or `session resume` would violate the current session-local effects
contract and make a session operation an implicit mutation path for upstream resources.

The capability should expand, but not by granting the current session-bound object wider powers.
Instead, one registered integration should expose optional scope-specific facets. Templates at each
owning level should explicitly select the integrations whose facets apply there.

| Concern                         | Direction                                                     |
| ------------------------------- | ------------------------------------------------------------- |
| Integration identity            | One `harness-integration/<name>` across all scopes            |
| Wider-scope selection           | Explicit on the template that owns the effect                 |
| VM and agent lifecycle          | Declarative, reapplied by `reinit`                            |
| Workspace and session lifecycle | Resolved and materialized at create                           |
| Session prerequisites           | Diagnose upstream state, never mutate it                      |
| Agentic artifacts               | Harness-independent logical contributions                     |
| Artifact placement              | Harness-integration responsibility                            |
| Filesystem ownership            | Smallest practical file, subtree, entry, or fragment          |
| Applied state                   | Persist resolved inputs, destinations, strategies, and hashes |
| Rulesync                        | Design reference, not a required runtime dependency           |

## Current Boundary and Why It Must Hold

Today a harness-integration instance is bound to one session, VM, workspace, launch identity, and
per-session state namespace. It owns session workload readiness, start, and resume behavior. The
current contract correctly forbids mutations that affect another session, a shared user home, a
workspace, or a VM.

Authentication is the clearest example. An environment variable injected into one session process is
session-local. A login written beneath the user's home is agent-scoped. Installing a plugin into the
checkout is workspace-scoped. Installing an executable or system configuration is VM-scoped. The
fact that all four may be needed to launch one harness does not give a session operation permission
to perform them.

This boundary should remain load-bearing:

> An operation may mutate only the resource lifecycle it owns. Discovering a missing prerequisite at
> another level produces diagnosis and remediation, not scope escalation.

## Scope Vocabulary

The existing operation model distinguishes `SYSTEM` from `VM`. In this design, "machine" normally
means the managed guest and therefore maps to `VM`. `SYSTEM` means the Agentworks installation or
control plane and has no VM execution target. Harness setup should not use `SYSTEM` as a synonym for
VM-wide setup.

The relevant hierarchy is:

```text
VM
├── admin identity
├── agent identity
│   └── sessions may use this identity in several workspaces
├── workspace
│   └── sessions may run here as an agent or admin
└── session
```

Agents are VM-scoped identities in the current model. Workspace access is a grant, not part of an
agent's identity. That distinction matters for user configuration: agent-scoped authentication or
plugins should not be duplicated per workspace merely because a session uses them there.

## Resource Lifecycles Are Not Uniform

The wider integration design must follow the resource lifecycles Agentworks actually provides,
rather than inventing one generic reconciliation verb.

| Resource  | Configuration lifecycle                  | Harness behavior                                |
| --------- | ---------------------------------------- | ----------------------------------------------- |
| VM        | Declarative create and `vm reinit`       | Apply and reapply VM/admin facets               |
| Agent     | Declarative create and `agent reinit`    | Apply and reapply agent facets                  |
| Workspace | Fixed at create                          | Materialize workspace facets once               |
| Session   | Fixed at create, narrow resume evolution | Materialize session state once; resume workload |

### VM and agent

VMs and agents are declaratively updated. Their harness facets should have idempotent apply
semantics and participate in the existing create and reinit operations. Configuration changes are
adopted intentionally by reinit, with last-applied state updated after successful application.

### Workspace

A workspace is primarily a Git checkout controlled by the repository and operator. Agentworks does
not have an idempotent workspace reinit operation, and it should not invent one as part of harness
integration work. Reapplying arbitrary initialization to an active checkout could overwrite edits,
change Git state, create conflicts, or interfere with running sessions.

`workspace repair` is deliberately narrower than reinit. Harness artifacts do not automatically
qualify as repair work. Workspace facets should therefore be resolved and materialized at workspace
creation. A future explicit artifact-application operation may be designed, but this effort should
not hide that lifecycle behind `repair` or session startup.

### Session

A session is also fixed at create. Resume may update integration-owned runtime state, select
resume-versus-launch behavior, and regenerate explicitly ephemeral launch material. It should not
reinterpret the session against arbitrary changes to its upstream templates or use resume as an
artifact update mechanism.

## Explicit Selection at the Owning Level

Reconciling every installed harness at every level is not harmless. Integration setup can prompt for
secrets, install packages, consume storage, rewrite configuration, authenticate accounts, and
conflict with other integrations. System-plugin enablement alone is not sufficient authorization to
perform those actions.

Each resource template should explicitly select the integrations whose contributions it wants:

```yaml
kind: vm-template
metadata:
  name: coding
spec:
  harness_integrations:
    - name: codex
      config: {}
---
kind: agent-template
metadata:
  name: azure-developer
spec:
  harness_integrations:
    - name: codex
      config: {}
---
kind: workspace-template
metadata:
  name: project
spec:
  harness_integrations:
    - name: codex
      config: {}
---
kind: session-template
metadata:
  name: coding
spec:
  harness_integration:
    name: codex
```

The exact schema remains open. The important property is ownership: VM configuration cannot live
only on a session template, and workspace configuration cannot live only on an agent template. A
future integration-profile resource may reduce repetition if it develops real operator-facing
identity, but it should expand into explicit owner-scoped attachments rather than obscure them.

Broader scopes may select several integrations because an agent or workspace can support sessions
using different harnesses. A session continues to select exactly one workload integration.

## One Integration with Scope-Specific Facets

One registered integration should expose independently supported facets rather than one instance
that accumulates every target and privilege:

```text
harness-integration/codex
    vm facet
    admin facet
    agent facet
    workspace facet
    session facet
```

Each facet should declare:

- Whether the integration supports that facet.
- The owning resource and lifecycle operation.
- Its configuration schema and inheritance or merge policy.
- Resource and secret references.
- Required execution identity and filesystem access.
- Readiness behavior and mutation operations.
- Applied-state schema and version.
- Idempotency and retry guarantees where the lifecycle supports reapplication.
- The requirements it satisfies for later session use.

An absent facet should mean unsupported. Inherited no-op methods would hide integration mistakes and
make support negotiation unreliable.

The existing session object can remain session-bound and continue to own start, resume, readiness,
and session state. Wider facets should be separate instances or operation objects constructed for
their owning resources. They should not reuse a session object's target identity, readiness cache,
or state namespace.

## Upstream Requirements and Degraded Operation

A session integration should be able to declare prerequisites supplied at broader scopes. Core can
check those requirements before launch and give uniform remediation guidance.

Conceptually:

```text
requirement:
    scope: agent
    key: codex-auth
    severity: required
    remediation: agent reinit <name>

requirement:
    scope: workspace
    key: project-rules
    severity: recommended
    remediation: recreate or explicitly apply workspace artifacts
```

Required prerequisites produce an error. Recommended prerequisites produce a warning and allow
degraded operation. The integration owns the semantic requirement and severity; core owns consistent
evaluation, reporting, and error framing.

Template declarations alone do not prove that a live resource contains the required setup. Config
can change after creation, setup can fail partially, and users or tools can modify target state.
Checks should use persisted applied state, inexpensive integration-owned probes, or both.

A missing prerequisite must never cause a session hook to apply an agent, workspace, or VM facet.
That would turn validation into implicit privilege escalation and recreate the scope violation this
design is intended to remove.

## First-Class Agentic Artifacts

Rules, skills, hooks, and future agentic artifacts should be first-class logical contributions,
independent of any one harness's filesystem format. Producers may include:

- Explicit blocks analogous to scoped `env` declarations.
- VM, agent, workspace, or session templates.
- Future `vm-feature`, `agent-feature`, `workspace-feature`, and `session-feature` capabilities.
- Other resources whose primary job is to publish reusable artifact content.

For example, `agent-feature/az-cli` might install the Azure CLI, authenticate it from declared
service-principal secrets, and emit a skill explaining that the CLI is authenticated and how the
environment was prepared. The feature should not need to know whether a later session runs Codex,
Claude Code, or another harness.

The flow should be:

```text
templates and features
        |
        v
logical, attributed contributions
        |
        v
scope and audience resolution
        |
        v
harness-specific materialization plan
        |
        v
scope and ownership validation
        |
        v
resource-owned application or session activation
```

## Scope Has Several Dimensions

One `scope` field is insufficient for every contribution. At minimum, the design must distinguish:

- **Declaration scope:** where the contribution enters the resource model.
- **Applicability or audience:** which downstream resources and sessions should receive it.
- **Materialization scope:** where a persistent representation is written.
- **Activation scope:** where its effect becomes visible.
- **Lifecycle owner:** which operation may create, update, or remove it.

Environment variables illustrate the distinction. They may be declared at VM, agent, workspace, or
session scope, collected through a session's ancestry, and materialized only in the session process
environment. Their activation and mutation remain session-local.

Filesystem artifacts often behave differently. A workspace rule may be declared by a feature,
written into the workspace root, and automatically discovered by every compatible harness session in
that checkout. A harness might also support a session-specific artifact stored beneath a workspace
directory and activated only through an explicit launch option. Its storage remains a workspace
mutation even if its semantic effect is session-local, so a session operation does not automatically
gain permission to create it.

Harness integrations must describe the harness's actual discovery semantics. Placing a file in a
workspace-level directory may broaden its effect to every session even when the logical contribution
claims a narrower audience.

## Harness Integrations Own Placement and Translation

Agentworks cannot dictate a universal path for agentic artifacts. Harnesses choose their own
locations, file formats, discovery rules, merge behavior, and support at user or project scope.

The logical artifact should identify meaning, scope, audience, content, provenance, and whether it
is required. The integration should translate that artifact into a materialization plan containing
destinations and composition strategies. Core should validate the plan against the current
operation's scope and filesystem grants before applying it.

This separation permits several integrations to render one logical contribution without forcing a
feature to contain harness-specific paths.

### Contribution composition

Contributions need stable identity and provenance. A feature may publish a complete skill, or it may
contribute an attributed fragment to a skill provided by another resource. Composition should be
deterministic and explicit, with:

- A stable target artifact identity.
- Ordered and attributed fragments.
- A declared merge strategy.
- Conflict detection.
- Provenance retained through resolution and rendering.

Blind text concatenation is not an adequate general merge model.

### Hooks require stronger treatment

Hooks are executable behavior, not merely content. They should be a distinct typed artifact with a
lifecycle event, execution identity, scope, command or action type, required permissions, timeout,
and failure policy. A harness integration may translate a canonical event into a harness-specific
event only when the semantics remain honest. Unsupported or lossy mappings must be reported.

## Workspace Filesystem Ownership

Repositories should be expected to contain their own `AGENTS.md`, `.claude`, `.codex`, and other
harness configuration. They may also use generators such as Rulesync. Agentworks must coexist with
that content rather than claiming an entire harness directory.

Ownership should normally be the smallest practical unit:

- A complete file created by Agentworks.
- A namespaced directory subtree created by Agentworks.
- A keyed entry in a structured document.
- A delimited fragment in a shared text document, only when the format supports it safely.
- A reference to an existing artifact that already satisfies the contribution, with no write.

A central `.agentworks` directory may store workspace-local manifests or diagnostics, but it cannot
serve as the universal artifact destination. The harness determines where active artifacts live. The
namespace should be reserved and carry a recognizable ownership marker. An unrecognized or
repository-tracked `.agentworks` directory should not be silently adopted.

Singleton files are the hardest case. If a repository already owns `AGENTS.md` and the harness
provides no safe composition mechanism, the integration may have no safe automatic write. It should
warn or fail according to the contribution requirement rather than overwrite the file.

Generated content deserves particular caution. A repository-local generator may legitimately replace
or remove an Agentworks-managed fragment. Agentworks should detect drift and report it, not fight
the repository's source-of-truth workflow.

## Last-Applied Configuration and Artifact State

Materializing last-applied configuration into the database is especially valuable for harness facets
and artifacts. The database can be the authoritative ownership ledger even when workspace files are
moved, reset, edited, or removed.

Three states should remain distinct:

1. **Resolved configuration:** the inputs selected for the resource at create or reinit.
2. **Materialization plan:** the writes and activation behavior the integration intended.
3. **Applied state:** what was successfully written or configured, including destinations and
   hashes.

An applied artifact record will likely need:

- Owning resource identity and scope.
- Integration, facet, and schema version.
- Logical artifact identity and producer provenance.
- Destination path and write strategy.
- Content hash and relevant file mode after application.
- Resolved configuration fingerprint.
- Completion status for interrupted operations.

The record enables conservative behavior:

- If the current hash matches the applied hash, Agentworks can prove the artifact is unchanged.
- If the hash differs, Agentworks reports external modification and does not overwrite or delete it
  automatically.
- If an expected path is missing, Agentworks reports external removal.
- If a path existed before the first write, matching content alone does not prove Agentworks owns
  it.
- Cleanup or replacement is allowed only when provenance and current content prove it is safe.

Write strategy is part of applied state. Creating a complete file, creating a subtree, inserting a
structured entry, and inserting a managed block have different update and cleanup semantics.

Secrets must never be persisted in resolved configuration or artifact state. Store secret references
and redacted configuration only. Artifact content itself may also be sensitive, so the design should
prefer hashes and necessary provenance over unconditional content snapshots.

The fixed-at-create lifecycle makes these snapshots meaningful. Existing workspaces and sessions
retain the configuration that created them. VM and agent reinit intentionally resolve and persist a
new applied snapshot.

## Rulesync as Prior Art

Rulesync is strong prior art for canonical agentic artifacts and harness-specific translation. It
supports many coding tools and demonstrates several important facts:

- Rules, skills, hooks, permissions, and related features are not uniformly supported.
- Project and user/global locations differ by harness.
- Canonical hooks require harness-specific event and action translation.
- Some features must be simulated, skipped, or rendered with reduced fidelity.
- Source and output roots can be separated.
- Generated output often must live directly in a repository where the harness discovers it.

Agentworks should use Rulesync as a guide to formats, target support, and the practical limits of a
canonical model. It should not require Rulesync at runtime in the initial design.

A runtime dependency would introduce Node or binary installation, version management, subprocess or
cross-language API concerns, and a repository-centric ownership model that does not cover
Agentworks's VM, resource-lifecycle, secret, and feature-output requirements. Direct rendering is
small enough to implement within each integration and keeps collision decisions under Agentworks
control.

The architecture should not preclude a future optional Rulesync-backed renderer. If one is added, it
should generate into staging first. Agentworks should inspect the resulting tree, apply its scope
and ownership rules, and install only approved outputs. Rulesync should not receive unrestricted
authority to delete or replace content in an arbitrary workspace.

## Safety Properties

The eventual requirements should preserve these properties:

1. A session operation never mutates an upstream resource to satisfy a prerequisite.
2. A capability receives only targets, secrets, and filesystem roots appropriate to its facet.
3. Every mutating facet has one explicit lifecycle owner.
4. Workspace and session configuration is fixed at create unless a later explicit lifecycle says
   otherwise.
5. VM and agent reinit are the intentional adoption paths for declarative changes at those levels.
6. `workspace repair` remains narrow and does not become artifact reconciliation.
7. Integrations describe actual harness discovery semantics and do not understate effect scope.
8. Existing repository and generator-owned content is never silently adopted or overwritten.
9. Filesystem plans reject absolute paths, traversal, unexpected symlinks, and scope escapes.
10. Ownership originates from a recorded successful write, not from a coincidental content match.
11. Resolved secret values never enter persisted configuration or artifact state.
12. Unsupported required contributions fail; unsupported advisory contributions warn.

## Existing Debt and Migration Direction

The current Claude-specific agent and admin fields, including `claude_marketplaces` and
`claude_plugins`, are evidence that user-scoped harness behavior lacks a proper home. They should
eventually migrate into the Claude harness integration's agent/admin facet configuration rather than
remain permanent core template vocabulary.

That migration should not occur until the facet selection, inheritance, last-applied state, and
backward-compatibility rules are specified. Existing resources need an honest adoption path through
VM or agent reinit, and existing session behavior must remain valid during the transition.

The session workload facet can migrate incrementally because it already exists. Wider facets and
artifact rendering should be additive first, with the current session-local effects prohibition
remaining in force until each owning lifecycle is wired.

## Suggested Design Sequence

1. Specify integration attachments at VM, admin, agent, workspace, and session ownership points.
2. Define the common facet descriptor, schema, dependency, grant, and state contracts.
3. Define last-applied configuration and generic facet-state persistence.
4. Add one vertical integration with a VM or agent facet and prove create/reinit semantics.
5. Add workspace create-time materialization with file-level ownership and collision handling.
6. Add upstream requirement reporting to session readiness without implicit remediation.
7. Migrate one existing harness-specific template field family into its owning facet.
8. Design first-class agentic contribution schemas and feature-produced outputs in a separate
   effort, using the established materialization seam.
9. Add direct integration renderers for the first artifact kinds, informed by Rulesync's target
   formats and support matrix.

## Questions for the Remaining SDD Artifacts

- What is the exact attachment and config shape at each template level?
- Does admin setup share the VM attachment or need an independently configured facet?
- How are multiple broader-scope integrations ordered, and how are conflicts reported?
- What requirement identifiers and severity vocabulary should session readiness expose?
- Which probes supplement persisted applied state, and when may they run?
- What generic schema stores resolved configuration, facet state, artifact ownership, and hashes?
- Where does workspace-local `.agentworks` metadata add value beyond the database?
- What is the retry contract for a partially created workspace with some artifacts already written?
- Which file composition strategies are safe enough to support initially?
- How are tracked repository files, untracked files, symlinks, and repository generators detected?
- Which artifact scope combinations do Codex and Claude Code actually support?
- Which Rulesync canonical concepts transfer cleanly, and which encode assumptions Agentworks should
  avoid?
- What explicit future operation, if any, may apply updated artifacts to an existing workspace?

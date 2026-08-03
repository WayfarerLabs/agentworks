# FRD: Harness Adapter (multi-scope tool integration and rename)

- Status: Draft
- Start date: 2026-08-03
- Supersedes: the planned `harness-user-provisioner` capability (see
  `cli/agentworks/capabilities/README.md` "Planned Future Capabilities")
- Related prior SDDs: `docs/sdd/2026-07-07-session-harness`, `docs/sdd/2026-08-01-codex-harness`

## Summary

The `harness` capability today is a session-scoped adapter: it knows how to launch and resume one
tool as a session's workload and nothing else. Two pressures have built up against that shape:

1. **Scope.** Real tool integration is not only session-deep. Getting a tool like Claude Code or
   Codex working well also involves user-level setup (installing it, authenticating it, user-level
   configuration) and workspace-level setup (publishing rules, skills, hooks, or workspace config).
   Today none of that has a home in the capability, so it either does not happen or is wired ad hoc
   outside the capability model.
2. **Naming.** The industry (and Anthropic's own Claude Code glossary) has settled on "harness"
   meaning the tool itself: "Claude Code is the harness; Claude is the model inside it." Agentworks
   uses "harness" for the Agentworks-side adapter that runs such a tool. That is a genuine misnomer,
   and it gets worse as the capability grows past the session, because the thing being named is no
   longer even the session runtime, it is the whole cross-scope integration.

This effort proposes: (a) expand the capability so a single tool integration contributes hooks at
multiple stages of the provisioning flow (user, workspace, session), under a per-hook scope
contract; and (b) rename the capability so it stops colliding with the industry meaning of
"harness".

This document covers functional requirements only. The interface shape, the readiness/lifecycle
wiring, and the migration mechanics are for the HLA, the plan, and a `migration-strategy.md`.

## Background and prior art

The term "harness" is no longer ambiguous in the agentic-coding space; it has converged on "the tool
that wraps a model into an agent" (the loop, tool calls, context management, memory, guardrails).
Anthropic's Claude Code glossary defines "agentic harness" and states plainly that Claude Code is
the harness and Claude is the model inside it. Independent glossaries and eval literature use the
term the same way, distinguishing "harness" (the running integration) from "scaffold" (the
instructions/tools the model starts from).

The consequence for Agentworks: naming our adapter "harness" fights the settled vocabulary. The
cleanest framing is a three-layer model:

1. the **model** (for example, Claude): the weights.
2. the **harness** (for example, Claude Code): the tool that turns the model into an agent. This is
   the industry meaning.
3. the **Agentworks adapter**: the thing that integrates a particular harness into Agentworks:
   installs it, authenticates it, configures it, launches and resumes it. Today's capability lives
   here but is named after layer 2.

Layer 3 is not one-to-one with layer 2: several distinct adapters can drive the same harness (an
interactive-transcript-resume adapter, a headless `-p` adapter for unattended runs, adapters that
differ only in how they provision the user or workspace). The current model bakes a one-to-one
assumption into the resource name (a resource literally named `claude-code`), which the expansion
makes untenable.

Sources are captured in the accompanying research notes; the load-bearing one is the Claude Code
glossary entry for "agentic harness" (`code.claude.com/docs/en/glossary`).

## Goals

- Give tool-specific integration a home at the user, workspace, and session stages of provisioning,
  in one cohesive unit per tool.
- Keep the safety story intact under expansion: effects stay bounded to the stage that produced
  them.
- Rename the capability so it no longer collides with the industry meaning of "harness", and remove
  the baked-in assumption that an adapter is identified with the tool it drives.
- Preserve a simple operator experience for the common case (pick a tool for a session).

## Non-goals

- Designing the concrete hook interface, readiness stages, or registry wiring (HLA).
- Building out multiple adapters per tool now. The model must **permit** multiplicity; shipping it
  is out of scope until a second adapter for some tool actually exists.
- Absorbing the account-isolation strategy (shared service account vs per-agent user vs locked-down
  sandbox) into the tool integration. That axis stays separate (see R4).
- Machine-scope provisioning. This effort covers user, workspace, and session scopes; machine scope
  is explicitly deferred.

## Personas

- **Operator**: runs Agentworks; enables tools and picks one for a session; wants each tool to be
  installed, authenticated, configured, and launched correctly without hand-wiring each stage; wants
  to pair a tool with an account-isolation posture of their choosing.
- **Integration author**: implements Agentworks support for a tool (a built-in or a plugin); wants
  one cohesive place for all of that tool's setup across scopes, with a clear contract for what each
  stage may and may not do.
- **Maintainer / reader**: wants the capability's name and model to reflect reality (an adapter that
  integrates a harness), not a misnomer that has already confused the team.

## Functional requirements

- **R1 (multi-scope integration).** The capability MUST be invocable at the user-provisioning,
  workspace-provisioning, and session-launch stages, contributing tool-specific setup at each stage
  it opts into.
- **R2 (per-hook scope containment).** Each stage's contribution MUST stay within that stage's
  scope: a user hook affects only the user, a workspace hook only the workspace, a session hook only
  the session. No hook may cause effects at a wider scope than its stage. This replaces the current
  blanket "a harness must stay session-scoped" rule with a per-stage version of the same guarantee.
- **R3 (optional stages).** An integration that needs nothing at a given stage MUST be able to omit
  it, defaulting to a no-op. Example: `shell` needs no user or workspace provisioning.
- **R4 (account strategy stays orthogonal).** The account-isolation/structure strategy (shared
  service account, per-agent user, locked-down sandbox) MUST remain independent of the tool
  integration, so any tool pairs with any strategy without the integration's code changing. The
  integration owns tool-specific setup; it MUST NOT own the account-shape decision.
- **R5 (cohesion).** All tool-specific integration for one tool SHOULD live in one implementation
  unit, so an author reasons about (and a reader finds) a tool's whole Agentworks story in one
  place.
- **R6 (operator surface stays simple).** Selecting a tool for a session MUST remain a single,
  simple choice. Existing session-templates that reference a tool by name MUST either keep working
  or be carried by a defined migration (see R10).
- **R7 (rename the kind).** The capability kind MUST be renamed to remove the "harness-is-the-tool"
  collision. Candidate names and the recommendation are in Decision D1. The word "workload" is
  reserved for the session-scoped facet (the thing that runs in the pane), not the kind, because it
  describes only one stage.
- **R8 (adapter identity vs tool attribute).** An adapter MUST have its own identity and declare
  which underlying harness/tool it drives, rather than being identified with the tool. This permits
  several adapters for one tool. For the current one-to-one reality, an adapter MAY take the tool's
  name as its default identity (ergonomics), but the model MUST NOT assume identity equals tool.
- **R9 (supersede `harness-user-provisioner`).** This multi-scope model supersedes the planned
  separate `harness-user-provisioner` capability for tool-specific provisioning. The "Planned Future
  Capabilities" section MUST be updated to reflect that, keeping the orthogonal account-strategy
  concern (R4) as the part that may still warrant its own capability.
- **R10 (migration).** Because the kind slug appears in operator configs, YAML manifests, the
  registry, `agw resource list --kind ...`, plugin descriptors, and docs, the rename is a breaking
  change and MUST ship with a migration path (a deprecation shim and/or an `agw resource migrate`
  step). The mechanics belong in `migration-strategy.md`.

## User stories

- As an operator, I enable Claude Code and it is installed, authenticated, and configured at the
  user level, its workspace integration is published, and each session launches and resumes it,
  without my wiring each stage by hand.
- As an operator, I pair the same tool with either a shared service account or a per-agent user, and
  the tool's integration does not change.
- As an integration author, I implement one unit that declares what my tool needs at the user,
  workspace, and session stages, and I rely on the framework to call each at the right time with a
  clear scope contract.
- As a reader, I look at the capability's name and understand it is the adapter that integrates a
  harness, not the harness itself.

## Open decisions

- **D1 (kind name).** Options, with the trade-off to settle explicitly rather than by feel:
  - `harness-adapter`: keeps the now-standard "harness" vocabulary and is honest that our thing is
    the adapter. Slightly long. `shell` is a degenerate ("no-harness") member.
  - `tool-adapter`: neutral; spans install/configure/run naturally; less tied to the "harness" term;
    more generic.
  - `tool`: cleanest single word and spans scopes better than `workload`, but re-collapses the
    adapter/tool distinction at the name level (mitigated by R8 making the tool an explicit
    attribute).
  - Rejected for the kind: `workload` (names only the session facet; repeats the under-scoping the
    rename is meant to fix). Recommendation: **`harness-adapter`**, keeping `workload` for the
    session facet. Revisit against `tool-adapter` in HLA.
- **D2 (selector field).** Keep the operator-facing `harness:` selector as sugar for "the harness I
  want this session to run" (which is correct under the industry meaning) even though the kind is
  renamed, versus renaming the field too. Recommendation: keep `harness:` for operators; the rename
  is primarily to the kind and the implementation vocabulary.
- **D3 (multiplicity now vs later).** Permit multiple adapters per tool in the model (R8) but do not
  build the selection UX or ship a second adapter until one is needed. Recommendation: permit, do
  not build.
- **D4 (stage set).** Confirm the exact stages that get hooks (user, workspace, session) and that
  machine scope is deferred (a non-goal here).

## Out of scope for this FRD

The hook interface and lifecycle wiring (HLA), the phased plan and definitions of done (plan), and
the rename/migration mechanics (`migration-strategy.md`) are follow-ups, reviewed after this FRD is
agreed.

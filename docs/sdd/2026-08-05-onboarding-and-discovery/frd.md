# FRD: Onboarding and Discovery

- Status: Draft
- Start date: 2026-08-05
- Roadmap: `docs/sdd/2026-08-04-next-steps` (this effort is the onboarding-and-discovery child,
  destination 1 of that roadmap; the user perspective in its `inputs/` is the source material)
- Related SDDs: `docs/sdd/2026-07-31-declarative-schema` (wave 2, running in parallel; its schema
  emission, samples, and describe surfaces are this effort's raw material as they land)

## Summary

Agentworks has accumulated substantial functionality and complexity, and new-operator feedback says
it is a lot to take in. This effort delivers the roadmap's first destination: an onboarding and
discovery experience that scales with the surface because it is derived from the platform's own
registries, schemas, and samples, and that serves humans and agents alike.

The delivery model is plan A from the roadmap's user perspective: an operator already has a vanilla
harness (Claude Code, Codex) on their workstation and uses it to set Agentworks up and manage it.
Agentworks publishes harness-specific onboarding plugins or marketplace entries in its own
repository; the operator adds one straight from GitHub, asks their agent to set up Agentworks, and
the agent executes the onboarding process described in the skill, installing, configuring, and using
the CLI as needed. The onboarding agent deliberately sits outside Agentworks: a managed agent must
not modify the system it runs in, so the agentic-artifacts layer is not the delivery path for
onboarding skills.

The first slice needs nothing from wave 2. The schema-derived depth (generated samples, describe
surfaces, dynamic content) adopts wave 2's surfaces as they land rather than blocking on them.

## Functional requirements

- **R1 (harness plugin delivery).** Agentworks MUST publish a harness-specific onboarding plugin or
  marketplace entry in its own repository, installable directly from GitHub, for Claude Code first
  and Codex before this effort closes. The plugin's skills MUST teach an agent to onboard and manage
  Agentworks using the CLI, and MUST track CLI releases in a defined way (the versioning scheme is
  the HLA's call).
- **R2 (progressive golden path).** Onboarding MUST reach a first working session in minutes, with
  the major capability areas (config, resources, plugins, secrets, VMs, workspaces, sessions)
  discoverable progressively from there rather than front-loaded.
- **R3 (idempotent and rerunnable).** Onboarding MUST be rerunnable at any time: already-done steps
  are recognized and reported rather than redone, so operators revisit it to confirm they are
  getting the most out of the platform as it evolves. Together with R2 and R5 this delivers the
  assisted onboarding flow of issue #391.
- **R4 (consent-first probing).** Probing the operator's machine MUST happen with consent and
  explanation, never silently: ask before looking for existing material (SSH keys are the canonical
  example), state what will be looked for and what will never be read, and honor refusals with a
  manual alternative. Onboarding is where trust is established.
- **R5 (interactive and non-interactive).** Both a guided path and a scriptable, replayable
  non-interactive path MUST exist, producing equivalent results.
- **R6 (derived content).** Onboarding and discovery content MUST derive from the platform's
  registries and, as wave 2 lands them, its emitted schemas, live samples, and describe surfaces, so
  it cannot drift from behavior. Hand-written connective narrative is bounded and clearly separated
  from derived fact. Content unavailable before wave 2's surfaces exist MUST be staged to adopt
  them, not reimplemented against them.
- **R7 (machine-readable output).** The CLI MUST offer a machine-readable output contract covering
  at least the existing entity list and describe commands and doctor, so agents consume the same
  facts humans see. Whether this is per-command flags or a global output mode is the HLA's call; the
  contract MUST be documented and stable once shipped. Wave 2's schema and describe surfaces adopt
  the same contract when they land under the names wave 2 chooses; this effort does not define
  contracts over surfaces wave 2 has not yet named.
- **R8 (capability discovery).** `agw` MUST be able to answer "what can you do and how do I use it"
  from its own inventory. Before wave 2's surfaces exist, that answer covers the registry inventory
  (capability kinds and registered implementations, per the merged descriptor contract);
  configuration-surface answers adopt wave 2's emitted schemas, samples, and describe surfaces as
  they land, under R6's staging rule. Discovery MUST stay coherent with `agw doctor`'s view of what
  is configured, ready, and wrong.
- **R9 (discovery conventions).** New CLI surfaces MUST follow the platform's list/describe
  conventions, participate in shell completions, and update docs in the same commits as behavior.

## Personas and stories

- As a new operator with Claude Code on my workstation, I add the Agentworks plugin from GitHub, ask
  my agent to set Agentworks up, and reach my first working session in minutes, being asked before
  anything on my machine is examined.
- As a returning operator, I rerun onboarding after an upgrade and see what is new and what I have
  not adopted, without redoing what is already configured.
- As an operator who scripts environments, I run the non-interactive path in provisioning and get
  the same result the guided path produces.
- As an agent operating the CLI on an operator's behalf, I read machine-readable list, describe, and
  doctor output and skills that teach the current surface, not a stale tutorial.
- As the wave 2 effort lead, my emitted schemas and samples get consumed by this effort's surfaces
  instead of a parallel hand-maintained copy.

## Non-goals

- Delivering onboarding skills through the agentic-artifacts layer (wave 6). The onboarding agent
  sits outside Agentworks by design.
- Onboarding driven by an Agentworks-managed agent. A managed agent must not modify the system it
  runs in.
- Building schema emission, live samples, or describe surfaces themselves; those are wave 2's, and
  their CLI naming is wave 2's call (coordinated with this effort).
- Editor integration for manifest authoring. It follows wave 2's emission as schema-derived depth
  per the roadmap's phasing; pulling it into this effort later is a plan change for the effort lead
  to record, not baseline scope.
- A documentation-site overhaul. This effort's docs work is bounded to onboarding and discovery
  surfaces.

## Acceptance criteria

1. A fresh operator with a vanilla Claude Code and no prior Agentworks knowledge reaches a working
   session using only the published plugin and its skills, with every probe consented.
2. Rerunning onboarding on an unchanged, fully-adopted system is a clean no-op; rerunning after an
   upgrade reports the delta (new and not-yet-adopted capability) without redoing completed work.
3. The non-interactive path reproduces the guided path's result on a clean machine.
4. List, describe, and doctor surfaces offer documented machine-readable output consumed by the
   plugin's skills themselves (dogfooding the contract).
5. Discovery answers are generated from the live registry inventory; adding an implementation
   changes the answers without touching onboarding content.
6. Completions and docs are current for every surface this effort adds (repo rule).
7. The Codex plugin exists with parity for the onboarding path before the effort locks.

## Decisions

- **D1 (plan A).** The vanilla-harness plugin model is settled (operator ruling recorded in the
  roadmap's user perspective and target-state); this FRD does not reopen it.
- **D2 (parallel to wave 2).** This effort seeds now, consumes wave 2's surfaces as they land, and
  must not block on them nor duplicate them.
- **D3 (Claude Code first).** First-party onboarding ships for Claude Code first (the operator's
  primary harness), Codex second, both before lock (R1, AC7).
- **D4 (issue #390 disposition).** The "examples system plugin" ask is subsumed rather than built:
  wave 2's live-rendered samples (adopted here per R6) provide example content that cannot drift,
  and this effort's discovery surfaces present it. No separate examples plugin ships; #390 closes
  against that combination.

## Open questions

- The concrete onboarding form: a guided `agw` command, a skill-driven tour, or both, and where the
  step-state for rerunnability lives (HLA's call).
- The machine-readable output contract's shape: per-command `--json` versus a global output mode,
  and its versioning story (HLA's call, informed by issue #257).
- Plugin release engineering: how marketplace entries version against CLI releases and how
  compatibility is checked at onboarding time (HLA's call).
- What feedback loop tells us onboarding is working for new operators (recorded from the user
  perspective; may resolve to a simple ask-the-operator step rather than telemetry).

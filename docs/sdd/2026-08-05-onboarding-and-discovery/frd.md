# FRD: Onboarding, Discovery, and Management

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
harness (Claude Code or Codex) on their workstation and uses it to set Agentworks up and manage it.
Agentworks publishes a marketplace and plugin (or whatever packaging the harness requires; both
Claude Code and Codex support the marketplace-plus-plugin model) in its own repository; the operator
adds it straight from GitHub, asks their agent to set up Agentworks, and the agent executes the
process described in the skill, installing, configuring, and using the CLI as needed. The onboarding
agent deliberately sits outside Agentworks: a managed agent must not modify the system it runs in,
so the agentic-artifacts layer is not the delivery path for these skills.

The skill is positioned as onboarding, discovery, and management, one lifecycle: discovery is
onboarding on an ongoing basis (a section of the same skill, not a separate thing), and management
is the day-two work the same agent keeps doing with the same CLI surfaces.

The centerpiece mechanism is the guide command (R13): the CLI serves its own teaching content,
blending static authored material with dynamic material from the live system, and the published
plugins reduce to thin bootstraps that point at it. Teaching content versions with the surfaces it
teaches and cannot fork across harnesses, because there is exactly one copy and the CLI serves it.

The first slice needs nothing from wave 2. The schema-derived depth (generated samples, describe
surfaces, dynamic content) adopts wave 2's surfaces as they land rather than blocking on them.

## Functional requirements

- **R1 (marketplace and thin-bootstrap plugins).** Agentworks MUST publish a marketplace and plugin
  (or the harness's equivalent packaging) in its own repository, installable directly from GitHub,
  for both Claude Code and Codex. The marketplace MUST be structured so additional Agentworks
  plugins can be added to it later; this effort's plugin is the first entry, not the marketplace's
  shape. Each plugin's skill is a deliberately thin bootstrap: install the CLI, state the R12
  security disclosure, and direct the agent to run the guide command (R13) for everything else.
  Teaching content lives in the CLI, not the plugins, so it versions with the surfaces it teaches;
  the bootstrap's own (small) compatibility story is the HLA's call.
- **R2 (progressive golden path).** Onboarding MUST reach a first working session in minutes, with
  the major capability areas (config, resources, plugins, secrets, VMs, workspaces, sessions)
  discoverable progressively from there rather than front-loaded.
- **R3 (idempotent and rerunnable).** Onboarding MUST be rerunnable at any time: already-done steps
  are recognized and reported rather than redone, so operators revisit it to confirm they are
  getting the most out of the platform as it evolves. Together with R2 and R5 this delivers the
  assisted onboarding flow of issue #391.
- **R4 (agent probes, agw verifies; consent-first).** Agentworks itself never probes the operator's
  machine. Discovery of existing material (SSH keys are the canonical example, installed tools
  another) is the agent's act, and the guide's onboarding content MUST instruct it to be
  consent-first: ask before looking, state what will be looked for and what will never be read, and
  honor refusals with a manual alternative. Within that consent frame the content SHOULD direct the
  agent to verify wherever possible rather than trusting declarations, using non-probing
  verification surfaces `agw` provides for configured state: confirm a secret reference (a 1Password
  item, for example) resolves without reading its value, test SSH connections, confirm installed
  tools respond. The result is verified setup, not blind configuration, and onboarding is where
  trust is established, in both directions.
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
- **R10 (ongoing management).** The skill MUST cover management, not only first-run onboarding:
  day-two operations (creating and changing resources, adopting new capability, resolving
  deprecations across upgrades, troubleshooting with doctor) through the same CLI surfaces, so the
  agent that onboarded the operator remains useful for the life of the installation.
- **R11 (cross-harness parity, by construction).** Teaching content lives in the CLI (R13), so it
  cannot fork between harnesses. The thin bootstraps (R1) MUST NOT drift apart in substance: share
  or generate them from one source where the harness formats allow (the repo's own Rulesync pipeline
  is prior art); otherwise a CI guard verifies equivalence. The guard's scope is the bootstraps
  only.
- **R12 (security disclosure).** Before setup proceeds, onboarding MUST state plainly that an agent
  managing Agentworks gains access to everything Agentworks can reach: every managed resource and
  secret reference, and anything accessible over SSH from the operator's machine. It MUST recommend
  a strict harness security posture, especially once Agentworks is in real use, and point at the
  relevant settings rather than leaving the recommendation abstract.
- **R13 (the guide command).** The CLI MUST provide `agw guide [topic ...]`, serving skill-shaped
  markdown for agents and humans alike. With no topic it gives a top-level overview and lists the
  available topics. Topics MUST cover at least: resource kinds (`agw guide vm-template`, including
  the live list of defined instances with descriptions), specific resources
  (`agw guide vm-template/heavy`, a cheap delegation over the same sources describe uses),
  capability implementations (`agw guide secret-backend/onepassword`, including configuration), and
  `concept-` prefixed meta topics (`agw guide concept-secrets`, `agw guide concept-onboarding`)
  whose shared prefix avoids collisions with kind names and makes concept docs discoverable by
  completion. Content blends static authored material with dynamic material from the live system
  (registries, resources, enablement state), so a disabled implementation or an added resource
  changes what the guide says. Output is markdown only; structured data appears only inside the
  markdown. R7's machine-readable contract is a separate surface and stays so. Topics are sized like
  skills, with sub-topics referenced rather than inlined, and topic names participate in shell
  completions. Prior art for the effort's `prior-art-research.md`: PowerShell's module-contributed
  `about_*` topics, `kubectl explain`'s live schema walks, `git help` concept guides, `go help`
  topics, `rustc --explain`, and Terraform's per-provider schema-plus-prose docs generation.
- **R14 (universal contribution).** Guide content MUST arrive through one generic contract that
  every participant uses: core resource kinds, capability implementations, and plugins (system
  today, external later) each contribute their own topics. Built-in static content lives beside the
  kind or implementation it documents, so it is maintained with the code it describes; plugin
  content is bundled with the plugin. A participant that contributes nothing simply has no topic;
  there are no empty stubs. The contract's shape (and whether it becomes a descriptor concern) is
  the HLA's call, designed with wave 2 so schema walkers, samples, and blurbs are shared sources
  rendered differently by describe (reference) and guide (teaching).
- **R15 (safe templating over a me-anchored graph projection).** Contributed content that needs
  dynamic material (concept docs, kind overviews, anything that lists) uses a locked-down template
  mechanism: declarative placeholders and core-provided dynamic blocks (live instance lists,
  enablement state, schema fragments), not a general-purpose template engine with expression
  evaluation. The data those blocks draw on is a pared-down, read-only projection of the resource
  graph, anchored with shorthand: `me` is the kind, implementation, or resource the topic documents,
  and the vocabulary is traversals from `me` (its instances, its kind, resources it references or
  that reference it) plus a small set of named roots for concept topics. Anchoring on `me` makes
  templates position-independent, so a shared kind-overview template serves every kind. The
  projection carries identity, descriptions, enablement and readiness, and relationships; secrets
  and secret-bearing config are excluded at the projection boundary, not by per-template discipline.
  Rendering is side-effect-free: it never resolves secrets, probes targets, or mutates state (probes
  belong to onboarding actions, not to displaying a page). The projection is expected to be the
  resource graph itself running in a gated access mode, not a second structure kept in lockstep:
  powers (secret readers, run targets, capability API objects) sit behind callables a mode can gate,
  while universal facts are plain data on the nodes. In a gated mode, data is only what is already
  materialized (finalize-computed verdicts, stored rows, declared relationships); nothing lazily
  computes through a power while wearing attribute syntax, which is also what keeps rendering
  side-effect-free. Whether a mode gates by permission check or by leaving the power unwired is the
  HLA's call; both are legitimate mechanisms, done properly. Templates fill in the dynamic bits
  only: the authored text still carries the teaching, and the effort should not over-index on
  pushing everything into the graph. Contributed guide content MUST be data, never code, and
  rendering MUST NOT execute anything a contribution supplies. This holds for curated system plugins
  now precisely so the content channel is already safe when external plugins arrive (wave 8).
- **R16 (README bootstrap block).** The repository README's getting-started section MUST lead with a
  single copy-paste block (a fenced block, so GitHub renders a copy button) addressed to the
  operator's agent, along these lines: "I'd like your help installing and setting up Agentworks.
  It's available on PyPI as `agentworks-cli` and runs on any Python runtime >= 3.12 (uv is the
  standard approach). Please install it and then run `agw guide` to get started." This is a
  first-class zero-plugin onboarding path; the harness plugins (R1) say essentially the same thing
  and remain primarily an advertising and discoverability channel.

## Personas and stories

- As a new operator with Claude Code or Codex on my workstation, I add the Agentworks marketplace
  and plugin from GitHub, ask my agent to set Agentworks up, and reach my first working session in
  minutes, told up front what access I am granting and asked before anything on my machine is
  examined.
- As an operator six months in, I ask the same agent to add a VM site or rotate a secret, and the
  skill's management section teaches it the current surfaces rather than leaving it to guess.
- As a returning operator, I rerun onboarding after an upgrade and see what is new and what I have
  not adopted, without redoing what is already configured.
- As an operator who scripts environments, I run the non-interactive path in provisioning and get
  the same result the guided path produces.
- As an agent operating the CLI on an operator's behalf, I run `agw guide` to learn the current
  surface (never a stale tutorial) and read machine-readable list, describe, and doctor output for
  facts.
- As a human operator, I run `agw guide concept-secrets` in a terminal and get the same teaching
  content my agent gets.
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

1. A fresh operator with a vanilla Claude Code or Codex and no prior Agentworks knowledge reaches a
   working session using only the published bootstrap plugin and the guide command, with every probe
   consented.
2. Rerunning onboarding on an unchanged, fully-adopted system is a clean no-op; rerunning after an
   upgrade reports the delta (new and not-yet-adopted capability) without redoing completed work.
3. The non-interactive path reproduces the guided path's result on a clean machine.
4. List, describe, and doctor surfaces offer documented machine-readable output consumed by the
   plugin's skills themselves (dogfooding the contract).
5. Discovery answers are generated from the live registry inventory; adding an implementation
   changes the answers without touching onboarding content.
6. Completions and docs are current for every surface this effort adds (repo rule).
7. Both harness bootstraps exist, equivalent per R11, and both pass criterion 1's fresh-operator
   test.
8. The security disclosure (R12) appears before any setup action in both the guided and
   non-interactive paths.
9. Verification during onboarding is agent-driven and consented per the guide's instructions;
   `agw`'s verification surfaces read no secret values, and a declined probe leaves an explicit
   manual-verification note.
10. The README's getting-started section leads with the R16 copy-paste block, and following it on a
    clean machine reaches `agw guide` successfully.
11. `agw guide` with no topic lists every available topic; a kind topic reflects the live instance
    list; disabling an implementation visibly changes its topic's rendering.
12. Guide topics complete in the shell, including `concept-` prefix discovery, and the completion
    tree includes dynamic topic elements per the repo's completions mechanism.
13. A plugin's contributed guide content renders with zero contributed code executed (R15),
    demonstrated by a test that rejects a contribution attempting expression evaluation.

## Decisions

- **D1 (plan A).** The vanilla-harness plugin model is settled (operator ruling recorded in the
  roadmap's user perspective and target-state); this FRD does not reopen it.
- **D2 (parallel to wave 2).** This effort seeds now, consumes wave 2's surfaces as they land, and
  must not block on them nor duplicate them.
- **D3 (both harnesses, no ordering).** Claude Code and Codex plugins are both required deliverables
  with equivalent content (R1, R11, AC7). Build order is the effort lead's call; neither is an
  afterthought.
- **D4 (issue #390 disposition).** The "examples system plugin" ask is subsumed rather than built:
  wave 2's live-rendered samples (adopted here per R6) provide example content that cannot drift,
  and this effort's discovery surfaces present it. No separate examples plugin ships; #390 closes
  against that combination.
- **D6 (bootstrap posture; operator rulings, 2026-08-06).** The README copy-paste block is an equal
  first-class bootstrap beside the plugins, which are kept primarily for advertising and
  discoverability. Agentworks never probes the operator's machine; agents probe (consent-first per
  guide content) and `agw` verifies.
- **D5 (the guide command; operator rulings, 2026-08-05).** The teaching command is `agw guide`,
  deliberately not `skill` (wave 6 needs "skill" as an artifact noun). Output is markdown only. Meta
  topics carry the `concept-` prefix. Teaching content lives in the CLI with thin plugin bootstraps,
  and contributed content is data rendered through locked-down templating, never code.

## Open questions

- How `concept-onboarding` determines done versus not-yet-done from live state, and whether any
  step-state must persist beyond what the system already records (HLA's call).
- The machine-readable output contract's shape: per-command `--json` versus a global output mode,
  and its versioning story (HLA's call, informed by issue #257).
- The R14 contribution contract's shape, including whether it becomes a descriptor concern, and the
  exact split with wave 2 (shared sources, two presentations); being negotiated with the wave 2
  effort lead via the roadmap's note.
- The R15 template vocabulary: which core-provided dynamic blocks exist first, how a contribution
  declares which blocks it uses, the projection's traversal set from `me`, how concept topics anchor
  (named roots versus anchoring at the contributor), and the access-mode gating mechanism
  (permission check versus unwired powers, per surface) (HLA's call).
- Topic taxonomy details: precedence if a future topic name ever collides with a kind slug, and the
  behavior of multiple topics in one invocation.
- Bootstrap release engineering: how the thin bootstraps declare which CLI versions they bootstrap
  (small, since teaching content ships in the CLI), and the R11 bootstrap-sharing mechanism.
- What feedback loop tells us onboarding is working for new operators (recorded from the user
  perspective; may resolve to a simple ask-the-operator step rather than telemetry).

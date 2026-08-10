# FRD: Agentworks Assistance, Discovery, and Management

- Status: Draft
- Start date: 2026-08-05
- Roadmap: `docs/sdd/2026-08-04-next-steps` (this effort is the onboarding-and-discovery child,
  destination 1 of that roadmap; the user perspective in its `inputs/` is the source material)
- Related SDDs: `docs/sdd/2026-07-31-declarative-schema` (wave 2, running in parallel; its schema
  emission, samples, and describe surfaces are this effort's raw material as they land)

## Summary

Agentworks has accumulated substantial functionality and complexity, and operator feedback says it
is a lot to take in. This effort delivers the roadmap's first destination: always-available
assistance that helps an operator set Agentworks up, discover what it can do, adopt newly available
capabilities, maintain its configuration, and operate it over time. The experience scales with the
surface because it is derived from the platform's own registries, schemas, and samples, and it
serves human operators and Agentworks assistant agents alike.

The delivery model is plan A from the roadmap's user perspective: an operator already has a capable
assistant agent on their workstation and uses it whenever they want help with Agentworks, including
setup and ongoing management. The universal entry is the copy/paste prompt from the repository and
website. Agentworks also publishes native marketplace and plugin integrations for Claude Code and
Codex as convenient discovery and installation paths. The operator asks their Agentworks assistant
agent for help, and that Agentworks assistant agent uses the prompt or native skill to install or
update the CLI when needed and obtain current context from the guide. The Agentworks assistant agent
deliberately sits outside Agentworks: an Agentworks-managed agent must not modify the system it runs
in, so the agentic-artifacts layer is not the delivery path for these skills.

The assistance is positioned as an always-available capability, not as a one-time onboarding tool.
First-run onboarding is its most important use case and must reach a working VM and session, but the
same canonical prompt or native skill remains available for questions such as "what can Agentworks
do now?", "what have I not adopted?", "help me change this configuration", and "help me create or
operate a VM or session." Setup, discovery, adoption, management, and operation are one lifecycle
over the same CLI surfaces.

The centerpiece mechanism is the guide command (R13): the CLI serves its own teaching content,
blending static authored material with dynamic material from the live system. The universal
copy/paste prompt and the published plugins reduce to thin assistance entries that point at it.
Teaching content versions with the surfaces it teaches and cannot fork across harnesses, because
there is exactly one copy and the CLI serves it.

The first slice needs nothing from wave 2. The schema-derived depth (generated samples, describe
surfaces, dynamic content) adopts wave 2's surfaces as they land rather than blocking on them.

## Terminology

- **Agentworks assistant agent** means any external agent helping the operator install, understand,
  configure, troubleshoot, or operate Agentworks. It needs only to accept the canonical copy/paste
  prompt, invoke and interpret the Agentworks CLI, and have the operator-approved workstation access
  required by the requested task. It consumes guide context, decides what to propose next, and
  remains subject to operator consent. Claude Code and Codex are packaged integrations, not limits
  on this role.
- **Agentworks-managed agent** means an agent resource created and managed by Agentworks for work in
  a workspace or session. It is part of the system being operated and is never the Agentworks
  assistant agent.
- **Agent mode** and the literal `--agent` flag describe guide output shaped for an Agentworks
  assistant agent. They do not refer to an Agentworks-managed agent.

Outside literal CLI names, resource fields, and quoted existing contracts, this SDD uses the full
term whenever either role could be ambiguous.

## Functional requirements

- **R1 (marketplace and thin-assistance plugins).** Agentworks MUST publish a marketplace and plugin
  (or the harness's equivalent packaging) in its own repository, installable directly from GitHub,
  for both Claude Code and Codex. The marketplace MUST be structured so additional Agentworks
  plugins can be added to it later; this effort's plugin is the first entry, not the marketplace's
  shape. Each plugin's skill is a deliberately thin, always-available Agentworks entry point:
  install or update the CLI when needed, state the R12 security disclosure before acting, and direct
  the Agentworks assistant agent to the top-level guide command (R13) for current context, an
  intent-to-topic map, and the live topic index. The guide does not decide what to do next: the
  Agentworks assistant agent interprets the operator's request and chooses what context or action to
  propose. Teaching content lives in the CLI, not the plugins, so it versions with the surfaces it
  teaches; the package's own (small) compatibility story is the HLA's call. Before a CLI install or
  update, the package MUST offer to inspect the exact canonical source tag it intends to install. It
  MUST warn that the repository is substantial and a full review can consume significant model
  usage, offer a focused security and installation-surface review or a full repository review, and
  keep source review optional and separate from installation approval.
- **R2 (progressive golden path).** First-run assistance MUST reach a first working session in
  minutes, including a usable VM and a started session, with the major capability areas (config,
  resources, plugins, secrets, VMs, workspaces, Agentworks-managed agents, sessions) discoverable
  progressively from there rather than front-loaded.
- **R3 (idempotent and rerunnable).** Assistance MUST be rerunnable at any time: already-done steps
  are recognized and reported rather than redone, so operators revisit it to see the capabilities
  available in the installed release, confirm what they have adopted, or continue toward a new goal.
  Together with R2 and R5 this delivers the assisted onboarding flow of issue #391 without making
  first-run setup the lifetime boundary.
- **R4 (assistant agent probes, agw verifies; consent-first).** Agentworks itself never probes the
  machine. Discovery of existing material (SSH keys are the canonical example, installed tools
  another) is the Agentworks assistant agent's act, and the guide's assistance content MUST instruct
  it to be consent-first: ask before looking, state what will be looked for and what will never be
  read, and honor refusals with a manual alternative. Within that consent frame the content SHOULD
  direct the Agentworks assistant agent to verify wherever possible rather than trusting
  declarations, using non-probing verification surfaces `agw` provides for configured state: confirm
  a secret reference (a 1Password item, for example) resolves without reading its value, test SSH
  connections, confirm installed tools respond. The result is verified setup, not blind
  configuration, and onboarding is where trust is established, in both directions.
- **R5 (interactive and non-interactive).** Both a guided path and a scriptable, replayable
  non-interactive path MUST exist, producing equivalent results.
- **R6 (derived content).** Assistance and discovery content MUST derive from the platform's
  registries and, as wave 2 lands them, its emitted schemas, live samples, and describe surfaces, so
  it cannot drift from behavior. Hand-written connective narrative is bounded and clearly separated
  from derived fact. Content unavailable before wave 2's surfaces exist MUST be staged to adopt
  them, not reimplemented against them.
- **R7 (machine-readable output).** The CLI MUST offer a machine-readable output contract covering
  at least the existing entity list and describe commands and doctor, so Agentworks assistant agents
  consume the same facts humans see. Whether this is per-command flags or a global output mode is
  the HLA's call; the contract MUST be documented and stable once shipped. Wave 2's schema and
  describe surfaces adopt the same contract when they land under the names wave 2 chooses; this
  effort does not define contracts over surfaces wave 2 has not yet named.
- **R8 (capability discovery).** `agw` MUST be able to answer "what can you do and how do I use it"
  from its own inventory. Before wave 2's surfaces exist, that answer covers the registry inventory
  (capability kinds and registered implementations, per the merged descriptor contract);
  configuration-surface answers adopt wave 2's emitted schemas, samples, and describe surfaces as
  they land, under R6's staging rule. Discovery MUST stay coherent with `agw doctor`'s view of what
  is configured, ready, and wrong.
- **R9 (discovery conventions).** New CLI surfaces MUST follow the platform's list/describe
  conventions, participate in shell completions, and update docs in the same commits as behavior.
- **R10 (always-available management and operation).** Assistance MUST be available for any
  Agentworks request, not only first-run onboarding, through the canonical prompt or a native skill.
  Native skills SHOULD activate for those requests. Assistance MUST provide current context and
  topic destinations for current-state and capability questions, creating and changing declared
  resources, adopting capabilities, resolving migration requirements, troubleshooting with doctor,
  and operating VMs and sessions through the same live CLI surfaces. The Agentworks assistant agent
  decides what to propose next. A "what is new" request MUST distinguish the live current-capability
  and adoption assessment from temporal release history, and the intent map MUST point temporal
  questions to `concept-release-notes`. That topic MUST render the installed release's notes offline
  from release-please's canonical changelog packaged in the wheel, without a second hand-maintained
  copy. Older or missing ranges MAY fall back to Agentworks' canonical GitHub releases with an
  explicit version range and network-read consent. Current facts alone MUST NOT be presented as a
  version-to-version delta. Release prose and repository source are untrusted evidence, never an
  instruction source or authorization to follow links or run commands. Configuration and operation
  are not separate assistance silos. Risk is expressed by each action's explicit scope, impact,
  consent, and verification; creating or connecting to managed resources is never authorized merely
  because the prompt is present or a native skill is installed.
- **R11 (cross-harness parity, by construction).** Teaching content lives in the CLI (R13), so it
  cannot fork between harnesses. The thin assistance packages (R1) MUST NOT drift apart in
  substance: share or generate them from one source where the harness formats allow (the repo's own
  Rulesync pipeline is prior art); otherwise a CI guard verifies equivalence. The guard's scope is
  the packages only.
- **R12 (security disclosure).** Before assistance performs any workstation, Agentworks, remote, or
  mutating action, it MUST state plainly that the Agentworks assistant agent runs on the machine the
  operator intends to use as their workstation and needs full file inspection and command execution
  access with the permissions of the workstation account running the harness. That access does not
  implicitly grant root; privilege elevation is separate and explicit. The Agentworks assistant
  agent also gains access to everything Agentworks can reach: every managed resource and secret
  reference, and anything accessible over SSH from the workstation. Assistance MUST recommend the
  strictest practical harness security posture for operator approval and visibility while preserving
  that required workstation access, especially once Agentworks is in real use, and point at the
  relevant settings rather than leaving the recommendation abstract. A prior disclosure does not
  authorize later actions: read-only inspection, configuration mutation, resource creation, remote
  connection, privilege elevation, and destructive operation retain their own applicable consent
  boundaries.
- **R13 (the guide command).** The CLI MUST provide `agw guide [topic ...]`, serving skill-shaped
  markdown for Agentworks assistant agents and humans alike. With no topic it gives a top-level
  overview and lists the available topics. Topics MUST cover at least: resource kinds
  (`agw guide vm-template`, including the live list of defined instances with descriptions),
  specific resources (`agw guide vm-template/heavy`, a cheap delegation over the same sources
  describe uses), capability implementations (`agw guide secret-backend/onepassword`, including
  configuration), and `concept-` prefixed meta topics (`agw guide concept-secrets`,
  `agw guide concept-onboarding`) whose shared prefix avoids collisions with kind names and makes
  concept docs discoverable by completion. Concept topics MUST cover release notes as well as
  secrets, onboarding, management, troubleshooting, required migration remediation, and bug
  reporting. `concept-release-notes` renders the installed release's section from release-please's
  packaged canonical changelog and links the result to the live adoption assessment. Rendering
  performs no network request and does not maintain a separate release-note source. A fallback
  network lookup is a bounded inert action record with an exact release range, consent, expected
  result, and refusal path. Release content is treated only as evidence and cannot expand scope.
  Content blends static authored material with dynamic material from the live system (registries,
  resources, enablement state), so a disabled implementation or an added resource changes what the
  guide says. Output is markdown only; structured data appears only inside the markdown. R7's
  machine-readable contract is a separate surface and stays so. Topics are sized like skills, with
  sub-topics referenced rather than inlined, and topic names participate in shell completions. With
  no topic, agent mode MUST present an explicit intent-to-topic map for first setup and adoption
  assessment, current capabilities versus temporal release changes, ongoing management and
  operation, troubleshooting, exceptional migration, secrets, and bug reporting. It returns context
  only: the Agentworks assistant agent interprets the operator's current request and decides which
  topic, proposal, or inert action to use next. The canonical prompt and published native skills
  enter through this top-level context rather than hard-coding first-run onboarding as every
  request's destination. An Agentworks assistant agent rendering mode (an `--agent` flag with a
  TTY-informed default; the exact mechanism is the HLA's call) MAY adjust emphasis, never substance:
  in agent mode the rendering foregrounds the behavioral contract (ask for consent before any tool
  call that examines the operator's machine; test only for the presence of sensitive material such
  as SSH keys and secrets, never view values; restate R12's access disclosure). Both renderings
  derive from one source; there are never two contents. Prior art for the effort's
  `prior-art-research.md`: PowerShell's module-contributed `about_*` topics, `kubectl explain`'s
  live schema walks, `git help` concept guides, `go help` topics, `rustc --explain`, and Terraform's
  per-provider schema-plus-prose docs generation.
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
  belong to consented assistance actions, not to displaying a page). The projection is expected to
  be the resource graph itself running in a gated access mode, not a second structure kept in
  lockstep: powers (secret readers, run targets, capability API objects) sit behind callables a mode
  can gate, while universal facts are plain data on the nodes. In a gated mode, data is only what is
  already materialized (finalize-computed verdicts, stored rows, declared relationships); nothing
  lazily computes through a power while wearing attribute syntax, which is also what keeps rendering
  side-effect-free. Whether a mode gates by permission check or by leaving the power unwired is the
  HLA's call; both are legitimate mechanisms, done properly. Templates fill in the dynamic bits
  only: the authored text still carries the teaching, and the effort should not over-index on
  pushing everything into the graph. Contributed guide content MUST be data, never code, and
  rendering MUST NOT execute anything a contribution supplies. This holds for curated system plugins
  now precisely so the content channel is already safe when external plugins arrive (wave 8).
- **R16 (README assistance block).** The repository README's getting-started section MUST lead with
  a single copy-paste block (a fenced block, so GitHub renders a copy button) addressed to the
  Agentworks assistant agent, along these lines: "I'd like your help using Agentworks. You are my
  Agentworks assistant agent, not an agent managed by Agentworks. Please install or update it if
  needed, help me understand what it can do, and help me set it up or operate it. It is available on
  PyPI as `agentworks-cli` and runs on Python >= 3.12. Before installing it, please offer to review
  the exact source tag and warn me that a full repository review may incur substantial model usage.
  Please obtain my consent before acting, then run `agw guide --agent` for current context and
  decide what to propose next based on my request." This is a first-class zero-plugin assistance
  path; the harness plugins (R1) say essentially the same thing and remain primarily an advertising
  and discoverability channel. The prompt MUST avoid product-specific harness assumptions beyond the
  ability to accept the prompt, drive the CLI, and request or use appropriate operator-approved
  workstation access.

## Personas and stories

- As an operator using any agent that meets the Agentworks assistant agent capability definition, I
  paste the canonical website prompt and receive the same disclosure, CLI-driven context, and
  consent boundaries without installing a native Agentworks plugin.
- As a new operator with Claude Code or Codex on my workstation, I add the Agentworks marketplace
  and plugin from GitHub, ask my Agentworks assistant agent to set Agentworks up, and reach my first
  working session in minutes, told up front what access I am granting and asked before anything on
  my machine is examined.
- As an operator six months in, I ask the same Agentworks assistant agent to add a VM site, create a
  VM or session, rotate a secret, or troubleshoot a failure. The assistance entry supplies current
  guide context, and the Agentworks assistant agent chooses what to propose rather than guessing
  from stale knowledge.
- As a returning operator, I ask "what is new in Agentworks and what have I not adopted?" and get a
  current capability and adoption assessment without redoing what is already configured, plus the
  canonical release-history source when I mean changes between versions.
- As an operator who scripts environments, I run the non-interactive path in provisioning and get
  the same result the guided path produces.
- As an Agentworks assistant agent operating the CLI on an operator's behalf, I run `agw guide` to
  learn the current surface (never a stale tutorial) and read machine-readable list, describe, and
  doctor output for facts.
- As a human operator, I run `agw guide concept-secrets` in a terminal and get the same teaching
  content my Agentworks assistant agent gets.
- As the wave 2 effort lead, my emitted schemas and samples get consumed by this effort's surfaces
  instead of a parallel hand-maintained copy.

## Non-goals

- Delivering assistant-agent skills through the agentic-artifacts layer (wave 6). The Agentworks
  assistant agent sits outside Agentworks by design.
- Assistance driven by an Agentworks-managed agent. An Agentworks-managed agent must not modify the
  system it runs in.
- Native packages for every possible assistant product. Claude Code and Codex are the packaged
  integrations; the universal copy/paste prompt is the portability contract for other capable
  Agentworks assistant agents.
- Building schema emission, live samples, or describe surfaces themselves; those are wave 2's, and
  their CLI naming is wave 2's call (coordinated with this effort).
- Editor integration for manifest authoring. It follows wave 2's emission as schema-derived depth
  per the roadmap's phasing; pulling it into this effort later is a plan change for the effort lead
  to record, not baseline scope.
- A documentation-site overhaul. This effort's docs work is bounded to assistance and discovery
  surfaces.

## Acceptance criteria

1. A fresh operator with any Agentworks assistant agent meeting the capability definition and no
   prior Agentworks knowledge reaches a working session using only the canonical copy/paste prompt
   and the guide command, with every probe consented. The native Claude Code and Codex packages each
   pass the same path.
2. Asking for an adoption assessment on an unchanged, fully-adopted system is a clean no-op; asking
   after an upgrade reports all currently not-yet-adopted capabilities without redoing completed
   work. `concept-release-notes` renders the installed release's packaged canonical notes offline. A
   broader temporal range uses the consented canonical GitHub fallback and is not inferred from
   current-state facts.
3. The non-interactive path reproduces the guided path's result on a clean machine.
4. List, describe, and doctor surfaces offer documented machine-readable output consumed by the
   plugin's skills themselves (dogfooding the contract).
5. Discovery answers are generated from the live registry inventory; adding an implementation
   changes the answers without touching assistance-package content.
6. Completions and docs are current for every surface this effort adds (repo rule).
7. Both harness assistance packages exist, equivalent per R11, and both pass criterion 1's
   fresh-operator test.
8. The security disclosure (R12) appears before any assistance action in both the guided and
   non-interactive paths, and each later boundary retains its applicable operator decision.
9. Verification during assistance is driven by the Agentworks assistant agent and consented per the
   guide's instructions; `agw`'s verification surfaces read no secret values, and a declined probe
   leaves an explicit manual-verification note.
10. The README's getting-started section leads with the R16 copy-paste block, and following it on a
    clean machine with a capable prompt-driven assistant reaches the top-level `agw guide --agent`
    context successfully. The Agentworks assistant agent, not the guide command, decides what to
    propose next.
11. `agw guide` with no topic lists every available topic; a kind topic reflects the live instance
    list; disabling an implementation visibly changes its topic's rendering.
12. Guide topics complete in the shell, including `concept-` prefix discovery, and the completion
    tree includes dynamic topic elements per the repo's completions mechanism.
13. A plugin's contributed guide content renders with zero contributed code executed (R15),
    demonstrated by a test that rejects a contribution attempting expression evaluation.
14. With the canonical prompt available or a native skill installed, an Agentworks assistant agent
    can use the top-level guide's intent-to-topic map and live index for setup, adoption assessment,
    release history, configuration, troubleshooting, and VM or session operation requests. The
    Agentworks assistant agent decides what to propose next. First-run assistance creates and
    verifies a usable VM and a started session. Every mutating, remote, privileged, or destructive
    action retains an explicit operator decision.
15. Before installing or updating the CLI, the assistance package offers an exact-tag source review,
    warns about full-repository model usage, and preserves separate decisions for focused review,
    full review, no review, and installation. Declining source review does not claim the source was
    reviewed and does not itself block a separately approved install.

## Decisions

- **D1 (plan A).** The vanilla-harness plugin model is settled (operator ruling recorded in the
  roadmap's user perspective and target-state) for the two native integrations; this FRD does not
  reopen it. R16's universal copy/paste prompt keeps native plugin support optional for any other
  capable Agentworks assistant agent.
- **D2 (parallel to wave 2).** This effort seeds now, consumes wave 2's surfaces as they land, and
  must not block on them nor duplicate them. D7 narrowly supersedes this historical sequencing rule
  for PR #428's Phase 1 release boundary: the migration-remediation topic waits for authoritative
  wave 2 services on `main` because the same release deletes the migrator it replaces.
- **D3 (both harnesses, no ordering).** Claude Code and Codex plugins are both required deliverables
  with equivalent content (R1, R11, AC7). Build order is the effort lead's call; neither is an
  afterthought.
- **D4 (issue #390 disposition).** The "examples system plugin" ask is subsumed rather than built:
  wave 2's live-rendered samples (adopted here per R6) provide example content that cannot drift,
  and this effort's discovery surfaces present it. No separate examples plugin ships; #390 closes
  against that combination.
- **D6 (assistance posture; operator rulings, 2026-08-06 and 2026-08-10).** The README copy-paste
  block is an equal first-class entry beside the plugins, which are kept primarily for advertising
  and discoverability. The package is always-available Agentworks assistance, not a one-time
  onboarding artifact. Agentworks never probes the operator's machine; the Agentworks assistant
  agent probes with consent and `agw` verifies. The assistance package offers an optional,
  usage-disclosed review of the exact source tag before installing or updating the CLI.
- **D7 (migration remediation; operator ruling, 2026-08-07).** Automated resource migrators do not
  ship. Precise load errors name the offending input, and `agw guide concept-migration` teaches the
  exceptional operator-led rewrite using live sample, field-reference, and verification surfaces.
  Migration is deliberately narrower than upgrading: ordinary upgrades should remain routine and do
  not imply a guide workflow.
- **D5 (the guide command; operator rulings, 2026-08-05).** The teaching command is `agw guide`,
  deliberately not `skill` (wave 6 needs "skill" as an artifact noun). Output is markdown only. Meta
  topics carry the `concept-` prefix. Teaching content lives in the CLI with thin assistance
  packages, and contributed content is data rendered through locked-down templating, never code. The
  top-level rendering for the Agentworks assistant agent is the stable assistance entry and presents
  the intent-to-topic map; the Agentworks assistant agent decides what to propose next.
  `concept-onboarding` remains the specialized first-run and adoption-assessment path, while
  `concept-release-notes` renders the installed release's release-please-authored notes and owns the
  consented canonical fallback for wider temporal history.

## Open questions

- How `concept-onboarding` determines done versus not-yet-done from live state, and whether any
  step-state must persist beyond what the system already records (HLA's call).
- The agent-mode mechanism: flag spelling, the TTY-informed default, and how emphasis differs
  between renderings without forking content (HLA's call).
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
- Assistance-package release engineering: how the thin packages declare the CLI versions they can
  install or update (small, since teaching content ships in the CLI), and the R11 sharing mechanism.
- What feedback loop tells us onboarding is working for new operators (recorded from the user
  perspective; may resolve to a simple ask-the-operator step rather than telemetry).

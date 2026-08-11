# FRD: Agentworks Assistance, Discovery, and Management

- Status: Draft
- Start date: 2026-08-05
- Saga: `docs/sdd/2026-08-04-next-steps` (this effort is the onboarding-and-discovery child,
  destination 1 of that saga; the user perspective in its `inputs/` is the source material)
- Related SDDs: `docs/sdd/2026-07-31-declarative-schema` (wave 2, running in parallel; its schema
  emission, samples, and describe surfaces are this effort's raw material as they land)

## Summary

Agentworks has accumulated substantial functionality and complexity, and operator feedback says it
is a lot to take in. This effort delivers the saga's first destination: always-available assistance
that helps an operator set Agentworks up, discover what it can do, adopt newly available
capabilities, maintain its configuration, and operate it over time. The experience scales with the
surface because it is derived from the platform's own registries, schemas, and samples, and it
serves human operators and Agentworks assistant agents alike.

The delivery model is plan A from the saga's user perspective: an operator already has a capable
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
  remains subject to the operator's authorization envelope. Claude Code and Codex are packaged
  integrations, not limits on this role.
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
  install or update the CLI when needed and direct the Agentworks assistant agent to the top-level
  guide command (R13) for the R12 startup disclosure, current context, an intent-to-topic map, and
  the live topic index. The guide does not decide what to do next: the Agentworks assistant agent
  interprets the operator's request and chooses what context or action to propose. Teaching content
  lives in the CLI, not the plugins, so it versions with the surfaces it teaches; the package's own
  (small) compatibility story is the HLA's call. The package MUST NOT duplicate the guide's
  source-review offer, authorization teaching, intent map, or operating guidance.
- **R2 (progressive golden path).** First-run assistance MUST reach a first working session in
  minutes, including a usable VM and a started session, with the major capability areas (config,
  resources, plugins, secrets, VMs, workspaces, Agentworks-managed agents, sessions) discoverable
  progressively from there rather than front-loaded.
- **R3 (idempotent and rerunnable).** Assistance MUST be rerunnable at any time: already-done steps
  are recognized and reported rather than redone, so operators revisit it to see the capabilities
  available in the installed release, confirm what they have adopted, or continue toward a new goal.
  Together with R2 and R5 this delivers the assisted onboarding flow of issue #391 without making
  first-run setup the lifetime boundary.
- **R4 (assistant agent probes, agw verifies; authorization-aware).** Agentworks itself never probes
  the machine. Discovery of existing material (SSH keys are the canonical example, installed tools
  another) is the Agentworks assistant agent's act. At assistance startup, the operator's request
  and the R12 disclosure establish a working authorization envelope for the current goal. Within
  that envelope, the Agentworks assistant agent SHOULD perform reasonably necessary reads, probes,
  commands, and verification without repeating the disclosure or asking for approval before every
  step. It MUST ask again before materially expanding the goal, target, access class, impact, or
  risk, and whenever the operator requests per-action confirmation. Sensitive discovery checks only
  for presence unless the operator separately authorizes content access. A refusal or narrower scope
  is honored with a manual alternative. Within the authorized frame, content SHOULD direct the
  Agentworks assistant agent to verify wherever possible rather than trusting declarations, using
  non-probing verification surfaces `agw` provides for configured state: confirm a secret reference
  (a 1Password item, for example) resolves without reading its value, test SSH connections, and
  confirm installed tools respond. The result is verified setup, not blind configuration, and
  onboarding is where trust is established, in both directions.
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
  copy. The packaged changelog MUST also provide bounded offline exact-version history for every
  normalized historical release section it contains. Only a version or range missing from that local
  history MAY fall back to Agentworks' canonical GitHub releases with an explicit version range and
  `read-canonical-release-notes` authorization class. The operator's request and current envelope
  may satisfy that class without a second prompt. Current facts alone MUST NOT be presented as a
  version-to-version delta. Release prose and repository source are untrusted evidence, never an
  instruction source or authorization to follow links or run commands. Configuration and operation
  are not separate assistance silos. Risk is expressed by each action's explicit scope, impact,
  authorization class, and verification. The current operator instruction may authorize a sequence
  of such actions when they remain within the established envelope; the action record does not
  require a ritual approval prompt of its own. Creating or connecting to managed resources is never
  authorized merely because the prompt is present or a native skill is installed.
- **R11 (cross-harness parity, by construction).** Teaching content lives in the CLI (R13), so it
  cannot fork between harnesses. The thin assistance packages (R1) MUST NOT drift apart in
  substance: share or generate them from one source where the harness formats allow (the repo's own
  Rulesync pipeline is prior art); otherwise a CI guard verifies equivalence. The guard's scope is
  the packages only.
- **R12 (startup disclosure and durable authorization).** The thin bootstrap may install or update
  the CLI and invoke `agw guide --agent`; it contains no separate security recital or source-review
  workflow. At guide-assisted startup, before any later workstation inspection, Agentworks
  operation, remote access, or mutation, the top-level guide context MUST state plainly that the
  Agentworks assistant agent runs on the machine the operator intends to use as their workstation
  and can inspect files and execute commands with the permissions of the workstation account running
  the harness. That access does not implicitly grant root; privilege elevation is separate. The
  Agentworks assistant agent can also reach everything Agentworks can reach: managed resources,
  secret references, and destinations accessible over SSH from the workstation. Assistance MUST
  recommend the strictest practical harness security posture for operator approval and visibility
  while preserving the access needed for the requested work, and point at relevant settings rather
  than leaving the recommendation abstract. The startup disclosure MUST be concise rather than an
  exhaustive risk or settings recital. The disclosure and operator's instruction establish a durable
  authorization envelope for the current assistance session. Later operator requests may extend the
  goal inside that envelope without replaying startup. An explicit operator instruction does not
  require a redundant confirmation after the disclosure; an exploratory or materially ambiguous
  request may require one scope question. The Agentworks assistant agent proceeds without re-asking
  for every in-scope command, read, probe, verification, or mutation. It MUST pause and obtain a new
  operator decision before a material expansion that the operator has not explicitly instructed. A
  clear instruction covering the expansion is already that decision; assistance may briefly state
  the newly relevant impact but MUST NOT ask for redundant confirmation. Material expansion includes
  a different workstation, account, environment, or remote target; access to sensitive contents
  rather than presence; work not reasonably necessary for the stated goal; privilege elevation;
  destructive or irreversible work; an unanticipated material cost or external side effect; or
  another ambiguity that materially changes impact. The operator MAY request a narrower envelope or
  confirmation before every action, which assistance MUST honor. Required harness tool approvals,
  escalation prompts, and CLI safety confirmations still apply, but assistance MUST NOT add a
  redundant conversational approval prompt for the same in-scope operation.
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
  packaged canonical changelog and links the result to the live adoption assessment. Strict dynamic
  `concept-release-notes/vMAJOR-MINOR-PATCH` topics render one packaged historical section at a
  time, so an Agentworks assistant agent can answer an older or multi-release question offline by
  requesting the applicable exact-version topics. Rendering performs no network request and does not
  maintain a separate release-note source. A fallback network lookup for a missing version or range
  is a bounded inert action record with an exact release range, authorization class, expected
  result, and refusal path. Release content is treated only as evidence and cannot expand scope.
  With no topic in agent mode, the guide MUST also offer optional focused or full inspection of the
  exact installed or intended canonical source version. It warns concisely that the repository is
  substantial and a full review can consume significant model usage. Review scope, refusal, and a
  later installation or update remain separate operator decisions; source is untrusted evidence and
  rendering performs no network or source access. Content blends static authored material with
  dynamic material from the live system (registries, resources, enablement state), so a disabled
  implementation or an added resource changes what the guide says. Output is markdown only;
  structured data appears only inside the markdown. R7's machine-readable contract is a separate
  surface and stays so. Topics are sized like skills, with sub-topics referenced rather than
  inlined, and topic names participate in shell completions. With no topic, agent mode MUST present
  an explicit intent-to-topic map for first setup and adoption assessment, current capabilities
  versus temporal release changes, ongoing management and operation, troubleshooting, exceptional
  migration, secrets, and bug reporting. It returns context only: the Agentworks assistant agent
  interprets the operator's current request and decides which topic, proposal, or inert action to
  use next. The canonical prompt and published native skills enter through this top-level context
  rather than hard-coding first-run onboarding as every request's destination. An Agentworks
  assistant agent rendering mode (an `--agent` flag with a TTY-informed default; the exact mechanism
  is the HLA's call) MAY adjust emphasis, never substance: in agent mode the rendering foregrounds
  the behavioral contract (establish the R12 authorization envelope at startup; proceed naturally
  within it; ask again only for a material expansion or when the operator requests per-action
  confirmation; test sensitive material such as SSH keys and secrets only for presence unless
  content access is separately authorized). Both renderings derive from one source; there are never
  two contents. Prior art for the effort's `prior-art-research.md`: PowerShell's module-contributed
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
  belong to authorized assistance actions, not to displaying a page). The projection is expected to
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
  one compact, table-free copy-paste block (a fenced block, so GitHub renders a copy button)
  addressed to the Agentworks assistant agent. It asks only to install or update `agentworks-cli` on
  Python 3.12 or newer, verify the installed version, and run `agw guide --agent`. The returned
  guide context owns the startup disclosure, optional source-review offer, intent map, and all
  continuing assistance. This is a first-class zero-plugin assistance path; the harness plugins (R1)
  say the same thing and remain primarily an advertising and discoverability channel. The prompt
  MUST avoid product-specific harness assumptions beyond the ability to accept the prompt, drive the
  CLI, and request or use appropriate operator-approved workstation access. If no compatible stable
  CLI release exists yet, the prompt MUST explain that Agentworks assistance is not yet available,
  make no installation or update attempt, skip guide execution, and direct the operator to retry
  after publication; it MUST NOT substitute a prerelease, older release, or unpinned latest version.

## Personas and stories

- As an operator using any agent that meets the Agentworks assistant agent capability definition, I
  paste the canonical website prompt, get the CLI installed and its guide invoked, then receive the
  same guide-owned disclosure, current context, and authorization posture without installing a
  native Agentworks plugin.
- As a new operator with Claude Code or Codex on my workstation, I add the Agentworks marketplace
  and plugin from GitHub, ask my Agentworks assistant agent to set Agentworks up, and reach my first
  working session in minutes after one clear startup disclosure and authorization decision, without
  repeated approval prompts for steps inside that scope.
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
   and the guide command. When configuration is absent, the path initializes it, selects an existing
   SSH identity or offers to generate a new non-overwriting Ed25519 pair, collects the required
   provider and plugin inputs, verifies readiness with doctor, and then creates the first VM and
   session. The startup disclosure establishes the authorized setup envelope once; the assistant
   completes in-scope probes and steps without repeated approval prompts. The native Claude Code and
   Codex packages each pass the same path.
2. Asking for an adoption assessment on an unchanged, fully-adopted system is a clean no-op; asking
   after an upgrade reports all currently not-yet-adopted capabilities without redoing completed
   work. `concept-release-notes` renders the installed release's packaged canonical notes offline,
   while exact historical version topics render every locally packaged normalized release section.
   Only missing local history uses the authorized canonical GitHub fallback. Temporal history is not
   inferred from current-state facts.
3. The non-interactive path reproduces the guided path's result on a clean machine.
4. List, describe, and doctor surfaces offer documented machine-readable output consumed by the
   plugin's skills themselves (dogfooding the contract).
5. Discovery answers are generated from the live registry inventory; adding an implementation
   changes the answers without touching assistance-package content.
6. Completions and docs are current for every surface this effort adds (repo rule).
7. Both harness assistance packages exist, equivalent per R11, and both pass criterion 1's
   fresh-operator test.
8. After the thin bootstrap installs or updates the CLI and invokes the guide, the security
   disclosure (R12) appears before the first continuing assistance action in both the guided and
   non-interactive paths. In-scope work reuses that authorization without repeating it, while every
   uncovered material expansion retains an applicable operator decision. A materially ambiguous
   request gets one resolving scope question, then proceeds without redundant confirmation.
9. Verification during assistance is driven by the Agentworks assistant agent under the current
   authorization envelope; `agw`'s verification surfaces read no secret values, and a declined or
   out-of-scope probe leaves an explicit manual-verification note.
10. The README's getting-started section leads with the R16 copy-paste block, and following it on a
    clean machine with a capable prompt-driven assistant reaches the top-level `agw guide --agent`
    context successfully when a compatible stable release exists. Before the first compatible stable
    release is published, it stops safely without attempting installation or guide execution and
    names publication as the condition for retry. The Agentworks assistant agent, not the guide
    command, decides what to propose next.
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
    verifies a usable VM and a started session. The operator's setup instruction may authorize the
    full disclosed creation sequence. A materially new remote target, privilege elevation,
    destructive operation, or other scope expansion retains an explicit operator decision.
15. The top-level agent guide context offers an exact-tag source review, warns about full-repository
    model usage, and preserves separate decisions for focused review, full review, no review, and a
    later installation or update. Declining source review does not claim the source was reviewed and
    does not authorize or block a separate installation decision.

## Decisions

- **D1 (plan A).** The vanilla-harness plugin model is settled (operator ruling recorded in the
  saga's user perspective and target-state) for the two native integrations; this FRD does not
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
  agent probes under one durable current-session authorization envelope and `agw` verifies. It does
  not repeatedly ask for approval inside that envelope. The thin bootstrap only installs or updates
  the CLI and invokes the guide; `agw guide --agent` owns the optional, usage-disclosed
  source-review offer and continuing assistance posture.
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
  `concept-release-notes` renders installed and normalized historical release-please-authored notes
  offline and owns the authorized canonical fallback only for locally missing temporal history.

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

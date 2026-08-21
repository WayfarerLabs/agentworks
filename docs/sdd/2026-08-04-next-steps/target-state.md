# Target State

- Status: North star, accumulating settled rulings
- Last updated: 2026-08-21

This document describes where Agentworks is going across this saga effort, synthesized from the
perspectives in `inputs/`. It is the target of these waves, not a forever vision: when
`current-state.md` agrees with this document and every child SDD is locked, the saga is done. Every
phasing choice is tested against these destinations, and individual efforts must not paint over
them. Settled operator rulings are recorded here with dates; a child SDD builds on these rather than
reopening them.

Vocabulary (operator ruling, 2026-08-08): this construct is a **saga**, replacing the earlier
"roadmap" term ("program" was retired 2026-08-05). A saga is a grouping of related efforts that
overlap enough to require coordination; the distributed-systems borrowing is deliberate, since
children commit independently and completely, intermediate states stay visible on `main`, a lead
orchestrates rather than locks, and the whole runs to its lock or is deliberately unwound.
Historical text in immutable records (checked ledger boxes, locked SDDs, the frozen starting-state)
keeps the old words.

## The seven destinations

Destination 1 is the priority; the rest are not strictly ordered.

1. **An operator experience that scales with the surface.** Onboarding, capability discovery, and
   schema discovery are derived from the same registries, schemas, and samples the framework makes
   authoritative, so they cannot go stale, and every surface serves humans and agents alike
   (discoverable CLI, machine-readable output, shipped skills). The user perspective's
   skills-plus-CLI pattern is one investment serving all of these.
2. **One declarative resource model.** Registration-time Pydantic models are the single authority
   for validation, reference extraction, schema emission, samples, and describe surfaces. One decode
   frontend (YAML manifests), no lockstep twins.
3. **A capability framework that scales by kind.** A core-owned capability-kind descriptor replaces
   the per-kind switchboard, so adding a kind is a registration, not a coordinated edit across
   adapter tables, graph stamps, publishers, and snapshot logic.
4. **Harness integration as one identity with per-scope contributions.** VM, admin, agent,
   workspace, and session contributions are explicitly selected at their owning level, applied by
   their owning lifecycle, and never smuggled through session operations.
5. **The session event stream as a platform.** Every integration fuses its best available sources
   into one Agentworks-owned, best-effort event vocabulary. Transcripts, live frontends, ACP,
   structured control, audit sinks, the distiller, and the VM auto-suspend idle signal are all
   downstream consumers of that one representation.
6. **The memory-learning loop.** Learnings flow out of sessions over the event stream, a high-trust
   distiller curates them across all sessions and agents, and the agentic-artifacts layer (rules,
   skills, hooks) is the reviewed write-back path into future sessions. This was present as
   "distillation" in the harvested harness-transcripts FRD
   (`inputs/harness-transcripts-harvest.md`), was dropped in the observability reframe, and is
   restored as a first-class consumer. It is why the event vocabulary must stay analysis-friendly
   and Agentworks-owned.
7. **A stable plugin boundary, last.** External plugin promises come only after the internal
   contracts (descriptor, schema, scope participation) have been proven by first-party use.

## Settled contracts and rulings by area

### Operator experience (destination 1)

Plan A for onboarding: the operator's existing vanilla workstation harness drives setup, consuming
harness-specific plugins or marketplace entries published from the Agentworks repo. The onboarding
agent deliberately sits outside Agentworks (a managed agent must not modify the system it runs in),
so the agentic-artifacts layer is not the delivery path for onboarding skills. Onboarding is
idempotent and rerunnable, and conspicuously consent-first about examining the operator's machine:
Agentworks itself never probes, the agent does (instructed by guide content), and `agw` supplies
non-probing verification of configured state. The repository README leads with a single copy-paste
bootstrap block addressed to the agent (install from PyPI, run `agw guide`), a first-class
zero-plugin path beside the plugins, which are kept primarily for advertising and discoverability
(operator rulings, 2026-08-06). Discovery and schema help are derived from registries, schema
emission, live samples, and describe surfaces so they cannot drift.

The teaching surface (operator rulings, 2026-08-05): `agw guide [topic ...]` serves skill-shaped
markdown for agents and humans alike, blending static authored content with dynamic content from the
live system. Topics span resource kinds (with live instance lists), specific resources, capability
implementations, and `concept-` prefixed meta topics (collision-free and discoverable by
completion). Output is markdown only. Every kind, implementation, and plugin contributes its own
topics through one generic contract, with built-in content living beside the kind it documents, and
contributed content is data rendered through declarative content blocks (a closed, core-owned block
vocabulary, not a template engine), never code, against a pared-down, read-only projection of the
resource graph anchored by `me` shorthand (the resource the topic documents), with rendering
side-effect-free. The published harness plugins reduce to thin bootstraps (install, disclose, run
`agw guide`), which makes cross-harness parity structural. The command is named `guide`, not
`skill`, reserving the skill noun for the artifacts layer (destination 6's rules, skills, hooks).
Reference surfaces (describe, schema, samples) and the teaching surface render the same underlying
sources: wave 2 owns the sources and the reference surfaces, the onboarding child owns the guide. A
live example of the artifact need (operator observation, 2026-08-05): this workspace authenticates
GitHub through a custom git credential helper serving fine-grained PATs by full HTTP path,
environment knowledge an agent currently must be told in conversation; a feature provisioning such a
helper should emit exactly that fact as a skill.

**Ruling (operator, 2026-08-12):** `agw guide` with no topic becomes a **trail sign** that points at
the topics rather than teaching, and the onboarding walkthrough it currently carries (the
source-review offer and the rest) moves into a dedicated onboarding topic. The same round takes
whatever other simplifications the onboarding and discovery track has accumulated; the operator's
own reading is that earlier phases of that effort landed tech debt while unattended. Shipped
2026-08-15 (PRs #519 and #537).

**Ruling (operator, 2026-08-17, the derived index and grammar-native guide; gates 0.14.0):** the
guide's no-topic response derives from the shell catalog instead of a hand-maintained tuple, and the
guide's final public grammar is `agw guide` (the index), `agw guide list`, and single-topic
`agw guide show TOPIC`, with no direct or variadic compatibility alias. Direction arrived in the
onboarding child session (its FRD, HLA, and LLD record it at PR #593 head `1d31beab`), with the gate
and scope confirmed through the saga lead's authenticated channel. A reserved `_index.md` shell
renders first, followed by ordered ordinary concept metadata (an optional bounded `index-order`
frontmatter features concepts; generated exact release-note topics stay listable but outside the
omitted-concept count), all still static and package-only with no config, runtime state, registry,
or release-history loading. This supersedes the 2026-08-12 trail-sign ruling's shipped
fixed-destination response (the catalog-free rendering of PRs #519/#537, later the corrected model's
shared destination tuple) and, in the onboarding effort's own record, the shells FRD's acceptance
that the no-topic path loads no shell catalog; the trail-sign intent (point, do not teach) survives
as `_index.md`'s content, now owned like any other shell. **This work gates 0.14.0**; the release
does not ship the retired guide grammar.

**Ruling (operator, 2026-08-17, the simplification pass no longer gates 0.14.0):** superseding the
2026-08-16 gate ruling below, the pass's remaining work (the sweep's execution, the gcp dedup, the
reassessment, and the lock) continues on its own merits but does not gate the release. The
distinction the operator drew: the remaining sweep estate is repository-internal test quality,
invisible in the shipped artifact, while the release's real gates are artifact-facing (the guide
grammar above; the changelog repair in issue #589). This also resolves `phasing.md`'s
test-consolidation soft gate, which the superseded ruling had subsumed: the operator's call is cut.
Separately, the operator holds a personal edit pass over guide content and website wording before
the cut; it rides the operator's own hands and needs no gate, since the operator cuts the release.

**Ruling (operator, 2026-08-17, markdown concept shells):** the guide's retained typed model is
replaced by auto-discovered Markdown concept shells, confirmed through the saga lead's authenticated
channel after the onboarding effort's design checkpoint (PR #580). One Markdown file per ordinary
`concept-` topic with required description frontmatter defines the catalog; the only additions to
ordinary Markdown are balanced agent-only fences and one bounded inert import of a uniquely named
section from the installed first-party package tree (whose named consumer is the packaged canonical
root README, carried through one Hatch build-hook lifecycle with repository-relative links and
images mapped to fixed GitHub URLs); there are no variables, loops, conditionals, live projections,
or template engine. Shells are static documentation: a subsequent same-day operator direction in the
child session removed the initially sanctioned `resource-kinds` and `resource-list` projections and
their entire state-loading and degradation subsystem, so anything live is command-owned and the
guide points at it. Typed guide blocks, actions, consent and evidence replay, the onboarding
assessment, and manual contribution registration are removed. Three older sentences are superseded
with it: the 2026-08-15 survey disposition's projector-parity clause served an assessment this
ruling removes and is discharged (the projector shipped with PR #556 and dies with the shells
cutover); the plugin-topic-namespacing sentence under the capability rulings no longer describes a
shape wave 8 inherits, since plugins do not contribute shells in this format version and a plugin
contribution API is out of scope; and the survey disposition's raw-aggregation principle now stands
without exception, the guide carrying no live composition at all. The consent posture survives the
machinery: guide text instructs and never authorizes (the effort FRD's R5 restates it over the new
surface), structural shell defects stay nonzero as repository defects, and with no live loading the
environmental-degradation surface is gone rather than failing soft. This supersedes the typed-block
and action-record retention in the 2026-08-05 teaching-surface rulings and the 2026-08-15 rubric's
"action records" keep clause; the corrected-guide-model ruling below otherwise stands (one catalog,
presentation-only modes, the few-line bootstrap). The 2026-08-16 corrected guide model shipped first
(PR #579, merged 2026-08-17), satisfying its 0.14.0 gate; no gate is minted here for the shells
implementation without separate operator direction.

**Ruling (operator, 2026-08-16, the corrected guide model; gates 0.14.0):** hands-on testing in the
operator's real environment found the shipped guide badly over-indexed on agents: the human trail
sign exposes two destinations while the agent trail sign exposes seven that are plainly useful to
humans, eight core topics carry separate agent-contract files, and the "simple" copy/paste bootstrap
prompt is 23 lines of defensive specification. The corrected model, proposed by the onboarding lead
and adopted by the operator: one topic catalog for everyone; human and agent modes show essentially
the same destinations and differ in presentation, never in access to ordinary information; one
agent-specific assistance topic explains how an external assistant behaves; individual topics carry
a short agent note only when genuinely necessary; and the bootstrap prompt shrinks to a few lines
that install the CLI and point at `agw guide --agent`, which is itself the specification. This
supersedes the 2026-08-12 trail-sign ruling's destination asymmetry (the trail-sign form itself
stands). The operator's words carry the bar: "I'd be embarrassed to ship this. It has to be included
in 0.14. This should all end up dramatically simpler." The onboarding child owns the work, and
0.14.0 does not ship without it.

The operator supplied a worked example of the agent-hint species and directed it recorded, since the
miss was large enough that everyone should know the intended shape (the wording may drift; the shape
may not). For VM platforms, the onboarding journey's agent hint should run along the lines of:
"Check out the available platforms and offer to guide the operator through a discovery and
configuration process, first determining which platform(s) they want to use, and then walking
through the configuration options to create actual sites." The operator expects roughly a dozen
journey hints of this species across the guide, developed by the onboarding effort, not just this
one. The saga lead's placement note, advisory: a hint of this shape describes a journey across kinds
and so belongs to the onboarding topic's agent note rather than being sprinkled per kind, which is
how the "only when genuinely necessary" clause and a dozen journeys coexist.

**Superseded (operator disposition, 2026-08-20, delivered through the onboarding session's
authenticated channel and published by that effort's lock, PR #615):** the roughly-dozen-hints
expectation is retired. The current sparse agent-only content is intentional: general posture stays
centralized in `concept-assistant-agent`, and other topics add agent-only context only where a
journey genuinely needs it. The worked example above stands as the species description for any hint
that is written, not as a quota. The onboarding effort locked against this disposition rather than
against the earlier expectation; a lead reading the paragraph above must read this one with it.

**Ruling (operator, 2026-08-16, the simplification pass gates 0.14.0):** the pass's remaining
deletion work, the assertion sweep's execution and the gcp fixture dedup behind it, plus the pass's
reassessment and lock, complete before 0.14.0 ships. The operator's rationale: "I don't want to
start really sharing this repo with a bunch of obviously shit code." This hardens and subsumes
`phasing.md`'s test-consolidation soft gate, whose carrying vehicle the sweep already is. Follow-on
efforts the reassessment proposes do not inherit the gate; the pass closing does.

**Ruling (operator, 2026-08-15, the guide-value rubric):** guide content earns its place only by
conveying concepts or higher-level synthesis not available from a single CLI command. A guide block
whose content is one command's output wearing guide clothes is removed, and the guide points at the
command instead, signpost-style all the way down. Kept by the rubric: `concept-` topics, the
onboarding sequence and adoption assessment, the agent contract, and action records with their
consent boundaries and refusal alternatives. The rubric executes survey-first: a read-only inventory
classifying every guide block with machinery consequences priced and reviewed before anything is
removed. If the survey finds the cuttable surface small, keeping it is an acceptable answer.

**Survey disposition (operator, 2026-08-15):** a raw aggregation of several command outputs is not
higher-level synthesis. The reviewed survey found 59 fixed blocks to keep and 141 to remove, plus a
dormant kind fallback and every runtime resource topic. Remove the full cut set in one effort after
corrected PR #548 rather than coordinating current-owner and future-owner phases. The onboarding
effort owns the combined content and directly orphaned machinery deletion as a one-time exception to
the ordinary content-versus-machinery boundary. A smaller projector with behavioral parity must
serve the retained onboarding assessment before `_dynamic_topic` or generic `GuideView` leaves.
Temporary loss of guide copies is accepted on unreleased `main`: `resource describe-kind` and
`resource describe` provide partial interim ownership, while the CLI grammar child lands the settled
`resource explain` and `graph show` destinations. This does not relax the release gate; 0.14.0 MUST
NOT ship until the CLI grammar rewrite restores those command surfaces.

**Ruling (operator, 2026-08-15):** `resource describe` is removed pre-0.14. Its relational facts
move to the grammar child's `graph` namespace; no generic replacement card is created. This retires
the A-or-B previously recorded in the ledger. The one shipped spelling requires the operator's
deprecation-runway waiver, pending on PR #491.

### Declarative model (destination 2)

Adopted from the declarative-schema effort as fixed input: the model-as-authority contract, the
two-walker split (a total, never-raising reference extractor plus a field-documentation walk), the
tagged-union capability config discriminated on `name` and assembled per kind at the
post-registration boundary, the error-framing bridge, and validation on effective (merged) config
keyed to the finalize fold. Phase 2 executes through the descriptor, not ahead of it.

Four doors stay open for per-instance configuration and the future living graph: source-agnostic
reference extraction, a general layer-stack merge rather than a template-only chain, graph
post-finalize immutability staying a registry/fold property rather than a model-layer assumption,
and one instance-state store designed once for instance specs, integration applied-state, and
artifact ownership records (three perspectives converge on that store).

**The variant-modeling contract** (operator rulings, 2026-08-07 and 2026-08-08) has three tiers.
First, config variants are explicit shapes: a genuine mechanism choice carries a discriminated union
on a string `Literal` discriminator (spelled `mode` on today's action-named fields; the README's
grammar rule governs the key), one arm per required-field shape (the discriminator tracks shape, not
concept), the union field named for what it selects (sibling capabilities may diverge, as `auth`
versus `placement` do), and new variants added as arms, never by pre-grouped mechanism awaiting a
consumer; when distinct required-key shapes discriminate themselves, an untagged structural union
emitted as plain `oneOf` is the sanctioned selector-free form. Second, anything that decides whether
a secret reference or resource edge exists must be model-visible to the walkers: extraction reaches
exactly what validation can select, a union may default only to the mode its omission historically
selected (never a new arm), and extraction reads declared defaults as if written so a defaulted
choice is graph-visible exactly like a written one. Third, cross-field validity among plain config
fields, where the combination touches no graph edge (mutual exclusions, dependencies), may be
enforced by validators failing loudly at load with the emitted schema under-constraining there; wave
2's soundness rule, that the schema must never reject what the loader accepts, sanctions exactly
that under-reporting.

Three tests precede any restructure. First, try dissolving the constraint by giving the forbidden
combination a meaning; one dissolution is ruled (operator, 2026-08-08): install-command entries
accept multiple test predicates with AND semantics, the install skipped only when at least one test
is declared and every declared test passes, so with zero declared tests the command always runs, and
previously invalid documents become valid, a pure widening. Second, the common spelling must not get
heavier; defaults, scalar shorthands, and untagged structural unions are the mitigations, and a
restructure that makes the common case more verbose fails its own test. Third, weigh who is actually
affected against what the payoff buys; an editor-validation gain does not justify heavier manifests
for everyone. Permanent homes: the complete rule in `cli/agentworks/capabilities/README.md`, the
extraction invariant in `schema/extract.py`'s docstring, the default posture's reasoning at the
union sites themselves; the retirement pattern for old shapes is the exact-rewrite hard error plus
the upgrade guide.

Two companion rulings (operator, 2026-08-08): **secret sources are simple KV stores with shared
config**; creation specifications (a minted credential's scopes, repos, permissions) belong to the
consuming capability's domain, never the source or per-secret mapping, so credential minting models
as a git-credential variant. Consequently **git-credential joins the variant contract before the
0.14.0 cut**: a one-arm union restructure (defaulting to the stored arm per the omission-history
rule, with the scalar shorthand as the stored arm's spelling) so minting later lands as a purely
additive arm. Executed: PR #455, merged 2026-08-08.

### Capability descriptor (destination 3)

A core-owned, typed capability-kind descriptor registered once per kind, from which graph stamping,
plugin registration, row publication, inspection, and consistency checks derive. Kinds remain
core-owned; domain operations remain on each kind's interface. The descriptor owns the config schema
contract and the per-kind tagged-union assembly.

**Secret backends are ordinary capabilities, full stop** (operator mandate, 2026-08-05). They live
in the `capabilities/` tree on the shared capability contract, and the descriptor work is free to
massage the base abstraction to make that true; the backend/source split is the mechanism for the
mandate, not an exemption from it (backend parallels `vm-platform`, source parallels `vm-site`). The
one piece the descriptor design must address deliberately is lifecycle layering: secret resolution
runs upstream of every other capability's runup, so a source's own lifecycle sits one stage earlier,
and a source's config must not depend on secrets served by another source unless the active chain's
ordering is explicitly promoted to a resolution order.

### Secrets (destinations 2 and 3)

The two-level `secret-backend` / `secret-source` model: a source exposes KV secrets and maps to one
backend with that source's config; per-source mapping to multiple backends is not required. The
settled reference shape (operator, 2026-08-05) is the synthesized-source model: every per-secret
reference names a source, and zero-config backends get synthesized sources under their current names
(`env-var`, `prompt`) so the simple case keeps its current spelling with only one concept in the
model. Direct backend references hard-error in 0.14 with the exact rewrite (operator ruling,
2026-08-08, superseding the earlier deprecated-compatibility-path posture): no warn window, because
prompt and env-var spellings cross unchanged through their synthesized sources and the affected
surface is effectively the operator's own onepassword config. The resolution API evolves in the same
effort: typed per-secret outcomes, explicit failure categories, policy-aware interaction
requirements, timeouts and cleanup, and bounded-lifetime source clients. The simple case must not
get more verbose.

**Non-TTY secret resolution (open problem, 2026-08-18, from the onboarding-run field evidence):** an
operator-approved, ready secret source is unreachable from a non-TTY caller: the field run's agent
could not resolve through the configured 1Password source and completed the run by resolving the
value itself with `op read` and handing it in through the env-var source, routing around the
configured source and its approval flow. (The env-var source itself is a supported named-secret
workflow per ADR 0013; the problem is the unreachable configured source, not the mechanism the
workaround used.) Two solution attempts (a generalized `--allow-interaction` flag, then an
interaction-channel split attempted as PR #608) were abandoned unmerged by operator direction; the
problem restarts from its statement alone in `task-2026-08-18-non-tty-secret-resolution.md`, with no
solution shape carried over. The restart runs as a new child SDD adopted into this saga (operator
direction, 2026-08-19), seeded by that problem statement.

**Workload-gated config issues (operator ruling, 2026-08-18, same evidence):** config problems are
classified by the fact, not by the commands that tolerate them. A workload-gated issue is one that
only matters when something provisions or interacts with a workload (today's one member: operator
SSH key files missing from disk, a filesystem fact rather than config shape); the loader records it,
one `load_config` parameter says whether it is fatal (default) or warn-only, and the read-only and
diagnostic commands pass warn-only. No severity taxonomy is built until a second class member
exists. Shipped as PR #604; the use-time refactor that retires the parameter is issue #603.

**Instance model and its storage (operator rulings, 2026-08-08 and 2026-08-19):** the database
evaluation, a light repository layer, instance applied-state, CLI instance-spec overlays, and
resolved-spec surfaces are one push rather than four, since they are one data-model arc from
declared spec through overlay and resolution to what was actually applied. The storage work is
assessment-first, and the repository layer is "something in that direction, not full ORM": its shape
is judged by the queries its consumers actually make, never by generality. Seeded as the
`2026-08-19-instance-model` child, which walks through the four open doors above rather than
reopening them; its store contract is what wave 4's applied-state slice depends on.

### Harness scopes (destination 4)

One registered integration identity with per-scope participation (operator simplification,
2026-08-05, superseding the earlier facet framing; see `scope-participation-contract.md`). The
integration API carries per-scope init methods (vm, user, workspace, where `user_init` runs for the
admin during VM init and for each agent during agent init) alongside session start and resume,
called by the per-scope orchestrators at the end of each setup pipeline: core first, then features
in declaration order receiving env-to-date, then integrations receiving all env and agent artifacts
for the scope. Templates at each owning level select their integrations and may attach per-scope
config, which is ordinary capability config belonging to the consuming resource, validated one blob
at a time against the chosen facet's schema (`config_for(facet)`, where a facet is the level a
capability is driven at: vm, user, workspace, session; core owns the scope-to-facet mapping, and
per-facet config is a harness-integration specialty, not framework machinery). Scope discipline is
trust-based: core does not enforce harness behavior; code review and testing gate system plugins,
and wave 8's distribution-trust model gates external ones. Sessions receive all ancestor env and
artifacts, and the integration owns hoisted representation, deduplication, and double-provisioning
avoidance (isolation, not security). Session operations diagnose upstream gaps but never repair
them. Artifact conduct is conventional: smallest ownership unit, no silent overwrite of repository
or generator-owned content, applied state recorded, drift reported. The Claude-specific template
fields (`claude_marketplaces`, `claude_plugins`) migrate into the Claude integration's agent-scope
and admin-scope config. Rulesync informs the artifact design but is not a runtime dependency.

### Observability (destinations 5 and 6)

The universal event vocabulary is Agentworks-owned and independently versioned; ACP is a projection,
never the system of record. Integrations own fusion of every useful source (PTY parsing is
legitimate); core owns identity, primitives, transport, and persistence. Session/run identity
distinguishes the stable logical session from one workload incarnation. Heartbeats are liveness, not
activity: the vocabulary must keep "the workload is alive" and "someone is doing something"
distinguishable, because VM auto-suspend keys on the latter. The layered threat model (observation
fidelity, collector survivability, adversarial assurance) frames what any slice may honestly claim.
The distiller consumes the record store and proposes reviewed PRs, never direct commits; harness
memory is a cache, the repository is the system of record, and distillation is the flush.

### Core surface: installer plugins (operator ruling, 2026-08-07)

The miscellaneous built-in installers in core VM initialization (the package installers and similar
setup steps; authoritative inventory owned by the child effort) move behind one or more system
plugins before the 0.14.0 cut, as a child of this saga (`docs/sdd/2026-08-07-installer-plugins/`).
The ruling deliberately accepts the reopened current-equals-target gap that a late target addition
costs; the child's ledger entry and the 0.14.0 release gate are the catch-up plan. The core keeps
only what is essential to what a VM is. An existing config referencing a moved surface without the
owning plugin enabled fails with a crisp disabled error naming the surface, the plugin, and the
exact remediation, per the remediation posture below. This gives the internal plugin boundary
first-party exercise ahead of wave 8's external promises.

**Scope correction (operator, 2026-08-13):** the effort is the bucketing and nothing more. "I
literally just wanted to bucket the existing installers." The disabled-error experience described
above is a nice-to-have, not this effort's work, so the paragraph's crisp-disabled-error requirement
is deferred rather than dropped.

### Core surface: subtraction before the cut (operator rulings, 2026-08-13)

Six days of high-velocity agentic development left a scaffolding tax around sound core models:
119,584 lines of tests against 83,151 lines of `cli/` code, adversarial validation of first-party
content, tests that police form rather than behavior, and generality with no shipped consumer. Three
rulings follow, recorded here as settled; `phasing.md` carries the ordering detail.

**The simplification pass is a child of this saga** (`docs/sdd/2026-08-12-simplification-pass/`),
not standalone work. It touches every wave's output, its deferred findings route through the saga
lead, and adopting it collapses ledger structure rather than adding it: the pre-0.14
test-consolidation child and the prose-test-purge child are both absorbed into it, and the closeout
wave's test-consolidation item shrinks to a verification sweep.

**Deletion precedes the grammar rewrite.** Rewriting the CLI grammar over a surface that still
carries the deletable scaffolding means the rewrite carries it too, so the pass's wave 0 and its
wave 1 deletions land first. Wave 0 leads in turn, because establishing that always-on rules
actually reach the agents they bind (issue #511) has to precede adding deletion criteria to those
rules. Wave 2, the process and rule subtraction, runs in parallel rather than on that spine; the
pass's reassessment and lock wait for both waves (`phasing.md` carries the detail).

**Sequence amendment (operator, 2026-08-15):** corrected PR #548 is the only remaining
simplification-pass prerequisite for the grammar rewrite. The onboarding-owned one-wave guide
deletion follows it, then the grammar rewrite proceeds. Other wave 1 deletions and wave 2 run
independently; the simplification pass's reassessment and lock still wait for both waves.

**The 0.14 breaking-truth items travel separately.** The four contract-truth fixes that are free
only while 0.14 is unreleased (the secret mapping key that names sources, the one-arm token union,
the env-entry compat flag, the compat layers missing from the retired-shapes inventory) run as their
own dispatched task rather than as a rider on the grammar rewrite, which is already large. Migration
guidance for them flows through `BREAKING CHANGE:` footers, the packaged changelog, and the guide
release-notes topics rather than through compat code.

### Plugin namespace and name stability (operator ruling, 2026-08-07)

**One name, one source.** In-repo contributions (built-ins and system plugins) share one curated
namespace in which a collision is a defect, caught at registration seat time with attribution, never
a runtime policy question. Third-party plugins (wave 8) MUST NOT collide with in-repo names, and use
their unique plugin name as a namespace so cross-plugin uniqueness is structural rather than
policed; the guide's plugin-topic namespacing with its ownership gate (settled in the onboarding
child's design, landing with its phase 1) is the shape wave 8 inherits. How operator declarations
interact with provided names is **open, not settled** (correction, 2026-08-13): the collision
semantics recorded here (name-is-the-contract; silent collision a hard error naming both
remediations; disable-and-redeclare as the sanctioned replacement with provenance surfaced) came
from the installer-plugins child's broader design, which the same day's scope correction cut back to
bucketing the existing installers. That child now preserves current same-name override behavior
unchanged, so collision redesign is deferred to whichever effort next needs it, wave 8 being the
likely home. Defaults-with-override remains reserved for surfaces that declare it, wave 3's
synthesized sources being canonical.

### Compatibility posture (all destinations)

Breaking changes are acceptable across this saga provided each ships with a deprecation runway: warn
in one release, reject in the next (the 0.13 to 0.14 pattern). The runway is a default, not an
absolute: an operator ruling may waive the warn release where the affected population is known and
near-zero, as with wave 2's settings-reference hard errors (2026-08-07) and the secret-sources
direct-reference break (2026-08-08). Deprecations are dropped on their scheduled release rather than
accumulating: wave 1 restores that baseline by clearing every expired surface, and each later
breaking wave clears its own runway on schedule so the target state carries no expired
compatibility. The generic deprecation framework survives every cleanup.

**Remediation is precise errors plus the guide, not automated migrators** (operator ruling,
2026-08-07). A breaking change ships with hard errors that name the offending input and the exact
remediation, and with guide content that walks the operator or their agent through the rewrite;
Agentworks does not maintain automated migration tooling. The ruling came from the wave 2 review:
`agw resource migrate` required a frozen re-implementation of the old shapes as a verification
oracle, and every divergence between oracle and model surfaced as a self-blaming failure. Its
deliberate deletability (the separability guard) let it be removed before release rather than
maintained until a scheduled expiry. The agent-led path verifies with real surfaces (`agw doctor`,
loading the result) instead of an oracle, and the guide teaches it in the same release that breaks
the old inputs. One consequence stays on the ledger: the manifest surface currently has no
deprecation warn-window channel, so a future manifest-shape deprecation must rebuild one or ship as
a hard break with guide coverage.

**Requirements are priced like code** (operator rulings, 2026-08-09, from the twin scope
corrections). Two efforts grew multi-thousand-line defensive subsystems from requirements no owner
had priced: doctor's hostile-filesystem snapshot protocol (defending a diagnostic read against
threats outside the workstation trust model) and wave 3's frame-erasure machinery (attempting to
prove a Python string never survives in any traceback frame, when immutable strings make the process
the only real trust boundary). Both were unwound to simple, honest contracts: doctor reads state
through an ordinary read-only open and says so, and secret handling promises no persistence, no
argv/logs/exception-objects, late resolution, and stdin delivery, with in-memory retention
explicitly best-effort. The durable posture: adversarial verification verifies the contract and
never expands it; a finding that survives two or three fix rounds indicates a contract to re-price
with the operator, not machinery to grow; and where absolute in-memory elimination is ever truly
required, the sanctioned design is an isolated short-lived process, not application-layer cleanup.

### Cross-cutting: anchored projections (all destinations)

A recurring principle, now named (operator agreement, 2026-08-05), that child SDDs should test
designs against: contributions declare rather than do, and access arrives as an anchored, typed
projection rather than ambient authority. Instances already settled across this saga: the `me`
anchored template projection, per-integration state namespacing, declared secret references resolved
at the operation boundary, core performing tmux and PTY operations on integrations' behalf, and the
universal event representation. The principle governs surfaces where enforcement is real; trusted
in-process integration code is governed by trust, review, and disclosure instead (operator ruling,
2026-08-05), which is why harness scope discipline is a reviewed convention, not a grant system. The
review question for any new contribution surface: what does the contribution see, and where is that
view enforced, and if it cannot be, who reviewed the trust?

The template projection is expected to be the resource graph itself in a gated access mode, not a
second structure kept in lockstep: powers (secret readers, run targets, capability API objects) sit
behind callables a mode can gate, while universal facts are plain data on the nodes. Gated modes
expose only already-materialized data; nothing lazily computes through a power while wearing
attribute syntax. Gating by permission check or by leaving powers unwired are both legitimate
mechanisms, chosen per surface and done properly. Authored content still carries the teaching; the
graph carries the dynamic truth, and no effort should over-index on pushing everything into the
graph. Where a projection is impossible (the workstation agent sits outside the platform), the
principle inverts to disclosure, per the onboarding security disclosure.

### Cross-cutting: shared traversal discipline (all destinations)

Traversals of operator-controlled graphs (inheritance chains, reference graphs, nested model walks
over operator data) go through shared, iterative or memoized, cycle-safe helpers rather than each
module hand-rolling recursive descent (operator agreement, 2026-08-07, from the wave 2 review: the
registry's cycle detector was deliberately iterative while a finalize-pass walker upstream of it was
hand-rolled, unmemoized, and exponential on diamond inheritance). Bounded walks over code-shaped
structures (a model class's own fields) may stay naturally recursive; the discipline applies where
the input size or shape is the operator's to choose. The closeout wave checks this property across
everything the saga touched.

## Explicitly out of scope

These are not part of this saga's target state. They are recorded so their triggers are not lost,
and so no wave accidentally forecloses them:

- **The living graph** (per-instance specs introducing post-finalize graph updates). A future SDD;
  the four open doors under destination 2 keep it unblocked.
- **The herdr rendering backend.** Gated on its spike per the 2026-07-30 ruling; the
  ephemeral-agents direction and observability's authoritative state reporting are the revisit
  triggers.
- **The named-console-template selector SDD** (`2026-07-19`, drafted pre-saga) and the
  companion-shell and resilient-attach wins unbundled from the herdr FRD. Standalone work that
  proceeds independently of this saga.
- **The agentworks.build website** (`docs/sdd/2026-08-07-website/`, seeded 2026-08-07 at operator
  request as a standalone SDD, deliberately not a child: it consumes saga outputs rather than gating
  any wave, and adding it late would reopen the current-equals-target gap). Relationship: the site
  renders from the same authoritative sources as the guide and reference surfaces, never a
  hand-maintained second copy, and its growth path (web-rendered guide topics, schema-derived
  reference) consumes wave 2 and onboarding surfaces as they land on `main`. Launch timing may pair
  with the 0.14.0 cut as an operator call without structural coupling. **Ruling (operator,
  2026-08-11):** one coupling does exist, at the end of the sequence rather than at launch. The
  Lander and onboarding both gate the **final custom-domain cutover**, so the interim activation the
  website plan's Phase 6 describes may not proceed while onboarding is pending. Rationale: the
  domain is the front door, and the operator will not point it at a site whose onboarding path is
  unfinished. This qualifies the no-structural-coupling statement above without creating a saga-lock
  edge: the website still gates no wave and no lock, `phasing.md` acquires no reverse dependency,
  and the gate binds two adjacent efforts to each other. The website effort owes the matching
  updates to its Phase 6, 8 and 9 definitions of done, its activation runbook, and
  `website/README.md` (flagged on PR #486).

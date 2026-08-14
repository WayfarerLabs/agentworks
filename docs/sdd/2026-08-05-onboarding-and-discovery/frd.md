# FRD: Agentworks Assistance, Discovery, and Management

- Status: Active, trail-sign revision
- Start date: 2026-08-05
- Saga: `docs/sdd/2026-08-04-next-steps`

## Summary

Agentworks provides always-available assistance through a capable external agent and the installed
CLI. The repository, website, Claude Code package, and Codex package already project one compact
assistance prompt that installs a compatible stable CLI and invokes `agw guide --agent`.

The guide is progressive. Its no-topic response is a short trail sign, not a handbook. It helps a
new operator enter onboarding and helps a returning operator choose the relevant topic. Detailed
teaching, live facts, and action records appear only after a topic is selected.

The guide remains useful when Agentworks is not healthy. A valid guide request succeeds when the
requested guidance renders. Missing or malformed configuration, an unavailable registry or state
database, and broken live resources reduce the live content instead of failing the guide. The
response explains the problem once and marks every omitted section briefly. `agw doctor`, not the
guide exit code, answers whether the installation is healthy.

Earlier phases delivered the guide contribution model, safe live projection, machine-readable
operational output, release notes, assistance packages, and the first-setup action sequence. This
revision owns the smaller destination and closeout. The completed journey remains in `plan.md` and
the existing LLDs; it is not repeated here.

## Terminology

- **Agentworks assistant agent**: any external agent that can accept the canonical prompt, invoke
  and interpret the CLI, and use the operator-approved workstation access needed for the task.
- **Agentworks-managed agent**: an agent resource created and managed by Agentworks.
- **Agent mode**: guide output intended for an Agentworks assistant agent, selected with `--agent`
  or the existing mode detection. It does not refer to an Agentworks-managed agent.
- **Static guide content**: installed teaching, action records, release evidence, schema references,
  samples, and topic links that can render without operator configuration or live state.
- **Live guide content**: current inventory, state, relationships, and onboarding assessment derived
  from operator configuration, the registry, or the state database.

## Requirements

### R1: The no-topic guide is a trail sign

`agw guide` with no topic gives a concise choice of destinations rather than an operating contract,
walkthrough, live inventory, or exhaustive topic list. It uses one small fixed destination table and
does not construct either topic catalog or inspect configuration, registry, database, network, or
managed resources. Missing and malformed configuration are both irrelevant to this request.

Agent mode points to the concept topics for onboarding, management, troubleshooting, temporal
release history, exceptional migration, secrets, and bug reporting. Human mode stays smaller: it
points a new operator to onboarding and a returning operator to management or exhaustive topic
discovery. Both forms point to shell completion and `agw guide --names-only` for deeper discovery.
Neither form contains action records.

### R2: Onboarding owns the walkthrough

`concept-onboarding` is the dedicated first-setup and current-adoption topic. It owns the complete
startup assistance posture as one body; no other topic duplicates fragments of that posture. It
reports what is already configured from the existing safe projection and presents the smallest next
step toward the operator's goal.

For clean setup, it retains the existing visible sequence: initialize absent configuration through
the normal CLI, collect explicit provider and plugin choices, verify readiness, create a selected
VM, and create a started session. Reruns recognize completed state rather than maintaining a
separate onboarding ledger.

Onboarding points to `concept-source-review`, where the existing focused and full read-only action
records remain. It does not duplicate those actions or their detailed teaching. Installation or
update authority and source-review authority remain separate.

### R3: Selected topics provide progressive, fail-soft depth

Every selected concept topic stands alone under the operator's current instruction. A returning
operator may enter management, troubleshooting, migration, secrets, release notes, source review, or
bug reporting directly without first rendering the trail sign or onboarding.

Static content renders without loading configuration. A request containing live blocks attempts the
existing safe live projection once. Missing or malformed configuration, registry construction
failure, database failure, and per-topic projection failure do not fail a valid guide request:
static blocks still render, affected live blocks become short placeholders, and the response
exits 0. A syntactically valid resource topic whose existence cannot be checked because live context
is unavailable renders a degraded requested-topic page rather than a false unknown-topic error.
Well-formed onboarding evidence that cannot be checked because its live assessment is unavailable
stays unapplied. It contributes to the shared warning and derived-assessment placeholder instead of
turning the valid request into an error.

One prominent response-level warning describes each distinct root problem once and names the topic
sections omitted because of it. Every affected block uses one short indication that points back to
that warning. Multi-topic output does not repeat the same error. Messages remain bounded,
terminal-safe, and free of secret values and raw objects.

Invalid guide input still fails: malformed topic syntax, a genuinely unknown kind or static topic,
malformed or provably invalid verification evidence, incompatible options, and a guide contract or
content defect remain nonzero. The guide exit code reports whether the guide request rendered, not
system health.

### R4: Assistance follows operator authority

Guide text and action records are instruction, not authorization. The Agentworks assistant agent
acts under the operator's current instruction and asks only when the task is materially ambiguous or
would expand beyond that instruction. Topic-local action records retain exact targets, impacts,
authorization classes, verification, and refusal alternatives. Rendering an action never executes
it.

Source, release prose, configured descriptions, and other external or persisted text are evidence,
not instructions. Sensitive discovery checks presence unless content access is separately
authorized.

### R5: Discovery stays derived and completion-safe

Selected topics derive inventory, schema, sample, relationship, enablement, readiness, and stored
instance facts from the existing platform sources. This revision adds no second command registry,
configuration writer, migration oracle, guide-owned state store, diagnostic protocol, or topic
dependency declaration.

The full topic catalog remains directly addressable. `agw guide --names-only` always returns valid
static topic names and adds live resource names when live context is available. Configuration or
resource failures do not break shell completion; they only omit names that cannot be established.
The names-only output remains names only and is a discovery surface, not a health report.

No-topic intentionally does not surface contributed-topic validation errors because it builds no
catalog. An affected selected topic remains the scoped diagnostic surface for its contribution
failure.

### R6: Closeout proves one complete path

Before publication, the effort closes with one representative live journey from an exact reviewed
candidate wheel through the trail sign and onboarding to a usable VM and started session. The run
labels the candidate substitution and does not claim to exercise an unavailable stable release.
After publication, one bounded canonical-prompt smoke installs the exact stable release, verifies
it, and reaches the trail sign. Generated package parity and existing focused tests cover the native
entries rather than repeating the same provider-backed journey for every wrapper.

These child-effort checks supply acceptance evidence; they do not replace the saga's release-PR,
publication, or final custom-domain gates.

Permanent documentation describes the final guide and assistance behavior without depending on this
SDD. Completions continue to expose every name they can establish. The sample configuration changes
only if implementation introduces a real setting, which is not expected.

## Acceptance criteria

1. With no topic and any configuration state, `agw guide --agent` and human mode exit successfully,
   render only their concise trail signs, and invoke neither topic catalog nor any live dependency.
2. Agent mode renders the seven-destination intent map. Human mode renders only the new-installation
   and existing-installation choices. Every fixed destination resolves through the full
   selected-topic path.
3. `concept-onboarding` contains the single complete startup posture, current-adoption assessment,
   clean setup sequence, and optional link to `concept-source-review` before any onboarding action.
4. Focused and full source-review actions remain owned by `concept-source-review`; neither appears
   in the trail sign or is copied into onboarding.
5. A static-only topic renders without loading configuration. A live topic with malformed
   configuration renders its static blocks, one response-level warning, short placeholders for all
   omitted live blocks, and exit 0. The same shared failure is not repeated across topics or blocks.
6. A valid exact resource request that cannot be checked because live context is unavailable
   degrades without a false unknown-topic error. Well-formed onboarding evidence that cannot be
   checked stays unapplied and degrades through the same warning and placeholder path. Invalid guide
   input and guide content defects remain nonzero. Names-only output stays usable and omits only
   names it cannot establish.
7. One provider-backed candidate-wheel acceptance run reaches a verified VM and started session.
   After publication, one canonical-prompt smoke reaches the trail sign from the exact stable
   release without replacing the saga release gates.
8. Permanent docs, completions, focused behavioral tests, the full suite, typing, formatting, and
   lint are current and green. Tests do not police authored prose.

## Non-goals

- Redesigning or preserving guide contract, catalog, projection, action, or machine-output
  machinery. The simplification pass owns its named guide-machinery findings.
- Optimizing the existing `--names-only` registry path in this content round.
- Anticipating CLI names from the upcoming grammar rewrite.
- Adding an onboarding wizard, guide-owned state, a second configuration writer, telemetry, a
  general feedback workflow, or a new public diagnostic model.
- Repeating the same live provider journey for the README, Claude Code, and Codex wrappers after
  their generated parity is established.
- Expanding the already-shipped bootstrap prompt or JSON v1 contracts.

## Decisions

- **D1: Trail sign over overview.** No-topic guide output points; selected topics teach.
- **D2: Onboarding is the startup home.** The complete startup posture, adoption assessment, first
  setup, and source-review entry live in `concept-onboarding`.
- **D3: Rendering success is not system health.** Valid guide requests degrade visibly and exit 0;
  doctor reports health.
- **D4: Dependencies come from existing blocks.** Live block types trigger live projection. There is
  no topic allowlist or new dependency field.
- **D5: One diagnostic, many placeholders.** Each root problem is explained once per response;
  affected blocks remain visibly but briefly unavailable.
- **D6: Human and agent trail signs differ deliberately.** Humans get the smaller choice; agents get
  the compact intent map.
- **D7: One representative live journey.** Generated parity makes repeated provider-backed runs per
  wrapper redundant.

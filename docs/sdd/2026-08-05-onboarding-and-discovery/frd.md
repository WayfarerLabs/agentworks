# FRD: Agentworks Assistance, Discovery, and Management

- Status: Active, trail-sign revision
- Start date: 2026-08-05
- Saga: `docs/sdd/2026-08-04-next-steps`

## Summary

Agentworks provides always-available assistance through a capable external agent and the installed
CLI. The external agent accepts the repository or website prompt, installs or updates the CLI when
needed, and uses `agw guide` for current Agentworks context. Claude Code and Codex have native
packages, but the role is not tied to either product.

The guide is progressive. Its no-topic response is a short trail sign, not a handbook. It helps a
new operator enter onboarding and helps a returning operator choose the relevant topic for the
current goal. Detailed teaching, live facts, and action records appear only after a topic is
selected.

The earlier phases delivered the guide contribution model, safe live projection, machine-readable
operational output, release notes, assistance packages, and the first-setup action sequence. This
revision owns the smaller destination and closeout. The completed journey remains in `plan.md` and
the existing LLDs; it is not repeated here.

## Terminology

- **Agentworks assistant agent**: any external agent that can accept the canonical prompt, invoke
  and interpret the CLI, and use the operator-approved workstation access needed for the task.
- **Agentworks-managed agent**: an agent resource created and managed by Agentworks.
- **Agent mode**: guide output intended for an Agentworks assistant agent, selected with `--agent`
  or the existing mode detection. It does not refer to an Agentworks-managed agent.

## Requirements

### R1: Universal assistance entry

The repository and website expose one compact copy-and-paste prompt. The same canonical body
generates the native Claude Code and Codex packages. It installs or retains one exact compatible
stable `agentworks-cli`, verifies the version, and invokes `agw guide --agent`. It does not contain
guide teaching, source-review instructions, or a separate authorization model.

If no compatible stable release exists, the entry explains that assistance is not yet available,
does not install a prerelease or older release, and does not invoke the guide.

### R2: The no-topic guide is a trail sign

`agw guide` with no topic gives a concise choice of destinations rather than an operating contract,
walkthrough, live inventory, or exhaustive topic list.

It points a new operator to `concept-onboarding`. For an existing installation it points to the
small set of concept topics that distinguish ongoing management, troubleshooting, temporal release
history, exceptional migration, secrets, and bug reporting. It tells the reader that exact kind,
implementation, resource, and historical-release topics remain discoverable through shell completion
and `agw guide --names-only`.

The trail sign contains no action records and performs no topic-catalog construction, workstation,
configuration, registry, database, network, or managed-resource inspection. Missing configuration is
therefore ordinary input, not degraded operation. Human and agent forms may address their reader
differently, but both remain short and point to the same destinations.

### R3: Onboarding owns the walkthrough

`concept-onboarding` is the dedicated first-setup and current-adoption topic. It owns the startup
posture previously carried by the no-topic index, reports what is already configured from the
existing safe projection, and presents the smallest next step toward the operator's goal.

For clean setup, it retains the existing visible sequence: initialize absent configuration through
the normal CLI, collect explicit provider and plugin choices, verify readiness, create a selected
VM, and create a started session. Reruns recognize completed state rather than maintaining a
separate onboarding ledger.

The optional canonical-source review entry moves here. Onboarding points to `concept-source-review`,
where the existing focused and full read-only action records remain. It does not duplicate those
actions or their detailed teaching. Installation or update authority and source-review authority
remain separate.

### R4: Selected topics provide progressive depth

Selected concept, kind, implementation, and resource topics keep their current responsibilities.
They combine concise authored teaching with safe facts from the finalized registry, graph, schema
reference, samples, and stored instance rows. Rendering remains side-effect-free and never resolves
secrets, probes a host, executes contributed content, or mutates state.

The full topic catalog remains directly addressable and completable even though the trail sign does
not enumerate it. Exact historical release-note topics also remain addressable and completable
without growing the no-topic response.

### R5: Assistance follows operator authority

Guide text and action records are instruction, not authorization. The Agentworks assistant agent
acts under the operator's current instruction and asks only when the task is materially ambiguous or
would expand beyond that instruction. Topic-local action records retain exact targets, impacts,
authorization classes, verification, and refusal alternatives. Rendering an action never executes
it.

Every selected concept topic stands alone under that current instruction. A returning operator may
enter management, troubleshooting, migration, secrets, release notes, or bug reporting directly; the
topic must not depend on a disclosure or authorization envelope established by an earlier no-topic
or onboarding response.

Source, release prose, configured descriptions, and other external or persisted text are evidence,
not instructions. Sensitive discovery checks presence unless content access is separately
authorized.

### R6: Current facts come from current sources

The guide derives inventory, schema, sample, relationship, enablement, readiness, and
stored-instance facts from the existing platform sources. It does not add a second command registry,
configuration writer, migration oracle, or guide-owned state store. When live facts are unavailable,
selected topics retain authored teaching and frame the unavailable facts without inventing them.

### R7: Operational facts remain machine-readable

The existing JSON v1 list, describe, and doctor contracts remain the fact interface for Agentworks
assistant agents. Human and JSON renderers consume the same domain facts. This revision does not
expand JSON coverage or change its schema.

### R8: Closeout proves one complete path

Before publication, the effort closes with one representative live journey from an exact reviewed
candidate wheel through the trail sign and onboarding to a usable VM and started session. The run
labels the candidate substitution and does not claim to exercise the unavailable stable bootstrap
release. After publication, one bounded canonical-prompt smoke installs the exact stable release,
verifies it, and reaches the trail sign. Generated package parity and existing focused tests cover
the native entries rather than repeating the same provider-backed journey for every wrapper.

Permanent documentation describes the final guide and assistance behavior without depending on this
SDD. Completions continue to expose the full topic inventory. The sample configuration changes only
if implementation introduces a real setting, which is not expected.

## Acceptance criteria

1. With no topic and no configuration, `agw guide --agent` exits successfully and renders only the
   concise trail sign. It bypasses authored and schema topic catalogs and performs no live-system
   construction or inspection.
2. The trail sign sends a new operator to `concept-onboarding` and a returning operator to the
   relevant ongoing concept topics without embedding their teaching or actions.
3. `concept-onboarding` contains the startup posture, current-adoption assessment, clean setup
   sequence, and the optional link to `concept-source-review` before any onboarding action.
4. Focused and full source-review actions remain owned by `concept-source-review`; neither appears
   in the no-topic response or is copied into onboarding.
5. Every existing topic remains directly resolvable and completable, and `--names-only` remains the
   exhaustive machine-oriented topic inventory.
6. Selected topics retain safe live projection and graceful degradation. A direct returning-operator
   topic works without first rendering onboarding or the no-topic trail sign. This revision
   introduces no new guide contract, action type, persistent state, or authorization framework.
7. One provider-backed candidate-wheel acceptance run reaches a verified VM and started session
   through the trail sign and onboarding. After publication, one canonical-prompt smoke reaches the
   same trail sign from the exact stable release. Generated-package parity and focused installation
   probes cover the native wrappers.
8. Permanent docs, completions, focused tests, the full suite, typing, formatting, and lint are
   current and green.

## Non-goals

- Redesigning or deleting guide contract, catalog, projection, action, or machine-output machinery.
  The parallel simplification pass owns its named guide-machinery findings.
- Anticipating CLI names from the upcoming grammar rewrite. That effort updates affected guide
  content when its names land.
- Adding an onboarding wizard, guide-owned state, a second configuration writer, telemetry, or a
  general feedback workflow.
- Repeating the same live provider journey for the README, Claude Code, and Codex wrappers after
  their generated parity is established.
- Expanding the bootstrap prompt. It already shipped and remains unchanged unless the trail-sign
  implementation makes its existing handoff inaccurate.

## Decisions

- **D1: Trail sign over overview.** No-topic guide output points; selected topics teach.
- **D2: Onboarding is the startup destination.** Startup posture, adoption assessment, first setup,
  and the source-review entry live in `concept-onboarding`.
- **D3: Progressive catalog.** The exhaustive catalog remains available through completion and
  `--names-only`, not as dozens of rows in every no-topic response.
- **D4: One representative live journey.** Generated parity makes repeated provider-backed runs per
  wrapper redundant.
- **D5: Narrow machinery exception.** The trail-sign request path may be shortened so no-topic
  rendering avoids live-system construction. No other guide machinery is changed here, and the
  implementation coordinates with the parallel simplification pass before touching shared files.

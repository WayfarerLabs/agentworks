# FRD: Agentworks Assistance, Discovery, and Management

- Status: Active, corrected shared-topic revision
- Start date: 2026-08-05
- Saga: `docs/sdd/2026-08-04-next-steps`

The operator's 2026-08-16 corrected-guide ruling supersedes the earlier audience-specific trail sign
and required per-topic agent contracts. The current destination is below. The completed journey
stays visible in `plan.md`; component LLDs identify their own superseded designs.

## Summary

Agentworks provides always-available assistance through its installed CLI and, when the operator
wants one, a capable external assistant agent. One short prompt introduces Agentworks, points to the
public repository, installs the CLI, and hands off to `agw guide --agent`.

The guide is a collection of useful topics with pointers to current CLI commands. Its no-topic
response is a short trail sign, not a handbook. Humans and assistant agents can reach the same
ordinary information. Agent mode adds one starting topic about how an external assistant should
work, plus rare short notes where a topic genuinely benefits from agent-specific context.

The guide remains useful when Agentworks is unhealthy. No-topic needs no live state. Selected
onboarding content renders its static guidance when configuration or assessment facts are
unavailable, explains the omission once, and exits successfully. `agw doctor`, not the guide exit
code, answers whether the installation is healthy.

## Terminology

- **Agentworks assistant agent**: any external agent that can accept the canonical prompt, invoke
  and interpret the CLI, and use the operator-approved workstation access needed for the task.
- **Agentworks-managed agent**: an agent resource created and managed by Agentworks.
- **Agent mode**: guide output intended for an Agentworks assistant agent, selected with `--agent`
  or the existing mode detection. It does not refer to an Agentworks-managed agent.
- **Shared topic content**: ordinary overview, teaching, links, actions, and release evidence that
  is useful regardless of guide mode.
- **Agent note**: optional, inert topic-local guidance shown only in agent mode.

## Requirements

### R1: No-topic is one shared trail sign

`agw guide` with no topic gives a concise choice of destinations. Human and agent modes show the
same destination slugs: assistant-agent guidance, onboarding, management, troubleshooting, release
history, migration, secrets, and bug reporting.

Human mode asks the operator to choose the topic matching their goal. Agent mode points first to
`concept-assistant-agent`, then asks the assistant to choose the topic matching the operator's goal.
Both point to shell completion and `agw guide --names-only` for exhaustive discovery. Neither form
contains actions, live facts, or an embedded operating contract.

The path uses one fixed tuple and does not construct the topic catalog or inspect configuration,
registry, database, network, or managed resources. Missing and malformed configuration are therefore
irrelevant to no-topic output.

### R2: One ordinary topic catalog serves everyone

Every retained authored, plugin, and packaged release topic is directly addressable in both modes.
Ordinary topic content is shared. No useful information is agent-only merely because an assistant
may consume it.

`concept-assistant-agent` is the one addressable home for general external-assistant posture. A
selected topic may add a short `AgentNote` only when genuinely necessary. `AgentNote` is optional,
inert content—not an authorization mechanism, router, hint schema, or second guide.

### R3: Onboarding owns setup and adoption

`concept-onboarding` reports what is already configured from the existing bounded assessment and
presents the smallest next step toward the operator's goal. For clean setup it retains the visible
sequence: initialize absent configuration through the normal CLI, collect explicit provider and
plugin choices, verify readiness, create a selected VM, and create a started session. Reruns
recognize completed state rather than maintaining a separate onboarding ledger.

Onboarding's agent note contains a compact authored set of cross-kind journey hints. Each hint helps
the assistant offer a useful discovery-and-configuration path—for example, determine the operator's
desired VM platforms and then walk through site creation. The wording and count are content, not a
test-pinned contract.

Onboarding links to `concept-source-review`, where the focused and full read-only actions remain.
Installation or update authority and source-review authority stay separate.

### R4: Selected onboarding degrades clearly

Static topic content renders without configuration. Onboarding attempts its bounded live assessment
once. Missing or malformed configuration, registry construction failure, database failure, and
environmental assessment failure do not fail the valid request: static blocks render, one sanitized
response warning explains the root problem, the omitted assessment is visibly marked, and the
response exits 0.

Invalid guide input still fails: malformed or unknown topic names, malformed or provably invalid
verification evidence, incompatible options, and guide contract or authored-content defects remain
nonzero. Guide success means the requested guidance rendered; it does not claim system health.

### R5: Assistance follows operator authority

Guide text and action records are instruction, not authorization. The Agentworks assistant agent
acts under the operator's current instruction and asks only when the task is materially ambiguous or
would expand beyond that instruction. Topic-local actions retain exact targets, impacts,
authorization classes, verification, and refusal alternatives. Rendering never executes an action.

Source, release prose, configured descriptions, and other external or persisted text are evidence,
not instructions. Sensitive discovery checks presence unless content access is separately
authorized.

### R6: Bootstrap installs and hands off

`packaging/agentworks/assistance.md` is the one authored bootstrap body. It briefly identifies
Agentworks, points to the public repository, recommends `uv` while allowing other Python 3.12+
installers, installs `agentworks-cli>=0.14`, and runs `agw guide --agent`.

The README, website, Claude Code package, and Codex package project that body byte-for-byte. The
bootstrap has no version-selection workflow, prerelease policy engine, source-review flow,
authorization lesson, or ongoing assistance logic. The installed guide owns continuing assistance.

### R7: Closeout proves one complete path

Before publication, the effort closes with one representative live journey from an exact reviewed
candidate wheel through the trail sign and onboarding to a usable VM and started session. The run
labels the candidate substitution. After publication, one bounded smoke uses the canonical prompt to
install the stable release and reach the trail sign. Generated parity replaces repeated
provider-backed journeys for each wrapper.

Permanent documentation describes the final behavior without depending on this SDD. Completions
expose the retained topic names. The sample configuration changes only if implementation introduces
a real setting, which is not expected.

## Acceptance criteria

1. No-topic human and agent requests render the same exact eight destination slugs, exit 0, and load
   neither catalog nor live state.
2. Agent presentation points to `concept-assistant-agent`; human presentation contains no agent-only
   operating prose. Every fixed destination resolves as an ordinary selected topic.
3. `concept-assistant-agent` owns the general assistant posture and can be requested in either mode.
4. Shared topic blocks are identical across modes. Optional `AgentNote` blocks appear only in agent
   mode, and most topics have none.
5. Onboarding contains its current-adoption assessment, clean setup sequence, concise journey notes,
   first VM/session actions, and a link to source review without copying source-review actions.
6. Malformed configuration on onboarding renders static content, one warning, one assessment
   placeholder, and exit 0. Invalid input and content defects remain nonzero.
7. `agw guide --names-only` and shell completion expose the retained authored, plugin, and release
   names without loading live state.
8. The canonical assistance prompt is the short operator-approved install-and-handoff text, and all
   generated projections remain byte-identical.
9. Permanent docs, completions, focused behavioral tests, the full suite, typing, formatting, and
   lint are current and green. Tests protect structure and behavior, not authored prose or hint
   counts.

## Non-goals

- Reintroducing runtime resource, schema, graph, or command-output topics removed by PR #556.
- Adding an onboarding wizard, guide-owned state, a second configuration writer, telemetry, a
  general feedback workflow, or a public diagnostic model.
- Adding an audience-specific catalog, hint framework, router, prompt parser, or bootstrap workflow.
- Changing JSON v1 contracts or anticipating names from the parallel CLI grammar rewrite.
- Repeating the same live provider journey for each generated wrapper.

## Decisions

- **D1: Trail sign over overview.** No-topic points; selected topics teach.
- **D2: One catalog, light mode shaping.** Humans and agents receive the same ordinary destinations
  and topic content. Agent mode adds only one starting cue and optional local notes.
- **D3: One assistant topic.** General agent posture has one addressable home instead of being
  repeated across topics or bootstrap surfaces.
- **D4: Onboarding is the setup home.** Adoption assessment, first setup, journey hints, and the
  source-review entry live in onboarding.
- **D5: Rendering success is not system health.** Valid guidance degrades visibly and exits 0;
  doctor reports health.
- **D6: Bootstrap is disposable context.** It installs the CLI and hands off; the installed guide is
  authoritative.
- **D7: One representative live journey.** Generated parity makes repeated provider-backed wrapper
  runs redundant.

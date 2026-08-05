# Session Observability and Interaction Perspective

- Status: Initial perspective
- Date: 2026-08-04
- Baseline: Agentworks 0.13.0 (`v0.13.0`)
- Source material: Prior draft transcript requirements and the architectural review that followed

## Purpose

This document records a perspective on session transcripts, observability, and optional interactive
control. It is an input to the requirements and architecture work that will follow in this SDD. It
is not yet a functional specification or implementation plan.

The central idea is that every harness integration synthesizes one ordered, best-effort Agentworks
event stream from every useful source available for that harness. Core supplies common observation,
input, correlation, transport, and persistence primitives. Each integration owns the
harness-specific fusion problem.

The universal guarantee is the representation and its semantics, not completeness of capture.

## Strategic Motivation

Agentworks sessions are terminal-backed workloads that can persist across detach and reattach while
retaining a disposable session lifecycle. Operators need two additional ways to understand and use
them:

1. A durable transcript that records, as well as reasonably possible, what happened in the session.
2. An optional secondary interface for observing and driving limited interactions without replacing
   the ordinary terminal experience.

The terminal-backed workload must remain independently operable. A structured control plane cannot
become a prerequisite for attaching to, operating, or recovering a TUI-backed session.

This is strategically important because native SDK and app-server access can be constrained by
changing vendor authentication terms, supported credential flows, licensing, product policy, or
protocol availability. An architecture that requires a vendor SDK as the only workload path would
make Agentworks dependent on policies outside its control.

Running optional structured observation and control alongside the standard TUI produces a different
risk profile. If the secondary frontend, ACP bridge, or vendor integration fails, the operator can
still attach through tmux and continue working. The secondary interface is an additional mobile and
programmatic access path, not the foundation on which the session depends.

## Core Model

### One session, one synthesized event stream

A transcript belongs to exactly one session. Named consoles and other aggregators may display or
control sessions, but they do not produce, merge, or own transcript streams.

The PTY is one information source, not the canonical source. An integration should consider every
source available for its harness:

```text
PTY observation ------------------+
Harness transcript files ---------+
Hooks ----------------------------+--> integration fusion --> Agentworks events
Native APIs and state stores ------+
Supervisor and process signals ----+
```

One integration might obtain messages from a transcript file, pending decisions from hooks, tool
activity from a native event stream, and terminal liveness from the PTY. Another might have no
structured source and derive most useful events by parsing PTY output. Both emit the same universal
Agentworks representation where their observations support it.

PTY parsing is therefore explicitly allowed. The integration uses the highest-fidelity mechanisms
available, but no global rule forbids a lower-fidelity mechanism when that is what makes a harness
observable.

### Best effort is a load-bearing contract

No integration can promise perfect capture under every condition. Sources may be delayed,
duplicated, reordered, incomplete, undocumented, or unavailable. A TUI changes its rendering. A
transcript file flushes only after a turn. A hook observes decisions but not messages. A bounded
buffer drops output under pressure. A compromised workload suppresses or falsifies its own events.

The framework should not hide these facts. Universal means:

- Common event and intent semantics.
- Stable session and correlation identity.
- Explicit origin and observation source.
- Declared observation and control support.
- Explicit loss, degradation, and blindness signals where detectable.
- A common API for downstream consumers.

Universal does not mean equal semantic fidelity across harnesses or proof that every activity was
captured.

### Integration-owned fusion

Each integration owns:

- Which sources to use and how to access them.
- Source priority and authority for each event class.
- Ordering and reconciliation across sources.
- Deduplication of repeated observations.
- Correlation of inputs, echoes, harness records, and resulting output.
- Interpretation of current harness state.
- Translation into the universal event vocabulary.
- Harness-specific extension data.
- Reporting of known degradation or loss.
- The subset of interactive intents that can be delivered safely.

This is what makes the model scalable. Adding a harness creates one bounded harness-specific fusion
problem. It does not require Agentworks core or every frontend to learn that harness's transcript
timing, terminal grammar, hook vocabulary, state database, or native protocol.

## Universal Representation

### Agentworks owns the vocabulary

The durable representation should be Agentworks-owned and independently versioned. ACP and other
client protocols are projections or adapters over it, not the system of record.

The vocabulary should include at least:

- Session and process-incarnation lifecycle.
- Inputs and their ingress origin.
- User and agent messages.
- Tool activity.
- Pending decisions and their resolutions.
- Shell and supervisor activity.
- Raw or diagnostic terminal observations.
- Integration degradation and loss.

Human shell commands must not be presented as model tool calls. Observation provenance must not
fabricate model intent.

### State transitions and pending interactions

Interactions with duration should be represented as correlated state transitions rather than only
completion summaries:

```text
decision.requested --> decision.resolved | decision.expired | decision.cancelled
tool.started -------> tool.completed
turn.started -------> turn.ended
command.started ----> command.exited
```

Pending state matters both historically and interactively. It makes blocked or hung activity visible
and supplies the state against which a later interactive response can be validated.

### Logical sessions and run incarnations

The representation should distinguish the stable logical session from one launch of its workload. A
resume can retain the same session while replacing its workload process. The current per-session
tmux topology also replaces that session's tmux server, but workload incarnation is the durable
identity concept; tmux-server identity should be modeled separately only if a later topology needs
it.

Conceptually:

```text
session_id: stable across the logical session
run_id: one workload-process incarnation
```

Sequencing, heartbeat expectations, lifecycle events, and unfinished interaction pairs need explicit
semantics across run replacement.

### Observation metadata

Events need to distinguish where a logical activity originated from where a particular observation
was obtained.

For example:

```text
origin: acp
source: pty-output
```

means that an input entered through ACP and was subsequently observed on the terminal. An input
typed at an attached terminal and later found in a harness transcript could have:

```text
origin: pty
source: harness-transcript
```

The schema may also need a small fidelity vocabulary, such as `native`, `observed`, `inferred`, and
`raw`. This should remain simple and should communicate meaningful limitations rather than becoming
an elaborate confidence-scoring system.

## Inputs Are Part of the Transcript

Every input Agentworks accepts, and every externally entered input the integration can observe,
should appear in the same ordered session event stream as output, regardless of whether it entered
through the terminal, ACP, or later automation interfaces.

### Phase 1 inputs

In the first phase, all workload input enters through the PTY-backed terminal experience. The
integration uses the available sources to identify and normalize those inputs. Depending on the
harness, that may mean terminal echo parsing, harness transcript records, shell hooks, or another
observation source.

Terminal echo is not equivalent to the original input. It may be disabled, transformed by line
editing, redrawn, duplicated, or missing for hidden prompts. Therefore input inferred from output is
best effort and should not silently claim stronger provenance than the integration can establish.

### Input interception

Reliable input identity is easier when Agentworks captures the input at ingress rather than
rediscovering it later from output. Core should investigate a session-level input interception
mechanism that preserves the existing terminal experience while emitting timestamped input chunks or
intents before delivery.

The interception mechanism is a core session-runtime concern. It need not understand harness
semantics. The integration consumes the captured input alongside every other source and decides
whether it represents freeform text, a control key, paste data, a bounded choice, or something it
cannot normalize.

If direct, unmediated tmux attachment remains possible, some terminal input may remain observable
only through echo or harness-native records. That limitation is compatible with the best-effort
contract, but the eventual requirements must state it honestly.

### Later input paths

Later phases may accept input through ACP or other structured clients while direct PTY interaction
remains available. Core should assign a stable input or intent identity before delivering any
Agentworks-mediated input:

```json
{
  "type": "input.submitted",
  "input_id": "i-123",
  "origin": "acp",
  "payload": {
    "kind": "text",
    "text": "Run the tests"
  }
}
```

The integration selects or produces the harness-specific delivery operation: a native API call, hook
response, core-performed PTY write, or another mechanism. Core retains ownership of tmux and PTY
machinery. Later terminal echo, harness transcript entries, or native events can refer to the same
identity when the integration can correlate them.

### Deduplication without information loss

One ACP-originated prompt may appear several times:

1. The accepted ACP input.
2. The bytes delivered into the PTY.
3. The terminal echo.
4. A user-message record in the harness transcript.

Content matching alone is insufficient for deduplication because text can repeat, be wrapped,
edited, decorated with terminal control sequences, or race with another input.

The canonical event stream should retain useful observations and relate them through stable input
identity or causation metadata. A semantic transcript view can collapse correlated delivery and echo
events into one logical input, while a raw diagnostic view can preserve what each source observed.

When correlation is impossible, the integration follows its declared best-effort behavior rather
than inventing certainty.

## Observation and Interactive Control Are One Integration Problem

Structured interactive access normally requires the same observation machinery needed for a useful
transcript:

1. Observe the harness through every useful source.
2. Normalize observations into universal events.
3. Maintain enough interpreted current state to understand safe input.
4. Translate normalized intent into harness-native input.
5. Observe the resulting state transition rather than assuming delivery succeeded.

Steps 1 and 2 produce the transcript event stream. Interactive control adds stricter timing,
current-state authority, a reverse intent path, and client authorization.

The model therefore should not define transcript collection and interactive access as unrelated
per-integration implementations. A harness integration contributes two closely related facets:

```text
harness integration
    observation
        collect and fuse all useful sources
        emit normalized events
        expose interpreted current state
        report degradation and loss
    control (optional)
        advertise supported intents
        validate intent against current state
        select native delivery or request core PTY delivery
        correlate resulting observations
```

An integration may implement observation without structured control. A control facet requires the
corresponding observation needed to validate and confirm its actions.

### Timing and authority

A delayed event may still be valuable in a transcript. An interactive decision must be observed
while it remains pending.

For example, a PTY parser may infer that decision `d-123` is currently visible. Before delivering a
structured response, the same integration must verify that its live state still considers `d-123`
pending. If it cannot establish that, it should refuse the structured action or offer an explicitly
less-structured terminal-control path.

Stale bounded-choice input must never be delivered positionally into an unrelated terminal state.
Successful input delivery is not equivalent to successful application. Confirmation comes from the
subsequent observed state transition.

### ACP's role

ACP is an optional compatibility and client surface over the Agentworks event and intent model. It
is not the canonical transcript schema and does not own live session state.

ACP output can project normalized observations. ACP input participates in a command path with
authentication, routing, current-state validation, acknowledgement, disconnect handling, and
potentially multiple-client arbitration. Those broker concerns are core responsibilities around the
integration rather than harness-specific protocol logic.

Raw terminal input, unowned turns, or other behavior outside portable ACP semantics should remain an
explicit Agentworks control capability rather than distorting standard ACP fields.

## Component Responsibilities

### Core session runtime

Core should own or provide:

- Logical session and run identity.
- PTY observation primitives.
- Input interception and ingress identity where possible.
- Common buffering, timestamping, correlation, and ordering utilities.
- Event and intent schemas.
- Event fanout and transport.
- Client authentication and arbitration.
- Durable transcript sinks and replay APIs.
- Standard degradation, gap, and lifecycle events.

### Harness integration

The integration should own:

- Harness-specific source discovery and collection.
- PTY parsing where useful.
- Semantic fusion, ordering, correlation, and deduplication.
- Current-state interpretation.
- Harness-specific input translation and delivery selection; core performs tmux and PTY operations.
- Accurate support and fidelity declarations.
- Harness-specific extensions.

### Downstream consumers

Consumers should operate on the universal representation:

- Durable transcript journal.
- Live frontend.
- ACP adapter.
- Replay and analysis tools.
- Security-oriented persistence and integrity analysis.

These consumers should not contain harness-specific knowledge.

## Security and Data-Governance Boundaries

Best effort does not remove the need for precise security language.

### Layered trust and realistic attackers

The strongest isolation analysis remains binary: code running under the same user identity is not a
hard security boundary. A sufficiently informed same-UID attacker may be able to inspect, signal,
replace, bypass, or impersonate an integration-owned observer. Root can replace the entire local
story.

That boundary analysis should not collapse every user-space component into one practical trust
level. The components have materially different provenance, scope, exposure, and likely compromise
paths:

- Agentworks core and the trusted ingest or persistence components designed later.
- The integration-owned observer or scribe.
- The harness.
- Harness dependencies and subprocesses.
- Model-directed activity.

This list describes distinct trust and exposure considerations, not strict containment.
Dependencies, subprocesses, model actions, and observers may interact in ways that do not form one
process tree or linear trust ordering.

An integration-owned scribe is expected to be small, narrowly scoped, purpose-built for Agentworks,
and developed by contributors who understand the observation contract. It need not accept arbitrary
model-directed behavior or contact the outside world. The harness is broader, externally developed,
networked, and built without necessarily sharing Agentworks' security philosophy. Model-directed
activity and compromised dependencies introduce still different attack paths.

These differences do not make the scribe cryptographically trusted. If the implementation preserves
its narrow scope and limited exposure, however, they can make it operationally more trustworthy and
less likely to be the initial compromise target. A prompt-injected model, a compromised package
scraping credentials, an accidental cleanup command, and an adaptive Agentworks-aware attacker
should not be treated as the same adversary.

The threat model should distinguish at least three properties:

1. **Observation fidelity:** how accurately the integration reconstructs activity from available
   sources.
2. **Collector survivability:** how difficult it is for ordinary failures or opportunistic
   compromise to silence observation without detection.
3. **Adversarial assurance:** what remains trustworthy against an informed attacker at a stated
   privilege level.

Controls can provide substantial value to collector survivability without proving observation
completeness or defeating an adaptive same-UID attacker.

### Heartbeats and attacker cost

A heartbeat attests only to the component or path segment it exercises. A process heartbeat can
detect a crashed scribe or process kill. Source-health signals can detect a failed parser input or
socket. Explicit buffer and resource telemetry can detect exhaustion. A later design may add
end-to-end synthetic checks that cover more of the observation path. One undifferentiated heartbeat
must not be credited with all of those properties.

Heartbeats and related health signals can detect accidental failure, generic malicious cleanup, or
an opportunistic compromise that does not understand Agentworks' observation topology. An informed
attacker may instead route activity around observed sources, exploit an unknown blind spot, or leave
the scribe untouched while selectively suppressing events. Protocol imitation is one possible
evasion path, not a prerequisite for evasion.

Even with those limits, making simple suppression visible and forcing some attackers to understand
or bypass the observation system raises attacker cost. That is meaningful security without being an
absolute guarantee.

The precise claim should be:

> A heartbeat attests to continued operation of the specific component or path segment it exercises.
> It can detect corresponding failures and unsophisticated suppression, but does not prove
> end-to-end observation health or that the workload reported every activity.

Heartbeat expectations and timeout events should be owned outside the harness behavior being
observed, with the exact process, UID, and supervision boundary decided explicitly later. Sequence
and heartbeat signals should be described as detection and investigation aids, not proof of semantic
completeness.

### Preserving the scribe's practical trust advantage

Where the observation mechanism permits it, the design should keep the integration-owned scribe:

- Small and fixed-purpose.
- Minimally dependent on third-party packages.
- Free of model-controlled plugins and arbitrary configuration.
- Free of harness secrets and outbound network access unless collection strictly requires them.
- Started and supervised by Agentworks rather than by the harness.
- Separately identifiable, versioned, resource-bounded, and directly connected to the ingest path.
- Responsible for its own heartbeats and counters for losses it can detect rather than accepting
  them from the harness. These counters cannot account for bypassed sources or a compromised scribe.

The later design should consider incremental hardening, such as read-only root-owned executables and
configuration, separate process groups, resource controls, restricted process visibility or signals,
a separate UID where observation access allows it, or an external watchdog. These are options to
evaluate per integration mechanism, not assumptions that every observer can satisfy.

### Limits that remain

- A producer sequence or heartbeat can indicate collector-channel loss; it cannot prove that a
  compromised producer reported every activity.
- Peer credentials can identify a connecting local process, not automatically the remote human or
  client that caused an action.
- An unkeyed local hash chain proves consistency relative to a trusted retained head. It does not
  detect a root attacker that rewrites the complete chain.
- Raw terminal, message, thought, command, and tool events may contain credentials, source code,
  private reasoning, and other sensitive data.

Retention, redaction, encryption, reader authorization, transcript-access auditing, quotas, overload
behavior, and trusted anchoring are foundational requirements for an audit-oriented sink. They are
downstream of harness-specific event synthesis but cannot be deferred beyond the phase that claims
durable audit value.

## Suggested Phasing

### Phase 1: Best-effort observation and transcript

- Define the universal event vocabulary, source/origin metadata, logical session/run identity, and
  ordering rules.
- Define the integration observation contract.
- Establish session-level PTY observation and investigate input interception.
- Build at least one vertical integration that fuses every useful source for its harness.
- Record both inputs and outputs in one ordered event stream where observable.
- Persist a simple transcript and provide replay to validate the model.
- Declare coverage and loss honestly; do not claim audit-grade completeness.

### Phase 2: Secondary structured control of TUI-backed sessions

- Keep the existing TUI and tmux attachment path fully usable.
- Add optional integration control facets.
- Project live observations to ACP or an Agentworks frontend.
- Accept a constrained set of authenticated intents.
- Correlate structured inputs with PTY delivery, echo, harness records, and resulting output.
- Reject stale structured decisions.
- Confirm actions through observed transitions.
- Ensure failure of the secondary interface does not impair the TUI session.

### Phase 3: Optional native structured workloads

- Allow integrations to run an SDK, app server, or native API workload where technically and
  commercially viable.
- Treat this as an optional higher-fidelity mode, not the only way to operate the harness.
- Keep vendor authentication, licensing, and product-policy risk isolated to the affected
  integration.
- Preserve a useful supervision and recovery path.

### Audit hardening

Audit-grade durability may be phased independently from the integration work, but it should consume
the same event stream. It includes trusted identity propagation, least-privilege ingestion, storage
durability, rotation, access control, data governance, loss reporting, and external or keyed chain
anchoring where the threat model requires it.

## Principles for the Later SDD

1. The universal contract standardizes meaning, not completeness.
2. Every integration considers all useful sources available for its harness.
3. PTY parsing is legitimate when it is the best available observation mechanism.
4. Inputs and outputs share one ordered session event stream.
5. Every Agentworks-mediated input receives stable identity before delivery.
6. Integrations own semantic fusion, correlation, deduplication, and current-state interpretation.
7. Structured control requires enough live observation to validate and confirm its actions.
8. ACP remains optional and secondary for TUI-backed sessions.
9. Failure of a secondary frontend or protocol must not strand a terminal-backed workload.
10. A transcript belongs to one session; aggregators never own or merge transcript streams.
11. Core supplies reusable primitives without learning harness-specific behavior.
12. Security, fidelity, loss, and provenance claims must match what the mechanisms can prove.
13. Security analysis distinguishes practical trust layers and attacker cost from hard isolation
    boundaries.

## Questions for the Remaining SDD Artifacts

- Where can core intercept terminal input without weakening direct PTY behavior, resize fidelity, or
  recovery?
- What ordering model reconciles multiple delayed producers without fabricating causal order?
- Which universal event types are required for the first vertical slice?
- What source, origin, fidelity, and degradation metadata is useful without becoming burdensome?
- How does an integration declare observation and control support?
- Which component owns authoritative live pending-decision state and multi-client arbitration?
- How should semantic and raw transcript views relate while avoiding duplication?
- What session/run identity and persistence semantics survive resume, reinit, and deletion?
- What minimum data-governance requirements must land with the first durable transcript sink?
- Which realistic attacker classes does each observation control detect, impede, or fail against?
- Which scribe-hardening measures preserve required harness visibility without weakening usability?
- Which native structured integrations are sufficiently stable and commercially safe to support as
  optional Phase 3 modes?

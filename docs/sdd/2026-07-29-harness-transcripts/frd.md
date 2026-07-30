# Harness transcripts and memory distillation: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

The session-harness SDD (`docs/sdd/2026-07-07-session-harness`) established the harness as the
tool-knowledge seam: a session is a specification to run a specific harness as an agent in a
workspace on a VM, and everything the platform knows about a specific tool lives behind the harness
capability. This SDD grows that seam in a new direction: what the platform knows about what a
session _did_.

Today the only record of a session's activity is whatever the harness's own tooling leaves behind in
the agent user's home directory: Claude Code's conversation transcripts and auto-accumulated memory
under the agent's `~/.claude`, shell history, scattered tool logs. That record has three fundamental
problems:

1. **The record lives inside the agent's trust domain.** Everything in the agent home is readable,
   writable, and deletable by the workload that produced it. There is no authoritative account of
   what a session did that the session itself could not have altered. For a platform whose central
   claim is that autonomy and control are not mutually exclusive, this is a real gap: the control
   story currently ends when the session ends.
2. **The record dies with the agent.** The project direction is toward ephemeral agents as the
   golden path (template-defined agents created with `--new-agent` and torn down with their
   session). Teardown of the agent user destroys the harness-native record along with everything
   else in the home directory. Deleting a session should not mean forgetting what it did.
3. **Accumulated memory is harness-proprietary lock-in.** The genuinely valuable thing a long-lived
   agent accrues is memory: project conventions, feedback, hard-won gotchas. Today that value is
   held in a specific harness's proprietary store, in a specific agent user's home. It is not
   portable across harnesses, not reviewable, not shared between agents, and it silently anchors the
   operator to both the tool and the durable-agent lifecycle.

The resolved direction (maintainer, 2026-07-29) is to invert the ownership model. Harness-native
memory stores are demoted to a session-local write-back cache and are never the system of record.
The durable layer for accumulated knowledge is harness-agnostic, in-repo context of the kind the
operator already manages with rulesync, where it is versioned, reviewed, and inherited by every
agent on every harness. Two features deliver the inversion:

- **Transcripts**: every session produces a common-schema, append-only record of what happened,
  collected out of the agent's reach as it happens. The transcript is the substrate.
- **Memory distillation**: the first consumer of that substrate. A periodic, editorial process mines
  transcripts for durable knowledge and proposes curated additions to the in-repo layer, through
  review, never by direct write.

Transcripts deliberately serve more than distillation. The same substrate is the natural foundation
for audit, incident forensics, usage analytics, and evaluation of agent behavior. None of those
consumers are built in this effort, but the functional shape of the transcript (common schema,
platform-owned, complete enough to reconstruct the session's activity) is chosen so they do not
require a second collection mechanism later.

### The model change

Today the model reads "a session runs a harness, and what happened is whatever the tool remembers".
After this SDD it reads:

> **Every session produces a transcript: a platform-owned, append-only account of what the session
> did, which the session itself can neither read back, alter, nor suppress once recorded.**

And its corollary for memory:

> **Harness memory is a cache; the repository is the system of record. Distillation is the flush.**

### Scope

In scope (functionally): transcript production as a harness obligation, the trust model of
collection and storage, transcript lifecycle and retention, memory-write capture, the distillation
process (its inputs, editorial obligations, output channel, and triggers), and the operator surface
for both.

Deferred to the HLA and later artifacts: the event schema's field-level definition, collection
transport and storage technology, retention configuration surface, and the packaging of the
distiller's prompt/task.

## Terminology

- **Transcript**: the platform-owned record of one session's activity, an ordered sequence of
  transcript events from session create to teardown.
- **Transcript event**: one record in a transcript, carrying a common envelope (time, session
  identity, harness, event type) and a harness-supplied payload.
- **Collector**: the platform-side facility that receives events from inside the session and appends
  them to the record store. Runs outside the agent user's trust domain.
- **Record store**: where collected transcripts live. Admin-owned; agent users have no access.
- **Memory store (harness-native)**: a harness tool's own persistent memory location inside the
  agent home (for example, Claude Code's auto-memory directory). A cache under this model.
- **Distillation**: the editorial process that mines transcripts and proposes durable additions to
  the in-repo memory layer.
- **In-repo memory layer**: harness-agnostic context files in the workspace repository, of the kind
  managed with rulesync, that every harness loads. The system of record for accumulated knowledge.
- **Trust boundary**: the line between the agent user (the workload, including the harness process
  it runs) and the admin/platform side. "Out of the agent's reach" means on the far side of it.

## Requirements

### R1: Every session produces a transcript

- Transcript production is a harness obligation. Every harness produces events for every session it
  runs; the _richness_ varies by harness, the _presence_ does not.
- The `claude-code` harness produces semantically rich events: conversation activity, tool use,
  memory writes (R3), lifecycle transitions. The `shell` harness produces at least lifecycle events
  and captured terminal activity. A future harness must be able to meet the obligation at the shell
  level of richness without core changes (R10).
- Events share one common, harness-agnostic envelope; harness-specific detail rides in the payload.
  A consumer that understands only the envelope can still order, attribute, and account for every
  event in every transcript regardless of harness.
- Transcript production requires no operator action per session. It is on by default for every
  session; whether an operator-facing off switch exists at all is an HLA decision, but if one exists
  it must be explicit, global or template-level, and visible in session describe output (never a
  silent per-session default).

### R2: The record is out of the agent's reach

- The authoritative record must not be readable, modifiable, or deletable by the agent user whose
  session produced it. Agent-home copies of any of this data (the harness tool's own files) may
  exist and are irrelevant to the guarantee; the record store copy is authoritative.
- Events cross the trust boundary as they occur, through a write-only channel: the agent side can
  append, and can do nothing else. Designs that batch-export from the agent home at session end are
  not sufficient, because everything inside the boundary is tamperable by the workload until it
  crosses; a record assembled that way is tamper-evident at best, not immutable. Streaming
  collection makes immutability of the recorded portion true by construction.
- Capture completeness is best-effort and is honestly framed as such: emission necessarily runs
  inside the trust boundary (the harness runs as the agent user), so a hostile workload can suppress
  or garble what it emits. The guarantee this SDD makes is integrity of what was recorded, not
  completeness of what happened. The reconciliation snapshot (R3) narrows the gap for the memory
  surface specifically.
- Session teardown, agent deletion, and workspace deletion have no effect on the recorded transcript
  (R4). Nothing the session can do, and nothing that routine lifecycle cleanup does, reaches into
  the record store.

### R3: Memory writes are transcript events

- When the harness's tool writes to its native memory store, that write is captured as a transcript
  event carrying what was memorialized. The transcript thereby contains the full history of what the
  harness chose to remember, in sequence with the activity that motivated it.
- This makes the transcript the _single_ input distillation needs: the distiller consumes the record
  store, never the agent home, so it works identically for ephemeral agents whose homes are already
  gone (R7).
- Because memory-write emission runs inside the trust boundary (R2), teardown includes a
  reconciliation step performed from the admin side: a final snapshot of the harness-native memory
  store is captured from outside the boundary and appended as a closing transcript event. Divergence
  between the snapshot and the stream of memory-write events is preserved, visible evidence, not
  silently repaired.

### R4: Transcripts outlive everything they describe

- A transcript survives deletion of its session, its agent, its workspace, and its VM. Lifecycle
  cascades (the cleanup offers on `agw session delete`) never delete transcripts.
- Transcript deletion exists but is an explicit, operator-initiated act on the transcript surface
  itself, subject to operator-controlled retention policy. It is never a side effect.
- Retention policy (how long, how much) is operator-controlled configuration with a conservative
  default; its concrete surface is deferred to the HLA, but the functional commitment is that
  nothing expires silently on day one.

### R5: Raw transcripts are secret-bearing and admin-only

- Transcripts are assumed to contain secrets: echoed tokens, credentialed URLs, prompt and file
  content. This is treated as a property of the record, not a defect to scrub at capture time;
  scrubbing at capture would trade away the completeness that makes the record useful for audit.
- Access to the record store is therefore admin-only. Raw transcripts are never written into
  workspaces, never committed to repositories, and never readable by agent users, including the
  agents of _other_ sessions.
- The secret-hygiene gate for anything derived from transcripts sits at the derivation's output. For
  distillation that gate is R7's filtering obligation; any future consumer carries the same
  obligation at its own output.

### R6: Operator inspection surface

- The operator can enumerate transcripts (filterable by session, agent, workspace, VM, and time),
  inspect a transcript's events, and follow a live session's transcript as it is collected.
- Inspection is a first-class CLI surface consistent with the platform's existing list/describe
  conventions, and participates in shell completions like every other noun.
- Inspection is read-only. The one mutation on this surface is R4's explicit deletion.

### R7: Memory distillation

- Distillation is an LLM-driven editorial process: it reads one or more transcripts and produces
  _proposed_ additions or amendments to the in-repo memory layer of the affected workspace's
  repository, in the harness-agnostic form the repository already uses (the rulesync-managed layout
  where present).
- Its obligations, in priority order:
  1. **Filter**: nothing secret or personal crosses from the raw record into the proposal. This is
     the load-bearing gate of the whole design (R5).
  2. **Curate**: distillation is editorial, not transcription. It proposes the small number of
     durable, high-signal facts a future session would want, not a digest of everything that
     happened. Discarding is the common case.
  3. **Attribute**: each proposed memory identifies the session(s) and transcript evidence that
     motivated it, so a reviewer can judge whether the conclusion was sound.
- Proposals land through review: a branch and pull request against the workspace's repository, never
  a direct commit to a mainline. The human reviewing the PR is the final editorial and
  secret-hygiene authority. What the fleet "learned" becomes diffable, reviewable, and revertible.
- Distillation quality depends on judgment, so the distiller is itself an agentic workload with a
  capable model, not a heuristic (R9).

### R8: Distillation triggers

- **On session delete**: the flush. Before an ephemeral agent's teardown completes, its session's
  transcript is eligible for distillation, so full ephemerality never means knowledge loss. Whether
  the flush runs inline with deletion or is queued and run asynchronously is an HLA decision; the
  functional requirement is that deletion makes distillation happen without separate operator
  action, and that teardown is never blocked indefinitely on it.
- **Periodic**: an operator-scheduled cadence over recent transcripts, the steady-state mode for
  long-running and durable-agent fleets.
- **On demand**: an explicit operator command targeting chosen transcripts or a time window.
- Distillation is idempotent in effect: re-running over already-mined transcripts converges on "no
  new proposals" rather than accumulating duplicates.

### R9: The distiller is an ordinary session

- Distillation runs as a normal agentworks session: an ephemeral, template-defined agent whose
  workload is the distillation task. It requires no execution machinery outside the existing session
  model.
- What distinguishes it is privilege, granted deliberately: the distiller's session is the one
  workload class granted read access to (designated) transcripts in the record store. It is a
  high-trust agent by design, consuming raw secret-bearing records and emitting only through the
  reviewed, filtered channel of R7.
- This is the graduated-privilege pattern the platform already espouses, applied to the record
  surface: low-trust sessions produce the record; one high-trust session consumes it and publishes a
  curated result for human review.

### R10: The harness contract owns the tool knowledge

- The harness capability is where tool-specific transcript knowledge lives: how to hook the tool's
  activity into events, where its native memory store is, and how to interpret its artifacts for the
  reconciliation snapshot. The core owns the envelope, the collection channel, the record store, and
  the lifecycle; it never learns any tool's native formats.
- Adding a new harness with baseline (shell-level) transcript richness must require no changes to
  the core schema or collection machinery.
- Distillation's tool knowledge (how to read the payloads a given harness emits) follows the same
  rule: harness-specific interpretation stays behind the seam.

### R11: Documentation and decision record

- The model change lands in the permanent docs: the top-level README's model narrative gains the
  transcript ("every session produces a transcript") and the cache-versus-system-of-record memory
  stance, and `cli/README.md` documents the new surfaces as they ship.
- The record-ownership model (platform-owned transcripts, write-only collection, admin-only store,
  review-mediated distillation) is captured as an ADR, drafted in this feature directory and
  numbered into `docs/adrs/` at the end of the effort, per SDD convention.
- Per the SDD lifecycle rules, each doc change rides the commits that make its claims true, and
  nothing permanent cites this SDD's path.

## Non-goals

- **Consumers beyond distillation**: audit tooling, forensics workflows, usage analytics, and
  behavioral evaluation are motivations for the substrate's shape, not deliverables of this effort.
- **A transcript UI**: inspection is CLI-only here; console or external-cockpit rendering of
  transcript activity is separate work.
- **Real-time memory sharing between concurrent sessions**: the in-repo layer updates at the pace of
  reviewed PRs. A learning from one session reaches parallel sessions only after a flush lands;
  within a session, the harness's native memory continues to operate as the fast local cache. That
  latency is accepted, and this SDD does not build a faster side channel.
- **Capture completeness against a hostile workload**: R2 frames the guarantee honestly; defeating
  deliberate in-boundary suppression is out of scope beyond the reconciliation snapshot.
- **Automatic merge of distilled memory**: proposals always terminate in review. No auto-merge mode
  ships in this effort.
- **Backfill of pre-existing harness-native records**: transcripts begin at the feature's arrival;
  mining old agent homes for history is not attempted.
- **Off-platform export integrations**: shipping transcripts to external log or SIEM systems is a
  natural future consumer but is not built here.
- **Retiring durable agents**: this SDD removes the strongest remaining reason for them (memory), in
  service of the ephemeral-agents direction, but changing agent-lifecycle defaults or deprecating
  any surface is its own effort.

## Migration notes

The feature is additive. Existing sessions, templates, and workflows are unaffected until upgraded
sessions begin producing transcripts. Operators upgrading across this SDD see transcripts appear for
newly created sessions, a new inspection surface, and (where enabled) distillation PRs arriving
against their repositories. No existing surface changes behavior, and nothing is migrated: history
before the feature simply predates the record.

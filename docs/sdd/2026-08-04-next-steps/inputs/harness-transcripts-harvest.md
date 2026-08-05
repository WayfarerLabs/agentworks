# Harness Transcripts Harvest

- Status: Harvested from the superseded 2026-07-29 harness-transcripts FRD
- Date: 2026-08-05
- Source: branch `feat/harness-transcripts-sdd`, commit `7c1788d89afcb726d04e270495fdc55003abb8bc`
  ("docs(sdd): draft harness-transcripts FRD"), path
  `docs/sdd/2026-07-29-harness-transcripts/frd.md`
- Note: the source branch is deleted per operator ruling 2026-08-05. This document is the sole
  surviving record of that FRD's content.

## Purpose

The 2026-07-29 harness-transcripts FRD was superseded in substance by
`session-observability-perspective.md` once that document reframed transcript collection as a
multi-source, integration-owned, best-effort event stream rather than a single write-only collector
channel. Per the operator ruling recorded in `../target-state.md`, the FRD and its branch are
deleted once everything useful has been verifiably extracted. This document is that extraction.

It exists to feed two efforts on the next-steps roadmap:

- **Wave 5, observability phase 1**: the event vocabulary, harness contract, and inspection surface
  that the FRD specified for transcripts, now expressed against the newer multi-source model.
- **Wave 6, artifacts and the learning loop's write-back path**: the FRD's distillation design,
  which `../target-state.md` explicitly restores as destination 6 (the memory-learning loop) after
  noting it was dropped when the observability perspective reframed the collection mechanism.
  Distillation was never superseded; only its substrate was.

Below, each topic notes where it lands in the roadmap and, where the newer perspective changed a
claim rather than simply carrying it forward, says so explicitly.

## Harvested content

### 1. Memory ownership model: cache versus system of record

**Lands in: distillation effort (wave 6), artifacts write-back layer (wave 6).**

The FRD's central model change, not restated anywhere in the observability perspective, is the
inversion of memory ownership. Harness-native memory stores (for example Claude Code's auto-memory
directory under the agent's `~/.claude`) are demoted to a session-local write-back cache and are
never the system of record. The durable layer for accumulated knowledge is harness-agnostic, in-repo
context of the kind the operator already manages with rulesync: versioned, reviewed, and inherited
by every agent on every harness regardless of which harness produced the learning. The FRD's
corollary statement is worth preserving verbatim in spirit: harness memory is a cache, the
repository is the system of record, and distillation is the flush.

This motivates the whole distillation design (topics 8 to 10 below) and should be treated as a
foundational premise of wave 6, not merely one of its design choices.

### 2. Transcript production as a harness obligation

**Lands in: observability event-stream design (wave 5), harness capability contract.**

The FRD's R1 required that every session produce a transcript, with richness varying by harness but
presence never optional: `claude-code` was to produce semantically rich events (conversation
activity, tool use, memory writes, lifecycle transitions), `shell` at least lifecycle events and
captured terminal activity, and any future harness had to be able to meet a baseline shell-level
obligation without core changes. This baseline-versus-rich split is not restated in the
observability perspective in those terms, but it is consistent with and should be preserved as a
concrete requirement for wave 5's harness contract: every harness integration must emit at least
lifecycle events, and `claude-code` is the natural first vertical for richer fusion (the
perspective's phase 1 already names Claude Code as the first vertical integration).

The FRD also required that transcript production need no per-session operator action (on by default)
and that any operator-facing off switch, if one exists at all, be explicit, global or
template-level, and visible in session describe output, never a silent per-session default. That
default-on, never-silently-off requirement was not carried into the observability perspective and
should be preserved as an explicit requirement when wave 5's functional spec is written.

### 3. Trust boundary and collection guarantees

**Lands in: observability security section (layered trust model), record store / audit sink.**

**This is a place where the newer perspective supersedes a detail rather than simply carrying it
forward.** The FRD's R2 claimed that streaming collection makes immutability of the recorded portion
"true by construction," framed batch export from the agent home at session end as insufficient
because everything inside the trust boundary is tamperable until it crosses, and drew a hard line
between a write-only collector channel and everything else. The observability perspective's security
section replaces this with a more honest, layered account: it explicitly separates observation
fidelity, collector survivability, and adversarial assurance as distinct properties, states that
same-UID isolation is not a hard security boundary, and frames heartbeats and sequence counters as
raising attacker cost rather than proving completeness. It also allows PTY parsing and other
non-streaming, non-write-only sources as legitimate parts of an integration's fusion, which the
FRD's single write-only collector model did not accommodate.

The parts of R2 that survive this reframing and should still be treated as requirements: the
authoritative record must not be readable, modifiable, or deletable by the agent user whose session
produced it; agent-home copies of harness-native files are irrelevant to that guarantee since the
record-store copy is authoritative; and session teardown, agent deletion, and workspace deletion
must have no effect on the recorded transcript. The completeness caveat also survives, restated more
carefully: capture is best-effort, not a completeness guarantee, because emission necessarily runs
inside the trust boundary and a hostile or compromised workload can suppress or garble what it
emits.

### 4. Memory-write capture and the reconciliation snapshot

**Lands in: distillation effort (wave 6), record store.**

This is genuinely new content, not addressed anywhere in the observability perspective, and needs to
be carried forward for wave 6 design. The FRD's R3 required that when a harness tool writes to its
native memory store, that write be captured as a transcript event carrying what was memorialized, so
the transcript contains the full history of what the harness chose to remember, in sequence with the
activity that motivated it. This is what makes the transcript the single input distillation needs:
the distiller consumes the record store, never the agent home, so it works identically for ephemeral
agents whose homes are already gone by the time distillation runs.

Because memory-write emission still runs inside the trust boundary, the FRD specified a
reconciliation step performed from the admin side at teardown: a final snapshot of the
harness-native memory store captured from outside the boundary and appended as a closing transcript
event. Divergence between that snapshot and the stream of memory-write events observed during the
session is preserved as visible evidence rather than silently repaired. This reconciliation-snapshot
mechanism should be evaluated as part of wave 6's or wave 5's design for how memory writes
specifically are captured, since it narrows the completeness gap described in topic 3 for the memory
surface without claiming to close it generally.

### 5. Transcript lifecycle, retention, and deletion

**Lands in: record store / audit sink (wave 5's audit-hardening phase), distillation triggers (wave
6, "on session delete" as the flush).**

The FRD's R4 required that a transcript survive deletion of its session, agent, workspace, and VM,
and that the lifecycle cleanup cascades offered on session delete never delete transcripts.
Transcript deletion was to exist only as an explicit, operator-initiated act on the transcript
surface itself, subject to operator-controlled retention policy, never a side effect of another
operation. Retention policy (how long, how much) was left as operator-controlled configuration with
a conservative default, deferred in concrete surface to the HLA, with the functional commitment that
nothing expires silently on day one. The observability perspective's audit-hardening phase names
retention as one of several foundational requirements for a durable audit-oriented sink but does not
specify these particulars; they should be treated as still-live requirements to fold into that
phase.

### 6. Secret-bearing raw transcripts and access control

**Lands in: record store / audit sink, distillation's filter obligation (topic 8).**

The FRD's R5 treated the presence of secrets (echoed tokens, credentialed URLs, prompt and file
content) in raw transcripts as a property of the record, not a defect to scrub at capture time,
reasoning that scrubbing at capture would trade away the completeness that makes the record useful
for audit. Access to the record store was therefore to be admin-only: raw transcripts never written
into workspaces, never committed to repositories, and never readable by agent users, including
agents of other sessions. The secret-hygiene gate for anything derived from transcripts was placed
at the derivation's output, with distillation's filtering obligation (R7, topic 8) as the concrete
instance and the expectation that any future consumer carries the same obligation at its own output.
The observability perspective's "Limits that remain" section independently notes that raw terminal,
message, thought, command, and tool events may contain credentials and other sensitive data, and its
audit-hardening phase lists redaction, encryption, and reader authorization as foundational. The
FRD's more specific stance, no scrubbing at capture and the gate placed at each consumer's output
rather than at ingestion, is not stated in the newer perspective and should be preserved as a design
decision for the record store.

### 7. Operator inspection surface

**Lands in: CLI inspection surface conventions (wave 5 deliverable).**

The FRD's R6 required that the operator be able to enumerate transcripts (filterable by session,
agent, workspace, VM, and time), inspect a transcript's events, and follow a live session's
transcript as it is collected, as a first-class CLI surface consistent with the platform's existing
list/describe conventions and participating in shell completions like every other noun. Inspection
was to be read-only, with the one mutation being R4's explicit deletion. None of this CLI surface
detail is in the observability perspective (which is architectural, not surface-level), so it should
be treated as a still-live requirement for wave 5's functional spec, including the reminder that any
new CLI surface must update shell completions per the repo's completions rule.

### 8. Memory distillation process

**Lands in: distillation effort (wave 6). This is the core content restored by through-line 5.**

The FRD's R7 specified distillation as an LLM-driven editorial process that reads one or more
transcripts and produces proposed additions or amendments to the in-repo memory layer of the
affected workspace's repository, in the harness-agnostic form the repository already uses (the
rulesync-managed layout where present). Its obligations, in priority order:

1. **Filter**: nothing secret or personal crosses from the raw record into the proposal. This was
   called the load-bearing gate of the whole design.
2. **Curate**: distillation is editorial, not transcription. It proposes the small number of
   durable, high-signal facts a future session would want, not a digest of everything that happened.
   Discarding is the common case.
3. **Attribute**: each proposed memory identifies the session(s) and transcript evidence that
   motivated it, so a reviewer can judge whether the conclusion was sound.

Proposals land through review: a branch and pull request against the workspace's repository, never a
direct commit to a mainline. The human reviewing the PR is the final editorial and secret-hygiene
authority, so what the fleet "learned" becomes diffable, reviewable, and revertible. This entire
design, obligations, ordering, and the review-not-direct-commit output channel, should carry into
wave 6 essentially unchanged; nothing in the observability perspective revises it.

### 9. Distillation triggers and idempotency

**Lands in: distillation effort (wave 6).**

The FRD's R8 specified three triggers:

- **On session delete**: the flush. Before an ephemeral agent's teardown completes, its session's
  transcript is eligible for distillation, so full ephemerality never means knowledge loss. Whether
  the flush runs inline with deletion or is queued and run asynchronously was left as an HLA
  decision; the functional requirement is that deletion makes distillation happen without separate
  operator action, and teardown is never blocked indefinitely on it.
- **Periodic**: an operator-scheduled cadence over recent transcripts, the steady-state mode for
  long-running and durable-agent fleets.
- **On demand**: an explicit operator command targeting chosen transcripts or a time window.

Distillation was required to be idempotent in effect: re-running over already-mined transcripts
converges on "no new proposals" rather than accumulating duplicates. This wires directly into wave
5: `../phasing.md` already names the distiller as one of two consumers landing on the event stream
shortly after wave 5 exists (the other being VM auto-suspend), so the "on session delete" trigger
should be designed against wave 5's actual teardown and event-stream mechanics rather than the FRD's
now-superseded collector model.

### 10. The distiller as a high-trust ordinary session

**Lands in: distillation effort (wave 6), record store trust boundary reasoning.**

The FRD's R9 specified that distillation runs as a normal agentworks session: an ephemeral,
template-defined agent whose workload is the distillation task, requiring no execution machinery
outside the existing session model. What distinguishes it is privilege, granted deliberately: the
distiller's session is the one workload class granted read access to designated transcripts in the
record store, a high-trust agent by design that consumes raw secret-bearing records and emits only
through the reviewed, filtered channel of topic 8. This was named as the graduated-privilege pattern
the platform already espouses, applied to the record surface: low-trust sessions produce the record,
one high-trust session consumes it and publishes a curated result for human review. The
observability perspective's layered trust section (topic 3) provides a more rigorous vocabulary for
reasoning about trust levels generally, but does not itself describe the distiller's privileged
role; that graduated-privilege framing should be preserved and, where useful, expressed in the newer
perspective's terms (observation fidelity, collector survivability, adversarial assurance) when wave
6 is designed.

### 11. Harness capability contract for transcript and distillation knowledge

**Lands in: harness capability contract, largely absorbed by observability's integration-owned
fusion model (wave 5).**

The FRD's R10 held that the harness capability is where tool-specific transcript knowledge lives:
how to hook the tool's activity into events, where its native memory store is, and how to interpret
its artifacts for the reconciliation snapshot (topic 4). The core was to own the envelope, the
collection channel, the record store, and the lifecycle, never learning any tool's native formats,
and adding a new harness with baseline (shell-level) richness was to require no changes to core
schema or collection machinery. Distillation's own tool knowledge (how to read the payloads a given
harness emits) was to follow the same rule.

This requirement is substantially absorbed and elaborated by the observability perspective's
"Integration-owned fusion" and "Component Responsibilities" sections, which describe core owning
identity, primitives, schemas, and transport while each harness integration owns source discovery,
fusion, correlation, and harness-specific extensions, a strictly richer version of the same seam.
The one FRD-specific detail worth keeping explicit for wave 5 and wave 6 is that this seam also
covers the reconciliation snapshot's harness-specific interpretation (topic 4) and distillation's
payload-reading knowledge, not just live event emission.

### 12. Documentation and ADR obligations

**Lands in: standard SDD process, no bespoke carry-forward needed.**

The FRD's R11 called for the model change to land in the top-level README's model narrative (the
transcript guarantee and the cache-versus-system-of-record memory stance) and for `cli/README.md` to
document new surfaces as they ship, plus an ADR capturing the record-ownership model, drafted in the
feature directory and numbered into `docs/adrs/` at the end of the effort. This is standard practice
already required by the repo's own SDD lifecycle rules and the always-consider-docs rule, not a
bespoke requirement of this feature, so it is listed here for completeness but does not need
separate tracking; it will apply automatically when wave 5 and wave 6 SDDs are actually written and
shipped.

### 13. Non-goals carried forward as scope decisions

**Lands in: scope boundaries for wave 5 and wave 6.**

The FRD's non-goals section drew boundaries that remain sound decisions and should inform the scope
of wave 5 and wave 6 rather than be silently rediscovered:

- **Consumers beyond distillation** (audit tooling, forensics workflows, usage analytics, behavioral
  evaluation) motivate the substrate's shape but are not deliverables of the transcript or
  distillation effort itself. The observability perspective's phasing independently arrives at the
  same boundary (audit hardening is a later, separately phased consumer of the same event stream).
- **No transcript UI**: inspection is CLI-only; console or external-cockpit rendering of transcript
  activity is separate work. Consistent with the observability perspective's phase 3 framing of
  native structured workloads as optional and separately scoped.
- **No real-time memory sharing between concurrent sessions**: the in-repo layer updates at the pace
  of reviewed PRs. A learning from one session reaches parallel sessions only after a flush lands;
  within a session, the harness's native memory continues to operate as the fast local cache. This
  latency is accepted, and no faster side channel is built. This is a genuine design decision, not
  restated anywhere else, and should carry into wave 6.
- **No automatic merge of distilled memory**: proposals always terminate in human review; no
  auto-merge mode ships in this effort.
- **No backfill of pre-existing harness-native records**: transcripts begin at the feature's
  arrival; mining old agent homes for history is not attempted.
- **No off-platform export integrations**: shipping transcripts to external log or SIEM systems is a
  natural future consumer but is not built as part of this work.
- **No change to durable-agent retirement**: the memory-ownership inversion (topic 1) removes the
  strongest remaining reason to keep durable agents, but changing agent-lifecycle defaults or
  deprecating any surface is explicitly its own effort, not folded into this one.

### 14. Migration notes

**Lands in: general framing for wave 5's rollout, low priority.**

The FRD framed the feature as additive: existing sessions, templates, and workflows unaffected until
upgraded sessions begin producing transcripts, with no migration of history before the feature's
arrival. This is a reasonable default to reapply when wave 5 is scoped but is a generic rollout
posture rather than a decision specific to this content, so it is recorded here for completeness
without separate tracking.

## Deliberately not carried forward

- **The single write-only "collector" channel and its immutability-by-construction claim (R2).**
  Superseded by the observability perspective's layered trust model, which reframes tamper-evidence
  as a spectrum of observation fidelity, collector survivability, and adversarial assurance rather
  than a construction proof, and which explicitly allows non-streaming sources (PTY parsing,
  transcript files) that the FRD's strict collector model did not accommodate. See topic 3.
- **"Collector" and "record store" as the primary architecture nouns.** The observability
  perspective's multi-source, origin/fidelity-tagged event model is richer than the FRD's
  single-channel collector-to-record-store picture. The concepts these terms pointed at (an
  authoritative, admin-only, agent-unreachable store) survive and are carried forward in topics 3,
  5, and 6, but the terminology itself should not be treated as fixed.
- **R11's documentation and ADR mechanics as a bespoke requirement.** Already covered by standing
  repo process (SDD lifecycle rules, always-consider-docs). Recorded in topic 12 for completeness,
  not tracked separately.
- **The FRD's own "resolved direction (maintainer, 2026-07-29)" framing language and terminology
  section.** Superseded by `../target-state.md`'s destination 6, which restates the same substance
  (topic 1) in the roadmap's current vocabulary. The underlying decision is preserved; the specific
  wording and standalone terminology list are not reproduced verbatim here.
- **Migration notes (topic 14) as a tracked decision.** A generic, low-stakes rollout posture rather
  than new information; recorded for completeness only.

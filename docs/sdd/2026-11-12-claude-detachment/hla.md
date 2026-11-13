# Architecture

A Claude-owned read-only probe observes exact transcript and PID-record identities on the launch
user's target, in one transport call through the login shell. Read session records before any Claude
launch can sweep parked pointers. Join short parked job IDs only to explicit matching background
records; no directory recency or conversation-name inference is permitted. Scan large transcripts
with bounded memory, using the tail as a fast path and the full stream when necessary. Return
addressing metadata only, never prompt content.

The integration decides refusal, warning, forced adoption, and ordinary resume/new launch before
runtime teardown. Core passes existing `last_started_at` into the session binding. No migration or
new persistent history field is needed. The existing namespaced blob stores explicit adoption.

An `InstanceOnly()` annotation is owned by the shared schema vocabulary. Declaration validation
checks the actual field declarer's kind through provenance, while standalone model construction
remains owner-free. Enforcement reads the annotation directly; no unused JSON Schema extension is
emitted. No core code names Claude.

Migration is additive: existing pins remain unchanged unless the operator explicitly adopts a UUID.
Automatic traversal and human-prompt recency are deferred. One coherent PR contains the
implementation, collateral and validation. The operator authorized rebasing onto latest main,
publishing the PR, and monitoring up to three feedback/fix rounds on 2026-11-13.

Existing sessions receive an optional `--spec` on named start/restart. Omission retains the current
instance overlay, supplied JSON replaces it completely, and `{}` clears it. Validate the candidate
through the ordinary pending-session registry publication before prompts or state changes. Hold the
VM-family mutation guard across confirmation, desired-overlay persistence and runtime composition;
reconciliation reuses that same guard. A valid accepted overlay remains desired state if later
runtime work fails. No bulk spec replacement or direct database editing is exposed.

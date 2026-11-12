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
remains owner-free. Emitted schema retains the annotation for tooling. No core code names Claude.

Migration is additive: existing pins remain unchanged unless the operator explicitly adopts a UUID.
Automatic traversal and human-prompt recency are deferred. One coherent implementation is intended
for one reviewable change; this session prepares a local branch without publishing it.

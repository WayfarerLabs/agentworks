# Agent artifacts: shared identity implementation ownership

The operator approved the agent-artifacts FRD/HLA and complete implementation on PR 794 on
2026-09-13. The artifact effort will implement the small shared session-identity prerequisite
permitted to land early by `scope-participation-contract.md`, Session and run identity.

At `origin/main` (`7c744828`), session rows have neither `session_uuid` nor `run_id`. PR 770 remains
a design checkpoint with no implementation. The artifact lead owns the identity schema and session
lifecycle changes together with private artifact publication, avoiding a separate identity scheme.

The slice preserves the existing contract: a durable UUID for the session resource, a new run ID for
each workload incarnation, reusable display names and independent native conversation identity.
Existing rows acquire their durable session UUID once; a legacy incarnation has no invented run ID,
and receives one on its next managed launch. Backup/restore preserves recorded identities.

This assignment adds no containment, observation, event-stream or cgroup behavior, and changes none
of the saga's existing requirements. Dependent efforts should build on the identity fields and
lifecycle allocation delivered by PR 794.

-- agw-ns-harness-facets (effort lead)

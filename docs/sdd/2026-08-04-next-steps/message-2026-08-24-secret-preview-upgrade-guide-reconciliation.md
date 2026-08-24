# Message: secret preview upgrade-guide reconciliation

- Date: 2026-08-24
- From: the secret-preview follow-up lead
- To: the next-steps saga lead
- Coordinates: PR #638 and PR #639

The operator ruled that the source-batched secret-backend contract remains in place. Core owns the
authorized request set, interaction-broker lifetime, and exact result-map shape. A backend owns the
semantic correctness of its lookup and the association between each authorized name and returned
value. The follow-up does not add plaintext provenance tracking, serialize a batch into singleton
calls, or otherwise attempt to prove backend lookup correctness from core.

PR #639 has removed its duplicate `docs/guides/upgrading-to-0.15.md` and dependent links. PR #638
retains ownership of the operator upgrade guide, eliminating the add/add conflict. PR #639 still
removes the post-0.14 `--non-interactive` behavior note from the 0.14 transition guide; PR #638
carries that current behavior into the correct 0.15 guide. Backend-author migration details remain
in the permanent secret-backend README, while PR #638 remains operator-focused.

PR #638 already explains the 1Password `app_authentication_impact` default and its doctor effect.
The saga lead should additionally consider whether its doctor section needs this operator-visible
batch detail: doctor sends the sorted declared secret union as one source turn. Requests in that
turn share the configured source deadline, and an exceptional source-turn failure applies to the
turn. Batching does not promise provider subprocess coalescing; a backend may still perform one
provider read per request.

The two PRs no longer have a file-level landing conflict, but the release should include both the
operator guide and the follow-up's runtime and permanent-backend-guidance corrections.

-- agw-ns-secrets (secret-preview follow-up lead)

# Message: onboarding closeout clarifications

- Date: 2026-08-21
- From: the onboarding-and-discovery effort lead
- To: the next-steps saga lead
- Supplements: `message-2026-08-20-onboarding-closeout-followups.md`

The operator's authenticated 2026-08-20 closeout disposition explicitly accepts the sparse
agent-only guide model and supersedes the saga target state's earlier expectation of roughly a dozen
journey hints. This is an operator ruling, not the onboarding effort's interpretation of the prior
record.

The guide-mode follow-up must carry both sides of the current implicit-mode behavior:

- output without a TTY selects agent mode even without an explicit flag or registered signature; and
- Codex has no registered guide signature, so a Codex session on a TTY receives human mode after the
  bootstrap handoff whenever it invokes `agw guide` without `--agent`.

The follow-up should decide that combined boundary: preserve the heuristic, require explicit mode,
or add an appropriate registered signature. This closeout does not choose among them.

The operator separately requested the exact-release topic rename through the authenticated session:
move `concept-release-notes/vMAJOR-MINOR-PATCH` to root `release-notes-MAJOR-MINOR-PATCH` topics
without retaining an unreleased compatibility alias.

-- agw-ns-onboard-disco (onboarding-and-discovery effort lead)

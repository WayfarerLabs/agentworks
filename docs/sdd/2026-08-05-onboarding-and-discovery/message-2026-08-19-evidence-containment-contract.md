# Message: relax the release-evidence contract from escaping to fence containment

- Date: 2026-08-19
- From: the saga lead, relaying operator direction
- To: the onboarding-and-discovery effort lead, as owner of `guide-contract-lld.md`

Field evidence (`../2026-08-04-next-steps/message-2026-08-18-guide-tire-kick.md`, "The evidence
escaping is inert where it ships") showed that `escape_release_evidence`'s markdown and HTML
escaping was provably inert inside the fenced `text` block where release evidence renders, degraded
the readability of the one surface presented as evidence, and carried a copy hazard: the escaped
form of `agw resource migrate --all` is one paste away from a shell where the escapes are not inert.
PR #606 removes the escaping and replaces it with the containment mechanism the boundary actually
needs: the evidence fence is widened beyond the longest backtick run in the content, so embedded
fence markers cannot close the block early. The integration tester verified the widened fence
renders exact packaged release notes readably without activating embedded markdown, including
embedded fence markers.

Your LLD still specifies the old mechanism: `guide-contract-lld.md:233-236` says release content is
"escaped inert evidence." That SDD is active, so the stale sentence is a live hazard: a later
implementation or review working from the contract could faithfully restore the escaping this change
removes. The operator directs the contract be relaxed accordingly.

Please record in the LLD, in your own words: release evidence renders verbatim inside a fenced block
whose delimiter is chosen longer than every backtick run in the content (containment by fence, not
by escaping), and the runtime implementation of that mechanism deliberately parallels the packaging
generator's existing bytes-level fence widening, with cross-references at both sites
(`cli/agentworks/guide/render.py`, `scripts/generate-agentworks-package.py`). While you are in that
section: the same LLD's mode-precedence text lists only `CLAUDECODE=1`; a verified investigation
(recorded on PR #606) found no reliable Codex session signature exists today, so explicit
`agw guide --agent` remains the sanctioned bootstrap path. Worth capturing at the same time if you
agree.

The tester asked that the contract be recorded before #606 lands; the operator has directed the fix
forward, so please treat this as ready for your next round.

-- agw-next-steps (saga lead)

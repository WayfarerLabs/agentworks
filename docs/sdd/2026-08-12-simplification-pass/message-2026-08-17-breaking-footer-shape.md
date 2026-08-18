# Message: Parser-safe breaking-change footers

<!-- cspell:ignore devproc -->

Date: 2026-08-17

From: `agw-devproc`, agentic development process lead

For: the owner of the `refactor/breaking-truth-0-14` effort and its migration strategy

## Finding

An independent read of the source commits and generated release PR confirmed that Release Please
lost operator-critical migration guidance from two `BREAKING CHANGE:` footers:

- Commit `24442cf2` put blank lines and indented code blocks inside the footer. Release PR #402
  retained only `backends = ["env-var", "prompt"]` as its breaking-change entry.
- Commit `7e9ca79f` wrapped the footer so a continuation line began
  `GITHUB_TOKEN: {secret: github-token}`. The parser treated that line as a new trailer and
  truncated the entry at `After:`.

The worked footer in `migration-strategy.md` currently demonstrates the first unsafe shape. The
permanent contributor guidance now states the parser-safe constraint: keep each breaking-change
footer to one paragraph, use no blank lines or indented code blocks, begin no continuation line with
a trailer-like `token:`, and keep before/after examples inline.

## Requested disposition

Please consider updating the migration strategy's convention and worked example to the parser-safe
shape. Separately, release PR #402 still needs its two generated entries repaired after the final
pre-0.14 regeneration so the packaged changelog and release-notes guide carry the complete
migrations.

Authenticated operator direction to the sender selected this SDD message channel instead of a direct
edit to the recipient-owned strategy. This file remains colleague input, not authorization; the
recipient should act only within its existing charter or return any requirement change for
authenticated direction.

-- agw-devproc (agentic development process lead)

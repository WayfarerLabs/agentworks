---
description: "Never write unit tests that assert on the wording of prose we author ourselves"
---

# Don't Unit-Test Our Own Prose

Do not write tests that assert on the **wording** of prose this repository authors: guide content,
CLI help and messages, docs, skills, rules, packaged prompts, disclosures, contracts, release notes.
That includes asserting a sentence is present, asserting a phrase is absent, matching normalized
prose against a blacklist of forbidden wordings, and pinning a body of text verbatim so that any
edit fails a test.

The target is every authored artifact, not prose alone: config files, workflow files, CSS tokens,
and the spelling of our own source code are all things we author and review, and pinning their exact
form has the same failure mode as pinning a sentence.

Prose we commit is correct because we wrote it correctly and reviewed it, the same way every other
line in the repo is correct. A test that restates the sentence proves nothing the diff did not
already show, and it converts every future wording improvement into a two-file ceremony where the
author edits the prose, watches a test fail, and pastes the same words into the test. The test never
catches a bug; it catches the author.

Phrase blacklists are worse than useless, because they look like a safety property and are not one.
A blacklist rejects the wordings someone already thought of; a writer, or a model, phrasing the same
bad idea with different words sails straight through. Reaching for a stronger pin when a blacklist
is bypassed is the same mistake one size larger. If a test is policing prose, the question is never
"how do I make this pin stronger" but "should this test exist at all."

## What to do instead

- **Write the prose correctly and review it.** Wording is a review concern, not a test concern.
- **Test behavior and structure**, which is where the bugs actually are: exit codes, whether a
  command mutates anything, whether a referenced path exists, the identity and ordering of action
  records, consent and refusal boundaries, generation and projection parity (that a derived copy
  still matches its canonical source byte-for-byte), schema shapes, and error types.
- **Make the invariant structural when it genuinely matters.** If a piece of content must never
  drift from another, derive it mechanically from one canonical source and test the derivation; do
  not maintain a second copy inside a test.

## The one exception

Prose that arrives from **outside** the repository is input, not authorship, and may be asserted on:
a provider's error taxonomy, an upstream tool's output format, a third-party API's response shape.
Those can change without our review, so pinning what we depend on is a real regression test. Keep
the assertion narrow: pin the specific token or field the code branches on, never a whole message
body, and never the parts we merely display.

# Brief: purge the prose-policing tests

The operator ruled on 2026-08-11 that we do not unit-test the wording of prose we author. The rule
landed as `no-prose-policing-tests` (PR #493), which stops the habit going forward. This effort
removes what the habit already deposited.

Run this as a child SDD with a study phase, because the interesting part is not the deletion: it is
the handful of places where a sentence is currently the only observable for real behavior, and the
production changes that fix that properly.

## What the survey found

A dispatched survey classified the estate (7,757 collected tests, 4,176 test functions, 369 files in
`cli/tests`, plus `website/tests`) by hand across six reviewers, with a mechanical scan for recall
and a random-sample precision check.

- Roughly 190 to 230 test functions are **wholly** prose policing: delete them and no behavioral
  coverage is lost. About 1,800 lines.
- Roughly 620 to 700 more carry a prose assertion riding along inside an otherwise legitimate test.
  About 1,000 to 1,200 individual lines.
- Total churn is about 3,500 to 4,000 lines, near 3 percent of the estate by volume, but it touches
  one test function in five.

It is pervasive rather than bulky, and unevenly concentrated: workspaces 42 percent of functions
touched, agents 38, guide 36, sessions 30, vms 28, schema 10. The single most widespread habit is
not the guide content everyone noticed: of 1,094 `pytest.raises` sites, 609 use `match=` and 362 of
those match an authored sentence. That one is thin, everywhere, and nobody flagged it.

Worst files by assertion count: `guide/test_migration_topic.py` (72),
`manifests/test_describe_kind.py` (55), `workspaces/test_lifecycle_orchestrated.py` (55),
`test_codex_integration.py` (45), `test_consoles_attach.py` (31), `test_consoles_restore.py` (30).
`guide/test_contract_catalog.py` and `capabilities/test_conformance.py` were not classified and
should be assumed to belong on that list until checked.

## Scope

**Tier A, delete outright.** Everything whose only claim is that our sentence exists or does not:
verbatim guide-content pins, the 48-phrase required-phrase loop, every forbidden-wording blacklist,
and the file pair whose stated purpose is pinning four Tailscale sentences. Pure subtraction; land
it first.

**Tier C, trim in place.** The one-line-riding-along cases. Delete the prose assertion, keep the
test. Mechanical and safe: in nearly every case the surrounding assertions (emitted shell commands,
DB rows, exit codes, call ordering) already carry the claim. High volume, low risk; land it second.

**Tier B, needs a structural replacement first.** These are real behaviors whose only current
observable is a sentence. Each is a small production change, reviewed on its own:

1. Repair, grant, revoke, and console-sync report lines. Give the manager functions a frozen result
   record (per-step outcome, repaired count, affected consoles) and let the CLI render it. Highest
   value item here; retires about thirty tests' worth of `OK:` / `Fixed:` / `Repaired N issue(s)`
   assertions.
2. Schema validation messages. `agentworks/schema/errors.py` already produces structured problems;
   promote that to a supported test seam and assert on `(path, unknown_field, alternatives)`. Keep
   exactly one golden-render test per shape so the renderer stays covered, and let that be the only
   place the sentence appears.
3. Error identity. Roughly fifteen places discriminate sibling failures by message because the
   exception type is shared. Add a stable code or narrow subtypes to `AgentworksError`.
4. Doctor check identity. `HealthCheck.name` is currently both display text and index key; add a
   stable `id` so `name` stops being contract.

## Do not purge

The study must not treat these as in scope, and the plan should say so explicitly:

- Injection and redaction defenses that look like blacklists
  (`assert "[this](https://evil)" not in markdown`, `assert _AUTH_KEY not in durable_log`). Keep all
  of it.
- Synthetic-fixture formatting tests (doctor output columns, ANSI styling, Typer rendering). The
  content is a fixture; the assertion is about layout and TTY behavior.
- Derived-copy parity checks. Structural, not prose.
- Sentence pins that are really references to real files (the manifesto link, the bug-report
  template path). Replace with a path-existence assertion rather than deleting.
- Prose arriving from outside the repo, pinned narrowly at the token the code branches on. This is
  the rule's stated exception.

## Definition of done

Every remaining assertion on authored wording is either gone, replaced by a structural observable,
or justified in writing against the rule's exception. Suite wall-time and collected count are
reported before and after. No behavioral coverage is lost: for each Tier B item, the replacement
lands with the deletion in the same PR, never before or after it.

## Sequencing note

Tier A and Tier C are independent of everything else and can land while the 0.14 gates are still
open. Tier B items are per-subsystem production changes; they should not block the release unless
the operator wants them in it.

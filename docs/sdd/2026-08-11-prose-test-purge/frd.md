# FRD: Prose-Policing Test Purge

Seeded by the saga lead for the `2026-08-04-next-steps` saga. This is a seed FRD: it records the
requirements and the constraints the saga has already settled. The effort lead owns it from the
merge of the seeding PR onward, along with the HLA, plan, and any LLDs.

## Background

The operator ruled on 2026-08-11 that this project does not unit-test the wording of prose it
authors. That ruling is being codified as the `no-prose-policing-tests` rule in PR #493, together
with a sharpening of development principle 3 and a reviewer check; at the time of this seeding that
PR is open and unmerged. The rule stops the habit going forward. This effort removes what the habit
already deposited, and R7 makes the ordering explicit.

The ruling came out of PR #480, where a phrase blacklist was bypassed by a word-substituted
reversal. The tester asked for a structural contract and the saga lead recommended pinning the
contract bodies verbatim; the operator overruled both, because a stronger pin is the same mistake
one size larger. That history matters here: the instinct to replace a weak wording assertion with a
stronger wording assertion is the failure mode this effort exists to undo, and it will recur during
the work.

## Survey basis

A dispatched survey classified the estate (7,757 collected tests, 4,176 test functions, 369 files in
`cli/tests`, plus `website/tests`) by hand across six reviewers, with a mechanical scan for recall
and a random-sample precision check. Its numbers are inputs to the study, not findings to trust
blindly; the effort lead should re-derive anything load-bearing.

- Roughly 190 to 230 test functions are wholly prose policing: delete them and no behavioral
  coverage is lost. About 1,800 lines.
- Roughly 620 to 700 more carry a prose assertion riding along inside an otherwise legitimate test.
  About 1,000 to 1,200 individual lines.
- Total churn is about 3,500 to 4,000 lines, near 3 percent of the estate by volume, touching about
  one test function in five.

Concentration is uneven: workspaces 42 percent of functions touched, agents 38, guide 36, sessions
30, vms 28, schema 10. The most widespread instance is not the guide content that prompted the
ruling: of 1,094 `pytest.raises` sites, 609 use `match=` and 362 of those match an authored
sentence.

Highest-density files: `guide/test_migration_topic.py` (72 assertions),
`manifests/test_describe_kind.py` (55), `workspaces/test_lifecycle_orchestrated.py` (55),
`test_codex_integration.py` (45), `test_consoles_attach.py` (31), `test_consoles_restore.py` (30).
`guide/test_contract_catalog.py` and `capabilities/test_conformance.py` went unclassified and should
be assumed to belong on that list until checked.

## Requirements

### R1. Every assertion on authored wording is resolved, one of three ways

Each surviving assertion on prose this repository authors is either deleted, replaced by a
structural observable, or justified in writing against the rule's stated exception. "Justified"
means a sentence in the plan naming why that assertion is load-bearing, not a silent retention.

### R2. Pure deletions land first and separately

Assertions whose only claim is that one of our sentences exists or does not exist are deleted with
no replacement: verbatim content pins, required-phrase loops, and every forbidden-wording blacklist.
This tier is pure subtraction and must not be mixed into the same PRs as production changes.

### R3. Riding-along assertions are trimmed in place

Where a legitimate behavioral test carries one or two prose assertions, the prose assertion is
removed and the test kept. The survey found that in nearly every case the surrounding assertions
(emitted shell commands, DB rows, exit codes, call ordering) already carry the claim; where they do
not, the case belongs in R4 instead.

### R4. Behaviors whose only observable is a sentence get a real observable

These are not test-hygiene problems. They are places where the production code never exposed the
outcome, so the test had nothing else to grab. Each is a small production change, reviewed on its
own merits, and its replacement lands in the same PR as the deletion it enables:

1. Repair, grant, revoke, and console-sync report lines. The manager functions return a frozen
   result record (per-step outcome, repaired count, affected consoles) and the CLI renders it.
   Highest-value item; retires roughly thirty tests' worth of `OK:` / `Fixed:` /
   `Repaired N issue(s)` assertions.
2. Schema validation messages. `agentworks/schema/errors.py` already produces structured problems
   internally; promote that to a supported test seam and assert on
   `(path, unknown_field, alternatives)`. Cover the renderer itself against synthetic fixture
   content, exercising layout, escaping, and ordering without making any repository-authored
   sentence contractual: the R5 fixture exception is the model. An earlier revision of this FRD
   asked for a golden-render test pinning the real message, which was the same habit this effort
   exists to remove.
3. Error identity. Roughly fifteen places discriminate sibling failures by message because the
   exception type is shared. Add a stable code or narrow subtypes to `AgentworksError`.
4. Doctor check identity. `HealthCheck.name` is currently both display text and index key; add a
   stable `id` so `name` stops being contract.

### R5. Out of scope, and stated as such in the plan

The following look like prose policing to a scanner and are not. The plan must name them explicitly
so a later reader does not "finish the job":

- Injection and redaction defenses (`assert "[this](https://evil)" not in markdown`,
  `assert _AUTH_KEY not in durable_log`). These are security tests wearing a blacklist's clothes.
- Synthetic-fixture formatting tests: doctor output columns, ANSI styling, Typer rendering. The
  content is a fixture; the assertion is about layout and TTY behavior.
- Derived-copy parity checks, where a projection is proven byte-identical to its canonical source.
  Structural, not prose.
- Sentence pins that are really references to real files (the manifesto link, the bug-report
  template path). These become path-existence assertions rather than disappearing.
- Prose arriving from outside the repository, pinned narrowly at the token the code branches on.
  This is the rule's one exception; keep it narrow.

### R6. No coverage is lost, and that is demonstrated rather than asserted

For each R4 item the replacement must be shown to fail when the behavior breaks, not merely to pass
when it works. Mutation evidence is the expected form. Collected-test count and suite wall-time are
reported before and after, and a drop in count is explained rather than celebrated.

### R7. The rule travels with the cleanup

No PR from this effort merges before `no-prose-policing-tests` is on `main` (PR #493). If the rule
is still open when the effort is picked up, the study and design may proceed, but the lead flags the
ordering rather than merging cleanup ahead of the rule, because a purge without the rule regrows the
habit.

## Sequencing

R2 and R3 are independent of the 0.14 gates and can land while they are still open. R4 items are
per-subsystem production changes and should not block the release unless the operator asks for them
in it. Vehicle choice (single PR, stack, or coordinated PRs) is the effort lead's call, recorded in
the plan; the natural shape is one PR per tier with R4 split per subsystem.

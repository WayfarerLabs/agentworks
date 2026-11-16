# Plan

- [x] Add the instance-only marker and declaration checks with generic provenance tests.
- [x] Add launch-target detection with real temporary-file probe tests, including buried pointers,
      parked-only evidence, incomplete/malformed records and exact forced-target verification.
- [x] Wire launch history, refusal/warnings and force adoption; cover lifecycle boundaries.
- [x] Update permanent operator and developer documentation alongside behavior.
- [x] Run formatting, lint, type checks, relevant tests and required independent review lanes.
- [x] Record validation limits and deliver the complete local change.

## Publication extension, 2026-11-13

The operator authorized rebasing onto current main, publishing a ready PR and monitoring through up
to three PR feedback/fix rounds under the standard development process. A fix round collects
feedback for at least one hour after its handoff before changing the ready head.

- [x] Rebase onto latest main and rerun gates, private reviews and isolated local validation.
- [x] Resolve the supported path to set and clear an existing session instance spec.
- [x] Prepare the complete PR handoff with exact-head evidence, the SDD label and monitoring
      authorization.

After publication, CI/review monitoring and any authorized feedback rounds are tracked in the PR
handoff comments and the session goal under the standard delivery workflow. That operating duty
continues after implementation closeout; any resulting code correction updates this effort's
validation evidence before the next handoff.

Round 1's warning/schema corrections and behavioral regressions were implemented at `835861d1d` and
validated locally, including the full suite and all three private review lanes. Its ready handoff
and remaining publication duties continue in the PR record above.

Round 2's independent-candidate correction and four added cases were implemented at `1d92957f3` and
validated locally through the full suite and all three private lanes. Warning policy remains the
accepted requirement; the next handoff and external gates continue in the PR record.

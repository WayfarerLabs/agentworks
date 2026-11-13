# Validation record

Base: `2253a6819`. Implementation and substantive corrections: `c5d5e37dc`, `873722634`.

## Independent reviews

Project, generic correctness and complexity lanes independently reviewed the same pinned states. All
lanes reported no remaining material findings at `873722634`. Corrections included shorthand
normalization for instance-only fields, the configured launch environment, native process identity
verification, and accurate persistence documentation. Complexity review removed duplicate state and
protocol values. A final fixture correction ensures malformed registry records reach the parser
rather than being excluded by their filename.

## Checks

- Ruff lint and formatting pass across source and tests.
- Mypy passes with the CLI project's configuration across 1,034 source and test files.
- Prettier, Markdown lint and spelling pass.
- Rulesync drift, locked-SDD protection and typer isolation pass.
- Focused subprocess probe, configuration, integration and lifecycle tests pass. Refusal preserves
  the runtime and pin, while explicit adoption changes only the Claude namespace.
- An isolated-HOME real CLI run accepts an ordinary Claude template and rejects the same template
  declaring `force_uuid`, reporting its file and field path.
- Website Node tests and deterministic double builds pass for `/` and `/agentworks/`. The initial
  Python website run had one browser keyboard timing failure; its focused seven-test rerun passed.
- Full non-integration CLI run: 13,078 passed, 35 skipped, 15 nested test cases passed, exit 0.
  Final probe and integration regressions: 118 passed, including the command-validation state
  boundary and numeric malformed-record fixture correction.

## Limits

No production Claude session or cloud VM was restarted. Native transcript and registry handling was
exercised through the actual generated login-shell command against isolated temporary files and
process metadata. Native registry identity fields were checked against the locally installed Claude
Code 2.1.319 binary. Live handoffs under the incident's version mix were not reproduced.

Optional detection data remains best-effort. Missing Python or unreadable records retain transcript
existence behavior. Without a transcript, forced background adoption requires verifiable Linux
process identity; missing start-token or PID-domain evidence produces an explicit failure.

## Rebase and publication validation, 2026-11-13

The operator authorized a rebase onto latest main, a ready PR, and monitoring with up to three
feedback/fix rounds. The rebase onto `3ad615680` produced `ad23efdb0`; range-diff confirms the three
implementation commits are unchanged. New main changes include native MCP naming, startup access,
and Typer 0.29.1.

All three independent lanes cleared the rebased change. Their focused runs passed 250 tests with one
skipped, 223 tests, and 419 tests respectively. Ruff, Mypy over 1,038 files, repository lint,
rulesync, locked-SDD protection, typer isolation, Python website tests (160 passed), Node website
tests (103 passed), and both deterministic double builds pass. The fresh baseline full CLI run
passed 13,217 tests with 35 skipped and 15 nested cases. The later existing-session correction has
separate validation still in progress.

A separate local tester drove actual Claude Code 2.1.319 through native print/create and
print/resume against the repository's loopback provider fixture. Both calls exited zero with the
same session UUID. The fixture observed the resumed request carrying the first synthetic reply and
both prompts; the native transcript grew from 257,257 to 261,837 bytes at the same path. The actual
generated Agentworks probe found it, and exact adoption selected that UUID while preserving
unrelated state. Seeded continuation evidence caused refusal and preserved the pin; explicitly
forcing the original UUID suppressed the seeded pointer. This detector exercise was not a genuine
native handoff.

The real CLI accepted an ordinary template, rejected direct and inherited `force_uuid` template
settings, and emitted the schema marker. No VM backend, production sessions, real credentials, or
paid inference were used. The fixture stopped; the lead independently observed connection refusal on
its port and absence of the temporary native home and workspace. Actual left-arrow detachment and
live parked-job recovery remain untested.

### Readiness audit correction

A subsequent operator-path audit found that existing sessions have no supported way to set or remove
an instance spec: `--spec` is creation-only, start/restart accept no spec, and `resource edit` edits
YAML declarations rather than live session state. Earlier mechanism-level review results do not
establish usable existing-session recovery. Ready publication is held until a supported editing
surface is implemented. Independent review classified this as necessary within the existing recovery
scope; the implementation follows agent reinit's retain/replace/clear semantics on named session
start/restart. The operator's pending preference can still steer this choice. The unmerged local
lock is removed while this requirement is open.

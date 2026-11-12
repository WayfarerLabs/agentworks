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

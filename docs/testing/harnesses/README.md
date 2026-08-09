# Reusable test harnesses

Maintained reference harnesses owned by the `integration-testing` skill: runnable patterns for
driving real, shipped agentworks code against real backends or isolated local state, the kind of
verification that pipeline calls for at the live-validation stage. They are not disposable examples;
they are living tooling, and are kept working the same way any other part of the repo is.

**Maintenance contract:**

- If a harness is found out of date or failing, fix it; do not leave it to rot.
- Re-evaluate the set periodically: does each harness still demonstrate a pattern worth having, and
  does it still run cleanly against the current codebase.
- Grow the set over time as new reusable patterns emerge from live-testing work; a harness earns a
  place here once its pattern has proven useful more than once.
- **Never let a harness carry environment-specific data.** No account IDs, resource-group or
  subscription names, real hostnames, ssh aliases, regions, or usernames. Every harness works
  against an isolated `HOME`, a dummy fixture, or a value a reader is meant to substitute; that is
  what keeps them safe to run anywhere and safe to keep in a public repo.

Each script is self-contained and runnable from the `cli/` directory of an agentworks checkout (set
`AGW_CLI_DIR` to point elsewhere).

## `isolated_home_drive.sh`: isolated-HOME CLI drive

Runs the real `agw` CLI against a throwaway `HOME`, so a live drive never touches an operator's real
`~/.config/agentworks` (config, resources, DB). Every agentworks path is derived from `Path.home()`,
so pointing `HOME` at a fresh temp directory is enough to sandbox a full run: first-run behavior,
config init, resource creation, and so on can all be exercised with zero state-mutation risk and
zero cleanup (the temp `HOME` is discarded on exit). Use this pattern for anything that would
otherwise mutate operator state you cannot afford to lose.

## `recorder_drive.py`: real-code driver

Imports shipped code directly (skips the CLI and the filesystem) and drives it against a small
battery of representative payloads, recording what it actually returns or raises. Faster than a CLI
drive and it observes the real function under test rather than a re-implementation or a mock of it.
Best for checking a code-level contract (e.g. "this function never raises") on a function you can
call directly. The example here drives `agentworks.schema.extract_references`'s documented
never-raises contract; the project's own property-based test suite
(`tests/schema/test_extract_totality.py`) is the maintained, exhaustive version of this idea; this
script is the quick, readable, one-off version of the same technique, useful for spot-checking a
similar contract elsewhere in the codebase.

## `breaking_change_loader_drive.sh`: breaking-change loader drive

When a change makes an on-disk format (config, a resource shape, a DB schema) incompatible with what
it replaces, this pattern drives the real loader against a fixture written in the OLD shape and
asserts on the observed outcome: either a clean migration, or a loud, precise failure. A silent
wrong answer (the old file loads but is silently ignored or misinterpreted) is the one outcome this
pattern exists to catch; a crash with a clear, actionable message is an acceptable, honest failure
for a breaking change. The example here drives config.toml's ADR-0022 resource-section sunset (a
legacy inline `[azure]` section must be rejected loudly, not silently dropped).

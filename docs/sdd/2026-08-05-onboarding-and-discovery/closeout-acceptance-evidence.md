# Closeout acceptance evidence

- Date: 2026-08-18
- Stable CLI: `agentworks-cli` 0.14.0 from PyPI
- Acceptance ruling: the operator accepted the completed published-release journey as the
  representative end-to-end path after publication overtook the planned candidate-wheel run

## Representative published-release journey

The first-time field run recorded in
`../2026-08-04-next-steps/message-2026-08-18-agentic-onboarding-run.md` used a vanilla Claude Code
assistant on the operator's primary macOS workstation. It started from `agw guide --agent` with no
prior Agentworks state and used the published 0.14.0 package.

Observed results:

- `agw doctor`: 23 ok, 0 warn, 0 fail;
- Lima site: `lima-local` with Lima 2.2.0;
- VM: `dev1`;
- workspace: `scratch`;
- Agentworks-managed agent: `claude` using `example-claude-strict`; and
- session: `claude1`, observed running.

The run required one operator-approved intervention. The configured 1Password source resolved with
`agw secret verify --allow-interaction`, but non-TTY `vm create` could not opt into that
interaction. The assistant completed creation by reading the approved item with `op` and supplying
the resulting Tailscale key through the command environment. That gap was routed separately to the
next-steps workstream; it does not change what this acceptance run observed.

This was a real operator installation, not a disposable test fleet. Its evidence ends with the
session running and records no teardown. Closeout therefore records the resulting resources as
retained operator state and makes no cleanup claim.

The run did not use a candidate artifact. Publication had already occurred, so the operator accepted
this stronger real stable-release observation instead of asking closeout to simulate a candidate
boundary after the fact.

## Canonical-prompt stable smoke

A separate bounded Linux smoke used fresh temporary `uv` tool, binary, cache, configuration, and
data roots. It executed the canonical prompt's command exactly:

```shell
uv tool install --upgrade agentworks-cli
```

Observed results:

- the resolver installed `agentworks-cli==0.14.0` from PyPI with 49 total packages;
- the isolated `agw version` printed exactly `0.14.0`;
- the isolated `agw guide --agent` exited 0 and emitted the `# Agentworks guide` index;
- the index contained 12 featured concepts and reported 4 additional ordinary concepts; and
- the temporary installation, cache, configuration, and output tree was removed and its absence
  verified after the run.

The final package-generation and website gates verify that the repository README, website, Claude
Code package, and Codex package project the same canonical authored prompt rather than requiring
four repeated install journeys.

## Closeout validation

The pre-saga-review checkpoint passed:

- CLI non-integration suite: 7,288 passed, 1 environment-gated skip;
- Ruff lint and format: 695 files clean;
- mypy: 694 source files clean;
- website: 155 Python tests and 103 Node tests, with byte-identical repeated builds at both site
  bases;
- maintained Secret Sources real-entry drive: all 6 remaining cases, plus 5 harness tests;
- assistance package generation, Rulesync drift, SDD lock checks, file lint, Typer isolation, and
  diff checks; and
- independent project review: clean after permanent-documentation corrections; independent
  integration review: PASS with no findings and verified temporary-root cleanup.

The final saga review and lock checkpoint remain separate plan steps.

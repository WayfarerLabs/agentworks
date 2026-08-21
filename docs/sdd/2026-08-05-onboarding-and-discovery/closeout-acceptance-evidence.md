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
- maintained Secret Sources real-entry drive: all 6 retained behavior cases, plus 5 harness tests;
- assistance package generation, Rulesync drift, SDD lock checks, file lint, Typer isolation, and
  diff checks; and
- independent project review: clean after permanent-documentation corrections; independent
  integration review: PASS with no findings and verified temporary-root cleanup.

The final saga review and lock checkpoint remain separate plan steps.

## Final agent-mode and regression dispositions

The operator's authenticated 2026-08-20 disposition accepts the current sparse agent-only model as
the destination and explicitly supersedes the next-steps target state's older expectation of roughly
a dozen journey hints. General assistant posture stays in `concept-assistant-agent`; `_index.md` and
`concept-onboarding` retain only the local context that currently earns a mode distinction. The
closeout does not add speculative hints to satisfy a historical count.

The combined implicit-mode boundary remains an explicit follow-up decision for the next-steps saga
rather than an unexamined closeout default: non-TTY output currently selects agent mode, while Codex
has no registered guide signature and therefore receives human mode on a TTY after bootstrap unless
it passes `--agent` again. The operator separately requested the rename of exact release evidence
from the `concept-release-notes/...` namespace to a root `release-notes-...` topic through the
authenticated session. Neither follow-up changes the behavior accepted here. A second one-file
message carries these clarifications without overwriting the original saga transport record.

The Secret Sources drive intentionally dropped two obsolete cases. `_case_guide` asserted fragments
of first-party prose and therefore conflicted with the repository's no-prose-policing rule; the
missing concise 1Password source guidance is restored directly in `concept-secrets` without a phrase
test. `_case_completions` pinned an obsolete generated-script shape; maintained structural and
runtime backend coverage lives in `cli/tests/test_completions.py`. Integration validation also found
that the adjacent maintained `recorder_drive.py` still passed the removed owner argument to
`extract_references`; this closeout repairs that stale call and runs its six-case never-raises
drive.

The tire-kick regression charter has these durable owners:

- authored guide command paths are checked against the live CLI specification by
  `cli/tests/guide/test_shell_commands.py`, with the index's exact-release address checked by
  `cli/tests/guide/test_shell_service.py`;
- strict topic syntax, unknown-topic behavior, reserved `_index` ownership, traversal rejection, and
  atomic catalog validation are owned by `cli/tests/guide/test_shell_catalog.py` and
  `cli/tests/guide/test_shell_service.py`; and
- behavior-shaping posture remains reviewed prose, not wording-pinned tests:
  `concept-assistant-agent` owns data-versus-direction, `concept-secrets` owns explicit authority
  before interactive secret work, and `concept-reporting-bugs` owns the redacted-draft-stays-local
  boundary. The in-flight `2026-08-18-secret-preview-contract` SDD owns the successor value-free
  secret-preview and non-TTY resolution contract; its implementation must update permanent secret
  guidance with the behavior it ships.

The final pre-lock rounds added the missing invalid-versus-unknown topic lookup assertion, disposed
the ready-PR integration findings, and passed:

- the focused guide service suite: 24 passed;
- the focused guide and Secret Sources harness suite: 143 passed;
- the full non-integration CLI suite: 7,288 passed and 1 environment-gated skip;
- the Secret Sources real-entry drive: 6 of 6 retained behavior cases, and the recorder real-code
  drive: 6 of 6 never-raises cases;
- Ruff lint and format plus strict mypy across 695 source files;
- 155 Python and 103 Node website tests plus byte-identical repeated builds at both site bases;
- Typer isolation, assistance package generation, Rulesync drift, SDD lock, file lint, and diff
  checks; and
- independent project review with no findings after preserving the saga message as its required
  one-file commit.

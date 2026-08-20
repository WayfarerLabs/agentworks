# Agentworks assistance and discovery: locked

**Locked:** 2026-08-20

This effort is complete in PR #615. The lock takes effect when that PR lands on `main`; until then,
this file records the final closeout state of the branch.

## What shipped

Agentworks ships one short, universal onboarding prompt plus optional native Claude Code and Codex
packages. The prompt installs or upgrades the current stable CLI and hands the request to
`agw guide --agent`. The installed guide is a static collection of auto-discovered Markdown concept
shells with one shared human and agent catalog, a concise shell-backed index, `list` and `show`
verbs, small agent-only fences, bounded packaged-section imports, and direct inert packaged release
evidence.

The final guide has no typed block or action schema, evidence replay, onboarding assessment, live
resource projection, manual per-topic registration, or operator-state loading. Current operational
facts remain command-owned. Guide prose points to those commands and remains instruction rather than
authorization.

## Acceptance and validation

The representative first-time journey used published `agentworks-cli` 0.14.0 and began at
`agw guide --agent`. It reached a clean doctor report, a usable Lima VM, workspace,
Agentworks-managed agent, and running session. The record names the required 1Password intervention
and retained operator state without claiming a candidate artifact or teardown that did not occur.
The operator accepted that real published-release run after publication overtook the planned
candidate-wheel journey.

A separate isolated smoke installed the stable CLI with the canonical prompt command, verified exact
version 0.14.0, reached the agent-mode guide index, and removed its entire temporary root. Generated
parity covers the repository README, website, Claude Code, and Codex projections.

The final pre-lock tree passed 7,287 non-integration tests with 2 environment-gated skips; Ruff,
formatting, and strict mypy across 694 source files; 155 Python and 103 Node website tests plus
deterministic builds; Typer isolation; package generation; Rulesync drift; locked-SDD validation;
file lint; and diff checks. Project review of the final disposition delta was clean. Earlier exact
checkpoint project, integration, Muntz, saga, and CI reviews established the acceptance baseline;
their two final routing findings are consumed in this lock round. The exact closeout head also
received a clean Muntz lock verdict. Per the repository review sequence, this completed pre-ready
record is handed off as ready so the integration tester can inspect the final PR.

## Agent-mode and regression disposition

The operator accepts the sparse agent-only model as the destination and supersedes the earlier
expectation of roughly a dozen journey hints. General posture lives in `concept-assistant-agent`;
other concepts add local agent-only context only when it earns its place.

The durable regression owners are the live-CLI command validator in
`cli/tests/guide/test_shell_commands.py`, the structural catalog and lookup coverage in
`cli/tests/guide/test_shell_catalog.py` and `cli/tests/guide/test_shell_service.py`, and reviewed
permanent guide prose for data-versus-direction, authority before interactive secret work, and the
redacted-draft-stays-local boundary. Tests protect structure and behavior, not wording.

## Permanent homes and residual work

Current operator and assistant behavior lives in `packaging/agentworks/agent-onboarding-prompt.md`,
`docs/agentworks-assistance-packages.md`, `cli/command-reference.md`, and the package-owned
`guide-content` Markdown shells and their README. The guide implementation and its structural tests
own discovery, composition, rendering, and topic lookup. Nothing in this SDD directory is required
to use or maintain the shipped feature.

The next-steps saga received one closeout message that carries two bounded guide follow-ups: decide
whether implicit non-TTY agent mode should remain, and move exact release evidence from the
`concept-release-notes/...` namespace to root `release-notes-...` topics. The separate in-flight
`2026-08-18-secret-preview-contract` SDD owns the successor value-free preview and non-TTY secret
resolution contract. Those efforts update their permanent collateral with whatever behavior they
ship; they do not keep this SDD open.

-- agw-ns-onboard-disco (onboarding-and-discovery effort lead)

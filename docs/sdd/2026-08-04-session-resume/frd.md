# FRD: Session Resume Rename

- Status: Draft
- Start date: 2026-08-04
- Target release: 0.13.0
- Compatibility removal: 0.14.0
- Related SDD: `docs/sdd/2026-08-03-harness-integration`

## Summary

Rename the session lifecycle operation from `restart` to `resume`. The canonical command becomes
`agw session resume`; `agw session restart` remains available for one release with a suppressible
deprecation warning and is removed in the following release.

`resume` names the operator-visible intent: continue the same Agentworks session and, when the
harness integration has durable conversation state, continue the same harness conversation. The
implementation may replace a process or tmux session while fulfilling that intent, but that
mechanism should not define the public operation name.

The rename also makes the lifecycle vocabulary coherent. Agentworks creates a session and later
resumes it. The old `restart` verb implied a matching `start` operation that the CLI does not have.

## Functional requirements

- **R1 (canonical command).** `agw session resume` MUST become the documented and completed command.
  It MUST accept the same positional target, batch flags, filters, force behavior, prompts, and
  `--yes` behavior as `agw session restart` accepts before this change.
- **R2 (one-release command compatibility).** In 0.13.0, `agw session restart` MUST remain a fully
  functional alias for `agw session resume`. Each invocation MUST emit one warning that names the
  replacement and states that the alias will be removed in 0.14.0. The global `--no-deprecations`
  option MUST silence this warning. The alias MUST be removed in 0.14.0.
- **R3 (behavior preservation).** The rename MUST NOT alter target selection, status detection,
  confirmation policy, secret resolution, activation gates, process teardown, transport behavior,
  persisted session state, console behavior, or error propagation. Claude Code and Codex MUST
  continue an existing conversation when their saved state permits it and launch a new conversation
  under the same conditions as before.
- **R4 (canonical implementation vocabulary).** Live code identifiers and operation-specific text
  MUST use `resume`: CLI handlers and helpers, manager functions, batch functions, local variables,
  operation contexts, logger operation names, user-facing output, hints, errors, tests, fixtures,
  and comments. Mechanical uses of `restart` that refer to a VM, service, process, retry loop, or
  another genuinely restarting object MUST remain unchanged.
- **R5 (harness integration contract).** The harness integration operation MUST be renamed from
  `restart(ctx)` to `resume(ctx)` across the base contract and every implementation. Plugin authors
  MUST see only the canonical method in current documentation and examples.
- **R6 (shell config vocabulary).** `resume_command` MUST become the canonical shell integration
  field. In 0.13.0, previously valid uses of `restart_command` MUST continue to load with a
  suppressible deprecation warning that names `resume_command` and the 0.14.0 removal. If both names
  are declared for one effective integration config, loading MUST fail rather than choose a
  precedence. Canonical samples, migration output, and serialization MUST emit only
  `resume_command`. The old field MUST be removed in 0.14.0.
- **R7 (canonical documentation and discovery).** Current operator docs, contributor docs, CLI help,
  sample manifests, completion metadata, and architecture docs MUST teach `resume`. Historical
  changelog entries and locked historical SDDs MUST remain unchanged. Active SDDs whose current
  contract is affected MUST be updated before they lock.
- **R8 (no duplicate implementation).** The canonical and deprecated CLI commands MUST share one
  execution path. Compatibility wrappers may add warnings but MUST NOT fork validation or lifecycle
  behavior.

## Personas and stories

- As an operator, I can type `agw session resume <name>` and understand that I am continuing an
  existing logical session.
- As an operator upgrading to 0.13.0, my existing `session restart` scripts continue to work and
  tell me exactly what to change.
- As an operator who suppresses migration guidance in automation, I can pass `--no-deprecations` and
  receive no alias warning.
- As a template author, I can use `resume_command` for the command that continues a shell-backed
  session, while my existing `restart_command` declaration works during the transition.
- As an integration author, I implement `resume(ctx)` and reason about the logical operation rather
  than the process-management mechanism used by the session manager.

## Non-goals

- Changing what counts as resumable harness state or when a harness launches a new conversation.
- Adding an `agw session start` command.
- Preserving public Python imports or method aliases for `restart_session`, `restart_all_sessions`,
  or `HarnessIntegration.restart`. These are internal interfaces and cut over with the codebase.
- Renaming unrelated restart operations, including VM and service restarts.
- Rewriting historical release notes or locked SDDs to use terminology that did not exist when they
  were written.

## Acceptance criteria

1. `session resume` passes the existing single and batch lifecycle coverage under its new name.
2. `session restart` produces the same result plus exactly one warning in 0.13.0.
3. `agw --no-deprecations session restart ...` produces the same result without that warning.
4. Help and completion expose `resume` as canonical while retaining the deprecated alias for the
   compatibility release.
5. Canonical shell integration configuration accepts and emits `resume_command`; old
   `restart_command` input is warned, not silently ignored or ambiguously merged.
6. A scoped residual search finds old lifecycle terminology only in compatibility code, historical
   artifacts, and text describing genuinely mechanical restarts.

## Decisions

- **D1:** `resume` is a logical operation even when fulfillment kills and recreates a process.
- **D2:** The compatibility window is 0.13.0 only; removal is 0.14.0.
- **D3:** Deprecated command and config warnings use the existing deprecation channel and obey
  `--no-deprecations`.
- **D4:** Current code cuts over rather than carrying internal Python aliases for one release.

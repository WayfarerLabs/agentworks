# HLA: Session Resume Rename

- Status: Draft
- Start date: 2026-08-04
- Builds on: `frd.md`
- Migration details: `migration-strategy.md`

## 1. Architecture summary

The change preserves the existing session lifecycle topology and renames the operation at each
semantic boundary. The CLI gains a canonical `resume` entry point and retains a thin deprecated
`restart` wrapper for 0.13.0. Both enter one private command implementation, which calls the renamed
session manager API. The manager continues to orchestrate the same nodes, readiness checks,
transports, teardown, integration operation, and tmux creation path.

```text
agw session resume  ------------------+
                                       +--> shared CLI implementation
agw session restart [warn in 0.13] ---+             |
                                                     v
                                         resume_session / resume_all_sessions
                                                     |
                                                     v
                                      HarnessIntegration.resume(RunContext)
                                                     |
                                                     v
                                      existing process and tmux mechanics
```

There is no new runtime component and no persisted-data migration.

## 2. Target vocabulary

| Surface                  | Old                    | Canonical             |
| ------------------------ | ---------------------- | --------------------- |
| CLI command              | `agw session restart`  | `agw session resume`  |
| CLI handler              | `session_restart`      | `session_resume`      |
| Single manager operation | `restart_session`      | `resume_session`      |
| Batch manager operation  | `restart_all_sessions` | `resume_all_sessions` |
| Integration method       | `restart(ctx)`         | `resume(ctx)`         |
| Shell config field       | `restart_command`      | `resume_command`      |
| Operation context        | `restart_ctx`          | `resume_ctx`          |
| SSH log operation        | `session-restart`      | `session-resume`      |
| Result language          | restarted / restarting | resumed / resuming    |

Names that accurately describe lower-level mechanics remain unchanged. Examples include restarting
`tailscaled`, restarting a VM, and an attach loop waiting for a process to return.

## 3. CLI composition

`session_resume` is the canonical Typer command and owns the public help text. A private helper owns
argument validation and dispatch. The deprecated `session_restart` command accepts the same
parameters, emits its warning when deprecations are not suppressed, and delegates immediately to
that helper.

This shape is preferred over registering one callback under two names because the runtime must know
which spelling the operator invoked before deciding whether to warn. It also keeps the deprecation
text out of the canonical command's help and execution.

The wrapper warning is emitted before lifecycle work and after successful CLI parsing. It uses
`output.warn` guarded by `output.deprecations_suppressed()`. Configuration deprecation warnings may
also be present in the same invocation; each warning describes a distinct deprecated input and is
not deduplicated with the command warning.

## 4. Manager and integration boundaries

The manager exports only `resume_session` and `resume_all_sessions` after the 0.13.0 change. All
in-repository callers and tests move in the same commit range. No Python compatibility alias is
needed because this package is an internal service layer rather than a supported external SDK.

The `HarnessIntegration` abstract operation becomes `resume(ctx)`. The `shell`, `claude-code`, and
`codex` implementations change in lockstep. Their algorithms do not change:

- `shell` chooses `resume_command`, falling back to `command`.
- `claude-code` continues the recorded Claude session when available and otherwise launches under
  the existing fallback rules.
- `codex` continues the recorded Codex conversation when available and otherwise launches under the
  existing fallback rules.

The lifecycle implementation can still stop or kill an old workload and create a new tmux session.
Those details are subordinate to the logical resume operation and remain named for the actual
mechanical action where useful.

## 5. Shell configuration compatibility

The shell integration's canonical config schema accepts `command`, `resume_command`, and
`required_commands`. Compatibility normalization accepts `restart_command` as an old spelling in
0.13.0 and records one deprecation issue per request through the existing suppressible deprecation
channel.

Normalization happens before inheritance so inherited configurations have one vocabulary. A
declaration graph that supplies both old and new spellings for the same effective shell integration
is rejected. This avoids order-dependent behavior where a child using one spelling silently
overrides a parent using the other.

The manifest migrator rewrites `restart_command` to `resume_command`; canonical resource samples and
emitted YAML use only the new key. The compatibility and conflict matrix is specified in
`migration-strategy.md`.

## 6. Completions and help

The completion spec moves dynamic mappings from `session.restart` to `session.resume` and retains
matching `session.restart` mappings for the compatibility release. Extracted command trees contain
both commands in 0.13.0, but descriptions mark `restart` deprecated and direct users to `resume`.
Completion tests pin both functionality and the canonical wording.

In 0.14.0, the deprecated command and its completion mappings are deleted together.

## 7. Documentation policy

Current docs, examples, sample manifests, capability documentation, CLI tables, hints, and active
SDDs use `resume`. The implementation phase updates the still-draft harness-integration SDD because
its integration contract currently names `restart(ctx)` and its examples are current design input.

Historical changelog entries and locked SDDs retain `restart`. They describe prior releases and are
not canonical documentation. Residual searches classify rather than blindly replace every match.

## 8. Verification strategy

- Move existing lifecycle unit and orchestrated tests to canonical manager and integration names.
- Add CLI tests that compare canonical and alias argument behavior, output, failures, and
  suppression.
- Add config tests for canonical, old-only, mixed, inherited, migrated, and suppressed cases.
- Update completion snapshots or command-tree assertions for both 0.13.0 spellings.
- Run targeted session, integration, config, manifest, migration, and completion suites, followed by
  the full project gate.
- Perform a classified residual search across live code and current documentation.

## 9. Risks and safeguards

- **Behavior drift between commands:** prevented by one shared CLI implementation and parity tests.
- **Warning emitted too late:** emit the command warning before configuration load and lifecycle
  mutation.
- **Config precedence ambiguity:** reject effective configurations containing both spellings.
- **Incomplete rename:** use targeted inventories plus a final classified residual search.
- **Accidental historical rewrite:** exclude changelog and locked SDD content from bulk replacement.
- **Active branch mismatch:** update the active harness-integration SDD and implementation together
  so its eventual lock records the canonical contract.

# Residual Inventory: Session Restart to Resume

- Status: 0.13.0 documentation sweep, 2026-08-04
- Scope: current tree at this sweep, excluding generated caches

The Phase 4 residual search used:

```console
rg -n -i 'session restart|restart_command|\.restart\(|restart_session|restart_all_sessions|restart_ctx' .
```

Most remaining matches are intentional and fit one of these classifications:

| Classification               | Remaining locations                                                                                            | Reason                                                                                                                |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| 0.13.0 command compatibility | `cli/agentworks/cli/commands/session.py`, completion metadata, and their tests                                 | `agw session restart` remains a warning-producing alias through 0.13.0.                                               |
| 0.13.0 config compatibility  | shell integration, config and manifest loaders, migration code, and their tests                                | `restart_command` normalizes to `resume_command`, warns unless deprecations are suppressed, and is removed in 0.14.0. |
| Explicit migration guidance  | this SDD, the active harness-integration SDD correction, operator docs, ADR 0020, and the 0.13.0 release notes | The old spellings are named only to explain the compatibility window, migration, downgrade, and removal.              |
| Historical record            | `cli/CHANGELOG.md` entries before 0.13.0 and earlier SDD artifacts                                             | Those entries document vocabulary that was correct when written and are not rewritten.                                |
| Mechanical restart           | VM, service, process, and retry-loop terminology                                                               | These operations genuinely restart the named object and do not refer to the session lifecycle API.                    |

One non-compatibility residual remains outside the documentation sweep: canonical session-lifecycle
text in `cli/agentworks/sessions/manager/_lifecycle.py` still says `restart` in comments, prompts,
hints, and errors. It is a Phase 1/4 implementation correction, not a documentation exception, and
blocks the final residual-sweep checkbox until the lifecycle owner updates it.

No current documentation, sample manifest, or configuration example uses `restart` terminology as a
canonical surface. The release notes and operator docs identify the old command and field only as
temporary compatibility inputs.

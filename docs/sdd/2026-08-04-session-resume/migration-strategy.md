# Migration Strategy: Session Restart to Resume

- Status: Draft
- Start date: 2026-08-04
- Compatibility release: 0.13.0
- Removal release: 0.14.0

## 1. Current-state inventory

At the start of this effort, `restart` appears across four distinct categories:

1. The operator command `agw session restart`, including single and batch forms.
2. The session service layer and orchestration internals (`restart_session`, `restart_all_sessions`,
   contexts, messages, and tests).
3. The harness integration contract (`restart(ctx)`) and shell config (`restart_command`).
4. Unrelated mechanical operations such as VM, service, and process restarts.

Only the first three categories migrate. The fourth is intentionally retained.

There is no database field or stored session status named for this operation. Existing session rows
and harness integration state require no data migration.

## 2. Additive-first command transition

### 0.13.0

- Add `agw session resume` as the canonical command.
- Retain `agw session restart` with identical parameters and behavior.
- Emit exactly one alias warning per deprecated-command invocation unless `--no-deprecations` is
  set:

  ```text
  'agw session restart' is deprecated; use 'agw session resume'. It will be removed in 0.14.0.
  ```

- Canonical docs and examples use only `resume`, except migration guidance that explicitly shows the
  deprecated spelling.
- Completion includes both spellings so existing interactive workflows do not break. The deprecated
  command's help identifies the replacement.

### 0.14.0

- Delete the `restart` Typer command wrapper and its completion mappings.
- Delete alias-warning tests and replace them with an unknown-command assertion.
- Record the removal in the release notes.

No automatic script rewriting is provided. The warning gives a direct one-token replacement, and
shell scripts continue working throughout 0.13.0.

## 3. Direct internal cutover

All current Python identifiers and integration method implementations cut over in 0.13.0. There is
no temporary `restart_session` function alias and no `HarnessIntegration.restart` forwarding method.
This keeps the compatibility boundary at operator and declarative-config inputs rather than
embedding old vocabulary in new code.

The cutover order within the implementation branch is always-green:

1. Rename the integration abstract method and all implementations and call sites together.
2. Rename manager functions, exports, callers, and tests together.
3. Add the canonical CLI command and shared implementation, then reduce the old command to its
   warning wrapper.
4. Rename operation-specific output, logs, variables, comments, and current docs.

## 4. Shell config transition

### Input matrix for 0.13.0

| Effective shell configuration                       | Loads | Warns | Canonical value                |
| --------------------------------------------------- | ----- | ----- | ------------------------------ |
| `command` only                                      | yes   | no    | `command` is fallback          |
| `resume_command` only                               | yes   | no    | `resume_command`               |
| `restart_command` only                              | yes   | yes   | normalized to `resume_command` |
| neither resume field nor `command`                  | yes   | no    | empty login shell              |
| both `resume_command` and `restart_command` locally | no    | n/a   | conflict error                 |
| parent old, child new                               | no    | n/a   | effective conflict error       |
| parent new, child old                               | no    | n/a   | effective conflict error       |
| parent old, child unrelated override                | yes   | yes   | inherited value normalized     |

The same semantic matrix applies whether the shell integration originates in YAML or deprecated TOML
resource input. Existing source-level deprecation warnings remain independent of the field rename.

The field warning is suppressible with `--no-deprecations` and states:

```text
restart_command is deprecated; use resume_command instead. It will be removed in 0.14.0. Silence this warning with --no-deprecations.
```

When several loaded resources use the old field, the loader SHOULD aggregate them into one warning
that names the affected session templates, matching the repository's ambient deprecation pattern.
The runtime config passed to `ShellIntegration` contains only `resume_command`.

### Migration and emitted form

`agw resource migrate` rewrites old shell config fields to `resume_command` in canonical YAML. If a
source contains both spellings, migration fails without modifying the source. Sample output,
packaged manifests, and documentation emit only `resume_command`.

### 0.14.0 removal

- Remove `restart_command` recognition and normalization.
- Remove its deprecation aggregation and migrator rewrite.
- Treat the old key as an unknown integration config field.
- Retain `resume_command` behavior and inheritance unchanged.

## 5. Worked examples

### Command

Before:

```console
$ agw session restart coding
Warning: 'agw session restart' is deprecated; use 'agw session resume'. It will be removed in 0.14.0.
...
Session 'coding' resumed
```

After updating the caller:

```console
$ agw session resume coding
...
Session 'coding' resumed
```

### Shell integration config

Before:

```yaml
harness_integration:
  name: shell
  command: claude
  restart_command: claude --resume
```

Canonical:

```yaml
harness_integration:
  name: shell
  command: claude
  resume_command: claude --resume
```

## 6. Residual classification

The implementation closes with searches for `session restart`, `restart_session`,
`restart_all_sessions`, `restart_command`, `restart_ctx`, and integration `.restart(` calls. Each
remaining match must fit one of these categories:

- the 0.13.0 command or config compatibility shim and its tests;
- explicit migration documentation;
- a historical changelog entry or locked historical SDD;
- a mechanical restart of another object;
- quoted old input in a migration fixture.

Unclassified matches block completion.

## 7. Rollback and failure behavior

The change does not mutate stored sessions, so rollback to the previous release requires no data
rollback. Configurations authored with `resume_command` are not understood by 0.12.0; operators who
must downgrade need to restore `restart_command`. This is an ordinary forward-compatible config
rename and should be called out in the 0.13.0 release notes.

The CLI alias is safe to retry because it delegates to the existing lifecycle behavior. A failure
after the warning follows the same recovery path as the canonical command.

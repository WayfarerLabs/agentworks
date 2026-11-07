# Runtime context templates: migration

Snapshot: 2026-11-06 at `c8d4dfb3cb042eea2ae5ac12b06624faf703400b`.

## Current state

- The session manager defines two names, `session_name` and `workspace_name`, and one regex
  substituter in `sessions/manager/_constants.py:7` and `_env.py:120`.
- Session env plaintext is expanded before the cross-scope merge in `_env.py:223` and `:291`.
- Create and lifecycle launch commands are expanded after the integration returns its command in
  `_create_roll.py:348` and `_lifecycle.py:901`.
- Other env scopes and `env show` do not run the session env substituter.
- `compose_env` is consumed by session launch, VM access, agent access, console extra panes, and
  harness setup. The shared core is in `env/compose.py:37`.
- `quote_literal_argv` in `capabilities/harness_integration/base.py:194` protects workload data from
  interpolation of returned shell command strings. Some native extra args intentionally participate
  in interpolation.

## Target and examples

Keep the valid existing syntax and names. Add the accepted context catalogue across winning
plaintext env values and existing command consumers.

```yaml
env:
  TOOL_CONFIG_DIR: "{{user_home_dir}}/.config/tool"
  WORKTREE_LABEL: "{{workspace_name}}"
```

Given an agent resource `reviewer` running as Linux user `agt-reviewer`, the first value becomes
`/home/agt-reviewer/.config/tool`. The template remains unchanged when used by another agent or the
admin user. `WORKTREE_LABEL` requires a workspace-rooted consumer.

For literal brace text, replace `{{component}}` with `{{{{component}}` in an interpolated field.
Secret values and ordinary harness prompt fields remain literal without this migration.

## Behavior changes to disclose

1. A previously literal valid placeholder in a non-session env scope starts expanding. Unknown
   placeholders in those scopes become errors. Operators needing literal text must escape it.
2. The new parser rejects malformed opening `{{` sequences previously ignored by the regex. Closing
   braces outside placeholders remain literal, including nested JSON ending in `}}`.
3. Losing env entries are not rendered. A lower-scope unavailable placeholder overridden by a higher
   scope no longer causes a runtime failure.
4. `env show` previews expanded winning plaintext rather than raw placeholders. It remains an
   offline managed-config inspection, not a view into a running shell.
5. Existing valid command substitutions keep their meaning; command source still follows shell
   quoting rules. Ordinary native harness input remains literal.

No DB schema conversion is needed for the proposed context increment. Persisted instance specs
retain template expressions; recomputation happens when an invocation is prepared. Existing running
sessions retain their current env until restarted. No background reconfiguration occurs.

## Sequence

Land parser, context catalogue, env and command callers, tests, permanent reference updates, and
release guidance as one coherent implementation increment. Remove the old manager-owned parser and
early session env preprocessing in that increment; do not leave two implementations active. Inspect
stored specs through normal fixtures, without reading an operator's private live state.

If environment references are accepted, revise this migration before implementing them. Their lookup
phase, sensitivity, and inspection behavior cannot be inherited from context placeholders by
accident.

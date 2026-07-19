# Harness API: low-level design

**Status:** Stub (to be written before Phase 1) **Repo:** `agentworks` **Path:** `cli/agentworks/`

Pins the shared harness contract so the `shell` and `claude-code` built-ins and the session-node
swap all reason about one shape. Read `hla.md` "The Harness API" and "Open questions / for LLD"
first; this document turns those open questions into pinned answers.

## To pin

- **`Harness(Capability)` construction.** Exact signature and types for
  `(owner_name, config, *, session_name, vm_name, workspace_name, target, admin)`; the `Node | None`
  type of `target` without importing `orchestration/` (structural/`TYPE_CHECKING`);
  `owner_kind = "session-template"`.
- **`validate_config` split.** Shape-only at load (unknown fields error, per-field vocab) vs
  completeness on the merged blob at resolve (required/cross-field). Both built-ins return `()`.
- **`merge_config` hook.** Classmethod vs instance; default shallow `{**base, **child}`; `shell`'s
  `required_commands` append-dedupe union override while scalars child-win.
- **Readiness relocation.** The four-way fork (skip/defer/probe/error) plus the fifth
  `scope is None` loud branch, moved from `RequiredCommandsCheck._check` unchanged; the single-fire
  guard.
- **SESSION-level identity guard.** RESOLVED that it arrives WITH the harness in Phase 3 (the guard
  does not exist on `RequiredCommandsCheck` at HEAD, confirmed), not retrofitted onto the interim
  check. Still to pin: which fields the guard compares (`session`/`vm`/`workspace` + agent-or-admin)
  against the harness's captured identity, and raise vs warn (raise recommended).
- **`require_commands` helper.** Signature and the relocated `_probe` body (the
  `$SHELL -lic 'command -v <cmd>'` loop, `check=False`, missing-command error + label parity).
- **Op-start `RunContext` assembly.** How the op call sites build a ctx carrying execution targets +
  scoped secrets for `start`/`restart`, since `_build_session_command` takes no ctx today. Create
  (`manager.py:1932`) mirrors the runup ctx at `1873-1880`; restart (`2483`) has no runup ctx, so it
  mirrors the preflight ctx at `2373-2381`, assembled after the kill. The scoped secrets are scoped
  to the session node's `secret_refs()` union (declare-and-receive; the harness's contribution
  included, empty for the built-ins), not raw `secret_values`.
- **Template-variable substitution relocation.** Lifting `_substitute_template_vars` out of the
  former `_build_session_command` to wrap the harness's returned string; escaping so generated
  claude-code snippets with literal braces are not mangled (or restricting substitution to shell's
  operator-authored values).
- **`ResolvedSessionTemplate` reshape and `_merge_pair`.** The pair merge walk per FRD R5.

**Done when:** every item above has a pinned answer or explicit deferral, reviewed.

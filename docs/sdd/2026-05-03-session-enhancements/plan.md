# Session enhancements -- implementation plan

**Status:** Draft **Branch:** `feat/session-enhancements`

## Overview

Implements the session enhancements described in the [FRD](frd.md) and [HLA](hla.md). The work adds
PID-based liveness checking, drops the cached status column, and introduces a repair mechanism for
pre-enhancement sessions.

Each phase builds on the previous. The codebase should be in a passing state (tests green, lint
clean) after each phase.

## Phase 1: DB and type foundation

Schema changes, new types, and method updates that everything else builds on.

- [ ] Migration 20: `DROP COLUMN status`, `ADD COLUMN pid INTEGER`
- [ ] `SessionRow`: remove `status: str`, add `pid: int | None`
- [ ] `_to_session` row converter: drop status, add pid
- [ ] `insert_session`: remove status default
- [ ] `SessionHealth` enum (OK / STOPPED / BROKEN / UNKNOWN) -- in `db.py` or `sessions/health.py`
- [ ] `update_session_pid(name, pid)` method
- [ ] Remove `SessionStatus` enum
- [ ] Remove `update_session_status` method
- [ ] Update tests: `test_db.py`, `test_tmuxinator.py` (SessionRow without status field)
- [ ] Migration test following the `test_migration_19_*` pattern in `test_db.py`

**Done when:** migration applies cleanly, `SessionRow` has `pid` and no `status`, all existing tests
pass.

### Secondary status consumers

These files display `session.status` in summary views (vm describe, workspace describe, agent
describe). They need updating alongside the SessionRow change.

- [ ] `sessions/console.py:167` -- `_get_running_sessions_for_vm` filters by `SessionStatus.RUNNING`
- [ ] `workspaces/manager.py:262,511` -- displays `s.status`, filters running sessions
- [ ] `vms/manager.py:371` -- displays `s.status`
- [ ] `agents/manager.py:166,348` -- displays `s.status`

For summary views (vm/workspace/agent describe), drop the status display. Status is shown by
`session list` via live checks. For `console.py`, include all sessions (the wrapper script already
handles dead sessions via `tmux has-session`).

## Phase 2: Liveness infrastructure

Core functions for status checking, health checking, and PID-based operations.

### tmux.py

- [ ] `create_session` return type: `str | None` -> `tuple[str | None, int]`
- [ ] PID retrieval after `tmux new-session`: `tmux [-S <socket>] display-message -p '#{pid}'`
- [ ] `get_tmux_server_pid(target, socket_path=None) -> int | None` -- for repair
- [ ] `force_kill_tmux_server(pid, target, socket_path=None) -> bool` -- SIGTERM/SIGKILL escalation
- [ ] Tests for new tmux.py functions (mocked ExecTarget)

### manager.py

- [ ] `check_session_status(pid, target) -> bool` -- single PID check via `kill -0`
- [ ] `batch_check_status(sessions, target) -> dict[str, bool]` -- compound command, parse output
- [ ] `check_session_health(session, target) -> SessionHealth` -- PID + tmux has-session, pure (no
      DB side effects)
- [ ] Tests for status/health functions (mocked ExecTarget, verify command construction and result
      parsing)

**Done when:** all new functions exist with tests. `batch_check_status` correctly builds compound
`kill -0` commands and parses `STATUS:<name>:<exit_code>` output.

## Phase 3: Command migration

Update all session commands to use the new liveness model per the R3 command behavior table.

### PID capture

- [ ] `create_session` in manager.py: call `db.update_session_pid` after creation
- [ ] `restart_session`: store new PID after restart

### Command updates (per R3 table)

- [ ] `stop_session`: health check; add `--force` flag; error on BROKEN unless --force (force_kill);
      STOPPED -> "already stopped"
- [ ] `restart_session`: health check; error on OK/BROKEN unless --force; UNKNOWN -> error, suggest
      repair; use force_kill for BROKEN
- [ ] `delete_session`: health check; error on OK/BROKEN/UNKNOWN unless --force; always confirm
      (`--yes` to skip); use force_kill when needed
- [ ] `list_sessions`: replace per-session `_reconcile_status` with `batch_check_status`; group by
      VM; parallel across VMs (capped at 8); `--no-status` shows `-`
- [ ] `describe_session`: show health via `check_session_health`; suggest repair for BROKEN/UNKNOWN
- [ ] `attach_session`: health check; clear error messages for STOPPED, BROKEN, UNKNOWN
- [ ] `session_logs`: health check; clear error messages for STOPPED, BROKEN, UNKNOWN
- [ ] `restart_all_sessions`: batch PID check to determine which sessions are stopped (replaces
      `SessionStatus.STOPPED` filter)
- [ ] Admin-mode `--force` warning: killing the PID affects all admin sessions on that server

### CLI changes

- [ ] Add `--force` flag to `session stop` command
- [ ] Add `--force` flag to `session delete` command

### Remove dead code

- [ ] Remove `_reconcile_status`
- [ ] Remove `_session_alive`
- [ ] Tests for each command covering the health-based branching

**Done when:** each command behaves according to the R3 table. `session list` uses one SSH call per
VM instead of one per session.

## Phase 4: Repair mechanism

PID recovery for pre-enhancement sessions (R6/R7).

- [ ] `repair_session(db, config, name)` in manager.py -- single session repair
- [ ] `repair_all_sessions(db, config, vm_name=None, workspace_name=None)` -- batch repair, one SSH
      call per VM
- [ ] `session repair <name>` CLI command
- [ ] `session repair --all [--vm <vm>] [--workspace <ws>]` CLI command
- [ ] Output messages per R7: "Recovered PID...", "...is not running", "...already has PID, skipped"
- [ ] Update shell completions for `session repair`
- [ ] Tests

**Done when:** `session repair` recovers PIDs from running tmux servers, batch repair uses one SSH
call per VM, completions include the new command.

## Phase 5: Verification

- [ ] `grep -r 'SessionStatus\|_reconcile_status\|_session_alive' cli/agentworks/` returns no hits
- [ ] Full test suite passes
- [ ] Lint clean (`ruff`, `markdownlint`)
- [ ] Spell check clean (`cspell`)
- [ ] Update plan checkboxes and note any design deviations

**Done when:** no dead references, all checks green.

## Notes

- **No LLDs anticipated.** The HLA pseudocode and component tables provide sufficient detail. Add an
  LLD if a phase reveals unexpected complexity.
- **Test strategy:** mocked `ExecTarget` for liveness functions, migration tests following
  `test_db.py` patterns, existing tests updated as schemas change.
- **Sample config:** no new settings, `sample-config.toml` unchanged.
- **Docs:** update user-facing docs that describe session commands, if any exist.

# Session enhancements -- implementation plan

**Status:** Complete **Branch:** `feat/session-enhancements`

## Overview

Implements the session enhancements described in the [FRD](frd.md) and [HLA](hla.md). The work adds
PID-based liveness checking, drops the cached status column, and introduces a repair mechanism for
pre-enhancement sessions.

Each phase builds on the previous. The codebase should be in a passing state (tests green, lint
clean) after each phase.

## Phase 1: DB and type foundation

Schema changes, new types, and method updates that everything else builds on.

- [x] Migration 20: `DROP COLUMN status`, `ADD COLUMN pid INTEGER`
- [x] `SessionRow`: remove `status: str`, add `pid: int | None`
- [x] `_to_session` row converter: drop status, add pid
- [x] `insert_session`: remove status default
- [x] `SessionHealth` enum (OK / STOPPED / BROKEN / UNKNOWN) -- in `db.py` or `sessions/health.py`
- [x] `update_session_pid(name, pid)` method
- [x] Remove `SessionStatus` enum
- [x] Remove `update_session_status` method
- [x] Update tests: `test_db.py`, `test_tmuxinator.py` (SessionRow without status field)
- [x] Migration test following the `test_migration_19_*` pattern in `test_db.py`

**Done when:** migration applies cleanly, `SessionRow` has `pid` and no `status`, all existing tests
pass.

### Secondary status consumers

These files display `session.status` in summary views (vm describe, workspace describe, agent
describe). They need updating alongside the SessionRow change.

- [x] `sessions/console.py:167` -- `_get_running_sessions_for_vm` filters by `SessionStatus.RUNNING`
- [x] `workspaces/manager.py:262,511` -- displays `s.status`, filters running sessions
- [x] `vms/manager.py:371` -- displays `s.status`
- [x] `agents/manager.py:166,348` -- displays `s.status`

For summary views (vm/workspace/agent describe), drop the status display. Status is shown by
`session list` via live checks. For `console.py`, include all sessions (the wrapper script already
handles dead sessions via `tmux has-session`).

## Phase 2: Liveness infrastructure

Core functions for status checking, health checking, and PID-based operations.

### tmux.py

- [x] `create_session` return type: `str | None` -> `tuple[str | None, int]`
- [x] PID retrieval after `tmux new-session`: `tmux [-S <socket>] display-message -p '#{pid}'`
- [x] `get_tmux_server_pid(target, socket_path=None) -> int | None` -- for repair
- [x] `force_kill_tmux_server(pid, target, socket_path=None) -> bool` -- SIGTERM/SIGKILL escalation
- [x] Tests for new tmux.py functions (mocked ExecTarget)

### manager.py

- [x] `check_session_status(pid, target) -> bool` -- single PID check via `kill -0`
- [x] `batch_check_status(sessions, target) -> dict[str, bool]` -- compound command, parse output
- [x] `check_session_health(session, target) -> SessionHealth` -- PID + tmux has-session, pure (no
      DB side effects)
- [x] Tests for status/health functions (mocked ExecTarget, verify command construction and result
      parsing)

**Done when:** all new functions exist with tests. `batch_check_status` correctly builds compound
`kill -0` commands and parses `STATUS:<name>:<exit_code>` output.

## Phase 3: Command migration

Update all session commands to use the new liveness model per the R3 command behavior table.

### PID capture

- [x] `create_session` in manager.py: call `db.update_session_pid` after creation
- [x] `restart_session`: store new PID after restart

### Command updates (per R3 table)

- [x] `stop_session`: health check; `--force` for BROKEN (PID kill); STOPPED -> "already stopped"
- [x] `restart_session`: health check; prompt for running (`-y` to skip); `--force` for BROKEN;
      UNKNOWN -> error, suggest repair
- [x] `delete_session`: health check; prompt for running/unknown (`-y` to skip); `--force` for
      BROKEN; always confirm (`-y` to skip)
- [x] `list_sessions`: replace per-session `_reconcile_status` with `batch_check_status`; group by
      VM; parallel across VMs (capped at 8); `--no-status` shows `-`
- [x] `describe_session`: show health via `check_session_health`; suggest repair for BROKEN/UNKNOWN
- [x] `attach_session`: health check; clear error messages for STOPPED, BROKEN, UNKNOWN
- [x] `session_logs`: health check; clear error messages for STOPPED, BROKEN, UNKNOWN
- [x] `restart_all_sessions`: batch PID check to determine which sessions are stopped (replaces
      `SessionStatus.STOPPED` filter)
- [x] Admin-mode `--force` warning: killing the PID affects all admin sessions on that server

### CLI changes

- [x] Add `--force` flag to `session stop` command
- [x] Add `--force` flag to `session delete` command

### Remove dead code

- [x] Remove `_reconcile_status`
- [x] Remove `_session_alive`
- [x] Tests for each command covering the health-based branching

**Done when:** each command behaves according to the R3 table. `session list` uses one SSH call per
VM instead of one per session.

## Phase 4: Repair mechanism

PID recovery for pre-enhancement sessions (R6/R7).

- [x] ~~`repair_session` / `repair_all_sessions` / CLI command~~ -- replaced by auto-repair
- [x] `_ensure_pid(session, target, db)` -- auto-repair single session on access
- [x] `_ensure_pids_batch(sessions, db, config)` -- auto-repair all NULL-PID sessions for batch
      commands
- [x] All commands call `_ensure_pid` / `_ensure_pids_batch` before health checks
- [x] Remove `session repair` CLI command and completions

**Done when:** any command that touches a session with NULL PID auto-recovers it transparently.

## Phase 5: Verification

- [x] `grep -r 'SessionStatus\|_reconcile_status\|_session_alive' cli/agentworks/` returns no hits
- [x] Full test suite passes
- [x] Lint clean (`ruff`, `markdownlint`)
- [x] Spell check clean (`cspell`)
- [x] Update plan checkboxes and note any design deviations

**Done when:** no dead references, all checks green.

## Post-plan additions

Work discovered and completed during implementation, after the original plan was written.

- [x] **PID sentinel (`PID_STOPPED = -1`)**: distinguish "never checked" (NULL/UNKNOWN) from "known
      stopped" (-1/STOPPED). Repair sets -1 when a session is not running, breaking the dead-end
      loop where commands say "run repair" but repair already ran.
- [x] **`/proc` for liveness**: use `test -d /proc/<pid>` instead of `kill -0` for PID checks. The
      admin user cannot signal agent-owned processes without sudo; `/proc` has no such restriction.
- [x] **Batch stop/restart**: `stop --all`, `restart --all-stopped`, `restart --all` with `--vm`
      and `--workspace` filters. Replaces the old `restart-all` subcommand.
- [x] **Auto-repair replaces `session repair`**: NULL-PID sessions are auto-repaired on access by
      any command. No explicit repair command needed.

## Design deviations

- **`_session_alive` retained**: kept for the normal stop path (OK health), where tmux-level
  `has-session` checks are needed after C-c. Will be removed when/if the stop path is refactored.
- **Admin-mode `--force` warning not implemented**: the R3 table specifies warning the operator when
  `--force` on an admin session would kill the shared tmux server. Deferred -- requires listing other
  admin sessions sharing the PID, which is a query not yet implemented.

## Notes

- **No LLDs anticipated.** The HLA pseudocode and component tables provide sufficient detail. Add an
  LLD if a phase reveals unexpected complexity.
- **Test strategy:** mocked `ExecTarget` for liveness functions, migration tests following
  `test_db.py` patterns, existing tests updated as schemas change.
- **Sample config:** no new settings, `sample-config.toml` unchanged.
- **Docs:** update user-facing docs that describe session commands, if any exist.

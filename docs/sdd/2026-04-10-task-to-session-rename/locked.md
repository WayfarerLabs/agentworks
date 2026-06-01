# Task-to-session rename -- locked

Locked: 2026-04-12

## Summary

Renamed the "task" concept to "session" across the entire agentworks codebase. Session names are now
globally unique (no longer scoped to a workspace), simplifying the CLI, tmux integration, and
internal code.

## Key decisions

- **Globally unique session names** instead of workspace-scoped task names. The session name IS the
  tmux session name -- no compound key derivation. Most CLI commands take just a session name with
  no `--workspace` flag.
- **`--` separator in migration** for existing tasks (`<workspace>--<task>`). This is collision-free
  (disallowed in names) and matches the existing tmux session and socket naming, so migrated names
  work immediately with no legacy fallback.
- **`socket_path` persisted in DB** rather than derived at runtime. Decouples naming conventions
  from socket location. `_effective_socket_path()` derives the path as a fallback for migrated
  sessions with NULL `socket_path`.
- **Clean break on config** -- no backward compatibility for `[task.*]` keys. Users must update to
  `[session.*]`.
- **Socket lifecycle hardened**: pre-create check (remove stale, fail on active), post-delete
  cleanup (remove if server exited, warn if still running), bulk cleanup on vm/agent reinit.
- **SGID on workspace subdirectories** fixed during this work -- `workspace create`, `repair`,
  `rehome`, and `copy` now set SGID on all subdirectories so files created by atomic-write tools
  (including Claude Code) inherit the workspace group.
- **README rewritten** with four-concept domain model (Operator, VM, Workspace, Agent, Session), Key
  Principles section, and Tightly Integrated Tools (SSH, Tailscale, Tmux).

## Files changed

- `cli/agentworks/db.py` -- migrations 17-18, renamed types/methods, socket_path column
- `cli/agentworks/config.py` -- SessionTemplate, SessionConfig, loader functions
- `cli/agentworks/sample-config.toml` -- renamed sections and template variables
- `cli/agentworks/sessions/` (renamed from `tasks/`) -- tmux.py, manager.py, console.py,
  templates.py
- `cli/agentworks/cli.py` -- session command group, simplified interfaces
- `cli/agentworks/completions/` -- spec.py, bash.py, zsh.py, powershell.py
- `cli/agentworks/workspaces/manager.py` -- session references, SGID fix
- `cli/agentworks/workspaces/tmuxinator.py` -- simplified to use session name directly
- `cli/agentworks/workspaces/backends/vm.py` -- SGID on clone
- `cli/agentworks/agents/manager.py` -- session references, socket cleanup on reinit
- `cli/agentworks/vms/manager.py` -- session references
- `cli/agentworks/vms/initializer.py` -- socket cleanup on vm reinit
- `cli/agentworks/vms/backup.py` -- session references
- `cli/tests/test_tmuxinator.py` -- updated for SessionRow and new API
- `cli/tests/test_db.py` -- session_name in grant tests
- `cli/README.md` -- full rewrite

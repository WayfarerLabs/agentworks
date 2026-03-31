# User-Based Security Model -- Implementation Plan

## Completed

- [x] Admin user created during VM provisioning with sudo
- [x] Agent users created via `agent create` with derived Linux username
- [x] Workspace group (`ws-<name>`) created idempotently during agent creation
- [x] Agent users added to workspace group for shared file access
- [x] Agent templates: full per-user configuration (shell, dotfiles, git credentials, mise, etc.)
- [x] Agent lifecycle: create, list, shell, delete
- [x] Agent cleanup on workspace delete (cascade)

## Remaining

- [x] Create workspace group during workspace creation (not just agent creation)
- [x] Set workspace directory group to `ws-<name>` with setgid (`chgrp ws-<name> <dir>`,
  `chmod 2770 <dir>`) so files created by any agent are group-accessible to other agents
- [x] Ensure cloned repo files inherit the workspace group (setgid handles new files; initial
  clone needs `chgrp -R`)

## Not Planned (future SDDs)

- Agent workload identity (SPIFFE)
- Secret injection via command shims
- Jails / resource controls
- Network isolation

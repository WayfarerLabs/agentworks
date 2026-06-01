# Locked: 2026-03-29

## Summary

The user-based security model is implemented. This SDD was substantially rewritten on 2026-03-29 to
reflect the actual implementation, which diverged significantly from the original design.

### What was implemented

- Admin user with sudo, created during VM provisioning
- Agent users as isolated Linux users with workspace group membership
- Workspace groups with setgid for shared file access
- Admin added to all workspace groups
- Full agent templates: shell, dotfiles, git credentials, user install commands, mise
- Agent lifecycle: create, list, shell, delete with cascade on workspace delete

### What was dropped from the original design

- `aw-tools` group and `/opt/agentworks/tools/` directory (not needed)
- SSH agent daemon for credential sharing (not implemented)
- SUID-based credential access via nerfed commands (nerf tools use a different mechanism)
- Per-agent resource control via cgroups/ulimits (not implemented)

### Future work (separate SDDs)

- Agent workload identity (SPIFFE)
- Secret injection via command shims
- Jails / resource controls
- Network isolation

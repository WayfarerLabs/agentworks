# Locked: 2026-03-28

## Summary

The core agentworks SDD is complete. All planned phases have been implemented or explicitly
deferred.

### Completed

- **Phase 1**: VM workspaces and core CLI (provisioning, initialization, lifecycle, git credentials,
  workspace templates, shell completions, init resilience and logging)
- **Phase 2**: Local workspaces (delivered in Phase 1)
- **Phase 4**: Agents (database schema, lifecycle, tmuxinator integration, CLI commands)
- **Future: VM templates**: Implemented as `[vm_templates.*]` with inheritance, replacing the
  original `[vm.config]` section
- **Future: Workspace move**: Superseded by `workspace copy`

### Deferred

- **Phase 3: File templating**: Deferred indefinitely. Rulesync and dotfiles cover the primary use
  cases for now.

### Remaining future items (not planned)

- VM initialization plugins
- Agent install commands / agent templates
- Non-VM workspace hosts (Kubernetes, containers)
- Azure auto-suspend
- Auto-authentication

### Manual testing gaps

- E2E testing items in 1.13 and 4.6 are unchecked (manual, not code)

### Related SDDs

Work that continued from this SDD in separate feature directories:

- `2026-03-08-nerfed-commands` - nerfed agent commands
- `2026-03-08-user-based-security` - user isolation and RBAC
- `2026-03-15-install-enhancements` - installer catalog system
- `2026-03-16-resilient-provisioning` - provisioning error handling
- `2026-03-17-nerf-tools` - nerftools CLI
- `2026-03-23-tasks` - task management
- `2026-03-26-mise-integration` - mise tool manager, source refs, config restructuring

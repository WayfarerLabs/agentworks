# VM-Scoped Agents - Implementation Plan

## Definition of Done

- Agents are VM-scoped with `agt--<name>` usernames
- Workspace access controlled via explicit and implicit grants
- `agent create` takes `--vm`, not `--workspace`
- `agent grant-workspaces` and `agent deny-workspaces` commands work
- `--grant-all-workspaces` on create and `--all` on grant/deny work
- Task create auto-grants implicit workspace access
- Task delete cleans up implicit grants and group membership
- Workspace create adds grant_all agents to new workspace groups
- All existing tests pass, new tests for grant logic
- ADR-0006 superseded

## Phase 1: Database Schema

- [ ] Add migration: restructure agents table (`workspace_name` -> `vm_name`, new `grant_all`
  column, new primary key)
- [ ] Add migration: create `agent_workspace_grants` table
- [ ] Update `AgentRow` dataclass
- [ ] Add grant CRUD operations to `db.py`: `insert_grant`, `delete_grant`, `list_grants`,
  `has_any_grant`, `delete_grants_for_task`
- [ ] Update `insert_agent`, `get_agent`, `list_agents`, `delete_agent` for new schema

## Phase 2: Agent Manager Refactor

- [ ] Change `derive_linux_user()` to `agt--<name>`
- [ ] Update `create_agent()`: `--vm` instead of `--workspace`, optional
  `--grant-all-workspaces`. No workspace group setup at creation time.
- [ ] Update `_create_agent_on_vm()`: remove workspace group setup (moved to grant step)
- [ ] Update `delete_agent()`: remove agent from all workspace groups, delete all grants
- [ ] Update `reinit_agent()`: `--vm` instead of `--workspace`
- [ ] Update `list_agents()`: show VM, grant count
- [ ] Update `shell_agent()`: default to home dir, optional `--workspace` for cd
- [ ] Implement `grant_workspaces()`: add explicit grants, manage group membership
- [ ] Implement `deny_workspaces()`: remove explicit grants, clean up group membership

## Phase 3: Task Integration

- [ ] Update task create: when `--agent` is specified, check if agent has access to the workspace.
  If not, create implicit grant and add to group.
- [ ] Update task delete: remove implicit grant for the task, check if any remaining grants exist,
  remove group membership if none.

## Phase 4: Workspace Create Integration

- [ ] On VM workspace create: check for agents with `grant_all = true` on the VM, add them to the
  new workspace group, create explicit grant records.

## Phase 5: CLI and Completions

- [ ] Update `agent create` CLI: `--vm` instead of `--workspace`, `--grant-all-workspaces` flag
- [ ] Add `agent grant-workspaces` command (positional agent name, CSV workspaces or `--all`)
- [ ] Add `agent deny-workspaces` command (positional agent name, CSV workspaces or `--all`)
- [ ] Update `agent shell` CLI: optional `--workspace` flag
- [ ] Update `agent reinit` CLI: `--vm` instead of `--workspace` (or derive from DB)
- [ ] Update completions for all changed commands
- [ ] Update README

## Phase 6: Workspace Group Rename

- [ ] Change workspace group naming from `ws-<name>` to `ws--<name>` (double hyphen, consistent
  with `agt--<name>`)
- [ ] Update all references in: `agents/manager.py`, `workspaces/backends/vm.py`, grant logic
- [ ] Existing VMs/workspaces need manual fix (operator runs `groupmod -n ws--<name> ws-<name>`)

## Phase 7: ADR, Docs, and Review

- [ ] Supersede ADR-0006 with new ADR explaining the change
- [ ] Update security SDD (frd.md, hla.md, plan.md)
- [ ] Update idempotency guide if needed
- [ ] Review and update ALL documentation for accuracy: README, guides (mise, source-refs,
  config-migration, idempotency), sample config, all SDDs, all ADRs
- [ ] Verify no stale references to workspace-scoped agents, old username convention, or
  `ws-<name>` group naming

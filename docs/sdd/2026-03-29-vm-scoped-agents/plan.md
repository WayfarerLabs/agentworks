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

- [x] Add migration: restructure agents table (`workspace_name` -> `vm_name`, new `grant_all`
      column, new primary key)
- [x] Add migration: create `agent_workspace_grants` table
- [x] Update `AgentRow` dataclass
- [x] Add grant CRUD operations to `db.py`: `insert_grant`, `delete_grant`, `list_grants`,
      `has_any_grant`, `delete_grants_for_task`
- [x] Update `insert_agent`, `get_agent`, `list_agents`, `delete_agent` for new schema

## Phase 2: Agent Manager Refactor

- [x] Change `derive_linux_user()` to `agt--<name>`
- [x] Update `create_agent()`: `--vm` instead of `--workspace`, optional `--grant-all-workspaces`.
      No workspace group setup at creation time.
- [x] Update `_create_agent_on_vm()`: remove workspace group setup (moved to grant step)
- [x] Update `delete_agent()`: remove agent from all workspace groups, delete all grants
- [x] Update `reinit_agent()`: `--vm` instead of `--workspace`
- [x] Update `list_agents()`: show VM, grant count
- [x] Update `shell_agent()`: default to home dir, optional `--workspace` for cd
- [x] Implement `grant_workspaces()`: add explicit grants, manage group membership
- [x] Implement `deny_workspaces()`: remove explicit grants, clean up group membership

## Phase 3: Task Integration

- [x] Update task create: when `--agent` is specified, check if agent has access to the workspace.
      If not, create implicit grant and add to group.
- [x] Update task delete: remove implicit grant for the task, check if any remaining grants exist,
      remove group membership if none.

## Phase 4: Workspace Create Integration

- [x] On VM workspace create: check for agents with `grant_all = true` on the VM, add them to the
      new workspace group, create explicit grant records.

## Phase 5: CLI and Completions

- [x] Update `agent create` CLI: `--vm` instead of `--workspace`, `--grant-all-workspaces` flag
- [x] Add `agent workspace-grants grant` command (positional agent name, CSV workspaces or `--all`)
- [x] Add `agent workspace-grants deny` command (positional agent name, CSV workspaces or `--all`)
- [x] Add `agent workspace-grants list` command
- [x] Add `agent describe` command
- [x] Update `agent shell` CLI: optional `--workspace` flag
- [x] Update `agent reinit` CLI: derive VM from DB
- [x] Update completions for all changed commands
- [x] Update README

## Phase 6: Workspace Group Rename and Repair

- [x] Change workspace group naming from `ws-<name>` to `ws--<name>` (double hyphen, consistent with
      `agt--<name>`)
- [x] Update all references in: `agents/manager.py`, `workspaces/backends/vm.py`, grant logic
- [x] Implement `workspace repair` command for infrastructure reconciliation (group rename,
      permissions, ACLs, agent access)

## Phase 7: ADR, Docs, and Review

- [x] Supersede ADR-0006 with ADR-0010 explaining the change
- [x] Update security SDD (frd.md, hla.md, plan.md)
- [x] Update idempotency guide if needed
- [x] Review and update ALL documentation for accuracy: README, guides (mise, source-refs,
      config-migration, idempotency), sample config, all SDDs, all ADRs
- [x] Verify no stale references to workspace-scoped agents, old username convention, or `ws-<name>`
      group naming

# VM-Scoped Agents - High-Level Architecture

## Overview

Agents move from workspace-scoped (`<workspace>--<agent>`) to VM-scoped (`agt--<agent>`).
Workspace access is controlled through a grant system stored in the database. Linux group
membership enforces file access at the OS level.

## Database Changes

### Agents table

The `workspace_name` column is replaced by `vm_name`. The `linux_user` derivation changes.

```
agents:
  name TEXT
  vm_name TEXT           -- NEW (was workspace_name)
  linux_user TEXT        -- agt--<name> (was <workspace>--<name>)
  template TEXT
  created_at TEXT
```

Primary key changes from `(workspace_name, name)` to `(vm_name, name)`.
Foreign key changes from `workspaces(name)` to `vms(name)` with cascade delete.

### New: agent_workspace_grants table

```
agent_workspace_grants:
  agent_name TEXT
  vm_name TEXT
  workspace_name TEXT
  grant_type TEXT        -- 'explicit' or 'implicit'
  task_name TEXT         -- NULL for explicit grants, task name for implicit grants
  created_at TEXT
  PRIMARY KEY (agent_name, vm_name, workspace_name, grant_type, task_name)
  FOREIGN KEY (vm_name, agent_name) REFERENCES agents(vm_name, name) CASCADE
  FOREIGN KEY (workspace_name) REFERENCES workspaces(name) CASCADE
```

### Agent record: grant_all flag

A boolean `grant_all` column on the agents table. When true, the agent is automatically added to
all workspace groups (existing and newly created).

## Group Membership Flow

### On grant (explicit or implicit)

1. Look up the workspace to get the workspace group (`ws--<name>`)
2. Add the agent user to the group: `usermod -aG ws--<name> agt--<agent>`
3. Insert grant record into `agent_workspace_grants`

### On deny / task delete

1. Remove the specific grant record from `agent_workspace_grants`
2. Check if any remaining grants (explicit or implicit) exist for this agent + workspace
3. If no remaining grants, remove the agent from the workspace group:
   `gpasswd -d agt--<agent> ws--<name>`

### On workspace create (if agent has grant_all)

1. For each agent on the VM with `grant_all = true`:
   - Add to the new workspace's group
   - Insert an explicit grant record

## CLI Changes

### Modified commands

| Command | Change |
| --- | --- |
| `agent create` | `--workspace` replaced by `--vm` (prompted if multiple). New `--grant-all-workspaces` flag |
| `agent shell` | Default: agent home. Optional `--workspace` to cd into a workspace |
| `agent delete` | Removes from all workspace groups, deletes all grants |
| `agent list` | Shows VM instead of workspace. Shows grant count |
| `agent reinit` | `--workspace` replaced by `--vm` |

### New commands

| Command | Description |
| --- | --- |
| `agent grant-workspaces <agent> <workspaces>` | Grant explicit access to workspaces (CSV list) |
| `agent grant-workspaces <agent> --all` | Grant access to all workspaces |
| `agent deny-workspaces <agent> <workspaces>` | Remove explicit grants (CSV list) |
| `agent deny-workspaces <agent> --all` | Remove all explicit grants and clear grant_all flag |

### Task create changes

When creating a task with `--agent` in a workspace the agent doesn't have access to:

1. Create an implicit grant for that workspace
2. Add the agent to the workspace group
3. Proceed with task creation as normal

## Username Convention

- Old: `<workspace>--<agent>` (e.g., `myproject--coder`)
- New: `agt--<agent>` (e.g., `agt--coder`)

The `agt--` prefix:
- Avoids collision with system users
- Is immediately identifiable in process listings, logs, and audit trails
- Follows the existing double-hyphen naming convention

## Impact on Existing Code

### agents/manager.py

- `derive_linux_user()` changes from `<workspace>--<name>` to `agt--<name>`
- `create_agent()` takes `vm_name` instead of `workspace_name`
- `_create_agent_on_vm()` no longer sets up workspace group at creation (moved to grant)
- New functions: `grant_workspaces()`, `deny_workspaces()`

### workspaces/backends/vm.py

- On workspace create: check for agents with `grant_all` and add them to the new group

### tasks/manager.py

- On task create with `--agent`: check grants, add implicit grant if needed
- On task delete: remove implicit grant, clean up group membership if needed

### db.py

- New table and migration
- Agent CRUD updated for new schema
- New grant CRUD operations

### completions/spec.py

- New completions for `agent grant-workspaces` and `agent deny-workspaces`

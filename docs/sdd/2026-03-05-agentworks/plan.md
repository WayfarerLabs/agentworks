# Agentworks -- Implementation Plan

**Status:** Active **Branch:** `feat/agentworks-sdd` (specs), `main` (implementation)

---

## Phase 1: VM Workspaces + Core CLI

The initial implementation. Delivers a working CLI that can provision VMs, initialize them, and
manage workspaces on those VMs.

### 1.1 Project Scaffolding

- [x] Create `agentworks` repo with `cli/` subdirectory for the Python CLI
- [x] Set up `cli/pyproject.toml` with uv, Python 3.12+ target
- [x] Set up Typer CLI entrypoint (`cli.py`) with command groups: `vm-host`, `vm`, `workspace`
- [x] Set up linting/formatting (ruff, mypy)
- [x] Add basic README

**Definition of done:** `agentworks --help` runs and shows the command groups.

### 1.2 Configuration and State

- [x] Implement config loader (`config.py`): parse `~/.config/agentworks/config.toml`, validate
      structure, expand paths
- [x] Implement state database (`db.py`): SQLite schema creation, migrations, CRUD for `vm_hosts`,
      `vms`, `workspaces`, `vm_git_host_keys`
- [x] Write LLD: [config-db-lld.md](config-db-lld.md)
- [x] Ship sample config file (`sample-config.toml`) in repo, used by `init` command

**Definition of done:** Config loads from a sample TOML file. Database creates tables and
round-trips records.

### 1.3 SSH Execution Primitive

- [x] Implement `ssh.py`: run commands on a remote host via native `ssh` subprocess
- [x] Support: single command execution, file copy (scp/rsync), interactive shell
- [x] Respect user's SSH config and agent -- no custom key handling beyond what SSH provides

**Definition of done:** Can execute a command on a remote host and retrieve output. Can copy a file
to a remote host.

### 1.4 VM Host Management

- [x] Implement `vm-host add`, `vm-host list`, `vm-host remove` commands
- [x] OS auto-detection on first connect (via SSH)
- [x] Persist to state database

**Definition of done:** Can add a VM host, list it, remove it. OS is detected and stored.

### 1.5 VM Provisioning -- Platform Provisioners

- [x] Write LLD: [vm-provisioning-lld.md](vm-provisioning-lld.md)
- [x] Implement Lima provisioner (`provisioners/lima.py`): local and remote VM Host variants
  - [x] Lima template for Debian VM
  - [x] Local: run `limactl create` directly
  - [x] Remote: SSH to VM Host, run `limactl create`
- [x] Implement Azure provisioner (`provisioners/azure.py`):
  - [x] `az vm create` with Debian image and cloud-init
  - (Auto-suspend deferred to future phase -- see vm-provisioning-lld.md)
- [x] Implement WSL2 provisioner (`provisioners/wsl2.py`):
  - [x] PowerShell subprocess to import Debian distro
- [x] Implement `exec_target()` on all provisioners for Tailscale rejoin support

**Definition of done:** Each provisioner can create a raw VM on its platform. The VM is
SSH-accessible.

### 1.6 VM Initialization

- [x] Implement `initializer.py`: uniform post-provisioning setup over SSH (tracks `init_status` in
      DB)
- [x] Steps (Tailscale-first approach):
  - [x] Bootstrap (over provisioning transport): ensure user, apt system packages, add SSH key,
        Tailscale join
  - [x] Setup (over Tailscale SSH): apt user packages, snap, install commands, shell, SSH keypair,
        git host keys, dotfiles
- [x] Pre-flight auth verification for selected git host providers (fail-fast)
- [x] Implement `rejoin_tailscale()` for ephemeral Tailscale node recovery

**Definition of done:** A freshly provisioned VM is fully initialized and reachable over Tailscale.
SSH keys are registered with configured git hosts.

### 1.7 Git Host Providers

- [x] Write LLD: [git-hosts-lld.md](git-hosts-lld.md)
- [x] Implement `GitHostProvider` base interface (`git_hosts/base.py`)
- [x] Implement AzDO provider (`git_hosts/azdo.py`): register/remove key via REST API with Azure AD
      token
- [x] Implement GitHub provider (`git_hosts/github.py`): register/remove key via REST API with
      `gh cli` or PAT
- [x] Track registered keys in `vm_git_host_keys` table for clean removal on `vm delete`

**Definition of done:** SSH keys are registered with AzDO and GitHub during VM init. Keys are
removed on `vm delete`.

### 1.8 VM Lifecycle Commands

- [x] Implement `vm create`: orchestrates platform provisioning + initialization
- [x] Implement `vm list`: query state database
- [x] Implement `vm shell`: SSH into VM home directory
- [x] Implement `vm start`, `vm stop`: platform-specific with ephemeral Tailscale handling
- [x] Implement `vm delete`: stop VM, remove git host keys, platform-specific cleanup, remove from
      state database

**Definition of done:** Full VM lifecycle works end-to-end on at least one platform. Can create,
list, start, stop, delete.

### 1.9 Workspace Templates

- [x] Implement workspace template resolution (`templates.py`): `--template` flag, fall back to
      `default`, fall back to built-in empty
- [x] Implement workspace template inheritance: depth-first resolution, merge rules (booleans
      last-one-wins, lists append with dedup), cycle detection
- [x] Workspace template processing: create directory, optional git clone
- [x] Conditional tmuxinator config generation and symlink (based on resolved `tmuxinator` field)

**Definition of done:** Workspace creation with and without a repo workspace template works.
Inheritance resolves correctly. Tmuxinator config is generated only when enabled.

### 1.10 Workspace Lifecycle (VM and Local)

- [x] Implement `workspace create` for VM workspaces: remote steps over SSH + local
      `.code-workspace` generation
- [x] Implement `workspace create --local` for local workspaces (originally Phase 2, delivered
      early)
- [x] Implement `workspace shell`: SSH into VM with working directory set to workspace root.
      Auto-start deallocated Azure VMs (poll SSH readiness with timeout). Tmuxinator integration: if
      enabled, run `tmuxinator start <workspace-name>`; support `--no-tmuxinator` flag.
- [x] Implement `workspace list`: query state database, display type/VM/template/timestamp
- [x] Implement `workspace delete`: confirmation prompt, remote cleanup, local cleanup, state
      database removal
- [x] Implement `--open-vscode` flag on create
- [x] HTTPS clone failure hints for private repos (suggest SSH URL)

**Definition of done:** Can create a workspace on a VM, shell into it, list it, and delete it. VS
Code workspace file is generated and opens correctly.

### 1.11 Top-Level Commands

- [x] Implement `init` command: generates sample config from shipped `sample-config.toml`
- [x] Implement `doctor` command: environment health checks (Python, tools, Tailscale, config, SSH
      keys, DB schema, git host auth)
- [x] Implement `completion zsh` command: outputs zsh completion script

**Definition of done:** `init` creates a valid sample config. `doctor` reports environment health.
`completion zsh` outputs a working completion script.

### 1.12 Init Resilience and Logging

Improve VM initialization to handle non-fatal failures gracefully, capture init logs for
troubleshooting, and enforce clear states for failed VMs.

#### Init step classification

- **Fatal steps** (abort on failure): user creation, SSH key setup, Tailscale join. If these fail,
  the VM is unreachable and useless.
- **Non-fatal steps** (warn and continue): apt packages, snap packages, install commands, shell
  configuration, git host key registration, dotfiles. These can fail without making the VM unusable.

#### Init statuses

- `not_started` -- provisioned, not yet initialized
- `in_progress` -- currently running
- `completed` -- clean run, everything succeeded
- `partial` -- core succeeded, one or more non-fatal steps had warnings
- `failed` -- fatal step failed

#### Failed VM handling

VMs in `failed` state are unusable from an agentworks perspective. The only supported operation is
`vm delete`. On fatal init failure, prompt the user:

> Init failed. Delete VM? (You can keep it for manual troubleshooting, but agentworks cannot manage
> it.) [Y/n]

Default is yes (delete). If the user keeps it, all agentworks commands except `vm delete` refuse to
operate on it with a clear message pointing at the init log.

#### Partial VM handling

VMs in `partial` state are fully usable. Workspace and agent operations work normally. The `partial`
status serves as a reminder that something was skipped during init. `vm list` shows the status, and
`doctor` reports VMs in non-`completed` states.

#### Init logging

- Write init output to `~/.local/share/agentworks/logs/vm-init-<name>-<timestamp>.log`
- Capture both stdout and stderr from each init step, with step headers for readability
- On `completed`: keep the log (cheap, useful for debugging future issues)
- On `partial` or `failed`: print the log path so the user knows where to look
- `vm delete` cleans up associated log files

#### Tasks

- [x] Add `partial` to `init_status` enum (currently: `not_started`, `in_progress`, `completed`,
      `failed`)
- [x] Classify init steps as fatal vs non-fatal in `initializer.py`
- [x] Wrap non-fatal steps in try/except, collect warnings, continue on failure
- [x] Implement init log writer (structured output with step headers and timestamps)
- [x] On fatal failure: prompt to delete or keep, block subsequent operations on `failed` VMs
- [x] On partial completion: set `partial` status, print warnings summary and log path
- [x] Update `vm list` to show init status for non-`completed` VMs
- [x] Update `doctor` to report VMs in `partial` or `failed` state with log paths
- [x] Guard agentworks commands (workspace/agent ops) against `failed` VMs

**Definition of done:** Non-fatal init failures produce warnings and a `partial` status rather than
aborting. Fatal failures prompt for deletion. Init logs are captured and accessible for
troubleshooting.

### 1.13 End-to-End Testing

- [ ] Manual end-to-end test on Lima (local)
- [ ] Manual end-to-end test on Azure
- [ ] Manual end-to-end test on WSL2 (if Windows host available)
- [ ] Document known issues and gaps

**Definition of done:** Full workflow (vm create, workspace create, workspace shell, workspace
delete, vm delete) works on at least two platforms. Init resilience verified: non-fatal failures
produce `partial` status with log output.

---

## Phase 2: Local Workspaces (delivered in Phase 1)

Local workspaces were originally planned as a separate phase but were delivered alongside Phase 1.

### 2.1 Local Workspace Backend

- [x] Implement `LocalWorkspaceBackend` (`workspaces/backends/local.py`):
  - [x] Create directory under configurable local path (default: `~/workspaces/`)
  - [x] Apply workspace template (optional git clone, tmuxinator config)
  - [x] Shell access: open a new shell in the workspace directory
  - [x] Delete: remove workspace directory and tmuxinator symlink
- [x] Wire `--local` flag into `workspace create`
- [x] Generate local-path `.code-workspace` files (no SSH Remote)

**Definition of done:** Can create, shell into, list, and delete local workspaces. They appear
alongside VM workspaces in `workspace list`.

### 2.2 Unified Workspace Listing

- [x] `workspace list` shows both VM and local workspaces with type indicator
- [x] `workspace list --local` filters to local only
- [x] `workspace list --vm <name>` filters to specific VM

**Definition of done:** `workspace list` shows all workspaces regardless of type.

---

## Phase 3: File Templating

Adds file templating support to workspace templates.

### 3.1 Workspace Template File Processing

- [ ] Write LLD for workspace template file processing (templating language choice, variable model,
      processing order)
- [ ] Implement `files` section in workspace templates: copy files into workspace with variable
      substitution
- [ ] Define standard variables (workspace name, VM name, workspace template name, etc.)
- [ ] Use cases: VS Code settings, Claude Code permissions, editor configs, etc.

**Definition of done:** Workspace templates with a `files` section copy and process files into new
workspaces with variable substitution.

---

## Phase 4: Agents

Adds agent management to Agentworks. Agents are isolated Linux users within a workspace,
representing AI coding agents.

### 4.1 Name Validation Tightening

- [x] Update `NAME_RE` in `config.py`: pattern `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$` with no
      consecutive hyphens (`--`)
- [x] Remove dots from allowed characters (breaking change to existing names containing dots, if
      any)
- [x] Apply validation uniformly to VM hosts, VMs, workspaces, and agents
- [x] Add unit tests for name validation (valid names, rejected names, edge cases)

**Definition of done:** Names containing dots, double hyphens, or starting/ending with special
characters are rejected at creation time for all entity types. Existing entities with non-conforming
names still work (validation is at creation time only).

### 4.2 Agent Database Schema

- [x] Add `agents` table: name, workspace_name, linux_user, created_at
- [x] Primary key: `(workspace_name, name)`
- [x] Foreign key to `workspaces(name)` with cascade delete
- [x] Add migration from current schema version
- [x] Add CRUD operations to `db.py`: `insert_agent`, `list_agents`, `get_agent`, `delete_agent`,
      `delete_agents_for_workspace`

**Definition of done:** Agent records can be created, listed, queried, and deleted. Deleting a
workspace cascades to its agents in the database.

### 4.3 Agent Lifecycle Manager

- [x] Create `agents/manager.py` with `create_agent`, `delete_agent`, `list_agents` functions
- [x] Agent creation: validate name, derive Linux username (`<workspace>--<agent>`), SSH to VM to
      create Linux user and add to workspace group, insert DB record
- [x] Agent deletion: SSH to VM to kill processes and remove Linux user, remove DB record
- [x] Workspace delete integration: delete all agents before removing workspace directory

**Definition of done:** Agents can be created and deleted on a VM. Linux users are properly
provisioned with workspace group membership. Workspace deletion cascades to agents.

### 4.4 Tmuxinator Integration

- [x] Implement tmuxinator config regeneration: user window + one window per agent
- [x] Agent windows configured with `su - <agent-linux-user>` and workspace root as working
      directory
- [x] Regenerate on agent create and agent delete
- [x] Existing workspace creation continues to generate initial tmuxinator config (user window only,
      no agents yet)
- [x] Live session updates: add/remove windows in running tmux sessions (best-effort)

**Definition of done:** `workspace shell` opens a tmux session with a user window and one window per
agent. Adding or removing agents updates the tmuxinator config.

### 4.5 Agent CLI Commands

- [x] Implement `agent create <name> --workspace <workspace-name>`: orchestrates Linux user
      creation + DB
- [x] Implement `agent list [--workspace <workspace-name>]`: list agents, optionally filtered
- [x] Implement `agent shell <name> --workspace <workspace-name>`: SSH as user account, su to agent
      user
- [x] Implement `agent delete <name> --workspace <workspace-name>`: orchestrates Linux user
      removal + DB
- [x] Add shell completions for agent commands (workspace name completers for --workspace)

**Definition of done:** Full agent lifecycle works end-to-end. Can create an agent, see it in the
workspace tmuxinator session, shell into it, list agents, and delete it.

### 4.6 Agent Testing

- [x] Unit tests for name validation (double-hyphen rejection, Linux username derivation)
- [x] Unit tests for agent DB operations (CRUD, cascade delete)
- [x] Unit tests for tmuxinator config generation
- [ ] Manual end-to-end test: create workspace, add agents, verify tmuxinator windows, shell into
      agent, delete agent, delete workspace (verify cascade)

**Definition of done:** Agent lifecycle works correctly on at least one platform. Cascading deletion
verified.

---

## Future (Not Planned)

These items have architectural room in the current design but are not scheduled for implementation.

- **VM templates**: named VM configurations (packages, install commands, shell) that can be selected
  at `vm create` time, replacing the current single implicit default in `[vm.config]`
- **VM initialization plugins**: structured, reusable initialization steps (built-in and
  user-provided) that replace raw `install_commands` with declarative, version-aware building blocks
  (e.g. `install.bun` installs bun and writes `.bun-version`)
- **Non-VM Workspace Hosts**: Kubernetes StatefulSet pods as Workspace Hosts (`--platform k8s`),
  and/or container-based workspaces on existing VMs. When non-VM types ship, the `vm` CLI command
  group may be generalized to `host` or similar.
- **Workspace move**: `workspace move <name> --to <vm-name|local>` to relocate workspaces between
  VMs/local
- **Azure auto-suspend**: systemd timer on Azure VMs that deallocates after idle timeout (requires
  `az cli` auth on the VM -- authentication mechanism TBD)
- **Auto-authentication**: auto-authenticate tools (az cli, Claude Code, etc.) during VM
  initialization
- **Non-interactive Tailscale join**: avoid interactive prompt for Tailscale auth key

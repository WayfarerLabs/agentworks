# Agentworks -- High-Level Architecture

**Status:** Active **Repo:** `agentworks` (in upstreams) **Path:** `cli/`

---

## Overview

Agentworks is a Python CLI that orchestrates workspace lifecycle across multiple compute targets
(VMs and local host). Its execution model is built on two primitives -- local subprocess and remote
subprocess over SSH -- with a SQLite database tracking all state. The architecture is designed
around a uniform workspace abstraction that works regardless of where the workspace physically
lives.

The core abstraction is the **Workspace Host** -- an environment that can host workspaces. In the
current implementation, Workspace Hosts are always VMs, and the CLI/database/config use `vm`
terminology throughout. The architecture is designed so that non-VM Workspace Host types (K8s
StatefulSet pods, containers on VMs) can be added as new platform provisioners without changing the
workspace layer. When non-VM types ship, the `vm` command group may be generalized.

---

## Core Concepts

Agentworks organizes work into three layers. Each layer narrows the scope of the one above it --
permissions compose downward and can only constrain, never expand.

- **VM -- the environment**: defines the capability ceiling. The tools, runtimes, packages, and
  system configuration available to everything running inside it. This is the maximum set of
  possibility. Nothing below the VM layer can use a tool or capability that the VM does not provide.
- **Workspace -- the project**: defines the project scope. The repo(s) being worked on, plus the
  behavioral configuration that shapes how tools operate within this project (rulesync artifacts,
  workspace-level code assistant permissions, editor configs). A workspace narrows the VM's raw
  capability into a project-specific context. Workspaces can also live locally on the User
  Workstation, but local workspaces do not support agents.
- **Agent -- the actor**: defines a task-specific identity with scoped permissions. Each agent is an
  isolated Linux user within a VM workspace. The agent's effective capability is the intersection of
  all three layers: it can only use tools present on the VM, configured at the workspace level, and
  granted to the agent via RBAC (nerfed commands). Agents are VM-only because the isolation model
  requires Linux user management.

The layers also differ in **ephemerality**. VMs are long-lived -- provisioned once, used across many
projects. Workspaces are more ephemeral -- created per task or project, destroyed when done. Agents
are completely ephemeral -- spun up for a specific task within a workspace and discarded when the
task is complete.

Each layer has (or will have) a **templating mechanism** so that patterns can be defined once and
stamped many times. VM templates define what is installed and how the environment is configured.
Workspace templates define which repos are cloned and how tools are configured for the project.
Agent templates (future, dependent on nerfed commands) will define the permission model for
different agent roles.

This layering means the VM is provisioned once with all the tools anyone might need, workspaces
configure how those tools behave for a specific project, and agents operate within the intersection
of both. The architecture ensures that each layer can be reasoned about independently while the
security model guarantees that lower layers cannot exceed the constraints of higher ones.

---

## Topology

Agentworks's execution model uses two primitives:

- **Local subprocess**: provisioning operations that run on the User Workstation (Azure via
  `az cli`, WSL2 via PowerShell), and all local workspace operations
- **Remote subprocess over SSH**: provisioning operations that run on a VM Host (Lima via
  `limactl`), and all VM workspace operations which target the VM directly

After provisioning, the VM Host is only involved for VM lifecycle operations (start, stop, delete).
All workspace operations go directly from User Workstation to VM over Tailscale.

### Platform Matrix

| Platform      | VM Host          | Provisioning                    | VM Access                   |
| ------------- | ---------------- | ------------------------------- | --------------------------- |
| Lima (remote) | Remote SSH Host  | SSH into VM Host, run `limactl` | Direct to VM over Tailscale |
| Lima (local)  | User Workstation | Local `limactl`                 | Direct to VM over Tailscale |
| Azure         | _(none)_         | Local `az cli`                  | Direct to VM over Tailscale |
| WSL2          | _(none)_         | Local `wsl` + OCI rootfs import | Direct to VM over Tailscale |
| Local         | _(none)_         | _(none)_                        | Local filesystem/pushd      |

---

## Workspace Abstraction

All workspace types share a common identity model and lifecycle, but differ in how operations are
executed. The workspace manager dispatches to the appropriate backend based on workspace type.

```text
WorkspaceManager
  ├── VMWorkspaceBackend      -- operations via SSH to VM
  ├── LocalWorkspaceBackend   -- operations on local filesystem
  └── (future) ContainerWorkspaceBackend
```

Each backend implements the same interface: create directory, apply workspace template, inject
config, open shell, delete. The workspace manager handles state database operations, VS Code
workspace file generation, and identity management uniformly.

---

## State Database

`~/.config/agentworks/agentworks.db` (SQLite)

Tables:

- `vm_hosts`: name, ssh_host, platform, os
- `vms`: name, platform, vm_host_name, vm_user, cpus, memory, disk, extra_packages,
  provisioning_status, init_status, tailscale_host, azure_resource_id, created_at. Runtime status
  (running/stopped/deallocated) is always queried live from the platform, never cached.
- `workspaces`: name, type (vm/local), vm_name (nullable), template, workspace_path, created_at
- `agents`: name, workspace_name, linux_user (derived: `<workspace>--<agent>`), created_at
- `vm_events`: append-only lifecycle event log per VM (provisioning, initialization, errors)

Names are globally unique within each table (vm_hosts, vms, and workspaces are separate namespaces
-- a VM and a workspace can share the same name). Agent names are unique within their workspace; the
`(workspace_name, name)` pair forms the primary key.

---

## Git Credential Architecture

Git authentication uses `git credential-store` with personal access tokens (PATs) written to
`~/.git-credentials` on the VM. No SSH keys are generated during VM init.

Git credential providers are pluggable. Each provider implements:

```text
GitCredentialProvider
  verify_auth() -> bool
  auth_hint() -> str
  obtain_token() -> str
  credential_lines() -> list[str]
```

Providers are registered by type in the user config. During VM provisioning, tokens are collected
upfront -- either prompted interactively or read from `GIT_CREDENTIALS_<NAME>` environment variables
(with a console message when env vars are used). Tokens are written to `~/.git-credentials` and
`git credential-store` is configured globally.

### Provider Implementations

**AzDO**: uses a PAT (prompted or via `GIT_CREDENTIALS_AZDO`). Writes credential lines for
`dev.azure.com` and `ssh.dev.azure.com` HTTPS endpoints.

**GitHub**: uses a PAT (prompted or via `GIT_CREDENTIALS_GITHUB`). Writes a credential line for
`github.com`.

Additional providers can be added by implementing the interface and registering a new type.

### `vm add-git-credential`

Adds or rotates a git credential on an existing VM. Prompts for the token (or reads from env var),
updates `~/.git-credentials` on the VM.

---

## Workspace Templates

### Template Selection

1. Explicit `--template <name>` flag
2. The `default` workspace template if it exists in the user config
3. Built-in empty workspace template (`tmuxinator = true`, no repo)

### Inheritance Resolution

Workspace templates can inherit from multiple parents. Resolution is depth-first, left-to-right:

```text
resolve(template):
  result = empty
  for parent in template.inherits:
    merge(result, resolve(parent))
  merge(result, template)
  return result
```

Merge rules:

- **Booleans** (`tmuxinator`): last-one-wins -- child overrides parents
- **Strings** (`repo`): last-one-wins
- **Lists** (future `files`): append with dedup

Cycles in the inheritance graph are detected and rejected at config load time.

### Template Processing

After resolution, workspace creation applies the resolved workspace template:

1. Create workspace directory
2. If `repo` is set: `git clone <repo> <workspace-dir>`
3. If `tmuxinator` is enabled: write `.tmuxinator.yml` and symlink
4. (Future) Apply template file processing -- copy files with variable substitution

File templating (Phase 3) will add a `files` section to workspace templates for injecting
per-workspace files (VS Code settings, Claude Code permissions, editor configs, etc.).

---

## Agent Management

Agents are managed as Linux users on the VM that hosts their workspace. The agent manager handles
user provisioning, group membership, and tmuxinator config regeneration.

### Agent Creation Flow

```text
User runs: agentworks agent create coder --workspace ws-task-123

1. Validate agent name (naming conventions, no double hyphens)
2. Look up workspace in state database, resolve VM
3. SSH to VM:
   a. Create Linux user: useradd ws-task-123--coder
   b. Add user to workspace group: usermod -aG ws-task-123 ws-task-123--coder
   c. Create home directory with appropriate permissions
4. Regenerate workspace tmuxinator config (add agent window)
5. Insert agent record in state database
```

### Agent Shell Flow

```text
User runs: agentworks agent shell coder --workspace ws-task-123

1. Look up agent in state database, resolve workspace and VM
2. SSH to VM as user account
3. su - ws-task-123--coder
4. cd to workspace root (~/workspaces/ws-task-123)
```

### Agent Deletion Flow

```text
User runs: agentworks agent delete coder --workspace ws-task-123

1. Look up agent in state database
2. SSH to VM:
   a. Kill any processes owned by the agent user
   b. Remove Linux user and home directory: userdel -r ws-task-123--coder
3. Regenerate workspace tmuxinator config (remove agent window)
4. Remove agent record from state database
```

### Tmuxinator Integration

The workspace tmuxinator config is regenerated whenever agents are added or removed. The generated
config includes:

- A "user" window (the default) running as the user account in the workspace root
- One window per agent, each running `su - <agent-linux-user>` with the working directory set to the
  workspace root

This gives operators a single `workspace shell` entry point with visibility into all active agents.

### Cascading Deletion

When a workspace is deleted, all its agents are deleted first (Linux users removed, home directories
cleaned up, agent records removed from the database). This happens automatically as part of
`workspace delete` -- no separate agent deletion step is needed.

---

## VM Lifecycle Model

VM creation has two distinct phases with different characteristics:

- **Provisioning** -- platform-specific, one-time, pass/fail. Creates the VM, configures it for
  basic access, and gets it on Tailscale. If provisioning fails, the VM must be deleted and
  recreated.
- **Initialization** -- platform-agnostic, repeatable over Tailscale SSH. Installs user packages,
  configures dotfiles, writes git credentials, registers install commands, etc. Initialization can
  be re-run via `vm reinit` without reprovisioning.

This separation means a user who wants to change their dotfiles, add packages, or rotate git
credentials does not need to destroy and recreate the VM -- they can just reinitialize it. It also
means provisioning failures (platform-specific and harder to debug) are cleanly separated from
initialization failures (generic and easier to retry).

### Provisioning Flow

```text
User runs: agentworks vm create --platform lima --vm-host mac-studio

0. Collect secrets upfront: Tailscale auth key and git credential tokens
   (prompted, or read from TAILSCALE_AUTH_KEY / GIT_CREDENTIALS_* env vars)
1. Verify auth for selected git credential providers (fail-fast)
2. Platform provisioning (platform-specific):
   Lima: SSH to VM Host -> limactl create
   Azure: az vm create with cloud-init
   WSL2: import Debian rootfs, configure systemd, install base packages
3. Bootstrap (over provisioning transport):
   a. Ensure agentworks user exists
   b. apt install system dependencies
   c. Add user's public key to authorized_keys
   d. Tailscale join (using pre-collected auth key)
4. Mark provisioning complete, record Tailscale address
5. Automatically proceed to initialization
```

Provisioning is one-time and irreversible. If it fails, the only recovery is `vm delete` and
recreate.

### Initialization Flow

```text
Runs automatically after provisioning, or manually via: agentworks vm reinit <name>

All steps run over Tailscale SSH (platform-agnostic):
  1. apt install (user packages + extra packages)
  2. snap install (if any)
  3. Set default shell
  4. Run named install commands under the user's shell (e.g. zsh -lc '...')
  5. Configure PATH (~/.agentworks-path.sh sourced from ~/.profile)
  6. Write git credentials to ~/.git-credentials, configure git credential-store
  7. Copy and install dotfiles (if enabled and present)
  8. Mark initialization complete in state database
```

Initialization is designed to be idempotent and repeatable. `vm reinit` re-runs the full
initialization sequence on an already-provisioned VM. This is useful for applying config changes,
recovering from partial failures, or refreshing dotfiles and git credentials.

### Event Log

All lifecycle events are recorded in an append-only `vm_events` table. This provides a full audit
trail for debugging and diagnostics without cluttering the main status columns. Events include
provisioning start/complete/fail, initialization start/complete/fail, and individual step
completions. See [config-db-lld.md](config-db-lld.md) for the schema.

### Tailscale Rejoin (on vm start)

```text
1. Start VM via platform provisioner
2. If Tailscale host is stored: ping to check reachability
3. If unreachable (ephemeral node lost):
   a. Clear Tailscale host from DB
   b. Get provisioning transport via provisioner.exec_target()
   c. Re-join Tailscale (prompt for auth key or use TAILSCALE_AUTH_KEY)
   d. Store new Tailscale IP in DB
4. If no Tailscale host stored: same rejoin flow as (3)
```

---

## Workspace Creation Flow

### VM Workspace

```text
User runs: agentworks workspace create --vm dev-vm --template gruntweave --name ws-task-123

0. Resolve workspace template (walk inheritance, merge fields)

Remote (SSH to VM):
  1. mkdir ~/workspaces/ws-task-123
  2. git clone <template.repo> ~/workspaces/ws-task-123
  3. If tmuxinator enabled: write .tmuxinator.yml + symlink
  4. (Future) Apply template file processing

Local (User Workstation):
  5. Generate ws-task-123.code-workspace (SSH Remote target)
  6. Insert workspace record in state database
```

### Local Workspace

```text
User runs: agentworks workspace create --local --template gruntweave --name ws-task-456

0. Resolve workspace template (walk inheritance, merge fields)

Local (User Workstation):
  1. mkdir ~/workspaces/ws-task-456
  2. git clone <template.repo> ~/workspaces/ws-task-456
  3. If tmuxinator enabled: write .tmuxinator.yml + symlink
  4. (Future) Apply template file processing
  5. Generate ws-task-456.code-workspace (local path target)
  6. Insert workspace record in state database
```

---

## Tooling

| Concern              | Tool                                                           |
| -------------------- | -------------------------------------------------------------- |
| CLI framework        | Typer                                                          |
| Dependency/packaging | uv                                                             |
| SSH execution        | Native `ssh` subprocess (respects user's SSH config and agent) |
| Azure provisioning   | `az cli` subprocess                                            |
| Lima provisioning    | `limactl` subprocess (local or over SSH)                       |
| WSL2 provisioning    | `wsl` subprocess + Python OCI registry client                  |
| GitHub integration   | `gh cli` subprocess or REST API                                |
| User config format   | TOML (`tomllib` / `tomli-w`)                                   |
| Runtime state        | SQLite (`sqlite3` stdlib)                                      |
| Language             | Python 3.12+                                                   |

Agentworks runs natively on macOS, Linux, and Windows\*. WSL2 is not required or assumed on Windows.

\* Local workspaces are only supported on Unix-like hosts (macOS, Linux). Windows users must use WSL
or other remote VM solutions.

---

## Project Structure

```text
agentworks/                          # repo root (upstreams/agentworks)
├── cli/                             # Python CLI
│   ├── agentworks/
│   │   ├── cli.py                   # Typer entry point
│   │   ├── config.py                # user config loading/saving
│   │   ├── db.py                    # SQLite state management
│   │   ├── doctor.py                # environment/config health checks
│   │   ├── ssh.py                   # SSH execution primitive
│   │   ├── sample-config.toml       # sample config (used by init command)
│   │   ├── agents/
│   │   │   └── manager.py          # agent lifecycle (create/delete/list)
│   │   ├── completions/
│   │   │   ├── __init__.py          # public API: generate(shell)
│   │   │   ├── spec.py              # Typer introspection, CommandSpec tree
│   │   │   ├── zsh.py               # zsh completion generator
│   │   │   └── powershell.py        # PowerShell completion generator
│   │   ├── vm_hosts/
│   │   │   └── manager.py
│   │   ├── vms/
│   │   │   ├── base.py
│   │   │   ├── manager.py          # vm lifecycle commands (start/stop/shell)
│   │   │   ├── provisioners/
│   │   │   │   ├── lima.py
│   │   │   │   ├── azure.py
│   │   │   │   └── wsl2.py
│   │   │   └── initializer.py      # uniform VM init, platform-agnostic
│   │   ├── workspaces/
│   │   │   ├── manager.py          # workspace lifecycle orchestration
│   │   │   ├── backends/
│   │   │   │   ├── vm.py           # VM workspace backend (SSH operations)
│   │   │   │   └── local.py        # local workspace backend
│   │   │   └── templates.py        # workspace template resolution and processing
│   │   └── git_credentials/
│   │       ├── base.py             # GitCredentialProvider interface
│   │       ├── azdo.py             # AzDO provider
│   │       ├── github.py           # GitHub provider
│   │       └── prompt.py           # token prompting / env var resolution
│   ├── pyproject.toml
│   └── README.md
├── tools/                           # future: agent tools (MCP servers, etc.)
├── proxy/                           # future: tool proxy service
└── README.md
```

The repo is a monorepo where each component (`cli/`, `tools/`, `proxy/`) is self-contained with its
own language, toolchain, and dependencies. No shared build orchestrator -- each component manages
itself independently.

---

## Key Design Decisions

### SQLite for State

Runtime state is SQLite rather than flat files. This gives atomic operations, enforced uniqueness
constraints, and simple querying without introducing a server dependency. The database is local to
the User Workstation -- there is no shared state.

### SSH as the Universal Remote Primitive

All remote operations use native `ssh` subprocess calls rather than a Python SSH library. This
respects the user's SSH config, agent forwarding, and key management. It also means Agentworks works
with any SSH-accessible host without additional setup.

### Workspace Backends over Inheritance

Workspace types are implemented as backend strategies rather than subclasses. The workspace manager
owns lifecycle orchestration and state management; backends only handle the platform-specific
operations (create directory, clone repo, open shell, delete). This keeps the workspace identity
model and state management in one place.

### Git Credential Store over SSH Keys

Git authentication uses `git credential-store` with PATs rather than VM-generated SSH keypairs
registered with each provider. This eliminates SSH key lifecycle management (generation,
registration, tracking, removal on delete) and works with any git host that supports HTTPS token
auth. Credentials are written once during VM init and can be added/rotated via
`vm add-git-credential`.

### Named Install Commands

Install commands are defined as named top-level config sections (`[install_commands.*]`) with
`command` and `path` fields, rather than bare command strings. `vm.config.install_commands`
references these by name. This makes install commands reusable across VM templates (future) and
agent templates (future). PATH additions from `path` fields are accumulated and written to
`~/.agentworks-path.sh`, which is sourced from `~/.profile`. Install commands run under the user's
configured shell (e.g. `zsh -lc '...'`) so that installers write to the correct rc file.

### Open Questions

**Agent template compatibility with VM/workspace templates.** Agent templates (future) will
reference install commands from the same `[install_commands.*]` pool. Should an agent template only
be usable on VMs that have its required install commands already run? Should there be explicit
compatibility declarations between agent templates and VM templates, or is this left to the user to
manage?

### Provisioning Transport Reuse

Each platform provisioner exposes an `exec_target()` method that returns the provisioning transport
for an existing VM. This is used by the Tailscale rejoin flow to re-establish connectivity when a
Tailscale node is lost (e.g. ephemeral keys). The same transport used during initial provisioning is
reused, keeping the rejoin logic platform-agnostic.

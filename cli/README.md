# Agentworks CLI

CLI for orchestrating workspace lifecycle across multiple compute targets (VMs
and local host).

## Core Concepts

Agentworks organizes work into three layers. Each layer narrows the scope of the
one above it -- permissions compose downward and can only constrain, never
expand.

### VMs -- the environment

A VM defines the **capability ceiling**: the tools, runtimes, packages, and
system configuration available to everything running inside it. This is the
maximum set of possibility. Nothing below the VM layer can use a tool or
capability that the VM does not provide.

### Workspaces -- the project

A workspace defines the **project scope**: the repo(s) being worked on, plus the
behavioral configuration that shapes how tools operate within this project. This
includes rulesync artifacts (rules, skills), workspace-level code assistant
permissions (Claude Code, Copilot, etc.), and editor configs. A workspace
narrows the VM's raw capability into a project-specific context.

Workspaces can live on a VM or locally on the User Workstation. Local workspaces
do not support agents (see below).

### Agents -- the actor

An agent defines a **task-specific identity** with scoped permissions. Each
agent is an isolated Linux user within a workspace. The agent's effective
capability is the intersection of all three layers: it can only use tools
present on the VM, configured at the workspace level, and granted to the agent
via RBAC (nerfed commands). An agent cannot bypass a workspace-level permission
restriction or use a tool that is not installed on the VM.

Agents are only supported on VM workspaces because the isolation model requires
Linux user management (useradd, group membership, SUID executables).

### Ephemerality

The layers also differ in lifespan. VMs are long-lived -- provisioned once, used
across many projects. Workspaces are more ephemeral -- created per task or
project, destroyed when done. Agents are completely ephemeral -- spun up for a
specific task within a workspace and discarded when the task is complete.

### Templates

Each layer has (or will have) a templating mechanism so that patterns can be
defined once and stamped many times. VM templates define what is installed and
how the environment is configured. Workspace templates define which repos are
cloned and how tools are configured for the project. Agent templates (future,
dependent on nerfed commands) will define the permission model for different
agent roles.

## Getting Started

```bash
uv sync
uv run agentworks init       # creates ~/.config/agentworks/config.toml
```

Edit the config file (at minimum, set your SSH key paths), then:

```bash
agentworks vm create          # provision + initialize a VM
agentworks workspace create   # create a workspace on the VM
agentworks workspace shell my-workspace
```

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [Tailscale](https://tailscale.com/) installed and connected (for VM
  workspaces)
- One of: [Lima](https://lima-vm.io/), Azure CLI (`az`), or WSL2 (for VM
  provisioning)

## Commands

### Top-level

| Command                     | Description                  |
| --------------------------- | ---------------------------- |
| `agentworks doctor`         | Check environment and config |
| `agentworks init`           | Create a sample config file  |
| `agentworks completion zsh` | Output zsh completion script |

### VM Hosts

Manage machines that host VMs (for remote Lima mode).

| Command                                    | Description              |
| ------------------------------------------ | ------------------------ |
| `agentworks vm-host add <name> <ssh-host>` | Register a VM host       |
| `agentworks vm-host list`                  | List registered VM hosts |
| `agentworks vm-host remove <name>`         | Remove a VM host         |

### VMs

Manage virtual machines across Lima (local or remote), Azure, and WSL2.

| Command                       | Description                              |
| ----------------------------- | ---------------------------------------- |
| `agentworks vm create`        | Create a new VM (provision + initialize) |
| `agentworks vm list`          | List VMs with status and resources       |
| `agentworks vm shell <name>`  | SSH into a VM's home directory           |
| `agentworks vm start <name>`  | Start a stopped VM                       |
| `agentworks vm stop <name>`   | Stop a running VM                        |
| `agentworks vm delete <name>` | Delete a VM and clean up all resources   |

`vm create` accepts `--name`, `--platform`, `--vm-host`, `--vm-user`, `--cpus`,
`--memory`, `--disk`, `--azure-vm-size`, `--extra-packages`, and `--git-hosts`.
All have sensible defaults from config or built-in values.

### Workspaces

Manage workspaces on VMs or locally.

| Command                              | Description                          |
| ------------------------------------ | ------------------------------------ |
| `agentworks workspace create`        | Create a workspace (VM or `--local`) |
| `agentworks workspace shell <name>`  | Open a shell into a workspace        |
| `agentworks workspace list`          | List workspaces                      |
| `agentworks workspace delete <name>` | Delete a workspace                   |

`workspace create` accepts `--name`, `--vm`, `--local`, `--template`, and
`--open-vscode`.

### Agents

Manage agents (isolated Linux users) within VM workspaces.

| Command                                          | Description         |
| ------------------------------------------------ | ------------------- |
| `agentworks agent create <name> -w <workspace>`  | Create an agent     |
| `agentworks agent list [-w <workspace>]`         | List agents         |
| `agentworks agent shell <name> [-w <workspace>]` | Shell into an agent |
| `agentworks agent delete <name> -w <workspace>`  | Delete an agent     |

## Configuration

Config lives at `~/.config/agentworks/config.toml`. Run `agentworks init` to
generate a sample with all options documented. See
[sample-config.toml](agentworks/sample-config.toml) for the full reference.

Key sections:

- `[user]` -- SSH keys (required) and default shell
- `[paths]` -- local workspace and `.code-workspace` file directories
- `[defaults]` -- default platform, VM host, git hosts
- `[dotfiles]` -- dotfiles sync to VMs
- `[vm.config]` -- VM resources (cpus, memory, disk), packages, install
  commands, username
- `[workspace_templates.*]` -- workspace templates with inheritance
- `[git_hosts.*]` -- git host providers (GitHub, Azure DevOps)
- `[azure]` -- Azure-specific settings

## VM Initialization

VM creation follows a two-phase initialization:

1. **Phase A (Bootstrap)** -- over the provisioning transport (Lima shell, SSH,
   or WSL2 exec): create user, install system packages, add SSH key, install and
   join Tailscale

2. **Phase B (Setup)** -- over Tailscale SSH: install user packages, run install
   commands, set shell, generate SSH keypair, register keys with git hosts, sync
   dotfiles

## Tailscale

VMs join a Tailscale tailnet during initialization. All subsequent SSH access
(workspace shell, VM shell, Phase B setup) goes over Tailscale.

### Auth keys

During `vm create` (and `vm start` when re-joining), you will be prompted for a
Tailscale auth key unless the `TAILSCALE_AUTH_KEY` environment variable is set.
Generate keys at the
[Tailscale admin console](https://login.tailscale.com/admin/settings/keys).

### Ephemeral nodes

If you use an ephemeral auth key (one with `?ephemeral=true` appended), the
Tailscale node is automatically removed from the tailnet when the VM goes
offline. Agentworks handles this gracefully:

- **On stop**: checks whether the Tailscale node survived. If not, clears the
  stored IP so the next start knows to re-join.
- **On start**: verifies Tailscale connectivity. If the node is gone (ephemeral
  or otherwise), re-joins the tailnet via the provisioning transport (Lima
  shell, SSH, or WSL2 exec) and prompts for a new auth key (or uses
  `TAILSCALE_AUTH_KEY`).

This means ephemeral keys work fine for disposable VMs and are also resilient
across stop/start cycles. Non-ephemeral keys work without any re-joining.

### Cleanup on delete

`vm delete` performs a best-effort Tailscale logout via SSH before destroying
the VM. For ephemeral nodes this is a no-op since they auto-remove. For
non-ephemeral nodes, this deregisters the node from the tailnet.

## Shell Completion

```bash
mkdir -p ~/.zfunc
agentworks completion zsh > ~/.zfunc/_agentworks
```

Add to `.zshrc`:

```bash
fpath=(~/.zfunc $fpath)
autoload -Uz compinit && compinit
```

Completions include dynamic VM, workspace, and VM host name lookups.

## State

All state is stored in `~/.config/agentworks/agentworks.db` (SQLite). Schema
migrations are forward-only and run automatically.

## Environment Variables

| Variable             | Description                       |
| -------------------- | --------------------------------- |
| `TAILSCALE_AUTH_KEY` | Tailscale auth key (skips prompt) |

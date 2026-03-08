# Agentworks

CLI for orchestrating workspace lifecycle across multiple compute targets (VMs
and local host).

## Repository Structure

```text
cli/     Python CLI (uv, Python 3.12+)
tools/   Agent tools and MCP servers (future)
proxy/   Tool proxy service (future)
```

## Getting Started

```bash
cd cli
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

## Configuration

Config lives at `~/.config/agentworks/config.toml`. Run `agentworks init` to
generate a sample with all options documented. See
[sample-config.toml](cli/agentworks/sample-config.toml) for the full reference.

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

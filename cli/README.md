# Agentworks CLI

CLI for orchestrating workspace lifecycle across multiple compute targets (VMs and local host).

## Core Concepts

Agentworks organizes work into three layers. Each layer narrows the scope of the one above it --
permissions compose downward and can only constrain, never expand.

### VMs -- the environment

A VM defines the **capability ceiling**: the tools, runtimes, packages, and system configuration
available to everything running inside it. This is the maximum set of possibility. Nothing below the
VM layer can use a tool or capability that the VM does not provide.

### Workspaces -- the project

A workspace defines the **project scope**: the repo(s) being worked on, plus the behavioral
configuration that shapes how tools operate within this project. This includes rulesync artifacts
(rules, skills), workspace-level code assistant permissions (Claude Code, Copilot, etc.), and editor
configs. A workspace narrows the VM's raw capability into a project-specific context.

Workspaces can live on a VM or locally on the User Workstation. Local workspaces do not support
agents (see below).

### Agents -- the actor

An agent defines a **security identity** on a VM: a Linux user (`agt--<name>`) with its own shell,
tools, credentials, and dotfiles. Agents are VM-scoped and can be granted access to one or more
workspaces via explicit grants or implicitly through tasks. An agent's effective capability is the
intersection of all layers: it can only use tools present on the VM and access workspaces it has
been granted.

Agents are only supported on VM workspaces because the isolation model requires Linux user
management (useradd, group membership).

### Ephemerality

The layers also differ in lifespan. VMs are long-lived -- provisioned once, used across many
projects. Workspaces are more ephemeral -- created per task or project, destroyed when done. Agents
persist on a VM and can work across multiple workspaces and tasks. Tasks are the most ephemeral --
started, stopped, and deleted as work progresses.

### Templates

Each layer has (or will have) a templating mechanism so that patterns can be defined once and
stamped many times. VM templates define what is installed and how the environment is configured.
Workspace templates define which repos are cloned and how tools are configured for the project.
Agent templates define the per-user environment for agent users (shell, dotfiles, tools, credentials).

## Getting Started

```bash
uv sync
uv run agentworks config init    # creates ~/.config/agentworks/config.toml
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
- [Tailscale](https://tailscale.com/) installed and connected (for VM workspaces)
- One of: [Lima](https://lima-vm.io/), Azure CLI (`az`), or WSL2 (for VM provisioning)

## Global Options

| Flag                 | Description                    |
| -------------------- | ------------------------------ |
| `--non-interactive`  | Disable all interactive prompts |

When `--non-interactive` is set (or stdin is not a TTY), commands that would normally prompt for
missing values (VM selection, workspace selection, name generation) will fail with a clear error
indicating which flag is required. Auto-selection still works: if there is exactly one VM or
workspace, it is used without prompting.

## Commands

### Top-level

| Command                     | Description                  |
| --------------------------- | ---------------------------- |
| `agentworks doctor`         | Check environment and config |
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

| Command                                          | Description                                |
| ------------------------------------------------ | ------------------------------------------ |
| `agentworks vm create`                           | Create a new VM (provision + initialize)   |
| `agentworks vm list`                             | List VMs with status and resources         |
| `agentworks vm describe <name>`                  | Show VM details, workspaces, and event log |
| `agentworks vm shell <name>`                     | SSH into a VM's home directory             |
| `agentworks vm start <name>`                     | Start a stopped VM                         |
| `agentworks vm stop <name>`                      | Stop a running VM                          |
| `agentworks vm reinit <name>`                    | Re-run initialization on a provisioned VM  |
| `agentworks vm delete <name>`                    | Delete a VM (with confirmation)            |
| `agentworks vm logs <name>`                      | Show SSH logs for a VM                     |
| `agentworks vm console <name>`                   | Attach to the VM console                   |
| `agentworks vm add-git-credential <name> <cred>` | Add or update a git credential             |

`vm create` accepts `--name`, `--platform`, `--vm-host`, `--admin-username`, `--cpus`, `--memory`,
`--disk`, and `--azure-vm-size`. These are immutable provisioning parameters stored in the database.
All initialization behavior (packages, install commands, etc.) is driven by config.

`vm reinit` re-runs the initialization phase using the current config without reprovisioning the VM.
Changes to config (new packages, different install commands, etc.) are picked up automatically.

`vm delete` requires `--force` if the VM has workspaces, agents, or tasks. The confirmation
message shows what will be deleted. Pass `--yes` to skip the prompt.

### Workspaces

Manage workspaces on VMs or locally.

| Command                                   | Description                          |
| ----------------------------------------- | ------------------------------------ |
| `agentworks workspace create`             | Create a workspace (VM or `--local`) |
| `agentworks workspace describe <name>`    | Show workspace details and tasks     |
| `agentworks workspace shell <name>`       | Open a plain shell into a workspace  |
| `agentworks workspace console <name>`     | Open the workspace console (tmux)    |
| `agentworks workspace list`               | List workspaces                      |
| `agentworks workspace copy <source>`      | Copy a workspace to a new location   |
| `agentworks workspace rehome <name>`      | Move workspace to a new path         |
| `agentworks workspace repair <name>`      | Repair workspace infrastructure      |
| `agentworks workspace delete <name>`      | Delete a workspace                   |

`workspace create` accepts `--name`, `--vm`, `--local`, `--template`, and `--open-vscode`.

`workspace console` opens a tmuxinator session (`ws-<name>-console`) with an admin-shell
window plus one window per task in the workspace. Pass `--recreate` to kill and rebuild the
session. This is the recommended way to interact with tasks from within VS Code or any
terminal on the VM.

`workspace copy` copies a workspace to a new location. Accepts `--name`, `--vm`, and `--local`
(same pattern as `workspace create`). Works across VMs, VM to local, and local to VM.

`workspace delete` requires `--force` if the workspace has tasks. Running task sessions are
killed during deletion. Pass `--yes` to skip the confirmation prompt.

### Agents

Manage agents (isolated Linux users) on VMs. Agents are VM-scoped and access workspaces via grants.

| Command                                                    | Description                     |
| ---------------------------------------------------------- | ------------------------------- |
| `agentworks agent create [--name] [--vm]`                           | Create an agent on a VM         |
| `agentworks agent list [--vm <vm>]`                                 | List agents                     |
| `agentworks agent describe <name>`                                  | Show agent details and grants   |
| `agentworks agent reinit <name>`                                    | Re-run agent setup              |
| `agentworks agent workspace-grants grant <name> <ws>[,<ws>]`       | Grant workspace access          |
| `agentworks agent workspace-grants grant <name> --all`              | Grant access to all workspaces  |
| `agentworks agent workspace-grants deny <name> <ws>[,<ws>]`        | Remove workspace access         |
| `agentworks agent workspace-grants deny <name> --all`               | Remove all explicit grants      |
| `agentworks agent workspace-grants list <name>`                     | List workspace grants           |
| `agentworks agent shell <name> [--workspace <ws>]`                  | Shell into an agent             |
| `agentworks agent delete <name>`                                    | Delete an agent                 |

`agent create` accepts `--name`, `--vm`, `--template`, and `--grant-all-workspaces`.

`agent delete` requires `--force` if the agent has running tasks. Pass `--yes` to skip the
confirmation prompt.

### Tasks

Manage tasks (named work streams running in workspaces).

| Command                                          | Description                 |
| ------------------------------------------------ | --------------------------- |
| `agentworks task create`                         | Create and start a task     |
| `agentworks task describe <name> --workspace <ws>` | Show task details        |
| `agentworks task list [--workspace <ws>]`        | List tasks with status      |
| `agentworks task attach <name> --workspace <ws>` | Attach to a running task    |
| `agentworks task stop <name> --workspace <ws>`   | Stop a running task         |
| `agentworks task restart <name> --workspace <ws>` | Restart a task             |
| `agentworks task delete <name> --workspace <ws>` | Stop and delete a task      |
| `agentworks task logs <name> --workspace <ws>`   | Dump task scrollback buffer |
| `agentworks vm console <vm-name>`                | Attach to the VM console    |

`task create` accepts `--name`, `--workspace`, `--template`, `--admin`, and `--agent`. Workspace,
mode (admin vs agent), and name are prompted interactively if omitted. If agents exist on the VM
and neither `--admin` nor `--agent` is specified, you are prompted to choose. Pass `--new-workspace`
to create a workspace on the fly (with optional `--workspace-name`, `--workspace-template`, and
`--vm`). When a task created with `--new-workspace` is later deleted, you are offered the option
to delete the workspace as well (if no other tasks remain on it).

### tmux Architecture

Each task runs in its own locked-down tmux session on the VM. There are three ways to interact
with tasks, at different scopes:

| Method | Scope | Session name | Entry point |
| --- | --- | --- | --- |
| `task attach` | One task | `<workspace>--<task>` | Operator's machine |
| `workspace console` | One workspace | `ws-<workspace>-console` | On-VM or operator's machine |
| `vm console` | All workspaces | `vm-console` | Operator's machine |

#### Task sessions

Each task gets a locked-down tmux session (`<workspace>--<task>`). The admin user's
`~/.tmux.conf` (customizable via dotfiles) is loaded first so that familiar keybindings
(prefix, detach, copy mode, scroll) work for direct `task attach`. Window/pane creation,
session management, and the command prompt are selectively unbound.

#### Workspace console

`workspace console` uses tmuxinator to create or attach to a `ws-<name>-console` session.
The tmuxinator config (`.tmuxinator.yml` in the workspace root) is regenerated whenever tasks
change, so the session always reflects the current set of tasks. This is the recommended way
to interact with tasks from within VS Code or any terminal on the VM.

```text
ws-myproject-console (tmuxinator, full tmux)
  Window 1: admin-shell             login shell for the admin user
  Window 2: myproject--task-a       attached to task session
  Window 3: myproject--task-b       attached to task session
```

#### VM console

`vm console` creates or attaches to the `vm-console` session, which spans all workspaces on
the VM. This is built dynamically (not via tmuxinator) and is managed from the operator's
machine.

#### Shells

`workspace shell` and `vm shell` open plain login shells with no tmux. Use these when you just
need a terminal without the console structure.

#### Key behaviors

- **Direct attach** (`task attach`): the user's prefix key, detach, copy mode, and scroll all
  work normally. Status bar is hidden since there is only one pane.
- **Consoles** (`workspace console`, `vm console`): the console's prefix key eclipses the task
  session's prefix, so window switching, detach, etc. all operate at the console level. Task
  windows use a wrapper that re-attaches if the inner session disconnects and shows a message
  when the task ends.
- **Nesting protection**: both console commands refuse to run inside an existing tmux session to
  avoid prefix key conflicts. Pass `--allow-nesting` to override.
- **Console lifecycle**: consoles are independent of task sessions. Killing or detaching a
  console does not affect running tasks. `--recreate` rebuilds from scratch.

### Task Templates

Templates define the command a task runs. The built-in `default` template runs a login shell
(`$SHELL --login`), respecting whatever shell the user (admin or agent) is configured with.
Define custom templates in config:

```toml
[task_templates.default]               # override the built-in default
command = "claude --name {{task_name}}"
restart_command = "claude --resume {{task_name}}"
description = "Claude Code interactive session"
```

Template commands support `{{task_name}}` and `{{workspace_name}}` variable substitution
(double-brace syntax, consistent with nerftools manifests). The optional `restart_command` is
used by `task restart` -- useful for tools like Claude Code where `--resume` picks up the
previous conversation. If omitted, the regular `command` is used.

### Installers

Browse and inspect the built-in catalog of installable tools.

| Command                                | Description                        |
| -------------------------------------- | ---------------------------------- |
| `agentworks installer list`            | List all available catalog entries |
| `agentworks installer describe <name>` | Show details of a catalog entry    |

`installer list` accepts `--type` (apt-source, apt-package, system-install-cmd, user-install-cmd)
and `--source` (builtin, user) filters.

### Config

| Command                             | Description                            |
| ----------------------------------- | -------------------------------------- |
| `agentworks config init`            | Create a sample config file            |
| `agentworks config edit`            | Open config in `$EDITOR`               |
| `agentworks config sample`          | Print the sample config to stdout      |
| `agentworks config sync-ssh-config`        | Rebuild SSH config entries for all VMs       |
| `agentworks config sync-vscode-workspaces` | Regenerate .code-workspace files for all VMs |

## Configuration

Config lives at `~/.config/agentworks/config.toml`. Run `agentworks config init` to generate a
sample with all options documented. See [sample-config.toml](agentworks/sample-config.toml) for the
full reference.

Key sections:

- `[user]` -- SSH keys (required), additional authorized keys, SSH config management, default shell
- `[paths]` -- local workspace, VM workspace, and VS Code workspace file directories
- `[defaults]` -- default platform, VM host
- `[vm_templates.*]` -- VM resources, apt packages, system install commands, mise
- `[admin.config]` -- admin user shell, dotfiles, git credentials, user install commands, mise
- `[agent_templates.*]` -- agent user shell, dotfiles, git credentials, user install commands, mise
- `[task.config]` -- task defaults (history limit)
- `[task_templates.*]` -- task templates with variable substitution
- `[workspace_templates.*]` -- workspace templates with inheritance
- `[git_credentials.*]` -- git credential providers (GitHub, Azure DevOps)
- `[apt_sources.*]` -- user-defined third-party apt repositories
- `[apt_packages.*]` -- user-defined named apt package sets
- `[system_install_commands.*]` -- user-defined system-level install commands
- `[user_install_commands.*]` -- user-defined per-user install commands
- `[azure]` -- Azure-specific settings

### Mise (Polyglot Tool Manager)

Agentworks installs [mise](https://mise.jdx.dev/) by default on all VMs for managing CLI tools
(terraform, adr-tools, node, etc.) with optional lockfile-based integrity verification. See
[Using mise](../docs/guides/mise.md) for the full guide.

### Nerf Tools (Claude Code Plugin)

Agentworks can build and deploy a Claude Code plugin containing "nerf tools" -- scoped,
safety-constrained wrappers for CLI operations like git, az, and other tools. Nerf tools
enforce guardrails (validated parameters, restricted flags, pre-flight checks) so AI agents
operate safely.

Enable in your VM template:

```toml
[vm_templates.default]
nerf_build_claude_plugin = true
```

This builds the plugin to `nerf_home_dir/claude-plugin/` during VM init. To auto-install the
plugin for users, add to admin or agent config:

```toml
[admin.config]
nerf_install_claude_plugin = true
```

The plugin provides skills that document available tools, and operator commands for managing
permissions (`/nerftools:nerfctl-grant-allow`, `/nerftools:nerfctl-grant-deny`, etc.). Custom
tool manifests can be added via `nerf_addl_manifests`.

### Built-in Catalog

Agentworks ships a built-in catalog of common tools (apt sources, apt packages, system install
commands, and user install commands). Run `agentworks installer list` to see what is available.
Reference catalog entries by name in `vm_templates`, `admin.config`, and `agent_templates`.
User-defined entries in your config override built-in entries with the same name.

## VM Initialization

VM creation follows a two-phase lifecycle tracked by separate status columns:

1. **Provisioning** (`provisioning_status`) -- one-time, platform-specific, over the provisioning
   transport (Lima shell, SSH, or WSL2 exec): create user, install system packages, add SSH key,
   install and join Tailscale

2. **Initialization** (`init_status`) -- repeatable via `vm reinit`, over Tailscale SSH: configure
   apt sources, install apt packages, install snap packages, install mise, set shell, reconcile SSH
   authorized keys, run system install commands, write mise config, configure PATH, configure git
   credentials, sync dotfiles, fetch mise lockfile, run mise install, run user install commands for
   the admin user

Initialization is fully declarative -- driven entirely by config. `vm create` only accepts immutable
provisioning parameters (name, platform, resources). `vm reinit` takes only the VM name and re-runs
initialization using the current config.

Non-fatal initialization failures (packages, dotfiles) produce a `partial` status rather than
aborting. Fatal failures prompt for deletion or reinit. Use `vm describe` to view the full event
log.

## Tailscale

VMs join a Tailscale tailnet during initialization. All subsequent SSH access (workspace shell, VM
shell, Phase B setup) goes over Tailscale.

### Auth keys

During `vm create` (and `vm start` when re-joining), you will be prompted for a Tailscale auth key
unless the `TAILSCALE_AUTH_KEY` environment variable is set. Generate keys at the
[Tailscale admin console](https://login.tailscale.com/admin/settings/keys).

### Ephemeral nodes

If you use an ephemeral auth key (one with `?ephemeral=true` appended), the Tailscale node is
automatically removed from the tailnet when the VM goes offline. Agentworks handles this gracefully:

- **On stop**: checks whether the Tailscale node survived. If not, clears the stored IP so the next
  start knows to re-join.
- **On start**: verifies Tailscale connectivity. If the node is gone (ephemeral or otherwise),
  re-joins the tailnet via the provisioning transport (Lima shell, SSH, or WSL2 exec) and prompts
  for a new auth key (or uses `TAILSCALE_AUTH_KEY`).

This means ephemeral keys work fine for disposable VMs and are also resilient across stop/start
cycles. Non-ephemeral keys work without any re-joining.

### Cleanup on delete

`vm delete` performs a best-effort Tailscale logout via SSH before destroying the VM. For ephemeral
nodes this is a no-op since they auto-remove. For non-ephemeral nodes, this deregisters the node
from the tailnet.

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

Completions include dynamic VM, workspace, VM host, task, and template name lookups.

## State

All state is stored in `~/.config/agentworks/agentworks.db` (SQLite). Schema migrations are
forward-only and run automatically.

## Environment Variables

| Variable             | Description                       |
| -------------------- | --------------------------------- |
| `TAILSCALE_AUTH_KEY` | Tailscale auth key (skips prompt) |

# Agentworks CLI

The operator's command-line interface for managing agentic workloads on Agentworks.

For the project's problem space, core concepts, key principles, and tightly-integrated tool set, see
the [top-level README](../README.md). This document covers installing the CLI, the command surface,
configuration, and operational details.

## Getting Started

Install from PyPI:

```bash
uv tool install agentworks-cli
# or:  pipx install agentworks-cli
```

The package installs two entry points: `agentworks` is the canonical name (used in docs, error
messages, and anywhere the command needs to be unambiguous); `agw` is a short alias for the
keyboard. Use `agentworks` in writing and `agw` when you're typing.

```bash
agentworks config init                          # creates ~/.config/agentworks/config.toml
# edit the config; at minimum set your SSH key paths
agentworks vm create my-vm                      # provision + initialize a VM
agentworks workspace create my-workspace        # create a workspace on the VM
agentworks workspace shell my-workspace
```

## Prerequisites

- Python 3.12+ (uv will install one for you if needed)
- [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) for installation
- [Tailscale](https://tailscale.com/) installed and connected (for VM workspaces)
- One of: [Lima](https://lima-vm.io/), Azure CLI (`az`), or WSL2 (for VM provisioning)

## Global Options

| Flag                | Description                                                                  |
| ------------------- | ---------------------------------------------------------------------------- |
| `--non-interactive` | Disable all interactive prompts                                              |
| `--debug`           | Print the full Python traceback on unhandled errors (also via `AGW_DEBUG=1`) |

When `--non-interactive` is set (or stdin is not a TTY), commands that would normally prompt for
missing values (VM selection, workspace selection, name generation) will fail with a clear error
indicating which flag is required. Auto-selection still works: if there is exactly one VM or
workspace, it is used without prompting.

Domain errors (SSH timeouts, validation failures, missing resources, etc.) surface as a single clean
line: `Error: <message>`. Truly unexpected failures (internal bugs, OS-level errors, third-party
library failures) also get a clean single-line message, plus the full traceback appended to
`~/.config/agentworks/logs/error.log` for debugging. Pass `--debug` (or set `AGW_DEBUG=1`) to print
the traceback to stderr instead.

Pressing Ctrl-C during a long-running operation triggers best-effort cleanup. Where the operation
can roll back (e.g. `vm create` during the provisioning phase, `workspace create`, `agent create`,
`session create`) it undoes the partial DB / on-VM state and prints `Cancelling X... rolling back.`.
Where rollback isn't possible (`vm reinit`, `agent reinit`, the init phase of `vm create`) it prints
a recovery hint: the next command to run (`vm reinit`, `vm delete --force`, ...). Every cancellation
exits with the conventional SIGINT exit code (130).

## Commands

### Top-level

| Command                         | Description                            |
| ------------------------------- | -------------------------------------- |
| `agentworks doctor`             | Check environment and config           |
| `agentworks completion show`    | Print the completion script to stdout  |
| `agentworks completion install` | Install the completion script in-place |

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
| `agentworks vm create <name>`                    | Create a new VM (provision + initialize)   |
| `agentworks vm list`                             | List VMs with status and resources         |
| `agentworks vm describe <name>`                  | Show VM details, workspaces, and event log |
| `agentworks vm shell <name>`                     | SSH into a VM's home directory             |
| `agentworks vm start <name>`                     | Start a stopped VM                         |
| `agentworks vm stop <name>`                      | Stop a running VM                          |
| `agentworks vm reinit <name>`                    | Re-run initialization on a provisioned VM  |
| `agentworks vm delete <name>`                    | Delete a VM (with confirmation)            |
| `agentworks vm logs <name>`                      | Show SSH logs for a VM                     |
| `agentworks vm console <name>`                   | _Deprecated_: use `agentworks console`     |
| `agentworks vm add-git-credential <name> <cred>` | Add or update a git credential             |

`vm create <name>` takes the VM name as a required positional. Optional flags: `--platform`,
`--vm-host`, `--admin-username`, `--cpus`, `--memory`, `--disk`, and `--azure-vm-size`. These are
immutable provisioning parameters stored in the database. All initialization behavior (packages,
install commands, etc.) is driven by config.

`vm reinit` re-runs the initialization phase using the current config without reprovisioning the VM.
Changes to config (new packages, different install commands, etc.) are picked up automatically.

`vm delete` requires `--force` if the VM has workspaces, agents, or sessions. The confirmation
message shows what will be deleted. Pass `--yes` to skip the prompt.

### Workspaces

Manage workspaces on VMs.

| Command                                     | Description                         |
| ------------------------------------------- | ----------------------------------- |
| `agentworks workspace create <name>`        | Create a workspace on a VM          |
| `agentworks workspace describe <name>`      | Show workspace details and sessions |
| `agentworks workspace shell <name>`         | Open a plain shell into a workspace |
| `agentworks workspace console <name>`       | Open the workspace console (tmux)   |
| `agentworks workspace list`                 | List workspaces                     |
| `agentworks workspace copy <source> <name>` | Copy a workspace to a new VM        |
| `agentworks workspace rehome <name>`        | Move workspace to a new path        |
| `agentworks workspace reinit <name>`        | Reinit workspace infrastructure     |
| `agentworks workspace delete <name>`        | Delete a workspace                  |

`workspace create <name>` takes the workspace name as a required positional. Optional flags: `--vm`,
`--template`, and `--open-vscode`.

`workspace console` opens a tmuxinator session (`ws-<name>-console`) with an admin-shell window plus
one window per session in the workspace. Pass `--recreate` to kill and rebuild the console. This is
the recommended way to interact with sessions from within VS Code or any terminal on the VM.

`workspace copy <source> <name>` copies a workspace to a new VM workspace. Accepts `--vm`. Source
and destination can be the same VM (a clone) or different VMs.

`workspace delete` requires `--force` if the workspace has sessions. Running sessions are killed
during deletion. Pass `--yes` to skip the confirmation prompt.

### Agents

Manage agents (isolated Linux users) on VMs. Agents are VM-scoped and access workspaces via grants.

| Command                                            | Description                    |
| -------------------------------------------------- | ------------------------------ |
| `agentworks agent create <name> [--vm]`            | Create an agent on a VM        |
| `agentworks agent list [--vm <vm>]`                | List agents                    |
| `agentworks agent describe <name>`                 | Show agent details and grants  |
| `agentworks agent reinit <name>`                   | Re-run agent setup             |
| `agentworks agent grant-workspace <name> <ws>...`  | Grant workspace access         |
| `agentworks agent grant-workspace <name> --all`    | Grant access to all workspaces |
| `agentworks agent revoke-workspace <name> <ws>...` | Revoke workspace access        |
| `agentworks agent revoke-workspace <name> --all`   | Revoke all explicit grants     |
| `agentworks agent shell <name> [--workspace <ws>]` | Shell into an agent            |
| `agentworks agent delete <name>`                   | Delete an agent                |

`agent create <name>` takes the agent name as a required positional. Optional flags: `--vm`,
`--template`, and `--grant-all-workspaces`.

`agent delete` requires `--force` if the agent has running sessions. Pass `--yes` to skip the
confirmation prompt.

### Sessions

Manage sessions (persistent tmux sessions running in workspaces). Session names are globally unique
-- no `--workspace` flag needed for most commands.

| Command                                      | Description                    |
| -------------------------------------------- | ------------------------------ |
| `agentworks session create <name>`           | Create and start a session     |
| `agentworks session describe <name>`         | Show session details           |
| `agentworks session list [--workspace <ws>]` | List sessions with status      |
| `agentworks session attach <name>`           | Attach to a running session    |
| `agentworks session stop <name>`             | Stop a running session         |
| `agentworks session restart <name>`          | Restart a session              |
| `agentworks session delete <name>`           | Stop and delete a session      |
| `agentworks session logs <name>`             | Dump session scrollback buffer |
| `agentworks console attach <name>`           | Attach to a named console      |

`session create <name>` takes the session name as a required positional. Optional flags:
`--workspace`, `--template`, `--admin`, and `--agent`. Workspace and mode (admin vs agent) are
prompted interactively if omitted; if agents exist on the VM and neither `--admin` nor `--agent` is
specified, you are prompted to choose. Pass `--new-workspace` to create a workspace on the fly (with
optional `--workspace-name`, `--workspace-template`, and `--vm`; `--workspace-name` defaults to the
session name). Pass `--new-agent` to create a new agent for the session (with optional
`--agent-name` and `--agent-template`; `--agent-name` defaults to the session name); the new agent
is provisioned on the workspace's VM. When a session created with `--new-workspace` or `--new-agent`
is later deleted, you are offered the option to delete the workspace and/or agent as well -- the
workspace if no other sessions remain on it, the agent if it has no other sessions and no explicit
grants.

<!-- Linked from the top-level README; rename only if you also update README.md. -->

### Named consoles

Named consoles are persistent, curated tmux views over sessions on a VM. Each console is its own
tmux session (`aw-console-<name>`) containing one window per included session, plus any extra shell
panes you want preloaded into a session's window.

| Command                                                  | Description                                                       |
| -------------------------------------------------------- | ----------------------------------------------------------------- |
| `agentworks console create <name> [sessions...]`         | Create a console with the given sessions                          |
| `agentworks console list`                                | List consoles                                                     |
| `agentworks console describe <name>`                     | Show membership and shell layout                                  |
| `agentworks console attach <name>`                       | Attach (builds tmux state on first attach)                        |
| `agentworks console delete <name>`                       | Tear down and remove the console                                  |
| `agentworks console add-session <name> <sessions...>`    | Add session windows                                               |
| `agentworks console remove-session <name> <sessions...>` | Remove session windows                                            |
| `agentworks console add-shell <name> <session>`          | Add a shell pane to a session window (accepts `--cwd`, `--admin`) |

`console create` accepts:

- `--vm` -- target VM. **Inferred from the listed sessions when omitted**; if the listed sessions
  span more than one VM, `console create` errors and asks you to pick one with `--vm`. When no
  sessions are listed (e.g. with `--all` and no explicit specs), VM selection falls back to the
  standard prompt (auto-picked if you have a single VM, prompted otherwise).
- `--all` -- include every session on the VM with 0 shells, appended after the explicit specs
  (alphabetical).
- `--all-running` -- like `--all` but restricted to sessions whose live tmux state on the VM is OK
  (one SSH round-trip; same probe `aw session list` uses). Mutually exclusive with `--all`. Requires
  the VM to be reachable.
- `--add-admin-shell` -- include a top-level admin-shell window as window 0, matching the legacy
  `vm console` behavior.

`console list` accepts `--vm` to filter.

Session specs use `name` or `name+N` shorthand, where `N` is the number of default shell panes to
pre-open in that session's window (running as the session's agent user, cwd = workspace root):

```sh
# A console with three sessions; the first two get extra shells.
# VM is inferred from the sessions.
agentworks console create backend auth-server+2 auth-tests+1 docs

# Same, but also include a top-level admin-shell window (window 0).
agentworks console create backend auth-server+2 auth-tests+1 docs --add-admin-shell

# Everything currently running on the VM, after the explicit specs.
agentworks console create live auth-server+2 --all-running

# All sessions on the VM (running or not). Needs --vm since no sessions are
# named explicitly to infer from.
agentworks console create everything --vm aw-private --all

# Add an admin shell rooted in a sub-path of the workspace.
agentworks console add-shell backend auth-server --cwd src/api --admin
```

Memberships and shell layouts persist in the database. `aw console attach` builds the tmux session
on first attach (or with `--recreate`); subsequent attaches reuse the running tmux session. Adding
or removing sessions/shells while a console is attached updates the live tmux state immediately
(best-effort); when the console isn't running on the VM, only the DB is updated and changes appear
on next attach.

<!-- Linked from the top-level README; rename only if you also update README.md. -->

### tmux Architecture

Each session runs in its own locked-down tmux session on the VM. There are several ways to interact
with sessions, at different scopes:

| Method                    | Scope                            | tmux session name        | Entry point                 |
| ------------------------- | -------------------------------- | ------------------------ | --------------------------- |
| `session attach`          | One session                      | `<session-name>`         | Operator's machine          |
| `workspace console`       | One workspace                    | `ws-<workspace>-console` | On-VM or operator's machine |
| `console`                 | Curated subset across workspaces | `aw-console-<name>`      | Operator's machine          |
| `vm console` (deprecated) | All sessions on the VM           | `vm-console`             | Operator's machine          |

#### Session tmux sessions

Each session gets a locked-down tmux session using the session name directly as the tmux session
name. The user's `~/.tmux.conf` (customizable via dotfiles) is loaded first so that familiar
keybindings (prefix, detach, copy mode, scroll) work for direct `session attach`. Window/pane
creation, session management, and the command prompt are selectively unbound.

Agent-mode sessions run on a per-agent tmux socket so the agent's shell connects directly to the
tmux pane PTY. The socket path is persisted in the database.

#### Workspace console

`workspace console` uses tmuxinator to create or attach to a `ws-<name>-console` session. The
tmuxinator config (`.tmuxinator.yml` in the workspace root) is regenerated whenever sessions change,
so the console always reflects the current set of sessions. Best for in-VM work scoped to a single
workspace (e.g. inside VS Code's integrated terminal). For curated views that span workspaces, use a
named console (`console attach <name>`).

```text
ws-myproject-console (tmuxinator, full tmux)
  Window 1: admin-shell                login shell for the admin user
  Window 2: myproject-claude           attached to session
  Window 3: myproject-debug            attached to session
```

#### Named console

`console attach <name>` creates or attaches to the `aw-console-<name>` tmux session. Membership and
per-session shell layout are stored in the database. Each member session becomes a window running
the same wrapper used by the workspace and VM consoles, plus a configurable number of extra shell
panes (default user = session's agent user, default cwd = workspace root; override per pane with
`--cwd` / `--admin` on `console add-shell`).

```text
aw-console-backend
  Window 1: auth-server                attached session + 2 agent shells (workspace root)
  Window 2: auth-tests                 attached session + 1 agent shell
  Window 3: docs                       attached session only
```

The tmux session is built lazily on first `attach` (or rebuilt with `--recreate`). Adding or
removing sessions/shells while the console is attached updates tmux immediately; when offline, only
the DB is touched and changes appear on next attach. The mutation commands (`add-session`,
`remove-session`, `add-shell`) never auto-boot the VM; the explicit attach/repair commands
(`attach`, `restore-session`) do start a stopped VM, since their job is to bring live state up.

#### VM console (deprecated)

`vm console` creates or attaches to the `vm-console` session, which spans all sessions on the VM.
Built dynamically (not via tmuxinator). Superseded by named consoles, which let you curate which
sessions are in scope at any moment instead of seeing every session on the VM. Will be removed in a
future release.

#### Shells

`workspace shell` and `vm shell` open plain login shells with no tmux. Use these when you just need
a terminal without the console structure.

#### Key behaviors

- **Direct attach** (`session attach`): the user's prefix key, detach, copy mode, and scroll all
  work normally. Status bar is hidden since there is only one pane.
- **Consoles** (`workspace console`, `vm console`): the console's prefix key eclipses the inner
  session's prefix, so window switching, detach, etc. all operate at the console level. Session
  windows use a wrapper that re-attaches if the inner session disconnects and shows a message when
  the session ends.
- **Nesting protection**: both console commands refuse to run inside an existing tmux session to
  avoid prefix key conflicts. Pass `--allow-nesting` to override.
- **Console lifecycle**: consoles are independent of sessions. Killing or detaching a console does
  not affect running sessions. `--recreate` rebuilds from scratch.

### Session Templates

Templates define the command a session runs. The built-in `default` template runs a login shell
(`$SHELL --login`), respecting whatever shell the user (admin or agent) is configured with. Define
custom templates in config:

```toml
[session_templates.default]            # override the built-in default
command = "claude --name {{session_name}}"
restart_command = "claude --resume {{session_name}}"
description = "Claude Code interactive session"
```

Template commands support `{{session_name}}` and `{{workspace_name}}` variable substitution
(double-brace syntax, consistent with nerftools manifests). The optional `restart_command` is used
by `session restart` -- useful for tools like Claude Code where `--resume` picks up the previous
conversation. If omitted, the regular `command` is used.

### Catalog

Browse and inspect the built-in catalog of installable tools.

| Command                              | Description                        |
| ------------------------------------ | ---------------------------------- |
| `agentworks catalog list`            | List all available catalog entries |
| `agentworks catalog describe <name>` | Show details of a catalog entry    |

`catalog list` accepts `--type` (apt-source, apt-package, system-install-cmd, user-install-cmd) and
`--source` (built-in, custom) filters.

### Config

| Command                                    | Description                                  |
| ------------------------------------------ | -------------------------------------------- |
| `agentworks config init`                   | Create a sample config file                  |
| `agentworks config edit`                   | Open config in `$EDITOR`                     |
| `agentworks config sample`                 | Print the sample config to stdout            |
| `agentworks config sync-ssh-config`        | Rebuild SSH config entries for all VMs       |
| `agentworks config sync-vscode-workspaces` | Regenerate .code-workspace files for all VMs |

## Configuration

Config lives at `~/.config/agentworks/config.toml`. Run `agentworks config init` to generate a
sample with all options documented. See [sample-config.toml](agentworks/sample-config.toml) for the
full reference.

Key sections:

- `[operator]` -- SSH keys (required), additional authorized keys, SSH config management
- `[paths]` -- VM workspace and VS Code workspace file directories
- `[defaults]` -- default platform, VM host
- `[vm_templates.*]` -- VM resources, apt packages, system install commands, mise
- `[admin.config]` -- admin user shell, dotfiles, git credentials, user install commands, mise
- `[agent_templates.*]` -- agent user shell, dotfiles, git credentials, user install commands, mise
- `[session.config]` -- session defaults (history limit)
- `[session_templates.*]` -- session templates with variable substitution
- `[workspace_templates.*]` -- workspace templates with inheritance
- `[named_console]` -- named-console layout (tmux preset name)
- `[git_credentials.*]` -- git credential providers (GitHub, Azure DevOps)
- `[apt_sources.*]` -- user-defined third-party apt repositories
- `[apt_packages.*]` -- user-defined named apt package sets
- `[system_install_commands.*]` -- user-defined system-level install commands
- `[user_install_commands.*]` -- user-defined per-user install commands
- `[azure]` -- Azure-specific settings
- `[proxmox]` -- Proxmox VE API settings

### Mise (Polyglot Tool Manager)

Agentworks installs [mise](https://mise.jdx.dev/) by default on all VMs for managing CLI tools
(terraform, adr-tools, node, etc.) with optional lockfile-based integrity verification. See
[Using mise](../docs/guides/mise.md) for the full guide.

### Nerf Tools (Claude Code Plugin)

Agentworks can build and deploy a Claude Code plugin containing "nerf tools" -- scoped,
safety-constrained wrappers for CLI operations like git, az, and other tools. Nerf tools enforce
guardrails (validated parameters, restricted flags, pre-flight checks) so AI agents operate safely.

Enable in your VM template:

```toml
[vm_templates.default]
nerf_build_claude_plugin = true
```

This builds the plugin to `nerf_home_dir/claude-plugin/` during VM init. To auto-install the plugin
for users, add to admin or agent config:

```toml
[admin.config]
nerf_install_claude_plugin = true
```

The plugin provides skills that document available tools, and operator commands for managing
permissions (`/nerftools:nerfctl-grant-allow`, `/nerftools:nerfctl-grant-deny`, etc.). Custom tool
manifests can be added via `nerf_addl_manifests`.

Plugin identity (name, marketplace metadata) is defined in agentworks' own `nerf-config.yaml` and
loaded via the nerftools config API. The version is a date-based build stamp that changes on each
reinit. The build always emits an embedded marketplace so the plugin directory is installable
standalone via `claude plugin marketplace add`.

### Built-in Catalog

Agentworks ships a built-in catalog of common tools (apt sources, apt packages, system install
commands, and user install commands). Run `agentworks catalog list` to see what is available.
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

## Shell Completion

```bash
agentworks completion install
```

The shell is autodetected from `$SHELL`; pass `--shell {bash|zsh|powershell}` to override (or when
autodetection isn't unambiguous, e.g. on Windows). `completion install` writes the script to the
standard location for that shell. For PowerShell it also appends a dot-source line (`. "..."`) to
`$PROFILE`. For bash and zsh, if your rc file is missing the loader (`bash-completion` for bash,
`fpath=(~/.zfunc $fpath)` for plain zsh without a plugin manager), the installer prints a one-line
note telling you what to add.

To print the script without installing, use `agentworks completion show` (handy for piping into your
own config-management flow).

Completions include dynamic VM, workspace, VM host, session, and template name lookups.

## State

All state is stored in `~/.config/agentworks/agentworks.db` (SQLite). Schema migrations are
forward-only and run automatically.

## Environment Variables

| Variable                      | Description                                     |
| ----------------------------- | ----------------------------------------------- |
| `TAILSCALE_AUTH_KEY`          | Tailscale auth key (skips prompt)               |
| `GIT_CREDENTIALS_<CRED_NAME>` | Git credential for `<CRED_NAME>` (skips prompt) |

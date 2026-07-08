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

The everyday command is `agw`. The longer form `agentworks` is also installed if you ever want to
type it out; examples throughout this document use `agw`.

```bash
# Initial setup
agw config init                          # creates ~/.config/agentworks/config.toml
agw config edit                          # opens the config in your $EDITOR (or $VISUAL) to fill in required fields
agw doctor                               # sanity-checks installed tools, Tailscale, config validity, and the local DB

# Create a VM, workspace, agent, and session to see how the pieces fit together
agw vm create my-vm
agw workspace create my-workspace --vm my-vm
agw agent create my-agent --vm my-vm
agw session create my-session --workspace my-workspace --agent my-agent

# Attach to the session's tmux session to drive it
agw session attach my-session
# Use tmux's 'detach' command (default Ctrl-b unless overridden by config) to disconnect while
# leaving everything running on the VM.
agw session attach my-session    # You'll pick up right where you left off
agw session stop my-session      # Sessions can be stopped (or can exit on their own)
agw session list
agw session restart my-session
agw session attach my-session
agw session delete my-session    # When you're done with it. Agent and workspace are preserved.

# Alternatively, you can create ephemeral workspaces and agents along with your sessions
agw session create my-ephemeral-session --vm my-vm --new-workspace --new-agent
agw session attach my-ephemeral-session
agw session delete my-ephemeral-session    # This will prompt you to delete the associated workspace and agent, too

# Finally, create two sessions and a named console
agw session create s1 --vm my-vm --new-workspace --new-agent
agw session create s2 --vm my-vm --new-workspace --new-agent
agw console create my-console s1 s2+1      # The + syntax gives you extra shells as that agent
agw console attach my-console
agw console delete my-console              # Extra shells are lost but sessions are preserved
```

## Prerequisites

- Python 3.12+ (uv will install one for you if needed)
- [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) for installation
- [Tailscale](https://tailscale.com/) installed and connected (for VM workspaces)
- One of: [Lima](https://lima-vm.io/), Azure CLI (`az`), [Proxmox](https://www.proxmox.com/), or
  WSL2 (for VM provisioning)

## Global Options

| Flag                | Description                                                                  |
| ------------------- | ---------------------------------------------------------------------------- |
| `--non-interactive` | Disable all interactive prompts                                              |
| `--debug`           | Print the full Python traceback on unhandled errors (also via `AGW_DEBUG=1`) |
| `--no-deprecations` | Suppress deprecation warnings (e.g. the TOML resource-section nudge)         |

When `--non-interactive` is set (or stdin is not a TTY), commands that would normally prompt for
missing values (VM selection, workspace selection, name generation) will fail with a clear error
indicating which flag is required. VM auto-selection still works: if there is exactly one usable VM,
it is used without prompting. `session create` is an intentional exception -- it always prompts for
workspace and mode (even when only one choice exists) since those are part of the session's identity
and should be an explicit operator decision.

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

| Command                    | Description                              |
| -------------------------- | ---------------------------------------- |
| `agw doctor`               | Check environment and config             |
| `agw completion show`      | Print the completion script to stdout    |
| `agw completion install`   | Install the completion script in-place   |
| `agw completion uninstall` | Remove installed completions for a shell |

### VM Hosts

Manage machines that host VMs (for remote Lima mode).

| Command                             | Description              |
| ----------------------------------- | ------------------------ |
| `agw vm-host add <name> <ssh-host>` | Register a VM host       |
| `agw vm-host list`                  | List registered VM hosts |
| `agw vm-host remove <name>`         | Remove a VM host         |

`vm-host remove` refuses if the host has VMs registered against it; pass `--force` to clear those
VMs' `vm_host_name` reference and remove the host anyway. When the host has no VMs and you run
without `--force` or `--yes`, the command prompts for confirmation. Both `--yes` and `--force` also
bypass the confirmation prompt.

### VMs

Manage virtual machines across Lima (local or remote), Azure, and WSL2.

> **Note on WSL2:** WSL2 distros share the Windows workstation's lifecycle. They idle-shut after
> ~60s of no `wsl.exe` activity (`vmIdleTimeout` in `.wslconfig`) and do not survive workstation
> shutdown or sleep. agentworks holds a `wsl.exe` keepalive for the duration of each VM-touching
> command, so individual `agw` operations work cleanly, but agents and sessions on WSL2 are not
> suitable for unattended background workflows. Use a different provisioner that provides true
> long-lived VMs (e.g. Lima, Azure, Proxmox, etc.) if you need a VM that survives independent of
> your workstation.

| Command                                             | Description                                                   |
| --------------------------------------------------- | ------------------------------------------------------------- |
| `agw vm create <name>`                              | Create a new VM (provision + initialize)                      |
| `agw vm list`                                       | List VMs with status and resources                            |
| `agw vm describe <name>`                            | Show VM details, workspaces, and event log                    |
| `agw vm shell <name> [--workspace <ws>]`            | Admin shell on a VM (optionally rooted in a workspace)        |
| `agw vm exec <name> [--workspace <ws>] -- <cmd...>` | Run a one-shot command as admin (optionally from a workspace) |
| `agw vm start <name>`                               | Start a stopped VM                                            |
| `agw vm stop <name>`                                | Stop a running VM                                             |
| `agw vm reinit <name>`                              | Re-run initialization on a provisioned VM                     |
| `agw vm delete <name>`                              | Delete a VM (with confirmation)                               |
| `agw vm logs <name>`                                | Show SSH logs for a VM                                        |
| `agw vm console <name>`                             | _Deprecated_: use `agw console`                               |
| `agw vm add-git-credential <name> <cred>`           | Add or update a git credential                                |

`vm create <name>` takes the VM name as a required positional. Optional flags: `--platform`,
`--vm-host`, `--admin-username`, `--cpus`, `--memory`, `--disk`, and `--azure-vm-size`. These are
immutable provisioning parameters stored in the database. All initialization behavior (packages,
install commands, etc.) is driven by config.

`vm reinit` re-runs the initialization phase using the current config without reprovisioning the VM.
Changes to config (new packages, different install commands, etc.) are picked up automatically.

`vm delete` requires `--force` if the VM has workspaces, agents, or sessions. The confirmation
message shows what will be deleted. Pass `--yes` to skip the prompt.

`agw vm shell` is the agentworks-wrapped entry point; for raw SSH (VS Code Remote-SSH, `scp`, etc.),
use the `awvm--<vm>` alias documented under [Direct SSH aliases](#direct-ssh-aliases).

`vm shell` and `vm exec` both accept `--workspace <ws>` to root the admin session in a workspace
directory on this VM. The workspace's template env joins the env chain (between vm and admin), and
`AGENTWORKS_WORKSPACE` / `AGENTWORKS_WORKSPACE_DIR` are set in the session. The shell variant `cd`s
into the workspace; the exec variant runs the command from the workspace directory. A workspace that
lives on a different VM is rejected with a `ValidationError` before any SSH work.

Combining `--workspace` with `--provisioner` works (the shell still `cd`s into the workspace) but
the workspace's template env and the `AGENTWORKS_WORKSPACE` identity vars are not delivered: the
platform-native transports (`limactl shell`, `wsl.exe`) drop the `env=` kwarg by design. Treat
`--provisioner` as a transport-repair escape hatch, not a routine combination.

`agw vm shell --provisioner` opens the same shell over the platform-native transport
(`limactl shell` for Lima, `wsl.exe` for WSL2, SSH via a temporarily-attached public IP for Azure)
instead of Tailscale. Useful when Tailscale itself is the thing you need to reach the VM to fix (the
issue #117 latched DNS state is the canonical case: its heal involves restarting tailscaled, which
would terminate a Tailscale-SSH session mid-sequence). On Azure, a public IP is attached for the
duration of the session and detached on exit. Proxmox isn't supported by this flag because the QEMU
guest agent's exec interface is one-shot and non-interactive; use the Proxmox web UI's serial
console (`VM > Console` in the Proxmox VE web UI) as the equivalent escape hatch.

### Workspaces

Manage workspaces on VMs.

| Command                              | Description                                                           |
| ------------------------------------ | --------------------------------------------------------------------- |
| `agw workspace create <name>`        | Create a workspace on a VM                                            |
| `agw workspace describe <name>`      | Show workspace details and sessions                                   |
| `agw workspace list`                 | List workspaces                                                       |
| `agw workspace copy <source> <name>` | Copy a workspace to a new VM                                          |
| `agw workspace rehome <name>`        | Move workspace to a new path                                          |
| `agw workspace reinit <name>`        | Reinit workspace infrastructure                                       |
| `agw workspace delete <name>`        | Delete a workspace                                                    |
| `agw workspace shell <name>`         | _Deprecated_: use `vm shell --workspace` or `agent shell --workspace` |
| `agw workspace console <name>`       | _Deprecated_: use `agw console`                                       |

`workspace create <name>` takes the workspace name as a required positional. Optional flags: `--vm`,
`--template`, and `--open-vscode`.

`workspace copy <source> <name>` copies a workspace to a new VM workspace. Accepts `--vm`. Source
and destination can be the same VM (a clone) or different VMs.

`workspace delete` requires `--force` if the workspace has sessions. Running sessions are killed
during deletion. Pass `--yes` to skip the confirmation prompt.

`workspace shell` and `workspace console` are deprecated: a shell rooted in a workspace is always
_somebody's_ shell. Use `agw vm shell <vm> --workspace <ws>` for an admin shell or
`agw agent shell <agent> --workspace <ws>` for an agent shell. For curated tmux views over a
workspace's sessions, use `agw console create` + `agw console attach`.

### Agents

Manage agents (isolated Linux users) on VMs. Agents are VM-scoped and access workspaces via grants.

| Command                                                | Description                              |
| ------------------------------------------------------ | ---------------------------------------- |
| `agw agent create <name> [--vm]`                       | Create an agent on a VM                  |
| `agw agent list [--vm <vm>]`                           | List agents                              |
| `agw agent describe <name>`                            | Show agent details and grants            |
| `agw agent reinit <name>`                              | Re-run agent setup                       |
| `agw agent grant-workspaces <name> <ws>...`            | Grant workspace access                   |
| `agw agent grant-workspaces <name> --all`              | Grant access to all workspaces           |
| `agw agent revoke-workspaces <name> <ws>...`           | Revoke workspace access                  |
| `agw agent revoke-workspaces <name> --all`             | Revoke all explicit grants               |
| `agw agent shell <name> [--workspace <ws>]`            | Open an interactive shell as the agent   |
| `agw agent exec <name> [--workspace <ws>] -- <cmd...>` | Run a one-shot command non-interactively |
| `agw agent delete <name>`                              | Delete an agent                          |

`agent create <name>` takes the agent name as a required positional. Optional flags: `--vm`,
`--template`, and `--grant-all-workspaces`.

`agent shell` and `agent exec` both SSH directly as the agent's Linux user. `agent shell` opens an
interactive login shell (sources the agent's profile). `agent exec` runs a single command
non-interactively but still wraps it in the agent's login shell so the agent's `PATH` (mise shims,
`~/.local/bin`, etc.) is in scope. Useful for scripted invocations like
`agw agent exec myagent -- claude -p "..."`.

Both accept `--workspace <ws>` to root the session in a workspace the agent has access to. The
workspace's template env joins the env chain (between vm and agent), and `AGENTWORKS_WORKSPACE` /
`AGENTWORKS_WORKSPACE_DIR` are set in the session. The shell variant `cd`s into the workspace; the
exec variant runs the command from the workspace directory. A workspace on a different VM is
rejected with a `ValidationError`; a workspace the agent lacks access to raises `AuthorizationError`
with a hint to run `agent grant-workspaces`.

`agent delete` requires `--force` if the agent has running sessions. Pass `--yes` to skip the
confirmation prompt.

`agw agent shell` / `agw agent exec` are agentworks-wrapped entry points; for raw SSH access to an
agent (e.g. from VS Code Remote-SSH or `scp`), use the `awagent--<agent>` alias documented under
[Direct SSH aliases](#direct-ssh-aliases).

### Direct SSH aliases

Agentworks maintains operator-side SSH config entries for both VMs and agents under
`~/.ssh/config.d/agentworks.conf` (or inline in `~/.ssh/config` if `ssh_config_dir = false`):

| Alias shape        | Lands you as           | Use cases                                         |
| ------------------ | ---------------------- | ------------------------------------------------- |
| `awvm--<vm>`       | The VM's admin user    | `ssh awvm--myvm`, `scp file awvm--myvm:~/`        |
| `awagent--<agent>` | The agent's Linux user | `ssh awagent--claude`, VS Code Remote-SSH targets |

The agent alias is keyed on the agent's operator-facing name (the same name you use in
`agw agent ...` commands), not on the on-VM Linux user (which is an implementation detail). The
prefixes are configurable via `operator.ssh_host_prefix` (default `awvm--`) and
`operator.ssh_agent_host_prefix` (default `awagent--`).

Entries are rebuilt declaratively from the database on every agent / VM lifecycle operation, so a
fresh `agw agent create` or `agw vm delete` keeps the file in sync without manual intervention. Run
`agw config sync-ssh-config` to force a rebuild.

### Sessions

Manage sessions (persistent tmux sessions running in workspaces). Session names are globally unique
-- no `--workspace` flag needed for most commands.

| Command                       | Description                    |
| ----------------------------- | ------------------------------ |
| `agw session create <name>`   | Create and start a session     |
| `agw session describe <name>` | Show session details           |
| `agw session list`            | List sessions with status      |
| `agw session attach <name>`   | Attach to a running session    |
| `agw session stop <name>`     | Stop a running session         |
| `agw session restart <name>`  | Restart a session              |
| `agw session delete <name>`   | Stop and delete a session      |
| `agw session logs <name>`     | Dump session scrollback buffer |
| `agw console attach <name>`   | Attach to a named console      |

`session list` accepts `--workspace`, `--vm`, `--agent`, and `--admin` to narrow the result set.
Filters compose with AND. The name filters (`--workspace`, `--vm`, `--agent`) accept a single value
or a comma-separated list (`--vm vm1,vm2`); commas within a filter are OR-ed together.
`--agent <name>` matches agent-mode sessions only; `--admin` matches admin-mode sessions only (the
two are mutually exclusive).

`session stop` and `session restart` operate on a single session by default. Pass `--all`
(`session stop`/`session restart`) or `--all-stopped` (`session restart`) to batch over the sessions
on the VM. The batch form accepts `--vm <vm>`, `--workspace <ws>`, `--agent <agent>`, and `--admin`
to narrow the set; filters compose with AND and require one of the batch flags. The name filters
accept a single value or a comma-separated list (`--vm vm1,vm2`); commas within a filter are OR-ed
together. `--agent` matches agent-mode sessions only; `--admin` matches admin-mode sessions only
(the two are mutually exclusive). Pass `--force` to stop/restart broken sessions via PID kill.

`session create <name>` takes the session name as a required positional. Optional flags:
`--workspace`, `--template`, `--admin`, and `--agent`. If `--workspace` / `--new-workspace` is
omitted, you are prompted to pick from the existing workspaces or `[Create new workspace]` --
filtered to the known VM when `--vm` or `--agent` already pins one (the prompt prints
`Only showing workspaces on VM 'X'` when a filter is active). If `--admin` / `--agent` /
`--new-agent` is omitted, you are prompted with `admin`, the existing agents on the resolved VM, and
`[Create new agent]`. The prompts always fire when the flags are absent -- there is no single-option
auto-select for workspace or mode, since both are part of the session's identity. `--vm` works
differently: it auto-selects when exactly one usable VM exists (logged as `Using VM 'X'`), prompts
when multiple, and is required only in non-interactive mode when no workspace or agent anchor pins
the VM. In non-interactive mode (`--non-interactive` or no TTY), any required prompt raises a
`ValidationError` directing you to pass the corresponding flag. Pass `--new-workspace` to create a
workspace on the fly (with optional `--workspace-name`, `--workspace-template`, and `--vm`;
`--workspace-name` defaults to the session name). Pass `--new-agent` to create a new agent for the
session (with optional `--agent-name` and `--agent-template`; `--agent-name` defaults to the session
name); the new agent is provisioned on the workspace's VM. When a session created with
`--new-workspace` or `--new-agent` is later deleted, you are offered the option to delete the
workspace and/or agent as well -- the workspace if no other sessions remain on it, the agent if it
has no other sessions and no explicit grants.

<!-- Linked from the top-level README; rename only if you also update README.md. -->

### Named consoles

Named consoles are persistent, curated tmux views over sessions on a VM. Each console is its own
tmux session (`aw-console-<name>`) containing one window per included session, plus any extra shell
panes you want preloaded into a session's window.

| Command                                             | Description                                                       |
| --------------------------------------------------- | ----------------------------------------------------------------- |
| `agw console create <name> [sessions...]`           | Create a console with the given sessions                          |
| `agw console list`                                  | List consoles                                                     |
| `agw console describe <name>`                       | Show membership and shell layout                                  |
| `agw console attach <name>`                         | Attach (builds tmux state on first attach)                        |
| `agw console delete <name>`                         | Tear down and remove the console                                  |
| `agw console add-sessions <name> <sessions...>`     | Add session windows                                               |
| `agw console remove-sessions <name> <sessions...>`  | Remove session windows                                            |
| `agw console reorder-sessions <name> <sessions...>` | Bump member sessions to the front in the order given              |
| `agw console add-shell <name> <session>`            | Add a shell pane to a session window (accepts `--cwd`, `--admin`) |

`console create` accepts:

- `--vm` -- target VM. **Inferred from the listed sessions when omitted**; if the listed sessions
  span more than one VM, `console create` errors and asks you to pick one with `--vm`. When no
  sessions are listed (e.g. with `--all` and no explicit specs), VM selection falls back to the
  standard prompt (auto-picked if you have a single VM, prompted otherwise).
- `--all` -- include every session on the VM with 0 shells, appended after the explicit specs
  (alphabetical).
- `--all-running` -- like `--all` but restricted to sessions whose live tmux state on the VM is OK
  (one SSH round-trip; same probe `agw session list` uses). Mutually exclusive with `--all`.
  Requires the VM to be reachable.
- `--add-admin-shell` -- include a top-level admin-shell window as window 0, matching the legacy
  `vm console` behavior.

`console list` accepts `--vm`, `--workspace`, and `--agent` to narrow the result set. Each filter
takes a single value or a comma-separated list (`--workspace ws1,ws2`); commas within a filter are
OR-ed together. The `--workspace` and `--agent` filters use "any session matches" semantics: a
console is listed if at least one of its member sessions belongs to the given workspace / runs as
the given agent. When `--workspace` and `--agent` are both passed, the SAME session must satisfy
both predicates. The session count displayed is the total membership, not the count of matching
sessions. Filters compose with AND.

Session specs use `name` or `name+N` shorthand, where `N` is the number of default shell panes to
pre-open in that session's window (running as the session's agent user, cwd = workspace root):

```sh
# A console with three sessions; the first two get extra shells.
# VM is inferred from the sessions.
agw console create backend auth-server+2 auth-tests+1 docs

# Same, but also include a top-level admin-shell window (window 0).
agw console create backend auth-server+2 auth-tests+1 docs --add-admin-shell

# Everything currently running on the VM, after the explicit specs.
agw console create live auth-server+2 --all-running

# All sessions on the VM (running or not). Needs --vm since no sessions are
# named explicitly to infer from.
agw console create everything --vm aw-private --all

# Add an admin shell rooted in a sub-path of the workspace.
agw console add-shell backend auth-server --cwd src/api --admin
```

Memberships and shell layouts persist in the database. `agw console attach` builds the tmux session
on first attach (or with `--recreate`); subsequent attaches reuse the running tmux session. Adding
or removing sessions/shells while a console is attached updates the live tmux state immediately
(best-effort); when the console isn't running on the VM, only the DB is updated and changes appear
on next attach.

<!-- Linked from the top-level README; rename only if you also update README.md. -->

### tmux Architecture

Each session runs in its own locked-down tmux session on the VM. There are several ways to interact
with sessions, at different scopes:

| Method                           | Scope                            | tmux session name        | Entry point                 |
| -------------------------------- | -------------------------------- | ------------------------ | --------------------------- |
| `session attach`                 | One session                      | `<session-name>`         | Operator's machine          |
| `console`                        | Curated subset across workspaces | `aw-console-<name>`      | Operator's machine          |
| `workspace console` (deprecated) | One workspace                    | `ws-<workspace>-console` | On-VM or operator's machine |
| `vm console` (deprecated)        | All sessions on the VM           | `vm-console`             | Operator's machine          |

#### Session tmux sessions

Each session gets a locked-down tmux session using the session name directly as the tmux session
name. The user's `~/.tmux.conf` (customizable via dotfiles) is loaded first so that familiar
keybindings (prefix, detach, copy mode, scroll) work for direct `session attach`. Window/pane
creation, session management, and the command prompt are selectively unbound.

Agent-mode sessions run on a per-agent tmux socket so the agent's shell connects directly to the
tmux pane PTY. The socket path is persisted in the database.

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
the DB is touched and changes appear on next attach. The mutation commands (`add-sessions`,
`remove-sessions`, `reorder-sessions`, `add-shell`) never auto-boot the VM; the explicit
attach/repair commands (`attach`, `restore-session`) do start a stopped VM, since their job is to
bring live state up.

#### Workspace console (deprecated)

`workspace console` uses tmuxinator to create or attach to a `ws-<name>-console` session. The
tmuxinator config (`.tmuxinator.yml` in the workspace root) is regenerated whenever sessions change,
so the console always reflects the current set of sessions. Best for in-VM work scoped to a single
workspace (e.g. inside VS Code's integrated terminal). Predates the multi-console design and lacks
env-and-secrets integration; superseded by named consoles (`console attach <name>`). Will be removed
in a future release.

```text
ws-myproject-console (tmuxinator, full tmux)
  Window 1: admin-shell                login shell for the admin user
  Window 2: myproject-claude           attached to session
  Window 3: myproject-debug            attached to session
```

#### VM console (deprecated)

`vm console` creates or attaches to the `vm-console` session, which spans all sessions on the VM.
Built dynamically (not via tmuxinator). Superseded by named consoles, which let you curate which
sessions are in scope at any moment instead of seeing every session on the VM. Will be removed in a
future release.

#### Shells

`vm shell` and `agent shell` open plain login shells with no tmux (optionally rooted in a workspace
via `--workspace <ws>`). Use these when you just need a terminal without the console structure.

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
custom templates as `session-template` resources:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: default # override the built-in default
  description: Claude Code interactive session
spec:
  command: claude --name {{session_name}}
  restart_command: claude --resume {{session_name}}
  required_commands: [claude]
```

(TOML equivalent: `[session_templates.default]` in `config.toml`, deprecated but supported.)

Template commands support `{{session_name}}` and `{{workspace_name}}` variable substitution
(double-brace syntax). The optional `restart_command` is used by `session restart` -- useful for
tools like Claude Code where `--resume` picks up the previous conversation. If omitted, the regular
`command` is used.

The optional `required_commands` list names executables the template needs. They are checked on the
session's launch target (the agent, or the VM admin for admin sessions) before any state mutation,
so launching a `claude` session on an agent that doesn't have `claude` installed fails fast with a
clear error instead of a cryptic downstream tmux failure. `required_commands` is merged (de-duped,
order-preserving) across template inheritance.

### Config

| Command                             | Description                                  |
| ----------------------------------- | -------------------------------------------- |
| `agw config init`                   | Create a sample config file                  |
| `agw config edit`                   | Open config in `$EDITOR`                     |
| `agw config sample`                 | Print the sample config to stdout            |
| `agw config sync-ssh-config`        | Rebuild SSH config entries for VMs + agents  |
| `agw config sync-vscode-workspaces` | Regenerate .code-workspace files for all VMs |

### Resource Registry

Cross-kind inspection of the Resource Registry. The registry is the framework that owns every
operator-declared, auto-declared, and built-in resource the CLI knows about: secrets, VM templates,
agent templates, workspace templates, catalog entries, git credential providers, secret backends,
etc. The two commands below stop at the framework-uniform fields (`kind`, `name`, `origin`,
`references`, `used_by`, `description`). For kind-specific detail -- secret backend mappings,
template inheritance chains, resolution previews -- reach for the per-kind command (e.g.
`agw secret describe`).

| Command                              | Description                                                          |
| ------------------------------------ | -------------------------------------------------------------------- |
| `agw resource list`                  | List every resource in the registry across all kinds                 |
| `agw resource kinds`                 | List every kind: category (declarable/capability), counts, purpose   |
| `agw resource describe KIND/NAME`    | Show the per-resource detail view (header + Referenced by + Used by) |
| `agw resource edit KIND/NAME`        | Open the declaring YAML manifest in $EDITOR                          |
| `agw resource migrate [SELECTOR]...` | Move resources from config.toml to YAML manifests                    |
| `agw resource sample KIND [--write]` | Print (or save) a kind's commented sample manifest (--all for all)   |

`resource list` accepts `--kind <csv>` (e.g. `--kind secret,vm-template`) and `--origin <variant>`
where variant is `operator`, `auto`, or `builtin`. `--names-only` emits `kind/name` per line and
backs shell completion (`/` cannot appear in resource names, so the split is unambiguous). The
`kind/name` token is the one grammar across the resource group: `resource describe secret/npm-token`
and `resource migrate vm-template/dev` take the same shape.

`resource migrate` is a recurring, incremental mover -- run it any time you want to move resources
(or a subset) from TOML to YAML manifests. Selectors scope the run: `KIND` one kind, `KIND/NAME` one
resource (overlaps union), or `--all` for everything TOML-declared -- a bare invocation errors
rather than migrating the whole config by accident. `--layout per-kind|single|per-resource` picks
the file mapping (default one multi-document file per kind, e.g. `resources/vm-templates.yaml`).
Output is append-only: existing YAML files are never parsed or rewritten, new documents are appended
after a `---` separator. Because a resource declared in both sources is a hard load error, the
migrated TOML sections are commented out in place with a `# migrated to resources/<file>` marker
(default) or removed with `--toml delete`; either way the original `config.toml` is backed up to
`paths.backups` first and the rewrite is atomic. Deprecated `[secret_backends.*]` sections are
dropped (with a note) by any run, including a bare run with nothing else to migrate. Every real run
finishes by rebuilding the registry and verifying it is row-for-row identical to the pre-migration
one -- on mismatch the run rolls back and reports. `--dry-run` prints a summary of what would
migrate where and writes nothing; add `--full` for the complete YAML documents and the config.toml
diff.

`resource sample` prints a kind's fully-commented-out sample manifest (`--all` for every kind) --
the YAML teaching surface, mirroring `agw config sample` for the settings file. `--write <file>`
saves under the resources directory instead (relative `.yaml`/`.yml` path; appends if the file
exists). Written samples are inert until you uncomment them (delete one leading `#` per line), so
`--write` can never create a live resource or a duplicate.

## Configuration

Configuration splits into two surfaces:

- **Settings** live in `~/.config/agentworks/config.toml` -- your identity, paths, defaults,
  platform connections, and the secret backend chain. Run `agw config init` to generate a sample;
  see [sample-config.toml](agentworks/sample-config.toml) for the full reference.
- **Resources** -- secrets, templates, git credentials, catalog entries -- are declared as YAML
  manifests under `~/.config/agentworks/resources/`, auto-loaded whenever a command needs them.
  `agw resource sample <kind>` prints a commented starter (`--all` for every kind). The classic TOML
  resource sections keep working (deprecated, with one aggregated load warning naming the sections
  present; silence it with the global `--no-deprecations` flag); `agw resource migrate` moves them
  to YAML whenever you like. See [docs/guides/resources.md](../docs/guides/resources.md).

Settings sections (`config.toml`, permanent):

- `[operator]` -- SSH keys (required), additional authorized keys, SSH config management
- `[paths]` -- VM workspace, VS Code workspace file, and backup directories
- `[defaults]` -- default platform, VM host
- `[session.config]` -- session defaults (history limit)
- `[secret_config]` -- active secret backend chain (`[secret_backends.*]` sections are deprecated
  no-ops; see Secret Backends below)
- `[azure]` -- Azure-specific settings
- `[proxmox]` -- Proxmox VE API settings

Resource kinds (YAML manifests; the deprecated TOML section is noted for each):

- `vm-template` (`[vm_templates.*]`) -- VM resources, apt packages, system install commands, mise
- `admin-template` (`[admin.config]`) -- admin user shell, dotfiles, git credentials, user install
  commands, mise
- `agent-template` (`[agent_templates.*]`) -- agent user shell, dotfiles, git credentials, user
  install commands, mise
- `session-template` (`[session_templates.*]`) -- session commands with variable substitution
- `workspace-template` (`[workspace_templates.*]`) -- workspace templates with inheritance
- `named-console-template` (`[named_console]`) -- named-console layout (tmux preset names +
  `aw-session-vertical`)
- `git-credential` (`[git_credentials.*]`) -- git credentials; `spec.provider` selects github or
  azdo (TOML also accepts the legacy `type`)
- `secret` (`[secrets.*]`) -- secret declarations referenced by `{secret: name}` env entries
- `apt-source` / `apt-package` / `system-install-command` / `user-install-command`
  (`[apt_sources.*]` etc.) -- catalog extensions
- Env vars ride their owning resource: an `env` map in the template's `spec` (TOML: `[<scope>.env]`
  subsections) at vm / workspace / admin / agent / session scope

### Environment Variables and Secrets

Env tables can be declared at five scopes; for any given session the merged value is computed in
this precedence order (highest scope wins; identity vars win over everything):

```text
session > (agent | admin) > workspace > vm           (AGENTWORKS_* identity overrides all)
```

Admin and agent scopes are mutually exclusive: a shell opened as the admin user (e.g.
`agw vm shell`) sees admin scope; an agent-mode session sees agent scope. Each scope is an env map
on the owning resource, mapping env-var name to either a plaintext string or a secret reference:

```yaml
apiVersion: agentworks/v1
kind: vm-template
metadata:
  name: default
spec:
  env:
    HTTP_PROXY: http://proxy:3128
    NPM_TOKEN: { secret: npm-token }
```

(TOML equivalent: `[vm_templates.default.env]` with `NPM_TOKEN = { secret = "npm-token" }`.)

Every secret reference points to a `secret` resource declaration (auto-declared with a
framework-synthesized description if you skip it). Active backends (and their precedence order) are
listed in `[secret_config].backends`. Today the implemented backends are:

- `env-var` -- reads from the operator's process env. Default convention is
  `AW_SECRET_<UPPER_SNAKE_CASE>`, overridable per secret via the secret's `backend_mappings`
  (`env-var: CUSTOM_NAME`).
- `prompt` -- interactive prompt; batched at the start of the CLI run.

**Eager prompting (FRD R4):** every command that opens new shells resolves all needed secrets up
front, before any state mutation. The set of secrets is computed from the command's static filters
(positional targets, `--vm`, `--workspace`, `--agent`, etc.) -- dynamic predicates like
`--all-stopped` apply later, so the prompted set may over-approximate. Non-interactive mode (no TTY
or `--non-interactive`) surfaces missing secrets as `SecretUnavailableError` with a per-secret hint
naming which backends were tried. Commands that join existing shells (`session attach`,
`session list`, `console attach` against a live tmux session, `console add-sessions`) consume no
secrets per FRD R4 / R5.

**Miss semantics:** what "not found" means depends on the backend. Conventional sources (`env-var`,
`prompt`) treat a missing value as a soft miss and fall through to the next backend in the chain --
a `GITHUB_TOKEN` env var that isn't set is just-not-set, not a config error. Persistent-store
backends (1Password, Vault when implemented) will treat an explicit mapping that doesn't resolve as
a hard miss: they raise `SecretMappingError` and the chain halts so a wrong `op://` URI doesn't
quietly fall through to a prompt that masks the real problem.

Inspect the merged result for any context with `agw env show`:

```bash
agw env show --session my-session              # secrets redacted as <from secret: name>
agw env show --vm my-vm --reveal-secrets       # resolves through the active backend chain
```

Inspect how each active backend would resolve each declared or auto-declared secret (e.g. "which env
var name does this secret read from?") with `agw secret list`:

```bash
agw secret list
# 4 secrets (2 operator-declared, 2 auto-declared)
#
# NAME                 DESCRIPTION                                                                env-var                       prompt
# ----                 -----------                                                                -------                       ------
# api-key              OpenAI key for the operator's service                                      OPENAI_API_KEY                enabled
# force-prompt         Always prompted at command time                                            disabled                      enabled
# git-token-github     (auto) the auth token for git_credentials:github                           AW_SECRET_GIT_TOKEN_GITHUB    enabled
# tailscale-auth-key   (auto) the Tailscale auth key for vm-template:default (and 1 more)   AW_SECRET_TAILSCALE_AUTH_KEY  enabled
```

Columns are the active backends in `[secret_config].backends` precedence order. Cells show each
backend's static lookup identifier (env var name, vault path, `op://` URI) or `disabled` / `enabled`
for backends with an explicit opt-out or no static identifier (prompt). The Description column shows
the operator-supplied text for operator-declared secrets, or a framework-synthesized
`(auto) <usage> for <kind>:<name>` (plus `(and N more)` when more than one source requires the
secret) for auto-declared ones. The synthesized text reads as "what this secret is for, and who's
asking." The summary line breaks the rows down by origin. Values are never resolved.

For the full per-secret detail view, including the structured origin block, usage list (who requires
this secret), per-backend mapping table, and a resolution preview, use `agw secret describe`:

```bash
agw secret describe tailscale-auth-key
# Secret: tailscale-auth-key
#   Kind: secret
#   Description: (auto) the Tailscale auth key for vm-template:default (and 1 more)
#   Origin: auto-declared (vm-template:default)
#
# Referenced by:
#   - vm-template:default -- the Tailscale auth key
#   - vm-template:heavy -- the Tailscale auth key
#
# Backend mappings:
#   - env-var: AW_SECRET_TAILSCALE_AUTH_KEY
#   - prompt: (prompt at resolution time)
#
# Resolution preview:
#   would resolve via env-var
```

`describe` reports state -- it does not prompt and does not resolve the secret's value.

`agw doctor`'s Secrets group emits exactly one row per registry secret -- operator-declared and
auto-declared alike (auto-declared rows, e.g. `tailscale-auth-key` and the `git-token-*` family,
carry an `(auto)` marker; they are exactly the secrets most likely to prompt at command time):

- **OK** when at least one active backend would resolve the secret at runtime
  (`would resolve via env-var`, `would resolve via prompt`, ...). `would resolve via prompt` is the
  heads-up that the next command needing this secret will ask for it interactively.
- **WARN** when nothing in the chain would resolve it (config-valid but no path to a value, e.g.
  env-var has no matching env var set and `prompt` is opted out via
  `backend_mappings.prompt = false`).
- **FAIL** when `backend_mappings` references an unknown backend name (not a registered backend like
  `env-var` / `prompt`).

Backend-applicability detail (per-backend soft-skip reasons, inactive mappings, per-secret
references) lives in `agw secret list` and `agw secret describe`. `AGENTWORKS_*` identity overrides
surface in the Configuration group (they're a config-load warning). Broken `{ secret = "..." }`
references are caught earlier as a hard config-load error before doctor runs. Git-credential token
health reports here as ordinary `git-token-*` secrets (doctor has no separate git-credentials
group), and the Tailscale group checks only workstation connectivity -- the auth key is the
`tailscale-auth-key` secret row.

### Secret Backends

A **backend** is a capability resource that produces secret values (`env-var`, `prompt`; future
backends like `onepassword`): a read-only row backed by registered code, listed by
`agw resource list --kind secret-backend` and activated in precedence order by the chain
(`[secret_config].backends`). Per-secret behavior -- identifier overrides, structured store
addressing, opt-outs -- lives in each secret's `backend_mappings.<backend>`.

### Mise (Polyglot Tool Manager)

Agentworks installs [mise](https://mise.jdx.dev/) by default on all VMs for managing CLI tools
(terraform, adr-tools, node, etc.) with optional lockfile-based integrity verification. See
[Using mise](../docs/guides/mise.md) for the full guide.

### Claude Code Plugins

Agentworks can register Claude Code marketplaces and install plugins automatically per user (admin
and per-agent). Configure via `claude_marketplaces` and `claude_plugins` on the admin template or
any agent template. Requires the `claude` CLI on PATH (typically installed via
`user_install_commands`). To install nerftools this way:

```yaml
apiVersion: agentworks/v1
kind: admin-template
metadata:
  name: default
spec:
  claude_marketplaces: ["https://github.com/WayfarerLabs/nerftools#4.1.0"]
  claude_plugins: [nerftools-default@nerftools]
```

(TOML equivalent: `[admin.config]` in `config.toml`, deprecated but supported.)

### Built-in Catalog

Agentworks ships a built-in catalog of common tools (apt sources, apt packages, system install
commands, and user install commands). Run
`agw resource list --kind apt-package,system-install-command,user-install-command,apt-source` to see
what is available (or filter to any single kind). Reference catalog entries by name from VM, admin,
and agent templates. User-defined entries override built-in entries with the same name.

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
agw completion install
```

The shell is autodetected from `$SHELL`; pass `--shell {bash|zsh|powershell}` to override (or when
autodetection isn't unambiguous, e.g. on Windows). `completion install` writes the script to the
standard location for that shell. For PowerShell it also appends a dot-source line (`. "..."`) to
`$PROFILE`. For bash and zsh, if your rc file is missing the loader (`bash-completion` for bash,
`fpath=(~/.zfunc $fpath)` for plain zsh without a plugin manager), the installer prints a one-line
note telling you what to add.

To print the script without installing, use `agw completion show` (handy for piping into your own
config-management flow). To remove completions installed here, use
`agw completion uninstall --shell {bash|zsh|powershell}`. For PowerShell, uninstall also strips the
dot-source line the installer appended to `$PROFILE`; user-authored lines around it are left
untouched.

Completions include dynamic VM, workspace, VM host, session, and template name lookups.

## State

All state is stored in `~/.config/agentworks/agentworks.db` (SQLite). Schema migrations are
forward-only and run automatically.

## Environment Variables

Secret values are read from the operator's shell via the `env-var` backend, which follows the
convention `AW_SECRET_<UPPER_SNAKE_CASE>` derived from the secret's name. The Tailscale auth key
(secret `tailscale-auth-key`) reads from `AW_SECRET_TAILSCALE_AUTH_KEY`; a git credential's PAT
(secret `git-token-<name>`) reads from `AW_SECRET_GIT_TOKEN_<NAME>`; and so on. Override the
convention per secret via the secret's `backend_mappings` (`env-var: CUSTOM_NAME`).

Use `agw secret list` to see the exact env var name for each declared or auto-declared secret, and
`agw secret describe <name>` for the full per-secret view (origin, usages, backend mappings,
resolution preview).

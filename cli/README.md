# Agentworks CLI

CLI for interacting with Agentworks, the swiss army knife for managing agentic workloads.

## The Problem Space

Agentworks is an attempt to address several growing problems around agentic engineering with a
single, (hopefully) coherent framework.

These problems are:

### Security

Agentic engineering is inherently risky. These risks come from multiple directions, including:

- **Honest mistakes** - An agent can simply make a mistake that results in data loss, corruption, or
  unintended side effects. It's very easy to find stories of Claude wiping out entire directories or
  otherwise causing havoc.
- **Prompt injection** - Agents that are exposed to the outside world (e.g. by downloading untrusted
  web content) can potentially be manipulated into doing things outside of their operator's intent
  or control.
- **Supply chain attacks** - Agents may download and run compromised software or dependencies from
  external sources, which could introduce malicious code into the environment, at build time,
  runtime, or both.
- **Rogue agents** - The agent itself could behave maliciously due to a compromise of the model, the
  provider, or emergent behavior.

While these are already in play to some extent, increasing AI capabilities guarantee that attacks
will become increasingly frequent and sophisticated. Supply chain attacks in particular have become
a near-constant backdrop: the XZ Utils backdoor (a multi-year social engineering campaign against a
burned-out maintainer, caught by luck in 2024), the Shai-Hulud self-replicating npm worm (500+
packages compromised in September 2025, escalating to 25,000+ repositories as "Shai-Hulud 2.0" in
November 2025), and the TeamPCP campaign (compromising `litellm`, `telnyx`, and the widely-used
`axios` npm package in March 2026) are just a few recent examples. North Korean threat actors alone
have pushed 1,700+ malicious packages across npm, PyPI, Go, and Rust. The registries that developers
(and their agents) depend on are under active, sustained attack.

All of these suggest similar solutions, though. You need strong guardrails (isolation, permissions,
etc.) to ensure that _when_ things go sideways, the blast radius is contained and the operator
retains control.

### Workload Management

Anyone who has had more than one or two parallel agentic sessions has likely run into the problem of
keeping track of which agents are doing what, which sessions are active, how to coordinate work
across multiple agents (possibly working in the same repository or worktree), how to keep them all
running reliably (e.g. even when you close your laptop or lose your network connection), etc.

These are real challenges that impose real limits on how many agentic workloads a single operator
can reasonably manage at once. Most devs who have leaned into this space have developed some amount
of custom tooling to help with this problem. Solving for this at the platform layer should be a
significant enabler to delivering value more quickly.

### Consistency

Similar to workload management, inconsistency across workload environments (different tools,
configuration, files, etc.) creates significant friction and potential for errors when trying to
scale up agentic engineering.

While sometimes these differences are intentional and should be preserved (e.g. wanting Agent A to
have different tools and permissions than Agent B), they often are accidental and introduce
unnecessary complexity and risk.

### Control

The operator should retain control over what agents are doing, how workloads are executed, and what
resources they can access _even as those workloads become more autonomous_. This is a central design
goal of Agentworks, and it ties the preceding concerns together: without reliable knowledge of what
agents are doing, consistent environments, and contained blast radius, control is lost in practice
even if it's notionally retained.

A significant and growing part of the ecosystem treats loss of control as an inevitable cost of
agentic autonomy. Agentworks takes the opposite position: autonomy and control are not mutually
exclusive. A good platform should make it possible and straightforward to have both.

## Core Concepts

Agentworks organizes work into five core concepts:

### The Operator - the Person in Control

Agentworks is currently designed around a single human "operator" who is in control of all agentic
workloads. The operator is responsible for creating VMs, workspaces, agents, and sessions, and for
orchestrating how these components interact.

Note that while you might find some exceptions, we generally reserve the term "user" for the
technical Linux users that exist on the VMs (the admin user and the agentic identities).

### VMs - the Compute Environment

VMs define the base **compute environment** for all workloads. As discussed in
[ADR 0001](../docs/adrs/0001-vm-based-infrastructure.md), Agentworks uses VMs as the fundamental
unit of compute to provide for strong isolation while providing all the capabilities of a full Linux
environment (full daemonized services, multi-user, ability to run containers, etc.).

VMs further use a single operating system (Debian Bookworm, see
[ADR 0002](../docs/adrs/0002-use-debian-as-the-vm-base-image.md)) to ensure consistency and minimize
VM management complexity and risk.

VMs are generally intended to be long-lived and are designed to support any number of agentic
workloads. A robust configuration and templating mechanism is provided so that VM provisioning can
be automated and standardized across environments. VMs can further be "reinitialized" to
declaratively update them based on changes to the template or configuration.

Each VM also includes an "admin" user that has full sudo privileges that is used for all
provisioning and management tasks on the VM. While not recommended, the admin user is also available
for agentic workloads if the operator so desires.

### Workspaces - the Project

A workspace defines the **project scope**. Workspaces ultimately consist of a root directory that
can be based on a git repository or an empty directory. The workspace also maps to a Linux group
with workspace permissions and ACLs set to allow collaborative access to the files within the
workspace for all members of the group. Workspace-level configuration (e.g. Claude Code's project
settings) can be used to control how tools behave within the context of this workspace.

The Agentworks workspace mechanism fully supports any number of workspaces mapping to the same
underlying repository. To simplify administration, each is a full independent clone.

Workspaces always live on a VM. An earlier iteration supported local (on-workstation) workspaces,
but they did not support agents (which require Linux user management only available on VMs), so they
were removed to keep the model focused.

### Agents - the Actor

An agent defines a **security identity** on a VM. Each agent maps to its own full Linux user,
capable of having its own processes, private files, shell environment, etc. This allows for the
creation of different identities with different privileges and capabilities.

Agents are mapped to workspaces, either explicitly via grants or implicitly via sessions (see
below). This mapping drives standard group and filesystem permissions that control what agents are
able to access.

Agents are only supported on VM workspaces because the isolation model requires Linux user
management (useradd, group membership).

### Sessions - the Workloads

A session is the primary way of running interactive **workloads** in Agentworks (e.g. a Claude Code
instance). It provides the mechanism by which an agent can execute commands within the context of a
workspace. A unique name and a persistent tmux session allow the operator to have any number of
concurrent workloads running across their VMs, workspaces, and agents. Agentworks allows the
operator to attach to and detach from them as needed to monitor progress or interact with the
workload, and then to stop, restart, and delete them to manage their lifecycle.

For day-to-day work across many sessions, see [Named consoles](#named-consoles) — curated tmux
views that group the sessions you're actively focused on, optionally with extra shell panes
pre-opened in each session's window.

## Key Principles

### Opinionated Consistency

Broadly-applicable systems like Agentworks can easily spiral into significant complexity by
attempting to support too many ways of doing the same thing. To protect against this, Agentworks
takes an opinionated stance on how things should be set up. A single base operating system,
tightly-integrated tooling, and emphasis on declarative configuration all help minimize variation
and surprises across different workloads.

### Composable Isolation

This model provides several isolation mechanisms, which operators can compose to achieve their
desired security posture. While the system is optimized around the full isolation model (VMs,
agents, and workspaces), this is by no means required. Operators are free to use any subset that
makes sense for their security and operational requirements.

### Ephemerality

The layers differ in intended lifespan. VMs are intended to be long-lived: provisioned once and used
across many projects. Workspaces are intended to be medium-lived: created to support a particular
workstream or project and destroyed when done. Agents can be long-lived or short-lived depending on
the operator's preferences. Long-lived agents can be reused across multiple workspaces and sessions
or they can be created for a single workspace or session and destroyed when no longer needed.
Sessions are intended to be the most ephemeral: started for a specific activity and discarded when
done.

### Declarative Configuration and Templates

Each layer has a templating mechanism using declarative configuration so that patterns can be
defined once and stamped many times. The longer-lived resources (VMs and agents) provide for
[mostly idempotent](../docs/guides/idempotency.md) "reinitialization" so that they can be reliably
evolved over time.

## Tightly Integrated Tools

In the spirit of opinionated consistency, Agentworks tightly integrates a small set of excellent
tools that add significant value. While these tools could theoretically be replaced with
alternatives, this would involve significant additional complexity that would slow down development
and increase the likelihood of inconsistencies or errors.

Those using Agentworks are highly encouraged to embrace these tools rather than attempting to work
around them.

### SSH

SSH is the control plane for all VM operations. Agentworks uses SSH to provision VMs, initialize
them, manage agents, run sessions, transfer files, and execute commands. The operator's SSH key
(configured in `[operator]`) is deployed to VMs during provisioning and is the sole authentication
mechanism for all subsequent operations.

During provisioning, SSH access uses the platform's native transport (Lima shell, Azure public IP,
WSL2 exec, or Proxmox guest agent). Once Tailscale is joined (see below), all further SSH access
goes over the tailnet. Agentworks automatically manages `~/.ssh/config` entries for each VM so that
standard SSH tools (scp, ssh, VS Code Remote) work seamlessly.

### Tailscale

VMs join a [Tailscale](https://tailscale.com/) tailnet during provisioning. All subsequent SSH
access (workspace shell, VM shell, initialization) goes over Tailscale, providing secure
connectivity without exposing SSH ports to the public internet.

During `vm create` (and `vm start` when re-joining), you will be prompted for a Tailscale auth key
unless the `TAILSCALE_AUTH_KEY` environment variable is set. Generate keys at the
[Tailscale admin console](https://login.tailscale.com/admin/settings/keys).

Ephemeral auth keys (with `?ephemeral=true` appended) are fully supported. The Tailscale node is
automatically removed from the tailnet when the VM goes offline. Agentworks handles re-joining
gracefully on `vm start` by prompting for a new auth key (or using `TAILSCALE_AUTH_KEY`).

### Tmux

Sessions are built on [tmux](https://github.com/tmux/tmux), which provides persistent terminal
sessions that survive disconnects and support attach/detach. Each session maps 1:1 to a tmux session
on the VM.

Agentworks provides several console layers for interacting with sessions:

- **Workspace console** (`workspace console`): a tmuxinator-managed tmux session with one window per
  session in the workspace, plus an admin shell. Good for staying inside a single workspace.
- **Named consoles** (`console`): persistent, named tmux sessions that aggregate a curated subset of
  sessions across any workspaces on a VM, with optional extra shell panes per session window.
  Recommended when you juggle sessions across workspaces or want a focused view of the few you're
  actively working on.
- **VM console** (`vm console`, _deprecated_): a dynamically-built tmux session spanning every
  session on the VM. Replaced by named consoles; will be removed in a future release.

Agent-mode sessions run on per-agent tmux sockets for proper process isolation and terminal resize
propagation. See the [tmux Architecture](#tmux-architecture) section for details.

### Additional Tools

A few other tools, while not fundamental, warrant a brief mention:

- **Git** is fully integrated into workspace configuration, allowing operators to define workspace
  templates around specific repositories. Integrated git credential management makes it easy to use
  different providers (GitHub, Azure DevOps, etc.) with any number of scoped credentials (e.g.
  access tokens) to control capabilities and blast radius.
- **VS Code Workspaces** are automatically generated (using the Remote - SSH extension) for each
  workspace Agentworks manages, allowing developers to easily open an Agentworks workspace directly
  in VS Code to view files, use the terminal, and leverage the full VS Code feature set.
- **[Mise en Place](https://mise.jdx.dev/)** is supported out of the box for easily adding tools,
  including checksum validation using lockfiles where supported by the backend.
- **[Dotfiles](https://www.datacamp.com/tutorial/dotfiles)** can be configured for both the admin
  user and agents, helping to ensure a consistent terminal environment (shell configuration, editor
  settings, etc.) across workloads.

## Getting Started

```bash
uv sync
uv run agentworks config init    # creates ~/.config/agentworks/config.toml
```

Edit the config file (at minimum, set your SSH key paths), then:

```bash
agentworks vm create my-vm                       # provision + initialize a VM
agentworks workspace create my-workspace         # create a workspace on the VM
agentworks workspace shell my-workspace
```

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [Tailscale](https://tailscale.com/) installed and connected (for VM workspaces)
- One of: [Lima](https://lima-vm.io/), Azure CLI (`az`), or WSL2 (for VM provisioning)

## Global Options

| Flag                | Description                     |
| ------------------- | ------------------------------- |
| `--non-interactive` | Disable all interactive prompts |

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
| `agentworks workspace repair <name>`        | Repair workspace infrastructure     |
| `agentworks workspace delete <name>`        | Delete a workspace                  |

`workspace create <name>` takes the workspace name as a required positional. Optional flags:
`--vm`, `--template`, and `--open-vscode`.

`workspace console` opens a tmuxinator session (`ws-<name>-console`) with an admin-shell window plus
one window per session in the workspace. Pass `--recreate` to kill and rebuild the console. This is
the recommended way to interact with sessions from within VS Code or any terminal on the VM.

`workspace copy <source> <name>` copies a workspace to a new VM workspace. Accepts `--vm`. Source
and destination can be the same VM (a clone) or different VMs.

`workspace delete` requires `--force` if the workspace has sessions. Running sessions are killed
during deletion. Pass `--yes` to skip the confirmation prompt.

### Agents

Manage agents (isolated Linux users) on VMs. Agents are VM-scoped and access workspaces via grants.

| Command                                                      | Description                    |
| ------------------------------------------------------------ | ------------------------------ |
| `agentworks agent create <name> [--vm]`                      | Create an agent on a VM        |
| `agentworks agent list [--vm <vm>]`                          | List agents                    |
| `agentworks agent describe <name>`                           | Show agent details and grants  |
| `agentworks agent reinit <name>`                             | Re-run agent setup             |
| `agentworks agent workspace-grants grant <name> <ws>[,<ws>]` | Grant workspace access         |
| `agentworks agent workspace-grants grant <name> --all`       | Grant access to all workspaces |
| `agentworks agent workspace-grants deny <name> <ws>[,<ws>]`  | Remove workspace access        |
| `agentworks agent workspace-grants deny <name> --all`        | Remove all explicit grants     |
| `agentworks agent workspace-grants list <name>`              | List workspace grants          |
| `agentworks agent shell <name> [--workspace <ws>]`           | Shell into an agent            |
| `agentworks agent delete <name>`                             | Delete an agent                |

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
prompted interactively if omitted; if agents exist on the VM and neither `--admin` nor `--agent`
is specified, you are prompted to choose. Pass `--new-workspace` to create a workspace on the fly
(with optional `--workspace-name`, `--workspace-template`, and `--vm`; `--workspace-name` defaults
to the session name). Pass `--new-agent` to create a new agent for the session (with optional
`--agent-name` and `--agent-template`; `--agent-name` defaults to the session name); the new agent
is provisioned on the workspace's VM. When a session created with `--new-workspace` or
`--new-agent` is later deleted, you are offered the option to delete the workspace and/or agent as
well -- the workspace if no other sessions remain on it, the agent if it has no other sessions and
no explicit grants.

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

- `--vm` -- target VM. **Inferred from the listed sessions when omitted**; if the
  listed sessions span more than one VM, `console create` errors and asks you to
  pick one with `--vm`. When no sessions are listed (e.g. with `--all` and no
  explicit specs), VM selection falls back to the standard prompt (auto-picked
  if you have a single VM, prompted otherwise).
- `--all` -- include every session on the VM with 0 shells, appended after the
  explicit specs (alphabetical).
- `--all-running` -- like `--all` but restricted to sessions whose live tmux
  state on the VM is OK (one SSH round-trip; same probe `aw session list`
  uses). Mutually exclusive with `--all`. Requires the VM to be reachable.
- `--add-admin-shell` -- include a top-level admin-shell window as window 0,
  matching the legacy `vm console` behavior.

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

### tmux Architecture

Each session runs in its own locked-down tmux session on the VM. There are several ways to interact
with sessions, at different scopes:

| Method                  | Scope                            | tmux session name        | Entry point                 |
| ----------------------- | -------------------------------- | ------------------------ | --------------------------- |
| `session attach`        | One session                      | `<session-name>`         | Operator's machine          |
| `workspace console`     | One workspace                    | `ws-<workspace>-console` | On-VM or operator's machine |
| `console`               | Curated subset across workspaces | `aw-console-<name>`      | Operator's machine          |
| `vm console` (deprecated) | All sessions on the VM         | `vm-console`             | Operator's machine          |

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
the DB is touched and changes appear on next attach. The console does not auto-boot the VM for live
sync — VM start happens only on explicit `attach`.

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

### Installers

Browse and inspect the built-in catalog of installable tools.

| Command                                | Description                        |
| -------------------------------------- | ---------------------------------- |
| `agentworks installer list`            | List all available catalog entries |
| `agentworks installer describe <name>` | Show details of a catalog entry    |

`installer list` accepts `--type` (apt-source, apt-package, system-install-cmd, user-install-cmd)
and `--source` (builtin, user) filters.

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

Completions include dynamic VM, workspace, VM host, session, and template name lookups.

## State

All state is stored in `~/.config/agentworks/agentworks.db` (SQLite). Schema migrations are
forward-only and run automatically.

## Environment Variables

| Variable                      | Description                                     |
| ----------------------------- | ----------------------------------------------- |
| `TAILSCALE_AUTH_KEY`          | Tailscale auth key (skips prompt)               |
| `GIT_CREDENTIALS_<CRED_NAME>` | Git credential for `<CRED_NAME>` (skips prompt) |

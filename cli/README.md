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
agw session delete my-session    # When you're done with it. Agent and workspace are preserved unless this was their last session (see below).

# Alternatively, you can create ephemeral workspaces and agents along with your sessions
agw session create my-ephemeral-session --vm my-vm --new-workspace --new-agent
agw session attach my-ephemeral-session
agw session delete my-ephemeral-session    # This will prompt you to delete the associated workspace and agent, too

# Deleting a session also checks whether its workspace and agent are now unused
# (whether or not this session created them). A workspace is unused once it has
# no sessions; an agent is unused only once it has no sessions AND no standing
# workspace grant (no explicit grant and grant-all unset; a standing grant
# means you still intend to use the agent, so it is left alone). For each
# resource now unused, session delete offers to delete it interactively.
# Under --yes it auto-deletes only a workspace/agent
# this session created; anything else now unused is reported (naming
# `agw workspace delete <name>` / `agw agent delete <name>`) and left in place
# for you to remove by hand.
# One guard applies: if any agent holds an explicit per-workspace grant on the
# now-unused workspace (deleting it would silently revoke that grant), the
# --yes auto-delete is refused and the workspace is reported (naming the
# granting agents) instead, while the interactive offer discloses whose grants
# a delete would revoke. Grant-all agents don't trigger the guard: blanket
# access is policy, not per-workspace intent.

# Finally, create two sessions and a named console
agw session create s1 --vm my-vm --new-workspace --new-agent
agw session create s2 --vm my-vm --new-workspace --new-agent
agw console create my-console s1 s2+1      # The + syntax gives you extra shells as that agent
agw console attach my-console

# Deleting a session drops it from any console that referenced it (no dangling
# references are left behind). session delete lists the affected consoles, and
# for any console left with no sessions it offers to delete the now-empty
# console (interactively). Under --yes it reports the empty console but leaves
# it for you to remove with `agw console delete <name>`. `console remove-sessions`
# gets the same now-empty treatment when it drops a console's last session.
agw session delete s1                      # Reports that my-console still referenced s1

agw console delete my-console              # Extra shells are lost but sessions are preserved
```

## Prerequisites

- Python 3.12+ (uv will install one for you if needed)
- [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) for installation
- [Tailscale](https://tailscale.com/) installed and connected (for VM workspaces)
- One of: [Lima](https://lima-vm.io/), Azure CLI (`az`), AWS credentials for EC2,
  [Proxmox](https://www.proxmox.com/), or WSL2 (for VM provisioning; Azure, AWS, and Proxmox also
  need their [system plugin](#system-plugins) enabled)

## Global Options

| Flag                | Description                                                                                                                                 |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `--non-interactive` | Disable all interactive prompts                                                                                                             |
| `--debug`           | Print the full traceback on unhandled errors, and show the Azure SDK's own log lines that are otherwise suppressed (also via `AGW_DEBUG=1`) |
| `--no-deprecations` | Silence the ambient per-command deprecation banner (`agw doctor` always reports deprecation health)                                         |

When `--non-interactive` is set (or stdin is not a TTY), commands that would normally prompt for
missing values (VM selection, workspace selection, name generation) will fail with a clear error
indicating which flag is required. VM auto-selection still works: if there is exactly one usable VM,
it is used without prompting. `session create` is an intentional exception: it always prompts for
workspace and mode (even when only one choice exists) since those are part of the session's identity
and should be an explicit operator decision.

Domain errors (SSH timeouts, validation failures, missing resources, etc.) surface as a single clean
line: `Error: <message>`. Truly unexpected failures (internal bugs, OS-level errors, third-party
library failures) also get a clean single-line message, plus the full traceback appended to
`~/.config/agentworks/logs/error.log` for debugging. Pass `--debug` (or set `AGW_DEBUG=1`) to print
the traceback to stderr instead. Debug mode also restores the Azure SDK's own credential-chain log
lines, which are otherwise suppressed so a credential failure renders once as the typed error.

On an interactive terminal, output is tastefully colorized by role so it is easy to scan at a
glance: a yellow `Warning:` prefix, a red `Error:` prefix, bold section headers, a dim-green result
line (the closing "VM deleted", "rekeyed", etc.), and dimmed secondary detail. `agw doctor` colors
its per-check status labels the same way (green `[ok]`, yellow `[warn]`, red `[FAIL]`, unstyled
`[info]`), plus its summary line's `fail`/`warn`/`ok` counts. Color is a presentation aid only,
never carried in the message text. It is suppressed automatically when the target stream is not a
terminal (pipes, redirects, CI capture) and under `--non-interactive`, so scripted and captured
output stays byte-plain. Set the `NO_COLOR` environment variable (any value, honored by its
presence) to opt out of color even on a terminal.

Pressing Ctrl-C during a long-running operation triggers best-effort cleanup. Where the operation
can roll back (e.g. `vm create` during the provisioning phase, `workspace create`, `agent create`,
`session create`) it undoes the partial DB / on-VM state and prints `Cancelling X... rolling back.`.
On every platform the `vm create` provisioning-phase rollback also deletes the partially created
backend state: Azure the cloud resource set (VM, NIC, public IP, NSG, vnet, disk), which can take a
minute or two; Proxmox the partially cloned VM (cancelling a still-running clone task first); Lima
the instance (local, or on the site's `vm_host` for a remote site); WSL2 the distro plus its install
directory. A second Ctrl-C abandons that cleanup, printing what to remove manually: the resource
group and name prefix, the node and VMID, or the exact removal command
(`limactl delete --force <name>`, run on the `vm_host` for a remote site, or
`wsl --unregister <name>` plus deleting the install directory it names). Where rollback isn't
possible (`vm reinit`, `agent reinit`, the init phase of `vm create`) it prints a recovery hint: the
next command to run (`vm reinit`, `vm delete --force`, ...). Every cancellation exits with the
conventional SIGINT exit code (130).

## Commands

### Top-Level

| Command                    | Description                              |
| -------------------------- | ---------------------------------------- |
| `agw doctor`               | Check environment and config             |
| `agw version`              | Print the installed CLI version          |
| `agw completion show`      | Print the completion script to stdout    |
| `agw completion install`   | Install the completion script in-place   |
| `agw completion uninstall` | Remove installed completions for a shell |

### VMs

Manage virtual machines across declared vm-sites (Lima local or remote, Azure, AWS EC2, WSL2,
Proxmox).

Where VMs are created is declared as `vm-site` resources: YAML manifests under
`~/.config/agentworks/resources/` that pair a platform (the code that runs VMs on one backend kind)
with its configuration. The `lima-local` and `wsl2` sites ship built in and are always available;
the `azure-vm`, `aws-ec2`, and `proxmox` platforms ship as the opt-in `azure`, `aws`, and `proxmox`
system plugins (see [System Plugins](#system-plugins)) and are not-ready until enabled. Every site
registers on every host and reports not-ready when this host lacks what it needs (wsl2 is
Windows-only; a local Lima site needs `limactl`; a platform may simply not be installed, or its
plugin not enabled): a not-ready site still lists and describes, using it is an error naming the
requirement, and `agw doctor` shows each platform's and site's state with the reason. Run
`agw resource sample vm-site` for commented, ready-to-edit examples (an Azure site, a remote-Lima
site with a `vm_host` key). The former `agw vm-host` registry is gone: a remote Lima host is now
just a vm-site.

> **Note on WSL2:** WSL2 distros share the Windows workstation's lifecycle. They idle-shut after
> ~60s of no `wsl.exe` activity (`vmIdleTimeout` in `.wslconfig`) and do not survive workstation
> shutdown or sleep. Agentworks holds a `wsl.exe` keepalive for the duration of each VM-touching
> command, so individual `agw` operations work cleanly, but agents and sessions on WSL2 are not
> suitable for unattended background workflows. Use a site on a different platform that provides
> true long-lived VMs (e.g. Lima, Azure, Proxmox, etc.) if you need a VM that survives independent
> of your workstation.

| Command                                             | Description                                                   |
| --------------------------------------------------- | ------------------------------------------------------------- |
| `agw vm create <name>`                              | Create a new VM (provision + initialize)                      |
| `agw vm list`                                       | List VMs with status and resources                            |
| `agw vm describe <name>`                            | Show VM details, workspaces, and event log                    |
| `agw vm shell <name> [--workspace <ws>]`            | Admin shell on a VM (optionally rooted in a workspace)        |
| `agw vm exec <name> [--workspace <ws>] -- <cmd...>` | Run a one-shot command as admin (optionally from a workspace) |
| `agw vm start <name>`                               | Start a stopped VM and clear its manual-stop intent           |
| `agw vm stop <name>`                                | Stop a VM and keep it stopped (no auto-start)                 |
| `agw vm reinit <name>`                              | Re-run initialization on a provisioned VM                     |
| `agw vm delete <name>`                              | Delete a VM (with confirmation)                               |
| `agw vm backup <name>`                              | Back up a VM: metadata, agents, workspaces, and files         |
| `agw vm rekey <name>`                               | Assign a new Tailscale auth key to a VM (logout + rejoin)     |
| `agw vm port-forward <name> <ports...>`             | Forward local port(s) to a VM (like kubectl port-forward)     |
| `agw vm logs <name>`                                | Show SSH logs for a VM                                        |
| `agw vm console <name>`                             | _Deprecated_: use `agw console`                               |
| `agw vm add-git-credential <name> <cred>`           | Add or update a git credential                                |

**Power-state semantics:** a VM that stopped on its own (idle timeout, host reboot) is started
automatically, on demand, by any command that needs it live. A VM stopped with `agw vm stop` is
different: that records your intent, so it stays down and commands that would need it refuse with a
hint until you run `agw vm start`, which clears the intent. `agw vm describe` shows which case a
stopped VM is in: its status reads `stopped (manual)` versus `stopped (idle)`.

`vm create <name>` takes the VM name as a required positional. Optional flags: `--template` (a
declared vm-template), `--admin-template` (a declared admin-template; defaults to the reserved
`default` admin-template, which always exists), and `--site` (a declared vm-site; falls back to
`defaults.site`, else the one ENABLED site is inferred when there is exactly one, several prompt
interactively, and non-interactive runs error naming the options). The selected admin-template is
stored on the VM row (NULL = `default`) and drives its admin user on every later `vm reinit`,
`vm shell`, and admin-mode session. An unknown `--admin-template` name fails before any provisioning
or DB work. Hardware (`cpus`, `memory`, `disk`, `swap`) comes from the vm-template and the admin
username from the admin-template; there are no per-create overrides, so to deviate you declare a new
template. On Azure, `cpus` + `memory` select the smallest fitting VM size from the site's catalog
(built-in B-series, or the site's `vm_sizes` platform key); an off-ratio request rounds up and
warns. These are immutable provisioning parameters stored in the database. All initialization
behavior (packages, install commands, etc.) is driven by config. Templates carry no `site`:
placement is per-host, so it never travels inside a shared template.

The first interactive `vm create` asks once for an optional **system slug** (3-20 chars, lowercase
alphanumeric plus dash, no leading/trailing dash): a short identifier for this Agentworks
installation, used to namespace VM hostnames and backend-side names (`{slug}-{vm-name}`) so installs
sharing a cloud account, Proxmox cluster, or Windows/Mac user don't collide. Leave it blank if this
install is the only one using its sites' backends; a blank answer is remembered and it will never
ask again. Non-interactive runs never prompt (a later interactive create still asks once).

`vm reinit` re-runs the initialization phase using the current config without reprovisioning the VM.
Changes to config (new packages, different install commands, etc.) are picked up automatically.

`vm delete` requires `--force` if the VM has workspaces, agents, or sessions. The confirmation
message shows what will be deleted. Pass `--yes` to skip the prompt.

`agw vm shell` is the Agentworks-wrapped entry point; for raw SSH (VS Code Remote-SSH, `scp`, etc.),
use the `awvm--<vm>` alias documented under [Direct SSH aliases](#direct-ssh-aliases).

`vm shell` and `vm exec` both accept `--workspace <ws>` to root the admin session in a workspace
directory on this VM. The workspace's template env joins the env chain (between vm and admin), and
`AGENTWORKS_WORKSPACE` / `AGENTWORKS_WORKSPACE_DIR` are set in the session. The shell variant `cd`s
into the workspace; the exec variant runs the command from the workspace directory. A workspace that
lives on a different VM is rejected with a `ValidationError` before any SSH work.

In both exec commands the `--` separator is only required when the remote command's first token
starts with `-` (it stops Agentworks from reading the token as its own option); without it, a
dash-led first token is rejected with a hint naming the recoveries. Bare commands need no `--`.

Combining `--workspace` with `--platform` works (the shell still `cd`s into the workspace) but the
workspace's template env and the `AGENTWORKS_WORKSPACE` identity vars are not delivered: the
platform-native transports (`limactl shell`, `wsl.exe`) drop the `env=` kwarg by design. Treat
`--platform` as a transport-repair escape hatch, not a routine combination.

`agw vm shell --platform` (legacy alias `--provisioner`, one release) opens the same shell over the
platform-native transport (`limactl shell` for Lima, `wsl.exe` for WSL2, SSH via the VM's public IP
for Azure) instead of Tailscale. Useful when Tailscale itself is the thing you need to reach the VM
to fix (the issue #117 latched DNS state is the canonical case: its heal involves restarting
tailscaled, which would terminate a Tailscale-SSH session mid-sequence). On Azure, the VM's firewall
denies all inbound traffic at baseline; for the duration of the session an ephemeral SSH allow rule
scoped to your detected public IP is created (one per session, so concurrent sessions never tear
down each other's access), and removed again on exit (the public IP itself is permanent). If your
SSH traffic egresses through a different address than the detection sees (VPN split tunnel, proxy,
CGNAT), set `ssh_allow_cidrs` in the config's `[operator]` section to a list of IPv4 addresses
and/or CIDRs to allow additionally; if detection fails entirely, those entries are used alone.
Proxmox isn't supported by this flag because the QEMU guest agent's exec interface is one-shot and
non-interactive; use the Proxmox web UI's serial console (`VM > Console` in the Proxmox VE web UI)
as the equivalent escape hatch.

### Workspaces

Manage workspaces on VMs.

| Command                              | Description                         |
| ------------------------------------ | ----------------------------------- |
| `agw workspace create <name>`        | Create a workspace on a VM          |
| `agw workspace describe <name>`      | Show workspace details and sessions |
| `agw workspace list`                 | List workspaces                     |
| `agw workspace copy <source> <name>` | Copy a workspace to a new VM        |
| `agw workspace rehome <name>`        | Move workspace to a new path        |
| `agw workspace repair <name>`        | Repair workspace infrastructure     |
| `agw workspace delete <name>`        | Delete a workspace                  |

`workspace create <name>` takes the workspace name as a required positional. Optional flags: `--vm`,
`--template`, and `--open-vscode`.

`workspace copy <source> <name>` copies a workspace to a new VM workspace. Accepts `--vm`. Source
and destination can be the same VM (a clone) or different VMs.

`workspace list` accepts `--vm` to narrow the result set to one VM's workspaces. An unknown name in
the filter is an error, not an empty result.

`workspace delete` requires `--force` if the workspace has sessions. Running sessions are killed
during deletion. Pass `--yes` to skip the confirmation prompt.

There is deliberately no `workspace shell`: a shell rooted in a workspace is always _somebody's_
shell. Use `agw vm shell <vm> --workspace <ws>` for an admin shell or
`agw agent shell <agent> --workspace <ws>` for an agent shell. For curated tmux views over a
workspace's sessions, use `agw console create` + `agw console attach`.

### Agents

Manage agents (isolated Linux users) on VMs. Agents are VM-scoped and access workspaces via grants.

| Command                                                | Description                              |
| ------------------------------------------------------ | ---------------------------------------- |
| `agw agent create <name> [--vm]`                       | Create an agent on a VM                  |
| `agw agent list [--vm <vm>]`                           | List agents                              |
| `agw agent describe <name>`                            | Show agent details and grants            |
| `agw agent reinit <name> [--update-template <tmpl>]`   | Re-run agent setup                       |
| `agw agent grant-workspaces <name> <ws>...`            | Grant workspace access                   |
| `agw agent grant-workspaces <name> --all`              | Grant access to all workspaces           |
| `agw agent revoke-workspaces <name> <ws>...`           | Revoke workspace access                  |
| `agw agent revoke-workspaces <name> --all`             | Revoke all explicit grants               |
| `agw agent shell <name> [--workspace <ws>]`            | Open an interactive shell as the agent   |
| `agw agent exec <name> [--workspace <ws>] -- <cmd...>` | Run a one-shot command non-interactively |
| `agw agent delete <name>`                              | Delete an agent                          |

`agent create <name>` takes the agent name as a required positional. Optional flags: `--vm`,
`--template`, and `--grant-all-workspaces`.

`agent list` accepts `--vm` to narrow the result set to one VM's agents. An unknown name in the
filter is an error, not an empty result.

`agent reinit --update-template <tmpl>` re-points the agent to a different declared template
(validated against the resource registry, then persisted) before re-running setup. An unknown
template name is rejected up front, leaving the stored binding unchanged.

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

`agw agent shell` / `agw agent exec` are Agentworks-wrapped entry points; for raw SSH access to an
agent (e.g. from VS Code Remote-SSH or `scp`), use the `awagent--<agent>` alias documented under
[Direct SSH aliases](#direct-ssh-aliases).

### Direct SSH Aliases

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
or a comma-separated list (`--vm vm1,vm2`); commas within a filter are OR-ed together. An unknown
name in a filter is an error, not an empty result. `--agent <name>` matches agent-mode sessions
only; `--admin` matches admin-mode sessions only (the two are mutually exclusive).

`session stop` and `session restart` operate on a single session by default. Pass `--all`
(`session stop`/`session restart`) or `--all-stopped` (`session restart`) to batch over the sessions
on the VM. The batch form accepts `--vm <vm>`, `--workspace <ws>`, `--agent <agent>`, and `--admin`
to narrow the set; filters compose with AND and require one of the batch flags. The name filters
accept a single value or a comma-separated list (`--vm vm1,vm2`); commas within a filter are OR-ed
together, and an unknown name in a filter is an error, not an empty result. `--agent` matches
agent-mode sessions only; `--admin` matches admin-mode sessions only (the two are mutually
exclusive). Pass `--force` to stop/restart broken sessions via PID kill.

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

### Named Consoles

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
| `agw console remove-sessions <name> <sessions...>`  | Remove session windows (accepts `-y`/`--yes`)                     |
| `agw console reorder-sessions <name> <sessions...>` | Bump member sessions to the front in the order given              |
| `agw console add-shell <name> <session>`            | Add a shell pane to a session window (accepts `--cwd`, `--admin`) |
| `agw console restore-session <name> <session>`      | Repair one session window against its configured shell list       |

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
OR-ed together, and an unknown name in a filter is an error, not an empty result. The `--workspace`
and `--agent` filters use "any session matches" semantics: a console is listed if at least one of
its member sessions belongs to the given workspace / runs as the given agent. When `--workspace` and
`--agent` are both passed, the SAME session must satisfy both predicates. The session count
displayed is the total membership, not the count of matching sessions. Filters compose with AND.

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

`console restore-session` repairs a single session window in a running console: it re-adds shell
panes you killed by accident (each back in its configured position) and rebuilds the window from
config if it is gone entirely. It is additive and never kills a live pane or window, so it refuses,
pointing you at `console attach --recreate`, when the fix would require destroying live state: more
panes live than configured, shell panes it can't map back to the config (untagged, duplicated, or
out of range), or a window whose session pane itself was killed (the console then shows a plain
shell where the session should be).

Memberships and shell layouts persist in the database. `agw console attach` builds the tmux session
on first attach (or with `--recreate`); subsequent attaches reuse the running tmux session. Adding
or removing sessions/shells while a console is attached updates the live tmux state immediately
(best-effort); when the console isn't running on the VM, only the DB is updated and changes appear
on next attach.

When `console remove-sessions` (or the session-delete cascade) leaves a console with no configured
sessions, the console is a dead end (`console attach` would just warn "has no members"). It offers
to delete the now-empty console; pass `-y`/`--yes` to run non-interactively, which reports the
emptied console and leaves it in place (delete it yourself with `agw console delete <name>`). The
removed sessions themselves are untouched; only their membership in the console is removed.

<!-- Linked from the top-level README; rename only if you also update README.md. -->

### tmux Architecture

Each session runs in its own locked-down tmux session on the VM. There are several ways to interact
with sessions, at different scopes:

| Method                    | Scope                            | tmux session name   | Entry point        |
| ------------------------- | -------------------------------- | ------------------- | ------------------ |
| `session attach`          | One session                      | `<session-name>`    | Operator's machine |
| `console`                 | Curated subset across workspaces | `aw-console-<name>` | Operator's machine |
| `vm console` (deprecated) | All sessions on the VM           | `vm-console`        | Operator's machine |

#### Session tmux Sessions

Each session gets a locked-down tmux session using the session name directly as the tmux session
name. The user's `~/.tmux.conf` (customizable via dotfiles) is loaded first so that familiar
keybindings (prefix, detach, copy mode, scroll) work for direct `session attach`. Window/pane
creation, session management, and the command prompt are selectively unbound.

Agent-mode sessions each get their own tmux socket, so every session runs as its own tmux server
rather than as a window in a shared one. The sockets are grouped in a per-agent directory whose
ownership keeps one agent from reaching another's (cross-agent isolation), and giving each session
its own server means it inherits the environment delivered over its own SSH connection instead of
leaking env across sessions through a shared server. The agent's shell attaches directly to the tmux
pane PTY, and each socket path is persisted in the database.

#### Named Console

`console attach <name>` creates or attaches to the `aw-console-<name>` tmux session. Each member
session becomes a window running the same wrapper used by the VM console, plus a configurable number
of extra shell panes (default user = session's agent user, default cwd = workspace root; override
per pane with `--cwd` / `--admin` on `console add-shell`).

```text
aw-console-backend
  Window 1: auth-server                attached session + 2 agent shells (workspace root)
  Window 2: auth-tests                 attached session + 1 agent shell
  Window 3: docs                       attached session only
```

Membership and per-session shell layout are stored in the database; see
[Named Consoles](#named-consoles) above for the lazy-build and live-update semantics. Of note here
is the VM-boot behavior: the mutation commands (`add-sessions`, `remove-sessions`,
`reorder-sessions`, `add-shell`) never auto-boot the VM, whereas the explicit attach/repair commands
(`attach`, `restore-session`) do start a stopped VM, since their job is to bring live state up.

#### Workspace tmuxinator Config

Workspaces with tmuxinator enabled in their template (the default) carry a tmuxinator config
(`.tmuxinator.yml` in the workspace root, symlinked as `~/.config/tmuxinator/ws-<name>-console.yml`)
describing a `ws-<name>-console` session: an admin-shell window plus one window per session. It is
regenerated whenever sessions change. The `agw workspace console` command that attached to it was
removed (superseded by named consoles); the config remains usable directly on the VM via
`tmuxinator start ws-<name>-console` (e.g. inside VS Code's integrated terminal).

#### VM Console (Deprecated)

`vm console` creates or attaches to the `vm-console` session, which spans all sessions on the VM.
Built dynamically (not via tmuxinator). Superseded by named consoles, which let you curate which
sessions are in scope at any moment instead of seeing every session on the VM. Will be removed in a
future release.

#### Shells

`vm shell` and `agent shell` open plain login shells with no tmux (optionally rooted in a workspace
via `--workspace <ws>`). Use these when you just need a terminal without the console structure.

#### Key Behaviors

- **Direct attach** (`session attach`): the user's prefix key, detach, copy mode, and scroll all
  work normally. Status bar is hidden since there is only one pane.
- **Consoles** (`console`, `vm console`): the console's prefix key eclipses the inner session's
  prefix, so window switching, detach, etc. all operate at the console level. Session windows use a
  wrapper that re-attaches if the inner session disconnects and shows a message when the session
  ends.
- **Nesting protection**: the console commands refuse to run inside an existing tmux session to
  avoid prefix key conflicts. Pass `--allow-nesting` to override.
- **Console lifecycle**: consoles are independent of sessions. Killing or detaching a console does
  not affect running sessions. `--recreate` rebuilds from scratch.
- **Dropped connections restore your terminal**: an attach reconfigures your local terminal
  (alternate screen, mouse reporting, bracketed paste), and tmux only undoes that on a clean detach.
  When the connection dies instead (laptop suspends, lid closes, Wi-Fi drops), agentworks restores
  the terminal itself on the way out, so you don't land in a tab that echoes nothing and emits mouse
  escape codes on every click. Interactive SSH also carries client keepalives, which bound how long
  a dead connection hangs before it gives up (roughly a minute) so that cleanup can run. The
  tradeoff is that an outage longer than that budget ends the attach even if it would eventually
  have recovered, such as a slow Wi-Fi handoff or a tunnel renegotiation. Nothing on the VM is
  affected either way, so reattaching picks the console back up where you left it. A tab killed
  outright, before the command can return, runs no cleanup at all. The next attach sanitizes on the
  way in, which clears the leftover emulator modes (the mouse codes, the alternate screen), but it
  cannot recover a lost line discipline: there is no record of what echo and line-editing looked
  like before the attach that died, so a tab left not echoing stays that way. Open a fresh tab for
  that case.
- **A dropped connection says so.** When ssh exits 255 (its transport-failure code, as opposed to
  the remote command simply exiting non-zero) you get a one-line notice that the connection dropped
  and the terminal was restored. Without it the failure is silent: ssh writes its own diagnostic
  while tmux still holds the alternate screen, so leaving that screen discards the message before
  you can read it. A clean detach prints nothing.

### Session Templates

A session template selects the **harness integration** that runs the session's workload. The
integration is a [capability](../docs/guides/resources.md#harness-integrations) that owns
starting/restarting the harness or shell and checking its required executables; the template's
`spec.harness_integration` is one tagged table whose `name` key selects the integration and whose
remaining keys are the config block that integration validates. The old `harness` / `harness_config`
inputs still load with a deprecation warning in 0.13.0 and can be rewritten with
`agw resource migrate`. A template that names no integration runs the built-in `shell` integration
(a login shell, `$SHELL --login`, or an operator-supplied command), which is the built-in `default`
template's behavior. Define custom templates as `session-template` resources:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: htop
  description: Live process monitor
spec:
  harness_integration:
    name: shell
    command: htop
    required_commands: [htop]
```

`shell`'s config vocabulary is the command surface every template used to spell at the spec top
level:

- `command`: the pane command (empty/omitted is a plain login shell). Supports `{{session_name}}`
  and `{{workspace_name}}` variable substitution (double-brace syntax).
- `restart_command`: used by `session restart`, for a tool that needs a different invocation on
  restart. If omitted, `command` is used. (To run Claude Code, prefer the dedicated `claude-code`
  integration below, which resumes the previous conversation on its own.)
- `required_commands`: executables the command needs, checked on the session's launch target (the
  agent, or the VM admin for admin sessions) before any state mutation, so launching a session whose
  tool is not installed fails fast with a clear error instead of a cryptic downstream tmux failure.
  Merged (de-duped, order-preserving) across template inheritance.

In a YAML manifest these three keys live only inside the `harness_integration` table; spelling any
of them at the `spec` top level is a load error that points you at the nested shape. That check is
one instance of a general deprecated-field notice: any resource kind can flag retired or relocated
spec fields with an actionable message (a hard load error when ignoring the field would change
behavior, otherwise a warning that `agw doctor` also surfaces). It is separate from the TOML
flat-field handling below, which is a permanent supported spelling, not a deprecation.

The `claude-code` integration runs Claude Code as the session: `session create` starts a new Claude
session and `session restart` resumes the same conversation when its transcript still exists on disk
(launching fresh when Claude never wrote one). It ships as the opt-in `claude` system plugin (see
[System Plugins](#system-plugins)), disabled by default: a session-template naming it still lists
ready, but creating a session on it is refused with an "enable plugin `claude`" hint until you add
`claude` to `[plugins].system`. (The built-in `shell` integration stays the default and needs no
opt-in.) Once enabled, it needs only that `claude` is installed on the launch target, and announces
the chosen action (resume vs new session) in the pane, so the decision is never silent. Its config
vocabulary is three optional fields:

- `permission_mode`: forwarded verbatim to `claude --permission-mode` (its choice set is Claude's,
  not validated here).
- `model`: forwarded verbatim to `claude --model`.
- `extra_args`: a list of raw argv tokens appended last, the escape hatch for any flag the
  integration does not model. Each element is one argv token (shell-quoted, never re-split), and
  elements support the `{{session_name}}` / `{{workspace_name}}` variables.

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: claude
  description: Claude Code session
spec:
  harness_integration:
    name: claude-code
    permission_mode: acceptEdits
    model: opus
```

The `codex` integration runs Codex the same way: `session create` starts a new Codex session and
`session restart` resumes the same conversation once Codex has recorded it. Codex mints its own
session ids, so the integration discovers the id from Codex's on-disk state after the first launch
and stores it (a session archived with `codex archive` is deliberately treated as not resumable, and
a fresh one is started). It ships as the opt-in `codex` system plugin, disabled by default with the
same gating as `claude-code` above. Once enabled, it needs only that `codex` is installed on the
launch target, and announces the chosen action (resume, adopt-and-resume, or new session) in the
pane. Its config vocabulary is nine optional fields: `model`, `sandbox`, `approval_policy`, and
`profile` forward verbatim to `codex -m` / `-s` / `-a` / `-p` (their choice sets are Codex's, not
validated here); `network` (bool) forwards to Codex's `sandbox_workspace_write.network_access`
config key (sandboxed network is off by default, so coding sessions usually want `network: true`);
`writable_dirs` (list of paths) emits one `codex --add-dir` each; `web_search` (bool) enables the
live web-search tool (`codex --search`); `disable_strict_config` (bool) suppresses the
`--strict-config` the integration otherwise always passes (strictness makes a Codex config mistake
or a Codex-renamed key fail loudly at launch instead of being silently ignored); and `extra_args` is
the same appended-last escape hatch:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: codex
  description: Codex session
spec:
  harness_integration:
    name: codex
    sandbox: workspace-write
    approval_policy: on-request
    network: true
```

The integration-plus-config pair inherits as a unit: a child that restates the same integration
merges its config block into the parent's (child wins per key; `shell` unions `required_commands`
and `codex` unions `writable_dirs`), while a child naming a _different_ integration starts from a
fresh config (the parent's block was addressed to a different tool). `env`, `inherits`, and the
description merge as usual.

**TOML** (`[session_templates.<name>]` in `config.toml`, deprecated but supported): use the same
canonical pair, `harness_integration` plus a nested `harness_integration_config` table:

```toml
[session_templates.htop]
harness_integration = "shell"
[session_templates.htop.harness_integration_config]
command = "htop"
required_commands = ["htop"]
```

For `shell`, the legacy flat keys `command` / `restart_command` / `required_commands` keep working
at the section top level and are hoisted into `harness_integration = "shell"` plus the equivalent
`harness_integration_config`. The flat form is the documented default TOML shape; YAML manifests are
the primary authoring surface (run `agw resource sample session-template`). The flat fields cannot
be combined with a non-`shell` `harness_integration` or with an explicit
`harness_integration_config` table (one spelling per declaration), and both conflicts are load
errors. One inheritance interaction worth noting: a legacy flat-field child under a
`harness_integration: claude-code` parent hoists to `harness_integration = "shell"`, which (per the
different-integration rule) switches the lineage back to `shell` with a fresh config.

The old TOML `harness` / `harness_config` pair remains accepted with a deprecation warning through
0.13.0. It cannot be mixed with the canonical pair; run `agw resource migrate` to rewrite it.

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
agent templates, workspace templates, apt / install-command entries, git credential providers,
secret backends, etc. The two commands below stop at the framework-uniform fields (`kind`, `name`,
`origin`, `references`, `used_by`, `description`). For kind-specific detail (secret backend
mappings, template inheritance chains, resolution previews), reach for the per-kind command (e.g.
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
where variant is `operator`, `auto`, `builtin`, or `plugin`. Disabled rows (a not-enabled system
plugin's capabilities and bundled resources) are hidden by default; pass `--include-disabled` to
reveal them (combine with `--origin plugin` to see just a not-enabled plugin's rows). `--names-only`
emits `kind/name` per line and backs shell completion (`/` cannot appear in resource names, so the
split is unambiguous). The `kind/name` token is the one grammar across the resource group:
`resource describe secret/npm-token` and `resource migrate vm-template/dev` take the same shape.

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

- **Settings** live in `~/.config/agentworks/config.toml`: your identity, paths, defaults, and the
  secret backend chain. Run `agw config init` to generate a sample; see
  [sample-config.toml](agentworks/sample-config.toml) for the full reference.
- **Resources** (secrets, templates, git credentials, vm-sites, apt / install-command entries) are
  declared as YAML manifests under `~/.config/agentworks/resources/`, auto-loaded whenever a command
  needs them. `agw resource sample <kind>` prints a commented starter (`--all` for every kind). The
  classic TOML resource sections keep working (deprecated, with one aggregated load warning naming
  the sections present; silence it with the global `--no-deprecations` flag); `agw resource migrate`
  moves them to YAML whenever you like. See [docs/guides/resources.md](../docs/guides/resources.md).

Settings sections (`config.toml`, permanent):

- `[operator]` -- SSH keys (required), additional authorized keys, SSH config management
- `[paths]` -- VM workspace, VS Code workspace file, and backup directories
- `[defaults]`: `site`, the default vm-site for `vm create` (`platform` is the deprecated alias)
- `[session.config]` -- session defaults (history limit)
- `[secret_config]` -- active secret backend chain (`[secret_backends.*]` sections are deprecated
  no-ops; see Secret Backends below)
- `[plugins]`: the plugin-subsystem namespace; its `system` key is the opt-in list of enabled system
  plugins (see [System Plugins](#system-plugins) below)

Resource kinds (YAML manifests; the deprecated TOML section is noted for each):

- `vm-site` (`[azure]` / `[proxmox]`, flat legacy shape): a configured place to create VMs.
  `spec.platform` is one tagged table: its `name` key selects the backing platform and the remaining
  keys are its settings (Azure subscription/resource-group/region plus an optional
  `service_principal` block to authenticate as a specific service principal instead of with ambient
  credentials, Proxmox API endpoint + token secret, remote-Lima `vm_host`). The `lima-local` and
  `wsl2` sites ship built in (on hosts where their platform can run) and their names are reserved
- `vm-platform`: read-only capability rows for the VM platforms (`lima`, `wsl2` built in;
  `azure-vm`, `proxmox`, and `aws-ec2` ship as the opt-in `azure`, `proxmox`, and `aws` system
  plugins, disabled by default, see [System Plugins](#system-plugins)); listed by
  `agw resource kinds`, never declared
- `vm-template` (`[vm_templates.*]`): VM resources, apt packages, system install commands, mise, and
  the target `site`
- `admin-template` (`[admin.config]`) -- admin user shell, dotfiles, git credentials, user install
  commands, mise
- `agent-template` (`[agent_templates.*]`) -- agent user shell, dotfiles, git credentials, user
  install commands, mise
- `session-template` (`[session_templates.*]`) -- session commands with variable substitution
- `workspace-template` (`[workspace_templates.*]`): workspace repo, tmuxinator, optional git
  identity (`git_user_name` / `git_user_email`, stamped into the cloned repo), inheritance
- `named-console-template` (`[named_console]`) -- named-console layout (tmux preset names +
  `aw-session-vertical`)
- `git-credential` (`[git_credentials.*]`) -- git credentials; `spec.provider` selects github or
  azdo (TOML also accepts the legacy `type`)
- `secret` (`[secrets.*]`) -- secret declarations referenced by `{secret: name}` env entries
- `apt-source` / `apt-package` / `system-install-command` / `user-install-command`
  (`[apt_sources.*]` etc.): apt / install-command extensions
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
- `prompt` -- interactive prompt; you are never asked for the same secret twice in one command, and
  all prompting happens before the command starts changing anything.

**Resolve before any mutation:** a command resolves all the secrets its plan needs up front, before
it starts changing anything. A secret that cannot be resolved by any active backend fails at
preflight with a hint (`agw secret describe <name>` shows how each backend looks it up), before any
prompt and before any VM is started. The set of secrets is computed from the command's static
filters (positional targets, `--vm`, `--workspace`, `--agent`, etc.) -- dynamic predicates like
`--all-stopped` apply later, so the prompted set may over-approximate. Non-interactive mode (no TTY
or `--non-interactive`) surfaces missing secrets as `SecretUnavailableError` with a per-secret hint
naming which backends were tried. Commands that join existing shells (`session attach`,
`session list`, `console attach` against a live tmux session, `console add-sessions`) consume no
secrets.

**Miss semantics:** what "not found" means depends on the backend. Conventional sources (`env-var`,
`prompt`) treat a missing value as a soft miss and fall through to the next backend in the chain --
a `GITHUB_TOKEN` env var that isn't set is just-not-set, not a config error. Persistent-store
backends (1Password, Vault when implemented) will treat an explicit mapping that doesn't resolve as
a hard miss: they raise `SecretMappingError` and the chain halts so a wrong `op://` URI doesn't
quietly fall through to a prompt that masks the real problem.

Inspect the merged result for any context with `agw env show`:

```bash
agw env show --session my-session              # secrets redacted as <from secret: name>
agw env show --vm my-vm --resolve              # resolves through the active backend chain
```

(The flag was formerly spelled `--reveal-secrets`; it was renamed to `--resolve` as a breaking
change, the old spelling no longer works.)

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
references are caught earlier as a hard config-load error before doctor runs. Git-credential tokens
are just secrets: their _resolvability_ reports as ordinary `git-token-<name>` rows in the Secrets
group, like any other secret. Doctor is preflight-only and never prompts, so it does not
authenticate tokens against their provider; live verification (a token expired, revoked, or
wrong-scope) happens at the capability `runup()` stage inside provisioning ops, and on-demand via
the planned `agw doctor --runup` (which may prompt). The Tailscale group checks only workstation
connectivity; the auth key is the `tailscale-auth-key` secret row.

When the config or a resource manifest fails to load, the groups that depend on them (VM sites,
Secrets) do not vanish: each renders a single
`[info] ... skipped (config or manifests unavailable; see the Configuration group)` row, so a
degraded run keeps the same section skeleton as a healthy one and the Configuration group carries
the actual failure.

### Secret Backends

A **backend** is a capability resource that produces secret values (`env-var`, `prompt` built in;
`onepassword` ships as the opt-in `onepassword` system plugin, disabled by default, see
[System Plugins](#system-plugins) below): a read-only row backed by registered code, listed by
`agw resource list --kind secret-backend` and activated in precedence order by the chain
(`[secret_config].backends`). Per-secret behavior -- identifier overrides, structured store
addressing, opt-outs -- lives in each secret's `backend_mappings.<backend>`.

### System Plugins

Agentworks ships some vendor- and tool-specific capabilities (VM platforms, harness integrations,
git-credential providers, secret backends) as **system plugins**: separable bundles that are
installed but off by default. The shipped build installs `azure` (the `azure-vm` VM platform, the
`azdo` git-credential provider, and the `az-cli` install-command), `proxmox` (the `proxmox` VM
platform), `aws` (the `aws-ec2` VM platform), `onepassword` (the `onepassword` secret backend),
`claude` (the `claude-code` harness integration and the `claude` CLI install-command), and `codex`
(the `codex` harness integration and the `codex` CLI install-command). (This is a different sense of
"plugin" from [Claude Code Plugins](#claude-code-plugins) below, which installs marketplace plugins
into Claude Code itself.)

Opt in by name in `config.toml`:

```toml
[plugins]
system = ["azure", "aws", "proxmox", "onepassword", "claude", "codex"]   # only the ones you use
```

A resource that references a not-enabled plugin's contribution (an `azure-vm` vm-site, a
`claude-code` session-template, a secret mapped to `onepassword`, ...) is not-ready, or refused at
use, with an "enable plugin `<name>`" hint, never an unknown-name error. The default local path (the
`lima` / `wsl2` platforms, the `shell` harness integration, the `env-var` / `prompt` secret
backends, and the `github` git-credential provider) is built in, always on, and needs no `[plugins]`
entry.

A not-enabled plugin's rows are hidden from `agw resource list` by default; pass
`--include-disabled` to reveal them (see [Resource Registry](#resource-registry) above).
`agw doctor` has a **System plugins** group listing every installed plugin, its description, and
whether it is enabled.

See [docs/guides/resources.md](../docs/guides/resources.md#system-plugins) for the full model
(origins, the disabled-resource semantics, config-error deferral) and the upgrade note for configs
that relied on Azure, Proxmox, 1Password, or Claude Code before they became opt-in.

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

### Built-in Apt / Install-Command Entries

Agentworks ships built-in entries for common tools (apt sources, apt packages, system install
commands, and user install commands), bundled as YAML manifests under
`agentworks/manifests/builtin/`. Run
`agw resource list --kind apt-package,system-install-command,user-install-command,apt-source` to see
what is available (or filter to any single kind). Reference these entries by name from VM, admin,
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

Initialization is fully declarative, driven entirely by config. `vm create` only accepts a name,
`--template`, `--admin-template`, and `--site`; the immutable provisioning parameters (resources,
admin username) come from the selected templates. `vm reinit` takes only the VM name and re-runs
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

Completions include dynamic VM, vm-site, workspace, session, and template name lookups.

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

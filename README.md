# Agentworks

[![CI](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml/badge.svg)](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![Python](https://img.shields.io/pypi/pyversions/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A comprehensive toolkit for managing agentic workloads: VMs, workspaces, agents, sessions,
harnesses, secrets/config, and the tools that glue them together. Built around the conviction that
autonomy, security, and control are not mutually exclusive: a good platform makes it possible and
straightforward to have it all.

Run a fleet from one workstation. **Durable agents** keep their own tools, git credentials, and
accumulated tool state (a coding assistant's context and memory, interactive logins), each as an
isolated Linux user on a VM you control. **Disposable sessions** spin up against them for a single
piece of work and are thrown away when done. One `agw` CLI drives all of it, declaratively, over an
SSH-on-Tailscale control plane.

## Architecture at a Glance

The operator runs the `agw` CLI on their workstation. VMs are created at declared **vm-sites**
(configured places to create VMs), each backed by a **vm-platform** that knows how to work with a
given provider (Lima, WSL2, Proxmox, Azure VMs, and AWS EC2 today, with more to come). Regardless of
the platform, every VM runs the same base operating system (Debian Bookworm), is joined to the same
Tailscale tailnet, and is accessible over SSH at its Tailscale IP address using the operator's keys.

![Agentworks topology: the operator's workstation runs the agw CLI, which creates VMs at declared sites across local platforms (Lima or WSL2), a remote SSH VM site (e.g. Lima), Azure, and Proxmox, with room reserved for future platforms. Every VM and the workstation itself join a shared Tailnet overlay, which is how the CLI reaches them all.](docs/images/agw-topology.png)

Beyond the VMs themselves, Agentworks provides several layered primitives for organizing agentic
workloads:

- Project files and repositories can be organized into **workspaces** (as filesystem subtrees).
- **Agents** each have their own Linux user, which provides a strong isolation boundary for
  capabilities and access.
- Agentic workloads (simple shell, Claude Code, Codex, etc.) can be run as **sessions** via tmux,
  which provides for both persistence and the ability to attach to and detach as needed.
- Each session launches a **harness** that knows how to run a particular tool (e.g. a Claude Code or
  Codex instance, or just a plain login shell). The harness owns start/restart semantics (e.g.
  resuming a Claude Code or Codex conversation right where it left off) as well as validating the
  target environment for its tooling. Additionally, since each harness is tightly coupled to its
  target tooling, it is the perfect place to grow further tool-specific functionality
  (authentication handling, specific configuration, deeper integrations, ...).
- Sessions can be organized into **named consoles**: curated tmux views that organize active
  sessions along with optional extra shell panes.
- Both **config** and **secrets** (together with **secret backends**) can be managed and securely
  injected at any level (VM, workspace, agent, session) to control access and behavior.

And all of this is managed via a **declarative, idempotent configuration system** that makes it easy
for operators to define, evolve, and scale their infrastructure over time.

Zooming in on a single VM, the diagram below shows how these primitives fit together inside one
machine: sessions (each running a harness and drawing on injected secrets/config) run as isolated
Linux users, work in workspaces, and can be grouped into named consoles, all reachable over the
tailnet.

![Agentworks VM internals: an Agentworks VM at a vm-site runs sessions, each pairing a tmux session and harness with injected secrets and config. Sessions run as fully isolated Linux users (the admin user or an agent user) and work inside workspaces backed by git repos. Any number of sessions can be organized into named consoles, and a tailnet NIC connects the VM directly to the tailnet regardless of platform. The VM sits on a configured platform instance, alongside other VMs in the site and other vm-sites.](docs/images/agw-vm-internals.png)

## Getting Started

Install from PyPI:

```bash
uv tool install agentworks-cli
# or:  pipx install agentworks-cli
```

The everyday command is `agw` (the longer `agentworks` is installed too). Set up your config, then
stand up a VM and drive your first session:

```bash
agw config init        # writes ~/.config/agentworks/config.toml
agw config edit        # fill in required fields (at minimum, your operator SSH keys)
agw doctor             # sanity-check tools, Tailscale, config, and the local DB

# Create a VM and run a session on it
agw vm create my-vm
agw session create my-session --vm my-vm --new-workspace --new-agent
agw session attach my-session      # detach with tmux (Ctrl-b d) and it keeps running
agw session delete my-session      # offers to clean up the workspace and agent it created
```

That is the whole loop: create a VM once, then spin sessions up and down against it. See
[cli/README.md](cli/README.md) for a guided first session, the full command reference, configuration
schema, and tmux architecture.

## Core Concepts

Agentworks organizes work into a handful of layered concepts.

### The Operator

A single human **operator** is in control of all agentic workloads: they create VMs, workspaces,
agents, and sessions, and orchestrate how the pieces interact, all through the `agw` CLI on their
workstation. (We reserve "user" for the technical Linux users on the VMs, the admin user and the
agentic identities.)

### VMs - The Compute Environment

A VM is the base **compute environment** and the strong isolation boundary
([ADR 0001](docs/adrs/0001-vm-based-infrastructure.md)): a full Linux environment (daemonized
services, multi-user collaboration, containers) rather than a narrower sandbox. Every VM runs the
same base OS (Debian Bookworm, [ADR 0002](docs/adrs/0002-use-debian-as-the-vm-base-image.md)) for
consistency. VMs are meant to be long-lived, are provisioned and templated declaratively, and can be
"reinitialized" to pick up template or config changes. Each VM has an admin user with full sudo used
for provisioning and management.

### Workspaces - The Project

A workspace is the **project scope**: a root directory, optionally cloned from a git repository,
mapped to a Linux group whose permissions and ACLs give every member collaborative access.
Workspace-level configuration (e.g. Claude Code's project settings) shapes how tools behave inside
it. Any number of workspaces can map to the same repository; each is a full independent clone.

### Agents - The Actor

An agent is a **security identity**: its own full Linux user, with the isolation and permissions
that entails (its own processes, private files, shell environment, and credentials). The boundary is
the standard Unix one, discretionary access control between users sharing a kernel: it keeps one
agent's mistakes and compromises away from another's state. It is deliberately not a sandbox that
restricts what the agent's own user may do, which is why the VM is the boundary that does the heavy
lifting when a stronger one is needed. Agents are mapped to workspaces (explicitly via grants or
implicitly via sessions), and that mapping drives the group and filesystem permissions that bound
what they can reach.

### Sessions and Harnesses - The Workloads

A **session** runs a specific **harness** as an agent user (or the admin user) in a workspace on a
VM. The session is the outer wrapper (the tmux session, config/secret specifications); the harness
is the piece that knows how to run a particular tool (a Claude Code instance, or just a plain login
shell), owning start/restart semantics and checking the tool's executables are present. A session
template selects a harness with a tagged table (e.g. `harness: {name: claude-code}`); a template
that names none gets the built-in `shell` harness, which just runs a login shell. Because harnesses
are a distinct extension layer, they can integrate tightly with their target tool. A unique name,
persistent tmux session, and harness-specific resume let the operator run any number of concurrent
workloads and attach, detach, stop, restart, and delete them at will. Whatever the harness, tmux
always owns the pane and its tty; the harness only decides what runs inside it.

### Named Consoles - Organizing Active Work

Once more than a handful of sessions are running, a **named console** is a curated tmux view that
groups the sessions (on a VM) you are actively working across, optionally with extra shell panes
pre-opened. Each console is its own persistent tmux session, built once and attached to and detached
from independently of the underlying sessions' lifecycles. Consoles reference sessions without
owning them: a session can appear in any number of consoles (or none), and adding or removing it
never affects the session itself, so you can slice the same pool of running work into whatever
task-focused views make sense (one per feature, incident, or review). See
[Named Consoles](cli/README.md#named-consoles) in the CLI reference for the command surface.

## Why It's Built This Way

A few convictions shape the whole design. The short version:

- **Autonomy and control are not a tradeoff.** Much of the ecosystem treats loss of control as the
  price of agentic autonomy; Agentworks is built on the opposite bet, that the right platform lets
  you have both.
- **Composable, Linux-native isolation.** The hard boundary is the VM; agents are Linux users and
  workspaces are Linux groups. Use the full model or any subset, and because it is all ordinary
  users, groups, and filesystem permissions, graduated privilege between cooperating agents (a
  low-privilege researcher handing artifacts to a privileged actor) is an everyday pattern, not a
  special case.
- **Durable agents, disposable sessions.** A durable agent is set up once and accrues the state a
  template cannot reproduce (tool context, memory, interactive logins); disposable sessions run
  against it and are thrown away. The agent carries the identity and its accumulated state; the
  session is just the unit of work.
- **Declarative and idempotent.** Every layer is templated and declared, and the long-lived
  resources (VMs and agents) can be reinitialized to pick up changes, so environments stay
  consistent and evolve predictably rather than drifting.

The full reasoning, including the threat model Agentworks is designed against and how it bounds
blast radius (and what it deliberately does not do), is in [Why Agentworks](docs/why-agentworks.md).

## Tightly Integrated Tools

In the spirit of opinionated consistency, Agentworks tightly integrates a small set of excellent
tools rather than abstracting over interchangeable alternatives. Users are encouraged to embrace
them rather than work around them.

- **SSH** is the control plane for every VM operation: provisioning, initialization, agent and
  session management, file transfer, command execution. The operator's key (configured in
  `[operator]`) is deployed during provisioning and is the sole authentication mechanism thereafter.
  Provisioning uses the platform's native transport (Lima shell, Azure or EC2 public IP, WSL2 exec,
  or Proxmox guest agent); once Tailscale is joined, all further access goes over the tailnet, and
  `~/.ssh/config` entries are managed automatically so standard tools (scp, ssh, VS Code Remote)
  work seamlessly.
- **[Tailscale](https://tailscale.com/)** is the network fabric. VMs join a tailnet during
  provisioning and all subsequent SSH access rides it, so SSH ports are never exposed to the public
  internet. The `tailscale-auth-key` secret resolves through the backend chain on `vm create` (and
  re-joins on `vm start`); ephemeral keys (append `?ephemeral=true`) are fully supported, with the
  node removed from the tailnet when the VM goes offline. Generate auth keys at the
  [Tailscale admin console](https://login.tailscale.com/admin/settings/keys).
- **[tmux](https://github.com/tmux/tmux)** is the persistence layer. Every session maps 1:1 to a
  tmux session on the VM with the same lifecycle (agent sessions on per-agent sockets for
  isolation), and consoles layer over them for multitasking. See
  [tmux Architecture](cli/README.md#tmux-architecture) for the full picture.

A few other tools are integrated but not fundamental: **Git** (workspace templates around
repositories, plus scoped git credential management for GitHub, Azure DevOps, and more), **VS Code**
(auto-generated Remote - SSH workspaces), **[Mise en Place](https://mise.jdx.dev/)** (tool
installation with checksum validation), and
**[dotfiles](https://www.datacamp.com/tutorial/dotfiles)** (consistent shell/editor setup for the
admin user and agents).

## Components

Today the repo contains a single component: [`cli/`](cli/), the Python CLI that is the operator's
primary interface. The structure leaves room for additional clients (a web UI is anticipated) to
land alongside it without relocating the CLI.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The project follows
[Conventional Commits](https://www.conventionalcommits.org/), is opinionated about consistency
across the surface, and pairs well with AI coding assistants.

## Security

Found a vulnerability? Please report it privately. See [SECURITY.md](SECURITY.md) for scope and
reporting channels.

Licensed under [MIT](LICENSE).

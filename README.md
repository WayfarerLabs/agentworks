# Agentworks

[![CI](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml/badge.svg)](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![Python](https://img.shields.io/pypi/pyversions/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A comprehensive toolkit for managing agentic workloads: VMs, workspaces, agents, sessions,
harnesses, secrets/config, and the supporting systems that glue them together. Built around the
conviction that autonomy, security, and control are not mutually exclusive: a good platform makes it
possible and straightforward to have it all.

Create and manage an agentic fleet from your own workstation. **Durable agents** run as separate
Linux users in **VMs** on infrastructure you choose and control. They retain their own tools, git
credentials, and accumulated application state (a coding assistant's context and memory, interactive
logins). **Disposable sessions** spin up against them for a single piece of work and are thrown away
when done. One `agw` CLI drives all of it declaratively via an SSH-over-Tailscale control plane.

## Architecture at a Glance

The operator runs the `agw` CLI on their workstation. VMs are created at declared **vm-sites**
(configured places to create VMs), each backed by a **vm-platform** that knows how to work with a
given provider (Lima, WSL2, Proxmox, Azure VMs, and AWS EC2 today, with more to come). Regardless of
the platform, every VM runs the same base operating system (Debian Bookworm), is joined to the same
Tailscale tailnet, and is accessible over SSH at its Tailscale IP address using the operator's keys.

![Agentworks topology: the operator's workstation runs the agw CLI, which creates VMs at declared sites across local platforms (Lima or WSL2), a remote SSH VM site (e.g. Lima), Azure, AWS EC2, and Proxmox, with room reserved for future platforms. Every VM and the workstation itself join a shared Tailnet overlay, which is how the CLI reaches them all.](docs/images/agw-topology.png)

Beyond the VMs themselves, Agentworks provides several layered primitives for organizing agentic
workloads:

- Project files and repositories can be organized into **workspaces** (as filesystem subtrees).
- **Agents** each have their own Linux user, which provides a strong isolation boundary for
  capabilities and access.
- Agentic workloads (simple shell, Claude Code, Codex, etc.) can be run as **sessions** via tmux,
  which provides for both persistence and the ability to attach to and detach as needed.
- Each session invokes a **harness integration** that knows how to run a particular workload (e.g. a
  full agentic harness such as Claude Code, Codex, etc. or just a plain login shell). The harness
  integration owns start/restart semantics (e.g. resuming a Claude Code or Codex conversation right
  where it left off) as well as validating the target environment for its workload. Additionally,
  since each harness integration is built for a specific workload, it is the perfect place to grow
  further harness-specific functionality (user and workspace setup, authentication handling,
  specific configuration, deeper integrations, ...).
- Sessions can be organized into **named consoles**: curated tmux views that organize active
  sessions along with optional extra shell panes.
- Both **config** and **secrets** (together with **secret backends**) can be managed and securely
  injected at any level (VM, workspace, agent, session) to control access and behavior.

And all of this is managed via a **declarative, idempotent configuration system** that makes it easy
for operators to define, evolve, and scale their infrastructure over time.

Zooming in on a single VM, the diagram below shows how these primitives fit together inside one
machine: sessions invoke a harness integration to launch a harness or shell workload inside tmux,
with injected secrets/config. Sessions run as dedicated Linux users, work in workspaces, and can be
grouped into named consoles, all reachable over the tailnet.

![Agentworks VM internals: an Agentworks VM at a vm-site runs sessions, each invoking a harness integration that launches a harness or shell workload inside tmux, complete with injected secrets and config. Sessions run as dedicated Linux users (the admin user or an agent user) and work inside workspaces backed by git repos. Any number of sessions can be organized into named consoles, and a tailnet NIC connects the VM directly to the tailnet regardless of platform. The VM sits on a configured platform instance, alongside other VMs in the site and other vm-sites.](docs/images/agw-vm-internals.png)

## Getting Started

Install from PyPI:

```bash
uv tool install agentworks-cli
# or:  pipx install agentworks-cli
```

Or, if you prefer to run from source:

```bash
git clone https://github.com/WayfarerLabs/agentworks.git
uv tool install -e ./agentworks/cli
```

The everyday command is `agw` (the longer `agentworks` is installed too). Set up your config, then
stand up a VM and drive your first session:

```bash
agw config init        # writes ~/.config/agentworks/config.toml
agw config edit        # fill in required fields (at minimum, your operator SSH keys, plus any plugins you need)
agw doctor             # sanity-check dependencies, Tailscale, config, and the local DB
                       # note that you must have at least one active vm-site to create a VM
                       # follow hints and/or enable plugins to address any issues

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

A major question the industry is wrestling with is "what is the right compute primitive for agentic
work?" Agentworks sits solidly in the virtual machine camp. The reasoning is simple: you would not
seal a good developer inside a single locked-down container and expect their best work. A capable
agent is no different. Containers and other less-than-full-machine primitives might work for basic
development but, just like with human devs, the more you expect, the more friction this introduces:
no system services, no room to install a real development environment or spin up containers of their
own, no ability to collaborate with other users, etc.

Agentworks gives workloads a **shared, full-featured Linux VM**, complete with the whole tapestry
that a full machine entails: massive libraries of standard software, daemonized services, the
ability to run containers when needed, and genuine multi-user collaboration between agents.
Underlying platforms that support nested virtualization can run nested VMs too. On the security
side, this choice taps into decades of multi-user Linux development and experience. While the VM
itself provides a strong isolation boundary, further isolation between workloads is possible using
the battle-tested Linux primitives of users, groups, and permissioned filesystem subtrees, all
mapped to the concepts described below, thus allowing many workloads to securely share a single VM.
For additional reasoning on the VM choice, see
[ADR 0001](docs/adrs/0001-vm-based-infrastructure.md).

And to support the [consistency principle](docs/why-agentworks.md#consistency), Agentworks demands
that every VM uses the same base OS (Debian Bookworm), the same admin user setup, and the same
Tailscale tailnet join, so that the technical reality of the VM largely disappears and all VMs can
be handled the same way, both by the human operator and Agentworks itself. See
[ADR 0002](docs/adrs/0002-use-debian-as-the-vm-base-image.md) for more information.

Consistent with the general declarative approach, VMs are long-lived, backed by declarative
templates, and can be idempotently reinitialized to pick up changes whenever desired, thus allowing
operators to evolve their VMs over time without having to tear them down and start over.

### Workspaces - The Project

A workspace is the **project scope**: a root directory, optionally cloned from a git repository,
mapped to a Linux group whose permissions and ACLs give every member collaborative access.
Workspace-level configuration (e.g. Claude Code's project settings) shapes how harnesses behave
inside it. Any number of workspaces can map to the same repository; each is a full independent
clone.

### Agents - The Actor

An agent is a **security identity**: its own full Linux user, with the isolation and permissions
that entails (its own processes, private files, shell environment, and credentials). The boundary is
the standard Unix one, discretionary access control between users sharing a kernel: it keeps one
agent's mistakes and compromises away from another's state. It is deliberately not a sandbox that
restricts what the agent's own user may do, which is why the VM is the boundary that does the heavy
lifting when a stronger one is needed. Agents are mapped to workspaces (explicitly via grants or
implicitly via sessions), and that mapping drives the group and filesystem permissions that bound
what they can reach.

### Sessions and Harness Integrations - The Workloads

A **session** runs an agentic workload in a persistent tmux session as a target user (agent or
admin) in a workspace on a VM. Every session invokes a **harness integration**: Agentworks code that
knows how to launch and resume a particular **agentic harness** (e.g. Claude Code or Codex) or a
plain shell, and that checks whether the target environment can support the workload. A unique name,
persistent tmux session, and integration-specific resume semantics let the operator run any number
of concurrent workloads and attach, detach, stop, restart, create, and delete them at will. Tmux
always owns the pane and its tty; the harness integration only decides what runs inside it.

Session templates make workload configuration reusable and predictable. Because the harness
integration is a distinct extension layer, it is the natural place for optimized, harness-specific
logic and for adding support for new harnesses over time.

### Named Consoles - Organizing Active Work

Once more than a handful of sessions are running, a **named console** is a curated tmux view that
groups the sessions (on a VM) you are actively working across, optionally with extra shell panes
pre-opened. Each console is its own persistent tmux session, built once and attached to and detached
from independently of the underlying sessions' lifecycles. Consoles reference sessions without
owning them: a session can appear in any number of consoles (or none), and adding or removing it
never affects the session itself, so you can slice the same pool of running work into whatever
task-focused views make sense (one per feature, incident, or review). See
[Named Consoles](cli/README.md#named-consoles) in the CLI reference for the command surface.

### Agentworks Is Not a Harness

One point is absolutely critical to understanding the Agentworks model: **Agentworks is not a
harness**. There are many incredible options for running agentic workloads, from first-party
harnesses (Anthropic's Claude Code, OpenAI's Codex, etc.) to independent alternatives (OpenCode,
Aider, etc.). Agentworks does not try to be any of those. Rather, it strives to be the platform that
makes it easy to run them, and to run them securely, consistently, and at scale. A harness
integration is the Agentworks layer that makes a particular harness easy to run. That's as far as we
want to go.

Harnesses are getting better and better every day. Our belief is that, before long, custom harnesses
simply won't be able to compete with vanilla harnesses running the latest models. Context will
always matter, but the harness minutiae will matter less and less (and even get in the way) as the
models get better and better at autonomous operation.

In that world, though, standing up and managing least-privilege environments for those agents and
harnesses will become increasingly important. Agentworks is designed to solve that problem, and to
do so in a way that is consistent, secure, and scalable.

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
- **Support for differing levels of ephemerality.** Different operators have different needs for how
  long-lived their workloads and related resources are. Robust, declarative templates facilitate
  rapid setup and scale, while idempotent reinitialization and reuse of resources across workloads
  allow durable resources such as agents to accumulate state and context.
- **Declarative and idempotent.** Every layer is templated and declared, and the long-lived
  resources (VMs and agents) can be reinitialized to pick up changes, so environments stay
  consistent and evolve predictably rather than drifting.

The full reasoning, including the threat model Agentworks is designed against and how it bounds
blast radius (and what it deliberately does not do), is in [Why Agentworks](docs/why-agentworks.md).

## Tightly Integrated Software

In the spirit of opinionated consistency, Agentworks standardizes on a small set of excellent
software rather than abstracting over interchangeable alternatives. Users are encouraged to embrace
these choices rather than work around them.

- **SSH** is the control plane after provisioning: initialization, agent and session management,
  file transfer, and command execution. Provisioning uses the platform's native transport (Lima
  shell, SSH over a scoped Azure or EC2 public route, WSL2 exec, or Proxmox guest agent). The
  operator's key (configured in `[operator]`) is deployed during provisioning and is the sole SSH
  authentication mechanism thereafter. Once Tailscale is joined, routine access goes over the
  tailnet, and `~/.ssh/config` entries are managed automatically so standard clients (scp, ssh, VS
  Code Remote) work seamlessly.
- **[Tailscale](https://tailscale.com/)** is the network fabric. VMs join a tailnet during
  provisioning and routine SSH access rides it. Azure and EC2 temporarily open TCP/22 on their
  public interfaces to the operator's detected public IPv4 address (as a `/32`), plus configured
  allow-list CIDRs, during bootstrap or explicit native-platform access, then close it again. The
  `tailscale-auth-key` secret resolves through the backend chain on `vm create` (and re-joins on
  `vm start`); ephemeral keys (append `?ephemeral=true`) are fully supported, with the node removed
  from the tailnet when the VM goes offline. Generate auth keys at the
  [Tailscale admin console](https://login.tailscale.com/admin/settings/keys).
- **[tmux](https://github.com/tmux/tmux)** is the persistence layer. Every session maps 1:1 to a
  tmux session on the VM with the same lifecycle (agent sessions on per-agent sockets for
  isolation), and consoles layer over them for multitasking. See
  [tmux Architecture](cli/README.md#tmux-architecture) for the full picture.

A few other integrations are useful but not fundamental: **Git** (workspace templates around
repositories, plus scoped git credential management for GitHub, Azure DevOps, and more), **VS Code**
(auto-generated Remote - SSH workspaces), **[Mise en Place](https://mise.jdx.dev/)** (software
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

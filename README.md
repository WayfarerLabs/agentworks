# Agentworks

[![CI](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml/badge.svg)](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![Python](https://img.shields.io/pypi/pyversions/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A toolkit for managing agentic workloads: VMs, workspaces, agents, sessions, harnesses,
configuration, secrets, and the systems that connect them.

Create and manage an agentic fleet from your own workstation. **Durable agents** run as separate
Linux users in **VMs** on infrastructure you choose and control. They retain their own tools, git
credentials, and accumulated application state (a coding assistant's context and memory, interactive
logins). **Disposable sessions** spin up against them for a single piece of work and are thrown away
when done. One `agw` CLI drives all of it declaratively via an **SSH-over-Tailscale control plane**.

## Architecture at a Glance

The operator runs the `agw` CLI on their workstation. VMs are created at declared **vm-sites**
(configured places to create VMs), each backed by a **vm-platform** that knows how to work with a
given provider (Lima, WSL2, Proxmox, Azure VMs, AWS EC2, and Google Compute Engine today).
Regardless of the platform, every VM runs the same base operating system (Debian Bookworm), is
joined to the same Tailscale tailnet, and is accessible over SSH at its Tailscale IP address using
the operator's keys.

![Agentworks topology: the operator's workstation runs the agw CLI, which creates VMs at declared sites across local platforms (Lima or WSL2), a remote SSH VM site (e.g. Lima), Azure, AWS EC2, and Proxmox, with a placeholder for future VM platforms. Every VM and the workstation itself join a shared Tailnet overlay, which is how the CLI reaches them all.](docs/images/agw-topology.png)

Beyond the VMs themselves, Agentworks provides several layered primitives for organizing agentic
workloads:

- Project files and repositories can be organized into **workspaces** (as filesystem subtrees).
- **Agents** each have their own Linux user, which provides a strong isolation boundary for
  capabilities and access.
- Agentic workloads (simple shell, Claude Code, Codex, etc.) can be run as **sessions** via tmux,
  which provides for both persistence and the ability to attach to and detach as needed.
- Each session invokes a **harness integration** that knows how to run a particular workload (e.g. a
  full agentic harness such as Claude Code, Codex, etc. or just a plain login shell). The harness
  integration owns start/resume semantics (e.g. resuming a Claude Code or Codex conversation right
  where it left off) as well as validating the target environment for its workload. It also owns
  workload-specific configuration and integration behavior.
- Sessions can be organized into **named consoles**: curated tmux views that organize active
  sessions along with optional extra shell panes.
- Both **config** and **secrets** (together with configured **secret sources**) can be managed and
  securely injected at any level (VM, workspace, agent, session) to control access and behavior.

All of this is managed through a **declarative, idempotent configuration system** for defining and
evolving infrastructure over time.

Zooming in on a single VM, the diagram below shows how these primitives fit together inside one
machine: sessions invoke a harness integration to launch a harness or shell workload inside tmux,
with injected secrets/config. Sessions run as dedicated Linux users, work in workspaces, and can be
grouped into named consoles, all reachable over the tailnet.

![Agentworks VM internals: an Agentworks VM at a vm-site runs sessions, each invoking a harness integration that launches a harness or shell workload inside tmux, complete with injected secrets and config. Sessions run as dedicated Linux users (the admin user or an agent user) and work inside workspaces backed by git repos. Any number of sessions can be organized into named consoles, and a tailnet NIC connects the VM directly to the tailnet regardless of platform. The VM sits on a configured platform instance, alongside other VMs in the site and other vm-sites.](docs/images/agw-vm-internals.png)

## Getting Started

<!-- BEGIN GENERATED AGENTWORKS ASSISTANCE -->

```markdown
# Agentworks CLI bootstrap

You are my external Agentworks assistant agent, not an Agentworks-managed agent resource. Use this
prompt only to make a compatible `agentworks-cli` available and hand off to its built-in agent
guide. Agentworks requires Python 3.12 or newer and provides the `agw` command.

## Install and hand off

1. Run `agw version`.
2. If it reports a valid version at least 0.14.0 and I did not request an update, retain it and skip
   installation. Otherwise, use an exact compatible stable version at least 0.14.0 that I requested,
   or read `https://pypi.org/pypi/agentworks-cli/json` and select the latest compatible
   non-prerelease.
3. If no exact compatible stable version at least 0.14.0 is available, explain that no compatible
   stable release is available. Make no installation or update attempt, do not run
   `agw guide --agent`, and ask me to retry after the release is published. Do not use a
   pre-release, a lower version, or an unpinned latest version.
4. When installation or update is needed, run `uv tool install --upgrade 'agentworks-cli==VERSION'`.
   If installation is unavailable or fails, stop before the guide and leave that exact pinned
   command.
5. After installation or update, run `agw version` again and require the selected exact version. For
   a retained installation, require version 0.14.0 or newer.
6. Run `agw guide --agent` and obey the returned guide context for all further Agentworks help.
```

<!-- END GENERATED AGENTWORKS ASSISTANCE -->

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
workstation. In Agentworks terminology, "user" refers to a Linux user on a VM: either the admin user
or an agent identity.

### VMs - The Compute Environment

Agentworks uses shared, full-featured Linux VMs as its compute primitive. A VM provides standard
software, system services, package installation, containers, and multi-user collaboration between
agents. Platforms that support nested virtualization can run nested VMs too. The VM is the strong
isolation boundary; Linux users, groups, and permissioned filesystem subtrees provide further
separation between workloads inside it. See [ADR 0001](docs/adrs/0001-vm-based-infrastructure.md)
for the decision record.

Every managed VM uses Debian Bookworm, the same admin-user setup, and the same Tailscale network
model. See [ADR 0002](docs/adrs/0002-use-debian-as-the-vm-base-image.md) for the base-image
decision. VMs are long-lived, backed by declarative templates, and can be idempotently reinitialized
to pick up changes without being replaced.

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

Agentworks does not constrain outbound network access, so an agent exposed to untrusted content can
reach the network with anything its Linux user can read. Network containment is tracked in
[#224](https://github.com/WayfarerLabs/agentworks/issues/224).

An agent may be durable and reused across sessions, or created with `--new-agent` for one session.
Interactive session deletion offers to remove an unused agent. `--yes` removes one automatically
only when that session created it and no remaining session or workspace grant needs it. Reproducible
agent setup belongs in its template and can be converged with `agw agent reinit`; harness state,
application memory, and interactive logins persist in the agent's home.

### Sessions and Harness Integrations - The Workloads

A **session** runs an agentic workload in a persistent tmux session as a target user (agent or
admin) in a workspace on a VM. Every session invokes a **harness integration**: Agentworks code that
knows how to launch and resume a particular **agentic harness** (e.g. Claude Code or Codex) or a
plain shell, and that checks whether the target environment can support the workload. A unique name,
persistent tmux session, and integration-specific resume semantics let the operator run any number
of concurrent workloads and attach, detach, stop, resume, create, and delete them at will. Tmux
always owns the pane and its tty; the harness integration only decides what runs inside it.

Session templates make workload configuration reusable. Harness integrations keep workload-specific
logic outside the core session lifecycle.

### Named Consoles - Organizing Active Work

Once more than a handful of sessions are running, a **named console** is a curated tmux view that
groups the sessions (on a VM) you are actively working across, optionally with extra shell panes
pre-opened. Each console is its own persistent tmux session, built once and attached to and detached
from independently of the underlying sessions' lifecycles. Consoles reference sessions without
owning them: a session can appear in any number of consoles (or none), and adding or removing it
never affects the session itself, so you can slice the same pool of running work into whatever
task-focused views make sense (one per feature, incident, or review). See
[Named Consoles](cli/command-reference.md#named-consoles) in the CLI reference for the command
surface.

### Agentworks Is Not a Harness

Agentworks is not an agentic harness. It provides infrastructure for running harnesses such as
Claude Code, Codex, OpenCode, Aider, and plain shells. A harness integration supplies the
workload-specific launch, validation, and resume behavior; Agentworks owns the environment and
session around it.

## Manifesto

The project's values, assumptions about agentic engineering, and reasoning behind these design
choices are collected in the [Manifesto](docs/manifesto.md).

## Tightly Integrated Software

Agentworks standardizes on a small set of integrated tools rather than abstracting over
interchangeable alternatives.

- **SSH** is the control plane after provisioning: initialization, agent and session management,
  file transfer, and command execution. Provisioning uses the platform's native transport (Lima
  shell, SSH over a scoped Azure, EC2, or GCE public route, WSL2 exec, or Proxmox guest agent). The
  operator's key (configured in `[operator]`) is deployed during provisioning and is the sole SSH
  authentication mechanism thereafter. Once Tailscale is joined, routine access goes over the
  tailnet, and `~/.ssh/config` entries are managed automatically so standard clients (scp, ssh, VS
  Code Remote) work seamlessly.
- **[Tailscale](https://tailscale.com/)** is the network fabric. VMs join a tailnet during
  provisioning and routine SSH access rides it. Azure, EC2, and GCE temporarily open TCP/22 on their
  public interfaces to the operator's detected public IPv4 address (as a `/32`), plus configured
  allow-list CIDRs, during bootstrap or explicit native-platform access, then close it again. The
  `tailscale-auth-key` secret resolves through the configured source chain on `vm create` (and
  re-joins on `vm start`); ephemeral keys (append `?ephemeral=true`) are fully supported, with the
  node removed from the tailnet when the VM goes offline. Generate auth keys at the
  [Tailscale admin console](https://login.tailscale.com/admin/settings/keys).
- **[tmux](https://github.com/tmux/tmux)** is the persistence layer. Every session maps 1:1 to a
  tmux session on the VM with the same lifecycle (agent sessions on per-agent sockets for
  isolation), and consoles layer over them for multitasking. See
  [tmux Architecture](cli/command-reference.md#tmux-architecture) for the full picture.

A few other integrations are useful but not fundamental: **Git** (workspace templates around
repositories, plus scoped git credential management for GitHub, Azure DevOps, and more), **VS Code**
(auto-generated Remote - SSH workspaces), **[Mise en Place](https://mise.jdx.dev/)** (software
installation with checksum validation), and
**[dotfiles](https://www.datacamp.com/tutorial/dotfiles)** (consistent shell/editor setup for the
admin user and agents).

## Components

The repository contains two operator-facing components:

- [`cli/`](cli/) is the Python CLI and the operator's primary interface.
- [`website/`](website/) is the package-free static source and builder for `agentworks.build`.

They share permanent product and security documentation without maintaining independent copies of
the same claims.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The project follows
[Conventional Commits](https://www.conventionalcommits.org/), applies consistent conventions across
the repository, and includes shared configuration for AI coding assistants.

## Security

Found a vulnerability? Please report it privately. See [SECURITY.md](SECURITY.md) for scope and the
reporting channel.

Licensed under [MIT](LICENSE).

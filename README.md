# Agentworks

[![CI](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml/badge.svg)](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![Python](https://img.shields.io/pypi/pyversions/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A swiss army knife for managing agentic workloads: VMs, workspaces, agents, sessions, and the tools
that glue them together. Built around the conviction that autonomy and control are not mutually
exclusive: a good platform makes it possible and straightforward to have both.

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
[ADR 0001](docs/adrs/0001-vm-based-infrastructure.md), Agentworks uses VMs as the fundamental unit
of compute to provide for strong isolation while providing all the capabilities of a full Linux
environment (full daemonized services, multi-user, ability to run containers, etc.).

VMs further use a single operating system (Debian Bookworm, see
[ADR 0002](docs/adrs/0002-use-debian-as-the-vm-base-image.md)) to ensure consistency and minimize VM
management complexity and risk.

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

For day-to-day work across many sessions, see [Named consoles](cli/README.md#named-consoles):
curated tmux views that group the sessions you're actively focused on, optionally with extra shell
panes pre-opened in each session's window.

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
[mostly idempotent](docs/guides/idempotency.md) "reinitialization" so that they can be reliably
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
propagation. See [tmux Architecture](cli/README.md#tmux-architecture) for details.

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

Install from PyPI:

```bash
uv tool install agentworks-cli
# or:  pipx install agentworks-cli
```

The everyday command is `agw`. The longer form `agentworks` is also installed if you ever want to
type it out (or if `agw` would be ambiguous in some context); examples throughout the docs use
`agw`.

Then:

```bash
agw config init                          # creates ~/.config/agentworks/config.toml
# edit the config; at minimum set your SSH key paths
agw vm create my-vm                      # provision + initialize a VM
agw workspace create my-workspace        # create a workspace on the VM
agw workspace shell my-workspace
```

The full command reference, configuration schema, and tmux architecture live in
[cli/README.md](cli/README.md).

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

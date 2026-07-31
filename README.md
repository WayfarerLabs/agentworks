# Agentworks

[![CI](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml/badge.svg)](https://github.com/WayfarerLabs/agentworks/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![Python](https://img.shields.io/pypi/pyversions/agentworks-cli.svg)](https://pypi.org/project/agentworks-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A Swiss Army knife for managing agentic workloads: VMs, workspaces, agents, sessions, harnesses,
secrets/config, and the tools that glue them together. Built around the conviction that autonomy,
security, and control are not mutually exclusive: a good platform makes it possible and
straightforward to have it all.

## Architecture at a Glance

The operator runs the `agw` CLI on their workstation. VMs are created at declared **vm-sites**
(configured places to create VMs), each backed by a **vm-platform** that knows how to work with a
given provider (Lima, WSL2, Proxmox, and Azure VMs today; the platform layer is built for more, e.g.
Amazon EC2). Regardless of the platform, every VM runs the same base operating system (Debian
Bookworm), is joined to the same Tailscale tailnet, and is accessible over SSH at its Tailscale IP
address using the operator's keys.

![Agentworks topology: the operator's workstation runs the agw CLI, which creates VMs at declared sites across local platforms (Lima or WSL2), a remote SSH VM site (e.g. Lima), Azure, and Proxmox, with room reserved for future platforms. Every VM and the workstation itself join a shared Tailnet overlay, which is how the CLI reaches them all.](docs/images/agw-topology.png)

Beyond the VMs themselves, Agentworks provides several layered primitives for organizing agentic
workloads:

- Project files and repositories can be organized into **workspaces** (as filesystem subtrees).
- **Agents** each have their own Linux user, which provides a strong isolation boundary for
  capabilities and access.
- Agentic workloads (Claude Code, etc.) can be run as persistent **sessions** including an
  associated tmux session, which can be attached to and detached from as needed.
- Each session launches a **harness** that knows how to run a particular tool (e.g. a Claude Code
  instance, or just a plain login shell). The harness owns start/restart semantics (e.g. resuming a
  Claude Code conversation right where it left off) as well as validating the target environment for
  its tooling. Additionally, since each harness is tightly coupled to its target tooling, it is the
  perfect place to grow further tool-specific functionality (authentication handling, deeper
  integrations, ...).
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

## The Problem Space

Agentworks is an attempt to address several growing problems around agentic engineering with a
single, coherent framework.

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

Being precise about what those guardrails do is as important as having them. Agentworks builds its
isolation from VM boundaries plus standard Linux users, groups, and filesystem permissions. That
separates agents' credentials and state from one another and bounds what a mistaken or compromised
agent can reach. Two things it deliberately does not do: it is not a kernel-level sandbox (agents on
one VM share a kernel, so a local privilege escalation is a path between them), and it does not yet
constrain outbound network access, so an agent that reads untrusted content can still reach the
network with whatever it can read (tracked in
[#224](https://github.com/WayfarerLabs/agentworks/issues/224)).

### Workload Management

Anyone who has had more than one or two parallel agentic sessions has likely run into the problem of
keeping track of which agents are doing what, which sessions are active, how to coordinate work
across multiple agents (possibly working in the same repository or worktree), how to keep them all
running reliably (e.g. even when you close your laptop or lose your network connection), etc.

These are real challenges that impose real limits on how many agentic workloads a single operator
can reasonably manage at once. Most devs who have leaned into this space have developed some amount
of custom tooling to help with this problem. Solving for this at the platform layer lets devs and
their agents focus on shipping code instead of fiddling with infrastructure.

### Consistency

Similar to workload management, inconsistency across workload environments (different tools,
configuration, files, etc.) creates significant friction and potential for errors when trying to
scale up agentic engineering.

While sometimes these differences are intentional and should be preserved (e.g. wanting Agent A to
have different tools and permissions than Agent B), they often are accidental and introduce
unnecessary complexity, friction, and risk.

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

Agentworks organizes work into six core concepts.

### The Operator - The Person in Control

Agentworks is currently designed around a single human **operator** who is in control of all agentic
workloads. The operator is responsible for creating VMs, workspaces, agents, and sessions, and for
orchestrating how these components interact. This is all done via a comprehensive CLI that runs on
the operator's workstation. For more information, see the [CLI reference](cli/README.md#commands).

Note that while you might find some exceptions, we generally reserve the term "user" for the
technical Linux users that exist on the VMs (the admin user and the agentic identities).

### VMs - The Compute Environment

VMs define the base **compute environment** for all workloads. As discussed in
[ADR 0001](docs/adrs/0001-vm-based-infrastructure.md), Agentworks uses VMs as the fundamental unit
of compute to provide for strong isolation while providing all the capabilities of a full Linux
environment (full daemonized services, multi-user collaboration, ability to run containers, etc.).

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

### Workspaces - The Project

A workspace defines the **project scope**. Workspaces ultimately consist of a root directory that
can be based on a git repository or an empty directory. The workspace also maps to a Linux group
with workspace permissions and ACLs set to allow collaborative access to the files within the
workspace for all members of the group. Workspace-level configuration (e.g. Claude Code's project
settings) can be used to control how tools behave within the context of this workspace.

The Agentworks workspace mechanism fully supports any number of workspaces mapping to the same
underlying repository. To simplify administration, each is a full independent clone.

### Agents - The Actor

An agent defines a **security identity** on a VM. Each agent maps to its own full Linux user, with
all of the isolation and permissions that entails. Each agent is capable of having its own
processes, private files, shell environment, etc. This allows for the creation of different
identities with different privileges and capabilities. Agents only have the access granted to their
user by the operator.

The boundary this creates is the standard Unix one: discretionary access control between users
sharing a kernel. It gives each agent its own credentials, home directory, and processes, and keeps
one agent's mistakes and compromises away from another's state. It is intentionally not a sandbox
that restricts what the agent's own user may do; within its granted access, an agent has the full
run of a Linux system. That is the point of the design, and it is why the VM is the boundary that
does the heavy lifting when a stronger one is needed.

Agents are mapped to workspaces, either explicitly via grants or implicitly via sessions (see
below). This mapping drives standard group and filesystem permissions that control what agents are
able to access.

Note that actors were a major driving factor in the choice to use VMs as the fundamental compute
unit. Containers and local workspaces were considered but ultimately rejected because they don't
provide the necessary isolation for multiple actors to safely coexist. With VMs, actors enjoy all
the capability provided by a full Linux environment, including the ability to collaborate with other
actors, all while leveraging the security and isolation benefits of separate Linux users, which is a
tried-and-true model that has been proven at massive scale for decades.

### Sessions and Harnesses - The Workloads

A **session** is a specification to run a specific **harness** as an agent user (or the admin user)
in a workspace on a VM. The session is the outer wrapper (the tmux session, config/secret
specifications, etc.) while the harness is the piece that knows how to run a particular tool (e.g. a
Claude Code instance, or just a plain login shell): it owns starting and restarting the workload and
checking that the tool's required executables are present on the launch target. A session template
selects a harness (e.g. `harness: claude-code`) for a default experience and can further customize
the behavior with a `harness_config` block. For even greater flexibility (e.g. the ability to run a
tool that doesn't yet have a dedicated harness), the default `shell` harness simply runs a login
shell, optionally executing a command or just leaving it in interactive mode; a template that names
no harness runs this built-in `shell` harness.

Because harnesses are a distinct extension layer separate from the core, they can be built to
integrate tightly with their target tool (e.g. Claude Code), maximizing the functionality and value
of running that tool in Agentworks.

A unique name and a persistent tmux session allow the operator to have any number of concurrent
workloads running across their VMs, workspaces, and agents. Agentworks allows the operator to attach
to and detach from them as needed to monitor progress or interact with the workload, and then to
stop, restart, and delete them to manage their lifecycle. Whatever the harness, tmux always owns the
pane and its tty; the harness only decides what runs inside it.

### Named Consoles - Organizing Active Work

Once more than a handful of sessions are running, the operator needs a way to focus on just the ones
that matter right now. A **named console** is a curated tmux view that groups the sessions (on a VM)
the operator is actively working across, optionally with extra shell panes pre-opened in each
session's window. Each console is its own persistent tmux session (one window per included session)
that is built once and attached to and detached from at will, independent of the underlying
sessions' own lifecycles.

Consoles are purely an organizing layer: they reference sessions without owning them. A session can
appear in any number of consoles (or none), and adding or removing it from a console never affects
the session itself. This lets the operator slice the same pool of running work into whatever
task-focused views make sense at a given moment (e.g. one console per feature, incident, or review)
without disturbing anything that's running. See [Named Consoles](cli/README.md#named-consoles) in
the CLI reference for the command surface and semantics.

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

Composition runs the other way too. Because agents are Linux users and workspaces are Linux groups,
granting _partial_ access costs no more than withholding it, which makes graduated privilege between
cooperating agents a practical everyday pattern rather than a special case. A research agent can be
created with workspace access and nothing else, gather material, and leave artifacts behind for a
more privileged agent to act on, so the privileged agent never crawls untrusted content itself.
Models built on container-per-agent isolation can express the separation, but pay for the sharing in
volumes, networking, or an orchestrator; here both halves are ordinary filesystem permissions.

A handoff like that narrows exposure rather than eliminating it. Whatever the low-privilege agent
writes is still attacker-influenced input to whoever reads it next, so those artifacts are best
treated as data to be evaluated, not as instructions to be followed.

### Ephemerality

The layers differ in intended lifespan. VMs are intended to be long-lived: provisioned once and used
across many projects. Workspaces are intended to be medium-lived: created to support a particular
workstream or project and destroyed when done. Agents can be long-lived or short-lived depending on
the operator's preferences. Long-lived agents can be reused across multiple workspaces and sessions
or they can be created for a single workspace or session and destroyed when no longer needed.
Sessions are intended to be the most ephemeral: started for a specific activity and discarded when
done.

This gives agents two modes. A **disposable** agent is created alongside a session (`--new-agent`)
and torn down with it, which suits one-off work that needs no standing identity. A **durable** agent
is set up once and reused across many sessions and workspaces. Its reproducible setup (installed
tools, dotfiles, git credentials) belongs in the agent template, so it is declared once and rebuilt
on demand rather than hand-maintained. What makes a durable agent worth keeping is the state a
template _cannot_ reproduce: the harness and app-specific state that accumulates in the agent's
home, such as a coding assistant's conversation context and memory, and interactive logins
(OAuth/MFA token caches) that no script can regenerate. That accumulated state is the expensive part
you cannot fully automate, so a long-lived agent lets you build it up once and run a fleet of
disposable sessions against it. The agent carries the durable identity and its accumulated state;
the session is just the unit of work.

### Declarative Configuration and Templates

Each layer has a templating mechanism using declarative configuration so that patterns can be
defined once and stamped many times. The longer-lived resources (VMs and agents) provide for
[mostly idempotent](docs/guides/idempotency.md) "reinitialization" so that they can be reliably
evolved over time.

Environment variables and secrets are first-class in the configuration: env tables can be declared
at vm, workspace, admin, agent, or session scope and merge in a defined precedence order. Secret
references (`{ secret = "name" }`) resolve through a configurable backend chain (`env-var` reads
from an `AW_SECRET_*` env var; `prompt` asks interactively at run time). Use `agw env show` to
inspect the merged result for any context. See
[cli/README.md](cli/README.md#environment-variables-and-secrets) for the shape, and
`agw config sample` for the full reference.

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
access (VM and agent shells, initialization) goes over Tailscale, providing secure connectivity
without exposing SSH ports to the public internet.

During `vm create` (and `vm start` when re-joining), the Tailscale auth key is resolved as the
`tailscale-auth-key` secret via the framework's backend chain (see
[Environment Variables and Secrets](cli/README.md#environment-variables-and-secrets)). The default
chain reads from `AW_SECRET_TAILSCALE_AUTH_KEY` first and falls through to an interactive prompt.
Generate keys at the [Tailscale admin console](https://login.tailscale.com/admin/settings/keys).

Ephemeral auth keys (with `?ephemeral=true` appended) are fully supported. The Tailscale node is
automatically removed from the tailnet when the VM goes offline. Agentworks handles re-joining
gracefully on `vm start` by re-resolving the same secret through the chain.

### tmux

[tmux](https://github.com/tmux/tmux) provides the persistence layer. Every Agentworks session maps
1:1 to a tmux session on the VM with the same lifecycle, and agent sessions run on per-agent sockets
for isolation. A console abstraction (`console`) layers over individual sessions to support
multitasking across workspaces. See [tmux Architecture](cli/README.md#tmux-architecture) for the
full picture (per-agent sockets, console comparisons, key behaviors).

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

See [cli/README.md](cli/README.md) for a guided first session, the full command reference,
configuration schema, and tmux architecture.

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

# Why Agentworks

Agentworks is opinionated, and the opinions come from a specific reading of where agentic
engineering is going and what it costs to do safely. This document is the long-form version of that
reasoning: the problems Agentworks sets out to solve, and the principles that shape how it solves
them. The [README](../README.md) has the short version; this is the argument behind it.

## The Problem Space

Agentworks is an attempt to address several growing problems around agentic engineering with a
single, coherent framework.

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

Anyone who has had more than a few parallel agentic sessions has likely run into the problem of
keeping track of which agents are doing what, which sessions are active, what tools and credentials
are available in each session, how to coordinate work across multiple agents (possibly working in
the same repository or worktree), how to keep them all running reliably (e.g. even when you close
your laptop or lose your network connection), etc.

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
and can be torn down when that session is deleted, which suits one-off work that needs no standing
identity. Interactive deletion offers to remove an unused session-created agent; `--yes`
automatically removes it when no remaining session or standing workspace grant still needs it. A
**durable** agent is set up once and reused across many sessions and workspaces. Its reproducible
setup (installed tools, dotfiles, git credentials) belongs in the agent template, so it is declared
once and converged on demand with `agw agent reinit` rather than hand-maintained. What makes a
durable agent worth keeping is the state a template _cannot_ reproduce: the harness and app-specific
state that accumulates in the agent's home, such as a coding assistant's conversation context and
memory, and interactive logins (OAuth/MFA token caches) that no script can regenerate. That
accumulated state is the expensive part you cannot fully automate, so a long-lived agent lets you
build it up once and run a fleet of disposable sessions against it. The agent carries the durable
identity and its accumulated state; the session is just the unit of work.

### Declarative Configuration and Templates

Each layer has a templating mechanism using declarative configuration so that patterns can be
defined once and stamped many times. The longer-lived resources (VMs and agents) provide for
[mostly idempotent](guides/idempotency.md) "reinitialization" so that they can be reliably evolved
over time.

Environment variables and secrets are first-class in the configuration: env tables can be declared
at vm, workspace, admin, agent, or session scope and merge in a defined precedence order. Secret
references (`{ secret = "name" }`) resolve through a configurable backend chain (`env-var` reads
from an `AW_SECRET_*` env var; `prompt` asks interactively at run time). Use `agw env show` to
inspect the merged result for any context. See
[cli/README.md](../cli/README.md#environment-variables-and-secrets) for the shape, and
`agw config sample` for the full reference.

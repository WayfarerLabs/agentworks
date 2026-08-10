# Manifesto

Agentworks is opinionated, and the opinions come from a specific reading of where agentic
engineering is going and what it costs to do safely. This is the argument behind the project. The
[README](../README.md) describes what Agentworks does; this document explains why it exists and the
convictions that shape it.

## The Problem

### Agentic Engineering Is Risky

Agentic engineering is inherently risky. These risks come from multiple directions, including:

- **Honest mistakes** - An agent can simply make a mistake that results in data loss, corruption, or
  unintended side effects. It's very easy to find stories of agents wiping out entire directories or
  otherwise causing havoc.
- **Prompt injection** - Agents exposed to untrusted content can potentially be manipulated into
  doing things outside their operator's intent or control.
- **Supply chain attacks** - Agents may download and run compromised software or dependencies,
  introducing malicious code at build time, runtime, or both.
- **Rogue agents** - The agent itself could behave maliciously because of a compromised model or
  provider, or because of emergent behavior.

Increasing AI capabilities will make these attacks more frequent and sophisticated. Supply chain
attacks in particular have become a near-constant backdrop. The registries that developers and their
agents depend on are under active, sustained attack.

All of these risks suggest the same response: strong guardrails that contain the blast radius when
things go sideways and leave the operator in control.

### Autonomy Needs Control

The operator should retain control over what agents are doing, how workloads are executed, and what
resources they can access even as those workloads become more autonomous. Without reliable knowledge
of what agents are doing, consistent environments, and contained blast radius, control is lost in
practice even if it is notionally retained.

A significant and growing part of the ecosystem treats loss of control as an inevitable cost of
agentic autonomy. Agentworks takes the opposite position.

### Workloads Need Structure

Anyone who has had more than a few parallel agentic sessions has likely run into the problem of
tracking which agents are doing what, which sessions are active, what tools and credentials are
available in each session, how agents coordinate across repositories or working trees, and how to
keep them running reliably when a workstation disconnects.

These challenges impose real limits on how many agentic workloads one operator can manage. Solving
them at the platform layer lets developers and their agents focus on shipping code instead of
rebuilding infrastructure around every harness.

Inconsistent workload environments create similar friction and risk. Some differences are
intentional and should be preserved. Many are accidental and make every operation harder to reason
about.

## Our Convictions

### Autonomy and Control Are Not a Tradeoff

Agentworks is built on the conviction that autonomy, security, and control are not mutually
exclusive. A good platform should make it possible and straightforward to have all three.

### Give Agents Real Environments

You would not seal a good developer inside a single locked-down container and expect their best
work. A capable agent is no different. Agentworks gives workloads a full-featured Linux VM with
standard software, system services, room to install a real development environment, the ability to
run containers, and genuine multi-user collaboration.

The VM provides the hard isolation boundary. Within it, ordinary Linux users, groups, and filesystem
permissions provide further separation and controlled collaboration between agents.

### Agentworks Is a Platform, Not a Harness

There are many strong options for running agentic workloads, including first-party harnesses and
independent alternatives. Agentworks does not try to replace them. It provides the infrastructure to
run them securely, consistently, and at scale.

Harnesses are getting better every day. Our belief is that custom harnesses will struggle to compete
with vanilla harnesses running the latest models. Context will always matter, but harness minutiae
will matter less, and may get in the way, as models become more capable.

In that world, standing up and managing least-privilege environments becomes more important.
Agentworks is designed to solve that problem.

### Consistency Beats Unbounded Choice

Broadly applicable systems can spiral into complexity by supporting too many ways to do the same
thing. Agentworks takes an opinionated stance: one base operating system, a small set of integrated
tools, declarative configuration, and common extension contracts.

The capability model follows the same conviction. Integrations should not accumulate as special
cases in the core. Shared extension points keep the core understandable and let operators select the
functionality they need without installing, configuring, or even seeing everything else.

### Isolation Should Be Composable

Operators should be able to compose the isolation mechanisms that match their security and
operational requirements. Agentworks is optimized around VMs, agents, and workspaces together, but
does not require every operator to use every layer.

Composition runs the other way too. Because agents are Linux users and workspaces are Linux groups,
granting partial access costs no more than withholding it. A low-privilege research agent can gather
material and leave artifacts for a more privileged agent to evaluate, so the privileged agent does
not crawl untrusted content itself.

That handoff narrows exposure rather than eliminating it. Anything the low-privilege agent writes is
still attacker-influenced input. Treat it as data to evaluate, not instructions to follow.

### Durable Identities, Disposable Work

Different resources deserve different lifetimes. Infrastructure and identities can be durable;
individual work sessions should be cheap to create and discard.

Reproducible setup belongs in declarative templates. The valuable state that cannot be reproduced,
including harness context, accumulated memory, and interactive authentication, belongs to a durable
agent identity. Sessions are the disposable unit of work that runs against it.

### Declare It, Then Converge It

Every layer should be templated and declared. Long-lived resources should be reinitialized
idempotently so they can evolve without being torn down and rebuilt. Infrastructure that can be
declared, inspected, and converged is infrastructure an operator can control.

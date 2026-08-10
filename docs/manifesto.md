# The Agentworks Manifesto

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

And when things go wrong here, they will do so faster than humans can respond. The operator may not
even be aware of the problem until it is too late.

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

### Identity and Workload Are Separate

Agentworks separates who performs work from the unit of work itself. Agents are identities. Sessions
are workloads that run in a workspace as either an agent or the VM's admin user. Creating an agent
does not start a workload, and creating a session does not require creating a new identity.

That separation lets identity and workload lifecycles vary independently. A durable agent can carry
tools, credentials, harness context, memory, and interactive authentication across many disposable
sessions. A session can also create a new agent alongside itself, supporting a one-off identity
lifecycle when the operator wants one.

Reproducible identity setup belongs in a template. The valuable state that cannot be reproduced
belongs to the identity, while the session remains the disposable unit of work.

### Set the Context, Tools, and Guardrails, Then Get Out of the Way

Early agentic systems often relied on lots of custom tooling around the harness itself: Ralph loops,
managed or brokered delegation, and even fully custom harnesses.

Starting around fall 2025 with Claude Opus 4.5, it became clear that the model could do much of this
work itself if given the proper direction. Models such as Claude Fable 5 and GPT-5.6 Sol need even
less custom orchestration. Simply tell the model how it should work. For example: "Delegate to
subagents where possible, consider less capable models for simpler tasks, and do not stop until you
have a merge-ready PR." As has become a theme in the agentic world, what once was necessary is now
counterproductive.

Today, built-in "auto" and goal-oriented modes for longer-running, more autonomous work have
proliferated across harnesses. These modes can be useful, but they are still orchestration.
Agentworks expects their specifics to matter less as models continue to improve.

But while custom orchestration is on its way out, setting the right context, giving the agent the
right tools, and, critically, establishing appropriate guardrails are more important than ever.
Agentworks is designed to make that easy while otherwise letting the harness and models operate
unimpeded.

### Consistency Beats Unbounded Choice

Broadly applicable systems can spiral into complexity by supporting too many ways to do the same
thing. Agentworks takes an opinionated stance: one base operating system, a small set of integrated
tools, declarative configuration, a shared command-plan orchestration model, and common extension
contracts.

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

### Declarative Within Reason

The DevOps movement demonstrated the power of declarative approaches. Agentworks follows that model
only where a declaration can be an honest contract.

For VMs and agents, `reinit` reruns the full Agentworks initialization contract while preserving
accumulated state outside that contract. For workspaces, `repair` is deliberately narrower. It
restores the invariants Agentworks can safely promise without treating the repository clone and the
work inside it as disposable implementation details. Sessions likewise accumulate harness history
and are managed through an imperative lifecycle.

Who created a piece of state does not decide whether it is safe to converge; the resource contract
does. Declarative machinery should rebuild what can be reproduced, but it must not erase accumulated
state merely because a declaration no longer names it. When reconciliation cannot be both complete
and safe, Agentworks names a narrower promise instead of pretending the resource is fully
declarative.

## And Remember

### Agentworks Is a Platform, Not a Harness

There are many strong options for running agentic workloads, including first-party harnesses and
independent alternatives. Agentworks does not try to replace them. It provides the infrastructure to
run them securely, consistently, and at scale.

Harnesses are getting better every day. Our belief is that custom harnesses will struggle to compete
with vanilla harnesses running the latest models. Context will always matter, but harness minutiae
will matter less, and may get in the way, as models become more capable.

In that world, standing up and managing least-privilege environments becomes more important.
Agentworks is designed to solve that problem.

### Security Is Everyone's Responsibility

Agentworks is built to support good security practices and to be reasonably secure by default, but
it cannot guarantee security for every operator in every environment. Given what it handles,
including agentic workloads with private code and data and sensitive credentials, it should be
considered a high-value target for attackers.

Your VMs and infrastructure remain your responsibility. Agentworks cannot secure an unsafe host, an
overly permissive configuration, or credentials exposed outside its boundaries.

Exercise appropriate caution, especially when using agents to configure and operate Agentworks
itself. The workstation from which you administer Agentworks necessarily holds powerful credentials,
so an attacker who compromises it can compromise everything it controls.

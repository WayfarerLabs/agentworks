# Security at Agentworks

Agentworks manages powerful autonomous workloads, credentials, files, and infrastructure. Its
security model is designed to preserve operator control and contain the blast radius when an agent,
tool, or dependency behaves unexpectedly. The boundaries are useful, but they are not absolute.

## Reporting a Vulnerability

If you believe you have found a security vulnerability in Agentworks, please report it privately
rather than opening a public issue.

Use GitHub's [private vulnerability reporting][gh-private] on this repository. Please include:

- A description of the issue and the impact you believe it has.
- Steps to reproduce (or a proof-of-concept, if applicable).
- The version, commit, or branch you observed it on.
- Any relevant configuration, sanitized of secrets.

You can expect an initial acknowledgement within a few days. We will work with you to understand the
issue, develop a fix, and coordinate disclosure.

## Threat Model

Agentic engineering carries risks from several directions:

- **Honest mistakes** - An agent can make a mistake that causes data loss, corruption, or unintended
  side effects.
- **Prompt injection** - Untrusted content can manipulate an agent into acting outside the
  operator's intent.
- **Supply chain attacks** - An agent can download and run compromised tools or dependencies at
  build time or runtime.
- **Rogue agents** - A model, provider, or agent could behave maliciously after compromise or
  through unexpected behavior.

These risks point toward the same response: use isolation and permissions so that a failure is
contained and the operator retains control.

## Boundaries and Current Limitations

Agentworks builds isolation from VM boundaries plus standard Linux users, groups, and filesystem
permissions. Agents run as separate Linux users. Workspaces use Linux groups to grant only the
shared filesystem access their members need. This separates credentials and state between agents and
limits what a mistaken or compromised agent can reach.

Those mechanisms do not make Agentworks a kernel-level sandbox. Agents on one VM share its kernel,
so a local privilege escalation can cross the user boundary. Use separate VMs when workloads must
not share that risk boundary.

Agentworks also does not yet restrict outbound network access. An agent that processes untrusted
content can still reach the network with any information it is able to read. Network restriction is
tracked in [issue #224](https://github.com/WayfarerLabs/agentworks/issues/224).

Agentworks currently loads only system plugins shipped in this repository. Those plugins are part of
the Agentworks trust boundary and are disabled until the operator opts in to them.

## Operator Posture

The operator workstation and each VM's admin account are trusted control points. A person or process
with access to either can act with their authority. If the workstation is compromised, an attacker
has multiple paths to reach the infrastructure and workloads it controls, along with the
credentials, code, and data they can access.

An agent that configures or operates Agentworks gains access to everything Agentworks can reach:
every managed resource and secret reference, plus anything accessible over SSH from the operator
workstation. Use the strictest practical harness approval and sandbox settings, narrowly scoped
credentials, and explicit consent boundaries. Review changes before applying them to a real
installation.

Operators can compose VMs, agents, workspaces, Linux permissions, and session lifetimes to match
their needs. The full model uses all of these layers, but each operator decides which layers a
workload requires. A strict posture grants only the access needed for the current job and keeps
privileged work separate from research or other untrusted-input work.

Partial access is an ordinary part of this model. For example, a low-privilege research agent can
write artifacts into a shared workspace for a more privileged agent to evaluate. That handoff
narrows exposure rather than eliminating it: attacker-influenced artifacts remain data to inspect,
not instructions to follow.

## Credentials and Secrets

Credentials belong at the narrowest practical scope. Agentworks can define environment variables at
VM, workspace, admin, agent, and session scopes with a deterministic precedence order. Secret
references resolve through configured backends at runtime instead of requiring secret values in
declarative configuration.

An agent can use every credential and secret available to its Linux user and current process. Keep
unrelated credentials out of that scope, prefer short-lived credentials where practical, and do not
assume that prompt instructions can substitute for operating-system permissions.

## Scope and Upstream Guidance

Reports of particular interest include:

- Privilege escalation allowing an agent to obtain the admin user's or another agent's permissions.
- Escapes from the documented agent or VM isolation model.
- Mishandling of operator credentials by Agentworks itself.
- Supply chain risks in how Agentworks fetches or installs external tooling.

Vulnerabilities in upstream dependencies should normally be reported to the respective upstream
project. We are happy to help coordinate when ownership is unclear or when Agentworks turns an
upstream flaw into a distinct vulnerability.

[gh-private]: https://github.com/WayfarerLabs/agentworks/security/advisories/new

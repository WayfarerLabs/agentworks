# Security at Agentworks

Security in agentic systems has two distinct layers: the security properties of Agentworks itself
and the security of the environment from which it is administered. They have different threats,
boundaries, and owners, so this document addresses them separately.

## Agentworks Security

Agentworks assumes that a managed workload, harness, tool, or dependency may make a mistake, become
compromised, or behave maliciously. Its security goal is not to establish that those components are
trustworthy. It is to preserve operator control and limit what they can reach when something goes
wrong. Prompt instructions are not a security boundary.

### Harness Controls and Defense in Depth

Harnesses and other applications can provide permission models, approval prompts, and their own
sandboxes. These controls are valuable: they prevent many accidental actions, make intent visible,
and can stop behavior that violates an operator's policy. Use them.

They are also the first line of defense, not the only one. A bug, compromise, misconfiguration, or
sandbox escape in the application layer can bypass the controls that layer provides. Fine-grained
approval can also carry substantial usability cost, especially when useful autonomy requires a human
to review every tool call.

Agentworks adds an enforcement layer below the harness. Linux identities, group membership,
filesystem permissions, credential scope, and VM boundaries constrain every process running as the
workload, whether it is a supported harness, another application, a plain shell, or code executed
from a compromised npm package. Those controls do not depend on the application recognizing or
cooperating with them. They complement security at the application layer rather than replace it, and
remain subject to lower-layer vulnerabilities such as kernel or hypervisor compromise.

### Isolation Model and Current Limitations

A VM is the strongest isolation boundary Agentworks provides. Within a VM, agents run as separate
Linux users, and workspaces use Linux groups and filesystem permissions to grant shared access. This
separates credentials and state between agents and limits what a mistaken or compromised workload
can reach.

The user boundary is not a kernel-level sandbox. Agents on one VM share its kernel, so a local
privilege escalation can cross between them. Use separate VMs when workloads must not share that
risk boundary.

Agentworks does not currently restrict outbound network access. A workload that processes untrusted
content can reach the network with any information its Linux user can read. Network restriction is
tracked in [issue #224](https://github.com/WayfarerLabs/agentworks/issues/224).

### Credentials and Secrets

Credentials belong at the narrowest practical scope. Agentworks can define environment variables at
VM, workspace, admin, agent, and session scopes with a deterministic precedence order. Secret
references resolve through configured backends at runtime instead of requiring secret values in
declarative configuration.

A workload can use every credential and secret available to its Linux user and current process. Keep
unrelated credentials out of that scope, prefer short-lived credentials where practical, and do not
assume that prompt instructions can substitute for operating-system permissions.

### Shipped Plugins

Current builds load only system plugins shipped in this repository. Their Python modules are
imported as part of Agentworks, so their code is inside the Agentworks trust boundary whether or not
an operator enables their contributions. Plugin opt-in controls the availability of contributed
capabilities and resources; it is not a code-loading security boundary. Agentworks does not
currently load externally distributed plugins.

## Environment Security

Agentworks cannot secure the surrounding systems on which its own guarantees depend. Operators are
responsible for the security of their workstation, infrastructure accounts, networks, hypervisors,
and other administration paths.

### Workstation and Infrastructure

The operator workstation and each VM's admin account are trusted control points. A person or process
with access to either can act with their authority. If the workstation is compromised, an attacker
has multiple paths to reach the infrastructure and workloads it controls, along with the
credentials, code, and data they can access.

Agentworks does not compensate for a compromised cloud account, VM host, tailnet, operating system,
or other underlying service. Apply the security controls and updates appropriate to each of those
systems.

### Agent-Assisted Administration

An agent used from the operator workstation to configure or operate Agentworks is outside the
managed VM and Linux-user boundaries it is administering. It can reach every Agentworks-managed
resource and secret reference available to the workstation account, plus anything that account can
access over SSH.

Use the strictest practical harness approval and sandbox settings, narrowly scoped credentials, and
explicit consent boundaries for such an agent. Where practical, require it to present plans and
changes for review before allowing it to modify a real installation.

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

### Scope and Upstream Guidance

Reports of particular interest include:

- Agentworks granting a workload access outside its documented user, workspace, or VM boundary.
- Privilege escalation through permissions or command paths that Agentworks creates or manages.
- Mishandling of operator credentials or resolved secrets by Agentworks itself.
- Unsafe behavior in how Agentworks fetches, verifies, or installs external tooling.
- Vulnerabilities in the system plugins shipped with Agentworks.

Vulnerabilities in upstream dependencies should normally be reported to the respective upstream
project. We are happy to help coordinate when ownership is unclear or when Agentworks turns an
upstream flaw into a distinct vulnerability.

[gh-private]: https://github.com/WayfarerLabs/agentworks/security/advisories/new

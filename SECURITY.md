# Security Policy

## Reporting a Vulnerability

If you believe you have found a security vulnerability in Agentworks, please report it privately
rather than opening a public issue.

Use GitHub's [private vulnerability reporting][gh-private] on this repository, or email the
maintainer directly. Please include:

- A description of the issue and the impact you believe it has.
- Steps to reproduce (or a proof-of-concept, if applicable).
- The version, commit, or branch you observed it on.
- Any relevant configuration (sanitized of secrets).

You can expect an initial acknowledgement within a few days. We will work with you to understand the
issue, develop a fix, and coordinate disclosure.

## Scope

Agentworks is a tool for managing agentic workloads on developer-controlled infrastructure. Reports
we are most interested in include:

- Privilege escalation between agents, workspaces, or the admin user.
- Escapes from the workspace or session isolation model.
- Mishandling of operator credentials (SSH keys, git credentials, Tailscale keys, etc.) by
  Agentworks itself.
- Supply-chain risks in how Agentworks fetches or installs external tooling.

Issues in upstream dependencies (Tailscale, tmux, mise, etc.) should be reported to the respective
upstream project. We are happy to help coordinate if it isn't clear where a report belongs.

[gh-private]:
  https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability

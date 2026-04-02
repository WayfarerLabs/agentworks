# 1. VM-based Infrastructure

Date: 2026-03-05

## Status

Accepted

## Context

At the start of the Agentworks project, there is a key decision: what do we use as the basis of our
infrastructure? The two obvious choices for isolated environments are virtual machines (VMs) and
containers. Both have their advantages and disadvantages, and the choice will have a significant
impact on the development and deployment of our system.

Our target users are operators running AI coding agents that need full development environments with
tools, runtimes, git access, and interactive shell sessions. These agents operate over extended
periods (hours to days), install and configure tooling, and need to persist state across sessions.

## Decision

We have decided to use VM-based infrastructure for the Agentworks project. This decision is based on
several factors:

1. **Security**: VMs provide stronger isolation than containers. Each VM has its own kernel,
   filesystem, and network stack. This is important when running untrusted or semi-trusted AI agents
   that may execute arbitrary code. Container escapes are a well-documented attack surface; VM
   escapes are significantly harder.

2. **Robustness**: VMs are real machines with all the capabilities of the full Linux environment.
   Tools, installers, development workflows, daemon processes, monitoring, ... all work as expected.
   Containers can be made to do some of this but it quickly devolves into hacks and workarounds.
   With VMs there are no filesystem overlay quirks, no pid namespace surprises, and no restrictions
   on syscalls. No matter what the user wishes to do in the Agentworks system, they will have all
   options available to do so.

3. **Simplicity**: A VM is a straightforward mental model for both operators and agents. It is a
   machine with a filesystem, users, and processes. There is no need to reason about layers,
   volumes, entrypoints, or container orchestration. SSH provides a universal access mechanism.

4. **Multi-user support**: VMs natively support multiple Linux users with standard permission models
   (users, groups, ACLs). Agentworks intends to utilize this for agent isolation and workspace
   access control. With VMs, each agent is a Linux user, workspace access is controlled via group
   membership, and file permissions enforce boundaries. Achieving this in containers would require
   highly non-standard configurations.

Containers may well be introduced at some point in the future as a lightweight option for certain
workloads, but the primary infrastructure will be VM-based to ensure a robust and secure foundation
for our users.

## Consequences

- The platform must support VM provisioning and lifecycle management across multiple providers
  (Lima, Azure, WSL2).
- VMs are heavier than containers: slower to create, more resource-intensive, and require disk
  provisioning. This is acceptable for long-lived environments but means we need good idempotent
  reinitialization (vm reinit) rather than disposable rebuild.
- The cost of running VMs in the cloud is generally higher than containers, so this could increase
  the cost for users without their own infrastructure to run on. That said, VMs should be able to
  host many agentic workloads simultaneously, so that may offset the cost difference for heavy
  users.
- Networking requires a solution for cross-platform SSH access. We chose Tailscale (ADR-0003) to
  provide a consistent overlay network.
- The VM model naturally supports the workspace and agent architecture described in the project
  README: VMs are the environment, workspaces are the project scope, and agents are Linux users
  within the VM.

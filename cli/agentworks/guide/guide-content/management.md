---
description: Configure and operate Agentworks resources and managed instances deliberately.
---

# Resource management

Kinds define the vocabulary, declared resources hold operator intent, capability implementations
provide behavior, and live instances record managed state. See `concept-core-model` for the domain
model and `concept-onboarding` for a first setup.

Use `agw resource kinds --output json` for the installed kind vocabulary. Use
`agw resource list --kind KIND --include-disabled --output json` for current registered members,
origins, enablement, and readiness. Use `agw graph show KIND/NAME --output json` for relationships.
The applicable VM, workspace, agent, session, console, and secret list or describe commands own
their operational facts.

Use `agw GROUP --help` for the current group surface and `agw GROUP COMMAND --help` for exact
syntax. Create and change declarable resources through their owning commands or canonical manifests,
then read command-owned JSON facts to confirm the result. Disabled and not-ready implementations are
facts, not instructions to enable or repair them.

Before a proposed operation reaches a new target or can mutate configuration, infrastructure, or
external systems, state the target and expected effect. If that work is outside the operator's
current instruction, ask first. If declined, leave state unchanged and provide the relevant
read-only inspection or command help instead.

After an upgrade, resolve emitted deprecation instructions before changing unrelated state. Use
`concept-migration` only for exceptional breaking-input conversion. For failures, start with
`concept-troubleshooting`.

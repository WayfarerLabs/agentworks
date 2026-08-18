---
description: Set up Agentworks for the first time or revisit an existing installation.
index-order: 20
---

# Agentworks onboarding

Onboarding is a repeatable path, not a one-time wizard. Start with
`agw guide show concept-core-model` and `agw guide show concept-prerequisites`, then work through
the sections below. On an existing installation, skip anything that is already ready.

<!-- agw:agent-only -->

Use CLI introspection to narrow choices as they become relevant.
`agw resource list --include-disabled` shows installed resources, including disabled or not-ready
entries, while `agw resource explain KIND/NAME` describes one capability's configuration. If the
operator has not already selected an option, summarize the meaningful choices and ask. If they have,
continue within that instruction rather than turning onboarding into a questionnaire.

<!-- /agw:agent-only -->

## Initialize configuration

`agw config init` creates the operator's configuration at the default path without overwriting an
existing file. Edit it directly or run `agw config edit` to use the configured `EDITOR` or `VISUAL`.
Commands that use configuration reload the current file for each invocation, so saved changes apply
to the next such command.

The configuration holds workstation settings such as SSH key paths, enabled system plugins, and the
secret-source order. It does not hold declared resources; those live in YAML manifests.

Choose an existing SSH key pair or create one specifically for Agentworks:

```bash
ssh-keygen -t ed25519 -f /path/to/your/ssh_key
```

<!-- agw:agent-only -->

This is a useful place to help the operator choose an existing key or generate a dedicated one. Keep
the sensitivity of SSH keys in mind: prefer checking paths and public-key material rather than
reading private-key contents, and take care not to overwrite an existing pair.

<!-- /agw:agent-only -->

## Choose a VM platform and site

A platform is the implementation that knows how to operate a VM backend. A site is a configured
place where that platform can create VMs. Inspect both, including unavailable choices:

```bash
agw resource list --kind vm-platform --include-disabled
agw resource list --kind vm-site --include-disabled
```

Use `agw resource explain vm-platform/NAME` for one platform's requirements and
`agw resource sample vm-site` when a new site manifest is needed. Some platforms ship in disabled
system plugins; if one is selected, enable it in the operator configuration and inspect the lists
again. See `agw guide show concept-virtual-machines` for the full model.

## Configure secrets and credentials

Agentworks refers to secrets by name and resolves them only when an operation needs their value.
Start with `agw guide show concept-secrets`, then use `agw secret describe NAME` to see where a
named secret would come from without resolving it.

By default, secret names map to workstation environment variables and fall back to an interactive
prompt. This is a useful starting point. Configure additional backends and sources when the
installation needs different storage or non-interactive resolution.

Git credentials are declared resources. Inspect existing choices with
`agw resource list --kind git-credential --include-disabled` and use
`agw resource explain git-credential` before authoring another one.

## Review templates and harness integrations

Templates capture repeatable choices for VMs, administrators, workspaces, agents, and sessions.
Harness integrations describe the coding assistants that sessions can run. Use `agw resource kinds`
for the complete vocabulary and `agw resource list --kind KIND --include-disabled` for any kind you
want to inspect. Shipped defaults are a useful starting point; add manifests when they do not fit
the operator's intended setup.

If Agentworks does not provide an integration for a chosen harness, use the built-in `shell`
integration in an agent-mode session and run the harness's CLI directly. `agw session create --help`
shows how to select or create the agent; use an admin session only when the operator intentionally
wants the workload to run as the VM administrator.

## Check the installation

Run `agw doctor` after configuration and resource choices are in place. It checks the workstation,
configuration, dependencies, and state database. Resolve a reported failure through its owning
command or `agw guide show concept-troubleshooting`; a disabled optional capability does not need to
be repaired.

Use `agw doctor --output json` when a machine-readable result is more useful.

## Start working

Create the first VM with `agw vm create --help` as the current syntax guide. The command can infer a
single ready site or prompt when several are available.

Then use `agw session create --help` to create a session. It can use existing resources or create a
new workspace and managed agent as part of the same request. Attach with `agw session attach NAME`.
Named consoles are optional curated views across multiple sessions; explore them later with
`agw console --help`.

For ongoing operation, continue with `agw guide show concept-management`.

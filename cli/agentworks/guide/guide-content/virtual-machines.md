---
description: Understand VM platforms and sites, inspect readiness, and work with managed VMs.
index-order: 17
---

# Virtual machines

Agentworks separates the code that operates a VM backend from the configured places where VMs can be
created.

## Platforms and sites

A `vm-platform` is an implementation for a backend such as a local hypervisor or cloud provider. A
`vm-site` selects one platform and supplies its placement and authentication settings. Commands
create VMs at sites; they do not configure platform implementations directly.

See what this installation provides:

```bash
agw resource list --kind vm-platform --include-disabled
agw resource list --kind vm-site --include-disabled
```

The output distinguishes ready choices from disabled or unavailable ones and explains why a choice
is not ready when that information is available. Platform definitions and host readiness are
different facts: use `agw resource explain vm-platform/NAME` for configuration and `agw doctor` for
workstation checks.

To add a site, begin with `agw resource sample vm-site`. Review or edit an existing operator-owned
manifest with `agw resource edit vm-site/NAME`.

## Managed VMs

`agw vm list` shows VMs Agentworks already manages. `agw vm describe NAME` owns the detailed site,
power, resource, and recorded-state view for one VM, plus its current VM/admin declarations, stored
instance specs, recorded hardware-request and SSH identity lifecycle evidence, and any comparison
drift. The hardware request is the configuration Agentworks used for successful creation, not a
provider observation of realized hardware. Human describe shows the complete current spec without
exhaustive per-leaf sources. Use `agw resource show vm-template/NAME` or
`agw resource show admin-template/NAME` to inspect fully resolved values and their provenance before
creation. Use `--output json` when another tool or agent needs structured facts and full
current-spec provenance.

`agw vm verify-connection NAME` performs an active SSH check without starting the VM. Creating,
starting, stopping, backing up, or rekeying a VM belongs to the corresponding `agw vm` command; use
`agw vm --help` for the current surface.

## Runtime intent and access

VM creation and explicit start select running intent. On-demand permits automatic activation without
requiring an idle VM to run; stopped intent blocks automatic power-on and running child workloads
while retaining their targets. The pure `agw vm set-desired-state NAME STATE` command records a
selected policy without runtime changes. Inspect its help for valid states.

After a VM start, recovery of its stored sessions and named consoles is a separate
`agw vm reconcile NAME` operation. Preview with `--dry-run` when reviewing the scope. Execute only
when that recovery is within the operator's request; otherwise report the preview or blocker. A
focused console reconciliation restores its view, not its member session runtimes.

On WSL2, an already-live guest with stopped intent receives no idle protection during access or
management work and may stop mid-command. This includes shells, workspace creation, and agent
reinitialization. Selecting `agw vm set-desired-state NAME on-demand` or `running` permits normal
holds and future activation; it is not only a per-command keepalive switch. Change the policy only
when those effects are intended, or retain stopped intent and report the access limitation. Even
live diagnostic access may report busy briefly while another operation holds the VM family.

## Host keys

Agentworks verifies each VM's SSH host key against a per-VM store and never trusts a first
connection. Commands that act on a VM refuse one with no record, while describe and listing commands
report the missing record per VM and carry on. `agw vm import-host-keys --help` explains how to fill
the store for a VM that predates it.

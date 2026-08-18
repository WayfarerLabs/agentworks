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
power, resource, and recorded-state view for one VM. Use `--output json` when another tool or agent
needs structured facts.

`agw vm verify-connection NAME` performs an active SSH check without starting the VM. Creating,
starting, stopping, backing up, or rekeying a VM belongs to the corresponding `agw vm` command; use
`agw vm --help` for the current surface.

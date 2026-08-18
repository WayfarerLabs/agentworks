---
description: Choose a VM platform and site, then inspect their current readiness and managed VMs.
index-order: 17
---

# Virtual machines

A `vm-platform` is a capability implementation: code that knows how to create and operate VMs on a
backend such as Lima, WSL2, or a supported provider. A `vm-site` is a configured place to create
VMs. Each site selects one platform and supplies that platform's placement and authentication
settings. Operators and commands select sites; they do not configure platform capability rows
directly.

Use the config-free reference surfaces to understand the available shapes:

- `agw resource explain vm-platform` lists the platform implementations shipped by this build.
- `agw resource explain vm-platform/NAME` documents one platform's settings and plugin ownership.
- `agw resource explain vm-site` documents the declarable site shape.
- `agw resource sample vm-site` prints an inert site manifest to review or edit.

Definitions are not readiness. Inspect the two live registry dimensions separately with
`agw resource list --kind vm-platform --include-disabled --output json` and
`agw resource list --kind vm-site --include-disabled --output json`. Those rows expose current
enablement and `not_ready_reason` facts; `agw doctor --output json` reports the corresponding host
checks and reasons. A disabled or not-ready row is information, not authorization to enable a
plugin, change credentials, or repair the workstation.

Managed VMs are a different state layer. `agw vm list --output json` lists them, and
`agw vm describe NAME --output json` owns one VM's site, power state, resources, and recorded
details. Use `agw vm verify-connection NAME` only when an active connectivity check is in scope; it
tests the canonical admin connection without starting the VM.

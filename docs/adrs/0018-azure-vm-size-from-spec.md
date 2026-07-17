# 18. Select Azure VM Size From the Standard Compute/Memory Spec

Date: 2026-07-16

## Status

Accepted. Removes the `azure_vm_size` template field and the `vm create` hardware/admin override
flags introduced in the earliest CLI. Implements issue #178.

## Context

`vm create` was one of the first commands and carried baggage the rest of the surface has since
shed. Two pieces stood out.

First, it accepted per-create override flags (`--cpus`, `--memory`, `--disk`, `--azure-vm-size`,
`--admin-username`) that duplicate values the vm-template and admin-template already own. Every
other resource in agentworks is created purely from a named template: if you want a different shape,
you declare a different template. The override flags let `vm create` drift from that model,
splitting the source of truth for a VM's hardware between the template and the command line.

Second, Azure sizing spoke a different dialect than every other platform. Lima, WSL2, and Proxmox
size a VM from generic `cpus` and `memory` (GiB); Azure alone took an opaque `azure_vm_size` SKU
string (`Standard_B2s`), baked into both the template schema and a hardcoded default in the
platform. An operator had to know Azure's SKU catalog to size an Azure VM, and a template written
for Azure could not be reused on any other platform.

## Decision

1. **Trim `vm create` to `name`, `--template`, and `--site`.** Hardware (`cpus`, `memory`, `disk`,
   `swap`) comes from the vm-template; the admin username comes from the admin-template. There are
   no per-create overrides. Deviation means a new template, matching `agent create`,
   `workspace create`, and the rest of the surface.

2. **Delete `azure_vm_size` entirely** from the vm-template schema, the resolved template, the
   config loader, and the `ProvisionRequest`. Azure is now sized from the same `cpus` + `memory`
   spec as every other platform. No escape-hatch SKU field survives: the per-site catalog below is
   the escape hatch.

3. **Select the Azure SKU from a catalog by spec.** The `azure-vm` platform carries a built-in
   catalog of `(cpus, memory, size)` entries (the B-series burstable ladder, from `Standard_B1ms`
   through `Standard_B20ms`). At create time it picks the smallest entry whose cpus **and** memory
   both satisfy the template's request, ordered by `(cpus, memory)`.
   - An exact match provisions that SKU silently.
   - An off-ratio request (Azure SKUs come in fixed vCPU:GiB ratios, so e.g. 4 vCPU / 8 GiB has no
     exact B-series match) rounds **up** to the nearest fitting SKU and warns, naming what it
     picked.
   - A request larger than every catalog entry is an **error**, naming the largest available size
     and hinting at both remedies (shrink the template, or extend the catalog).

4. **Make the catalog overridable per site** via `platform_config.vm_sizes`, a list of
   `{cpus, memory, size}` tables. Its shape is validated at config-load time (through the platform's
   `validate_config`), not deferred to the first `vm create`. This is where an operator who needs
   D-series, a specific SKU, or a region-specific availability set expresses it.

## Consequences

### Positive

- One source of truth for a VM's shape: the template. `vm create` is now shaped like every other
  create command, and the CLI conventions rule ("service layer is the authority", templates over
  flags) holds across the surface.
- Azure templates are portable: a template written with `cpus` / `memory` provisions on Lima,
  Proxmox, and Azure alike. Operators no longer need Azure's SKU vocabulary for the common case.
- The rounding warning makes over-provisioning visible instead of silent, and the no-fit error turns
  a previously-guaranteed Azure API rejection (deep in provisioning, after Azure network resources
  exist) into a failure raised before any Azure resource is created. The DB row is inserted just
  before the platform's `create()` runs, so a no-fit rolls that row back via the standard
  provisioning-failure path rather than leaving it orphaned.

### Negative

- Removing `azure_vm_size` and the override flags is a breaking change. A template or config that
  still sets `azure_vm_size` gets the standard unknown-key warning; a script passing the removed
  flags fails loudly. This is deliberate: the flags and field are gone, not soft-deprecated, because
  they duplicated template state that must not have two homes.
- The built-in catalog is a point-in-time snapshot of Azure's B-series. Azure adds and retires SKUs;
  a stale ladder is corrected by editing the built-in list or, per site, via `vm_sizes`. The catalog
  is intentionally small and burstable-only (cost-appropriate for often-idle agent VMs);
  steady-state workloads override to D-series.
- "Smallest fitting" optimizes for the tightest resource fit, which is not always the cheapest SKU
  across series. Since the built-in catalog is a single series this is moot by default; an operator
  mixing series in `vm_sizes` should order the catalog with that in mind (selection is
  deterministic: minimum by `(cpus, memory)`).

## Alternatives Considered

- **Keep `azure_vm_size` as an optional template override.** Rejected: it reintroduces the
  Azure-specific dialect and a second home for sizing state. The per-site `vm_sizes` catalog already
  covers "I need a specific SKU" without polluting the portable template.
- **Choose "cheapest" instead of "smallest fitting."** Rejected: price is region- and
  contract-dependent and not available offline. `(cpus, memory)` is a stable, inspectable proxy, and
  the catalog is operator-editable when a different preference matters.

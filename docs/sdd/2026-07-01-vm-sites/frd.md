# VM sites and platforms: functional requirements

## Background

The provisioning layer is the boundary between agentworks and the backends VMs run on (Lima locally,
Lima over SSH, Azure, WSL2, Proxmox). Every VM lifecycle operation (create, start, stop, delete,
status, native shell, keep-alive) dispatches through a `VMProvisioner` subclass.

The layer works, but it has accumulated irregularities that make adding a new backend (AWS) more
expensive than it should be, and that leak backend specifics into surrounding code:

1. **Two words for one concept, and a missing concept.** "Platform" is the string discriminator in
   the DB, config, and CLI; "provisioner" is the class name in code; they name the same thing (the
   technology: lima, azure, wsl2, proxmox). Meanwhile the concept operators actually target is
   missing entirely: there is no way to express "the Azure capability, instantiated twice against
   two subscriptions" or "Lima, on three different remote hosts". Today's single `[azure]` config
   section hard-codes exactly one instantiation per backend.
2. **Constructor irregularity.** `LimaProvisioner` takes an optional `vm_host_ssh`.
   `ProxmoxProvisioner` requires a `ProxmoxConfig`; the dispatcher `get_provisioner()` raises for
   `proxmox` and pushes callers to a second entry point. `AzureProvisioner` takes no constructor
   args but reads `config.azure` inside `create()`. Each irregularity is a special case a new
   backend must remember to fit.
3. **Backend-specific fields on shared types.** `ProvisionResult` carries `azure_resource_id`,
   `wsl_distro_name`, and `proxmox_vmid` as named fields, with one DB column each. Adding AWS adds a
   field and a column.
4. **`create()` signatures diverge per backend.** Each provisioner takes its own subset of
   `cpus/memory/disk/swap/azure_vm_size/admin_username/tailscale_auth_key`, no two alike. The
   manager unpacks resolved template values differently per backend to satisfy each shape.
5. **Remote Lima is encoded three ways at once.** A DB table `vm_hosts` (with `platform` and `os`
   columns pretending to be generic but only ever used by Lima); a constructor arg `vm_host_ssh`;
   and an internal `is_remote` branch. One distinction lives in three places because there is no
   concept of "an instantiation of the Lima capability with its own settings".
6. **Two different lifecycle-hook shapes conflated.** `WSL2Provisioner.vm_active()` today both holds
   the distro against idle-shutdown and boots a stopped distro as a side effect. This violates
   operator intent: an explicit `agw vm stop` is silently undone by any subsequent op.
7. **`provisioner_transport()` is abstract but not universally implementable.** Proxmox raises
   `NotImplementedError`. Every future backend must either implement it or raise.
8. **No system identity, no collision protection.** Multiple agentworks installs sharing a cloud
   account, a Proxmox cluster, or a workstation user (Lima, WSL2) will clobber each other's VMs on
   name collision. This is especially painful for people developing agentworks itself.

This SDD addresses the interface debt as a **foundational refactor before adding an AWS platform**.
The central move applies ADR 0016's model to VMs, under the naming the resource-manifests plan
ratified on 2026-07-08:

- A **VM platform** is the capability: baked-in code that knows how to run VMs on one kind of
  backend. One class per backend, registered in `VM_PLATFORM_REGISTRY` and entering the registry as
  a read-only capability resource (kind `vm-platform`). "Platform" keeps the meaning it already has
  on every existing surface (`--platform lima`, `vms.platform = 'azure'`, `AGENTWORKS_PLATFORM=wsl2`
  are all capability names today); "provisioner" retires as its synonym.
- A **VM site** is the declarable resource (kind `vm-site`): the configured API surface for a
  platform, the place where VMs are created, managed, and shelled into. "Azure, in this subscription
  and resource group"; "Lima, over SSH to gpu-box"; "Lima, locally".

VMs are where the dedicated instance kind earns its keep. Secrets collapsed their instantiation
layer (the 2026-07-07 capability collapse) because the instance identity carried no content; VMs
keep it because the identity does: many consumers name the site (`vm-template.spec.site`,
`agw vm create --site`, `defaults.site`, `vms.site` provenance), and "create a VM HERE" wants
multiple named heres per platform without carrying connection configuration on the create command.
This is the instance-identity test ADR 0016 records, and row 4 of the resource-manifests
capability-consumers companion.

VMs reference a site by name. Nothing outside the site layer knows or cares which platform a site
resolves to; platform selection and invocation are encapsulated behind site resolution.

AWS arrives on top of the cleaned interface in follow-on work; per the 2026-07-08 tiering ruling
(see Dependencies and coordination) it likely arrives as a plugin platform, which makes this
refactor its prerequisite either way. Session lifecycle intent and hibernate are also called out as
future work; they build on the operator-intent-vs-observed-state pattern this SDD introduces but do
not need to land together.

## Dependencies and coordination

This SDD builds directly on the resource-manifests SDD (`docs/sdd/2026-07-01-resource-manifests`,
locked and merged) and ADR 0016, and consumes their machinery as-is: the manifest loader and
envelope, per-kind flags (`category`, `builtin_override`), `Registry.add` collision handling, the
built-in manifest bundle (wired, currently empty; this SDD ships its first content), the capability
pattern and its shipped invoked-validation API (`validate_config` returning `ConfigReference`
tuples, per the `GitCredentialProvider` precedent), dual-path TOML publishing with deprecation
warnings, `agw resource migrate/sample/edit/kinds`, the `/` ban in resource names, the `kind/name`
display syntax, and the capability-consumers companion's rules (rows 3 and 4 are the shape this SDD
implements, including the 2026-07-08 naming ruling: `vm-platform` capability, `vm-site` declarable,
`platform` + `platform_config` fields, `--site` on the CLI).

Two coordination notes:

- **Supersession**: the resource-manifests FRD's R1 table routes `[azure]` / `[proxmox]` to "config
  (TOML)" with the note "provisioner capability settings; plugin SDD may revisit". This SDD is that
  revisit: those sections become legacy TOML declarations of `vm-site` resources (R2), riding the
  dual-path deprecation like every other TOML resource section.
- **Plugin SDD boundary and the tiering ruling**: the vm-platform/vm-site reshape lands HERE (it
  needs none of the plugin machinery); the plugin SDD builds on it later (plugin-registered
  `vm-platform` capabilities, plugin-shipped sites under the plugin origin tiers). The
  capability-consumers companion's 2026-07-08 tiering ruling (built-in requires necessary AND
  vendor-neutral) additionally plans to move `azure` / `proxmox` out into plugins and to ship vendor
  platforms like AWS as plugins from birth. This SDD is deliberately tier-neutral: it reshapes all
  four existing in-tree platforms into the capability shape without changing their distribution
  tier, and that shape (`VM_PLATFORM_REGISTRY` registration, capability rows, bundled sites) is
  exactly what the plugin SDD's move-out and any AWS work will reuse. Sequencing of AWS relative to
  plugin infrastructure is that SDD's call, not this one's.

## Terminology

- **VM platform** (capability): a class implementing the `VMPlatform` protocol, registered in
  `VM_PLATFORM_REGISTRY`. Baked into agentworks (plugin-registered platforms arrive with the plugin
  SDD). One per backend kind: `lima`, `wsl2`, `azure`, `proxmox` (later `aws`). Enters the registry
  as read-only capability resources of kind `vm-platform` (category `capability`), so references
  validate uniformly and the rows list/describe like everything else.
- **VM site**: a declarable resource of kind `vm-site` referencing its platform by name, with
  platform-specific configuration in `spec.platform_config`. Declared in operator manifests, shipped
  as bundled built-ins (`lima`, `wsl2`), or published from legacy TOML sections (`[azure]`,
  `[proxmox]`). The `vms.site` DB column (renamed from `vms.platform`), the `--site` flag (renamed
  from `--platform`), `defaults.site`, and `vm-template.spec.site` all hold site names.
- **Platform metadata**: the opaque `dict[str, str]` a platform stores in the DB to identify a VM's
  backend-side resources (e.g. `{'resource_id': '/subscriptions/.../vm-x'}`,
  `{'vmid': '104', 'node': 'pve1'}`). Read and written only by the owning platform. Distinct from
  `platform_config` (how to reach the site's backend) and unrelated to the resource registry.
- **System slug**: a short operator-chosen string identifying this agentworks installation. Used by
  platforms as a namespacing token in backend-side resource names. Nullable.
- **Operator-stopped**: a recorded fact, not a state: the operator's last lifecycle command for this
  VM was `stop`. Distinct from **observed state** (what the platform reports right now): an
  idle-shutdown VM is observed stopped without being operator-stopped, and that auto-stop is
  desired, not a divergence. Only explicit operator commands write the flag; auto-behaviors (WSL2
  idle-shutdown, future auto-suspend) never do.
- **Ensure-active**: the lazy gate. Before any operation that needs a VM running: respect an
  operator stop (error), otherwise start on demand. Fires only at op-time; there is no background
  controller.
- **`vm_active`**: the per-platform idle-hold context manager. Prevents the backend's own
  idle-shutdown mechanism from firing while an operation is in progress.
- **Provisioning** (the activity) keeps its name everywhere: `provisioning_status`,
  `ProvisionRequest`, vm event names. The noun retires: "provisioner" was a synonym for the platform
  and disappears from code and docs, including `ProvisionerError` (renamed `ProvisioningError`; it
  is morphologically the noun) and the `agentworks.transports.provisioner_transport()` factory
  (renamed `native_transport()`, R8).

## Requirements

> **Design revision (2026-07-12): capability model adoption.** `capability-model.md` (in this SDD)
> now owns the capability lifecycle contract; Phase 7 of the plan adopts it for the
> vm-platform/vm-site pair. It amends the requirements below at the requirement level:
>
> - Platforms construct bound to `(config, resolver)`, never to resolved secret values; binding
>   neither resolves nor prompts. Where R2 places value resolution "at the consuming command's
>   composition root", the timing within that root is revised: resolution happens after every
>   participating resource's `preflight` passes, never at command entry and never deferred to an
>   op's first need.
> - Everything preflights: every service-layer operation runs `preflight` on all the resources it
>   will use (the vm-template predicts its Tailscale key can resolve -- the template's
>   responsibility, not the site's; the platform instance checks tools, reachability, and secret
>   mappings) before any mutation and before any secret prompt. Preflight is read-only; doctor
>   reuses it.
> - The one resolve pass covers the union of secrets needed across all planned ops across all
>   participating resources: one prompt session per command, values cached.
> - Catch-all handling around best-effort spans (the resolve pass included) re-raises `UserAbort`; a
>   Ctrl-C at a prompt aborts the operation cleanly, always.
> - Mutating ops carry per-op idempotency flags on the kind ABC.
> - The base `Capability` class and the platform implementations live in a `capabilities/` subtree.

### R1: Platform capability, site resource

- One platform class per backend, registered in `VM_PLATFORM_REGISTRY` (domain-scoped symbol per ADR
  0016's naming rule). Each registration publishes a read-only `vm-platform` capability resource
  (origin `built-in`, error miss policy; manifest documents of the kind get the provided-by-the-app
  error), following the `secret-backend` / `git-credential-provider` precedents.
  `agw resource kinds` lists both new kinds with their categories.
- The vocabulary law holds: `vm-platform` and `vm-site` are registry kinds; `platform` is the
  capability-reference field on the site (named for what it references, per the 2026-07-08 ruling).
  Lifecycle entities (the VMs themselves) remain non-resources.
- **Encapsulation**: manager code selects and resolves sites; it never imports platform classes or
  `VM_PLATFORM_REGISTRY`. Platform invocation happens behind site resolution (dispatch returns the
  platform bound to the site's validated configuration). This is domain modeling, not framework law:
  the whole point of the site noun is that nothing else needs to pick a platform.
- Code identifiers follow the model: the ABC becomes `VMPlatform` (concrete `LimaPlatform`,
  `AzurePlatform`, `WSL2Platform`, `ProxmoxPlatform`), the module directory becomes
  `vms/platforms/`, and "provisioner" as a noun disappears from code and docs: `ProvisionerError`
  becomes `ProvisioningError`, and the `agentworks.transports.provisioner_transport()` factory
  becomes `native_transport()` alongside the R8 method rename. Names that refer to the provisioning
  activity (`ProvisionRequest`, `provisioning_status`, vm event names) are retained.

### R2: Sites are `vm-site` resources

Sites are declared through the resource-manifests machinery, not through new config sections:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: azure-dev
  description: Dev subscription in eastus
spec:
  platform: azure
  platform_config:
    subscription_id: "..."
    resource_group: agw-dev
    region: eastus
---
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: gpu-box
spec:
  platform: lima
  platform_config:
    vm_host: scot@gpu-box
```

- **Uniform spec envelope** (ADR 0016's reference + blob shape): `spec.platform` (required)
  references a `vm-platform` capability by name; `spec.platform_config` (optional mapping) is the
  single sibling blob the named platform owns and validates. Kind-generic fields live at the spec
  top level; platform-owned fields nest; `platform_config` keys may not shadow top-level spec keys.
  The internal representation nests too (`VMSiteDecl.platform_config`), matching the git-credential
  precedent.
- **Validation uses the shipped invoked-validation API**: the named platform's
  `validate_config(owner, config)` classmethod runs at each source's blob boundary (manifest decode
  with `file:line` framing; the legacy TOML loader) and at finalize via the site's
  `referenced_resources()`. It raises `ConfigError` on unknown or missing fields and returns the
  `ConfigReference` tuple the blob implies; the site attaches itself as the source. Same shape, same
  may-be-deprecated-for-registered-schemas note as `GitCredentialProvider.validate_config`.
- **`spec.platform` is a reference edge**: a typo'd platform name fails through the framework's
  uniform miss policy at finalize, like every other cross-resource reference.
- **Secrets in `platform_config` are ordinary secret references** (capability-consumers rule 8): a
  blob field can hold a secret NAME, defaulted to a well-known name or required-explicit; the site
  emits the reference (whoever hosts the config emits the reference), so auto-declaration,
  reachability checks, doctor rows, and `Referenced by:` all work stock. Values resolve at the
  consuming command's composition root through the standard single resolve pass (the
  `compute_needed_secrets(..., extra_decls=...)` hook the tailscale and git-credential secrets
  already use), never at registry build. Concrete first user: the Proxmox API token. Today
  `ProxmoxProvisioner.__init__` raw-reads a `PROXMOX_TOKEN_SECRET` env var and raises RuntimeError
  when absent; it becomes `platform_config.token_secret`, defaulting to the well-known secret name
  `proxmox-token-secret` (auto-declared, resolvable through the standard chain; see R13 for the
  operator-visible compat note).
- **Built-in sites**: platforms that work with zero configuration ship bundled site manifests named
  after themselves. Today that is `lima` (local) and `wsl2`: the first real content of the app's
  built-in manifest bundle, origin `built-in`. Their names are **reserved**
  (`builtin_override = "reserved"`, repopulating the tier the capability collapse left memberless):
  an operator manifest redeclaring one is a load error with a declare-a-sibling hint. Bundling
  zero-config sites (rather than letting VMs reference the capability directly) keeps `vms.site` one
  uniform reference space: every VM points at a `vm-site`, never sometimes at a capability.
- **Legacy TOML sections**: `[azure]` and `[proxmox]` become legacy TOML declarations of `vm-site`
  resources. The legacy loader lives in `config.py`, which ADR 0016 designates as the home of
  settings plus the legacy TOML resource loaders/publisher (the domains-own-their-kinds ruling moved
  kind definitions and row dataclasses, not TOML loaders; the shipped git-credential legacy loader
  is the precedent). It publishes them as `vm-site/azure` and `vm-site/proxmox` rows
  (operator-declared origin, TOML `file:line`), the standard aggregated dual-path deprecation
  warning points at `agw resource migrate`, and the migrator's kind-to-section mapping gains the
  (irregular) shape: section name becomes the site name, `spec.platform` is synthesized, section
  keys nest under `platform_config`. Flat TOML is the one place platform-owned fields sit outside
  the blob (ADR 0016); the TOML loader nests at its boundary, exactly as git-credential's loader
  does. Existing DB rows for `azure` and `proxmox` keep resolving with zero operator edits.
- **Site names** follow the VM-name rules (`validate_name`: lowercase alphanumeric, hyphens,
  underscores, max 30 chars); the framework's `/` ban applies as to every resource. A site named
  after a known platform must declare that platform (redeclaring a reserved built-in name is already
  an error; this rule covers non-bundled platform names like `azure`).
- **Site selection**: `agw vm create --site` replaces `--platform` (the sanctioned break; the old
  flag's values were always capability names, and the new flag names the site); `vm-template` gains
  an optional `site` field (a resource-to-resource reference by bare name, emitted as a
  vm-template-to-vm-site edge); `defaults.site` replaces `defaults.platform`, with the old key
  accepted as a deprecated alias for one release (warning joins the aggregated deprecation block).
  Precedence: CLI flag, then template, then `defaults.site`, then the built-in `lima`. Per ADR 0016,
  `defaults.site` is a setting that names a resource: never published, validated by the VM subsystem
  against the finalized registry at the composition boundary (`vms.validate_sites`, run by
  `build_registry`), config vocabulary in the errors.
- **Resource surfaces come for free**: `agw resource list --kind vm-site`,
  `agw resource describe vm-site/azure-dev` (and `vm-platform/azure`, which lists the sites
  referencing it), `agw resource edit vm-site/<name>` for manifest-declared rows, origin display,
  `agw resource migrate` selectors, and registry-backed completions. `agw resource sample vm-site`
  ships real, config-bearing sample documents.

### R3: Remote Lima is site configuration; vm_hosts retires

The distinction that today lives in three places collapses into one: the presence of `vm_host` in a
Lima site's `platform_config`.

- `LimaPlatform` accepts an optional `vm_host` key (SSH host). Present means limactl over SSH;
  absent means local limactl. The built-in `lima` site is local.
- The `vm_hosts` DB table, `vms.vm_host_name` column, the `agw vm-host` command group, the
  `--vm-host` flag on `vm create`, and `defaults.vm_host` are all removed. A remote Lima host _is_ a
  site declaration now.
- Config compat: a config file that still sets `defaults.vm_host` gets a config error carrying the
  replacement `vm-site` manifest snippet. This is a hard error where `defaults.platform` gets a
  one-release alias (R2) because no automatic alias is possible here: the replacement points at a
  site manifest only the operator can author.
- Migration (see R14): rows with a `vm_host_name` are rewritten to `site = <vm_host_name>`, and the
  migration prints ready-to-paste `vm-site` manifest documents built from the old `vm_hosts` rows,
  to be saved under the resources directory (agentworks does not write operator manifests unasked).
  Until the operator adds them, any op on such a VM raises `ConfigError` containing the same
  snippet. `agw doctor` verifies every `vm.site` resolves to a declared site.

### R4: System identity via a nullable slug

A new `settings` table stores install-level state; the `system_slug` key holds the slug.

- **Format**: 3-20 characters, lowercase alphanumeric plus dash, no leading/trailing dash. Passes
  Azure's naming rules (the strictest we target), therefore passes all of them.
- **Prompted once at first `vm create`** (there is no `agw init` command today, and this SDD does
  not add one). When the settings row is absent, `vm create` prompts:

  > A system slug uniquely identifies this agentworks installation. It is used to namespace VMs and
  > other resources so this install does not collide with others that share the same cloud account,
  > Proxmox cluster, or Windows/Mac user. Leave blank if this install is the only one using its
  > sites' backends. [system slug]:

  An empty response is accepted and recorded as declined (the settings row is written with an empty
  value, distinct from the absent row that means never-asked), so the prompt fires once regardless
  of the answer.

- **Non-interactive runs never prompt**: a non-interactive `vm create` proceeds with a null slug and
  does NOT write the settings row (a later interactive create still asks), and the deferred nudge
  below is skipped entirely when non-interactive.

- **Effectively immutable**: no rename command in this SDD. The design (R5, R10, R11) is such that a
  slug change would not corrupt existing state, but the operation is not exposed.
- **Deferred nudge**: when creating a VM on a shared-backend site (Azure, Proxmox, AWS later, or
  remote Lima) with a null slug, the CLI prompts non-blocking: "you are creating a VM on a site
  whose backend may be shared with other agentworks installs; VM names may collide. Set a slug now?
  [Y/n/never-remind-me]". `never-remind-me` writes a suppression flag in settings. Whether a site is
  shared-backend is declared by its platform (Lima computes it from the presence of `vm_host` in
  `platform_config`).
- **Never surfaced in normal CLI output**: `agw vm list` shows `vm.name`, not the slug. The slug
  appears in `agw doctor`, `agw vm describe`, and error messages that reference backend-side names.
- The slug is read from the DB at the points that need it (building a `ProvisionRequest`, SSH config
  sync). It is not loaded into `Config`, which stays purely config.toml-derived.

### R5: Platform-owned naming with opaque platform metadata

The platform owns backend-side names. The manager and CLI never construct one; they hand a `vm.name`
and the system slug to the site's platform and let it choose.

- The DB column `vms.platform_metadata JSON NOT NULL DEFAULT '{}'` replaces the three
  backend-specific columns (`azure_resource_id`, `wsl_distro_name`, `proxmox_vmid`).
- `ProvisionResult.platform_metadata: dict[str, str]` replaces the three named fields.
- `create()` returns `platform_metadata`; the manager writes it verbatim. Every subsequent lifecycle
  op receives the full `VMRow` and reads `vm.platform_metadata` to identify the backend-side
  resources. The owning platform is the only reader.
- Platforms record everything ops need to find the resource again without live configuration:
  Proxmox captures the node alongside the vmid (today a node change in config strands existing VMs);
  Azure keeps the full resource ID (which already embeds subscription and resource group).
- Keys are absent when there is nothing to record, never empty strings.
- The platform is **encouraged** to use the system slug when constructing new backend-side names
  (default derivation: `{slug}-{vm_name}`, or its backend-appropriate equivalent). When the slug is
  null, the platform falls back to `vm.name` alone, matching today's unqualified behavior.

**Non-goal**: adoption/import of existing backend-side VMs. The platform-owned-naming shape makes it
structurally possible (a future `agw vm adopt` could hand pre-existing platform_metadata to the DB),
but neither the CLI command nor a formal adoption entry point is in scope.

### R6: Operator-stop intent is recorded explicitly

A new column `vms.operator_stopped INTEGER NOT NULL DEFAULT 0` (a boolean; SQLite has no native
boolean storage class, and `agents.grant_all` is the existing 0/1 precedent, with `bool` on the row
dataclass) records one fact: the operator's last lifecycle command for this VM was `stop`.

This is deliberately a flag about a past action, not a "desired state" enum: on platforms with
idle-shutdown (WSL2 today, auto-suspending clouds later), a VM is expected to be observed stopped
much of the time while remaining available on demand, and that auto-stop is itself desired. A fact
about the operator's last command cannot be contradicted by anything an idle timer does. (An earlier
draft modeled this as `desired_state in {running|available, stopped}`; every value name for the
not-stopped case either lied or needed a paragraph of apology, which was evidence about the shape,
not the words.)

Writes:

- `agw vm create` clears `operator_stopped`.
- `agw vm start` clears `operator_stopped`, then starts the VM.
- `agw vm stop` sets `operator_stopped`, then stops the VM. The flag is set even when the VM is
  already observed stopped (today's `stop_vm` short-circuits with "already stopped"; stopping an
  idle-stopped VM is the operator saying "and keep it that way", so the intent write happens before
  the short-circuit).
- No other code writes it. Idle-shutdown, future auto-suspend, spot reclamation, and other
  auto-behaviors never touch it.

Reads: the manager's ensure-active gate (R7), and display: `vm describe` (and `vm list`'s status
rendering) can distinguish `stopped (operator)` from `stopped (idle)` by pairing observed state with
the flag.

Migration: existing rows default to false (there is no way to know whether an old VM was
operator-stopped; false matches today's auto-start-on-use behavior).

Future intents decompose onto other fields rather than widening this one: hibernated-vs-stopped is
how a VM sleeps (observed state / suspend mechanism, follow-on hibernate SDD), and an always-on
auto-suspend exemption is a suspend-side setting (follow-on auto-suspend SDD). This flag stays the
resume-side answer to exactly one question: may the gate start this VM?

### R7: Split ensure-active (gate) from vm_active (idle-hold)

Two distinct concerns, currently conflated inside `WSL2Provisioner.vm_active` and partially
duplicated by `workspaces.manager._ensure_vm_running`, are separated:

**Ensure-active** (new manager-layer function; absorbs `_ensure_vm_running`, which several modules
import privately from `workspaces.manager` today):

- Called before any op that needs the VM running.
- Fast path first: if the VM's Tailscale address answers a reachability probe, the VM is running;
  proceed without querying the platform. This keeps cloud-status API calls (Azure ARM round trip
  plus credential acquisition) off the per-op hot path.
- Otherwise query observed status:
  - RUNNING: proceed.
  - STOPPED or DEALLOCATED, `operator_stopped` clear: start the VM, wait for Tailscale reachability
    (inside the platform's `vm_active` hold so a freshly booted WSL2 distro does not idle out
    mid-wait), then proceed. **This is auto-resume.**
  - STOPPED or DEALLOCATED, `operator_stopped` set: raise `StateError` ("VM is stopped; start it
    with `agw vm start`").
  - UNKNOWN: proceed and let the operation surface the real error (matches today's
    `_ensure_vm_running`; a transient status failure must not trigger a spurious start).
- Never writes `operator_stopped`. Never starts a VM the operator has asked to stay stopped.

**`vm_active`** (per-platform context manager, retained from today):

- Contract: hold the VM against the backend's idle-shutdown mechanism for the context's duration.
  Callers run ensure-active first, so on entry the VM is either running or was just started.
- The WSL2 implementation keeps today's attach semantics
  (`wsl --distribution NAME -- sleep infinity`), including the property that attaching boots a
  stopped distro. Under the gate this is safe by construction: the only path that reaches
  `vm_active` with `operator_stopped` set raises in ensure-active first, so a boot-on-attach can
  only ever implement auto-resume (and self-heals the race where the idle timer fires between the
  gate's check and the attach). No hard precondition check; no extra status round trip.
- Default remains a no-op for platforms without an idle-shutdown mechanism (Lima, Azure, Proxmox).

The pair composes as `keep_active` (gate, then hold), replacing `keep_vm_active` /
`keep_vms_active`. The rename is deliberate: callers that relied on `keep_vm_active` implicitly
booting a WSL2 distro get the new gated behavior, and the old name cannot be called by stale code.
Existing deliberate carve-outs stay carved out: `stop_vm` (would fight `wsl --terminate`), the
`describe_*` family (degrade silently when unreachable), and the multi-console best-effort ops.

### R8: Uniform platform protocol behind site resolution

The platform surface after this SDD:

```python
class VMPlatform(ABC):
    # Class-level: name, validate_config (the shipped invoked-validation
    # API: validate the platform_config blob, return the ConfigReference
    # tuple it implies), shared_backend, legacy_platform_metadata
    # migration hook (see HLA).

    @abstractmethod
    def create(self, request: ProvisionRequest) -> ProvisionResult: ...
    @abstractmethod
    def start(self, vm: VMRow) -> None: ...
    @abstractmethod
    def stop(self, vm: VMRow) -> None: ...
    @abstractmethod
    def delete(self, vm: VMRow) -> None: ...
    @abstractmethod
    def status(self, vm: VMRow) -> VMStatus: ...
    @abstractmethod
    def display_backend_name(self, vm: VMRow) -> str: ...
    def native_transport(self, vm: VMRow, *, config: Config | None = None) -> Transport | None: ...
    def post_tailscale_ready(self, vm: VMRow) -> None: ...
    def transient_route(self, vm: VMRow) -> AbstractContextManager[None]: ...
    def vm_active(self, vm: VMRow, *, config: Config | None = None) -> AbstractContextManager[None]: ...
```

Key shape changes:

- **Dispatch returns a bound platform**: manager code resolves a site (`platform_for(vm, registry)`)
  and receives the platform instance bound to the site's validated `platform_config` (plus any
  resolved config secrets); it never touches `VM_PLATFORM_REGISTRY` or platform classes. A bound
  instance fits VM lifecycles (platforms hold API clients; every call consults the config), where
  secrets chose a stateless per-call API; each domain owns its invocation shape.
- **`create()` takes a single `ProvisionRequest`** carrying every field any platform might need:
  `vm_name`, `hostname`, `system_slug`, `admin_username`, `ssh_public_key`, `tailscale_auth_key`
  (nullable; WSL2 defers Tailscale to Phase A), and resolved template values in the codebase's
  native GiB units (`cpus`, `memory_gib`, `disk_gib`, `swap_gib`, `azure_vm_size`, ...). Each
  platform ignores what it does not use. Adding AWS means adding optional fields here, not changing
  the protocol.
- **`create()` returns a generalized `ProvisionResult`** whose `platform_metadata` is the opaque
  dict from R5, and whose transport field is `native_transport`.
- **`native_transport()` (was `provisioner_transport()`) becomes optional and may return `None`.**
  The `agentworks.transports` factory raises a typed `StateError` for `None` (same operator-visible
  behavior as today's `NotImplementedError` catch). Proxmox returns `None`. The
  `agw vm shell --provisioner` flag is renamed `--platform` (a boolean: connect via the platform's
  native transport instead of Tailscale; the value-taking `--platform` is gone from the surface, so
  no clash), keeping `--provisioner` as a hidden alias for one release (the same window as the
  `defaults.platform` alias, R2).
- **Constructors are uniform**: every platform is constructed by the site layer from the site name
  and its validated `platform_config`. No special-case branches anywhere.
- **`display_backend_name()`** (was sketched as `display_platform_name`; renamed because "platform
  name" now means `azure`) exposes a short human-readable identifier for the backend-side resource,
  replacing the `describe_vm` lines that read the three legacy columns directly.

### R9: Collision detection at create-time

Every platform's `create()` performs a pre-flight check: does a backend-side resource with the
intended name already exist? If yes, either:

1. Append a short random suffix and retry (for backends where the name is a soft identifier, e.g. an
   AWS Name tag), or
2. Raise `StateError` with clear guidance. This is the policy for all four in-tree platforms: Lima,
   WSL2, and Azure because the name is the primary identifier there, and Proxmox because although
   PVE names are soft platform-side (the vmid identifies), a duplicate name on the node is almost
   certainly operator confusion worth surfacing.

The check catches: an operator-created colliding resource, a DB row removed while the backend
resource survived, and slug-less collisions across installs.

### R10: SSH config multi-install coexistence

Applies to the config.d mode (`operator.ssh_config_dir = true`, the default). The legacy in-file
managed section remains single-install only and is documented as such.

- Managed file name becomes `~/.ssh/config.d/agentworks-{system_slug}.conf` when a slug is set;
  stays `agentworks.conf` when the slug is null.
- The existing `Include ~/.ssh/config.d/*` directive already matches both shapes; no new include
  directive is needed.
- The first sync after the slug is set (the slug arrives at first `vm create`, not at DB migration)
  writes the new file and removes the old `agentworks.conf` so stale aliases cannot shadow fresh
  ones.
- Host aliases inside the file continue to use `vm.name`. **Known limitation**: aliases
  (`awvm--{vm.name}`) can still collide across installs that use identical VM names; the existing
  per-install `operator.ssh_host_prefix` / `ssh_agent_host_prefix` settings are the supported
  remedy, and the multi-install docs say so.

### R11: VM hostname and Tailscale node naming

Today `bootstrap_script.vm_hostname()` derives `{platform}--{vm_name}` and bakes it as the VM's OS
hostname, which tailscaled picks up as the node name. Changes:

- New scheme: `{slug}-{vm.name}` when a slug is set, `{vm.name}` otherwise. The platform prefix is
  dropped (backend identity should not leak into hostnames). The composite is bounded by
  construction: slug max 20 (R4) plus dash plus name max 30 (`validate_name`) is 51 characters,
  inside the 63-character hostname-label limit and Azure's 64-character computer-name limit.
- The chosen hostname is recorded in a new `vms.hostname` column at create time. `vm reinit` reuses
  the recorded value, so reinitializing an existing VM does not silently rename it. Migration
  backfills `hostname = '{platform}--{name}'` for existing rows (the value the bootstrap script
  actually set).
- `vm.tailscale_host` continues to store the Tailscale IP (not the node name); connectivity never
  depends on node naming, which is why tailnet-side suffixing remains harmless.

### R12: Identity env vars

- `AGENTWORKS_PLATFORM` keeps its name AND its values: it has always carried the capability name
  (`lima`, `azure`, `wsl2`, `proxmox`), which is exactly what "platform" means post-rename. No
  existing VM's value changes, remote-Lima included.
- New `AGENTWORKS_SITE` carries the site name.
- `AGENTWORKS_VM_HOST` retires with the `vm_hosts` registry (its only source). It was emitted only
  for remote-Lima VMs; the site name now conveys the same information.
- Identity fragments are baked on-VM at init and refresh at reinit, unchanged mechanics.

### R13: Operator-visible changes are bounded to this list

- Ops against an operator-stopped VM now raise `StateError` instead of silently booting it (WSL2 was
  the silent-boot case). This is the headline semantic change of the SDD.
- **`--platform` becomes `--site` on `vm create`** (the maintainer-sanctioned break recorded in the
  resource-manifests plan); `defaults.platform` becomes `defaults.site` with the old key as a
  deprecated alias for one release; `vm list`'s PLATFORM column becomes SITE; `vm describe` shows
  both Site and Platform plus the platform's `display_backend_name()`.
- One-time slug prompt at first `vm create`; deferred nudge on shared-backend sites (R4).
- `agw vm-host` command group, `--vm-host` flag, and `defaults.vm_host` removed (R3).
- `[azure]` / `[proxmox]` sections warn as deprecated TOML resource sections pointing at
  `agw resource migrate` (R2), joining the aggregated dual-path deprecation warning (and its
  `--no-deprecations` escape).
- **Proxmox token sourcing changes** (R2): the raw `PROXMOX_TOKEN_SECRET` env var read is replaced
  by the `proxmox-token-secret` secret resolved through the standard chain. The default `env-var`
  backend convention reads `AW_SECRET_PROXMOX_TOKEN_SECRET`; operators keeping the old variable name
  declare the secret with a one-line `backend_mappings: {env-var: PROXMOX_TOKEN_SECRET}`.
  Interactive use falls back to the prompt backend instead of a RuntimeError. Release notes and the
  migration output carry the pointer.
- New registry kinds visible in `agw resource list` / `agw resource kinds`: declarable `vm-site`
  rows (bundled, manifest, and legacy TOML origins) and read-only `vm-platform` capability rows;
  `agw resource sample vm-site` gains real sample documents; `vm-template` accepts an optional
  `site` field (R2).
- New VMs get the R11 hostname scheme; existing VMs keep their hostnames.
- `vm shell --provisioner` renamed `--platform` (boolean; hidden alias retained).
- Error messages referencing "platform" strings are audited so the word consistently means the
  capability (bounded carve-out, same reasoning as the polymorphic-transports SDD).

Everything else (operation semantics, exit codes, retry behavior, environment injection, transport
dispatch) is preserved.

### R14: Migration is one-shot with platform-owned backfill

All schema and data changes land in a single migration version on next agw run. The migration runner
gains support for Python migration steps alongside SQL strings, because the platform_metadata
backfill is platform-owned code: each platform supplies a pure
`legacy_platform_metadata(row, legacy)` hook mapping the legacy column values to its metadata
conventions. `legacy` is a best-effort parse of the config's legacy TOML sections supplied by the
migration step; migrations run at `Database()` open where no validated `Config` exists, so the parse
is unvalidated and a missing or unreadable config yields an empty mapping (hooks are pure over their
two inputs; no network). Proxmox is the one consumer: it records the node from `legacy['proxmox']`
when present and omits the key otherwise, in which case ops fall back to the site's
`platform_config` node and opportunistically write it back to metadata (see HLA). The migration:

1. Adds `platform_metadata`, `operator_stopped`, and `hostname` columns.
2. Backfills `platform_metadata` per row via the owning platform's hook, and `hostname` per R11.
3. Renames `vms.platform` to `vms.site` (the values are already the right site names for every
   non-remote-Lima row: `lima` and `wsl2` are the bundled sites, `azure` and `proxmox` the legacy
   TOML sites). Remote-Lima rows (`vm_host_name` set) get `site = <vm_host_name>` instead, and the
   step prints ready-to-paste `vm-site` manifest documents from the old `vm_hosts` rows (R3).
4. Drops `azure_resource_id`, `wsl_distro_name`, `proxmox_vmid`, and `vm_host_name`, and drops the
   `vm_hosts` table. (The `vm_host_name` foreign key forces a table rebuild rather than
   `DROP COLUMN`, and the rebuild also carries the `platform` to `site` rename; see HLA.)
5. Creates the `settings` table.

Existing VMs retain their backend-side names verbatim (created without the slug prefix, they keep
that shape). `operator_stopped` defaults to false for all existing rows.

## Out of scope

- **The AWS platform itself.** This SDD prepares the interface; AWS arrives in follow-on work,
  likely as a plugin platform per the tiering ruling (see Dependencies and coordination). Note the
  capability-config secret machinery (R2) means AWS credentials-by-secret needs no new design.
- **Session lifecycle intent.** The operator-intent vs observed-state pattern applies naturally to
  sessions; separate SDD. Nothing here blocks it.
- **Hibernate.** A platform hibernate method and per-platform capability declarations are future
  work. Per R6, hibernated-vs-stopped is expected to decompose onto observed state plus the
  `operator_stopped` flag (hibernate is how a VM sleeps, not a distinct intent), so no schema change
  is anticipated; the follow-on SDD confirms or corrects that.
- **Auto-suspend enforcement.** The gate (R7) makes auto-resume correct without changing anything
  else. When and how to suspend an idle VM is a separate feature.
- **VM adoption / import CLI.** R5 makes it structurally possible; not built here.
- **Slug rename command.** R4/R5/R10/R11 make the slug effectively immutable-in-practice; ship no
  rename command, document manual steps if ever needed.
- **Continuous reconciliation.** The gate fires only when an op needs the VM.
- **Multiple systems on the same workstation.** The DB-path separation that would let two full
  installs coexist on one workstation is a prerequisite for the slug to fully deliver there, but is
  not changed in this SDD.
- **Cross-install visibility** (`agw vm list --other-systems`). Interesting; not in scope.
- **Plugin-registered VM platforms, plugin-shipped sites, and the tiering move-out.** All four
  existing platforms stay in-tree at their current distribution tier in this SDD;
  `VM_PLATFORM_REGISTRY`, the `vm-platform` capability kind, and the bundled-site mechanism are the
  paved road the plugin SDD extends when it executes the tiering ruling (`azure` / `proxmox` to
  plugins, vendor platforms born as plugins; see Dependencies and coordination).
- **Schema-registration for capability config.** This SDD uses the shipped invoked-validation API
  as-is; the declarative-schema upgrade the API's docstrings reserve is future work.

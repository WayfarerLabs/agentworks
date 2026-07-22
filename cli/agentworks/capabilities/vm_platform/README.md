# Developing a VM platform

> Practical guidance for authors of `vm-platform` capabilities. This is the platform-kind companion
> to the capability contract in [`../README.md`](../README.md): that doc defines the lifecycle every
> capability obeys (`validate_config`, preflight, runup, ops); this one covers what is specific to
> running VMs, plus the gotchas that have already bitten real platforms.

Four platforms ship today and are the working references throughout this guide: `lima` (`lima.py`),
`wsl2` (`wsl2.py`), `azure-vm` (`azure_vm.py`), and `proxmox` (`proxmox.py`). When a rule below has
a concrete example, it names the platform and file that demonstrates it.

## What a VM platform is

A VM platform is the code that runs VMs on one backend kind. Each subclasses `VMPlatform`
(`base.py`), registers in `VM_PLATFORM_REGISTRY` (`__init__.py`), and publishes as a read-only
`vm-platform` capability resource. Operators never invoke a platform directly: a declarable
`vm-site` binds a platform to a config blob (`spec.platform` + `spec.platform_config`), and all
invocation goes through site resolution (`agentworks.vms.sites`). ADR 0016 records the
capability/declarable split; ADR 0019 records the orchestration layer that now drives the lifecycle
(below).

## The platform surface

The authoritative contract is `base.py`. Implement the ops, override only the hooks your backend
needs, and fill in the class-level contract methods the site decoder and DB migration consume.

**Ops** (the mutation surface). Every op except `display_backend_name` takes the op-start
`RunContext` as its last parameter (see the next section for what that is and how to read from it):

- `create(request, ctx) -> ProvisionResult` is deliberately **not** `@idempotent_op`: it runs a
  pre-flight collision check and raises `StateError` on a name that already exists, so a re-run is a
  loud error, never a silent second VM.
- `start(vm, ctx)`, `stop(vm, ctx)`, `delete(vm, ctx)` are flagged `@idempotent_op` and must land in
  the same place run twice as run once. The marker is inherited through the MRO, so an override does
  not restate the decorator. `reinit` re-applies everything and failed commands are retried, so the
  guarantee has to be real: `start`/`stop` on Lima, WSL2, and Proxmox check `status()` first and
  short-circuit, because the backend verb is not reliably a no-op on an already-in-state instance;
  Azure needs no guard because its SDK start/deallocate calls are themselves idempotent; `delete` is
  unconditionally best-effort across all four (already-gone is success).
- `status(vm, ctx) -> VMStatus` is a read-only query.
- `display_backend_name(vm) -> str` is pure display and takes no `ctx`.

**Transport and lifecycle hooks** (sensible defaults on `VMPlatform`; override only what your
backend needs). All are entered by callers that gate first, so on entry the VM is running or was
just started:

- `native_transport(vm, *, config=None) -> Transport | None` (default `None`). The
  `agentworks.transports.native_transport` factory wraps the call in `transient_route`, probes
  reachability with an `echo ok` retry loop, and raises a typed `StateError` (using
  `no_native_transport_hint`) when a platform returns `None`. Lima returns a `limactl shell`
  transport, Azure an `SSHTransport` against the transient public IP, WSL2 a `wsl.exe`-backed
  transport. Proxmox deliberately returns the default `None` and sets `no_native_transport_hint` to
  point the operator at the Proxmox web-UI serial console, because its guest-agent exec is one-shot
  and cannot host an interactive shell.
- `transient_route(vm) -> context manager` (default `nullcontext()`). Azure attaches a public IP on
  enter and detaches it in a `finally`, bounding the exposure window to the transport's lifetime.
- `vm_active(vm, *, config=None) -> context manager` (default `nullcontext()`). WSL2 returns a
  keepalive that holds the distro against Windows' idle-shutdown for the span of a command, with
  Win32 Job-Object orphan-proofing for a hard-killed `agw`.
- `post_tailscale_ready(vm) -> None` (default no-op). Azure detaches its public IP here, the instant
  Tailscale is reachable. The asymmetry with `transient_route` is intentional: the attach happens
  inside `create()` (cloud-init needs the IP to bootstrap), and neither that nor this detach point
  is context-manager-shaped.

**Gates** (cheap, offline, distinct from preflight):

- `unsupported_reason()` is a class-level, zero-arg classmethod run at every registry build. It
  answers "could any config of this platform ever work on this host." WSL2 is the only platform that
  overrides it (`"Windows only"` off Windows). Lima deliberately does not: a remote-Lima site runs
  `limactl` on the `vm_host` over SSH and needs nothing locally.
- `disabled_reason()` (inherited from `Capability`) is instance-level: "is this configured site
  ready," host-introspection only, no network or secrets. A local Lima site with no `limactl` is
  disabled; a remote one is not. WSL2 disables a site with no `wsl` on PATH even on Windows.

**Class-level contract methods**:

- `validate_config(owner, config) -> tuple[ConfigReference, ...]` is a pure classmethod that
  validates the `platform_config` shape and declares any secret references the config implies.
  Proxmox returns a `ConfigReference(kind="secret", ...)` for its API token here; declaring it is
  what later lets the op read it (below).
- `legacy_platform_metadata(cls, row, legacy) -> dict[str, str]` maps pre-migration DB rows into the
  `platform_metadata` shape, consumed only by the one-shot DB migration.

**Inputs and outputs** are uniform. Every `create` receives the same `ProvisionRequest` and returns
a `ProvisionResult` whose `platform_metadata` is written verbatim to `vms.platform_metadata` and
read back only by the owning platform (Lima stores `instance_name`, WSL2 `distro_name`, Azure
`resource_id`, Proxmox `vmid` + `node`). Add a platform-specific **input** by adding a field to
`ProvisionRequest`, not by changing the protocol. But note the opposite pattern is also right:
purely internal translation stays inside the platform. Azure's VM-size selection (mapping the
request's `cpus`/`memory_gib`/`disk_gib` onto a concrete SKU, with a `platform_config.vm_sizes`
override, per ADR 0018) lives entirely in `azure_vm.py` and adds nothing to `ProvisionRequest`.

## How an op gets its dependencies: `RunContext`

This is the part the orchestration-layer refactor (ADR 0019) changed most, and the part a platform
author most needs to get right.

**A platform instance never holds a value source of its own.** Construction binds only
`(owner_name, config)`. There is no resolver parameter, no bound secret reader, no client bridge
(all retired by ADR 0019). Everything a stage needs arrives through the `RunContext` handed to it.

`RunContext` (`../base.py`) is a frozen dataclass, rebuilt fresh per stage (never mutated, never
`replace()`'d). It carries `config` and `operation_scope` as plain fields, and grants power through
accessor methods rather than bare fields so a future permission model can gate them without changing
signatures: `admin_target()` / `agent_target()` return execution `Transport`s, and `secret(name)`
returns a resolved secret value. `ctx.secret(name)` raises a typed `ConfigError` if the context was
assembled without a resolve pass, and it is scoped: an op can read only the names its
`validate_config` declared.

What differs between stages is timing, not shape. `preflight` gets the command-start slice (existing
targets only, no resolved secrets, which is what makes it structurally dependency-blind); `runup`
and the ops get the op-start slice (current targets, resolved secrets). Central secret-resolvability
prediction happens above the platform, in the `vm-site` node that holds the instance
(`agentworks.vms.nodes`), which is why `VMPlatform.preflight`/`runup` never touch secret machinery
themselves.

**The pattern for a backend client:** memoize the _derived client_, never the raw secret. Proxmox's
`_api(ctx)` builds a `ProxmoxAPI` from `ctx.secret(token_secret)` on first need and caches the
client (`self._api_cached`), never the token. Any future platform with an API token (a hypothetical
GCP or AWS backend) should follow that shape.

## The provisioning timeline: create-time bootstrap vs. initialization

Standing up a VM splits into two stages with different owners and, crucially, different re-run
behavior. (These are a provisioning-timeline concept, orthogonal to the capability lifecycle stages
and the operator-facing command banners that the rest of the codebase calls "phases.")

- **Create-time bootstrap** is `create()` plus whatever the backend runs at creation time to get the
  VM reachable over Tailscale. It is baked into the backend's own create mechanism: Lima's
  `provision` block, Azure's and Proxmox's cloud-init user-data. The shared payload is
  `bootstrap_script.py` (admin user, packages, SSH key, swap, hostname, the Apple-vz SVE grub mask,
  Tailscale), delivered natively by Lima and via `cloud_init.py`'s `#cloud-config` wrapper by Azure.
  WSL2 is the exception: with no cloud-init-like mechanism, it runs the same bootstrap script over
  the provisioning transport during initialization instead, and structurally never joins Tailscale
  at create time (its `create()` does not branch on `tailscale_auth_key` at all). **This stage runs
  once, at create.**
- **Initialization** is `run_initialization` (`agentworks.vms.initializer`) plus VM hardening
  (`agentworks.vms.hardening`), run over a `Transport` against the created VM. It is
  platform-agnostic. **It is re-runnable and is exactly what `agw vm reinit` re-runs.** (The Phase A
  bootstrap/connectivity driver `bootstrap_vm` is provisioning, not this stage, and runs only at
  create.)

`request.tailscale_auth_key` is the seam control: when present, the platform joins Tailscale during
create-time bootstrap; when `None`, every platform defers the join to initialization.

The seam between the two stages is the source of the most important gotcha below.

## `reinit` reaches existing VMs; create-time provisioning does not

Because create-time bootstrap is baked into the backend's create mechanism, **a change to it reaches
new VMs only.** `agw vm reinit` (`manager.reinit_vm`) re-runs initialization (`run_initialization`
-> the platform-agnostic setup and hardening) over a transport; it does not call `platform.create()`
again or re-run the backend's create-time user-data. So decide deliberately where a fix belongs:

- **Must reach already-provisioned VMs?** Put it in initialization, as an idempotent reconciliation
  step. The models are `initializer._preserve_ssh_host_keys` and the sysctl / `hidepid` steps in
  `agentworks.vms.hardening` (ADR 0012): each is written at create and re-applied on `reinit`,
  content-diffed so a steady-state VM produces no change. Initialization is platform-agnostic, so
  weigh whether the fix is truly generic before putting it there.
- **Genuinely platform-specific, and new-VMs-only is acceptable?** Put it in the platform's
  create-time provisioning and remediate existing VMs out of band. The Lima `subuid` cap took this
  route on purpose (below).

A subtler instance of the same seam: `skel.py`'s shell rc content is seeded into the admin user's
home exactly once by `bootstrap_script.py` at create, but written to `/etc/skel` on every `reinit`
by `initializer._write_skel_seeds` so future `useradd -m` inherits it. Same content, two writers,
two different re-run behaviors, on purpose.

If you find yourself wanting a platform-specific fix to also reach existing VMs via `reinit`, that
is a signal the initializer may need a platform hook. None exists today; raise it rather than
smearing platform-specific logic into the shared initialization path.

## Things to keep in mind

### The backend is not a blank slate: watch what it injects

The single biggest surprise with a new platform is that the backend creates its own users, groups,
ID ranges, mounts, and network config before agentworks touches the VM, and those can collide with
assumptions agentworks makes. Do not assume a clean Debian image with nothing but your bootstrap on
it. When bringing up a platform, inventory what the backend injects and check it against what
agentworks needs (notably: agent-user creation allocates a subordinate uid/gid block per agent).

**Worked example (Lima `subuid` exhaustion).** Lima creates a guest user matching the host username
and, in its `rootless-base` boot script, grants that user a **1 GiB** (`1073741824`) subordinate
uid/gid range for `rootless` container tooling:

```sh
# Lima's boot.Linux/20-rootless-base.sh (abridged)
grep -qw "${LIMA_CIDATA_USER}" "$f" || echo "${LIMA_CIDATA_USER}:${subuid_begin}:1073741824" >>"$f"
```

That single entry starts at `524288` and runs past `login.defs`' `SUB_UID_MAX` (`600100000`),
swallowing essentially the entire allocatable space. agentworks creates each agent as its own Linux
user with a plain `useradd`, which auto-allocates a `65536` subordinate block; once Lima's giant
range has eaten the space, `useradd` can no longer find a free block and **agent creation fails**.
The symptom is far from the cause: a VM that provisioned fine simply stops being able to add agents
after a handful.

The fix agentworks ships caps any oversized range back to the standard `65536` in `lima.py`'s create
provision block (see the `subuid`-cap step in `LIMA_TEMPLATE`). The general lessons, which apply to
any future platform:

- Enumerate backend-injected users and ID ranges early. `cat /etc/subuid /etc/subgid` and
  `cat /etc/passwd` on a freshly created VM with no agents yet is a five-minute check that would
  have caught this at design time.
- A working first VM does not prove the platform is correct. This bug only appears after N agents.
- When you correct backend state, understand the backend's own re-run behavior so your fix sticks.
  Lima's `grep -qw <user>` guard means a corrected entry is not re-added on reboot; a different
  backend might stomp your correction on every start, which changes where the fix has to live.

### A create-time step that needs a reboot: the restart sentinel

Some bootstrap steps only take effect after a reboot, and rebooting mid-provision is unreliable.
Currently only the Apple-vz SVE grub mask needs this. The convention (`bootstrap_script.py`'s
`REBOOT_SENTINEL_PATH`) is that such a step drops a sentinel file on tmpfs; the platform's
`create()` probes for it after provisioning and restarts the instance once if present (Lima does
this). The probe stays why-agnostic: the host restarts on the sentinel without needing to know which
step set it, and the sentinel clears itself on the restart. If your backend has a step that only
lands after a reboot, reuse this convention rather than inventing a second one.

### No host file sharing by default

agentworks VMs are self-contained. Do not mount host directories into a guest unless there is a
concrete need: it is an attack surface and a portability trap. Lima defaults to sharing the host
home; `LIMA_TEMPLATE` sets `mounts: []` explicitly to guarantee none. Hold the same line on any new
platform, and prefer an explicit "no sharing" over relying on a backend default.

### Your own cleanup on failure is not the orchestrator's unwind

A platform's `create()` may build several backend resources before one fails. Clean up your own
partial work in a best-effort sweep and re-raise (Azure's `create()` wraps NIC / IP / NSG / VNet /
disk creation and calls `_cleanup_vm_resources` on any exception). That is distinct from, and
composes under, the orchestrator's DB-row unwind (ADR 0019's `RealizationLog` / node `teardown`),
which rolls back the persisted VM row. Keep the two separate in your head: your sweep undoes
backend-side resources you created inside `create()`; the orchestrator undoes the agentworks-side
record on top of it.

### Quoting and escaping when you embed scripts

Platforms embed shell into YAML or cloud-init, sometimes through several layers (Python `.format`,
YAML block scalar, remote shell). Two traps that have already occurred:

- `str.format` templates: any literal `{` / `}` in embedded shell (an `awk` program, a `${VAR}`)
  must be doubled to `{{` / `}}`, or `.format` will treat it as a field.
- Render and parse before trusting it: a quick `yaml.safe_load` of the rendered template in a test
  catches brace and indentation mistakes that are otherwise found only at provision time. See
  `cli/tests/vms/test_lima_template.py` for the pattern.

## Adding a new platform

1. Subclass `VMPlatform` and implement the ops. Every op except `display_backend_name` takes
   `ctx: RunContext`; read declared secrets via `ctx.secret(name)` and never hold a resolver or raw
   reader on the instance. If your backend has a persistent client, memoize the derived client, not
   the secret (the Proxmox `_api` pattern). Remember `create` is intentionally not `@idempotent_op`;
   the idempotent ops must land in-state themselves.
2. Implement `validate_config` to validate your `platform_config` and return a `ConfigReference` for
   each secret you read. Declaring a secret there is what authorizes the op to read it later.
3. Register the class in `VM_PLATFORM_REGISTRY` (`__init__.py`).
4. Set `unsupported_reason` if the platform cannot run on some hosts (WSL2 off Windows); implement
   `disabled_reason` for per-site tool checks (Lima with no `limactl`). Add
   `legacy_platform_metadata` only if there are pre-migration rows to map.
5. Override only the transport/lifecycle hooks your backend needs; accept the defaults otherwise.
6. Reuse `bootstrap_script.py` / `cloud_init.py` for the create-time payload rather than reinventing
   it.
7. Add dispatch, idempotency, and (if you embed a template) render tests under `cli/tests/vms/`; see
   the next section.
8. Walk the "things to keep in mind" gotchas against your backend before calling it done.

## Testing

The existing tests under `cli/tests/vms/` are the templates to copy from:

- `test_platform_validate_config.py`: table-driven `validate_config` shape across all platforms,
  plus a registry-name/class parity check. A good template for a new platform's registration test.
- `test_platform_support.py`: `unsupported_reason` (host-wide) vs. `disabled_reason` (per-site) vs.
  the composed `site_disabled_reason`. Uses the `stub_platform_support` fixture to pin platforms
  supported regardless of host, so dispatch-shape tests do not depend on local tooling.
- `test_platform_idempotency_guards.py`: patches `status` to an already-in-state value and asserts
  the backend verb is never called, proving the `@idempotent_op` contract.
- `test_platform_runup.py`: Proxmox's authenticated pre-check, distinguishing a definitive 401/403
  (fatal) from a transient error (warn and continue unverified). The template for any platform with
  a credential to verify.
- `test_create_vm_dispatch.py` and `test_create_reinit_orchestrated.py`: the `ProvisionRequest`
  shape handed to the platform, the persisted row, and the orchestrated create/reinit graph
  including the `RealizationLog` unwind and the activation gate.
- `test_lima_create_flow.py`: create-time provisioning wiring with mocked `limactl` and transport,
  the pattern for pinning your own create steps without a real VM.
- `test_lima_template.py`: `yaml.safe_load` over a rendered template with tripwires for the baked-in
  hardening rules (`mounts: []`, subuid cap present and first).

A new `Transport` subclass belongs under `cli/tests/transports/` alongside the platform.

## Cross-references

- [`../README.md`](../README.md): the capability lifecycle contract (read this first).
- `base.py`: the `VMPlatform` ABC, `ProvisionRequest`, `ProvisionResult`.
- `../base.py`: the `Capability` base and `RunContext`.
- `bootstrap_script.py`, `cloud_init.py`, `skel.py`: shared create-time payload.
- `agentworks.vms.initializer`: the two-phase init driver (`bootstrap_vm` for Phase A provisioning
  bootstrap/connectivity, `run_initialization` for Phase B initialization).
- `agentworks.vms.hardening`: the hardening steps, and the model for idempotent reconciliation that
  reaches existing VMs via `reinit`.
- `agentworks.vms.sites`: how a `vm-site` binds a platform to config.
- `agentworks.vms.nodes`: the `vm-site` / live-VM nodes that hold and drive a platform instance
  under the orchestration layer.
- `agentworks.transports`: the `Transport` ABC and the `native_transport` factory that wraps
  `transient_route`.
- `docs/guides/idempotency.md`: the canonical table of what `vm reinit` reconciles.
- ADR 0012: VM hardening at init.
- ADR 0016: the `vm-platform` capability / `vm-site` declarable split.
- ADR 0018: Azure VM size from spec.
- ADR 0019: the orchestration layer (command plans over node graphs) that drives the lifecycle.

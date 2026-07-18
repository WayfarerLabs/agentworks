# Developing a VM platform

> Practical guidance for authors of `vm-platform` capabilities. This is the platform-kind companion
> to the capability contract in [`../README.md`](../README.md): that doc defines the lifecycle every
> capability obeys (`validate_config`, preflight, runup, ops); this one covers what is specific to
> running VMs, plus the gotchas that have already bitten real platforms.

**Status: placeholder.** A larger refactor is in flight on another branch, so the structural
sections below (the surface tour, the new-platform checklist) are deliberately thin and marked
_TODO_ until that lands. The "things to keep in mind" section is durable regardless of the refactor
and is worth reading now.

## What a VM platform is

A VM platform is the code that runs VMs on one backend kind: `lima`, `wsl2`, `azure-vm`, `proxmox`.
Each subclasses `VMPlatform` (`base.py`), registers in `VM_PLATFORM_REGISTRY` (`__init__.py`), and
publishes as a read-only `vm-platform` capability resource. Operators never invoke a platform
directly: a declarable `vm-site` binds a platform to config, and all invocation goes through site
resolution (`agentworks.vms.sites`). See ADR 0016 for the capability/declarable split.

The surface you implement (see `base.py` for the authoritative contract):

- **ops**: `create`, `start`, `stop`, `delete`, `status`, `display_backend_name`. `start` / `stop` /
  `delete` are flagged `@idempotent_op` and must land in the same place run twice as run once
  (`reinit` re-applies everything and failed commands are retried). `create` is deliberately
  one-shot: its collision check makes a re-run a loud error, not a silent second VM.
- **transport hooks**: `native_transport`, `transient_route`, `vm_active`, `post_tailscale_ready`.
  Each has a sensible default; override only what your backend needs (e.g. `azure-vm` attaches a
  public IP in `transient_route`; `wsl2` anchors the distro against idle-shutdown in `vm_active`).
- **gates**: `unsupported_reason` (class-level, "could any config ever work on this host": `wsl2`
  off Windows) versus `disabled_reason` (instance-level, "is this configured site ready": a local
  `lima` site with no `limactl`). These are cheap, offline, and distinct from preflight.

Inputs and outputs are uniform: every `create` receives the same `ProvisionRequest` and returns a
`ProvisionResult` whose `platform_metadata` is written verbatim to `vms.platform_metadata` and read
back only by the owning platform. Add a platform-specific input by adding a field to
`ProvisionRequest`, not by changing the protocol.

## The two-phase provisioning model

Standing up a VM splits into two phases with different owners and, importantly, different re-run
behavior:

- **Phase A (platform bootstrap):** `create()` plus whatever the backend runs at creation time to
  get the VM to the point agentworks can reach it over Tailscale. This is baked into the backend's
  own create mechanism: `lima`'s `provision` block, `azure-vm`'s cloud-init user-data, and so on.
  The shared bootstrap script (`bootstrap_script.py`: admin user, packages, SSH key, Tailscale) is
  the common payload. **Phase A runs once, at create.**
- **Phase B (initialization, platform-agnostic):** `initialize_vm` plus VM hardening
  (`agentworks.vms.hardening`), run over a `Transport` against the created VM. **Phase B is
  re-runnable and is exactly what `agw vm reinit` re-runs.**

The seam between them is the source of the most important gotcha below.

## Things to keep in mind

### The backend is not a blank slate: watch what it injects

The single biggest surprise with a new platform is that the backend creates its own users, groups,
ID ranges, mounts, and network config before agentworks touches the VM, and those can collide with
assumptions agentworks makes. Do not assume a clean Debian image with nothing but your bootstrap on
it. When bringing up a platform, inventory what the backend injects, and check it against what
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
provision block (see the `mounts: []` and `subuid`-cap steps in `LIMA_TEMPLATE`). The general
lessons, which apply to any future platform:

- Enumerate backend-injected users and ID ranges early. `cat /etc/subuid /etc/subgid` and
  `cat /etc/passwd` on a freshly created VM with no agents yet is a five-minute check that would
  have caught this at design time.
- A working first VM does not prove the platform is correct. This bug only appears after N agents.
- When you correct backend state, understand the backend's own re-run behavior so your fix sticks.
  Lima's `grep -qw <user>` guard means a corrected entry is not re-added on reboot; a different
  backend might stomp your correction on every start, which changes where the fix has to live.

### `reinit` reaches existing VMs; create-time provisioning does not

Because Phase A is baked into the backend's create mechanism, **a change to create-time provisioning
reaches new VMs only.** `agw vm reinit` re-runs Phase B (platform-agnostic initialization and
hardening) over a transport; it does not re-run the backend's create-time provisioning (the Lima
`provision` block, the cloud-init user-data). So decide deliberately where a fix belongs:

- **Must reach already-provisioned VMs?** Put it in Phase B, as an idempotent reconciliation step
  (the SSH-host-key drop-in and the sysctl/`hidepid` hardening are the models: written at create and
  re-applied on `reinit`). Phase B is platform-agnostic, so weigh whether the fix is truly generic.
- **Genuinely platform-specific, and new-VMs-only is acceptable?** Put it in the platform's
  create-time provisioning and remediate existing VMs out of band. The Lima `subuid` cap took this
  route on purpose: it is Lima-specific, so it lives in `LIMA_TEMPLATE`, and the handful of existing
  VMs were corrected manually rather than pushing the concern into platform-agnostic Phase B.

If you find yourself wanting a platform-specific fix to also reach existing VMs via `reinit`, that
is a signal the initializer may need a platform hook. None exists today; raise it rather than
smearing platform-specific logic into the shared bootstrap.

### No host file sharing by default

agentworks VMs are self-contained. Do not mount host directories into a guest unless there is a
concrete need: it is an attack surface and a portability trap. Lima defaults to sharing the host
home; `LIMA_TEMPLATE` sets `mounts: []` explicitly to guarantee none. Hold the same line on any new
platform, and prefer an explicit "no sharing" over relying on a backend default.

### Quoting and escaping when you embed scripts

Platforms embed shell into YAML or cloud-init, sometimes through several layers (Python `.format`,
YAML block scalar, remote shell). Two traps that have already occurred:

- `str.format` templates: any literal `{` / `}` in embedded shell (an `awk` program, a `${VAR}`)
  must be doubled to `{{` / `}}`, or `.format` will treat it as a field.
- Render and parse before trusting it: a quick `yaml.safe_load` of the rendered template in a test
  catches brace and indentation mistakes that are otherwise found only at provision time. See
  `cli/tests/vms/test_lima_template.py` for the pattern.

### TODO

- Native transport vs console fallback expectations per platform.
- Idle-shutdown / cost-control conventions (`vm_active`).
- Tailscale bootstrap timing and the `tailscale_auth_key`-absent deferral path.
- Testing conventions for platforms (dispatch tests, idempotency guards).

## Adding a new platform (skeleton)

_TODO: expand after the in-flight refactor settles the registration surface._ The rough shape today:

1. Subclass `VMPlatform`, implement the ops, override only the hooks your backend needs.
2. Register in `VM_PLATFORM_REGISTRY` (`__init__.py`).
3. Set `unsupported_reason` if the platform cannot run on some hosts; implement `disabled_reason`
   for per-site tool checks.
4. Reuse `bootstrap_script.py` / `cloud_init.py` for Phase A payload rather than reinventing it.
5. Add dispatch and idempotency tests under `cli/tests/vms/`.
6. Walk the "things to keep in mind" gotchas against your backend before calling it done.

## Cross-references

- [`../README.md`](../README.md): the capability lifecycle contract (read this first).
- `base.py`: the `VMPlatform` ABC, `ProvisionRequest`, `ProvisionResult`.
- `bootstrap_script.py`, `cloud_init.py`, `skel.py`: shared Phase A payload.
- `agentworks.vms.hardening`: the Phase B hardening steps, and the model for idempotent
  reconciliation that reaches existing VMs via `reinit`.
- ADR 0016: the `vm-platform` capability / `vm-site` declarable split.

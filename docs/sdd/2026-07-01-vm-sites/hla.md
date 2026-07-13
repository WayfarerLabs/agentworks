# VM sites and platforms: high-level architecture

> **Design revision (2026-07-12): capability model adoption.** `capability-model.md` (in this SDD)
> now owns the capability lifecycle contract, and Phase 7 of the plan adopts it. Where this document
> disagrees with it, the capability model wins. The concrete supersessions:
>
> - **"The VMPlatform protocol"**: instances are constructed as
>   `cls(site_name, validated_platform_config, resolver)`, not with resolved `secret_values`.
>   Construction re-runs `validate_config` and stays cheap (no network, no resolution, no prompt).
>   `VMPlatform` extends the instance-scoped `Capability` base and gains `preflight` (read-only,
>   best-effort); its mutating ops carry per-op idempotency flags.
> - **"Dispatch and site secrets"**: `resolve_site` / `platform_for` lose the `secret_values`
>   parameter, and the canonical composition-root ordering becomes: (1) config + registry; (2) VM
>   row + site declaration; (3) bind (construct with the resolver -- no resolution); (4) preflight
>   every participating resource, the vm-template (predicts its Tailscale key resolves; the key is
>   the template's responsibility, not the site's) and the platform instance, in either order; (5)
>   the one resolve pass, covering the union of secrets needed across all planned ops across all
>   participating resources; (6) ops, drawing values from the resolver's cache. Resolution never
>   happens at command entry and is never deferred to an op's first need. Gates still take the bound
>   platform and never bind or resolve.
> - **"File layout"**: the platform implementations move from `agentworks/vms/platforms/` to
>   `agentworks/capabilities/vm_platform/`, under the `Capability` base at the top of
>   `agentworks/capabilities/`.
> - Doctor's per-site health rows call the instance's `preflight` (read-only by contract).

## Model overview

ADR 0016's two-layer model (config / resources, with capabilities as read-only resources),
instantiated for VMs under the 2026-07-08 naming ruling:

```text
capability (code + registry row)   declarable resource (registry)          vm (DB row)
VM_PLATFORM_REGISTRY               kind vm-site                            vms.site
kind vm-platform                   ------------------------------------    ------------
--------------------
LimaPlatform             <---      vm-site/lima     (built-in, bundled)   <--- "lima"
                         <---      vm-site/gpu-box  (operator manifest)   <--- "gpu-box"
AzurePlatform            <---      vm-site/azure-dev (operator manifest)  <--- "azure-dev"
                         <---      vm-site/azure    (legacy [azure] TOML) <--- "azure"
WSL2Platform             <---      vm-site/wsl2     (built-in, bundled)   <--- "wsl2"
ProxmoxPlatform          <---      vm-site/proxmox  (legacy [proxmox])    <--- "proxmox"
```

`vms.site` (renamed from `vms.platform`; for every non-remote-Lima row the stored value is already
the right site name) stores the site name. Site rows arrive through the resource-manifests
publishers (built-in bundle, operator manifests, legacy TOML); dispatch resolves a site name to its
row, the row's `platform` field to the capability, and returns the platform instance bound to the
site's validated `platform_config`. Everything outside the site layer works with sites;
`VM_PLATFORM_REGISTRY` and the platform classes stay private to it.

Each registered platform also enters the registry as a read-only `vm-platform` capability resource
(origin `built-in`, error miss policy, category `capability`), so `spec.platform` references
validate through the framework like every other edge, `agw resource kinds` lists the kind, and
`agw resource describe vm-platform/azure` lists the sites referencing it.

## The VMPlatform protocol

Lives in `agentworks/vms/base.py`. The full surface after this SDD:

```python
# agentworks/vms/base.py

@dataclass
class ProvisionRequest:
    """All inputs a platform might need to create a VM.

    Every platform receives the same request shape; each ignores fields
    it doesn't use. Adding a platform-specific input means adding a
    field here, not changing the protocol. Units match the rest of the
    codebase (GiB), so no conversion seam.
    """
    vm_name: str
    hostname: str                     # R11 scheme, computed by the manager
    system_slug: str | None
    admin_username: str
    ssh_public_key: str
    ssh_private_key: Path | None      # azure/proxmox build their native SSH
                                      # transports during create() and no
                                      # longer receive Config
    tailscale_auth_key: str | None    # None: bootstrap deferred to Phase A
    cpus: int | None = None
    memory_gib: int | None = None
    disk_gib: int | None = None
    swap_gib: int | None = None
    azure_vm_size: str | None = None
    # ... future fields (aws_instance_type, ...) land here.


@dataclass
class ProvisionResult:
    """What a platform returns from create().

    ``platform_metadata`` is the opaque dict written verbatim to
    ``vms.platform_metadata``; the owning platform is its only reader.
    """
    native_transport: Transport
    platform_metadata: dict[str, str]
    bootstrap_complete: bool = False
    tailscale_ip: str | None = None


class VMPlatform(ABC):
    """Capability: the code that runs VMs on one backend kind.

    Registered in VM_PLATFORM_REGISTRY and published as a read-only
    vm-platform capability resource; invoked only through site
    resolution. Concrete platforms declare class-level attributes
    consumed by the vm-site kind decoder, the capability publisher, and
    migration:

    - ``name``: platform discriminator ("lima", "azure", ...).
    - ``validate_config(owner, config)``: the shipped invoked-validation
      API (same classmethod shape and semantics as
      GitCredentialProvider.validate_config, including the note that it
      may be deprecated for registration-time schemas). Validates the
      platform_config blob (unknown keys and missing required keys
      raise ConfigError framed with ``owner``) and returns the
      ConfigReference tuple the blob implies (e.g. proxmox returns a
      secret reference for ``token_secret``). Platforms with no
      required keys back a bundled built-in site named after
      themselves.
    - ~~``shared_backend(platform_config) -> bool``~~ REMOVED with the
      R4 nudge (2026-07-13 ruling: a blank slug answer is final); the
      nudge was its only consumer.
    - ``legacy_platform_metadata(row, legacy) -> dict[str, str]``: pure
      migration hook over the legacy row plus the best-effort parse of
      the legacy TOML sections; maps legacy column values to this
      platform's metadata conventions (see Migration).

    Instances are constructed only by the site layer, as
    ``cls(site_name, validated_platform_config, secret_values)`` (the
    last carries resolved values for any secret-name fields the blob
    declares; empty for secret-free platforms).
    """

    @abstractmethod
    def create(self, request: ProvisionRequest) -> ProvisionResult:
        """Create the backend-side VM.

        Responsibilities:
        - Construct a backend-side name, using ``request.system_slug``
          as the namespacing token when set (else ``request.vm_name``).
        - Pre-flight collision check (R9): auto-suffix and retry, or
          raise StateError with guidance (per-platform policy).
        - Create the resource(s).
        - Return ProvisionResult with platform_metadata capturing
          whatever identifiers subsequent ops need, without relying on
          live configuration (e.g. proxmox records node alongside
          vmid).
        """

    @abstractmethod
    def start(self, vm: VMRow) -> None:
        """Start a stopped VM. Reads vm.platform_metadata to identify."""

    @abstractmethod
    def stop(self, vm: VMRow) -> None:
        """Stop a running VM. Reads vm.platform_metadata."""

    @abstractmethod
    def delete(self, vm: VMRow) -> None:
        """Delete a VM and clean up backend resources.
        Reads vm.platform_metadata."""

    @abstractmethod
    def status(self, vm: VMRow) -> VMStatus:
        """Query live observed status. Reads vm.platform_metadata."""

    @abstractmethod
    def display_backend_name(self, vm: VMRow) -> str:
        """Short human-readable identifier for the backend-side
        resource, for ``agw vm describe`` and error messages (Azure
        returns the VM-name portion of the resource ID; WSL2 the
        distro name; Proxmox "vmid@node")."""

    def native_transport(
        self, vm: VMRow, *, config: Config | None = None
    ) -> Transport | None:
        """Platform-native transport for bootstrap and
        ``vm shell --platform``. Default None (opt-out). Proxmox
        returns None; the transports factory raises StateError with
        the web-console hint on None."""
        return None

    def post_tailscale_ready(self, vm: VMRow) -> None:
        """No-op default; Azure overrides to detach the public IP once
        Tailscale is reachable. Unchanged from today."""

    def transient_route(self, vm: VMRow) -> AbstractContextManager[None]:
        """No-op default; Azure overrides to attach a public IP on
        enter, detach on exit. Unchanged from today."""
        return nullcontext()

    def vm_active(
        self, vm: VMRow, *, config: Config | None = None
    ) -> AbstractContextManager[None]:
        """Hold the VM against the backend's idle-shutdown for the
        context's duration. Callers gate with ensure_active first.
        WSL2 overrides; others default to nullcontext."""
        return nullcontext()
```

`native_transport()` (was `provisioner_transport()`) moves from `@abstractmethod` to
concrete-with-`None`-default. Lima, WSL2, and Azure override; Proxmox (and any future SSM-only AWS
shape) does nothing. The `transports/__init__.py` factory replaces its platform-name branch with a
`None` check and is itself renamed `native_transport` (R1: the noun retires from its name too).

The `config: Config | None` parameter on `native_transport()` and `vm_active()` survives the move to
bound `platform_config` deliberately: it carries OPERATOR settings, not site configuration (Azure's
public-IP path needs `config.operator.ssh_private_key`; WSL2's reconnect wait builds the Tailscale
transport from operator config). The two kinds of configuration stay separate.

## The vm-site and vm-platform kinds

Both kinds register from the VM domain (`vms/kinds.py`, alongside the existing vm-template kind, per
the domains-own-their-kinds ruling):

- **`vm-platform`**: the capability kind. `category = "capability"` (manifest documents get the
  provided-by-the-app error), error miss policy, rows published by the platform registry's publisher
  with `Origin.built_in(source="agentworks.vms")`. Kind description (for `agw resource kinds`): "VM
  backend implementations (code)".
- **`vm-site`**: the declarable kind. `category = "declarable"`, `builtin_override = "reserved"`
  (the bundled `lima` / `wsl2` names get the declare-a-sibling error; this repopulates the reserved
  tier the capability collapse left memberless), error miss policy (a typo'd site reference must
  never auto-declare a site). Decode takes the uniform reference + blob envelope: `spec.platform`
  (required; becomes a `ResourceReference` to `vm-platform/<name>`) and `spec.platform_config`
  (optional mapping, default empty; keys may not shadow top-level spec keys). When the platform is
  registered, its `validate_config` runs at decode (raising with the document's `file:line`;
  returning the implied `ConfigReference`s the site attaches with itself as source); when it is not,
  decode defers so the unknown platform reports uniformly through the reference miss policy at
  finalize.

`VMSiteDecl` (the Resource dataclass) carries `name`, `description`, `platform`, `platform_config`
(nested, matching the git-credential internal shape), `declared_at` / `origin` / `references` (the
platform edge plus any secret references from `validate_config`).

Site rows arrive from three publishers:

1. **Built-in bundle**: `agentworks/manifests/builtin/vm-sites.yaml` declares the `lima` and `wsl2`
   sites (platform matching the name, empty `platform_config`), origin `built-in`. This is the
   bundle mechanism's first real content (its original content, the bundled secret backends, died in
   the capability collapse).
2. **Operator manifests**: the standard loader; nothing site-specific.
3. **Legacy TOML** (dual-path): the legacy loader lives in `config.py`, per ADR 0016's ruling that
   config.py holds "settings and the legacy TOML resource loaders/publisher, nothing else" (the
   shipped git-credential legacy loader is the in-file precedent). It reads `[azure]` / `[proxmox]`
   and publishes `vm-site/azure` / `vm-site/proxmox` rows (operator-declared origin with the TOML
   `file:line`). Flat TOML is the one home where platform-owned fields sit outside the blob; the
   loader nests them into `platform_config` at its boundary, exactly as the git-credential TOML
   loader does. The sections join the aggregated deprecation warning, and the migrator's
   kind-to-section mapping gains the entries so `agw resource migrate vm-site/azure` (or `--all`)
   moves them to manifests with the standard verification. (Small shape change: `KIND_SECTIONS` is
   one-section-per-kind today; vm-site maps to two sections whose section name becomes the resource
   name, so the mapping type and migrator grow a multi-section case.)

Consumers reference sites by bare name (resource-to-resource references): `vm-template` gains an
optional `site` field whose reference edge validates at finalize, and selection precedence is CLI
flag, template, `defaults.site`, then the built-in `lima`. Settings that name sites are validated at
the composition boundary, mirroring `secrets.validate_chain`: `vms.validate_sites(config, registry)`
runs in `build_registry` and checks that `defaults.site` (or its deprecated `defaults.platform`
alias), when set, resolves (config vocabulary in the error). DB references (`vms.site`) are checked
by `agw doctor` and at dispatch, not at registry build (lifecycle entities are not resources).

## Dispatch and site secrets

`agentworks/vms/sites.py` owns dispatch; `VM_PLATFORM_REGISTRY` lives in
`agentworks/vms/platforms/__init__.py` and is imported only here (and by `vms/kinds.py` for decode
delegation):

```python
# agentworks/vms/sites.py

def resolve_site(
    name: str, registry: Registry, *, secret_values: Mapping[str, str] | None = None
) -> VMPlatform:
    """Resolve a site name to its bound platform.

    Returns the platform class instantiated with the site's validated
    platform_config (and resolved values for any config secrets).
    Manager code holds the bound platform and never sees
    VM_PLATFORM_REGISTRY or platform classes.
    """
    try:
        decl = registry.lookup("vm-site", name)
    except KeyError:
        # registry.lookup raises on a miss; this is the R3 stranded-VM
        # path (e.g. a migrated remote-Lima row whose site manifest the
        # operator has not added yet).
        raise ConfigError(
            f"site '{name}' is not declared",
            hint=_site_manifest_hint(name),   # ready-to-paste YAML
        ) from None
    cls = VM_PLATFORM_REGISTRY[decl.platform]   # edge validated at finalize
    return cls(decl.name, decl.platform_config, _config_secrets(decl, secret_values))

def platform_for(vm: VMRow, registry: Registry, **kw) -> VMPlatform:
    """The bound platform for a VM, resolved through its site."""
    return resolve_site(vm.site, registry, **kw)
```

No name-based branches anywhere: the lookup-table-of-constructor-irregularities from the earlier
draft is gone entirely, because `platform_config` made every constructor the same shape.

**Site config secrets** follow the capability-consumers rules end to end, with Proxmox's API token
as the first real user:

- `ProxmoxPlatform.validate_config` declares `token_secret` as a secret-name field defaulting to the
  well-known name `proxmox-token` and returns the corresponding `ConfigReference`; the site is the
  reference source, so auto-declaration synthesizes the secret row
  (`(auto) the API token for vm-site/proxmox`), reachability validates at finalize, and doctor
  predicts resolution.
- Values resolve at the consuming command's composition root through the standard single resolve
  pass, and the platform is bound ONCE there, before any gate or lifecycle call (a Proxmox
  platform's very first use, `status()` inside ensure-active, already needs the token). The
  canonical composition-root ordering for a VM-touching command:
  1. `load_config()`; `build_registry(config)` (runs `validate_sites`).
  2. Read the VM row; resolve its site declaration.
  3. Add the site's secret declarations to the command's resolve set
     (`compute_needed_secrets(..., extra_decls=...)`, the tailscale-key and git-token precedent) and
     run the single resolve pass.
  4. `platform = platform_for(vm, registry, secret_values=values)`: the bound platform.
  5. Thread the bound platform down; `keep_active` / `ensure_active` take it as a parameter and
     never resolve or bind anything themselves. The registry never resolves values.

- The raw `PROXMOX_TOKEN_SECRET` env read in `ProxmoxProvisioner.__init__` (RuntimeError when
  absent) is deleted. Default resolution reads `AW_SECRET_PROXMOX_TOKEN` via the `env-var` backend
  or falls back to the prompt; a one-line `backend_mappings: {env-var: PROXMOX_TOKEN_SECRET}` on the
  secret preserves the old variable name (R13).
- Azure, Lima, and WSL2 declare no config secrets today; their `validate_config` returns no
  references, and their dispatch path never touches the resolve pass. AWS later rides the same rails
  for client secrets.

## Lima: one platform, remote-ness as platform_config

```python
# agentworks/vms/platforms/lima.py

@register
class LimaPlatform(VMPlatform):
    name = "lima"

    # validate_config accepts one optional key:
    #   vm_host: str | None    # SSH host; absent = local limactl
    ...
```

The `is_remote` property remains but is now derived from declared site configuration rather than
smeared across a table, a constructor arg, and a branch. `native_transport()` returns
`LimaTransport` or `RemoteLimaTransport` off the same key; that single branch point is the honest
encoding of a real capability difference and stays.

~~`shared_backend` is computed from `vm_host` presence~~ -- REMOVED with the R4 nudge (2026-07-13
ruling; see the plan's sequencing note).

## Ensure-active + vm_active in the manager

```python
# agentworks/vms/manager.py

def ensure_active(db: Database, config: Config, vm: VMRow,
                  platform: VMPlatform) -> None:
    """Respect an operator stop; otherwise start on demand.

    Fast path: a Tailscale reachability probe (cheap, no cloud API)
    short-circuits the common case. Keeps ARM round trips off the
    per-op hot path that today wraps a no-op keep_vm_active.
    """
    if vm.tailscale_host and _is_tailscale_reachable(vm.tailscale_host):
        return
    observed = platform.status(vm)
    if observed in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        if vm.operator_stopped:
            raise StateError(
                f"VM '{vm.name}' is stopped",
                hint=f"start it with: agw vm start {vm.name}",
            )
        output.info(f"VM '{vm.name}' is {observed.value}. Starting...")
        platform.start(vm)
        # Hold while tailscaled reattaches: a freshly booted WSL2
        # distro must not idle out during the handshake wait.
        with platform.vm_active(vm, config=config):
            _ensure_tailscale(db, config, vm)
    # RUNNING or UNKNOWN: proceed. A transient status failure must not
    # trigger a spurious start; the op will surface the real error.


@contextlib.contextmanager
def keep_active(db: Database, config: Config, vm: VMRow,
                platform: VMPlatform) -> Iterator[None]:
    """Gate (ensure_active), then hold (vm_active). Replaces
    keep_vm_active; keep_actives replaces keep_vms_active.

    Takes the BOUND platform from the composition root (see Dispatch
    and site secrets): binding may need resolved config secrets, which
    only the composition root's single resolve pass has.
    """
    ensure_active(db, config, vm, platform)
    with platform.vm_active(vm, config=config):
        yield
```

- Site resolution requires the registry and (for secret-bearing sites) resolved values, so every
  VM-touching command's composition root builds the registry, resolves, and binds the platform per
  the ordering in "Dispatch and site secrets", then threads the bound platform down. Most command
  entry points already call `build_registry` today (sessions, agents, workspaces managers, and
  several vms/manager sites); the actual delta is `start`/`stop`/`shell` plus the
  `_ensure_vm_running` absorption points, not a wholesale introduction.
- `ensure_active` absorbs `workspaces.manager._ensure_vm_running`, which sessions/console.py,
  agents/manager.py, and multi_console.py import privately today; those imports move to the public
  manager function.
- `start_vm` keeps its explicit probe-start-verify shape but drops its WSL2-ordering comment
  workarounds; `stop_vm`, the `describe_*` family, and the multi-console best-effort ops keep their
  documented carve-outs (no gate, no hold).
- WSL2's `vm_active` keeps today's `_keepalive` attach semantics unchanged (including
  boot-on-attach, which the gate makes safe by construction and which self-heals the idle-timer race
  between gate and attach). Its read paths switch from `vm.name` to
  `platform_metadata['distro_name']`.

## Settings table and system slug

```sql
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

Keys used in this SDD:

- `system_slug`: the slug, or empty for explicitly-declined (distinguishes "never asked" from
  "asked, operator skipped" so the prompt fires once and the blank answer is final).
- ~~`shared_backend_nudge_suppressed`~~ REMOVED with the R4 nudge (2026-07-13 ruling).

The slug is read from the DB at the points that need it: `create_vm` (building the
`ProvisionRequest` and the R11 hostname), `sync_ssh_config` (R10 file name), and the R4 prompts. It
is deliberately not loaded into `Config`, which stays purely config.toml-derived. (Install-level
state in the DB is also not a resource; the settings table is config-side state per ADR 0016's
config-is-config ruling.)

## Migration

The `MIGRATIONS` dict in `db.py` gains support for Python steps: a version maps to either a SQL
string (as today) or a callable receiving the connection plus a migration context. The context
carries `legacy`: a best-effort, unvalidated parse of the config file's legacy TOML sections.
Migrations run at `Database()` open, where no validated `Config` exists and the config file may be
missing or broken, so the parse is tolerant (missing or unreadable config yields an empty mapping)
and nothing in a migration may depend on it succeeding. This SDD ships one Python migration version
that performs, in order:

1. `ALTER TABLE vms ADD COLUMN platform_metadata TEXT NOT NULL DEFAULT '{}'`, plus
   `operator_stopped INTEGER NOT NULL DEFAULT 0` (bool; the `agents.grant_all` 0/1 precedent) and
   `hostname TEXT` (nullable only during this step; step 2 backfills every row and the step-4
   rebuild declares it `NOT NULL`, since every create writes it).
2. Per-row backfill via the owning platform's pure hook, keyed off the legacy platform value:

   ```python
   # Illustrative hooks; each lives on its platform class.
   LimaPlatform.legacy_platform_metadata     # {'instance_name': row['name']}
   WSL2Platform.legacy_platform_metadata     # {'distro_name': row['wsl_distro_name'] or row['name']}
   AzurePlatform.legacy_platform_metadata    # {'resource_id': row['azure_resource_id']}
   ProxmoxPlatform.legacy_platform_metadata  # {'vmid': row['proxmox_vmid'],
                                             #  'node': legacy['proxmox']['node'] if present}
   ```

   Keys with nothing to record are omitted (never empty strings). Proxmox's node comes from the
   context's `legacy['proxmox']` parse when present, the same value ops would have used; recording
   it decouples existing VMs from future config edits. When the section is absent at migration time,
   the `node` key is simply omitted, and `ProxmoxPlatform` ops fall back to the bound site's
   `platform_config` node whenever the metadata key is missing, opportunistically writing it back on
   the first successful op (the fallback also covers this SDD's own transition window). `hostname`
   backfills as `{platform}--{name}` (the value the bootstrap script actually set).

3. Site rename: `vms.platform` becomes `vms.site`. For every non-remote-Lima row the stored value is
   already the right site name (`lima` and `wsl2` are the bundled sites; `azure` and `proxmox` the
   legacy TOML sites). Remote-Lima rows (`vm_host_name` set) get `site = vm_host_name` instead, and
   the step collects the referenced `vm_hosts` rows and prints ready-to-paste `vm-site` manifest
   documents once at the end:

   ```yaml
   # Save under ~/.config/agentworks/resources/ (any filename):
   apiVersion: agentworks/v1
   kind: vm-site
   metadata:
     name: gpu-box
   spec:
     platform: lima
     platform_config:
       vm_host: scot@gpu-box
   ```

   If a vm-host name collides with a reserved built-in site name, the migration suffixes it
   (`<name>-host`) and says so.

4. Drop `azure_resource_id`, `wsl_distro_name`, `proxmox_vmid`, `vm_host_name`; drop `vm_hosts`.
   `vm_host_name` participates in a foreign key, which SQLite's `DROP COLUMN` refuses, so the `vms`
   table is rebuilt via the standard create-copy-rename dance inside the same step; the rebuild also
   carries the `platform` to `site` column rename. (`DROP COLUMN` elsewhere needs SQLite 3.35+,
   which the supported Pythons bundle; the rebuild sidesteps the question for `vms`.)
5. `CREATE TABLE settings (...)`.

`VMRow` changes: `site: str` (renamed from `platform`), `platform_metadata: dict[str, str]`
(JSON-parsed by the row loader), `operator_stopped: bool`, `hostname: str`; `azure_resource_id`,
`wsl_distro_name`, `proxmox_vmid`, `vm_host_name` removed. The `insert_vm` / `update_vm_*` helpers
follow.

## SSH config

Config.d mode (default):

- Slug set: managed file becomes `~/.ssh/config.d/agentworks-{system_slug}.conf`; the first sync
  after the slug is set removes the old `agentworks.conf`.
- Slug null: `agentworks.conf`, unchanged.
- The existing `Include ~/.ssh/config.d/*` directive (ensured on every sync today) already covers
  both names; no directive change.
- Host aliases keep `vm.name`; the file regenerates wholesale from DB state on every sync, so a
  hypothetical future slug change needs no rename step. Cross-install alias collisions are
  documented with `operator.ssh_host_prefix` as the remedy (R10).

Legacy mode (`ssh_config_dir = false`): untouched, documented as single-install.

## Hostname and Tailscale naming

`bootstrap_script.vm_hostname(platform, vm_name)` and its callers (lima.py, azure.py, proxmox.py,
initializer Phase A) are replaced by `request.hostname` / `vm.hostname`:

- The manager computes the hostname once at create time: `{slug}-{vm.name}` with a slug, `{vm.name}`
  without. Platforms pass it to their bootstrap paths; the manager records it in `vms.hostname`.
- `vm reinit` (initializer Phase A) reads `vm.hostname` instead of re-deriving, so existing VMs keep
  their `{platform}--{name}` hostnames across reinit.
- tailscaled derives the node name from the OS hostname as today; `vm.tailscale_host` remains the
  Tailscale IP and nothing re-derives node names downstream.

## Identity env

`env/identity.py` `ResourceContext` changes: `vm_host` field removed; `site` field added.
`vm_stable_identity_env` emits:

- `AGENTWORKS_VM` (unchanged)
- `AGENTWORKS_PLATFORM`: the capability name, which is what it has always contained; no existing
  VM's value changes
- `AGENTWORKS_SITE`: the site name (new)
- `AGENTWORKS_VM_HOST`: no longer emitted

The row stores only `vms.site`, so the platform name is resolved at the init/reinit composition root
via the site declaration (the same lookup that binds the platform). A stranded remote-Lima VM
therefore fails reinit at site resolution with the R3 `ConfigError` and snippet, before any env
baking. On-VM fragments refresh at reinit, unchanged mechanics. The `AGENTWORKS_*` inventory's
permanent home (the operator docs, not the locked env-and-secrets SDD, whose artifacts are not
edited post-lock) gets the addition/retirement.

## CLI, completions, resources

- `agw vm create --site` (replacing `--platform`): value validated against declared sites at
  dispatch; the static `click.Choice(["lima", "azure", "wsl2", "proxmox"])` is removed and a dynamic
  completer (`("vm.create", "site")` mapping to a `sites` completer sourced from
  `agw resource list --kind vm-site --names-only`, splitting the `vm-site/<name>` lines the same way
  the existing `resource_names` completer does) is added to `DYNAMIC_COMPLETIONS`.
- `agw resource migrate` selector completion picks up `vm-site` / `vm-site/<name>` through the
  existing cross-product completer once `decode.KIND_SECTIONS` gains the legacy-section mapping;
  `agw resource sample vm-site` ships bundled, loader-verified, config-bearing sample documents;
  `agw resource edit vm-site/<name>` works for manifest-declared rows; `agw resource kinds` shows
  both new kinds with categories and descriptions.
- `--vm-host` and the `("vm.create", "vm_host")` / `("vm-host.remove", "name")` completion entries
  are removed along with the `agw vm-host` group.
- `agw vm shell --platform` (boolean: platform-native transport; hidden alias `--provisioner`).
- `agw doctor` gains: every `vm.site` resolves to a `vm-site` row; slug shown; stranded remote-Lima
  VMs reported with their paste-ready manifest snippet. The proxmox token secret shows up in the
  existing per-secret doctor rows automatically (auto-declared resource).
- `sample-config.toml` drops the `[azure]` / `[proxmox]` examples in favor of a pointer at
  `agw resource sample vm-site` (the sections keep loading as deprecated legacy declarations per the
  dual-path convention); `defaults.platform` is documented as the deprecated alias of
  `defaults.site`.
- In passing: `proxmox.py`'s operator-facing error that embeds a `docs/sdd/` path (flagged in the
  resource-manifests lockfile as a tombstoning-time follow-up) is rewritten while this SDD touches
  the file.

## File layout

```text
cli/agentworks/vms/
    base.py                      # VMPlatform protocol + ProvisionRequest + ProvisionResult
    kinds.py                     # + vm-site and vm-platform kind registrations
                                 #   (joins the existing vm-template kind)
    sites.py                     # NEW: dispatch (resolve_site, platform_for),
                                 #   validate_sites, config-secret threading,
                                 #   snippet hints
cli/agentworks/config.py         # + legacy [azure]/[proxmox] vm-site loader/publisher
                                 #   (per ADR 0016: settings + legacy TOML loaders)
    manager.py                   # + ensure_active, keep_active, keep_actives
    initializer.py               # reads platform_metadata + vm.hostname
    bootstrap_script.py          # vm_hostname() removed; hostname is an input
    platforms/                   # renamed from provisioners/
        __init__.py              # VM_PLATFORM_REGISTRY + register + capability publisher
        lima.py                  # LimaPlatform (vm_host in platform_config; local + remote)
        azure.py                 # AzurePlatform (platform_config = subscription/rg/region)
        wsl2.py                  # WSL2Platform
        proxmox.py               # ProxmoxPlatform (native_transport -> None; token via secret)
        proxmox_api.py           # unchanged
cli/agentworks/manifests/builtin/
    vm-sites.yaml                # NEW: bundled lima + wsl2 sites (reserved names)
cli/agentworks/manifests/samples/
    vm-site.yaml                 # NEW: sample site documents
cli/agentworks/vm_hosts/         # removed
cli/agentworks/cli/commands/vm_host.py   # removed
```

## What is not changing

- The resource-manifests framework: loader, envelope, kind flags, collision handling, origins,
  migrate/sample/edit/kinds tooling, and the shipped invoked-validation API. This SDD adds two
  kinds, one bundled manifest, one sample, the legacy-section mapping entries (plus the small
  multi-section-per-kind extension to `KIND_SECTIONS` noted above), and one composition-boundary
  check.
- The transports layer (landed in polymorphic-transports), beyond the `None`-return check and the
  `provisioner_transport()` factory rename to `native_transport()` (R1).
- The initializer's Phase A / Phase B structure; only the fields it reads change.
- Provisioning-activity names: `ProvisionRequest`, `provisioning_status`, vm event names
  (`ProvisionerError` is NOT retained; it renames to `ProvisioningError` per R1).
- The secrets machinery: the proxmox token (R2) is a new consumer of existing hooks
  (auto-declaration, `extra_decls`, the resolve loop), not a change to them.

## Migration sequencing

One branch, logical phases for reviewability (plan.md spells these out):

1. **Kinds + platform protocol**: `vm-platform` capability kind and publisher, `vm-site` kind
   (decode with the invoked-validation API, flags, references), bundled built-in sites, legacy
   `[azure]`/`[proxmox]` loader with deprecation warnings, `validate_sites`,
   `ProvisionRequest`/`ProvisionResult`, dispatch. Old entry points alive as shims during the
   transition.
2. **DB migration**: Python-step support in the runner; platform_metadata backfill hooks;
   `operator_stopped` + `hostname` columns; site rename + remote-Lima rewrite + manifest snippet
   printing; `vm_hosts` + `vm_host_name` drop (table rebuild); settings table.
3. **Manager rewiring**: `create_vm` through dispatch with `ProvisionRequest`; registry threading
   and platform binding at VM-command composition roots; proxmox token onto the standard resolve
   pass; `ensure_active` / `keep_active`; `_ensure_vm_running` absorbed; platform read paths on
   `platform_metadata`; the `ProvisionerError` to `ProvisioningError` and transports-factory
   `native_transport` renames.
4. **Slug + prompts + naming**: settings reads, first-create prompt, shared-backend nudge, SSH
   config file naming, hostname scheme, identity env changes.
5. **CLI surface**: `--site` rename + `defaults.site` alias, vm-host removal, `--site` dynamic
   completion + `vm-template.site`, migrate section mapping, `resource sample vm-site` content,
   doctor checks, `vm shell --platform` rename, proxmox error-text cleanup.
6. **Tests, docs (`docs/guides/resources.md` gains the vm-site kind; ADR 0016 cross-reference),
   sample-config, completions regen, lint, PR.**

Phases 1-3 are testable with slug-null semantics before phase 4 lands, mirroring the reasoning that
put the slug last in the original draft.

# VM sites and platforms -- implementation plan

**Status**: Phases 1-4 complete. Phases follow the HLA's sequencing sketch, with two refinements
recorded there-vs-here: `defaults.site` parsing (plus the deprecated `defaults.platform` alias) and
the `vm-template.site` field land in Phase 1 with the rest of the config/kind surface, so Phase 3's
selection precedence has both to read; only the operator-facing flag/completion work stays in
Phase 5.

**Sequencing notes**:

- **2026-07-09, Phase 1: the non-compiling window was eliminated by bridging.** Instead of leaving
  old dispatch broken until Phase 3, Phase 1 ships explicit PHASE-1 BRIDGE shims: `get_provisioner`
  / `get_provisioner_for_vm` construct new-shape platforms from legacy inputs; `create_vm`
  dispatches through `resolve_site` + `ProvisionRequest` (interim `{site}--{name}` hostname, null
  slug, legacy `--vm-host` override); the manager's factory call sites use `native_transport` with
  the bridge-dispatched platform; `VMRow` gains a `platform_metadata` bridge (derived from the
  legacy columns in the row loader, mirroring the future backfill) and a `site` property aliasing
  the `platform` column; the vm-site kind's `instances()` reads through the alias. Result: the full
  suite (1500 tests), ruff, and mypy are green at the end of Phase 1. Remaining true window items:
  proxmox lifecycle ops need the token on the resolve pass (Phase 3); the R11 hostname / slug land
  in Phase 4; and CUSTOM-named sites are refused with a typed `StateError` -- `create_vm` guards up
  front (a custom-site create would otherwise provision and then half-complete, since every
  subsequent step dispatches through the legacy `get_provisioner` bridge, which only maps the four
  legacy names), and the bridge itself raises the same typed error for any custom-site row -- until
  Phase 3 dispatches everything through `platform_for`. A useful corollary: no `vms` row can hold a
  custom site name before the Phase 2 migration runs. Every bridge is marked `PHASE-1 BRIDGE` and is
  deleted by its owning phase.
- **2026-07-09, Phase 1: migrator vm-site support moved wholly to Phase 5.** `KIND_SECTIONS` grew
  the tuple-valued multi-section shape with the `vm-site -> (azure, proxmox)` entry (deprecation
  warnings and samples key off it), but `_MIGRATABLE_KINDS` excludes vm-site until Phase 5 (where
  the plan already listed the migrate section mapping): the flat-to-nested emission is migrator
  surgery that belongs with the rest of the CLI-surface work, and `agw resource migrate vm-site`
  errors as an unknown kind until then. The Phase 1 test bullet claiming end-to-end
  `test_resource_migrate` coverage moves with it.
- **2026-07-09, Phase 1 review round: the FRD R2 site-name rules landed in decode.** The plan had
  not scheduled them anywhere; the Phase 1 reviewer caught the gap. `_decode_vm_site` now applies
  `validate_name` to site names and enforces the platform-name-shadow rule (a site named after a
  known platform must declare that platform). The legacy TOML path needs neither check (its section
  names are exactly `azure` / `proxmox`, each declaring its own platform). Two artifact drifts were
  also recorded rather than reverted: `ProvisionRequest.ssh_private_key` (azure/proxmox build their
  native SSH transports during `create()` and no longer receive `Config`; now in the HLA sketch) and
  the vm-platform kind description prose.
- **2026-07-10, Phase 2 review round: the migration validates before any DDL and prints to stderr.**
  The reviewer caught that the designed unknown-platform loud failure fired AFTER the `ALTER TABLE`s
  -- sqlite3 auto-commits DDL, so the failure left a half-migrated v26 DB that died on
  duplicate-column at every retry. Both validation scans (unknown platform, and the newly-loud
  remote-Lima `-host`-suffix site-name collision, which would otherwise silently merge two hosts)
  now run pre-DDL, so the anticipated failure modes leave a pristine v26 DB and retry-after-fix
  works. The site-manifest snippets moved wholly to the warn channel (stderr): migrations run at
  every `Database()` open, including under the stdout-capturing completion helpers. Deferred with
  intent: a per-version `commit()` checkpoint in the migration runner (making retry safe for
  multi-version jumps, not just v26 -> v27) is a pre-existing runner property, scheduled as a Phase
  6 hardening candidate.
- **2026-07-11, Phase 3 review round: bind-once is now structural, not aspirational.** The reviewer
  caught the as-built shape binding at every gate site (`keep_active(..., bind_platform(...))`
  inline), which multiplied resolve passes -- on a prompt-backed proxmox token, reachable
  multi-prompting (up to five binds on `session create --new-workspace --new-agent`). Fixes:
  `_prepare_vm` / `_prepare_vm_target_for_attach` bind once and RETURN the platform (accepting a
  pre-bound one); holds that follow a gate use `platform.vm_active` directly instead of re-gating;
  `bind_platforms` shares one platform instance and one resolve pass per SITE (not per VM);
  `_ensure_tailscale` takes the caller's bound platform (the gates never bind); `copy_workspace`
  reuses the source platform on same-VM copies. Two more behavioral catches: `delete_vm` no longer
  gates the best-effort tailscale logout (an operator-stopped VM would have raised and skipped the
  backend delete; an idle-stopped VM would have been booted just to be deleted -- it now holds
  without gating, and a logout failure can no longer skip `platform.delete`); `ensure_active`
  re-reads `operator_stopped` from the DB on the slow path (a concurrent `vm stop` between the
  caller's row load and the gate no longer auto-restarts the VM). `vm describe` degrades on a
  stranded site (warn + manifest hint, row fields still render) instead of erroring -- describe is
  the inspection command an operator reaches for on exactly that row. `reinit` binds before git
  token collection so the R3 error fires first. New pinning tests: `test_bind_platform.py` (no
  resolve pass without site secrets, exactly one for a secret-bearing site, one pass + one shared
  instance per site across a batch, empty-set builds no registry) and the DEALLOCATED /
  concurrent-stop gate cases. Round 2 closed the residuals: the nested `create_workspace` /
  `create_agent` calls accept the parent's bound platform (a DELIBERATE carve-out to the
  resource-manifests SDD's CLI-shaped-args-only seam pin -- the bound platform is the command's one
  typed platform bind, not the open-ended values/registry smuggling that pin blocks; the pinned test
  documents the carve-out), so `session create --new-workspace --new-agent` is one resolve pass end
  to end; `delete_vm`'s hold+logout span is its own try/warn (a broken WSL2 hold -- the exact state
  delete cleans up -- can no longer abort before the backend delete), `UserAbort` at a bind prompt
  aborts the whole delete, and the bind-failure warn carries the R3 manifest hint; all pinned by a
  new `test_delete_vm_gating.py` suite.
- **2026-07-11, Phase 4 review round: prompt fidelity and three recorded rulings.** The reviewer
  caught that `TyperHandler.prompt` neither translated Ctrl-C to the typed `UserAbort` (every other
  interactive method does) nor suppressed the noisy `[]` empty-default suffix -- both fixed at the
  handler -- and that the nudge's `choose` menu lost FRD R4's default-yes affordance; the nudge is
  now the FRD's single-line `[Y/n/never-remind-me]` prompt (Enter accepts, unrecognized input reads
  as "no" since the nudge is non-blocking and repeats). Rulings on the three review questions: (1)
  declining the first-create prompt no longer triggers the nudge in the SAME create --
  `_resolve_system_slug` returns `(slug, asked_now)` and the nudge is skipped when the operator was
  just asked (twice back-to-back is noise, not a reminder); (2) the every-sync removal of the
  slug-less ssh-config file is accepted -- same-workstation multi-install is out of scope per the
  FRD (DB-path separation prerequisite), and `agentworks.conf` is an agentworks-owned name inside
  our own config.d; (3) `vm describe` keeps the install-level slug row but annotates it
  `(not applied to this VM)` when the VM's hostname predates the slug. Also: the settings keys moved
  next to their accessors in `db.py` (ssh_config was hardcoding the literal), and the suite now pins
  the FRD prompt wording and the slug-before-secrets/insert ordering.

**Compile boundaries**: Phases 1 through 3 are one logical commit boundary, mirroring the
polymorphic-transports precedent. As planned, Phase 1 would open a non-compiling window when the
platform classes reshape to the new protocol; as built, PHASE-1 BRIDGE shims keep everything
compiling and tested at every phase boundary (see the sequencing note above), with Phase 2 providing
the DB columns the new read paths need and Phase 3 rewiring the callers and retiring the bridges.
The end of Phase 3 is still the natural pause point: before it, proxmox lifecycle ops raise a typed
error (token not yet threaded) and the interim hostname/slug shapes are in effect. Remote-Lima VMs
are additionally non-functional between the Phase 2 migration and the operator adding their site
manifests; that is the designed R3 stranded state, and mid-branch it also applies to dev databases.

## Phase 1: Kinds, protocol, registry, dispatch

New resource machinery plus the platform-class reshape. Additive pieces first. As built, the reshape
did NOT open the non-compiling window: PHASE-1 BRIDGE shims keep the old call paths compiling and
green (see the sequencing note at the top of this plan).

- [x] `cli/agentworks/vms/base.py`: add `ProvisionRequest` (vm_name, hostname, system_slug,
      admin_username, ssh_public_key, tailscale_auth_key nullable,
      cpus/memory_gib/disk_gib/swap_gib, azure_vm_size) and reshape `ProvisionResult`
      (native_transport, platform_metadata, bootstrap_complete, tailscale_ip). Rename the ABC
      `VMProvisioner` to `VMPlatform` with the R8 surface: abstract
      create/start/stop/delete/status/display_backend_name; concrete native_transport (None default)
      / post_tailscale_ready / transient_route / vm_active. Class-level contract: `name`,
      `validate_config(owner, config) -> tuple[ConfigReference, ...]` (classmethod,
      `GitCredentialProvider` shape, including the may-be-deprecated note),
      `shared_backend(platform_config) -> bool` (classmethod), and
      `legacy_platform_metadata(row, legacy)` (pure).
- [x] `cli/agentworks/vms/platforms/`: `git mv` from `vms/provisioners/`; class renames
      (`LimaProvisioner` to `LimaPlatform`, etc.). Constructors become uniform
      `cls(site_name, platform_config, secret_values)`:
  - [x] `lima.py`: `vm_host` optional key in platform_config replaces the `vm_host_ssh` constructor
        arg; `is_remote` derives from it; `shared_backend` computes from it; read paths move to
        `platform_metadata['instance_name']`.
  - [x] `azure.py`: platform_config = subscription_id / resource_group / region (replaces
        `config.azure` reads); read paths move to `platform_metadata['resource_id']`;
        `display_backend_name` returns the VM-name portion.
  - [x] `wsl2.py`: read paths (`_keepalive`, start/stop/delete/status) move from `vm.name` to
        `platform_metadata['distro_name']`.
  - [x] `proxmox.py`: delete the `PROXMOX_TOKEN_SECRET` env read; the token arrives via
        `secret_values` (declared by `validate_config` as `token_secret`, default
        `proxmox-token-secret`); ops read `platform_metadata['vmid']` and `['node']` with the
        platform_config-node fallback plus opportunistic write-back; `native_transport` returns
        `None`; the operator-facing error embedding a `docs/sdd/` path is rewritten.
  - [x] `__init__.py`: `VM_PLATFORM_REGISTRY`, `@register`, and the capability publisher
        (`vm-platform` rows, `Origin.built_in(source="agentworks.vms")`).
- [x] `cli/agentworks/vms/base.py` + `cli/agentworks/errors.py` +
      `cli/agentworks/transports/__init__.py`: the noun-retirement renames:
      `provisioner_transport()` method to `native_transport()`, the transports factory
      `provisioner_transport` to `native_transport` (None check replaces the proxmox name branch),
      `ProvisionerError` to `ProvisioningError`.
- [x] `cli/agentworks/vms/kinds.py`: register both kinds alongside the existing vm-template kind.
      `vm-platform`: category `capability`, error miss policy, description "VM backend
      implementations (code)". `vm-site`: category `declarable`, `builtin_override = "reserved"`,
      error miss policy; decode takes `spec.platform` (required, `ResourceReference` to
      `vm-platform/<name>`) + `spec.platform_config` (optional mapping, no shadowing of top-level
      spec keys); registered platforms validate at decode via `validate_config`
      (defer-on-unknown-platform to the finalize miss policy). `VMSiteDecl` dataclass with nested
      `platform_config` and `referenced_resources()` emitting the platform edge plus
      `validate_config`'s ConfigReferences with the site as source.
- [x] `cli/agentworks/vms/kinds.py` + `cli/agentworks/vms/template.py`: vm-template gains the
      optional `site` field (bare-name reference, edge to `vm-site`); TOML and YAML decode parity.
- [x] `cli/agentworks/manifests/builtin/vm-sites.yaml`: bundled `lima` and `wsl2` sites (platform
      matching the name, empty platform_config). First real bundle content; wire through
      `manifests/builtin.py`.
- [x] `cli/agentworks/manifests/samples/vm-site.yaml`: sample documents (an azure site with
      platform_config, a remote-lima site); loader-verified via the existing samples test.
- [x] `cli/agentworks/config.py`: legacy `[azure]` / `[proxmox]` loader/publisher (per ADR 0016 this
      is their home): parse flat sections, nest into `platform_config` at the boundary, publish
      `vm-site/azure` / `vm-site/proxmox` rows with TOML `file:line`, join the aggregated
      deprecation warning. `defaults.site` parsing with `defaults.platform` as a one-release
      deprecated alias; `defaults.vm_host` becomes the hard `ConfigError` with the site-manifest
      snippet.
- [x] `cli/agentworks/vms/sites.py`: `resolve_site(name, registry, *, secret_values=None)` (KeyError
      from `registry.lookup` maps to the `ConfigError` + ready-to-paste manifest hint),
      `platform_for(vm, registry, **kw)`, `_config_secrets`, `vms.validate_sites(config, registry)`
      (wired into `build_registry` beside `secrets.validate_chain`), `_site_manifest_hint`.
- [x] `cli/agentworks/manifests/decode.py`: `KIND_SECTIONS` grows the multi-section-per-kind shape;
      vm-site maps to the `azure` and `proxmox` sections with section-name-becomes-resource-name
      semantics. `cli/agentworks/migrate/planning.py` does NOT follow yet: `_MIGRATABLE_KINDS`
      excludes vm-site until the migrator's flat-to-nested emission lands in Phase 5 (see the
      sequencing note).
- [x] Tests (new): `cli/tests/vms/test_vm_site_kind.py` (decode, shadowing rejection, R2 name rules,
      reserved built-in names, unknown-platform deferral, reference emission),
      `cli/tests/vms/test_vm_platform_kind.py` (capability rows, not declarable),
      `cli/tests/vms/test_sites_dispatch.py` (resolve_site happy path, stranded ConfigError + hint,
      secret threading), `cli/tests/vms/test_platform_validate_config.py` (all four platforms:
      unknown keys, lima vm_host, proxmox token_secret reference + default, azure required keys) --
      joins the pinned `test_capability_config_contract.py` patterns. Also (unplanned, per review):
      `cli/tests/vms/test_legacy_site_sections.py` (legacy `[azure]`/`[proxmox]` loading, defaults
      site/alias/vm_host, TOML-vs-manifest decode parity) and
      `cli/tests/vms/test_vm_template_site.py` (site field parse/parity/edge/inheritance,
      `select_site` precedence).
- [x] Tests (updated): `cli/tests/manifests/test_samples.py` picks up vm-site;
      `cli/tests/test_resource_kinds.py` counts the two new kinds. As built, the `[azure]` /
      `[proxmox]` + `defaults.platform` deprecation coverage lives in the NEW
      `cli/tests/vms/test_legacy_site_sections.py` (not `test_config_deprecation_warnings.py`), and
      the vm-site decode-parity case lives there too (not `test_decode_parity.py`). The
      `test_resource_migrate.py` vm-site coverage moves to Phase 5 with the migrator mapping (see
      the sequencing note).

**Definition of done**: kinds, publishers, dispatch, and samples in place with their tests green in
isolation. MET, with a positive deviation: instead of the expected open window (old dispatch not
compiling until Phase 3), PHASE-1 BRIDGE shims keep `get_provisioner*` and the manager callers
working against the new platform classes, so the full suite, ruff, and mypy are green at the phase
boundary. The bridges are marked in-code and retired by Phases 2/3 as originally planned.

## Phase 2: DB migration

One Python migration version; runner support first.

- [x] `cli/agentworks/db.py`: `MIGRATIONS` values become `str | Callable`; callables receive
      `(conn, context)` where `context.legacy` is the best-effort, unvalidated parse of the config
      file's legacy TOML sections (missing/unreadable config yields an empty mapping; tolerant by
      construction -- nothing may depend on it succeeding). As built, `context.legacy` carries the
      WHOLE parsed document (so hooks index `legacy["proxmox"]` etc.), built lazily once per run.
- [x] The migration step (v27, `_migrate_vm_sites`), in order:
  - [x] Add `platform_metadata TEXT NOT NULL DEFAULT '{}'`,
        `operator_stopped INTEGER NOT NULL     DEFAULT 0`, `hostname TEXT`.
  - [x] Backfill `platform_metadata` per row via the owning platform's
        `legacy_platform_metadata(row, context.legacy)` hook (lima instance_name, wsl2 distro_name,
        azure resource_id, proxmox vmid + node-if-present; absent keys omitted, never empty
        strings). Backfill `hostname = '{platform}--{name}'`. The per-platform map only needs the
        four legacy names: pre-SDD schemas constrain the `platform` column to them, and the Phase 1
        create guard refuses custom-named sites mid-window, so no row can hold anything else (a
        value outside the map would be a genuine corruption -- fail loudly, don't guess).
  - [x] Site rename: remote-Lima rows (`vm_host_name` set) get `site = vm_host_name`; collect the
        referenced `vm_hosts` rows and print ready-to-paste `vm-site` manifest documents once at the
        end (suffix `-host` on reserved-name collision, and say so). All other rows keep their value
        (already the right site name).
  - [x] Rebuild the `vms` table (the `vm_host_name` FK blocks `DROP COLUMN`): drop
        `azure_resource_id` / `wsl_distro_name` / `proxmox_vmid` / `vm_host_name`, rename `platform`
        to `site`, declare `hostname NOT NULL`. Drop `vm_hosts`.
  - [x] `CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)`.
- [x] `cli/agentworks/db.py`: `VMRow` becomes `site: str`, `platform_metadata: dict[str, str]`
      (JSON-parsed), `operator_stopped: bool`, `hostname: str`; legacy fields removed; `insert_vm` /
      `update_vm_*` helpers follow (including `set_operator_stopped` and
      `update_vm_platform_metadata`, replacing the three per-platform column writers); `VMHostRow`
      and the vm_hosts accessors delete.
- [x] Mechanical sweep: `vm.platform` readers become `vm.site` where they name the site
      (`vms/manager.py` list/describe, `sessions/`, `agents/`, `workspaces/`, `cli/_helpers.py`,
      `env/show.py`); display strings stay correct because the values are unchanged. As built, the
      sweep also had to bridge the surfaces whose DB backing vanished (all marked PHASE-2 BRIDGE,
      retired in later phases): `vm_hosts/manager.py` service functions raise the typed
      replaced-by-vm-sites error (commands removed in Phase 5; `--names-only` degrades quietly for
      completion); `create_vm --vm-host` raises the typed error with the site-manifest hint;
      doctor's vm-hosts check is stubbed pending the Phase 5 vm-site report;
      `ResourceContext.platform` now carries the site name and `vm_host` has no producer
      (`AGENTWORKS_VM_HOST` never emitted) until the Phase 4 identity redesign; describe shows a raw
      platform_metadata dump pending Phase 3's `display_backend_name`.
- [x] Tests: `cli/tests/test_db_migration_vm_sites.py` -- fixture DBs at the prior schema version
      covering all four platforms plus a remote-Lima row (and a platform-name-shadowing host);
      assert backfilled metadata shapes, the hostname backfill, the site rename, the printed
      snippets, the NOT NULL rebuild, empty-`legacy` behavior (proxmox node omitted), the
      unknown-platform loud failure, and settings-table creation. Existing test seeds move to the
      new row shape (the shared VM seeding lives per-file, not in `conftest.py`).

**Definition of done**: a pre-SDD database opens cleanly and lands on the new schema with correct
data; row helpers and fixtures compile against the new shape. MET -- and as with Phase 1, the
suite/ruff/mypy are fully green at the boundary (the plan expected the window to stay open here; the
bridges keep it closed). Remaining window items are unchanged from the Phase 1 note, plus:
remote-Lima rows (site = host name) fail typed at the `get_provisioner` bridge until Phase 3
dispatches through `platform_for`.

## Phase 3: Manager rewiring

Close the window: every caller onto the new dispatch, gate, and request shapes.

- [x] `cli/agentworks/vms/manager.py`: `create_vm` resolves the site (flag, then `template.site`,
      then `defaults.site`, then `lima`), runs the composition-root ordering from the HLA (registry
      -> site decl -> `extra_decls` for site secrets -> single resolve -> bind), computes the R11
      hostname (bare VM name until the slug lands in Phase 4), builds `ProvisionRequest`, and calls
      `platform.create(request)`; writes `platform_metadata` verbatim, `hostname`, and
      `operator_stopped = False`. Legacy `update_vm_azure_resource_id`-style writes deleted in
      Phase 2. As built, the site's secret decls join `_collect_secrets`' existing single resolve
      pass (a `site_decls` kwarg) rather than a second pass.
- [x] `cli/agentworks/vms/manager.py`: `ensure_active(db, config, vm, platform)` (fast-path
      tailscale probe; STOPPED/DEALLOCATED honoring `operator_stopped`; UNKNOWN proceeds; post-start
      `_ensure_tailscale` inside `vm_active`) and `keep_active(db, config, vm, platform)` /
      `keep_actives` taking the BOUND platform. `keep_vm_active` / `keep_vms_active` /
      `get_provisioner` / `get_provisioner_for_vm` delete. As built, the composition-root ordering
      is packaged as `bind_platform(config, vm, *, registry=None)` (and `bind_platforms` for the
      multi-VM sites, lazy so an empty VM set never builds a registry): each command entry binds
      once via the helper and threads the platform down; the gates never bind.
- [x] `cli/agentworks/vms/manager.py`: `start_vm` clears `operator_stopped` then starts; `stop_vm`
      sets it BEFORE the already-stopped short-circuit; `describe_vm` shows Site, Platform,
      `display_backend_name()`, and a live Status line pairing observed state with the flag
      (`stopped (operator)` vs `stopped (idle)`); `vm list` header PLATFORM became SITE in Phase 2.
- [x] `cli/agentworks/workspaces/manager.py`: `_ensure_vm_running` deletes; its callers
      (`sessions/console.py`, `sessions/multi_console.py`, `agents/manager.py`, and the in-module
      sites) move to the public gate, with their composition roots binding the platform per the HLA
      ordering. The existing `keep_vm_active` call sites across `sessions/`, `agents/`,
      `workspaces/` migrate to `keep_active` with the bound platform threaded.
- [x] `cli/agentworks/vms/initializer.py`: Phase A reads `vm.hostname` (stop re-deriving via
      `vm_hostname`) and the WSL2-native-swap decision moves to the caller (`script_swap` computed
      from the bound platform's name); `bootstrap_script.vm_hostname()` deletes; `initialize_vm`
      takes the bound platform from `create_vm`'s composition root; `reinit` binds via
      `bind_platform` (a stranded remote-Lima VM fails there with the R3 ConfigError, before any env
      baking).
- [x] `cli/agentworks/transports/__init__.py` + `cli/agentworks/vms/backup.py` + remaining callers:
      adopt the renamed factory and the bound platform; `vm shell --provisioner`'s internals go
      through the bound platform.
- [x] Tests: gate semantics (`cli/tests/vms/test_ensure_active.py`: fast path skips `status()`,
      auto-resume, operator*stopped StateError, UNKNOWN proceeds, stop-sets-flag-before-shortcut,
      start-clears-flag); `create_vm` request shape + row persistence and the proxmox token end to
      end (`cli/tests/vms/test_create_vm_dispatch.py`: resolve pass carries `proxmox-token-secret`
      via the AW_SECRET* env backend, the bound platform receives the value, the old raw
      `PROXMOX_TOKEN_SECRET` variable is provably unread); existing suites updated to the new shapes
      via a shared `stub_vm_gates` conftest helper (and `_StubRegistry` now serves the four built-in
      vm-site rows so namespace-config tests can bind for real).

**Definition of done**: the codebase compiles cleanly; full pytest passes (1523); `git grep` finds
no `VMProvisioner`, `get_provisioner`, `keep_vm_active`, or `ProvisionerError` references outside
historical SDDs. MET, with one carve-out the plan itself schedules: the `vm_hosts` PHASE-2 BRIDGE
module (typed replaced-by-vm-sites errors) survives until Phase 5 removes the `agw vm-host`
commands.

## Phase 4: Slug, prompts, SSH config, hostname, identity env

- [x] `cli/agentworks/vms/manager.py` (create path): first-create slug prompt (settings row absent;
      empty answer writes the empty-value declined row; non-interactive neither prompts nor writes);
      deferred shared-backend nudge (`sites.site_shared_backend(decl)` wrapping the platform's
      classmethod so the manager stays registry-blind; skipped non-interactively; `never-remind-me`
      suppression key; a plain "no" leaves everything unset so the nudge repeats); slug format
      validation (3-20, lowercase alnum + dash, no leading/trailing dash). An invalid prompt answer
      aborts the create with the settings row unwritten, so the next create asks again. The slug
      also surfaces on `vm describe` (R4's allowed surfaces; `vm list` stays name-only).
- [x] Slug consumption: `ProvisionRequest.system_slug` + R11 hostname (`{slug}-{vm.name}` /
      `{vm.name}`); per-platform backend-side naming with the R9 collision pre-flight landed in
      Phase 1 (all four platforms already compose `{slug}-{name}` from `request.system_slug`).
- [x] `cli/agentworks/ssh_config.py`: managed file `agentworks-{slug}.conf` when slug set (fallback
      `agentworks.conf`); every config.d sync removes the old slug-less file once a slug exists
      (idempotent superset of "first sync after the slug is set"); legacy (non-config.d) mode
      untouched. The declined (empty-value) row behaves like no slug.
- [x] `cli/agentworks/env/identity.py`: `ResourceContext.vm_host` removed, `site` added;
      `AGENTWORKS_SITE` emitted; `AGENTWORKS_VM_HOST` gone; `AGENTWORKS_PLATFORM` resolved at every
      ResourceContext composition root via `sites.site_platform_name(vm.site, registry)`
      (initializer, vm shell/exec, agent shell/exec, session env, console panes, env show).
      Permanent env-var docs (not the locked env-and-secrets SDD) updated in Phase 6.
- [x] Tests: settings encoding (absent vs empty vs value), prompt one-shot behavior including
      non-interactive, nudge suppression + skipped-for-local-sites + plain-no repeats, hostname
      composition (`test_create_vm_dispatch`) + 51-char bound, ssh-config file naming + old-file
      removal + declined-slug fallback, identity env emission (AGENTWORKS_SITE in, VM_HOST out).

**Definition of done**: slug-null behavior identical to Phase 3; slug-set behavior covered by tests;
existing VMs keep hostnames and env values. MET (suite 1550, ruff, mypy green).

## Phase 5: CLI surface and completions

- [ ] `cli/agentworks/cli/commands/vm.py`: `--platform` becomes `--site` (static Choice removed;
      validation at dispatch); `vm shell --provisioner` becomes boolean `--platform` with
      `--provisioner` as a hidden alias for one release; help text sweep.
- [ ] `cli/agentworks/cli/commands/vm_host.py` + `cli/agentworks/vm_hosts/`: removed.
- [ ] `cli/agentworks/completions/spec.py`: `("vm.create", "site")` maps to a `sites` completer
      (sourced from `agw resource list --kind vm-site --names-only`, splitting `vm-site/<name>` like
      `resource_names`); `vm_host` entries removed; shell generators + `agw completion` regenerated.
- [ ] `agw doctor`: every `vm.site` resolves (stranded rows report the paste-ready snippet); slug
      row.
- [ ] `cli/agentworks/sample-config.toml`: `[azure]` / `[proxmox]` examples replaced by a pointer at
      `agw resource sample vm-site`; `defaults.site` documented with the deprecated
      `defaults.platform` alias noted.
- [ ] Tests: `test_completions.py` (new completer, removed entries), CLI smoke tests for the renamed
      flags, doctor rows, `test_sample_config.py`.

**Definition of done**: full CLI surface matches FRD R13; completions regenerate cleanly.

## Phase 6: Tests, docs, release notes, PR

- [ ] Full pytest; no regressions vs the pre-branch count.
- [ ] `ruff` / `mypy` package-wide; `./scripts/lint-files.sh`.
- [ ] `docs/guides/resources.md`: vm-site and vm-platform join the kind story; `cli/README.md`:
      env-var inventory (`AGENTWORKS_SITE` added, `AGENTWORKS_VM_HOST` retired) and the
      settings-vs-resources reference; ADR 0016 gets a one-line cross-reference to this SDD as the
      vm-site implementation.
- [ ] Release-notes text: the `!`-flagged breaks (`--platform` to `--site`, `vm shell` flag,
      `PROXMOX_TOKEN_SECRET` sourcing) with their one-line remediations.
- [ ] Hardening candidate (from the Phase 2 review round): a per-version `commit()` checkpoint in
      the migration runner so retry is safe for multi-version jumps, not just the v26 -> v27 step
      (today an earlier version's auto-committed DDL re-runs on retry when a later version fails).
- [ ] Hardening candidate (from the Phase 3 review round): `session create`'s ephemeral ROLLBACK
      path calls `delete_agent` / `delete_workspace`, which self-bind -- a failed create on a
      secret-bearing site re-runs the resolve pass mid-rollback (possible re-prompt; degrades to the
      existing recover-with warn non-interactively). Decide whether the delete-side seams get the
      same `platform` carve-out or the degrade is accepted.
- [ ] Open the PR; agentworks-reviewer round on the branch.

**Definition of done**: PR open, CI green, reviewer findings addressed. `locked.md` lands after
merge per the SDD lifecycle.

## Risk and mitigations

- **Non-compiling window (Phases 1-3)**: the protocol reshape breaks old dispatch until the manager
  rewires. Mitigated by the declared pause point (end of Phase 3) and by Phase 1's new machinery
  carrying isolated tests that pass before the window opens.
- **One-shot destructive migration (Phase 2)**: the vms rebuild drops columns and a table. Mitigated
  by fixture-DB tests for every platform shape, the remote-Lima snippet printing being part of the
  same step (nothing to forget), and the pure backfill hooks being unit-testable without a database.
- **Proxmox token threading (Phase 3)**: the one platform whose gate calls need secrets; a missed
  composition root surfaces as a `secret_values` KeyError instead of a silent env fallback (the env
  read is deleted, so there is no shadow path). The end-to-end test pins the resolve pass.
- **Remote-Lima stranding**: designed (R3), but operators must see the snippet more than once.
  Mitigated by printing at migration, repeating in the per-op `ConfigError`, and the doctor row.
- **Breadth of the `keep_vm_active` sweep**: many call sites across sessions/agents/workspaces.
  Mitigated by deleting the old names in the same phase (stale callers fail loudly at import, not
  silently at runtime).

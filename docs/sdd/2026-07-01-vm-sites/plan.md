# VM sites and platforms: implementation plan

**Status**: all seven phases complete (Phase 7, the capability model adoption, was added 2026-07-12
and approved by its reviewer round the same day). Phases follow the HLA's sequencing sketch, with
two refinements recorded there-vs-here: `defaults.site` parsing (plus the deprecated
`defaults.platform` alias) and the `vm-template.site` field land in Phase 1 with the rest of the
config/kind surface, so Phase 3's selection precedence has both to read; only the operator-facing
flag/completion work stays in Phase 5.

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
  in Phase 4; and CUSTOM-named sites are refused with a typed `StateError`: `create_vm` guards up
  front (a custom-site create would otherwise provision and then half-complete, since every
  subsequent step dispatches through the legacy `get_provisioner` bridge, which only maps the four
  legacy names), and the bridge itself raises the same typed error for any custom-site row, until
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
  The reviewer caught that the designed unknown-platform loud failure fired AFTER the
  `ALTER TABLE`s: sqlite3 auto-commits DDL, so the failure left a half-migrated v26 DB that died on
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
  inline), which multiplied resolve passes: on a prompt-backed proxmox token, reachable
  multi-prompting (up to five binds on `session create --new-workspace --new-agent`). Fixes:
  `_prepare_vm` / `_prepare_vm_target_for_attach` bind once and RETURN the platform (accepting a
  pre-bound one); holds that follow a gate use `platform.vm_active` directly instead of re-gating;
  `bind_platforms` shares one platform instance and one resolve pass per SITE (not per VM);
  `_ensure_tailscale` takes the caller's bound platform (the gates never bind); `copy_workspace`
  reuses the source platform on same-VM copies. Two more behavioral catches: `delete_vm` no longer
  gates the best-effort tailscale logout (an operator-stopped VM would have raised and skipped the
  backend delete; an idle-stopped VM would have been booted just to be deleted; it now holds without
  gating, and a logout failure can no longer skip `platform.delete`); `ensure_active` re-reads
  `operator_stopped` from the DB on the slow path (a concurrent `vm stop` between the caller's row
  load and the gate no longer auto-restarts the VM). `vm describe` degrades on a stranded site
  (warn + manifest hint, row fields still render) instead of erroring: describe is the inspection
  command an operator reaches for on exactly that row. `reinit` binds before git token collection so
  the R3 error fires first. New pinning tests: `test_bind_platform.py` (no resolve pass without site
  secrets, exactly one for a secret-bearing site, one pass + one shared instance per site across a
  batch, empty-set builds no registry) and the DEALLOCATED / concurrent-stop gate cases. Round 2
  closed the residuals: the nested `create_workspace` / `create_agent` calls accept the parent's
  bound platform (a DELIBERATE carve-out to the resource-manifests SDD's CLI-shaped-args-only seam
  pin: the bound platform is the command's one typed platform bind, not the open-ended
  values/registry smuggling that pin blocks; the pinned test documents the carve-out), so
  `session create --new-workspace --new-agent` is one resolve pass end to end; `delete_vm`'s
  hold+logout span is its own try/warn (a broken WSL2 hold, the exact state delete cleans up, can no
  longer abort before the backend delete), `UserAbort` at a bind prompt aborts the whole delete, and
  the bind-failure warn carries the R3 manifest hint; all pinned by a new `test_delete_vm_gating.py`
  suite.
- **2026-07-11, Phase 4 review round: prompt fidelity and three recorded rulings.** The reviewer
  caught that `TyperHandler.prompt` neither translated Ctrl-C to the typed `UserAbort` (every other
  interactive method does) nor suppressed the noisy `[]` empty-default suffix (both fixed at the
  handler) and that the nudge's `choose` menu lost FRD R4's default-yes affordance; the nudge is now
  the FRD's single-line `[Y/n/never-remind-me]` prompt (Enter accepts, unrecognized input reads as
  "no" since the nudge is non-blocking and repeats). Rulings on the three review questions: (1)
  declining the first-create prompt no longer triggers the nudge in the SAME create:
  `_resolve_system_slug` returns `(slug, asked_now)` and the nudge is skipped when the operator was
  just asked (twice back-to-back is noise, not a reminder); (2) the every-sync removal of the
  slug-less ssh-config file is accepted: same-workstation multi-install is out of scope per the FRD
  (DB-path separation prerequisite), and `agentworks.conf` is an agentworks-owned name inside our
  own config.d; (3) `vm describe` keeps the install-level slug row but annotates it
  `(not applied to this VM)` when the VM's hostname predates the slug. Also: the settings keys moved
  next to their accessors in `db.py` (ssh_config was hardcoding the literal), and the suite now pins
  the FRD prompt wording and the slug-before-secrets/insert ordering.
- **2026-07-11, Phase 5 review round: two verification-after-write hazards and the noun sweep.** The
  reviewer caught (1) a `description` key in a legacy `[azure]`/`[proxmox]` section escaping the
  migrator's pre-write guard (vm-site is a `_DESCRIPTION_KINDS` member, so the metadata pop ran
  before the platform_config sweep) and failing registry-equivalence AFTER files were written;
  vm-site is now excluded from the pop (its flat sections never supported the key), so the value
  falls into `platform_config` and hits the clean pre-write refusal; and (2) doctor's new VM-sites
  group opening the Database BEFORE the Database group, silently auto-migrating mid-report; it now
  defers on `current != latest` with a pointer at the Database group. The R13 noun sweep also
  reached the last operator-facing `--provisioner` spellings (the shell_vm no-Tailscale hint and the
  issue-#117 heal hint teach `--platform`), and the service-layer kwarg renamed to
  `platform_transport` (the "provisioner" noun is retired; the CLI's `--provisioner` alias remains
  the one deliberate survivor, and it also appears in completions for its one-release life, both
  facets of the same recorded click-can't-hide-aliases deviation). Minors: `_MIGRATABLE_KINDS`
  became a set (`_SECTION_KINDS` owns the section mapping), the `sites` completer joined the
  registry-sourced completion pin, doctor's catch-all warn carries the exception detail, and
  `ssh.py`'s docstring stopped citing deleted modules.
- **2026-07-12, capability model adoption added as Phase 7.** After the six phases closed
  merge-ready, the maintainer landed `capability-model.md` (drafted with another dev; the contract
  vm-platform/vm-site, git credentials, and the session harness converge on) and directed adopting
  it in this PR, since vm-platform/vm-site is the first pair to implement the full shape and the PR
  is already breaking. The load-bearing inversions vs the phases above: platforms construct bound to
  `(config, resolver)` instead of receiving resolved `secret_values` (binding no longer resolves or
  prompts); everything preflights (the vm-template predicts its Tailscale key can resolve (the
  template's responsibility, not the site's) and the platform instance checks tools/reachability,
  all before any mutation or prompt); secret resolution happens once, at the preflight boundary,
  over the union of what all planned ops across all participating resources need (timing refined
  from "first op-need" to "as soon as preflight passes" by maintainer ruling the same day); the base
  `Capability` class and the platform implementations move to a `capabilities/` subtree; ops carry
  per-op idempotency flags. The doc also pins the abort discipline the move implies: catch-alls
  around best-effort spans (the resolve pass included) re-raise `UserAbort`. The doc promotes to a
  permanent `capabilities/README.md` once git credentials validates it; that promotion belongs to
  the other workstream, not this PR.
- **2026-07-12, Phase 7 implementation rulings (all ratified by the Phase 7 review round).** (1)
  rekey's is-it-running check moved PAST the boundary: it is a backend status read (an op; on
  proxmox it needs the token), so a stopped-VM failure now lands after the one prompt session
  instead of before it; the alternative was two prompt sessions, which the contract forbids. (2)
  Proxmox's preflight is the base's token prediction only; the API-reachability read was deferred
  (the version endpoint needs auth, so a useful read needs the token value). Superseded 2026-07-13:
  the maintainer ruled the whole class past preflight's structural ceiling (see that note); it is no
  longer a follow-up, it is the op's job. (3) reinit does NOT run the template preflight: the
  Tailscale key is not among reinit's planned ops (the broken-node rejoin has its own documented
  conditional-need late resolve), and preflighting a secret no op needs would fail installs that
  legitimately run reinit without a key configured. (4) Doctor's per-site preflight rows are
  severity-split: bundled sites report `info` when their local tooling is absent (normal for the
  host), while operator-declared sites report `warn`. The review round ratified all four (test pins
  added for 1 and 4) and two more decisions were recorded from its findings: (5) `bind_platforms`
  wholesale-fails a mixed-health batch when ANY distinct site's preflight fails: contract-consistent
  (preflight everything before anything real) and confirmed intended; a partial-batch degrade would
  act on some VMs while reporting failure, which is worse than failing clean. (6) The runtime
  env-chain resolve was initially deferred and then, on maintainer direction (the harness adoption
  needs the single seam), FOLDED in this PR: `bind_platform` takes `targets=` (the command's
  `SecretTarget`s, registered on the resolver via `compute_needed_secrets`) so `shell_vm` /
  `exec_vm` / `shell_agent` / `exec_agent` / `create_session` resolve site secrets AND env-chain
  secrets in the ONE boundary pass: one prompt session per command, pinned end to end
  (`test_env_targets_join_the_site_secret_pass`). Three recorded exceptions keep their own resolve
  timing, each with a rationale comment at the site: `restart_session` resolves its env chain after
  the BROKEN/--force refusal and the "Restart?" confirm (bail-before-prompt: a declined restart must
  not prompt for secrets it was about to discard); the console attach/restore build paths resolve
  conditionally on live tmux state discovered post-bind (conditional need, the rejoin's class); and
  `console add-sessions` / `add-shell` bind no platform at all (pure Tailscale live-sync), so their
  env resolve IS the operation's one session by construction.
- **2026-07-13, PR review round (the other workstream's dev): three fixes, one az-now/prox-defer,
  and the recorded deferrals.** Verdict "merge-ready, and genuinely excellent" with seven ruled
  items. Fixed: (1) `create_vm`'s two create/init spans gained the `UserAbort` re-raise carve-out
  (latent, not live: no prompt lives in those spans, but inconsistent with delete/describe/reinit);
  (2) `describe_vm`'s backend reads (`display_backend_name` / `status`) degrade under the same typed
  guard as the bind, so a live backend flake renders '-' instead of crashing the report; (3)
  `compute_needed_secrets` raises a `ConfigError` (naming the env var, target label, and secret) on
  a referenced name with no registry declaration instead of silently dropping it; a miss violates
  the auto-declare-at-finalize invariant and used to surface as a mysterious downstream resolve
  failure (regression test added; no legitimate path relied on the drop). **(4) reversed by
  maintainer ruling, same day: preflight's ceiling is structural.** The review's azure credential
  read briefly landed and was then REMOVED: verifying credentials before the resolve/credential
  stage forks readiness on where a secret happens to come from (a non-interactive chain is
  probeable; the browser-login fallback can't be probed without BEING the interaction), which is
  complexity without a principled line. The ruling, now pinned in capability-model.md's preflight
  section: preflight does what unresolved-secret, read-only checks can do (tools present, mappings
  predicted, unauthenticated reachability) and nothing more; every check past that ceiling
  (credential probes, authenticated reads, the once-mooted proxmox API-reachability follow-up and
  dry-run tier) is the OP's job, surfaced through the op's own typed, actionable error handling
  (azure's `_wrap_azure_error` is the pattern). azure and proxmox therefore deliberately keep the
  base preflight only. **Recorded deferred follow-ups (carry into locked.md at merge):** (a) The
  consoles/restart entrypoints are prompt-once-PER-BOUNDARY, not prompt-once (the exceptions in the
  note above; the no-prompt-before-preflight invariant holds everywhere). (b) `agent create` remains
  two prompt sessions (site secrets at bind, git tokens via `_collect_git_tokens`); the fold rides
  the git-credentials capability adoption (#167), not this PR. Also ratified: the idempotency marker
  stays test-enforced (semantic property; the behavioral guard suite is the enforcement).
- **2026-07-13, host-support gating: platforms self-report; bundled sites and the site fallback
  reshape; templates lose `site`.** Trigger: the maintainer's doctor on a lima-less Linux host
  showed preflight-noise rows for the unconditionally-bundled `lima`/`wsl2` sites: sites the host
  would never use (also the impetus for the reviewer rubric's new environment-diversity check). Two
  design iterations (a `[system].enabled_platforms` config knob was designed, partially built, and
  DISCARDED mid-build) landed on the cleaner model: the knowledge lives on the platform class. Two
  registration-time classmethods, both pure/fast/config-free and deliberately NOT preflight:
  `unsupported_reason()` (can any configuration of this platform ever run here; a non-None reason
  disables the platform wholesale: no capability row, nothing may reference it, doctor lists it as
  installed-but-disabled) and `bundled_site_unsupported_reason()` (should the zero-config bundled
  site publish; operator-declared sites are never gated by it). The split is load-bearing: lima the
  platform is supported everywhere (remote-Lima runs limactl on the vm_host over SSH), but
  `lima-local` (the bundled site's NEW name; "lima" conflated the platform with one configuration of
  it) needs a local limactl. wsl2 is categorically Windows-only, so the whole platform gates.
  Consequences: `build_registry` gains a pre-finalize guard so a declared site on an unsupported
  platform fails with the platform's stated requirement (the framework's generic reference-miss
  can't say "requires Windows"); `lookup_site` gives a bundled-site miss the requirement hint
  instead of the misleading paste-a-manifest hint (covers limactl uninstalled after VMs existed);
  the v27 migration writes `lima-local` for local-lima rows (unreleased, so no compat shim ever
  exists) and the remote-host `-host` suffix rule reserves bundled-site names too; the deprecated
  `defaults.platform` alias translates old `lima` to `lima-local`. Site selection drops BOTH the
  hardcoded lima fallback AND the vm-template `site` field (a template describes WHAT a VM is;
  placement is host/operator-scoped, and a shared template must not smuggle a per-host placement
  decision, reversing the Phase 1 design): `--site`, then `defaults.site`, then the house model over
  declared sites (infer exactly-one silently; several prompt interactively; non-interactive errors
  naming the options). Doctor's "VM platforms" group now derives from the same two classmethods (ok
  / installed-but-disabled-with-reason / enabled-with- bundled-site-note) replacing the raw
  tool-presence rows. Pinned by `test_platform_support.py` (gating end to end incl. the friendly
  error and the remote-site-survives-missing-limactl split), the select_site model tests, and the
  bundled-miss hint test; test scaffolding gains `stub_platform_support` / `publish_all_platforms`
  so shape tests are host-independent by construction. The review round (the rubric's new
  environment-diversity check's first outing) caught two blockings, both closed: the `--site` help
  text still taught the removed model, and `validate_sites` lacked the bundled-miss requirement hint
  (`defaults.site = "lima-local"` with limactl missing said "declare that site", a reserved name).
  Also from the round: bundled-site names are reserved UNCONDITIONALLY via a pre-finalize check (the
  registry's reserved-override only fires when the bundled row publishes, so a limactl-less host
  could otherwise squat `lima-local` and collide the moment the tool is installed); the real support
  classmethods are tested on both branches via `sys.platform` / `shutil.which` patching, not only
  stubs; the v27 reserved-name set is FROZEN as a literal (a migration's output must not change when
  later builds add platforms); and one ratification: a declared site on an unsupported platform
  fails EVERY command, not just VM commands (per the maintainer's original hard-failure ruling: a
  resources dir shared across hosts must diverge per host rather than half-work; the error's hint
  says so).

- **2026-07-13, doctor report polish (maintainer-directed).** Four presentation changes, no check
  semantics touched. (1) Row rendering switches from `name (message)` to `name: message`; the paren
  wrap fought messages that naturally want parens for asides, nesting them
  (`Schema (up to date (version 27))`); parens now appear at most one level, inside messages.
  Platform reason strings stay paren-free as policy so every composed surface (doctor rows, the
  bootstrap hard-failure, the bundled-miss hints) stays single-depth: wsl2's reason is now "Windows
  only", lima's "limactl not installed". (2) Group order decouples from which checks need config:
  the config/registry pair loads up front and each group renders in presentation order (System,
  Python, Required tools, Tailscale, VM platforms, VM sites, Configuration, Secrets, Database,
  completions; Tailscale moved above the VM pair by a later same-day tweak), putting the adjacent VM
  pair early per the maintainer's "VM stuff is fundamental". When config fails to load, VM sites
  renders a skipped-pointer row (the group now precedes the Configuration group that explains the
  failure; silent absence would read as "no sites"). (3) The system slug moves out of "VM sites"
  into a new leading "System" group: it namespaces install-wide (hostnames, backend-side names, the
  managed SSH config), not per-site; same pending-migration-defers guard. (4) Row names drop the
  `platform:` / `vm-site:` prefixes (the group header already says it) and the lima row drops the
  `enabled (...)` wrapper (`[ok]` is the enabled signal; the bundled-site note is the whole
  message).

- **2026-07-13, the disabled-resource model: sites register unconditionally and self-disable.**
  Maintainer-designed refinement superseding the same-day host-support gating (existence and
  availability are separate axes; the gating conflated them). The generic surface: any resource may
  answer "do you have what you need to run?" via `disabled_reason() -> str | None`: a default-None
  method on the `Capability` base and an optional structural hook on `ResourceKind` (the `instances`
  pattern: absent-on-kind = never disabled), surfaced by `resources.inspect.disabled_reason_for`.
  Contract: cheap, offline, host-introspection only; preflight remains the deeper op-boundary check.
  No `site_disabled_reason` platform hook exists (maintainer ruling: "sites aren't special at all");
  the platform's capability INSTANCE implements the generic method (a local-Lima site without
  `limactl`; wsl2's `wsl.exe`), and the vm-site kind derives its chain: platform missing ("not
  installed"; an uninstalled plugin and a typo are indistinguishable by design), platform
  host-disabled (`unsupported_reason`, which still gates the capability row), else the instance's
  answer. Rules: disabled sites still register, list (marked), describe (with reason), and hold
  references; `resolve_site` (the one chokepoint every op passes through) raises a typed
  `StateError` on use; references (VM rows, `defaults.site`) are doctor WARNINGS, never command
  failures (the shared-resources-dir scenario now degrades gracefully, superseding the
  fail-every-command ratification); `select_site` infers/prompts over ENABLED sites only. The site's
  vm-platform reference is emitted only when the capability row publishes, so a missing/unsupported
  platform never trips finalize. DELETED along the way: `bundled_site_unsupported_reason` and the
  `bundled_site` ClassVar, `bundled_site_platform()` / `unsupported_platforms()`, bootstrap's
  pre-finalize hard-error guard AND its unconditional reserved-name check (bundled rows publish
  everywhere now, so the registry's `builtin_override = "reserved"` fires on every host),
  `_bundled_site_miss_reason` and both bundled-miss error branches, and
  `builtin_manifests.publish_to`'s `skip` parameter. Doctor: platform rows carry only platform-level
  state; disabled sites are info rows with the reason (no preflight: pointless without
  requirements); enabled-site preflight failures warn regardless of origin (the old
  bundled-vs-declared severity split is gone: bundled sites that would have preflight-failed are now
  properly disabled instead). Pinned by the rewritten `test_platform_support.py` (register-always,
  reason chains, unknown-platform plugin case, reserved-on-every-host,
  valid-config-with-disabled-default, select-over-enabled, resource list/describe surfacing, real
  instance-method branches) plus the doctor reference-warning tests; `stub_platform_support` now
  pins `unsupported_reason` + instance `disabled_reason`. The other dev's PR review round
  (merge-ready, no blocking) landed four fixes: `create_vm` now applies the extracted
  `ensure_site_enabled` guard right after `lookup_site`, BEFORE the Tailscale check and the
  interactive system-slug prompt (the operator must never answer a prompt for an op the site already
  sank; pinned by an ordering test); the `--site` help text says ENABLED (matching `select_site` and
  the README); a host-disabled site now claims NO edges at all: the config-implied secret edges are
  gated with the platform edge, so doctor never predict-resolves a secret for a site that can never
  run here (pinned against the first plugin shipping a host-gated platform with a config secret);
  stale lima-vs-lima-local comments swept.

- **2026-07-13, the azure platform is named `azure-vm`.** Maintainer ruling: the capability is the
  Azure Virtual Machines service specifically, and Azure could plausibly offer other services worth
  backing platforms with someday. Consumers reference SITES, so compat needs no shims: the legacy
  `[azure]` TOML section still declares a site NAMED `azure` (existing VM rows and `defaults.site` /
  `defaults.platform = "azure"` values keep resolving) with platform `azure-vm` underneath, and the
  migrator emits the platform from `_LEGACY_SITE_SECTIONS` (one source of truth with the loader)
  while the site keeps the section name. The unreleased v27 migration updates in place: the pre-DDL
  validation/backfill now goes through a FROZEN legacy-name -> class map pinned to classes directly
  (`"azure" -> AzureVMPlatform`), so platform renames can never break the backfill and platform
  additions can never loosen the corruption check; `azure-vm` joins the `-host`-suffix reserved set
  (`azure` stays: the legacy site owns it). Module/class follow the name: `azure.py` ->
  `azure_vm.py`, `AzurePlatform` -> `AzureVMPlatform`. `AGENTWORKS_PLATFORM` now reads `azure-vm` on
  those VMs. `proxmox` is deliberately untouched (already the service name).

- **2026-07-13, manual-stop UX (from the maintainer's live install test).** Three fixes. (1)
  Latency: `ensure_active`'s Tailscale fast-path probe burned its full 5s timeout against a stopped
  VM just to reach the refusal; when the caller's row already says manually stopped, the gate now
  asks the backend directly and skips the probe (an out-of-band start still proceeds via the
  observed RUNNING: the flag is intent, not observed state, and both directions of the
  concurrent-start/stop race are re-read-guarded and pinned). Accepted trade in the flag-set path:
  ops now require a successful backend `status()` where the old path could proceed on the Tailscale
  ping alone, so a manually-stopped-then-out-of-band-started VM with an unreachable backend fails
  with the backend's error instead of proceeding, a narrow corner recoverable via `agw vm start`
  (which clears the flag), and the honest reading of a state where the operator's intent flag and
  the world disagree. (2) Vocabulary: "manually stopped" everywhere the OPERATOR reads it (the
  internal `operator_stopped` column keeps its name): the gate error is "VM 'x' was manually stopped
  so it will not be auto-started", describe's status annotation is `stopped (manual)` vs `(idle)`.
  (3) `vm stop` on an already-stopped VM no longer conflates auto-stop with explicit stop: an
  idle-stopped VM reports "had already stopped on its own; it is now marked manually stopped and
  will not be auto-started" (the command DID change something), and only an already-manually-stopped
  VM gets "is already manually stopped".

- **2026-07-13, the proxmox secret is named `proxmox-token`.** Maintainer ruling:
  "proxmox-token-secret" was redundant on every surface (`secret/proxmox-token-secret`,
  `AW_SECRET_PROXMOX_TOKEN_SECRET`). The auto-declared default is now `proxmox-token` (env-var
  convention `AW_SECRET_PROXMOX_TOKEN`); the `platform_config.token_secret` override key is
  unchanged, and the proxmox guide's compat example (mapping the secret to the released
  `PROXMOX_TOKEN_SECRET` env var via `backend_mappings`) deliberately stays: that name is not
  redundant, just a different word order, and it is the released vocabulary.

- **2026-07-13, a blank slug answer is final: the shared-backend nudge is removed.** Maintainer
  ruling from the live install test: blank is a perfectly valid system slug ("no slug"), and
  answering it must not lead to any further prompting. The settings encoding already differentiated
  declined ("" row) from never-asked (absent row); the offender was R4's deferred shared-backend
  nudge, whose ONLY interactive trigger was exactly the declined state (an absent row prompts the
  full question first). Deleted with everything that existed solely to drive it:
  `_nudge_shared_backend_slug`, the `never-remind-me` suppression settings key,
  `site_shared_backend`, and the `shared_backend` classmethod on the base and all four platforms
  (`_resolve_system_slug` loses its `asked_now` tuple return: the nudge-skip was its only purpose).
  `vm describe` now also renders the two null states distinctly: declined shows `(none)`,
  never-asked shows `-` (doctor already differentiated). The FRD's R4 nudge bullet is struck with a
  pointer here.

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
      unknown keys, lima vm_host, proxmox token_secret reference + default, azure required keys); it
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
      construction: nothing may depend on it succeeding). As built, `context.legacy` carries the
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
        value outside the map would be a genuine corruption: fail loudly, don't guess).
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
- [x] Tests: `cli/tests/test_db_migration_vm_sites.py`: fixture DBs at the prior schema version
      covering all four platforms plus a remote-Lima row (and a platform-name-shadowing host);
      assert backfilled metadata shapes, the hostname backfill, the site rename, the printed
      snippets, the NOT NULL rebuild, empty-`legacy` behavior (proxmox node omitted), the
      unknown-platform loud failure, and settings-table creation. Existing test seeds move to the
      new row shape (the shared VM seeding lives per-file, not in `conftest.py`).

**Definition of done**: a pre-SDD database opens cleanly and lands on the new schema with correct
data; row helpers and fixtures compile against the new shape. MET, and as with Phase 1, the
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

- [x] `cli/agentworks/cli/commands/vm.py`: `--platform` becomes `--site` (static Choice removed;
      validation at dispatch via `lookup_site`); `vm shell --provisioner` becomes boolean
      `--platform` with `--provisioner` as an alias for one release (deviation: click renders both
      names in help (it has no per-alias hiding), so the alias is visible rather than hidden); help
      text sweep. `create_vm`'s service-layer kwargs follow (`platform` -> `site`; the Phase-2
      `--vm-host` bridge error deletes with the flag).
- [x] `cli/agentworks/cli/commands/vm_host.py` + `cli/agentworks/vm_hosts/`: removed (the last
      PHASE-2 BRIDGE retires; `git grep vm_hosts` is now clean outside SDDs and the migration).
- [x] `cli/agentworks/completions/spec.py`: `("vm.create", "site")` maps to a `sites` completer
      (sourced from `agw resource list --kind vm-site --names-only`, splitting `vm-site/<name>` like
      the template completers); `vm_host` entries removed; all three shell generators (bash, zsh,
      powershell) gain the `sites` renderer and drop `vm_hosts`. `resource migrate` selector
      completion picks up vm-site automatically through `_MIGRATABLE_KINDS`.
- [x] `agw doctor`: new "VM sites" group: declared vm-site rows, the system slug (set / declined /
      unset), and every `vm.site` resolving to a declaration (stranded rows fail with the
      paste-ready manifest snippet as the hint).
- [x] `cli/agentworks/sample-config.toml`: `[azure]` / `[proxmox]` examples replaced by a pointer at
      `agw resource sample vm-site` (with the migrate command and the token secret's AW*SECRET* env
      var named); `defaults.site` documented with the deprecated `defaults.platform` alias noted;
      `vm_templates.*.site` documented.
- [x] Migrator: `_MIGRATABLE_KINDS` includes vm-site (the deferred Phase 1 item), the one
      multi-section kind: flat `[azure]` / `[proxmox]` sections discover as whole-section units
      (section name = resource name), emission nests platform-owned keys under
      `spec.platform_config` with pre-write capability validation (git-credential precedent), and
      the whole section comments out with the migrated-to marker.
- [x] Tests: `test_completions.py` (vm-host group removed from the pinned set), new
      `test_vm_cli_surface.py` (renamed flags, removed flags/group, `--provisioner` alias, doctor
      VM-sites rows incl. the stranded hint), `test_sample_config.py` (azure/proxmox gone),
      `test_resource_migrate.py` (vm-site in the golden --all run; flat-to-nested emission; by-name
      selector; stray-key refused pre-write with registry-equivalence verification).

**Definition of done**: full CLI surface matches FRD R13; completions regenerate cleanly. MET (suite
1558, ruff, mypy green).

## Phase 6: Tests, docs, release notes, PR

- [x] Full pytest; no regressions vs the pre-branch count (1561 vs ~1490 pre-branch).
- [x] `ruff` / `mypy` package-wide; `./scripts/lint-files.sh`.
- [x] `docs/guides/resources.md`: vm-site and vm-platform join the kind story (new "VM sites and
      platforms" section: manifest shape, reserved built-ins, config secrets, migrate pointer);
      `cli/README.md`: the vm-host section becomes the vm-site story, `--site` / slug / shell
      `--platform` documented, both new kinds in the settings-vs-resources inventory; ADR 0016 gains
      an implementation note that the sketched pair has shipped. Per maintainer direction
      (2026-07-12), permanent artifacts carry NO SDD references: the vm-sites SDD citations and bare
      R-number requirement IDs in code/test comments were replaced with self-contained descriptions
      (other efforts' pre-existing SDD references left as-is, out of scope).
- [x] Release-notes text: the `!`-flagged breaks with remediations live in the PR description
      (release-please derives the changelog from the `!` commits themselves).
- [x] Hardening candidate (from the Phase 2 review round): the migration runner commits each version
      as a durable checkpoint (with a per-version foreign_key_check), so retry is safe for
      multi-version jumps; pinned by a v25-fixture jump test (v26 checkpoints despite v27's designed
      failure, retry resumes at v27).
- [x] Hardening candidate (from the Phase 3 review round): RESOLVED as carve-out: `delete_agent` /
      `delete_workspace` accept the caller's bound platform and the ephemeral rollback threads it,
      same shape as the create side.
- [x] PR #169; agentworks-reviewer rounds ran per phase (recorded in the sequencing notes above)
      plus a final whole-branch round.

**Definition of done**: PR open, CI green, reviewer findings addressed. `locked.md` lands after
merge per the SDD lifecycle.

## Phase 7: Capability model adoption

Added 2026-07-12 (see the sequencing note). Adopts the `capability-model.md` contract for the
vm-platform/vm-site pair; the git-credentials and session-harness PRs adopt for theirs.

- [x] `capabilities/` subtree: the instance-scoped `Capability` base (identity, `validate_config`
      default, the construct/preflight contract, the per-op idempotency marker) at the top;
      `vms/platforms/` relocates to `capabilities/vm_platform/`. The already-merged `secret-backend`
      capability moves in under its own change, not this PR. As-built extras: `vms/base.py` (the
      ABC), `bootstrap_script.py`, `cloud_init.py`, and `skel.py` moved too (they are platform-side
      provisioning machinery; leaving them would have made the capability import the domain), and
      `VMPlatformEntry` moved out of `vms/kinds.py` for the same reason.
- [x] `Resolver`: thin adapter over the existing machinery (`resolve_secrets` for the one pass, the
      `would_attempt` chain via `preview_resolution` for prediction, the lookup-or-synthesize decl
      fallback absorbed as `register_name`). Accumulates participating resources' decls; `predict()`
      non-prompting; `resolve()` once at the preflight boundary, idempotent, with a loud guard
      against post-boundary registration (a second prompt session); strict cached `get()` (an op
      must never trigger resolution).
- [x] Constructor flip: `cls(site_name, platform_config, resolver)`; `resolve_site` / `platform_for`
      / `bind_platform` lose `secret_values` (the attribute is gone; proxmox ops read
      `resolver.get`); binding no longer resolves or prompts; construction auto-registers the
      declared config secrets on the resolver. `VMPlatform` extends the base; `site_name` /
      `platform_config` are domain-vocabulary properties over the generic base attributes.
- [x] `preflight` implementations: lima (local `limactl` present; remote sites defer to ops: an SSH
      probe is a real round trip), wsl2 (`wsl.exe` present), proxmox and azure (base only: both
      would need resolved credentials to check anything more, which is past preflight's structural
      ceiling per the 2026-07-13 ruling; their failures surface at the op with typed errors); plus
      `preflight_vm_template` predicting the Tailscale key resolves (the lookup-or-synthesize
      fallback now lives in `Resolver.register_name`).
- [x] Service-layer reorder (create, reinit, rekey, delete, and the gate-using paths): bind, then
      preflight all participating resources (either order), then the one union resolve pass, then
      ops. `_collect_secrets` dissolved into the resolver flow (create and reinit fold git tokens
      into the boundary pass via `_register_git_token_decls`, ending their second prompt session);
      `bind_platform(prepare=True)` runs the boundary itself so the ~20 single-VM roots stay
      one-liners; `bind_platforms` shares ONE resolver across sites (prompt-once now holds across a
      mixed-site batch, not just within one site). `delete_vm` keeps never-gates and gains the
      op-level `UserAbort` re-raise carve-outs. The rejoin path stays a documented conditional-need
      late resolve.
- [x] Idempotency flags on the `VMPlatform` ABC's ops (`start` / `stop` / `delete` flagged with
      docstring notes; `create` deliberately unflagged: its collision check makes a re-run a loud
      error).
- [x] Doctor: the VM-sites group calls each declared site's instance `preflight` for its health rows
      (read-only by contract, so doctor-safe). Bundled sites whose local tooling is absent report
      `info` (no WSL on macOS is normal); operator-declared sites report `warn` (that failure is the
      error their next command hits).
- [x] Tests: constructor/bind reshapes across the vms suites; `test_capability_base.py` (construct
      validates, secret registration, base preflight prediction, idempotency markers through
      overrides) and `test_secrets_resolver.py` (predict, one-pass idempotent resolve, strict get,
      late-registration guard); prompt-order pins (a failing preflight prevents the resolve pass;
      one pass per single- and mixed-site batch; the boundary resolve precedes the DB insert); the
      delete-abort regression covering both best-effort op spans.
- [x] agentworks-reviewer rounds: round 1 (no blocking findings; four important ones: the vm roots'
      prompt-before-preflight ordering, add_git_credential's second prompt session, describe's
      too-narrow degrade, two missing ruling pins; all fixed, and the four implementation rulings
      ratified); round 2 (one important: the AGENT shell/exec roots had the same ordering bug;
      fixed, plus proxmox/wsl2 op guards and the ordering/guard pins); round 3: **approve, Phase 7
      passes** (two non-gating minors, the exec_agent ordering pin and azure's
      idempotent-by-construction comments, closed in the final commit).

**Definition of done**: the vm-platform/vm-site pair conforms to `capability-model.md` end to end;
full gates green; reviewer round clean.

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

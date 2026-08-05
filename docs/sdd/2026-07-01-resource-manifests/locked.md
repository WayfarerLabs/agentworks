# Resource manifests -- Lockfile

## 2026-07-05

The lock takes effect when PR #156 merges (maintainer ruling: a lockfile written on the branch is
intent, not the lock). Until then, changes on the branch are "pre-lock" and the artifacts -- this
file included -- remain mutable.

The resource-manifests SDD shipped on one branch and PR (single-branch delivery per the 2026-07-02
sequencing note): `feat/resource-manifests-sdd`, PR #156. Phases 0 through 5 and the pre-lock Phases
5.5 (the capability collapse), 5.7 (the capability config-validation contract), and 5.8 (domains own
their kinds) are complete; every plan checkbox except Phase 6's is flipped.

### What shipped

- **Phase 0**: origin and kind vocabulary cleanup (`code-declared` -> `built-in`, lower-kebab kind
  identifiers, `git_credentials` -> `git-credential`).
- **Phase 1**: Config-to-Registry consumer repoint -- all resource reads go through registry
  accessors; resources no longer live on `Config`.
- **Phase 2**: the manifest loader (strict YAML, k8s envelope, decode-through-TOML-loaders parity),
  `Registry.add` collision handling, app-bundled built-in manifests.
- **Phase 3 / 3.5 / 3.6**: the secret provider/backend split, culminating in the
  backends-are-the-door runtime -- resolution is a loop, no resolver object, no caches, prompt-once
  structural, config-is-config. The interim resolver/source machinery built during 3/3.5 was deleted
  wholesale when the maintainer's rulings landed (see plan.md sequencing notes, which are the honest
  history of three mid-flight design corrections).
- **Phase 4**: `agw resource migrate` (recurring incremental mover: selectors or explicit `--all`,
  three layouts, append-only YAML, comment/delete TOML edit with backup-first ordering, per-run
  registry-equivalence verification with rollback) and `agw resource sample` (fully-commented
  bundled samples, one per manifest-declarable kind).
- **Phase 5**: per-section TOML deprecation warnings (later aggregated), YAML-first sample config,
  and the permanent-doc promotions -- ADR 0016 and `docs/guides/resources.md` now carry everything
  load-bearing; runtime docstrings cite the ADR, not this SDD.
- **Phase 5.5 (2026-07-07, the capability collapse)**: the provider/backend split was dissolved --
  resources reference capabilities directly, many-to-one; the declarable `secret-backend` kind, its
  bundled manifests, and the reserved-name tier were deleted; the capability (protocol
  `SecretBackend`, registry `SECRET_BACKEND_REGISTRY`) took the `secret-backend` kind name as a
  descriptor row, and the "door" metaphor was retired. A same-day follow-up expanded the resource
  definition: capability rows ARE resources, so the classifier became the per-kind `category` field
  (replacing `manifest_declarable`) and a read-only `agw resource kinds` command lists the kind
  inventory. Plugins publish resources of existing kinds, never new kinds. Full ruling chain in the
  plan's 2026-07-07 sequencing notes; ADR 0016 carries the model. Companion doc
  capability-consumers.md (marked SUGGESTION) prototypes consumer schema shapes for the plugin SDD.

A late pre-lock addition (Phase 5.7): the capability config-validation contract -- `validate_config`
returning implied `ConfigReference`s, invoked at blob boundaries and finalize, plus
`SecretBackend.validate_mapping` for the per-secret host; both noted as potentially superseded by
registration-time schemas.

And Phase 5.8 (domains own their kinds): the declared-resource dataclasses and every kind strategy
moved out of `config.py` / `resources/kinds/` into their domain packages; same-day corrections
re-homed AdminConfig to `vms/` (lifecycle over field shape), reframed the manifest envelope's
admin/named-console name gate as no-selector dead-config protection (issue #165 adds the selectors),
and deleted the TOML placeholder rows outright -- undeclared singleton defaults are auto-declared by
the always-materialize pre-step (their origin displays as auto rather than operator-declared at
`config.toml:0`), and `SYNTHESIZED_SINGLETON_KINDS` plus the registry's collision exemption are
gone; `resources/kinds/__init__.py` is a pure registration index and `config.py` keeps only settings
plus the legacy TOML loaders/publisher. Initially deferred to the plugin SDD, pulled in pre-merge on
the maintainer's fan-out rationale: parallel post-merge tracks (VM abstractions, harness) would
otherwise enshrine or diverge the placement pattern.

Four deliberate operator-facing breaking changes, `!`-flagged for release-please: resource names may
not contain `/` (FRD R13); `agw resource migrate` requires selectors (and `agw resource sample` a
kind) or `--all`; and `resource describe` takes a single `KIND/NAME` token (the `/` display-syntax
unification, ADR 0016). Two further `!` commits cover branch-internal secrets surface that never
shipped in a release. Other pre-lock additions: deprecation warnings aggregated behind a global
`--no-deprecations` silencer, and provider-owned configuration nests under `spec.provider_config`
(ADR 0016). See the plan's sequencing notes for detail.

### Permanent homes (the SDD-not-permanent promotions)

- **ADR 0016** -- the two-layer config/resource model (capability kinds included), the vocabulary
  law, resources-reference-capabilities (with the capability naming rule and the graduate-when-real
  clause), the envelope/auto-load decision, dual-path rationale, the slash ban, and the 0013/0014
  mechanism supersession.
- **`docs/guides/resources.md`** -- the operator-facing story.
- **`cli/README.md`** -- settings-vs-resources configuration reference and the command surface.

Nothing under this directory should be load-bearing for day-to-day work; per the SDD lifecycle,
these artifacts are candidates for tombstoning once the dual-path era is old news.

### Not delivered (deliberately)

- **Phase 6** (TOML resource-path retirement + loader-ownership inversion) is recorded in plan.md
  but deferred to an unscheduled future major release. Its checkboxes remain unchecked by design.
- Config-bearing secret backends (e.g. onepassword): per FRD R8 (revised), configuration is
  backend-scoped when one ships; a declarable instance kind returns only on a real multi-instance
  need.

### Follow-ups filed elsewhere

- Pre-existing SDD-path citations in permanent code from OTHER SDDs (worst: `proxmox.py`'s
  operator-facing error embedding a `docs/sdd/` path) await a sweep at tombstoning time -- noted in
  the 2026-07-05 Phase 5 review.
- VM base-image pinning is issue #161 (separate track; surfaced during this SDD's testing but not
  part of it).
- Relocating the declared-resource dataclasses was briefly recorded here as deferred to the plugin
  SDD, then pulled back in pre-merge (same day) on the maintainer's fan-out rationale -- executed as
  Phase 5.8 (see "What shipped" above). Kept for the honest record of the reversal.

### Review history

Every phase went through agentworks-reviewer cycles with findings addressed and verified, plus a
whole-branch review after the design corrections settled, two full review+verification rounds on the
Phase 4 artifacts and implementation (which also relayed four maintainer rulings), a Phase 5 review,
and a Copilot pass (one valid loader-robustness fix). The maintainer manually tested the dual-path
loading, doctor, and migration surfaces against a real config during development.

The FRD, HLA, plan, and LLDs are accurate as-built as of this date; they lock at merge.

## 2026-07-16: vm-template drops `azure_vm_size`

Post-lock follow-up (issue #178, [ADR 0018](../../adrs/0018-azure-vm-size-from-spec.md)). The
vm-template spec field list pinned in `manifest-schema-lld.md` (Per-kind spec schemas) included
`azure_vm_size`. That field is now removed: Azure VMs are sized from the standard `cpus` + `memory`
spec, and the `azure-vm` platform selects the smallest fitting SKU from a built-in catalog (or the
site's `platform_config.vm_sizes` override). The locked LLD is not edited in place (it is a
point-in-time record); this entry is the authoritative note that the vm-template field set is now
`inherits`, `cpus`, `memory`, `disk`, `swap`, `apt`, `apt_packages`, `snap`,
`system_install_commands`, `tailscale_auth_key`, `env`. The `vm create` hardware/admin override
flags were removed in the same change (see the ADR).

## 2026-07-26: secret-name length parity lifted (issue #275)

Post-lock follow-up. The "Name validation parity" note in `manifest-schema-lld.md` (max 30 for the
`secret` kind, "Tightening this uniformly is a candidate follow-up AFTER the migration equivalence
window") gated any cap revision on the migration equivalence window, which is now well past; this
change revises the cap in the opposite direction from the uniform tightening that note sketched
(secrets get their own larger bound because they never derive Linux usernames). The locked LLD is
not edited in place (it is a point-in-time record); this entry is the authoritative note.

The 30-char cap (`MAX_NAME_LENGTH`) exists ONLY because VM / workspace / session / agent names get
derived into Linux usernames on the VM (32-char limit, minus agent-username suffix headroom). Secret
names are never turned into usernames, so that cap was arbitrary for them, inherited purely from
TOML->YAML migration parity. It bit the per-credential default token secret
`git-token-<credential-name>`: a reasonable credential name like `github-fg-wf-agw-tester` (23
chars) yields the 33-char secret `git-token-github-fg-wf-agw-tester`, which failed the 30-char cap
and tripped `agw doctor`.

As-built: secret names now validate against `MAX_SECRET_NAME_LENGTH` (253, the k8s DNS-subdomain
ceiling) via a `max_length` parameter on `validate_name`; all character rules (NAME_RE, no
leading/trailing hyphen, no `--`) are unchanged. The username-bearing kinds (and vm-site, which
follows the VM-name rules) keep the 30-char cap. `_load_secrets` is the single validation point for
the secret kind, and the manifest decoder's `_decode_secret` delegates to it, so no other kind is
affected.

## 2026-07-27: name-length caps rationalized per kind (issue #278)

Post-lock follow-up completing the name-length work #275 began. #275 fixed only the secret kind and
left the blanket `MAX_NAME_LENGTH = 30` in place for everything else; that 30 was derived from
nothing and was in fact WRONG for the username / group-bearing kinds (`agt-` + 30 = 34 and `ws-` +
30 = 33 both overflow the 32-char Linux username/group limit). This change removes `MAX_NAME_LENGTH`
entirely and gives every kind a cap derived from its REAL downstream sink. The locked LLDs are not
edited in place (they are point-in-time records); this entry, together with the #275 entry above, is
the authoritative note. The "Name validation parity (max 30)" line in `manifest-schema-lld.md` and
the "vm-site names follow the VM-name rules ... they appear in hostnames and SSH aliases" claim in
the vm-sites FRD are both superseded here.

As-built per-kind caps (all still share the identical `NAME_RE` character rules; only the length
bound varies, via the existing `validate_name(max_length=...)` parameter):

- **agent -> 28**, derived as `LINUX_USERNAME_MAX_LENGTH (32) - len(AGENT_PREFIX)`. Co-located with
  `AGENT_PREFIX` in `agents/manager/_common.py` as `MAX_AGENT_NAME_LENGTH`.
- **workspace -> 29**, derived as `LINUX_GROUPNAME_MAX_LENGTH (32) - len(WS_GROUP_PREFIX)`.
  Co-located with `WS_GROUP_PREFIX` in `agents/grants.py` as `MAX_WORKSPACE_NAME_LENGTH`. Deriving
  the agent/workspace caps FROM the prefixes (rather than hard-coding 28 / 29) means a future prefix
  change cannot silently reintroduce an over-limit identifier; a pinned test asserts the derived
  username/group is exactly 32 at the cap.
- **vm -> 38** (`MAX_VM_NAME_LENGTH`), the MIN over two composed sinks: the OS hostname / Tailscale
  MagicDNS label `{slug}-{vm}` (`63 (DNS label) - 1 (dash) - 20 (max slug) = 42`) and the Azure
  virtual-network name `{slug}-{vm}-vnet`
  (`64 (vnet name) - 20 (max slug) - 1 (dash) - 5 (-vnet) = 38`). The vnet sink binds, so the cap is
  38, not the 63-char hostname and not Azure's 64-char computer-name. The MIN is expressed in code
  (constants for each sink's ceiling in `config/validation.py`; a pinned test asserts the worst-case
  vnet name is exactly 64 at the cap), so a slug-length or `-vnet` suffix change reshapes the cap
  rather than overflowing on Azure.
- **session -> 34** (`MAX_SESSION_NAME_LENGTH`, co-located with `AGENT_SOCKET_ROOT` in
  `sessions/tmux.py`): session names embed in the per-agent tmux AF_UNIX socket path
  `{AGENT_SOCKET_ROOT}/{linux_user}/{name}.sock`, and `sun_path` caps at 108 (107 usable). Under the
  longest possible agent username (`agt-` + a max agent name = the 32-char Linux ceiling) the fixed
  overhead is 73, leaving `107 - 73 = 34`. The prior blanket 64 was unreachable on every user (the
  admin ceiling is 56), so the "no OS-level identifier limit" rationale was wrong for sessions;
  live-measured (107-char path binds, 108 fails). The cap is derived from `len(AGENT_SOCKET_ROOT)`
  in the same module and a pinned test asserts the worst-case path is exactly 107 at the cap, so a
  socket-root change reshapes the cap automatically. (34 is still a loosening from main's blanket
  30, so there is no compatibility concern.)
- **console / vm-site -> 64** (`MAX_FREEFORM_NAME_LENGTH`): these hit no OS identifier limit (tmux
  labels, a registry key, display strings / paths only; a 64-char console name was verified to build
  fine, live). The vm-site comment claiming site names feed hostnames / SSH aliases was incorrect
  and is fixed (VM names, not site names, feed hostnames).
- **secret -> 253** (`MAX_SECRET_NAME_LENGTH`, unchanged from #275).

The `validate_name` default `max_length` moved from 30 to `MAX_FREEFORM_NAME_LENGTH` (64) so a
caller that forgets to pass a cap gets the generous freeform bound, never a silently-wrong OS cap;
the four OS/DNS-bearing kinds (agent, workspace, vm) pass their derived cap explicitly. Tightening
agent 30 -> 28 and workspace 30 -> 29 is intended: the old 30 already produced over-limit usernames
and groups. Separately, the `vm` / `agent` / `workspace` / `console` list tables gained NAME-cell
display truncation (via `output.truncate`) so a long or legacy over-cap name cannot misalign the
table; `--names-only` output is untouched and still emits full names for shell completion.

## 2026-07-31: TOML resource sunset announced (PR #315)

This SDD's dual-path stance ("deprecate, don't break"; removal deferred to a future major release
and explicitly "not a transitional window") is now revised: declaring resources in config.toml is
deprecated for removal in a future release, no longer gated on a major. PR #315 firmed the
aggregated load-time warning to say so, stripped the resource-declaring examples from the sample
config (settings sections remain), and re-homed the field documentation those examples carried into
the bundled YAML samples. ADR 0016 carries a matching status note; a superseding ADR will come from
the follow-on declarative-schema SDD (2026-07-31-declarative-schema), which plans the actual removal
of the TOML resource path (this SDD's TOML loaders, the decode-through-TOML-loaders parity layer)
and a registration-time schema model superseding the Phase 5.7 invoked-validation contract, as that
contract's own docstrings anticipated. Future PRs advancing that effort will append further entries
here as they retire pieces this SDD shipped.

## 2026-08-01: capability config becomes one tagged table (declarative-schema pre-support)

This SDD's manifest spec shape for the three capability-hosting surfaces (vm-site's
`platform`/`platform_config`, git-credential's `provider`/`provider_config`, session-template's
`harness`/`harness_config`, the "provider-owned configuration nests under `spec.provider_config`"
ruling recorded above and pinned in `manifest-schema-lld.md`) is now revised: the canonical shape is
ONE tagged table on the naming field, whose `name` key selects the capability and whose remaining
keys are its config (`platform: {name: lima, vm_host: ...}`; discriminator key `name` by maintainer
ruling). The old sibling shape still loads unchanged but is deprecated for removal: its usage emits
one aggregated deprecation warning (same channel and silencer as the TOML resource-section nudge,
surfaced as a doctor row), mixing the shapes on one resource is a hard error, and
`agw resource migrate` now emits the tagged shape (its registry-equivalence verification is
unchanged: both shapes normalize to the same internal fields at decode). The secret kind's
`backend_mappings` is untouched (its map key already names the capability). The hard error on the
old shape and an in-place manifest upgrade mode for `agw resource migrate` are deliberately NOT in
this change; they land with the follow-on declarative-schema SDD effort (in progress, PR #316) after
a released warning window. The locked LLD is not edited in place (a point-in-time record); this
entry is the authoritative note.

## 2026-08-05: TOML resource path removed (declarative-schema phase 1, PR #316)

The declarative-schema effort's phase 1 (the TOML resource sunset) landed and retires machinery this
SDD shipped. config.toml is now settings only: any resource-declaring section is a hard
`ConfigError` at load, and the aggregated load-time deprecation warning is gone (it became the
error). ADR 0016's dual-path stance is superseded by
[ADR 0022](../../adrs/0022-single-resource-declaration-frontend.md); see that ADR for the permanent
record.

What this SDD shipped that phase 1 now retires:

- **The TOML resource loaders.** Phase 2's "decode-through-TOML-loaders parity" (the manifest
  decoders routing through the flat TOML loaders so the two sources could not drift) is dissolved:
  the decoders now own their per-kind validation directly, and the loaders were relocated out of the
  config-load path into a private migrate module. ADR 0016's matching Consequences bullet is
  corrected in place.
- **Phase 5 per-section TOML deprecation warnings.** Retired with the sections they warned about;
  the `[secret_backends.*]` no-op warning is NOT a resource declaration and stays.
- **`agw resource migrate`'s verification pre-side (Phase 4).** It no longer builds the
  pre-migration registry and diffs it against the post-migration registry. The pre-side is now the
  relocated TOML loaders read as an independent oracle (flat TOML to decl), scoped to the selected
  migration units, compared against a settings-only post-load, plus an emitted-key-set equality
  guard.

What SURVIVES unchanged: the migrator itself and its whole operator-facing contract. Its recurring,
incremental, selector-scoped moves; its backup-first ordering (config.toml backed up, recovery
copies of rewritten YAML under `paths.backups`); its digest/CAS guards; and its rollback on a
verification mismatch all stand. Only the internal source of the verification pre-side changed; the
operator sees the same behavior.

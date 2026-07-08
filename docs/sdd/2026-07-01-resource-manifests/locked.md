# Resource manifests -- Lockfile

## 2026-07-05

The lock takes effect when PR #156 merges (maintainer ruling: a lockfile written on the branch is
intent, not the lock). Until then, changes on the branch are "pre-lock" and the artifacts -- this
file included -- remain mutable.

The resource-manifests SDD shipped on one branch and PR (single-branch delivery per the 2026-07-02
sequencing note): `feat/resource-manifests-sdd`, PR #156. Phases 0 through 5 and the pre-lock Phase
5.5 (the 2026-07-07 capability collapse) are complete; every plan checkbox except Phase 6's is
flipped.

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
- Relocating the declared-resource dataclasses out of `config.py` into their domain packages
  (VMTemplate -> vms/, AgentTemplate -> agents/, ...): deliberately deferred (2026-07-08) -- nothing
  locks module placement, and the plugin SDD reshapes these exact types, so the move is a natural
  plugin-SDD Phase 0 (one touch, with design context for the two dataclasses that lack an obvious
  home: AdminConfig, NamedConsoleConfig). Independent of Phase 6, which retires the TOML LOADERS;
  the dataclasses can move any time.

### Review history

Every phase went through agentworks-reviewer cycles with findings addressed and verified, plus a
whole-branch review after the design corrections settled, two full review+verification rounds on the
Phase 4 artifacts and implementation (which also relayed four maintainer rulings), a Phase 5 review,
and a Copilot pass (one valid loader-robustness fix). The maintainer manually tested the dual-path
loading, doctor, and migration surfaces against a real config during development.

The FRD, HLA, plan, and LLDs are accurate as-built as of this date; they lock at merge.

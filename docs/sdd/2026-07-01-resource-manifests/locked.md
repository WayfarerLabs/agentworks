# Resource manifests -- Lockfile

## 2026-07-05

The resource-manifests SDD shipped on one branch and PR (single-branch delivery per the 2026-07-02
sequencing note): `feat/resource-manifests-sdd`, PR #156. Phases 0 through 5 are complete; every
plan checkbox except Phase 6's is flipped.

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
  bundled samples; the secret-backend sample is prose-only until a config-bearing provider ships).
- **Phase 5**: per-section TOML deprecation warnings, YAML-first sample config, and the
  permanent-doc promotions -- ADR 0016 and `docs/guides/resources.md` now carry everything
  load-bearing; runtime docstrings cite the ADR, not this SDD.

Two deliberate breaking changes, both `!`-flagged for release-please: resource names may not contain
`/` (FRD R13, enforced at `Registry.add`), and `agw resource migrate` requires selectors or `--all`.

### Permanent homes (the SDD-not-permanent promotions)

- **ADR 0016** -- the three-layer config/resource/capability model, the vocabulary law,
  exposed-resources-are-the-door, the envelope/auto-load decision, dual-path rationale, the slash
  ban, and the 0013/0014 mechanism supersession.
- **`docs/guides/resources.md`** -- the operator-facing story.
- **`cli/README.md`** -- settings-vs-resources configuration reference and the command surface.

Nothing under this directory should be load-bearing for day-to-day work; per the SDD lifecycle,
these artifacts are candidates for tombstoning once the dual-path era is old news.

### Not delivered (deliberately)

- **Phase 6** (TOML resource-path retirement + loader-ownership inversion) is recorded in plan.md
  but deferred to an unscheduled future major release. Its checkboxes remain unchecked by design.
- Config-bearing secret providers (e.g. onepassword): the model has the room; the sample and a
  pinned test flip the day one ships.

### Follow-ups filed elsewhere

- Pre-existing SDD-path citations in permanent code from OTHER SDDs (worst: `proxmox.py`'s
  operator-facing error embedding a `docs/sdd/` path) await a sweep at tombstoning time -- noted in
  the 2026-07-05 Phase 5 review.
- VM base-image pinning is issue #161 (separate track; surfaced during this SDD's testing but not
  part of it).

### Review history

Every phase went through agentworks-reviewer cycles with findings addressed and verified, plus a
whole-branch review after the design corrections settled, two full review+verification rounds on the
Phase 4 artifacts and implementation (which also relayed four maintainer rulings), a Phase 5 review,
and a Copilot pass (one valid loader-robustness fix). The maintainer manually tested the dual-path
loading, doctor, and migration surfaces against a real config during development.

The FRD, HLA, plan, and LLDs are accurate as-built as of this date and are now locked.

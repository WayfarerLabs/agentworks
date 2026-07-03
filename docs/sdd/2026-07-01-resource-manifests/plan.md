# Resource manifests: plan

Delivery is a single branch and PR (`feat/resource-manifests-sdd`); phases are sequencing milestones
within it (each ends at a green test suite and a coherent commit series), not separately-merged PRs.
Phases 0 and 1 are pure refactors with unchanged behavior. TOML resource sections keep working
through the Phase 4 commits and are cut over in Phase 5; the dual-source condition exists only
inside the branch and never reaches main or a release.

See [frd.md](frd.md), [hla.md](hla.md), [migration-strategy.md](migration-strategy.md), and
[prior-art-research.md](prior-art-research.md).

## Phase 0: Origin and kind vocabulary cleanup

Standalone, mergeable independently of everything else.

- [x] Rename `Origin` variant `code-declared` to `built-in`; `code_declared()` factory becomes
      `built_in()`. Document `system-plugin` / `external-plugin` as reserved variants in the module
      docstring (not constructible until the plugin SDD).
- [x] Update all origin rendering (`resources/render.py`, doctor, secret list/describe, resource
      list/describe) to the `built-in (<source>)` display shape.
- [x] Update `agw resource list --origin` filter vocabulary to `operator | builtin | auto` (confirm
      current accepted values first; keep the filter's CSV/enum style consistent with
      cli-conventions).
- [x] Kind identifiers move to lower-kebab per FRD R9 (`vm_template` to `vm-template`,
      `secret_backend` to `secret-backend`, ...): `KIND_REGISTRY` keys, reference/origin source
      tuples, `--kind` filter and `agw resource describe` positional values, error message
      templates. TOML section names (`[vm_templates.*]`, `[secret_backends.*]`, ...) are keys, not
      kind identifiers, and are untouched; today-valid configs load unchanged.
- [x] Registry kind `git_credentials` renamed to `git-credential` (singular plus kebab) as part of
      the same sweep.
- [x] Update completions for the new `--origin` and `--kind` vocabularies (confirm whether either is
      enumerated in the completion tree).
- [x] **Tests**: rename-sweep over existing origin and kind tests; prose-scan test
      (naming-consistency style) asserting `code-declared` and the old snake_case kind spellings no
      longer appear in operator-facing strings; regression that the shipped sample config loads
      unchanged.
- [x] **Docs**: update guide/README text that mentions `code-declared` or spells kind identifiers
      (e.g. `agw resource list --kind` examples in `sample-config.toml` comments and
      `cli/README.md`).

Definition of done: no behavior change beyond display strings and CLI vocabulary (`--origin` and
`--kind` values); `code-declared` and old kind spellings absent from operator-facing surfaces; CI
green; reviewer-approved.

## Phase 1: Consumer repoint (Config reads move to Registry)

Pure refactor; TOML remains the source; behavior unchanged.

- [x] **LLD**: [consumer-repoint-lld.md](consumer-repoint-lld.md) inventorying every read of
      `config.secrets`, `config.vm_templates`, `config.agent_templates`,
      `config.workspace_templates`, `config.session_templates`, `config.git_credentials`,
      `config.admin`, `config.named_console`, and catalog-extension fields, with the registry query
      each moves to, plus the eager-template-resolution relocation out of `load_config`.
- [x] Repoint manager/CLI/service call sites to registry lookups per the LLD.
- [x] Relocate eager template resolution from `load_config` to the `build_registry` call path,
      preserving the cycle-guard behavior in the template resolvers.
- [x] `Config` resource fields become internal to the publish path (no external readers).
- [x] **Tests**: existing suites pass unchanged (that is the point); add a guard test that `Config`
      resource attributes have no readers outside `config.py`'s publish path (import- or grep-level
      check, same spirit as the naming-consistency prose scan).

Definition of done: all resource reads flow through the Registry; behavior identical; CI green;
reviewer-approved.

## Phase 2: Manifest loader and built-in manifest mechanism

- [x] **LLD**: [manifest-schema-lld.md](manifest-schema-lld.md) covering the envelope grammar,
      per-kind spec field tables (shared mapping used by loader and migrator), per-kind unknown-key
      strictness as currently implemented (pinned from a full loader survey), error message catalog
      with `file:line` framing, and the YAML library decision (PyYAML 6.0.3, latest stable verified
      via the uv resolver; compose_all mark plumbing).
- [x] Add the YAML dependency to `cli/pyproject.toml` (latest stable at implementation time);
      promote the library's name from this SDD's local cspell dictionary to the root `.cspell.json`
      once it appears in permanent code (skill promotion rule).
- [x] `agentworks/manifests/loader.py`: directory walk (sorted relative paths, dotfile skip,
      `.yaml`/`.yml`), YAML stream parse with document start-line capture, empty-document skip.
- [x] `agentworks/manifests/envelope.py`: apiVersion / kind / metadata / spec validation;
      `manifest_declarable` kind flag; singleton kinds restricted to `name: default`.
- [x] `agentworks/manifests/decode.py`: spec-to-Resource construction for every operator kind,
      reusing existing per-kind validation; `declared_at` attachment. (Decoders call the TOML
      loaders through a fixed-location shim, so validation is shared verbatim; `secret-backend`
      deferred to Phase 3 per the sequencing note.)
- [x] Cross-document duplicate detection with both locations in the error.
- [x] `ManifestSet.publish_to(registry)`; bootstrap gains the manifests publisher alongside the
      still-active config publisher (dual-source until Phase 5). `Config` gained a `source_path`
      field so the resources directory resolves relative to the loaded config file (test isolation
      from the developer's real manifests).
- [x] `Registry.add` duplicate semantics: replace today's silent last-writer-wins with explicit
      collision handling (operator-vs-operator collisions error citing both origins;
      operator-vs-built-in consults the kind's `builtin_override` flag). This is what makes a
      resource declared in both TOML and a manifest an error during the dual-source window. (Line-0
      sentinel rows -- omitted-singleton defaults and the legacy catalog publisher -- are
      replaceable by a real operator declaration; the looseness dies with the TOML publisher.)
- [x] `agentworks/manifests/builtin.py`: app-bundled manifest discovery via importlib.resources;
      published with `built-in` origin. Ship an empty-but-wired bundle (first content arrives in
      Phase 3).
- [x] `ResourceKind` gains `manifest_declarable` and `builtin_override` flags; `Registry.add`
      enforces the built-in override policy (allow for catalog kinds, reserved otherwise;
      `secret-backend` temporarily "allow" for the TOML dual-source window, flipped in Phase 5).
- [x] **Tests**: loader walk order and dotfile skip; envelope acceptance/rejection per rule;
      per-kind decode round-trips against TOML-parser equivalents (same Resource out of both
      sources); duplicate detection (same file, cross-file, cross-source); built-in override allow
      and reserved paths; singleton name restriction; `file:line` accuracy on multi-doc files.
- [x] **Docs**: none yet (operator surface unchanged until cutover).

Definition of done: a resources directory fully declares any operator kind with feature parity to
TOML; both sources coexist correctly; CI green; reviewer-approved.

## Phase 3: Secret provider/backend split and git credential alignment

- [x] **LLD**: [provider-config-lld.md](provider-config-lld.md) covering the `SecretProvider`
      protocol (`validate_config`, `instantiate`), the test-only provider that exercises config
      validation, error framing for provider-config violations (manifest `file:line`), and the
      resolver construction swap (registry-derived `resolver_for` with `id(config)`-keyed
      prompt-once identity; parse-time chain validation relocates to first `resolver_for`,
      Config.secret_resolver retired).
- [x] `agentworks/secrets/providers.py`: code-side `PROVIDER_REGISTRY` (env-var, prompt) and the
      `secret-provider` descriptor kind + publisher (built-in origin, error miss policy, not
      manifest-declarable).
- [x] `SecretBackendDecl` resource (name, description, provider, provider config mapping);
      `referenced_resources()` emits the `secret-provider` reference; `secret-backend` kind becomes
      manifest-declarable with `builtin_override = "reserved"` (enforced for manifest-declared rows
      only, at `ManifestSet.publish_to`; legacy TOML `[secret_backends.*]` rows keep today's
      override-allowed publish until Phase 5).
- [x] Built-in `secret-backends.yaml` bundled manifest (env-var and prompt backends, no
      configuration); `secrets.publish_to` became the provider-descriptor publisher.
- [x] Built-in providers accept no configuration (non-empty backend config is a schema validation
      error for both); the provider-config plumbing (schema validation, defaults, `file:line` error
      framing, config reaching `instantiate`) is exercised end to end by a test-only provider
      registered only in the test suite, never shipped in the app.
- [x] Resolver construction from the chain: `secret_config.backends` names looked up in the
      registry; `SecretBackendDecl` rows instantiate via their provider, legacy TOML rows continue
      to resolve (their kind IS the provider name; the kind-keyed path retires in Phase 5 with the
      TOML resource surface). Shipped as `providers.resolver_for(config, registry=None)` per the
      LLD, replacing `Config.secret_resolver`, with the chain-name and unreachable-secret checks
      relocated from load time (unreachable restricted to operator-declared rows for parity).
- [x] `git_credentials.<name>` entries gain `provider`: TOML parse accepts it as an alias for `type`
      (`provider` wins when both are present), so every today-valid config still loads at this
      phase; manifests accept only `provider`; `type` is removed with the TOML resource surface in
      Phase 5.
- [x] Inspection follow-through: `agw secret describe` backend mappings / resolution preview and
      doctor rows compute conventions via instantiated sources (through `resolver_for`);
      `agw resource list` shows `secret-provider` and `secret-backend` rows with references.
- [x] **Tests**: provider registry lookup and instantiation; test-only-provider config validation
      and resolution end to end; custom backend in chain; reserved-name rejection for
      `env-var`/`prompt` operator manifests; multiple backends sharing a provider; chain naming an
      unknown backend; describe/doctor rendering; regression: the shipped sample config and a
      maximal today-valid TOML config (including `type =` and `[secret_backends.*]` sections) load
      unchanged at this phase's HEAD. (tests/secrets/test_providers.py plus updated legacy pins;
      prompt-once identity pinned directly.)
- [x] **Docs** (lockstep with what becomes true at this phase's HEAD): `cli/README.md` configuration
      schema and command reference for the new `secret-provider` / `secret-backend` rows,
      describe/doctor rendering, and the `provider` alias on `[git_credentials.*]`;
      `sample-config.toml` comments where they mention `type`.

Definition of done: chain-driven resolution runs entirely through provider-instantiated backends;
built-in backends ship as bundled manifests; git credential `provider` field aligned; CI green;
reviewer-approved.

## Phase 3.5: Registry-purity revisit (maintainer-directed)

Design revisit against the resource-registry invariant (config publishes, registry validates,
runtime reads the registry): YAML manifests must be "just another publisher", and anything more
complex is a leaky abstraction. The audit found the resolver assembly doing graph validation the
registry should own, rooted in `[secret_config]` not being published.

- [x] Framework: optional `validate(registry)` hook on `ResourceKind` (getattr-gated, like
      `instances`); `Registry.finalize` runs each kind's hook over the complete acyclic graph
      immediately before freeze. Home for cross-resource SEMANTIC validation that referential
      integrity can't express.
- [x] `secret-config` kind (singleton, `auto-declare` reserved name `default`, NOT
      manifest-declarable): `SecretConfig.referenced_resources()` emits one `secret-backend` edge
      per chain entry; `Config.publish_to` publishes `secret-config:default` (TOML stays its only
      home -- publishing is not moving). Chain-name validation collapses into the framework's error
      miss policy; the bare-registry sentinel (empty chain, always-materialize origin, hooks skip
      it) keeps hand-built test registries finalize-clean.
- [x] Reachability + provider instantiation move from resolver assembly to the kind's
      `validate(registry)` hook (delegating to `providers.validate_chain`); operator-declared-only
      filter preserved.
- [x] `resolver_for(registry)` is registry-pure: a plain projection of the published chain onto
      instantiated sources, memoized per Registry; the `config` parameter is gone. Deep call paths
      (`secrets/orchestration.py` -- whose entry points now take a registry --, `env/show.py`, the
      manager `compose_env(resolver=...)` sites, `_collect_git_tokens` / `_collect_secrets` /
      `_collect_agent_credentials`) receive the registry from their command entries.
- [x] **Tests**: chain-unknown and unreachable errors pinned at `build_registry` finalize; the
      secret-config kind (published row, default-chain edges as usage on built-in backend rows,
      bare-registry sentinel); manual-publisher-equivalence updated to the full publisher set;
      orchestration/describe/collect tests repointed to registry-first signatures.
- [x] **Docs** (this phase's HEAD): FRD R1 note, HLA layer-changes + pseudocode + validation table,
      provider-config LLD swap section rewritten as built.
- [x] Framework (reviewer round): optional `miss_hint(name, references)` hook on `ResourceKind`
      (same getattr gating); the error-miss-policy `ConfigError` includes the reference's usage in
      its message and the kind-supplied hint. `secret-backend` implements it, restoring the
      `[secret_config].backends` operator vocabulary and remediation that the relocation into the
      generic miss policy had lost. Both hooks pinned by framework-level probe-kind tests.

Definition of done: the runtime never reads resource-graph data from Config after the registry
exists (`secret_config_data` has no readers outside `config.py`); all secret-system validation fires
at `Registry.finalize`; CI green; reviewer-approved.

## Phase 4: Migration tool

- [ ] Add tomlkit dependency (latest stable at implementation time; used only by the migrate path).
- [ ] `agentworks/migrate/`: TOML section split per the FRD R1 table; manifest emission (by-kind
      files, multi-document, declaration order) through the shared field mapping from
      `manifest-schema-lld.md`; renames (`type` to `provider`, `[secret_backends.<kind>]` to
      `secret-backend` documents, empty env-var/prompt sections dropped).
- [ ] Comment-preserving `config.toml` rewrite via tomlkit; timestamped backup of the original to
      the configured backups directory (`paths.backups`).
- [ ] `agw config migrate` command: preview + confirm, `--yes`, `--force`, `--dry-run`; idempotent
      no-op on an already-migrated config.
- [ ] Completions updated for the new subcommand.
- [ ] **Tests**: golden-file migration of a maximal config (every section type, comments in
      surviving sections preserved); rename coverage; refusal without `--force` when manifests
      exist; idempotency; backup creation; dry-run writes nothing.
- [ ] **Docs**: `cli/README.md` command reference entry for `agw config migrate` rides this phase
      (the command is real at this HEAD); cutover-dependent doc changes wait for Phase 5.

Definition of done: a representative real config migrates to a loadable manifest set plus a
config-only TOML with zero behavior change (verified by comparing finalized registries before and
after); CI green; reviewer-approved.

## Phase 5: Cutover, samples, and doc promotions

- [ ] `load_config()` rejects resource sections with a `ConfigError` listing the sections found and
      pointing at `agw config migrate`; `Config.publish_to` shrinks to exactly the `secret-config`
      singleton row (pure config that names resources keeps publishing its edges); TOML resource
      parsing survives only inside the migration tool. The `type` alias and the legacy TOML backend
      construction path are deleted with it.
- [ ] Flip `secret-backend`'s `builtin_override` to `reserved` AND delete the reserved-name shim in
      `ManifestSet.publish_to` in the same commit. The shim exists only because origin variants
      cannot distinguish TOML rows from manifest rows during the dual-source window; with the TOML
      surface gone, `Registry.add`'s collision policy is the sole enforcement -- publishers know
      nothing about kinds (registry-purity revisit).
- [ ] Rewrite `cli/agentworks/sample-config.toml` to config-only, with a pointer to the sample
      manifests; update `cli/tests/test_sample_config.py` conventions accordingly.
- [ ] Ship sample manifests (envelope examples for the commonly-used kinds) and wire
      `agw config sample` to emit them (flag shape per LLD).
- [ ] **Docs (permanent-home promotions, per SDD-not-permanent rule)**:
  - [ ] New operator guide `docs/guides/resources.md`: the config/resource split, the resources
        directory, the envelope, built-in resources and override rules, the provider/backend model,
        worked examples. Standalone; no SDD references.
  - [ ] ADR `docs/adrs/0016-yaml-resource-manifests.md` (number confirmed at write time): auto-load
        YAML manifests with k8s envelope over TOML sections; config/resource split; hard cutover
        rationale.
  - [ ] Sweep existing guides (`mise.md`, `source-refs.md`, `proxmox.md`, `idempotency.md`),
        `cli/README.md` (configuration schema and command reference; the largest doc blast radius of
        the cutover), and the top-level README for TOML-section references to resource kinds; update
        to manifest examples.
- [ ] Re-vocabulary the shared loader messages: once the TOML resource surface is gone, spec-level
      errors and warnings must speak manifest vocabulary (field paths, not `secrets.<name>.*` TOML
      section paths) since the loaders become manifest-only.
- [ ] Release notes: the cutover, the one-command migration, the rename list from FRD "Migration
      notes".
- [ ] Completions: verify the full command tree still round-trips (kind values, new subcommand).
- [ ] **Tests**: resource-section rejection message; sample manifests load clean through the real
      loader; guide/sample examples lint.
- [ ] Housekeeping: confirm the resource-registry SDD's locked docs need no drift note beyond
      "superseded source format; framework unchanged" (its lockfile anticipated this SDD; add a
      dated note there only if reviewers want one).

Definition of done: fresh install and migrated install both work end to end with TOML resource
sections rejected; samples, docs, completions, release notes shipped in the same release; CI green;
reviewer-approved.

## Phase 6: Loader-ownership inversion (post-cutover follow-through)

The dual-source window forced manifests to decode through `config.py`'s TOML loaders (the zero-drift
shim). With the TOML resource surface gone that dependency points the wrong way; this phase inverts
ownership so `config.py` is pure config (registry-purity revisit, leak 4).

- [ ] Manifest decoders (or the kinds) own resource field validation natively; the `_load_*`
      resource loaders and the decode-through-TOML shim are deleted from `config.py`.
- [ ] The migration tool validates by routing raw TOML tables through the manifest decode path
      rather than `config.py`'s resource loaders.
- [ ] `config.py` retains pure-config parsing plus `Config.publish_to` publishing exactly the
      `secret-config` singleton; nothing else resource-shaped remains.
- [ ] Loader messages speak manifest vocabulary natively (subsumes Phase 5's re-vocabulary item if
      that landed as a shim over shared loaders).
- [ ] **Tests**: the decode-parity suite retires with the shim; kind-level validation coverage moves
      to the decoders' own tests.

Definition of done: `config.py` contains no resource-section knowledge; the migration tool is the
only TOML-resource reader in the tree; CI green; reviewer-approved.

## Sequencing notes

(Recorded as they happen, per SDD convention. Deviations from FRD/HLA get an entry here and an
artifact update.)

- **2026-07-02: single-branch delivery.** At the maintainer's direction, all phases land on one
  branch and PR instead of PR-per-phase. Per-phase "reviewer-approved" in the definitions of done
  reads as "commit series complete and suite green"; review happens once on the full PR. Side
  effect: the dual-source window never exists on main.
- **2026-07-03: standard registry becomes a per-config singleton.** Refined at the maintainer's
  suggestion during Phase 3 review: instead of a resolver-only `id(config)` memo assuming all builds
  of one config produce equal rows, `build_registry`'s standard path is memoized per Config object
  and the resolver is memoized per Registry. Prompt-once identity follows from registry identity;
  explicit-`ManifestSet` calls always build fresh (pinned by test).
- **2026-07-03: resolver leaves Config (Phase 3 LLD).** `Config.secret_resolver` cannot survive
  manifest-declared backends (unknowable at `load_config`), so the resolver becomes registry-derived
  (`providers.resolver_for(config, registry)`) with an `id(config)`-keyed memo preserving the
  prompt-once per-command cache identity. Parse-time `[secret_config].backends` chain validation
  relocates to the first `resolver_for` call, the same sanctioned pattern as the Phase 1
  cycle-detection move. `GitCredentialConfig.type` keeps its field name this phase; only the TOML
  alias ships (the operator-surface vocabulary is what R9 requires).
- **2026-07-03: registry-purity revisit (maintainer-directed; Phase 3.5).** The maintainer
  re-examined the whole design against the registry invariant ("these new yaml resource files really
  should just be another way of publishing resources to the registry -- anything more complex
  suggests a leaky abstraction"). Four leaks identified: (1) resolver assembly doing graph
  validation because `[secret_config]` wasn't published -- fixed now (Phase 3.5: `secret-config`
  kind, finalize `validate` hook, registry-pure `resolver_for(registry)`); (2) the reserved-name
  shim in `ManifestSet.publish_to` -- window-forced, its deletion is now coupled to the Phase 5
  `builtin_override` flip; (3) deep runtime paths conjuring the registry from Config -- fixed now
  (registry threaded from command entries; the per-config singleton remains as the command-entry
  seam only); (4) manifests decoding through `config.py`'s TOML loaders -- window-forced, inverted
  in the new Phase 6. This supersedes the "relocates to the first `resolver_for` call" wording in
  the note above: validation now fires at `build_registry` finalize.
- **2026-07-03: name-validation parity, not the FRD's uniform rule.** FRD R3 originally implied the
  resource-name rule applies to every manifest `metadata.name`; the implementation pins TOML parity
  instead (`validate_name` for `secret` only, pass-through elsewhere) so the Phase 4
  registry-equivalence test cannot trip on legitimately-named existing resources. The FRD was
  amended; uniform tightening is a post-cutover follow-up.
- **2026-07-03: secret-backend not manifest-declarable in Phase 2.** The manifest-schema LLD defers
  `secret-backend` manifest declarability to Phase 3: its Phase 2 manifest shape (the bare
  kind-keyed TOML form) would be broken by Phase 3's provider/backend reshape inside the same PR, so
  the kind errors with a pointer until the reshaped spec (`spec.provider` + provider config) lands.
  TOML `[secret_backends.*]` sections are unaffected until the cutover.
- **2026-07-02: Phase 1 fail-fast expansion.** The consumer repoint gives shell-opening and console
  commands (`vm shell/exec`, `agent shell/exec`, `session restart`, the console add/restore/attach
  family, `env show`) a `build_registry` call they previously lacked, because their env-scope
  resolution now reads the registry. Two observable consequences, both accepted: these commands now
  fail fast on any framework config error (previously only resource-provisioning commands did),
  realizing the original resource-registry SDD's "registry construction is universal" intent; and
  each invocation pays one registry build (config-load-scale work, no backend calls). Recorded here
  alongside the cycle-detection relocation as the two sanctioned behavior deltas of the "behavior
  unchanged" phase.

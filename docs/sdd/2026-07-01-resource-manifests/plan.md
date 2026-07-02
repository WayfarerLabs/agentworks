# Resource manifests: plan

Phases are sequenced so each ends at green CI and a usable intermediate state. Phases 0 and 1 are
pure refactors with unchanged behavior. TOML resource sections keep working through Phase 4 and are
cut over in Phase 5; the dual-source condition exists only between merged phases, never in a release
(Phase 5 ships in the same release train as the tool it points at).

See [frd.md](frd.md), [hla.md](hla.md), [migration-strategy.md](migration-strategy.md), and
[prior-art-research.md](prior-art-research.md).

## Phase 0: Origin and kind vocabulary cleanup

Standalone, mergeable independently of everything else.

- [ ] Rename `Origin` variant `code-declared` to `built-in`; `code_declared()` factory becomes
      `built_in()`. Document `system-plugin` / `external-plugin` as reserved variants in the module
      docstring (not constructible until the plugin SDD).
- [ ] Update all origin rendering (`resources/render.py`, doctor, secret list/describe, resource
      list/describe) to the `built-in (<source>)` display shape.
- [ ] Update `agw resource list --origin` filter vocabulary to `operator | builtin | auto` (confirm
      current accepted values first; keep the filter's CSV/enum style consistent with
      cli-conventions).
- [ ] Kind identifiers move to lower-kebab per FRD R9 (`vm_template` to `vm-template`,
      `secret_backend` to `secret-backend`, ...): `KIND_REGISTRY` keys, reference/origin source
      tuples, `--kind` filter and `agw resource describe` positional values, error message
      templates. TOML section names (`[vm_templates.*]`, `[secret_backends.*]`, ...) are keys, not
      kind identifiers, and are untouched; today-valid configs load unchanged.
- [ ] Registry kind `git_credentials` renamed to `git-credential` (singular plus kebab) as part of
      the same sweep.
- [ ] Update completions for the new `--origin` and `--kind` vocabularies (confirm whether either is
      enumerated in the completion tree).
- [ ] **Tests**: rename-sweep over existing origin and kind tests; prose-scan test
      (naming-consistency style) asserting `code-declared` and the old snake_case kind spellings no
      longer appear in operator-facing strings; regression that the shipped sample config loads
      unchanged.
- [ ] **Docs**: update guide/README text that mentions `code-declared` or spells kind identifiers
      (e.g. `agw resource list --kind` examples in `sample-config.toml` comments and
      `cli/README.md`).

Definition of done: no behavior change beyond display strings and CLI vocabulary (`--origin` and
`--kind` values); `code-declared` and old kind spellings absent from operator-facing surfaces; CI
green; reviewer-approved.

## Phase 1: Consumer repoint (Config reads move to Registry)

Pure refactor; TOML remains the source; behavior unchanged.

- [ ] **LLD**: `consumer-repoint-lld.md` inventorying every read of `config.secrets`,
      `config.vm_templates`, `config.agent_templates`, `config.workspace_templates`,
      `config.session_templates`, `config.git_credentials`, `config.admin`, `config.named_console`,
      and catalog-extension fields, with the registry query each moves to, plus the
      eager-template-resolution relocation out of `load_config`.
- [ ] Repoint manager/CLI/service call sites to registry lookups per the LLD.
- [ ] Relocate eager template resolution from `load_config` to the `build_registry` call path,
      preserving the cycle-guard behavior in the template resolvers.
- [ ] `Config` resource fields become internal to the publish path (no external readers).
- [ ] **Tests**: existing suites pass unchanged (that is the point); add a guard test that `Config`
      resource attributes have no readers outside `config.py`'s publish path (import- or grep-level
      check, same spirit as the naming-consistency prose scan).

Definition of done: all resource reads flow through the Registry; behavior identical; CI green;
reviewer-approved.

## Phase 2: Manifest loader and built-in manifest mechanism

- [ ] **LLD**: `manifest-schema-lld.md` covering the envelope grammar, per-kind spec field tables
      (shared mapping used by loader and migrator), per-kind unknown-key strictness as currently
      implemented (pinned, not changed), error message catalog with `file:line` framing, and the
      YAML library / version decision (verify latest stable; document the mark plumbing).
- [ ] Add the YAML dependency to `cli/pyproject.toml` (latest stable at implementation time);
      promote the library's name from this SDD's local cspell dictionary to the root `.cspell.json`
      once it appears in permanent code (skill promotion rule).
- [ ] `agentworks/manifests/loader.py`: directory walk (sorted relative paths, dotfile skip,
      `.yaml`/`.yml`), YAML stream parse with document start-line capture, empty-document skip.
- [ ] `agentworks/manifests/envelope.py`: apiVersion / kind / metadata / spec validation;
      `manifest_declarable` kind flag; singleton kinds restricted to `name: default`.
- [ ] `agentworks/manifests/decode.py`: spec-to-Resource construction for every operator kind,
      reusing existing per-kind validation; `declared_at` attachment.
- [ ] Cross-document duplicate detection with both locations in the error.
- [ ] `ManifestSet.publish_to(registry)`; bootstrap gains the manifests publisher alongside the
      still-active config publisher (dual-source until Phase 5).
- [ ] `Registry.add` duplicate semantics: replace today's silent last-writer-wins with explicit
      collision handling (operator-vs-operator collisions error citing both origins;
      operator-vs-built-in consults the kind's `builtin_override` flag). This is what makes a
      resource declared in both TOML and a manifest an error during the dual-source window.
- [ ] `agentworks/manifests/builtin.py`: app-bundled manifest discovery via importlib.resources;
      published with `built-in` origin. Ship an empty-but-wired bundle (first content arrives in
      Phase 3).
- [ ] `ResourceKind` gains `manifest_declarable` and `builtin_override` flags; `Registry.add`
      enforces the built-in override policy (allow for catalog kinds, reserved otherwise).
- [ ] **Tests**: loader walk order and dotfile skip; envelope acceptance/rejection per rule;
      per-kind decode round-trips against TOML-parser equivalents (same Resource out of both
      sources); duplicate detection (same file, cross-file, cross-source); built-in override allow
      and reserved paths; singleton name restriction; `file:line` accuracy on multi-doc files.
- [ ] **Docs**: none yet (operator surface unchanged until cutover).

Definition of done: a resources directory fully declares any operator kind with feature parity to
TOML; both sources coexist correctly; CI green; reviewer-approved.

## Phase 3: Secret provider/backend split and git credential alignment

- [ ] **LLD**: `provider-config-lld.md` covering the `SecretProvider` protocol (`config_schema`,
      `instantiate`), the test-only provider that exercises config validation, error framing for
      provider-config violations (must carry manifest `file:line`), and the resolver construction
      swap.
- [ ] `agentworks/secrets/providers.py`: code-side `PROVIDER_REGISTRY` (env-var, prompt) and the
      `secret-provider` descriptor kind + publisher (built-in origin, error miss policy, not
      manifest-declarable).
- [ ] `SecretBackendDecl` resource (name, description, provider, provider config mapping);
      `referenced_resources()` emits the `secret-provider` reference; `secret-backend` kind becomes
      manifest-declarable with `builtin_override = "reserved"` (enforced for manifest-declared rows
      only; legacy TOML `[secret_backends.*]` rows keep today's override-allowed publish until Phase
      5).
- [ ] Built-in `secret-backends.yaml` bundled manifest (env-var and prompt backends, no
      configuration).
- [ ] Built-in providers accept no configuration (non-empty backend config is a schema validation
      error for both); the provider-config plumbing (schema validation, defaults, `file:line` error
      framing, config reaching `instantiate`) is exercised end to end by a test-only provider
      registered only in the test suite, never shipped in the app.
- [ ] Resolver construction from the chain: `secret_config.backends` names looked up in the
      registry; `SecretBackendDecl` rows instantiate via their provider, legacy TOML rows continue
      through the existing construction path (retired in Phase 5 with the TOML resource surface).
- [ ] `git_credentials.<name>` entries gain `provider`: TOML parse accepts it as an alias for `type`
      (`provider` wins when both are present), so every today-valid config still loads at this
      phase; manifests accept only `provider`; `type` is removed with the TOML resource surface in
      Phase 5.
- [ ] Inspection follow-through: `agw secret describe` backend mappings / resolution preview and
      doctor rows compute conventions via instantiated sources; `agw resource list` shows
      `secret-provider` and `secret-backend` rows with references.
- [ ] **Tests**: provider registry lookup and instantiation; test-only-provider config validation
      and resolution end to end; custom backend in chain; reserved-name rejection for
      `env-var`/`prompt` operator manifests; multiple backends sharing a provider; chain naming an
      unknown backend; describe/doctor rendering; regression: the shipped sample config and a
      maximal today-valid TOML config (including `type =` and `[secret_backends.*]` sections) load
      unchanged at this phase's HEAD.
- [ ] **Docs** (lockstep with what becomes true at this phase's HEAD): `cli/README.md` configuration
      schema and command reference for the new `secret-provider` / `secret-backend` rows,
      describe/doctor rendering, and the `provider` alias on `[git_credentials.*]`;
      `sample-config.toml` comments where they mention `type`.

Definition of done: chain-driven resolution runs entirely through provider-instantiated backends;
built-in backends ship as bundled manifests; git credential `provider` field aligned; CI green;
reviewer-approved.

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
      pointing at `agw config migrate`; config publisher removed from bootstrap; TOML resource
      parsing survives only inside the migration tool. The `type` alias and the legacy TOML backend
      construction path are deleted with it.
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

## Sequencing notes

(Recorded as they happen, per SDD convention. Deviations from FRD/HLA get an entry here and an
artifact update.)

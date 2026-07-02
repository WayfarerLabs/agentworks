# Resource manifests: plan

Phases are sequenced so each ends at green CI and a usable intermediate state. Phases 0 and 1 are
pure refactors with unchanged behavior. TOML resource sections keep working through Phase 4 and are
cut over in Phase 5; the dual-source condition exists only between merged phases, never in a release
(Phase 5 ships in the same release train as the tool it points at).

See [frd.md](frd.md), [hla.md](hla.md), [migration-strategy.md](migration-strategy.md), and
[prior-art-research.md](prior-art-research.md).

## Phase 0: Origin taxonomy cleanup

Standalone, mergeable independently of everything else.

- [ ] Rename `Origin` variant `code-declared` to `built-in`; `code_declared()` factory becomes
      `built_in()`. Document `system-plugin` / `external-plugin` as reserved variants in the module
      docstring (not constructible until the plugin SDD).
- [ ] Update all origin rendering (`resources/render.py`, doctor, secret list/describe, resource
      list/describe) to the `built-in (<source>)` display shape.
- [ ] Update `agw resource list --origin` filter vocabulary to `operator | builtin | auto` (confirm
      current accepted values first; keep the filter's CSV/enum style consistent with
      cli-conventions).
- [ ] Update completions if origin filter values are enumerated in the completion tree.
- [ ] **Tests**: rename-sweep over existing origin tests; prose-scan test (naming-consistency style)
      asserting `code-declared` no longer appears in operator-facing strings.
- [ ] **Docs**: update any guide/README text that mentions `code-declared`.

Definition of done: no behavior change beyond display strings; `code-declared` absent from the
codebase except migration-tool comments if needed; CI green; reviewer-approved.

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
- [ ] Add the YAML dependency to `cli/pyproject.toml` (latest stable at implementation time).
- [ ] `agentworks/manifests/loader.py`: directory walk (sorted relative paths, dotfile skip,
      `.yaml`/`.yml`), YAML stream parse with document start-line capture, empty-document skip.
- [ ] `agentworks/manifests/envelope.py`: apiVersion / kind / metadata / spec validation;
      `manifest_declarable` kind flag; singleton kinds restricted to `name: default`.
- [ ] `agentworks/manifests/decode.py`: spec-to-Resource construction for every operator kind,
      reusing existing per-kind validation; `declared_at` attachment.
- [ ] Cross-document duplicate detection with both locations in the error.
- [ ] `ManifestSet.publish_to(registry)`; bootstrap gains the manifests publisher alongside the
      still-active config publisher (dual-source until Phase 5; duplicate across sources is an error
      via the existing publish path).
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
      `instantiate`), the env-var `prefix` semantics, error framing for provider-config violations
      (must carry manifest `file:line`), and the resolver construction swap.
- [ ] `agentworks/secrets/providers.py`: code-side `PROVIDER_REGISTRY` (env-var, prompt) and the
      `secret_provider` descriptor kind + publisher (built-in origin, error miss policy, not
      manifest-declarable).
- [ ] `SecretBackendDecl` resource (name, description, provider, provider config mapping);
      `referenced_resources()` emits the `secret_provider` reference; `secret_backend` kind becomes
      manifest-declarable with `builtin_override = "reserved"`.
- [ ] Built-in `secret-backends.yaml` bundled manifest (env-var backend with default prefix, prompt
      backend).
- [ ] env-var provider `prefix` config; `env_var_name_for` parameterized; prompt provider rejects
      non-empty config.
- [ ] Resolver construction from the chain: `secret_config.backends` names looked up in the
      registry, provider instantiated per backend config; retire the kind-keyed
      `SecretBackendConfig` construction path.
- [ ] `git_credentials.<name>.type` renamed to `provider` (TOML parse accepts only `provider` from
      here; the migration tool handles old configs).
- [ ] Registry kind `git_credentials` renamed to `git_credential` (kind literals, source tuples,
      `--kind` values, completions, naming-consistency test).
- [ ] Inspection follow-through: `agw secret describe` backend mappings / resolution preview and
      doctor rows compute conventions via instantiated sources (custom prefix shows through);
      `agw resource list` shows `secret_provider` and `secret_backend` rows with references.
- [ ] **Tests**: provider registry lookup and instantiation; prefix-parameterized resolution end to
      end; custom backend in chain; reserved-name rejection for `env-var`/`prompt` operator
      manifests; multiple backends sharing a provider; chain naming an unknown backend;
      git_credential rename sweep; describe/doctor rendering.
- [ ] **Docs**: none yet beyond docstrings (operator surface changes announced at cutover).

Definition of done: chain-driven resolution runs entirely through provider-instantiated backends;
built-in backends ship as bundled manifests; git credential vocabulary aligned; CI green;
reviewer-approved.

## Phase 4: Migration tool

- [ ] Add tomlkit dependency (latest stable at implementation time; used only by the migrate path).
- [ ] `agentworks/migrate/`: TOML section split per the FRD R1 table; manifest emission (by-kind
      files, multi-document, declaration order) through the shared field mapping from
      `manifest-schema-lld.md`; renames (`type` to `provider`, `[secret_backends.<kind>]` to
      `secret_backend` documents, empty env-var/prompt sections dropped).
- [ ] Comment-preserving `config.toml` rewrite via tomlkit; timestamped backup of the original
      alongside.
- [ ] `agw config migrate` command: preview + confirm, `--yes`, `--force`, `--dry-run`; idempotent
      no-op on an already-migrated config.
- [ ] Completions updated for the new subcommand.
- [ ] **Tests**: golden-file migration of a maximal config (every section type, comments in
      surviving sections preserved); rename coverage; refusal without `--force` when manifests
      exist; idempotency; backup creation; dry-run writes nothing.
- [ ] **Docs**: command reference material staged for Phase 5 (ships with the cutover so docs match
      HEAD reality).

Definition of done: a representative real config migrates to a loadable manifest set plus a
config-only TOML with zero behavior change (verified by comparing finalized registries before and
after); CI green; reviewer-approved.

## Phase 5: Cutover, samples, and doc promotions

- [ ] `load_config()` rejects resource sections with a `ConfigError` listing the sections found and
      pointing at `agw config migrate`; config publisher removed from bootstrap; TOML resource
      parsing survives only inside the migration tool.
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
  - [ ] Sweep existing guides (`mise.md`, `source-refs.md`, `proxmox.md`, `idempotency.md`) and
        README for TOML-section references to resource kinds; update to manifest examples.
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

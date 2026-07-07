# Resource manifests: plan

Delivery is a single branch and PR (`feat/resource-manifests-sdd`); phases are sequencing milestones
within it (each ends at a green test suite and a coherent commit series), not separately-merged PRs.
Phases 0 and 1 are pure refactors with unchanged behavior. TOML resource sections keep working
permanently (dual-path, revised 2026-07-03 from the original cutover plan): Phase 5 deprecates them
with warnings and repoints the docs; removal waits for an unscheduled future major (Phase 6).

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

> Superseded 2026-07-03 by Phase 3.6: the maintainer's config-is-config ruling reversed the
> secret-config reification (settings are not resources), and the runtime-model LLD replaced the
> resolver machinery entirely. The completed boxes above stand as history; the `validate` /
> `miss_hint` framework hooks, the `secret-config` kind/row/sentinel, and `resolver_for` were all
> deleted in 3.6.

## Phase 3.6: Runtime model -- backends are the door (maintainer-directed)

Two maintainer rulings landed together (see [runtime-model-lld.md](runtime-model-lld.md), which this
phase implements):

1. **Config is config.** Settings that name resources (`[secret_config].backends`, future active
   plugins) do NOT become pseudo-resources; the owning subsystem consumes them in normal operation
   and validates them against the finalized registry at the composition boundary.
2. **Backends are the door.** All runtime access to a capability goes through its exposed resource;
   providers are raw capabilities in a per-domain registry, invoked only by the door.

- [x] `SecretBackendDecl` gains the door methods (`mapping_for`, `would_attempt`, `describe_lookup`,
      `resolve`, `interactive`); `backend_mappings` keyed by BACKEND NAME (built-ins unaffected by
      name coincidence, documented, never relied on); operator surfaces display backend names
      everywhere.
- [x] Provider API reshaped to stateless capability calls (`validate_config`, `would_attempt`,
      `describe_lookup`, `batch_get`, `interactive`), consumed only by the door; `EnvVarProvider` /
      `PromptProvider` replace the source classes.
- [x] Resolution is a loop (`secrets/resolve.py`): `active_backends(config, registry)` +
      `resolve_secrets(secrets, backends)`; hard-miss halt, SetEnv control-character guard,
      per-secret backends-tried errors, `preview_resolution` for inspection surfaces.
- [x] NO caching: `resolve_for_command(targets, config, registry)` is the command's one resolve
      call; the returned VALUES thread down to `compose_env(values=...)`, which raises loudly on
      eager/render drift. Prompt-once is structural. Both weakref memos deleted; `build_registry` is
      a pure function called once per command.
- [x] `validate_chain(config, registry)` runs in `build_registry` after finalize (config vocabulary;
      reachability restricted to operator-declared secrets).
- [x] Unwind of the Phase 3.5 reification: `secret-config` kind/row/sentinel deleted; `validate` /
      `miss_hint` framework hooks deleted (the miss message keeps the reference-usage suffix);
      `Config.publish_to` publishes no settings.
- [x] Legacy TOML `[secret_backends.<kind>]` sections become warned no-ops (typo protection
      retained); `SecretBackendConfig` and the `Config.secret_backends` field deleted;
      `builtin_override` flips to "reserved" and the `ManifestSet.publish_to` reserved-name shim is
      deleted in the same change (`Registry.add` is the sole enforcement; publishers know nothing
      about kinds).
- [x] **Tests**: door-method suites for both built-in providers (mapping-by-name, per-backend
      opt-out, name/provider split end to end); resolve-loop suite (precedence, hard-miss, dedupe,
      hint content); two-backends-one-provider through real manifests (the old conflation test
      inverted: `sibling-env` presents as `sibling-env`); compose drift error; purity pin for
      `build_registry`; deprecated-section warn + built-in-survives pins.

- [x] Follow-up (broad-review finding): pin the nested-create seam with a test --
      `session create --new-workspace/--new-agent` spans multiple composition units (each nested
      create builds its own registry; `create_agent` runs its own git-token resolve). No test
      currently counts resolves or registry builds across that path; add one so the "disjoint secret
      sets in practice" comment is enforced rather than assumed. (Delivered in Phase 5:
      `test_nested_creates_are_their_own_composition_units` pins that the seam passes CLI-shaped
      args only -- no values/registry -- so threading secret material through it trips the test.)

Definition of done: no resolver object, no cache, no memo anywhere in the secrets runtime; every
operator surface speaks backend names; provider API unreachable outside the door; CI green;
reviewer-approved.

## Phase 4: Resource migration and authoring commands

Redesigned 2026-07-05 with the maintainer for the dual-path era (design: `migration-tool-lld.md`).
The tool is `agw resource migrate` -- a recurring incremental mover, not a one-time converter -- and
the phase also ships the YAML authoring surface `agw resource sample`. A CONVENIENCE, not a gate:
TOML keeps working; operators migrate on their own schedule, one kind at a time if they like.

- [x] Add tomlkit dependency (latest stable at implementation time; used only by the migrate path).
- [x] `agentworks/migrate/`: selector resolution (none / `KIND` / `KIND/NAME` split at the first
      `/`; overlaps union; operator-declared TOML rows only; an EXPLICIT selector matching nothing
      errors before writes, while the bare form with nothing left is a "nothing to migrate" exit-0);
      manifest emission through `decode.KIND_SECTIONS` (multi-document, declaration order); layouts
      `per-kind` (default, plural-`s` filenames) / `single` / `per-resource` (`<kind>/<name>.yaml`;
      REFUSES filename-unsafe names with a pointer at per-kind); APPEND-ONLY writes (existing YAML
      never parsed or rewritten; `---`-separated appends, newline-guarded); renames (`type` to
      `provider`); `[secret_backends.<kind>]` sections DROPPED with a note on any TOML rewrite --
      and offered on a bare run with nothing else to migrate, so the tool can silence that residue,
      while `resource migrate secret-backend` explains they are no-ops instead of erroring
      generically; the `admin` and `named_console` singletons emit as `admin-template/default` and
      `named-console-template/default`; supported declaration shapes are standard `[section.name]`
      header tables (plus sub-sections, contiguous or not, one unit); dotted-key / inline-table
      declarations under a parent header are refused with their location and a hand-migration hint.
- [x] TOML edit via tomlkit round-trip: `--toml comment` (default; in-place comment-out with
      `# migrated to resources/<file>` markers, multi-section resources handled as one unit) and
      `--toml delete`; timestamped backup to `paths.backups` taken before ANY write (manifests
      included); atomic rewrite.
- [x] Per-run registry-equivalence verification: rebuild from the result and compare KEYED by
      `(kind, name)` -- not iteration order, which legitimately changes when rows move between
      publishers -- normalizing declaration locations and origin variants recursively, including the
      attribution locations inside auto-declared rows (sharing the decode-parity normalization);
      print `verified: registry unchanged (N resources)`; on mismatch roll back (restore backup,
      remove created files and directories, truncate appends to recorded lengths) and error.
- [x] `agw resource migrate` command in `commands/resource.py`: preview + confirm, `--yes`,
      `--dry-run` (prints would-be YAML and the TOML diff, writes nothing), "nothing to migrate"
      exit-0 on the bare form when everything is already migrated (explicit selectors matching
      nothing error instead). (No `--force`: append-only means nothing can be overwritten.)
- [x] Bundled sample manifests (one per manifest-declarable kind, FULLY commented out so written
      samples are inert -- `--write` can never create a duplicate or a live resource; the loader
      test mechanically un-comments them so "loads clean" is tested against real documents, not
      vacuously) and `agw resource sample [KIND] [--write FILENAME]`: stdout by default; `--write`
      saves under the resources directory (relative paths only, `.yaml`/`.yml` required, parents
      created, appends if the file exists -- as comment text, no `---`: a separator would create a
      null document the loader rejects).
- [x] Completions: both new subcommands; a NEW cross-product selector completer (kind identifiers
      plus `kind/name` pairs from the operator's TOML -- the existing dynamic completers are flat
      per-parameter name lists, so this is new plumbing on the same machinery); `--layout` /
      `--toml` enums; `resource sample` kind argument.
- [x] **Tests**: golden-file migration of a maximal config (every section type, surviving-section
      comments preserved); selector filtering (kind, kind/name, unknown, overlap dedupe, explicit
      selector matching nothing errors, bare nothing-to-migrate exits 0); all three layouts plus the
      per-resource unsafe-name refusal; append to existing files including one lacking a trailing
      newline; comment vs delete including markers, multi-section units, non-contiguous sections,
      and the dotted-key / inline-table refusal; rename coverage; `[secret_backends.*]` drop note
      including the bare-run-only case; backup creation and its before-any-write ordering; dry-run
      writes nothing; verification success, PARTIAL-migration verification (one kind moved, rest
      still TOML), and mismatch-rollback (files, created directories, append truncation);
      `resource sample` stdout / kind filter / `--write` create + append + traversal and suffix
      refusals; un-commented samples load clean through the real loader.
- [x] **Docs**: `cli/README.md` command reference entries for `agw resource migrate` and
      `agw resource sample` ride this phase (the commands are real at this HEAD); the
      `paths.backups` comment in `cli/agentworks/sample-config.toml` widens from "vm backup
      directory" to cover config backups too; the broader doc repoint waits for Phase 5.

Maintainer refinements after the review cycle (2026-07-05; see the sequencing note):

- [x] `--all` required for whole-config runs: bare invocation errors with a hint, `--all` +
      selectors errors, the nothing-to-migrate exit-0 moves to `--all`; service-level and CLI-level
      tests updated and added.
- [x] `--dry-run` prints the summary by default with a `--full` opt-in for the YAML documents and
      TOML diff (`--full` without `--dry-run` errors); tests pin summary content, full content, and
      the flag interaction.

Definition of done: a representative real config migrates -- wholesale or incrementally -- to a
loadable manifest set plus a config-only TOML with zero behavior change, and every real run proves
it via the built-in registry-equivalence verification; CI green; reviewer-approved.

## Phase 5: Dual-path steady state -- deprecation and docs

Maintainer decision (2026-07-03): NO hard cutover. YAML manifests and TOML resource sections both
publish into the one registry indefinitely (different publishers, single registry -- the shipped
architecture, not a transitional window). Mixing is supported; cross-source duplicates error with
both locations. TOML resource sections are deprecated, not removed.

- [x] `load_config()` emits a deprecation issue for each TOML resource section present, naming the
      section and pointing at `agw resource migrate` (same shape as the `[secret_backends.*]`
      warning that already ships -- which itself gained the `--all` pointer).
- [x] Sample config leads with YAML: `cli/agentworks/sample-config.toml` keeps a minimal
      commented-out resource example with a deprecation pointer; sample manifests become the primary
      teaching surface. Update `cli/tests/test_sample_config.py` conventions accordingly. (As
      shipped, the sample declares no live resource sections and loads warning-free; the header
      prose carries the YAML-first pointer at `agw resource sample` / `agw resource migrate`.)

  > Superseded (2026-07-05): sample manifests and their delivery command moved to Phase 4 as
  > `agw resource sample` (`config sample` stays the TOML surface, per config-is-config).

- [x] Verify the Phase 5 doc sweep leads with `agw resource sample` output as the primary YAML
      teaching surface.
- [x] **Docs (permanent-home promotions, per SDD-not-permanent rule)**:
  - [x] New operator guide `docs/guides/resources.md`: the config/resource split, the resources
        directory, the envelope, built-in resources and override rules, the provider/backend model,
        worked examples. Standalone; no SDD references.
  - [x] ADR `docs/adrs/0016-yaml-resource-manifests.md` (number confirmed at write time): auto-load
        YAML manifests with k8s envelope; config/resource/capability split (promote runtime-model
        LLD Part 1, the vocabulary law -- it is load-bearing and must not live only in the SDD);
        dual-path (deprecate, don't break) rationale; backends-are-the-door runtime model, with a
        note that it supersedes the resolver/source MECHANISM described in ADRs 0013/0014 (their
        decisions stand). Repoint the code docstrings citing "runtime-model LLD" (`secrets/base.py`,
        `resolve.py`, `providers.py`, `secrets/__init__.py`) at the ADR (`env/compose.py` turned out
        to carry no LLD citation; `migrate/__init__.py` did and was repointed too).
  - [x] Sweep existing guides (`mise.md`, `source-refs.md`, `proxmox.md`, `idempotency.md`),
        `cli/README.md` (configuration schema and command reference; the largest doc blast radius),
        and the top-level README for TOML-section references to resource kinds; lead with manifest
        examples (TOML noted as deprecated-but-supported). (`proxmox.md`, `idempotency.md`, and the
        top-level README had no resource-section references; `mise.md`, `source-refs.md`, and
        `cli/README.md` were converted.)
- [x] Release notes: release-please compiles them from the conventional commits (maintainer
      direction, 2026-07-05) -- the branch's commit subjects/bodies carry the dual-path model, the
      deprecation, the migration command, and the rename story, and both breaking changes are
      flagged with `!` (FRD R13 '/' ban; `--all` required) -- so no hand-written notes document is
      produced.
- [x] Completions: verify the full command tree still round-trips (kind values and both Phase 4
      subcommands included). (Enforced structurally by `tests/test_completions.py`: every
      DYNAMIC_COMPLETIONS key must resolve to a real command/param, every completer id must have a
      snippet in all three shells, and the generated scripts must contain every command in the tree
      -- which now includes `resource migrate` / `resource sample`.)
- [x] **Tests**: per-section deprecation issue content (`tests/test_config_deprecation_warnings.py`
      -- one warning per section kind, command pointers, config-only TOML and the shipped sample
      warn nothing, secret_backends keeps its dedicated message); guide/sample examples lint (the
      samples-load-clean test ships with the samples in Phase 4).
- [x] Housekeeping: confirm the resource-registry SDD's locked docs need no drift note beyond
      "superseded source format; framework unchanged" (its lockfile anticipated this SDD; add a
      dated note there only if reviewers want one). (A dated vocabulary-translation note WAS added:
      the lockfile names kinds in the retired snake_case spelling, which would mislead a present-day
      reader without it.)

Definition of done: fresh installs learn YAML first; existing TOML configs keep working with a
deprecation nudge; samples, docs, completions, release notes shipped in the same release; CI green;
reviewer-approved.

## Phase 5.5: Capability collapse -- secret backends ARE the capability (maintainer-directed)

Pre-lock revision (2026-07-07), driven by the plugin-system SDD design review: the
capability/exposed-resource split is dissolved as a universal layer. Resources reference
capabilities directly, many-to-one, carrying provider-owned config at the reference site; a
dedicated instance kind exists only where instances need identity beyond a single reference site
(git-credential today; vm-platform in the plugin SDD). `secret-backend` the DECLARABLE kind dies;
`secret-provider` the capability is RENAMED `secret-backend` (the domain's natural noun, and the
word the released v0.10.0 TOML surface already uses). See the 2026-07-07 sequencing note for the
full ruling chain; FRD R8, the HLA secret architecture section, and ADR 0016 carry the revised
model.

- [x] Rename the capability: `SecretProvider` protocol -> `SecretBackend`,
      `SECRET_PROVIDER_REGISTRY` -> `SECRET_BACKEND_REGISTRY`; the `secret-provider` descriptor kind
      is renamed `secret-backend` (still read-only, error miss policy, not manifest-declarable). One
      registry row per capability -- the current duplicate `secret-provider/env-var` +
      `secret-backend/env-var` pair collapses.
- [x] Delete the declarable layer: `SecretBackendDecl`, the `secret-backend` decode path, the
      bundled `secret-backends.yaml` manifest, the reserved-name enforcement for backend names, and
      the prose-only secret-backend sample (SAMPLE_KINDS shrinks; `agw resource sample` choices
      follow).
- [x] Runtime survives keyed by capability: the `SecretBackend` API is exactly `would_attempt` /
      `describe_lookup` / `batch_get`; orchestration (mapping lookup, the generic `false` opt-out,
      prompt-once batching) lives in the resolution loop, with mappings looked up by backend name --
      which now IS the capability name. `active_backends` / `validate_chain` validate
      `[secret_config].backends` against the descriptor rows.
- [x] Retire the "door" metaphor in code prose and docs (maintainer ruling, 2026-07-07): it enforced
      providers-only-via-backends, which the collapse makes vacuous; `SecretBackend` is a
      well-defined API abstracting where secrets come from. Historical artifacts (Phase 3.6 heading,
      runtime-model-lld title/banner, checked boxes, provider-config-lld's superseded banner and
      body) keep the word as record.
- [x] TOML surface unchanged and now literally correct: `[secret_config].backends` lists backend
      (capability) names; `backend_mappings.<backend>` keys backends. No renames, no aliases. The
      `[secret_backends.*]` no-op deprecation and migrate's drop handling stay as-is.
- [x] Inspection surfaces: `agw secret describe` / doctor / `agw resource list` render the single
      kind; describe's mappings/preview logic unchanged.
- [x] Update FRD R8, HLA secret architecture, ADR 0016 (the former "exposed resources are the door"
      section becomes the resources-reference-capabilities model with the naming rule), and the
      SDD-artifact sweep (Background/Terminology/R1/R3/R6/R9/R10/R11/non-goals in the FRD;
      bootstrap/dual-source/kind-flags/validation/design-decision sections in the HLA;
      manifest-schema-lld, migration-tool-lld, migration-strategy, locked.md).
- [x] Doc sweep with the implementation: `cli/README.md` (the "Secret Providers and Backends"
      section, doctor wording, `--kind secret-provider,secret-backend` and
      `describe secret-provider/env-var` examples), `docs/guides/resources.md` (the secrets
      section), `cli/agentworks/sample-config.toml` comments -- these stay describing HEAD until the
      code lands and ride the implementation commits.
- [x] **Tests**: rename sweeps; kind-collapse pins (one row per capability; declarable manifest for
      `kind: secret-backend` is now an envelope error); chain validation against descriptor rows;
      existing resolution/inspection suites pass with mappings keyed by capability name.
- [x] **Companion (maintainer request)**: [capability-consumers.md](capability-consumers.md) --
      consumers and schema shapes for every current and planned capability, organized by cardinality
      (reference+blob for one; map keyed by capability name for many order-free; ordered name list
      when order is the meaning). Explicitly a SUGGESTION doc: feedback input for the plugin SDD,
      binding nothing.

Definition of done: one `secret-backend` kind (the capability); no declarable instantiation layer;
released TOML configs work verbatim; CI green; reviewer-approved.

## Phase 6: TOML resource-path retirement (future major; unscheduled)

Deferred until a future major release, on operator telemetry/feedback -- not part of this SDD's
delivery. Recorded so the end state is explicit:

- [ ] `load_config()` rejects TOML resource sections with a `ConfigError` pointing at
      `agw resource migrate`; the `type` alias on `[git_credentials.*]` and the deprecated
      `[secret_backends.*]` acceptance are deleted with it.
- [ ] Loader-ownership inversion: manifest decoders (or the kinds) own resource field validation
      natively; the `_load_*` resource loaders and the decode-through-TOML shim are deleted from
      `config.py` (pure config remains); the migration tool keeps its own TOML reader; loader
      messages speak manifest vocabulary natively; the decode-parity suite retires with the shim.

Definition of done: `config.py` contains no resource-section knowledge; the migration tool is the
only TOML-resource reader in the tree; CI green; reviewer-approved.

## Sequencing notes

(Recorded as they happen, per SDD convention. Deviations from FRD/HLA get an entry here and an
artifact update.)

- **2026-07-07: the capability collapse (maintainer ruling, pre-lock; Phase 5.5).** Reviewing the
  draft plugin-system SDD (which generalized the capability/exposed-resource split to five kinds),
  the maintainer challenged the split itself: forcing a dedicated instantiation kind per capability
  adds ceremony where the consumer could carry the config. The ruling chain, in order: (1)
  harness/feature exposed kinds die -- templates reference capabilities directly with inline
  provider config (dependencies become capability-name-to-capability-name, fixing an indirection
  wart); (2) the "discriminator" (dedicated kind iff instances need identity beyond one reference
  site) was found to describe ordinary domain modeling, not a special pattern -- git-credential was
  NEVER a split-model example, just resources referencing a capability many-to-one; (3)
  secret-backend, the only kind whose ENTIRE content was the exposure, dies too: the 1Password case
  is served by structured backend_mappings (vault+item per secret), and multi-ACCOUNT stores are a
  graduate-when-real future (reintroduce a declarable kind then; backend-keyed mappings default to
  the sole instance); (4) with the exposure layer gone, the capability takes the domain's natural
  noun: secret-provider RENAMES to secret-backend ("backend" is the ecosystem's word for a pluggable
  secret store, and the released v0.10.0 TOML keys -- [secret_config].backends, backend_mappings --
  already say it; the name coincidence we used to apologize for becomes the compatibility bridge).
  Naming rule: domains use their natural capability noun; a disambiguating suffix only on collision
  (git-credential-provider, vm-provider). The tells that the layer was ceremony, for the record: a
  prose-only sample (nothing declarable could exist), built-in names forced to coincide with their
  providers', duplicate env-var/prompt rows across two kinds, and docstring gymnastics
  distinguishing names "never relied on in code". Done pre-lock on this branch (maintainer: "sadly")
  because the layer never shipped in a release and ADR 0016 would otherwise enshrine a superseded
  model at merge.
- **2026-07-05: provider_config nesting (maintainer ruling, pre-lock).** Provider-owned
  configuration on exposed resources nests under one `spec.provider_config` key (an opaque blob the
  provider owns) instead of spreading across the spec tail; unknown top-level spec fields error with
  a pointer at the rule. Cheap now because no config-bearing secret provider ships; the test-only
  provider, the illustrative sample, and `SecretBackendDecl.config` (renamed `provider_config`)
  carry it. An initial carve-out for git-credential's `org` was reversed the same hour at the
  maintainer's push ("YAML can have a different shape than TOML"): `org` nests in manifests too (the
  decoder flattens back into the shared TOML loader, so validation is unchanged, and the migrator
  emits the nested shape -- proven shape-only by its own registry-equivalence verification);
  kind-owned `token`/`description` stay top-level. A follow-up clarification made the INTERNAL
  representation follow the nested shape as well (`GitCredentialConfig.org` became
  `provider_config`; the TOML loader nests at its boundary, so the flat section is the only flat
  domain). A full audit then compared every kind's internal dataclass against its YAML spec
  vocabulary (the maintainer's "best representations" rule: internal shapes match YAML shapes; TOML
  is the lone divergent domain, mapped at its loader): twelve kinds were already 1:1; the one
  offender was `GitCredentialConfig.type`, renamed `provider` to match `spec.provider` (the TOML
  section still accepts `type`/`provider`, mapped at the boundary). ADR 0016 records the pattern.
  Also extended `--all` to `resource sample` and aggregated the deprecation warnings behind
  `--no-deprecations` in this same pre-lock batch.
- **2026-07-05: `agw resource edit KIND/NAME` (maintainer request, pre-lock).** Opens the YAML
  manifest declaring a resource in $EDITOR (same contract as `config edit`), printing `file:line`
  first since per-kind layout files hold many documents. Deliberately minimal per the maintainer's
  scope ruling: no editor line-jump heuristics, no scaffolding; TOML-declared resources error with a
  pointer at `agw resource migrate KIND/NAME` or `agw config edit`, built-in and auto-declared
  resources have no file to open. `edit_location` in resources/inspect.py is the service authority
  (reusing describe's validated lookup); the `resource_refs` completer covers the new argument for
  free.
- **2026-07-05: kind/name display syntax unified on '/' (maintainer ruling, pre-lock).** The older
  inspection surfaces used `kind:name` (describe header, `--names-only`, auto-declared descriptions,
  references, used-by lines) while the migrate surfaces used `kind/name`. Everything now uses `/` --
  which the R13 ban makes the only parse-safe separator (`:` remains legal in pass-through names) --
  including `resource describe`, which takes a single `KIND/NAME` token matching migrate's grammar
  (breaking for the three-week-old two-token form). Live-instance lines in Used by: sections share
  the display syntax; they are still not kinds (vocabulary law unchanged; `instance_kind` stays),
  the section context carries the distinction. ADR 0016 records the rule; completions moved to `-F/`
  splitters and `resource_names` was replaced by a `resource_refs` completer whose candidates are
  the describe tokens verbatim.
- **2026-07-05: whole-config migration requires --all; dry-run is summary-first (maintainer
  refinement).** A bare `agw resource migrate` errors with a hint instead of migrating everything --
  "migrate everything" is an explicit `--all` opt-in (mutually exclusive with selectors), and the
  idempotent nothing-to-migrate exit-0 moves to the `--all` form. `--dry-run` prints the summary
  (which resources go where) by default; `--full` opts into the YAML documents and the TOML diff,
  which are unusably long as a first answer on whole-config runs. FRD R10 and the LLD amended; new
  checkboxes below record the delivery.
- **2026-07-05: secret-backend sample is prose-only (maintainer ruling, via the implementation
  review).** No config-bearing provider ships, so no operator-declarable backend can be real; an
  uncommentable onepassword document would teach a lie. The sample's illustrative shape moved into
  prose, the samples-load-clean and registry-build tests carve the kind out explicitly (the registry
  test is thereby exclusion-free over the whole uncommented set), and a test pins the prose-only
  state so it flips the day a real provider lands. Supersedes the checked Phase 4 sample checkbox's
  "loads clean for every kind" description for this one kind.
- **2026-07-05: '/' banned in resource names (maintainer ruling, via the implementation review).**
  Supersedes the slash-addressability note below: rather than supporting slash-bearing names in
  selectors, `/` is strictly disallowed in resource names, enforced source-independently at
  `Registry.add` (FRD R13). Deliberately breaking for configs with slash-bearing quoted section
  names; release-notes line rides Phase 5. The selector grammar and the `migrate_selectors`
  completions are unambiguous as a result, and the per-resource layout's unsafe-name refusal narrows
  to the remaining pass-through hazards (spaces, backslashes, leading dots).
- **2026-07-05: Phase 4 implementation notes.** Two deviations from the LLD text, both reconciled
  there: selector completion sources operator-origin registry rows (which include already-migrated
  YAML rows) rather than a TOML-only list -- the already-migrated error is clear and a TOML-only
  filter is not worth new CLI surface -- and names containing `/` were briefly made individually
  addressable (first-slash split leaves the full name as the remainder) before the ruling above
  removed the case entirely. Also discovered: `resource sample --write` appends WITHOUT a `---`
  separator (commented content plus a separator would create a null document the loader rejects);
  FRD/LLD updated.
- **2026-07-05: Phase 4 redesigned as a recurring mover (maintainer-directed).** The migration tool
  is `agw resource migrate` (renamed from `config migrate`; its object is resources): positional
  selectors for incremental runs, `--layout per-kind|single|per-resource`, append-only YAML output,
  mandatory TOML edit as `--toml comment` (default) or `delete`, and per-run registry-equivalence
  verification with rollback. Sample manifests moved from Phase 5 to Phase 4 behind a new
  `agw resource sample [KIND] [--write FILENAME]`; `config init/edit/sample` stay TOML-owned per
  config-is-config. FRD R10/R11, HLA, migration-strategy, and `migration-tool-lld.md` (new) carry
  the design.
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
- **2026-07-03: dual-path replaces the hard cutover.** Maintainer decision: open the YAML path while
  fully supporting TOML resource sections (with deprecation warnings), including mixing -- "this
  would really force the 'different publishers, single registry' concept," and operators migrate on
  their own schedule. Phase 5 re-planned from cutover to deprecation-and-docs; TOML removal deferred
  to an unscheduled future major (Phase 6). The FRD's R1/R11 hard-cutover language is superseded
  accordingly.
- **2026-07-03: config is config; backends are the door (maintainer-directed; Phase 3.6).** Two
  rulings that replaced the Phase 3/3.5 runtime machinery wholesale (see runtime-model-lld.md):
  settings that name resources stay config (the secret-config reification was unwound the same day
  it landed -- its sentinel/skip special cases were the model saying "I don't fit"); and all runtime
  capability access goes through the exposed resource (SecretSource/SecretResolver/ resolver_for and
  both weakref memos deleted; resolution is a loop; values thread from one resolve per command;
  prompt-once is structural, not cached). The maintainer also fixed a provider/backend identity
  conflation the interim runtime had inherited from the env-and-secrets-era source layer: sources
  carried provider identity, so mappings and operator surfaces couldn't distinguish two backends on
  one provider. backend_mappings are keyed by backend name as of 3.6.
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

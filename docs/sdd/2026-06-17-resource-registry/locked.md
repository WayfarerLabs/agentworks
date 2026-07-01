# Resource registry -- Lockfile

## 2026-06-30

The Resource Registry SDD shipped in two PRs:

- **PR #126** (`feat/resource-registry-sdd`, merged) -- Phases 0, 1a, 1b, 1c, 1d, 1e. Established
  `SourceLocation` at the Config layer, the `agentworks.resources` package with `Registry` /
  `ResourceKind` / `Origin` / `ReferenceEntry`, the publish-and-finalize lifecycle, the secret kind,
  and the operator-facing `agw secret describe`.
- **PR (this branch: `feat/resource-registry`)** -- Phases 2a (0..3), 2b (0..2), 2c, 3a, 3b, 3c.
  Generalized the framework to every operator-configurable kind, added the catalog +
  git-credential-provider + secret-backend kinds via standard publishers, shipped the cross-kind
  `agw resource list` / `agw resource describe`, renamed the framework's outbound/inbound reference
  types to match the operator-facing vocabulary, and added the dynamic per-kind `instances` hook so
  both `agw resource describe` and `agw secret describe` show a `Used by (per current config):`
  section alongside `Referenced by:`.

All checkboxes in [plan.md](plan.md) are flipped. The FRD and HLA are accurate as of this date and
are now locked.

### Shape of what shipped

- **Two-layer model.** `Config` (parser) composes `Resource`-shaped values and publishes them into
  the `Registry` (framework). The framework's `Origin` is a Registry-layer concept; Config carries
  `SourceLocation` and the framework translates at publish time.
- **Publish + finalize.** `Registry.empty()` -> publishers add Resources / References ->
  `finalize()` runs the always-materialize pre-step, walks references, dispatches per-kind miss
  policies (auto-declare with a reserved-name guard, or error), attaches `references` (the inbound
  projection), detects cycles, and freezes. `build_registry(config)` in `agentworks/bootstrap.py`
  orchestrates the standard publisher chain (catalog -> git_credentials -> secrets -> config).
- **Reference vocabulary.** Outbound: `ResourceReference` (with `SecretReference` /
  `TemplateReference` subclasses). Inbound: `ReferenceEntry`. Both carry the same `usage` prose
  field; the inbound type drops `kind` / `name` because they're implicit from the container
  Resource. See `resources/reference.py` for the full documented contract.
- **Kinds.** `secret`, `vm_template`, `agent_template`, `workspace_template`, `session_template`,
  `admin_template` (singleton with `name = "default"`), `named_console_template`, `apt_package`,
  `system_install_command`, `user_install_command`, `git_credential_provider`, `secret_backend`.
  Each kind owns its `synthesize`, miss policy, and (where applicable) reserved auto-declare names.
- **Per-kind `instances` hook.** Kinds with a live-instance concept (the four named template kinds
  plus `admin_template`, `named_console_template`, and `secret`) implement
  `instances(db, registry, resource) -> Iterable[InstanceRef]`. Absent-on-class signals "no instance
  concept" (catalog, providers, backends); the framework consumer (`resources/inspect.py`'s
  `used_by_for`) structurally duck-types via `getattr` so the Protocol can stay clean of
  Liskov-violating optional methods.
- **Operator surface.**
  - `agw secret describe <name>` -- per-kind detail: header (kind, name, description, origin, hint),
    `Referenced by:` (static), `Used by (per current config):` (dynamic, session projection),
    `Backend mappings:`, `Resolution preview:`.
  - `agw resource list [--kind csv] [--origin variant] [--names-only]` -- cross-kind list with
    `REFS` and `USED BY` columns; `USED BY` shows `-` for kinds with no instance concept.
  - `agw resource describe <kind> <name>` -- cross-kind detail; stops at framework-uniform fields
    (kind, name, origin, description, `Referenced by:`, `Used by (per current config):`). Per-kind
    describes include these same framework-uniform sections plus their kind-specific extensions --
    overlap is intentional (see FRD R12).
- **Shared rendering.** `agentworks.resources.render.format_origin_line` / `format_file_path` back
  both the cross-kind and per-kind describe views; `agentworks.resources.inspect.used_by_for` is the
  single-source-of-truth projection helper for the dynamic dimension; `output.detail` for the
  indented sub-sections.

### Pivots worth recording (deviations from the original FRD/HLA reasoning)

- **`always-materialize` pre-step in `Registry.finalize` (Phase 2a.0).** The framework now pre-emits
  reserved-default Resources before the references walk, so kinds with an unreferenced default
  (admin, agent_template, etc.) materialize even when no operator declared them. The pre-step uses a
  reserved `("framework", "always-materialize")` source on the synthesized `Origin`, and
  `synthesize(references=())` is the uniform contract every kind tolerates.
- **`NoUnreferencedDefaultError`** -- typed framework error raised by `synthesize` when a kind has
  no reserved default and the framework calls it with empty references. Defensive but explicit.
- **`TemplateReference` subclass (Phase 2a.1)** -- separate subclass under `ResourceReference` so
  template producers and the framework agree on target-kind without string dispatch.
- **Cycle guards moved into resolvers (Phase 2a.1+).** The original "detect cycles in
  `Registry.finalize`" plan was replaced by a per-resolver `_visiting: tuple[str, ...]` guard in
  `vms/`, `agents/`, `workspaces/`, `sessions/` template resolvers; this protects the eager resolve
  path in `load_config` against `RecursionError` on default-involving cycles too.
- **Description polish generalized (Phase 2a).** `Registry.finalize`'s polish step is now a
  structural check (any kind with a `description: str` field benefits) rather than the original
  secret-specific `isinstance(SecretDecl)`. The synthesized text is
  `"(auto) <usage> for <kind>:<name>"` for usage-driven auto-declares and
  `"(auto) auto-declared default <kind>"` for the empty-references default case.
- **`AdminTemplateKind` plurified (Phase 2a.3).** `AdminConfig` grew a `name: str = "default"` field
  and the framework treats it as a regular named kind. The `NamedConsoleConfig` plurification is
  intentionally deferred to a future SDD (the kind shape is already aligned; only the Config-side
  `name` field is missing).
- **Catalog as Resources via a publisher (Phase 2b.0).** Each catalog entry becomes an
  `AptPackageEntry` / `SystemInstallCommandEntry` / `UserInstallCommandEntry` Resource with the
  catalog publisher; no `validate_selections` pre-flight any more -- the registry's miss policy
  (`error` for these kinds) is the contract.
- **`GitCredentialProviderKind` + `SecretBackendKind` (Phase 2b.1/2b.2).** `type` and the section
  name on `[git_credentials.<name>]` and `[secret_backends.<kind>]` now reference real Resources
  (with reserved-name `auto_declare_names = {"azdo", "github"}` / `{"env-var", "prompt"}` and
  `error` miss policy). Unknown values surface as `ConfigError` at load time.
  `VALID_GIT_CREDENTIAL_TYPES` is gone; the `[secret_backends.<kind>]` unknown-kind warning was
  elevated to an error to match the symmetry.
- **Phase 2b.2 pragmatic scope cut.** `[secret_config].backends` active-chain validation kept its
  bespoke check rather than refactoring `SecretConfig` into a Resource (filed as a future SDD).
- **Phase 3 terminology rename (3a).** `ResourceRequirement` -> `ResourceReference`, `UsageEntry` ->
  `ReferenceEntry`, `Resource.usage` collection -> `Resource.references`, producer method
  `required_resources()` -> `referenced_resources()`. Framework's `usage: str` prose field is
  preserved on both outbound and inbound types with the same name. CLI labels: `USAGE` -> `REFS`,
  `Usages:` -> `Referenced by:`. Rename was pre-merge (no public API churn); prose-scan test in
  `tests/resources/test_phase3_naming_consistency.py` guards against future regressions.
- **Phase 3b+3c dynamic dimension.** `instances(db, registry, resource)` hook on kinds with a
  live-instance concept; new `USED BY` column and `Used by (per current config):` section on the
  operator surface. Naming choice: `USED BY` / "Used by" over the more clinical "Expected" -- reads
  naturally today, and a future provisioned-state SDD can add a contrasting `PROVISIONED ON` /
  "Provisioned on:" dimension without renaming this one. The framework hook stays generically named
  `instances(...)` for that reason. Secret projection is mutually exclusive by session mode
  (admin_template OR agent_template per session, not both) so an admin-env-only secret isn't
  over-attributed to agent-mode sessions.
- **Per-kind vs cross-kind describe overlap is intentional (FRD R12).** Every per-kind describe
  includes the framework-uniform sections (`Referenced by:`, `Used by:`, header) plus its
  kind-specific extensions. Cross-kind describe stops at framework-uniform fields. Operators running
  `agw secret describe X` get the complete view in one place; the cross-kind command is the
  truncated shortcut usable without knowing the specialized command.

### Phase 1 follow-ups (status at lock)

Resolved during Phases 2 and 3 (in-area cleanups, per the SDD's "address when the relevant code is
touched" convention):

- `output.detail` vs `output.info` for nested sections -- resolved in Phase 2c.
- `SecretDescription.kind = "secret"` hard-coded -- resolved in Phase 2c via `SECRET_KIND_NAME`.
- FRD R10 dedupe wording ambiguity -- resolved in Phase 2c; FRD now spells out `(source, text)`.
- Auto-declared description polish was secret-specific -- generalized in Phase 2a.

Carried forward (still deferred, non-blocking, picked up when the relevant code is next touched):

- `SecretDecl.auto_declared(name)` classmethod to single-source the fallback shape across the few
  remaining synthesize-on-the-fly call sites (`SecretResolver.render`,
  `_lookup_or_synthesize_secret`, `_collect_git_tokens` fallback).
- `_collect_git_tokens` placement -- still in `vms/manager.py`, cross-imported by
  `agents/manager.py`; a neutral home (`agentworks/git_credentials/resolve.py`) reads more
  naturally.
- `NamedConsoleConfig` plurification -- reserved for a future SDD when operator demand for named
  console templates lands. Framework side is already aligned.
- `SecretConfig` as a first-class Resource -- the bespoke active-chain validation in Phase 2b.2
  remains as a deliberate scope cut.
- **Provisioned-state (drift) tracking** -- Phase 3 leaves room for a sibling
  `provisioned_instances(db, resource)` hook returning the same `InstanceRef` shape, which would add
  a `PROVISIONED ON` column and "Provisioned on:" section paired with today's `USED BY` / "Used
  by:". The framework hook stays generically named `instances(...)` (not `expected_instances(...)`)
  so the internal rename cost of adding the sibling is low.

### Drift notes

None. The FRD and HLA accurately describe the shipped surface as of this date. The sequencing-notes
section in plan.md documents every per-phase pivot that touched the design.

See [plan.md](plan.md) for full per-phase detail and [frd.md](frd.md) / [hla.md](hla.md) for the
locked design. These specs are now locked.

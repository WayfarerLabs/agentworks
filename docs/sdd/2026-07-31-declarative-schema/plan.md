# Plan: TOML Resource Sunset and the Declarative Schema Model

Date: 2026-08-01

Status: DRAFT (authored alongside the FRD and HLA; implementation gated, see prerequisites)

## How to work this plan

- One feature branch and PR (`feat/declarative-schema-sdd`, PR #316); phases land as ordered commits
  on it. The suite stays green at every step; there is no flag-day cutover.
- Each numbered step is delegated to an `agentworks-dev` subagent (LLD first where one is called
  for, then implementation), then reviewed by `agentworks-reviewer` before its boxes are checked.
- Every step's definition of done includes the standing gates: `ruff check`, `ruff format --check`,
  `mypy .` (strict), `pytest -q` all green, `./scripts/lint-files.sh` clean, plus the step's own
  criteria. Steps that touch the CLI surface include the completions check; steps that change
  operator-visible behavior update docs in the same commit.
- Checked boxes are immutable history; plan changes add new boxes rather than editing old ones.

## Prerequisites (hard gates, in order)

- [ ] The codex harness effort has landed on `main` (operator direction, 2026-08-01: wait for it,
      then start). Its capability joins the phase 2 model inventory like any other.
- [ ] PR #315 (TOML deprecation warning) and PR #349 (tagged-shape pre-support) are both in a
      shipped release. Phase 1's hard error and phase 2's shape hardening each require one released
      warning version of runway (FRD dependencies).
- [ ] Branch rebased onto `main` after the above; capability inventory re-enumerated (new
      capabilities, e.g. codex, added to step 2.3's list).

## Phase 1: remove TOML resource declarations

### 1.1 Phase 1 LLD (the migrator verification rework is the core)

- [ ] LLD `toml-sunset-lld.md` written and reviewed. It must settle:
  - The independent pre-side derivation for migrate verification (FRD FR2, HLA phase 1). Candidate
    to evaluate first: RELOCATE the TOML resource loaders into `migrate/` as the migrator's private
    TOML reader instead of deleting them outright. That satisfies "reads the TOML file directly",
    keeps the pre-side independent of the emission mapping (old loader semantics vs section-to-spec
    sweep), and still removes the loaders from the app's config-load path. If the LLD rejects this,
    it must name the alternative independent derivation.
  - The hard-error mechanics: where the `KIND_SECTIONS` check raises, the aggregated error text, and
    which commands keep the settings-only escape hatch (`resource migrate` moves to it;
    `resource sample --write` and `resource edit` fallback already use it).
  - What happens to the `--no-deprecations` channel contents (the TOML nudge becomes an error and
    leaves the channel; the #349 tagged-shape warning remains on it).
  - The fate of every consumer of the deleted loaders (decode shims, `resource edit`'s TOML pointer
    text, DB-migration snippet printers, doctor rows naming TOML sections).

### 1.2 Hard error and loader removal

- [ ] Config load hard-errors (aggregated, actionable, naming sections, pointing at
      `agw resource migrate` and `agw resource sample`) when any resource-declaring TOML section is
      present; settings sections load exactly as before.
- [ ] TOML resource loaders removed from the config-load path per the LLD (relocated or deleted);
      decode layer stops routing through loader shims, owning surviving logic directly (interim
      state; phase 2 replaces it).
- [ ] `agw resource migrate` works end to end on a fixture config (proxmox or azure section, git
      credentials, secrets, session template) with the reworked verification: emitted YAML decodes
      and loads, comparison against the independent pre-side passes, rollback still fires on
      mismatch. `test_full_migration_golden` updated and green.
- [ ] Deprecation-warning tests for TOML sections replaced by hard-error tests (fires with sections
      present, not without; exempted commands still run).

### 1.3 Phase 1 records

- [ ] Superseding ADR replaces ADR 0016's dual-path stance (the ADR the status note promised); ADR
      0016 gains its superseded-by pointer.
- [ ] Guides, cli README, root README, and sample-config.toml describe only the manifest path
      (sample-config's pointer comments already do; sweep for stragglers).
- [ ] Dated lockfile entries: resource-manifests (TOML path removed; which shipped machinery from
      that SDD is retired) and any other locked SDD whose stance this revises.
- [ ] Reviewer pass on the whole phase; findings fixed.

## Phase 2: the declarative schema model

### 2.1 Schema foundation

- [ ] `schema-foundation-lld.md` written and reviewed: base model config (strict, frozen,
      `extra="forbid"`), the `SecretRef` / `ResourceRef` `Annotated` markers and their
      `json_schema_extra` (`x-agw-*`) encoding, `extract_references` (total, never raising, reads
      raw blobs, applies owner templates) and `iter_field_docs` signatures, and the pydantic pin
      policy (latest stable v2 at implementation time, checked then, not from memory).
- [ ] `resources/schema/` package implemented with unit tests, including totality tests for
      `extract_references` over malformed blobs (property-style: no input raises) and marker
      round-trip into emitted JSON Schema.
- [ ] pydantic dependency added; mypy plugin enabled; strict mypy green across the repo.
- [ ] `pydantic` and related vocabulary promoted from the SDD cspell dictionary to the root
      dictionary (it now appears in permanent code).

### 2.2 Error bridge

- [ ] `error-bridge-lld.md` (may fold into 2.1's LLD if small): `ValidationError.loc` to
      owner-framed message mapping, message normalization rules, `SourceLocation` framing, and the
      severity plumbing for fold-gated validation (FR12: the bridge raises for READY+ENABLED
      resources; the same rendering is reusable as diagnostic text elsewhere).
- [ ] Bridge implemented with the FRD's representative-mistakes corpus as a pinned test: unknown
      key, wrong type, missing required field, bad capability name, old sibling shape, each
      asserting owner framing and file/position context at least as good as today's.

### 2.3 Capability config models (the contract flip)

- [ ] Per-capability models declared and registered via `config_model` (empty-config shared model
      where applicable). Inventory at authoring time, re-enumerated at implementation: vm-platform
      lima, wsl2, azure-vm (including the nested `service_principal` model), proxmox;
      git-credential-provider github (scope union: repos/owner mutual exclusion as a model
      validator; `token` as `SecretRef` with the `git-token-{owner}` template), azdo; harness shell,
      claude-code, codex (new); secret backends env-var, prompt (no mapping), onepassword (mapping
      is itself a union: `op://` string or account/reference table).
- [ ] Core-driven validation and extraction wired: registry name-to-model maps per capability kind;
      `Capability.validate` / `Capability.dependencies` classmethods and
      `SecretBackend.validate_mapping` retired; per-capability hand-rolled validate code deleted;
      `capabilities/git_credential/base.py`'s token helpers absorbed into the model layer.
- [ ] Construction binds the validated model instance; ops read typed fields (this is a real
      per-capability migration: azure and proxmox ops currently read `self.platform_config[...]`
      dict keys; each capability's op code moves to model attributes with mypy enforcing it).
- [ ] Fold-gated severity proven by tests: broken blob on a disabled plugin's resource loads with
      the row marked, errors on enable/use; broken blob on an enabled resource is a load error;
      unregistered name still self-disables loudly.
- [ ] `test_capability_config_contract.py` and `test_capability_base.py` reworked to pin the new
      contract (declare-and-receive: models in, typed instances out).

### 2.4 Tagged-shape hardening

- [ ] Old sibling shape (`platform` + `platform_config` and kin) becomes a hard error naming the
      exact rewrite; #349's dual-shape normalization, its aggregated warning channel
      (`ManifestSet.deprecation_issues` for shape), and the bundle-gate special case are removed
      (the hard error makes the gate redundant).
- [ ] `agw resource migrate` gains the manifest-upgrade mode (LLD decides the in-place YAML rewrite
      mechanics and comment handling; backup-first discipline reused; completions updated for any
      new flag/subcommand).
- [ ] Upgrade mode proven on a fixture resources dir authored in the old shape (comments preserved
      per the LLD's decided policy; result loads clean; idempotent re-run is a no-op).

### 2.5 Kind spec models replace the decoders

- [ ] Kind-by-kind migration behind the stable decode entry points, smallest first to bed in the
      pattern: apt-package, apt-source, system/user-install-command, workspace-template,
      named-console-template, admin-template, agent-template, vm-template, secret, git-credential,
      vm-site, session-template. Each kind's box covers: model (with semantic validators for
      name/length caps and cross-field rules), decode swap, error parity via the bridge, tests
      updated.
- [ ] Unknown-key handling flips from warn to hard error for kind specs (FR12): the
      `_warn_unexpected_keys` machinery retires with the last kind; tests updated accordingly.
- [ ] `migrate/verify.py` normalization taught the model shape (the dataclass-only
      `strip_source_fields` stops silently no-oping when decl classes become models).
- [ ] Decl classes are frozen models (or thin wrappers where behavior-rich; per-kind LLD calls),
      with `DeclaredResource`'s hooks preserved for the registry.

### 2.6 Model-layer defaulting (FR15)

- [ ] Sweep and enumerate every consumer-side fallback for modeled fields
      (`is not None else     <literal>`, `or <literal>` on request/config reads). Known at authoring
      time: azure cpus=4 / memory=8 / disk=50 / swap=0, proxmox swap=0; the sweep is the authority.
- [ ] Defaults declared on the models; post-decode types non-optional where defaulted; every
      enumerated fallback deleted; consumers observing an unexpectedly-unset modeled field raise
      (DB-sourced legacy rows included: raise, never locally default).
- [ ] The end-to-end fixture-capability test (FRD success criterion): add a field with a default and
      description to a fixture capability model and prove validation, extraction, sample, describe,
      and emitted schema all reflect it with no other edits.

### 2.7 Schema emission and editor association

- [ ] JSON Schema (2020-12) emitted per kind plus the envelope schema, unions expressed as `oneOf` +
      discriminator over `name`; CLI surface (working name `agw resource schema`) prints/writes the
      set; completions tree updated for the new command.
- [ ] `agw resource sample --write` and migrator-emitted YAML stamp the yaml-language-server
      modeline referencing written schema files; end-to-end check that a schema-aware editor setup
      validates a sample manifest (documented manual check plus an automated schema-validates-the-
      sample test using a JSON Schema validator in tests only, if the LLD approves the dev-only
      dependency).

### 2.8 Live-rendered samples and describe

- [ ] Renderer over `iter_field_docs`: commented-YAML skeleton per kind and capability arm (one
      union arm rendered, alternatives listed), merged with registered prose blurbs (kind-level and
      capability-level; blurbs carry no field lists).
- [ ] `agw resource sample` (and `--write`) rendering live from the registry, plugins included;
      bundled sample YAML files deleted; `samples.py`'s bundled-file machinery retired;
      sample-pinning tests replaced by renderer tests (every kind renders, loads through the
      manifest path, and builds a registry; fixture-schema renderer unit tests).
- [ ] `agw resource describe` (or the sibling surface the LLD names) renders the field reference for
      kinds and capabilities from the same stream.
- [ ] Prose blurbs authored for every bundled kind and capability (content lifted from today's
      samples' narrative lines, field lists dropped).

### 2.9 Pointer sweep and docs promotion (FR16, FR4 tail)

- [ ] One-time sweep: guides, command help, and remediation text discussing a config shape point at
      the rendered sample / describe surfaces; redundant hand-stated field lists deleted
      (narrative-necessary ones may stay).
- [ ] Permanent-doc promotion: `capabilities/README.md` rewritten for the declare-schema contract
      (the invoked-validation sections and their standing deprecation notes retire);
      `docs/guides/resources.md` updated; the superseding ADR extended or a sibling ADR added for
      the schema model if the phase 1 ADR did not already cover it.
- [ ] Dated lockfile entries: resource-manifests (Phase 5.7 invoked-validation contract retired;
      sample machinery replaced) and vm-sites (its "schema-registration is future work" deferral
      resolved).
- [ ] SDD cspell words promoted to root wherever they now appear in permanent code or docs.

### 2.10 Stretch: settings schema (FR14; descope without renegotiating)

- [ ] Settings sections (`[operator]`, `[paths]`, `[plugins]`, `[defaults]`, `[secret_config]`,
      `[session.config]`) declared as models under the same regime, validated through the bridge
      with TOML line framing.
- [ ] config.toml JSON Schema emitted; taplo association documented (draft-4 subset decision made
      here, not before).

## Closeout

- [ ] Full-suite gates green; end-to-end live verification (fresh config init, sample-driven
      resource authoring with editor schema association, vm-site declare, migrate fixture, doctor).
- [ ] Final `agentworks-reviewer` pass over the whole branch; findings fixed.
- [ ] `locked.md` written summarizing final state, decisions, and deviations; PR #316 to non-draft;
      Copilot review triaged.

## Pressure-test notes (what writing this plan surfaced)

- The migrator pre-side "relocate the loaders into migrate/" candidate resolves FR2, verification
  independence, and loader removal with one move; the phase 1 LLD must confirm it or beat it.
- The ops-read-typed-fields migration (2.3) is the hidden bulk of the capability flip: azure and
  proxmox op code reads config dicts today, and the HLA's one line about it is a multi-file change
  with real review surface.
- #349's dual-shape normalization, warning channel, and bundle gate are deliberately temporary; 2.4
  removes them. Building then removing is the cost of the runway, accepted.
- The onepassword mapping model is itself a union (string or table), a good early test of union
  ergonomics inside a backend mapping model.
- The github scope rules (repos/owner mutually exclusive, list shapes) exercise model validators
  beyond field types on day one of 2.3.
- Deleting bundled samples (2.8) also retires the strip-one-`#` sample test convention; the renderer
  tests replace that coverage, and `agw resource sample`'s CLI contract (kind selection, `--all`,
  `--write` append-only behavior) must be pinned before the swap so the interface provably survives.

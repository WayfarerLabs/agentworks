# Plan: registry readiness refactor

Implements the [FRD](./frd.md) per the [HLA](./hla.md). The migration checklist and the R11 guard
baseline are the dated [caller inventory](./caller-inventory.md).

**Phasing principle: every phase ends green** (full gate passes: `ruff format --check`, `ruff`,
`mypy` over `agentworks/` and `tests/`, and the JS linters via `./scripts/lint-files.sh`; the test
suite passes). The phases are commit-by-commit reading order inside **one PR** (#289); none carries
independent standalone value, so this is not a multi-PR split. The one hard constraint the ordering
exists to honor is that **phase 3 is atomic** (R13 + fold + R12 gating + suppression removal land
together, or a non-green window regresses R12); every other phase is a clean green boundary.

The plan is lead-owned. Each implementation phase is delegated to an `agentworks-dev` subagent with
the governing LLD, the caller-inventory rows it touches, and the definition of done; each is
reviewed by `agentworks-reviewer` (reviewer tier >= dev tier) plus a fresh-eyes pass before the
phase is considered done.

## Definitions of done, global

- **DoD-green**: the full gate passes (above); no `# type: ignore` added without justification.
- **DoD-behavior**: the FRD R9 delta list is the acceptance contract. Every delta is pinned by a
  test that asserts the new behavior; every non-delta behavior has a test asserting it is preserved.
- **DoD-docs**: any permanent doc/help/completion made false or stale by the phase is updated **in
  the same phase** (SDD "lockstep with the change that makes the doc factual" rule).

## Phase 0: artifacts (this phase)

- [x] FRD reviewed and stable (multiple focused reviews, a two-reviewer clean-slate pass, a delta
      re-review).
- [x] HLA reviewed and stable (same).
- [x] Dated caller inventory produced ([caller-inventory.md](./caller-inventory.md)).
- [x] Five LLDs authored and reviewed:
  - [x] (a) [graph structure + query semantics](./graph-lld.md)
  - [x] (b) [finalize pass ordering](./finalize-ordering-lld.md) (owns the guard's banned-pattern
        definition + exemptions)
  - [x] (c) [readiness-fold contract](./readiness-fold-lld.md)
  - [x] (d) [secret-resolution layer](./secret-resolution-lld.md)
  - [x] (e) [operator surfaces](./operator-surfaces-lld.md)
- [x] Plan + LLDs pass `agentworks-reviewer` (two review rounds: all findings closed, greenness
      scaffold pinned).

## Phase 1: split the capability contract (`dependencies` + `validate`)

Governs: R2. LLD: (a) for the contract shapes; caller inventory section A.

The split lands for **all four** capability kinds up front, with resources still calling **both**
methods where they called `validate_config`, so behavior is identical (this phase is a pure
refactor, no behavior delta).

- [x] Add `dependencies(config) -> tuple[ConfigReference, ...]` (total, never raises) and
      `validate(config) -> None` (throwing) to the capability contract, replacing `validate_config`
      on the base and all impls: harness (`shell`, `claude_code`), vm-platform (`lima`, `proxmox`,
      `azure_vm`), git-credential-provider (`github`, `azdo`). The base "accepts no configuration"
      default becomes a no-op `dependencies` + a throwing `validate`.
- [x] `secret-backend` joins the split: `SecretBackend` gains `dependencies(mapping)` alongside its
      existing `validate_mapping` (which is already `validate(mapping)`'s shape); confirm the
      `would_attempt` purity constraint (pure function of `(secret, mapping)`, no host probing)
      holds for all three impls (`env-var`, `prompt`, `onepassword`).
- [x] Centralize the sourceless to sourced conversion (`ConfigReference` to `SecretReference` /
      `ResourceReference`, attaching `source`) into **one** helper, replacing the triplication in
      the three `referenced_resources` bodies (`vms/sites.py`, `git_credentials/credential.py`,
      `sessions/template.py`).
- [x] Resource-level `referenced_resources()` bodies call `capability.dependencies(config)` for edge
      extraction; the separate `validate(config)` call is retained wherever `validate_config` was
      invoked for its throwing side effect (decode, load, construct), so validation timing is
      unchanged **this phase** (it moves in phase 2b). **The resource-level method keeps its name
      `referenced_resources()` this phase**; the rename to the uniform `dependencies(context)` and
      the build-context threading land in phase 4 (where the one consumer of `context`, the secret,
      needs it). This answers "when does `context` appear": not until phase 4.
- [x] Split the remaining `validate_config` invocation sites so the method can be removed: the
      resolve-time `sessions/templates.py:175` call becomes `validate(config)` (LLD e confirms
      whether it is redundant with the finalize pass and removable, or a distinct resolved-view
      check), and the three `migrate/planning.py:458,502,548` dry-run calls become `validate` (not a
      finalized-registry path, so they keep their explicit validation).
- [x] Construct-time `_secret_refs` (`capabilities/base.py:306`) sources from
      `dependencies(config)`; construct still calls `validate(config)` to preserve the
      construct-time invariant (R3).
- [x] Tests: unit tests for each kind's `dependencies` (total, non-throwing on malformed config) and
      `validate` (throws with the same messages as the old `validate_config`); a test that
      `dependencies` returns the same edges `validate_config` did for valid config.

**DoD:** DoD-green; `validate_config` no longer exists; no behavior delta yet (validation still runs
at decode/load/construct); the `dependencies`/`validate` split is complete across all four kinds.

## Phase 2: introduce the retained `DependencyGraph`

Governs: R1, R11 (structure only). LLD: (a). Caller inventory section E (field-to-graph).

- [x] Add the frozen `DependencyGraph` (component 1): per node `(kind, name)`, outbound edges
      (`ResourceReference`), inbound references (`ReferenceEntry`), a readiness slot (populated in
      phase 3), enablement, and (capability nodes) the impl reference. Query API: `edges_of`,
      `dependents_of`, `reachable_from`, `readiness_of`, `is_ready`.
- [x] `finalize` builds and retains the graph from the edge map it already accumulates (`all_refs`),
      re-keyed by source for outbound; the `Registry` holds it after finalize alongside
      `_resources`. The builder still walks `referenced_resources()` (no `context` yet); secrets are
      still leaves (they do not emit `secret -> secret-backend` edges until phase 4), so the graph
      is a pure structural retention of today's edge set with no behavior delta.
- [x] Populate the capability nodes' `impl` field (LLD a): the publisher stamps each capability's
      impl onto its graph node (or the builder reads it from the code registry under the whitelisted
      builder exemption), so the fold (phase 3) and resolution (phase 4) read impls off the graph,
      not the live registry.
- [x] Move inbound references off the resource dataclasses onto the graph: remove the `references`
      field from `DeclaredResource` and each capability `Entry` (`harness/kinds.py`,
      `git_credential/kinds.py`, `vm_platform/__init__.py`, `secrets/kinds.py`); route both readers
      (`resources/inspect.py`, `secrets/inspect.py`) to `dependents_of`. This is the phase's one
      observable-internal change; rendered output is identical.
- [x] `reachable_from` implements the transitive closure `collect_secrets_for` needs (consumer still
      on its old path until phase 4, but the query must exist and be correct now).
- [x] Tests: graph query unit tests (`edges_of`/`dependents_of`/`reachable_from` on a fixture with a
      multi-hop chain and a diamond); a test that `dependents_of` reproduces exactly what the
      removed `references` field held.

**DoD:** DoD-green; the graph is retained and queryable; `references` fields are gone from the
dataclasses; consumers other than the reference readers still use their old recompute paths
(migrated in phase 4); no rendered-output delta.

## Phase 2b: move capability-block validation into the finalize `validate` pass

Governs: R3. LLD: (b) for the pass; caller inventory section A (MOVE rows).

The green-window care (HLA sequencing): decode/load must **stop** throwing on a capability block in
the **same change** the finalize `validate` pass **starts**, so no window validates neither.

- [x] Add the resource-level `validate()` aggregator (mirroring the resource-level edge-extraction
      method; it reads the resource's own fields, no `config` param): it pulls each capability
      sub-block and calls that capability's `validate` (phase 1), centralizing what decode/load call
      inline today. This is the method the finalize pass invokes per node.
- [x] Add the finalize `validate` pass: after the graph is built, run each present resource's
      `validate()` (which calls its capabilities' `validate`) with precise `file:line` framing. This
      phase runs it over **every** present resource (the readiness gating that scopes it to
      ready+enabled arrives in phase 3; until then all nodes are effectively ready, so scope is
      identical).
- [x] Remove the `validate_config`-for-validation calls from `manifests/decode.py:176,242,310` and
      the TOML loaders (`config/loaders_sessions.py:163`, `config/loaders_core.py:384`,
      `config/loaders_resources.py:430`) in the same commit.
- [x] Keep construct-time `validate` (the R3 invariant) and the `migrate/planning.py` dry-run
      validations (not finalized-registry paths).
- [x] Tests: a malformed capability block still raises a hard, `file:line`-framed error at
      `build_registry` (moved, not lost); the R9.3 reorder is pinned (a config with both a malformed
      block and a cycle now reports the cycle first); construct-time validation still fires for a
      constructed capability (R3 invariant test).

**DoD:** DoD-green; DoD-behavior for R9.3; capability-block validation happens **once**, in finalize
(plus the construct-time invariant); decode/load no longer validate capability blocks.

## Phase 3: the atomic readiness step (R13 + fold + R12 gating + suppression removal)

Governs: R4, R5, R7, R12, R13. LLDs: (c) fold, (b) finalize ordering. **This phase is one atomic
commit** (or a tight, individually-green sequence that is merged as a unit): the vm-site
edge-suppression does two jobs, R13 replaces job 1 and R12 replaces job 2, and splitting them opens
a non-green window where the suppression is gone but materialization is ungated (an R12 regression).

- [x] **R13 unconditional publication**: `vm_platform.publish_to` drops the
      `unsupported_reason() is not None: continue` skip; every installed platform publishes a
      `vm-platform` row. `unsupported_reason` becomes an input to the platform node's readiness.
- [x] **The `not_ready` reshape** (rename from `disabled_reason`): capability
      `not_ready(config) -> Readiness` is a **non-constructing classmethod** (does not build an
      instance, does not validate; tolerates malformed config); consuming-resource
      `not_ready(deps) -> Readiness` (an instance method; its config is `self`'s own fields) is pure
      over its own config and its deps' states. Add the `Readiness` verdict object and the
      `DependencyState` (carrying a dep's enablement and, when enabled, its readiness).
- [x] **The readiness fold**: finalize walks the graph reverse-topologically (after cycle
      detection), hands each node its deps' `DependencyState`, calls the node's `not_ready`, and
      stores the `Readiness` verdict on the graph node. Self-determined: the fold imposes no
      propagation rule. `vm-site` propagates from its single platform; `secret` implements no
      `not_ready` (always ready).
- [x] **Where the `site_disabled_reason` chain lands** (LLD c): platform-missing to the resolve-time
      hard error; platform-unsupported to the platform node's `not_ready`; the config-dependent tool
      check (local-Lima needs `limactl`) to the site's `not_ready`, calling the platform's
      `not_ready(site_config)` **off the graph node's impl**, non-constructing.
- [x] **R12 readiness-gated materialization**: the finalize ordering becomes build to resolve
      error-misses to cycle-detect to fold to **readiness-gated materialize (looping)** to attach to
      validate to freeze (component 3). A not-ready or disabled node's config-implied edges do not
      drive auto-declaration. The materialize **loop** structure lands here, but it is **dormant for
      secrets this phase**: secrets do not emit `secret -> secret-backend` edges until phase 4, so a
      materialized secret has no out-edges to walk yet. R12 is therefore tested this phase via the
      existing `site -> secret` edges (a host-disabled site's secret must not materialize); the
      loop's load-bearing secret case (a materialized secret's backend edges) and its VM-create
      acceptance test move to phase 4, where those edges exist.
- [x] **Remove the vm-site edge-suppression** (`sites.py:60-71`): the vm-site always emits its
      platform edge (the platform node is always present under R13).
- [x] **Enablement axis, modeled not produced**: the graph node carries `enabled | disabled`, and
      the fold takes an `enablement` map (all-enabled now, the extension point the plugin rebuild
      fills) so it **distributes** a disabled dep's `DependencyState` (readiness `None` when
      disabled). An **end-to-end test** finalizes a registry with an injected disabled `vm-platform`
      and asserts the dependent site's folded `readiness_of` verdict is the "enable its unit"
      string, proving the fold's distribution (not just the leaf `not_ready` branch).
      `has_ready_referrer` excludes a disabled referrer (R12 holds for the disabled case). No real
      producer ships (R7).
- [x] **Validate pass gated**: the finalize `validate` pass (phase 2b) now runs over the **ready +
      enabled** set only (R3, R9.4).
- [x] Tests (DoD-behavior): R9.2 (typo'd platform is now a hard error, not a silent self-disable);
      R9.4 (a not-ready resource's malformed block is deferred, not validated); R9.5 **at the graph
      level** (an installed host-unsupported platform, e.g. `wsl2` on Linux, has a **present**
      `vm-platform` node whose `readiness_of` is not-ready). NOTE (reconciled after implementation):
      because R13 publishes the row here, the wsl2 vm-platform row also **renders** in
      `resource     list` from phase 3 onward (not-ready, **old vocabulary**), via a thin
      `_VMPlatformKind` readiness projection pulled forward to render the now-published row sanely.
      Phase 3's tests still assert only the **graph verdict**; phase 4's R9.5 job narrows to the
      rendered-row test assertion, the `inspect` projection swap for the remaining kinds, and
      (phase 5) the vocabulary rename. R12 (a host-disabled site's secrets stay absent while a ready
      site's still materialize, via `site -> secret` edges); the bundled `wsl2` site is **not-ready,
      not a hard error** on a non-Windows host; the fixture disabled-node propagation; R5 (a ready
      resource with a malformed block still fails validation; a not-ready resource with a valid
      block is still not-ready).

**DoD:** DoD-green; DoD-behavior for R9.2, R9.4, R9.5, R12; readiness is stored on the graph;
suppression is gone; host-unsupported is readiness, not absence; the enablement axis is modeled and
fixture-tested with no producer.

## Phase 4: migrate consumers onto the graph

Governs: R10, R11, and the R9 secret deltas (R9.6, R9.9, R9.10, R9.11). LLDs: (d) resolution,
(a)/(b) queries. Caller inventory sections B, C, F. Each consumer is an independently-green
sub-step.

- [x] **Head step: the uniform `dependencies(context)` rename + build context + secret backend
      edges.** Rename the resource-level `referenced_resources()` to `dependencies(context)` across
      every resource (caller inventory section C: `vms/sites.py`, `vms/templates.py`,
      `vms/template.py`, `vms/admin.py`, `git_credentials/credential.py`, `sessions/template.py`,
      `agents/template.py`, `apt.py`, `workspaces/template.py`, `declared_resource.py`; **not**
      `env/entry.py`'s arg-taking variant). Thread the build `context` (a `BuildContext` carrying
      the available-backend list; the "read-only graph-in-progress" field the earlier draft
      mentioned was deliberately not built, no resource consumes it) from the builder walk
      (`registry.py:321`) to every resource; most ignore it. The **secret's**
      `dependencies(context)` now emits the `secret -> secret-backend` edges (the union of present
      would-attempt backends and every explicit non-`false` mapping key, LLD d / HLA component 2).
      This is the deliberate landing point for R9.9/R9.10/R9.11 and makes the materialize loop
      load-bearing (a materialized secret now has backend edges). **Greenness scaffold:** the rename
      breaks callers that invoke `referenced_resources()` by name, the two node factories
      (`vms/nodes.py:412`, `git_credentials/nodes.py:93`, direct `decl.referenced_resources()`
      calls) and, via the `registry.py:397` getattr helper, cycle detection, which each migrate to
      graph reads in their **own** later sub-steps. So this step retains a thin
      `referenced_resources()` alias delegating to `dependencies(<empty context>)` (context is
      unused by every non-secret caller) so those sub-steps stay independently green; the alias is
      removed when its last caller migrates, before the phase-6 guard (which would flag a consumer
      re-walking `dependencies`). The builder walk itself is pointed at `dependencies(context)` in
      this step (it is the one caller that needs the real context). Tests: the materialize-fixpoint
      VM-create acceptance (an auto-declared `tailscale-auth-key`'s backend edges exist and resolve;
      the no-loop regression is pinned here); R9.11 (a typo'd `backend_mappings` key is a dangling
      edge that hard-errors).
- [x] **Cycle detection** reads `edges_of` (removes the second
      `validate_config`/`referenced_resources` pass at `registry.py:542`). Landed in Phase 2:
      `_detect_cycles` three-colors over the built `all_outbound` map, no re-walk.
- [x] **`walk.collect_secrets_for`** becomes a thin filter over `reachable_from`; its caller
      (`secrets/kinds.py:188`) is unaffected.
- [x] **Node factories** `vm_site_node` (`vms/nodes.py:412`) and `git_credential_node`
      (`git_credentials/nodes.py:93`) read secret edges off `edges_of`.
- [x] **`inspect`** reads readiness via `readiness_of` and usage via `dependents_of` (R10); the list
      cell and describe line adopt the readiness vocabulary (folded into phase 5's surface work, but
      the projection swap is here). NOTE (reconciled after phase 3): the **rendered** R9.5 not-ready
      `vm-platform` row already appears in `resource list` from phase 3 (old vocabulary), via a thin
      `_VMPlatformKind` readiness projection pulled forward there. So phase 4's R9.5 work narrows
      to: pin the rendered-row assertion, swap the remaining inspect projections (vm-site and the
      other kinds) onto `readiness_of`, and retire the phase-3 shim projection in favor of the
      unified `readiness_of` read. The vocabulary rename itself is phase 5.
- [x] **Site selection and the use-time gate**: `select_site` / `resolve_site` /
      `ensure_site_enabled` (`sites.py:146,150,243,258`), doctor's `defaults.site` warning
      (`doctor.py:294-297`), and `resource.py:111` stop calling `site_disabled_reason` (a lazy
      recompute reaching into `VM_PLATFORM_REGISTRY`, an R11-banned pattern) and read `readiness_of`
      off the graph. `ensure_site_enabled` / `resolve_site` / `select_site` gain registry-graph
      access (HLA component 6 flags the `ensure_site_enabled(decl)` signature change). Either
      `site_disabled_reason` is deleted or it survives only as a thin `readiness_of` wrapper that
      takes the graph; the guard (phase 6) rejects the old recompute either way. Operator strings
      adopt the readiness vocabulary in phase 5.
- [x] **Op-time held-capability secret refs** (`Harness.secret_refs`,
      `GitCredentialProvider.secret_name`): the single shared derivation is `dependencies(config)`;
      LLD (d) confirms single-derivation vs graph-threading before implementation.
- [x] **Secret resolution as a distinct layer** (component 6a, LLD d): resolution reads candidate
      backends off `edges_of`, applies the opt-in chain (`secret_config.backends`), walks
      `present ∧ enabled ∧ ready ∧ opted-in` candidates, and `batch_get`s per backend. Deletes the
      `validate_chain` chain re-derivation and the `SECRET_BACKEND_REGISTRY` probe. Skip semantics
      (R9.6): a not-ready opted-in backend is **skipped with a warning** and the chain falls
      through; the anti-masking **halt is kept** for a ready store's hard miss.
- [x] **`validate_chain` splits**: per-mapping spec validation moves into the finalize `validate`
      pass (every declared mapping to a present+enabled backend, R9.9); reachability stays an
      **eager post-finalize boundary check** reading the graph, scoped to operator-declared secrets,
      keyed on **would-attempt** (not readiness). R9.10 (backend rows gain inbound refs) and R9.11
      (a mapping to an unknown backend is now a hard error, with the noted doctor-granularity
      regression) fall out of the `secret -> secret-backend` edges.
- [x] Tests (DoD-behavior): R9.6 skip-with-warning + fall-through, halt-on-hard-miss preserved; R9.9
      (a stale mapping for a configured-but-not-opted-in backend now fails at build); R9.10 (backend
      "Referenced by" gains entries); R9.11 (unknown-backend mapping hard-errors; the doctor
      collapse); reachability invariants preserved (operator-declared-only scope, would-attempt
      keying, soft/hard miss semantics) per LLD (d)'s acceptance line.

**DoD:** DoD-green; DoD-behavior for R9.6, R9.9, R9.10, R9.11; every recompute/probe consumer reads
the graph; resolution is a distinct layer over the graph; no consumer re-derives edges or readiness.

## Phase 5: operator surfaces + docs + completions

Governs: R6 (surface strings), R9.1, R9.7, R9.8. LLD: (e). Caller inventory section G.

- [x] **`secret list` grid**: columns stay the opted-in backends; cells become would-attempt
      identifier / `not ready: <reason>` / won't-attempt, replacing the overloaded
      `disabled`/`enabled` literals (R9.7). Not-ready wins over the identifier; won't-attempt (a
      `false` opt-out or a mapping-required backend with no mapping) wins over not-ready.
- [x] **`secret describe`**: "Backend mappings" and "Resolution preview" become readiness-aware; the
      preview walks `present ∧ enabled ∧ ready ∧ opted-in` and shows skipped not-ready backends;
      interactive-optimism preserved (pinned by a test).
- [x] **Doctor**: a **new secret-backends group** (one readiness row per backend, parallel to
      `_check_vm_platforms`); `_check_vm_platforms`/`_check_vm_sites` read stored readiness off the
      graph (`_check_vm_platforms` now takes the registry, retiring its live-registry probe);
      `_check_secrets` becomes readiness-aware; the live `preflight` (network) stays.
- [x] **`--reveal-secrets` to `--resolve`** (R9.8): renamed the `env show` flag; kept
      `--reveal-secrets` as a hidden, deprecated alias for one release (single stderr deprecation
      notice, hidden from help + completions), per LLD e.
- [x] **The preflight resolvability predictor** (`secrets/resolve.py`, `preview_resolution`, used by
      `orchestration/secrets.predict_resolution`) becomes readiness-aware in lockstep, so it never
      predicts "would resolve via onepassword" for a backend resolution will skip (R9.6
      consistency).
- [x] **Readiness vocabulary rename across all surfaces** (R9.1, R6): every remaining "disabled"
      operator string for host readiness (`resource list`, `describe`, `doctor`, site selection and
      use-time strings, plus the fold-stored strings in `vms/sites.py` and the platform-node reason
      in `resources/graph.py`) adopts the readiness vocabulary; "enabled/disabled" is reserved for
      opt-in. `ensure_site_enabled` renamed `ensure_site_ready`; inspect's `disabled_reason`
      field/projection renamed `not_ready_reason` / `not_ready_reason_for`.
- [x] **Docs + completions (DoD-docs)**: `docs/guides/resources.md` "Secrets: backends and the
      chain" (the present/enabled/ready/opted-in/would-attempt vocabulary + not-ready skip),
      `sample-config.toml`, `cli/README.md` (the `--reveal-secrets` mention), command/section help
      strings; the completion tree regenerates live from the Typer-extracted spec (verified
      `--resolve` present, the hidden alias absent).
- [x] Tests (DoD-behavior): R9.1 (surface strings), R9.7 (grid cell states), R9.8 (`--resolve`, and
      the deprecated alias); LLD (e)'s acceptance line that interactive-optimism preview is
      unchanged.

**DoD:** DoD-green; DoD-behavior for R9.1, R9.7, R9.8; DoD-docs; every operator surface is
readiness-aware and vocabulary-clean; completions regenerated.

## Phase 6: the anti-bypass guard

Governs: R11. LLD: (b) (the guard's banned-pattern definition + exemptions). Caller inventory guard
baseline.

- [x] Add the guard test pinning the four banned patterns (inventory guard baseline), with both
      exemptions encoded (the construct-time single-derivation, and the builder-supplied context),
      or the honest path and the banned pattern are the same call.
- [x] Confirm the caller inventory has zero remaining un-migrated rows; the guard's baseline matches
      HEAD.

**DoD:** DoD-green; the guard passes and would fail if any banned pattern returned; the caller
inventory is fully discharged.

## Closeout

- [ ] All R1-R13 satisfied; the R9 delta list fully pinned by tests.
- [ ] SDD load-bearing content promoted out of the SDD where it belongs in permanent docs (the
      readiness vocabulary and the backend/opt-in model into `docs/guides/resources.md`; any
      graph-API contract that outlives the SDD into a module README), per the SDD-not-permanent
      rule.
- [ ] `locked.md` written summarizing the final state (created on-branch pre-merge; the lock binds
      when it lands on `main`).

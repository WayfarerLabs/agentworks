# Locked: registry readiness refactor

**Locked 2026-07-30.** The SDD is complete; all six implementation phases landed on
`feat/registry-readiness-refactor` (PR #289) and the full gate is green (2570 tests, `ruff`, `mypy`
over `agentworks/` + `tests/`, and the JS linters). The lock binds when this file lands on `main`;
pre-merge edits on the branch remain ordinary in-flight changes.

## What shipped

The resource registry now **produces and retains a first-class `DependencyGraph`** as the product of
`finalize`, decoupled from config validation. Readiness is a **dependency-ordered fold** computed
once and stored on the graph; the readiness hook is `not_ready` and "enabled/disabled" is reserved
for an opt-in **enablement** axis. Secret **resolution** is a distinct layer over the graph. An AST
**anti-bypass guard** pins the single-access-path invariant. Behavior is preserved for a valid,
enabled, ready configuration except for the enumerated R9 deltas.

The five original ideas, as delivered:

- **Graph, not recompute.** `finalize` runs ordered passes (reserved-defaults, build, resolve,
  cycle-detect, readiness-fold, readiness-gated materialize with a fixpoint loop, attach, validate,
  freeze) and retains a frozen `DependencyGraph` (`resources/graph.py`) with the query API
  `edges_of` / `dependents_of` / `reachable_from` / `readiness_of` / `is_ready` / `impl_of` /
  `enablement_of`. Every consumer reads the graph; the guard (`tests/resources/test_graph_guard.py`)
  forbids re-derivation.
- **`dependencies` / `validate` split.** The fused `validate_config` is gone; each capability kind
  and the secret-backend contract expose total non-throwing `dependencies` and throwing `validate`.
- **Validation as a distinct eager pass** at `finalize` (over the ready+enabled set), plus the
  construct-time invariant; decode/load no longer validate capability blocks.
- **Self-determined readiness fold** with a `Readiness` verdict and a `DependencyState` distributed
  to each node; the fold never constructs (avoids re-running the throwing construct-time validator).
  Host-support (`unsupported_reason`) is a not-ready verdict on a present node, not absence.
- **Secret resolution over the graph**: candidate backends off `edges_of`, filtered
  `present ∧ enabled ∧ ready ∧ opted-in`, walked in opt-in-chain order; a not-ready backend is
  skipped with a warning; the anti-masking hard-miss halt is kept.

## Requirement coverage (R1-R13)

All satisfied. Landed across the phases below; see the FRD for the full statements.

- **R1** retained first-class graph, total build: `resources/graph.py`, `resources/registry.py`
  (Phase 2).
- **R2** `dependencies`/`validate` split across all four kinds + the resource-level rename (Phases
  1, 4).
- **R3** eager validation pass + construct-time invariant (Phase 2b, 3).
- **R4** dependency-ordered self-determined fold stored on the graph (Phase 3).
- **R5** readiness is not validity (Phase 3; both-direction tests).
- **R6** `not_ready` rename; "enabled/disabled" freed (Phases 3, 5).
- **R7** three availability tiers; enablement axis modeled and fold-distributed, proven with a
  disabled fixture, no real producer ships (Phase 3).
- **R8/R12** one graph, ordered passes, readiness-gated materialization with the fixpoint loop
  (Phase 3).
- **R9** the enumerated deltas, each test-pinned (below).
- **R10** readiness projected off the graph, cheap (Phases 4, 5).
- **R11** single access path + the anti-bypass guard (Phases 4, 6).
- **R13** unconditional capability publication; host-support is readiness (Phase 3).

## R9 delta list (all pinned by tests)

R9.1 vocabulary rename; R9.2 typo'd capability reference is a hard error; R9.3 error reorder +
owner/origin re-framing; R9.4 not-ready block deferred; R9.5 host-unsupported platform is a present
not-ready row; R9.6 not-ready backend skips with a warning (hard-miss halt kept); R9.7 grid cell
states; R9.8 `--reveal-secrets` to `--resolve`; R9.9 every declared mapping validated; R9.10 backend
rows gain inbound refs; R9.11 unknown-backend mapping is a build-time hard error (with the
acknowledged doctor-granularity regression). Tests live under `cli/tests/` and
`cli/tests/resources/` (`resources/test_readiness_fold.py`, `test_capability_config_contract.py`,
`test_secrets_resolve.py`, `test_secret_describe.py`, `test_secrets_inspect.py`,
`test_doctor_env_and_secrets.py`, `test_env_show_flag.py`, `test_completions.py`, and the
graph/guard suites).

## Where the load-bearing content lives (SDD is deletable)

Nothing operator- or contributor-facing depends on `docs/sdd/`:

- The **operator model** (present / enabled / ready / opted-in / would-attempt; the not-ready
  skip-with-warning resolution behavior; offline backend readiness) is in `docs/guides/resources.md`
  ("Secrets: backends and the chain" / "The words the surfaces use") and `sample-config.toml`.
- The **capability contract** and the **readiness/fold model** are in
  `cli/agentworks/capabilities/README.md` and `cli/agentworks/capabilities/vm_platform/README.md`.
- The **graph API** is documented in `resources/graph.py`; the **guard's** banned patterns and
  exemptions are documented in `tests/resources/test_graph_guard.py` itself.
- `--resolve` is in `cli/README.md` and the command help.

## Notes for the next effort (the plugin rebuild)

- **The enablement axis is modeled but has no producer.** A node's `enablement` defaults to
  `enabled` via `Registry._node_enablement()`; the plugin rebuild produces disabled state for
  opted-out units. The fold distributes a disabled dependency's state, `has_ready_referrer` and
  resolution exclude disabled units, and a fixture test proves the "enable its unit" propagation end
  to end.
  - **Seam superseded 2026-07-30 by the system-plugin SDD** (`docs/sdd/2026-07-24-plugin-system/`,
    PR #237, stacked on this branch). This note originally said the producer "overrides
    `_node_enablement()`". The plugin design refined the seam: because the `Registry` must stay
    config-agnostic, `_node_enablement()` (a no-arg method) is **removed** in favor of injected
    **enablement sources** (`finalize(enablement_sources=[...])`, where `build_registry` constructs
    each source already bound to config), and the disabled state gains a carried remediation reason.
    The plugin SDD's Phase 4 makes this code change (migrating this refactor's four
    `test_readiness_fold.py` `_node_enablement` tests to inject a stub source instead of patching
    the method); when it lands, this supersession is already recorded here. The consumer rule below
    (gate on enablement, not readiness) is unchanged and honored.
- **Disabled-node consumers must gate on enablement, not readiness.** A disabled node's own
  readiness is a `Readiness.ready()` placeholder (readiness is computed only for enabled nodes);
  every consumer that must exclude a disabled node reads `enablement_of` (or the fold verdict of a
  dependent that already folded enablement in), never `is_ready` on the disabled node itself. The
  plugin rebuild's new disabled-node consumers must follow this rule.

## Accepted residuals (documented, not defects)

- The guard is **module-scoped** (a probe reintroduced inside an already-exempt module escapes
  patterns 2/3); the softest module (`vms/sites.py`, exempt for patterns 1/2/3) is closed by a
  function-scoped pin, and the detectors match qualified and aliased registry reads and
  annotated/plain/property `references` fields. Deep call-through-a-variable indirection
  (`fn = decl.dependencies; fn(ctx)`) is not chased (a deliberate two-line form; no accidental
  regression takes it).
- The R9.11 hard error means a typo'd `backend_mappings` key fails any registry build, consistent
  with every other unknown-reference; `doctor` reports it as one "Resource registry: FAIL" row
  rather than pinpointing the secret.

## Phase commit map (branch `feat/registry-readiness-refactor`)

- Phase 1 (contract split): `de898ce8`.
- Phase 2 (retained graph): `5d00a7f5`, review `6b256358`.
- Phase 2b (validation to finalize): `a2717c62`, review `33505cde`.
- Phase 3 (atomic readiness): `802a194f`, review `f7ecafbf`.
- Phase 4 (consumers + resolution): `c1c9179a` .. `8c446f6c`.
- Phase 5 (surfaces + vocab + docs): `0e3c6515` .. `a41cde42`.
- Phase 6 (the guard): `0ada7409`, review `11937c29`.

Each phase was reviewed by an `agentworks-reviewer` pass and a fresh-eyes pass (the atomic Phase 3
at Fable tier with a delta re-review); a capstone verification pass signed off the whole before
merge.

## 2026-08-07: the validate pass is no longer scoped to READY and ENABLED

Recorded here because this effort's R3 and R9.4 stated that scope, and the declarative-schema effort
(`docs/sdd/2026-07-31-declarative-schema/`) removed it.

The scope let a resource's own malformed config decide whether that config got checked. Readiness is
computed at finalize pass 4 from config the validate pass has not yet validated, so a misspelled
`vm_host` key read as an absent one, which made a remote lima site look local, which made it
not-ready for want of `limactl`, which suppressed the error naming the typo. The same document was
correctly refused on a host that happened to have `limactl` installed. Closed-world config that is
closed only on some hosts is not closed (operator ruling, 2026-08-07: unexpected keys are config
errors everywhere unless the capability's own schema allows them, enforced like anything else).

Finalize pass 7 now validates every present resource unconditionally. What this effort's FRD gave as
the justification, "there is nothing to run, and its capability may be unavailable", is served
without the gate: an implementation the host has not seated selects no model, so the call is a no-op
and the dangling capability edge reports it once as the R9.2 finalize miss.

**R9.4's load-bearing half is untouched.** The readiness fold stays total over unvalidated config
because `not_ready` is non-constructing, which is what prevents a malformed block from becoming a
permanent readiness reason that defers its own validation. That contract never required the validate
pass to be gated; the two were coupled by accident. A test now pins non-construction directly on
`not_ready` rather than by way of the deferral behavior.

One enablement-keyed suppression survives by design: R9.9's skip of a mapping addressed to a
present-but-disabled secret backend. It is recorded as an explicit exception at its site.

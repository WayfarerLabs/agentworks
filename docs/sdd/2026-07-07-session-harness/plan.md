# Session harness capability: implementation plan

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

Read `frd.md` and `hla.md` first; this plan pins the phasing, the exact swap anchors, and the
definition of done per item. The orchestration layer is merged and locked (`orchestration/`,
`capabilities/base.py`, `sessions/nodes.py`); the harness plugs into it and never modifies it.

## How this plan is phased

The work is cut into always-green vertical slices. Every merged phase is complete and honest on its
own: it either adds a fully-formed capability nothing yet consumes (the git-credential precedent),
or it moves a consumer wholesale so no half-reshaped representation ever lands on `main`.

The single load-bearing sequencing decision (ratified by the lead, see "Decisions"): the node and
orchestrator **swap lands BEFORE the template-surface change**. Phase 3 wires the session node onto
a `shell` harness built from `ResolvedSessionTemplate`'s still-flat fields, retiring
`RequiredCommandsCheck` and `_build_session_command`; Phase 4 then reshapes the template to the
`(harness, harness_config)` pair and deletes the flat fields. This keeps the risky orchestrator
wiring isolated from the dataclass reshape, and each is its own green slice. The alternative (one
combined surface-plus-swap phase) is larger and mixes two unrelated risk surfaces; swap-first is the
recommendation.

Named interim seams and the phase that retires each:

| Seam                                                                                                         | Introduced | Retired                                               |
| ------------------------------------------------------------------------------------------------------------ | ---------- | ----------------------------------------------------- |
| `require_commands` probe helper duplicates `RequiredCommandsCheck._probe` (copy, not move)                   | P1         | P3 (delete `RequiredCommandsCheck`)                   |
| Harness hardcoded to `shell`, built from `ResolvedSessionTemplate`'s flat fields via a small factory adapter | P3         | P4 (build from the resolved pair; delete flat fields) |
| `harness/claude-code` row published but not selectable from any template                                     | P2         | P4 (template `harness:` selection lands)              |

Both built-in rows render via `resource describe` with an empty `Referenced by:` for the phases
before a template can select them (`shell` P1 through P3, `claude-code` P2 through P3). This is the
standard additive-capability pattern (git-credential rows behave the same before any consumer), a
consciously accepted bit of interim operator-facing emptiness, not a defect.

Permanent-doc updates are threaded into the phase that makes each claim true (the SDD "docs ride the
change" rule), not deferred to a closeout. Only the ADR numbering/promotion and the final lock live
in the closeout phase.

## Pinned anchors (code at HEAD)

The swap and mirror points, cited so each phase edits the right lines:

- Interim readiness stand-in: `sessions/nodes.py:50-200` (`RequiredCommandsCheck`; four-way fork at
  `_check` `93-139`, plus a fifth `scope is None` loud branch at `97-106`; probe body `_probe`
  `141-200`). Node delegation: `LiveSessionNode.preflight/runup` `241-245`,
  `PendingSessionNode.preflight/runup` `290-295`. Construction: `pending_session_node` `390-397`,
  `live_session_node` `437-444` (both take `session_name`, `template_name`, `required_commands`,
  `target`, `admin`, `vm_name`; neither takes `workspace_name` today).
- Interim pane path: `sessions/manager.py:872-897` (`_build_session_command`); call sites
  `1932-1934` (create, op-time) and `2483-2488` (restart, op-time). Op-start `RunContext` assembled
  for runup at `1873-1880` (create; carries `admin_target`/`agent_target`); restart readiness runs
  via `preflight_all` with the ctx at `2373-2381` (no runup ctx on the restart path). `scoped_ctx`
  secrets-only ctx at `1764-1769`. `OperationScope` build at `1665-1673` (create) / `2302-2310`
  (restart).
- Template dataclass: `sessions/template.py:41-70` (`SessionTemplate`, flat fields `48-51`;
  `referenced_resources()` `56-70`).
- Resolver: `sessions/templates.py:21-31` (`ResolvedSessionTemplate`), merge walk `_merge` `112-119`
  and `_merge_template` `122-135`.
- TOML loader: `config.py:1044-1086` (`_load_session_templates`, `_SESSION_TEMPLATE_KEYS` `1044`).
- Manifest decoder: `manifests/decode.py:170-177` (`_decode_session_template`); mirror
  `_decode_git_credential` `179-257` (its flat-field rejection + `validate_config` invocation).
- Migration: `migrate/planning.py:450-549` (`_emit_document`; mirror the `git-credential` branch
  `506-549`).
- Capability mirror: `capabilities/git_credential/{__init__.py,kinds.py,base.py}`. Kind index:
  `resources/kinds/__init__.py`. Publisher block: `bootstrap.py:51` (import) and `84-90`
  (`publish_to` calls; add `harness.publish_to` alongside `git_credential.publish_to` at `86`).

## LLDs (write early, before the code they pin)

- [x] **Write `harness-api-lld.md`** (before Phase 1). Pins: the `Harness(Capability)` constructor
      and `owner_kind`; `validate_config` shape-only-at-load vs completeness-on-merged-blob split;
      the `merge_config` classmethod hook and `shell`'s `required_commands` union override; the
      readiness fork relocation including the fifth `scope is None` branch and the new SESSION-level
      identity guard (raise vs warn; which fields compared); the `require_commands` probe helper
      signature; the op-start `RunContext` assembly at the op call sites (targets + scoped secrets)
      and the relocation of template-variable substitution to wrap the harness's returned string;
      the `ResolvedSessionTemplate` reshape and `_merge_pair` walk. **Done when:** every Phase 1-4
      interface question in the HLA "Open questions / for LLD" list has a pinned answer or an
      explicit deferral, reviewed.
- [ ] **Write `claude-code-lld.md`** (before Phase 2). Pins: the resume-vs-launch detection
      mechanism (prefer folding the check into the launch snippet per the HLA decision), verified
      against the latest stable Claude Code CLI at implementation time (latest-stable rule); exact
      flag spellings for `permission_mode` / `model` / `extra_args`; the visible-decision mechanism
      (output line vs pane first output); the fixture/stubbing strategy for testing without a real
      `claude` binary. **Done when:** detection and every flag are verified against the current CLI
      and the test double is specified, reviewed.

## Phase 1: Harness capability + `shell` built-in (unconsumed infrastructure)

Stand up `capabilities/harness/` mirroring `capabilities/git_credential/`. Nothing in `sessions/`
changes; the rows appear in the registry and are inert until Phase 3 consumes them.

- [ ] **Package skeleton** `capabilities/harness/{__init__.py,base.py,kinds.py,shell.py}`.
      `__init__.py` exports `HARNESS_REGISTRY` (name -> class), `harness_for(name)` (registry lookup
      with typed framing), and `publish_to(registry)`. Pure Python; no import of `sessions/` or
      `orchestration/` (layering rule R1). **Done when:** `import agentworks.capabilities.harness`
      succeeds and the module imports neither forbidden package (assert by a layering test).
- [ ] **`base.py`: `Harness(Capability)` ABC** per the harness-api LLD. `owner_kind` is
      `"session-template"`; the constructor takes
      `(owner_name, config, *, session_name, vm_name, workspace_name, target, admin)`; abstract
      `start(ctx)` / `restart(ctx)` return the raw pane string; the optional `merge_config`
      classmethod hook (default shallow `{**base, **child}`); and a shared `require_commands(...)`
      helper carrying the relocated `_probe` body (the `$SHELL -lic 'command -v <cmd>'` loop,
      `check=False`, missing-command error + label parity from `nodes.py:141-200`). **Seam:** this
      helper is a COPY; `RequiredCommandsCheck._probe` stays until Phase 3. **Done when:** `Harness`
      is abstract, the helper reproduces the probe error shape verbatim, and unit tests cover a
      present/missing command against a stub transport.
- [ ] **`kinds.py`: `_HarnessKind` + `HarnessEntry`** mirroring `_GitCredentialProviderKind`
      (`category="capability"`, `miss_policy="error"`, `builtin_override="reserved"`,
      `auto_declare_names=None`, `synthesize` raising `NoUnreferencedDefaultError`); frozen
      `HarnessEntry(name, origin, references)`. Self-registers
      `KIND_REGISTRY["harness"] =     _HarnessKind()` at import. **Done when:** the kind is in
      `KIND_REGISTRY` after importing the module and a `kind: harness` manifest document gets the
      standard capability-kind envelope rejection.
- [ ] **Index + publisher wiring.** Add `import agentworks.capabilities.harness.kinds  # noqa: F401`
      to `resources/kinds/__init__.py`; add the `harness` import at `bootstrap.py:51` and
      `harness.publish_to(registry)` in the built-in block near `bootstrap.py:86`. `publish_to` adds
      one `HarnessEntry` per registered harness with
      `Origin.built_in(source="agentworks.capabilities.harness")`. **Done when:**
      `agw resource     list` shows the `harness/shell` row, `agw resource kinds` lists `harness`
      with its category/description, and `agw resource describe harness/shell` renders.
- [ ] **`shell.py`: the `shell` harness.** Config vocab `command` / `restart_command` /
      `required_commands` (all optional); `validate_config` accepts exactly these, shape-only,
      returns `()`; `start` returns `command` (empty = login shell), `restart` returns
      `restart_command` or `command`; `merge_config` unions `required_commands` (append-dedupe) and
      child-wins the scalars; `preflight`/`runup` call `require_commands`. **Done when:** unit tests
      cover start/restart strings, empty-config login shell, `merge_config` union + scalar override,
      and unknown-field rejection.
- [ ] **Docs riding this phase:** none of the model-narrative docs are true yet (nothing consumes
      the harness), so they wait. The SDD `.cspell.json` gains any new permanent-code vocabulary
      introduced here (e.g. `claude-code` already present in repo; add words only as code needs
      them).

**Tests P1:** kind registration + envelope rejection; `publish_to` row/origin;
`resource list/kinds/describe` surfaces; `shell` start/restart/merge/validate; the layering-import
guard.

## Phase 2: `claude-code` built-in

Add the second member. Still unconsumed by `sessions/` (its row lists; no template can select it
until Phase 4).

- [ ] **`claude_code.py`: the `claude-code` harness** per the claude-code LLD. Config vocab
      `permission_mode` / `model` / `extra_args`; unknown fields are validation errors naming the
      harness and field; `validate_config` shape-only, returns `()`. `start`/`restart` are symmetric
      and state-aware: detect a resumable session named by `self._session_name` and resume it, else
      launch fresh; required executable `claude` via `require_commands`. The chosen path is visible
      to the operator (never silent). Register in `HARNESS_REGISTRY`. **Done when:** with a stubbed
      target, start/restart resume when a session exists and launch fresh when it does not;
      `permission_mode`/`model`/`extra_args` map to the LLD-verified flags; the visible-decision
      output is asserted; no test invokes a real `claude` binary.
- [ ] **Docs riding this phase:** none yet (unconsumed). `docs/guides/resources.md` capability-story
      mention of `harness` may land here since the capability now has both members, but its worked
      session-template examples wait for Phase 4; prefer to land the whole resources.md story in P4
      to avoid a half-example.

**Tests P2:** detection both directions (resumable present/absent) via deterministic stubbing;
config vocab validation + unknown-field error; `extra_args` verbatim passthrough; visible-decision
output; required-command probe uses `claude`.

## Phase 3: Swap the session node onto the harness (retire the interim seams)

The finish-line phase for `RequiredCommandsCheck` and `_build_session_command`. The harness is built
as `shell` from the resolved flat fields (the surface has not changed yet); everything the interim
path did moves onto the harness in one slice.

- [ ] **Factory construction** in `sessions/nodes.py`: `pending_session_node` (`390-397`) and
      `live_session_node` (`437-444`) construct `harness_for("shell")(...)` and hand it to the node,
      replacing the `RequiredCommandsCheck`. Positional args are the template name and a `shell`
      blob built from `template.command` / `restart_command` / `required_commands`; keyword args are
      the captured identity (`session_name`, `vm_name=vm.row.name`, `workspace_name=workspace.name`,
      `target=agent`, `admin`). The one-object target wiring (same agent node as dep and as
      `target`) carries over unchanged. **Seam:** the flat-blob adapter is temporary (retired P4).
      **Done when:** both factories build a `Harness`, the node holds it, and the one-object
      invariant is preserved (asserted by the existing node-identity test).
- [ ] **Node reshape** in `sessions/nodes.py`: `LiveSessionNode` / `PendingSessionNode` hold
      `_harness` instead of `_check`; `preflight`/`runup` delegate to `self._harness`;
      `secret_refs()` folds in the harness's declared secrets (none for built-ins; plumbing
      present). `deps()`, `mark_realized()`, `teardown()`, `key` unchanged. **Done when:**
      delegation compiles and the readiness-fork tests (below) pass against the harness.
- [ ] **Op call sites** in `sessions/manager.py`: replace `_build_session_command` at `1932` with
      `harness.start(ctx)` and at `2483` with `harness.restart(ctx)`, where `ctx` is an op-start
      `RunContext` assembled at the call site carrying the execution targets and scoped secrets. The
      two sites take DIFFERENT anchors: create mirrors the runup ctx at `1873-1880`; restart has no
      runup ctx, so it mirrors the preflight ctx built for `preflight_all` at `2373-2381`
      (`admin_target=admin_target`, `agent_target=None if is_admin else session_target`) and the ctx
      is assembled AFTER the kill (the claude-code sequencing requirement). The op ctx's scoped
      secrets are scoped to the session node's `secret_refs()` union (the harness's contribution
      included, empty for the built-ins), not raw `secret_values`, preserving declare-and-receive;
      the harness-api LLD pins the exact assembly. Apply core template-variable substitution
      (`_substitute_template_vars`) to the harness's RETURNED string (substitution lifts OUT of the
      former `_build_session_command`). **Done when:** create and restart build the same pane string
      as today for every existing template, and the op ctx exposes `agent_target`/`admin_target`.
- [ ] **Retire the interim code.** Delete `RequiredCommandsCheck` (`nodes.py:50-200`) and
      `_build_session_command` (`manager.py:872-897`); the `require_commands` helper in
      `harness/base.py` is now the sole probe copy (seam retired). Land the SESSION-level identity
      guard on the harness's readiness (compare `scope.session/vm/workspace` + agent-or-admin to the
      harness's captured identity; raise on mismatch) and preserve the fifth `scope is None` loud
      branch. Any existing test that imports `RequiredCommandsCheck` directly is ported onto the
      harness (or removed) in this same slice so the phase stays green. **Done when:** neither
      symbol exists, `grep` finds no other reader (code or test), and the guard raises on a
      deliberately mis-wired scope in test.
- [ ] **Docs riding this phase:** `capabilities/README.md` gains the harness as the worked example
      of a capability HELD by a rich consuming node (this claim becomes true here). The ADR draft
      `adr-session-harness.md` is created in-feature now (unnumbered) covering the model
      formulation, the inline reference+blob shape, and pair-inheritance; kept current through P4.

**Tests P3 (the reviewer-expected carry, via deterministic stubbing, no real `claude` binary):**

- Orchestration gate-prompt parity: one boundary burst, nothing resolved or prompted twice;
  graph-derivation of the secret union; zero-resolve / zero-gate refusal when a pre-boundary
  validation fails (the walk-away discipline).
- Readiness-fork coverage: skip (out-of-level / system scan), defer (pending target), probe
  (realized target + every restart), loud error (in-scope target absent), the fifth `scope is None`
  loud branch, and the SESSION-level scope-mismatch guard.
- Parity: every template that loads today produces an identical pane command and identical readiness
  behavior; restart post-kill end state (row survives, old tmux gone, cleanly retryable, error names
  the failed step).

## Phase 4: Template surface (`harness` / `harness_config`), inheritance, TOML, migration, samples

Reshape the template to the pair and delete the flat fields; `harness:` selection becomes usable end
to end. Retires the flat-field and always-shell seams.

- [ ] **`SessionTemplate` reshape** (`template.py:41-70`): remove `command`/`restart_command`/
      `required_commands`; add `harness: str | None` and `harness_config: dict[str, object] | None`
      (`None` = not declared). `referenced_resources()` emits one `harness`-kind reference (usage
      "the session harness") when `harness` is declared, plus any capability-implied refs from
      `validate_config` (none for built-ins). Deleting the flat fields here forces every flat-field
      READER to be handled in this phase (or the tree will not compile), so clearing the consumer
      inventory (per the HLA: session node/orchestrator only; `env/show.py` touches only `env`; the
      DB stores the template NAME) is a green-gate of P4, not deferred to P5. **Done when:** a
      declared `harness` surfaces as a reference, `Referenced by:` lists the template, and no
      flat-field reader remains anywhere in the tree.
- [ ] **`ResolvedSessionTemplate` reshape** (`templates.py:21-31`): becomes
      `(name, description,     env, harness: str, harness_config: dict)` defaulting `("shell", {})`;
      delete the Phase 3 flat-blob adapter (the factory now reads `resolved.harness` /
      `resolved.harness_config`). Rework the merge walk to the pair rule (`_merge_pair`):
      child-silent leaves the pair untouched; a different `harness` starts a fresh blob; the same
      `harness` merges via `merge_config`; post-walk `(None, {})` -> `("shell", {})`. **Done when:**
      the R5 inheritance cases pass (child same/different/silent; `required_commands` union; the
      multi-parent divergence pinned by test) and `validate_config` runs once on the MERGED blob at
      resolve completion.
- [ ] **TOML loader** (`config.py:1044-1086`): accept `harness` (string) + `harness_config` (table);
      hoist the legacy flat fields to `harness="shell"` + equivalent blob; flat + non-`shell`
      `harness` is a `ConfigError`; flat + explicit `harness_config` is a `ConfigError`; update
      `_SESSION_TEMPLATE_KEYS`. **Done when:** a flat TOML template loads to the identical internal
      value the migrator emits, and both conflict cases raise with a clear message.
- [ ] **Manifest decoder** (`decode.py:170-177`): reject the flat fields before delegating (clean
      YAML spec, R2), error pointing at `harness: shell` + `harness_config`; pass `harness` /
      `harness_config` through; invoke `HARNESS_REGISTRY[name].validate_config` on the declared blob
      with `file:line` framing (unknown names skip invocation so the miss policy reports them at
      finalize). Mirror `_decode_git_credential`. `!`-flag this manifest-surface tightening (R2).
      **Done when:** a flat-field YAML manifest is rejected and a typo'd `harness` name errors at
      finalize naming the template.
- [ ] **Migration** (`planning.py:450-549`): add a `session-template` branch mirroring
      `git-credential`: pop the flat fields, emit `harness: shell` + a `harness_config` blob when
      present, pass declared `harness`/`harness_config` through; validate the rebuilt blob
      pre-write. **Done when:** migrating a flat TOML template emits the clean YAML shape and the
      per-run registry-equivalence verification passes (hoist and emission land on the identical
      value).
- [ ] **Samples + sample-config.** Rewrite `manifests/samples/session-template.yaml` leading with a
      commented `shell` + `harness_config` document, followed by the `claude-code` one-line document
      (runtime neutrality). Update `sample-config.toml`'s session-template example to the flat shell
      form (documented default TOML shape) pointing at `agw resource sample session-template`.
      **Done when:** the samples-load-clean test passes and `agw resource sample session-template`
      emits the new shape.
- [ ] **Docs riding this phase (the model change becomes true here):** top-level `README.md`
      "Sessions" narrative rewritten to "a session is a specification to run a specific harness as
      an agent in a workspace on a VM" (harness as first-class model concept); `cli/README.md`
      session-template schema (YAML `harness`/`harness_config`, nested TOML keys, flat-field rules,
      legacy-child harness-switch note per R6); `docs/guides/resources.md` harness capability story
      with worked session-template examples in the new shape. No permanent doc cites a `docs/sdd/`
      path.
- [ ] **Always-consider sweep:** confirm the CLI Typer tree is unchanged (no new commands/flags; the
      `harness` kind is data-driven through `resource kinds` / `--kind`), so completions need no
      regeneration; verify `--kind harness` completes via the existing dynamic path. Re-run the docs
      and sample-config rules.

**Tests P4:** hoist parity; flat+non-shell error; flat+blob error; decoder flat rejection; pair
inheritance (all R5 cases + multi-parent divergence); migration registry-equivalence; samples load
clean; `describe session-template/<name>` renders `harness`/`harness_config` and lists the
reference.

- **`claude-code` end-to-end carry (this is the first phase `claude-code` is reachable through the
  real orchestrator; do not leave it pinned only in P2 isolation).** Drive a `harness: claude-code`
  template through session create AND restart via the actual op call sites, asserting: (a)
  op-dispatch produces the claude launch/resume pane string through create and restart (not just the
  stubbed unit); (b) restart-post-kill detection and end state with a `claude-code` target (R7: row
  survives, old tmux gone, cleanly retryable, error names the failed step), which the P3 parity
  carry only exercised for `shell`; (c) the visible-decision requirement (R4, never silent) through
  the real launch. All via deterministic stubbing, no real `claude` binary.
- **Substitution-safety carry:** `claude-code`'s returned snippet is the first harness output that
  can carry literal braces, so assert the relocated template-variable substitution (P3) does not
  mangle a generated snippet, per the harness-api LLD's escaping decision.

## Phase 5: Closeout

- [ ] **Promote the ADR.** Move `adr-session-harness.md` into `docs/adrs/` as the next number
      (`0020-...` at this writing; confirm the max at promotion), referencing ADR 0016 (capability
      collapse) and ADR 0019 (orchestration layer) for the node/readiness model. **Done when:** the
      numbered ADR exists in `docs/adrs/` and the in-feature draft is removed.
- [ ] **Final sweeps.** RE-VERIFY the consumer inventory is empty of flat-field readers across code,
      tests, docs, and samples (the readers are actually cleared in P4, where deleting the fields
      forces it; this is a re-check, not the primary sweep); promote any SDD `.cspell.json` word
      that now lives in permanent code to the root dictionary; run `./scripts/lint-files.sh --fix`
      across touched files; full gate green. **Done when:** CI-equivalent gates pass and no
      `docs/sdd/` reference exists in permanent code or docs.
- [ ] **Lock.** Create `locked.md` summarizing the final state (per the SDD lifecycle). **Done
      when:** `locked.md` exists and the effort is closed.

## Decisions (resolved by the lead, 2026-07-19)

These are internal phasing/mechanics calls, resolved by the lead per the development process
(surfaced to the operator for visibility; not blocking). The operator can override any of them.

1. **Swap-before-surface phasing (P3 before P4): RATIFIED.** It teaches the better interim lesson
   (the harness mechanism is real and swapped in, only the template selector is pending) and
   isolates orchestrator risk from the dataclass reshape. The interim `main` state (harness always
   `shell`, built from flat fields) is complete and honest on its own.
2. **Probe helper copy-then-delete: RATIFIED.** Keeps Phase 1 purely additive (zero `sessions/`
   edits); the duplication is short-lived and retired in Phase 3.
3. **`ResolvedSessionTemplate.description` default: DEFERRED to the harness-api LLD.** Cosmetic;
   decide whether to keep "Login shell" or source it from the resolved harness when the LLD is
   written.

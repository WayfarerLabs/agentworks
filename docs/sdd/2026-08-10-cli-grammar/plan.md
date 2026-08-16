# CLI Grammar Correction: Implementation Plan

- Status: Approved; implementation in progress
- Date: 2026-08-16
- Requirements: `frd.md`
- Architecture: `hla.md`
- Detailed designs: `graph-query-lld.md`, `cli-cutover-lld.md`
- Code basis: `origin/main` at `24bc7a28`
- Delivery vehicle: draft PR #491, continuing through implementation on this branch by explicit
  operator direction

## Delivery posture

This remains one PR because the operator explicitly directed the reviewed artifact vehicle to become
the implementation vehicle. That direction is an exception to the active-saga default of merging
reviewed artifacts before implementation. The public draft artifact checkpoint supplies the saga's
coordination surface without an early artifact merge, and the PR retains no merge intent until the
operator says otherwise. Commits provide the reviewable layering:

1. additive frozen-graph, database, and resource-access primitives with their owning tests and
   assertion migrations;
2. an unregistered graph-query service, records, renderers, and the relationship/live-fact assertion
   migrations they now own;
3. one collateral-complete CLI cutover containing registrations, removals, machine IDs, completions,
   active documentation, hints, residual presentation-test deletion, and cutover tests; and
4. verification evidence, truthful plan updates, and SDD closeout.

The branch is already rebased over the survey-approved guide deletion merged in PR #556. That
prerequisite is satisfied at this plan's basis. Every later implementation rebase must preserve the
post-deletion surface and must not restore removed guide views, runtime resource topics, schema
adapters, or generic guide fact projections.

At implementation-kickoff basis `24bc7a28`, the saga records this grammar rewrite as the active
final spine item before reassessment. Simplification items C1/C5 are now complete in
`docs/sdd/2026-08-12-simplification-pass/plan.md`; they deleted inert descriptor generality without
introducing capability facets. This branch's overlap boundary remains unchanged: it does not modify
capability descriptors or introduce facet models. The future harness-integration work owns the first
real multi-facet descriptor and explain rendering. The capability-shape note newly present at this
basis is explicitly input to that future work and does not expand this implementation.

The compatibility posture is owned by the FRD and migration strategy; no phase weakens it.

## Final artifact gate

Implementation begins only after this final artifact set passes the operator-directed checkpoint.

- [x] Obtain clean reviews of the exact artifact head from the saga lead and Muntz complexity lane.
      The integration tester did not report during the sustained wait; the operator subsequently
      approved the design and explicitly directed implementation to begin.
- [x] Classify every material finding against the approved FRD and operator rulings.
- [x] If findings are clear and require no requirement change, apply the operator-pre-authorized
      final cleanup round to the SDD artifacts.
- [x] Re-run the saga-lead and Muntz lanes on the cleaned exact head and confirm no material finding
      remains; record the operator's implementation direction as disposition of the absent tester
      response rather than inventing a review result.
- [x] Stop and raise any significant issue that requires product direction, scope expansion, or a
      material redesign.
- [x] In this final artifact checkpoint handoff, deliver the dying-command inventory to the parent
      saga and simplification effort. At implementation kickoff, record the rebased `main` SHA and
      the completed C1/C5 status from `docs/sdd/2026-08-12-simplification-pass/plan.md`; preserve
      the explicit overlap boundary that this effort does not change capability descriptors or
      introduce facet models.
- [x] Record the clean artifact disposition in the PR handoff before implementation starts.

## Dying-command and ownership inventory

This is the assertion-sweep coordination list requested by the saga. The simplification effort may
skip these presentation-owned surfaces; this effort owns their disposition.

| Dying surface                                         | Direct owners to remove                                                                                                                                                                                                                                                        | Facts or coverage that must survive elsewhere                                                                                                              |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agw resource describe KIND/NAME`                     | registration and function in `cli/commands/resource.py`; `ResourceDescription`, `describe_resource`, `resource_description_data`, `render_resource_description`, and describe-only helpers in `resources/inspect.py`; `RESOURCE_DESCRIBE`; completion mapping; help/docs/hints | relationships move to graph; identity/origin/readiness/enablement remain with list, doctor, edit, and kind-specific commands                               |
| `cli/tests/test_resource_describe.py`                 | CLI card, human renderer, old JSON payload, and describe-only error tests                                                                                                                                                                                                      | graph service/CLI tests absorb inbound and live relationships; resource-access tests absorb identity errors; list/doctor tests retain non-relational facts |
| describe-backed assertions in plugin and domain tests | calls to `describe_resource` in plugin surface/parity tests, VM support tests, apt provenance tests, and resource-instance tests                                                                                                                                               | assert the owning registry row, graph fact, list projection, readiness/enablement verdict, or extracted resource resolver directly                         |
| `resource.describe` JSON v1                           | enum member, fixtures, command-reference schema, machine-output CLI cases                                                                                                                                                                                                      | new closed `graph.show` node-and-edge schema; `secret.describe` remains byte-for-contract unchanged                                                        |
| `agw resource describe-kind TARGET`                   | command/completion identity and every active hint, example, guide contribution, README, and command reference                                                                                                                                                                  | same config-free `reference_for` plus `render_reference` behavior under `resource explain`                                                                 |
| `agw resource schema --write`                         | boolean flag identity, errors, hints, docs, and completion/help expectations                                                                                                                                                                                                   | identical whole-set fixed-destination behavior under `--install`; sample `--write PATH` remains unchanged                                                  |

Historical SDDs and historical portions of ADRs are not mechanically rewritten. The active-reference
sweep classifies each remaining old spelling: only the 0.14 upgrade map and clearly historical
records may retain one.

## Phase 1: additive storage and access primitives

### 1.1 Preserve authoritative incoming edges

- [x] Extend the frozen graph node in `resources/graph.py` with full incoming `ResourceReference`
      tuples seated directly from the target-keyed edge map.
- [x] Add `DependencyGraph.incoming_edges_of(kind, name)` without changing `dependents_of`, its
      reduced `ReferenceEntry` projection, or any existing closure.
- [x] Keep `uses` and `inherits` in a graph-query-specific explicit traversal allowlist.
- [x] Add a relationship-coverage test that requires the explicit graph-query traversal set to equal
      the current relationship enum; a future enum member must force a new design decision rather
      than silently joining or being excluded from traversal.
- [x] Prove full inbound edges preserve source, relationship, usage, and optional `declared_by`,
      including parallel facts and inheritance.

Definition of done: existing graph/readiness/secret tests pass unchanged, the frozen graph remains
immutable, no publisher is widened, and graph query can read identical authoritative facts in both
directions.

### 1.2 Add a coherent read-only transaction boundary

- [x] Record the construction mode on `Database` and add the context-managed read transaction
      specified by `graph-query-lld.md`.
- [x] Reject misuse on writable instances and unsafe nesting rather than silently sharing the
      write-oriented transaction helper.
- [x] Begin one explicit SQLite read transaction, retain the first-read snapshot through all graph
      instance hooks, and end it without committing a logical write.
- [x] Test concurrent-writer visibility, exception cleanup, close-after-transaction, and read-only
      write rejection without weakening existing stale/newer/malformed/busy behavior.

Definition of done: one graph request can obtain a coherent persisted-state snapshot through the
public `Database` API, and every pre-existing database consumer retains its behavior.

### 1.3 Extract fact-minimal resource identity access

- [x] Add the shared first-slash parser and validated resource resolver described by
      `cli-cutover-lld.md` to `resources/access.py`.
- [x] Preserve legacy names containing double hyphens, dots, and colons; reject a missing slash or
      either empty side before registry work.
- [x] Return only identity, row, and origin facts needed by graph and edit; do not recreate the old
      resource card.
- [x] Repoint `edit_location` to the new resolver while retaining its tolerant invalid-manifest
      fallback, typed unknown-kind/name errors, and origin-specific edit guidance.
- [x] Add focused parser/resolver/edit tests, including `session/legacy--name`.
- [x] Move the former card assertions for parser failures, row identity/origin, whether a row can be
      edited, and declaration location into these additive access/edit tests before the card service
      is removed.

Definition of done: `resource edit` no longer imports or calls the describe service, and the old
presentation service can be deleted without losing edit lookup behavior.

## Phase 2: graph query service and projections

### 2.1 Implement closed facts and deterministic traversal

- [x] Add the frozen identities, query, node, edge, and result records specified by
      `graph-query-lld.md`.
- [x] Implement breadth-first shortest-distance traversal for dependencies, dependents, and
      per-expansion mixed `both`, with positive finite depths and unbounded `all`.
- [x] Expand resource nodes once, keep live nodes terminal, record edges to already-known nodes, and
      retain distinct parallel facts while collapsing exact duplicates.
- [x] After reachability settles, collect the allowlisted induced declared subgraph among reached
      resource nodes without discovering nodes or touching live state.
- [x] Apply the specified total node, neighbor, and edge ordering, including explicit null sort
      keys.
- [x] Cover defaults, every direction/depth mode, direction-changing paths, cycles, diamonds,
      parallel edges, boundary cross edges, no-neighbor results, and names with legacy punctuation.
- [x] Move former card assertions for inbound declaration relationships, usage, provenance, and
      registry graph facts into the additive graph-query tests before the card service is removed.

Definition of done: a pure service test can fully assert the returned safe fact graph without CLI,
renderer, config, database, resource-object reflection, or insertion-order dependence.

### 2.2 Implement demand-driven live projection

- [x] Add the four-state, single-use live source and exact source-demand predicate from the LLD.
- [x] Classify only a definite missing database as an empty source; retain permission/path/open
      failures as typed whole-query errors.
- [x] Open one `Database(read_only=True)` and one read transaction per demanded request, reuse both
      for every eligible kind hook, and close on all exits.
- [x] Construct that lazy source from the canonical `agentworks.db.DB_PATH`; do not add a config
      field or a second database-path resolution rule.
- [x] Copy each projected instance immediately into a terminal live node and intrinsic
      `live instance -> resource` edge; count it as one hop regardless of summarized config depth.
- [x] Treat live-edge `relationship=uses` as the fixed convention of `live-usage`, never as an
      inferred capability facet or declaration verb.
- [x] Keep live edges frontier-collected only. Do not synthesize them during the induced declared
      pass, and prove a boundary node does not demand or imply uncollected live facts.
- [x] Test absent, stale, newer, malformed, busy, unreadable, hook-failure, and close-on-error
      paths; assert no partial human or JSON output.
- [x] Add representative broad-registry and repeated-hook-kind scale coverage and record observed
      query counts.
- [x] Move former card assertions for live instance usage into the additive live-projection tests
      before the card service is removed.

Definition of done: declaration-only and depth-bound queries never inspect the database, demanded
queries see one coherent snapshot, and the platform-to-site-to-live-VM case proves depth and source
demand end to end.

### 2.3 Implement two projections over one result

- [x] Add the flat, distance-grouped human renderer from the graph LLD; arrows always retain
      intrinsic orientation and indentation never encodes discovery ancestry.
- [x] Add the explicit safe-scalar `graph.show` JSON projector with the fixed field set and
      `depth_limit: null` for `all`.
- [x] Encode the complete JSON envelope before writing stdout and retain terminal-control escaping.
- [x] Test service grouping and JSON records structurally. Feed the human renderer unique identity,
      relationship, usage, and provenance facts and prove complete, ordered, once-only projection
      without pinning authored labels, whitespace, or explanatory prose.
- [x] Prove neither renderer reaches registry, database, config, handlers, resource rows, origins,
      secrets, or arbitrary attributes.

Definition of done: both outputs are deterministic projections of the same completed `GraphResult`,
machine output is a closed v1 contract, and rendering cannot change graph membership.

## Phase 3: collateral-complete CLI cutover

The user-visible grammar and collateral boundary in this phase lands in one commit and one handoff.
The 39 fact assertions across 14 files that currently use `describe_resource` as a shortcut migrate
with their additive owners in phases 1 and 2. A partial registration or stale-doc midpoint may exist
only in private uncommitted work, never at a pushed review boundary.

### 3.1 Register the new grammar

- [ ] Add top-level `graph` registration and `graph show FOCUS` with closed direction/depth/output
      parsing and defaults.
- [ ] Load config and a finalized registry with host-readiness probing disabled; never use writable
      `get_db`, resolve secrets, prompt, activate resources, or call providers/remotes.
- [ ] Rename `resource describe-kind` to `resource explain` without changing its resolver, renderer,
      target forms, errors, ordering, or config-free behavior.
- [ ] Replace schema `--write` with flag-only `--install`, retaining exact whole-set destination,
      validation, overwrite, and reporting behavior.
- [ ] Update resource-group help to the resulting inventory, explanation, authoring, and editing
      responsibilities.

### 3.2 Remove the generic resource card atomically

- [ ] Delete `resource describe`, its DTO/service/renderer/projector, describe-only helpers,
      completion entry, CLI tests, and `resource.describe` command ID.
- [ ] Verify every fact assertion named in the dying inventory is already green under its phase-1 or
      phase-2 owner, then delete only the residual presentation-specific tests with the card.
- [ ] Leave `secret describe`, its reduced inbound relationship view, live grouping, human output,
      and `secret.describe` JSON records unchanged.
- [ ] Add `GRAPH_SHOW = "graph.show"` to the closed machine-command enum and no compatibility ID.
- [ ] Assert old spellings fail as unknown/invalid use rather than warning or dispatching.

### 3.3 Update completions and permanent teaching

- [ ] Map graph focus to the config-backed `resource_refs` source, explain to config-free
      `resource_kinds`, and direction/depth to their static candidates.
- [ ] Make `resource list --names-only` registry-only so graph/edit completion remains available
      when the database is absent, stale, newer, malformed, busy, or unreadable; keep config and
      finalized-registry failures silent.
- [ ] Remove `sites`, `ws_templates`, `git_credentials`, `session_templates`, `vm_templates`,
      `agent_templates`, `admin_templates`, and `resource_refs` from the database-backed completion
      inventory. Update its four inventory/probe tests so all eight paths share the registry-only
      contract.
- [ ] Prove the registry-only names path never calls `get_db` or `open_completion_database`, and
      that a healthy database produces an identical candidate set and order. Ordinary human and JSON
      resource-list paths remain database-backed.
- [ ] Remove old dynamic identities, regenerate/show bash, zsh, and PowerShell output, and test
      missing/broken-config behavior for each relevant path.
- [ ] Update active help, errors, hints, command reference, `cli/README.md`, sample-config comments,
      surviving guide contributions, domain/capability/plugin READMEs, resource and platform guides,
      and the 0.14 upgrade guide.
- [ ] Preserve historical records deliberately and perform a reviewed one-time active-reference
      search for `resource describe`, `describe-kind`, `resource.describe`, and schema `--write`; do
      not commit a test that polices repository-authored prose.
- [ ] Do not recreate any guide topic or adapter removed by PR #556.

Definition of done: every observable owner presents one coherent grammar at the commit, the three
shells agree with the Typer tree, active docs match behavior, and only the upgrade map or clear
history contains retired spellings.

## Phase 4: verification, live acceptance, and closeout

### 4.1 Automated gates

- [ ] Run focused resource graph/query, database, CLI, machine-output, completion, schema, edit,
      secret-describe, and plugin tests; retain the active-reference search as reviewed cutover
      evidence rather than a prose-policing test.
- [ ] Run `uv run ruff check .`, `uv run mypy agentworks tests`, and
      `uv run pytest tests/ -m 'not integration'` from `cli/`.
- [ ] Run `./scripts/lint-files.sh`, `git diff --check`, `./scripts/check-locked-sdds.sh`, and
      `./scripts/rulesync-upgen.sh --check`.
- [ ] Confirm every commit at a handoff is green and the branch remains based on current `main`.

### 4.2 Independent review and live CLI testing

- [ ] Run private Agentworks reviewer cycles after the graph-service batch and after the atomic
      cutover; resolve every material finding and obtain clean re-review.
- [ ] Run the required fresh-eyes code review for the code-heavy implementation and triage it
      independently of the project-values review.
- [ ] Invoke the Agentworks integration tester under the repository's integration-testing and test
      environment protocols with a bounded, residue-free charter.
- [ ] Exercise real human and JSON graph output for one-hop defaults, two-hop platform/site/live-VM
      dependents, dependencies-only, mixed-direction depth, and `all` on a bounded fixture registry.
- [ ] Prove explain with absent/invalid config, schema installation parity, old-spelling failure,
      secret-describe parity, missing-database success, and demanded bad-database failure.
- [ ] Require clean saga-lead, Muntz, tester, and CI dispositions before making merge intent.

### 4.3 Truthful SDD closeout

- [ ] Update plan boxes only as their work is actually complete; never pre-check implementation or
      merge claims.
- [ ] Reconcile FRD, HLA, LLDs, migration strategy, and implementation where contact with code
      required a design refinement.
- [ ] Promote every load-bearing operational contract into code, permanent docs, or tests so no
      shipped behavior depends on reading `docs/sdd/`.
- [ ] Add `locked.md` only when implementation, permanent collateral, review, and live acceptance
      are complete; record the final state and any consciously retained risk.
- [ ] Move the PR from draft to ready only when it is genuinely intended to merge, all changes are
      pushed, and the final signed handoff is posted.

Definition of done: the full scoped suite and repository gates are green on current `main`; the live
tester and required reviewers have clean dispositions on the exact head; every plan box and artifact
matches observed implementation; permanent collateral owns every shipped contract; and the PR has no
merge intent until the operator explicitly changes it.

## Requirement traceability

| Requirements | Primary plan coverage                                                            |
| ------------ | -------------------------------------------------------------------------------- |
| FR1-FR5      | 3.1 explain rename, 3.3 completion/docs, 4.2 config-free acceptance              |
| FR6-FR10     | 1.1, 2.1-2.3, 3.1, 3.2 machine ID                                                |
| FR11-FR14    | 1.2, 2.2-2.3, 3.1 no-probe registry, 4.2 live acceptance                         |
| FR15-FR18    | dying inventory, 1.3, 3.2, secret parity in 4.2                                  |
| FR19-FR22    | 3.1 schema install and 3.3 docs/completions                                      |
| FR23-FR26    | 3.3 collateral-complete cutover and 4.1 active sweep                             |
| QR1-QR5      | service tests in phases 1-2, thin CLI in 3.1, full gates/reviews/docs in phase 4 |

## Stop conditions

Stop and return to the operator instead of widening the effort if implementation requires a new
relationship taxonomy, a graph query without a focal resource, a changed explain target grammar, a
compatibility runway, database repair/migration, live-node expansion, provider access, secret
resolution, a machine-output version bump, restoration of deleted guide machinery, or a PR split.

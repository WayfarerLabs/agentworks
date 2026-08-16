# Findings Inventory

Consolidated evidence from the 2026-08-12 seven-lane review of the 2026-08-06..12 merge window (68
PRs, read at merged main `fe83aaf7`). Each finding carries an ID that the plan cites. File
references are to HEAD at `fe83aaf7`; line numbers drift as the pass lands, so treat them as
anchors, not coordinates.

Window arithmetic: cli code +36,029/-12,431; cli tests +50,661/-8,419 (test volume now 119,584 lines
under `cli/tests` against 83,151 lines under `cli/agentworks`, both exact recounts at the `fe83aaf7`
basis); SDD artifacts +21,913; process docs +7,699. Always-on rules context grew 7.8 KB to 30.6 KB.

## Rule-delivery facts (probed 2026-08-12, three controlled subagent probes)

**Resolved 2026-08-14; the bullets below describe the pre-wave-0 world and are kept as the record of
what was wrong, not as current fact.** PR #515 removed the path filter, and issue 511 closed on two
observations: a fresh session and a worktree-isolated subagent each carried all twelve rules, in
full text, before any tool use. The open sub-question about harness-created worktrees is answered by
the second of those; the delivery channel is now session-start `claudeMd` rather than path-triggered
injection, so worktree location no longer decides anything.

Tracked as [issue 511](https://github.com/WayfarerLabs/agentworks/issues/511); wave 0 resolves it
(FRD R1.0). Exact source inventory as of 2026-08-13: 18 files in `.rulesync/rules/`; thirteen
declare `globs: ["**/*"]` including `root.md` (which projects to `CLAUDE.md` and always loads);
generated, five load unconditionally and thirteen are path-conditioned, twelve broad plus the
deliberately narrow `cli-conventions.md`.

- Rules with `paths:` frontmatter are injected lazily, after the first tool call that touches a file
  under the primary project directory.
- A subagent that uses no tools, or that works only in a checkout outside the project root (for
  example a scratchpad git worktree), never receives them. It gets only CLAUDE.md and the four
  frontmatter-less `always-consider-*` rules.
- Consequence: worktree-isolated implementers, the exact agents the process mandates isolation for,
  run without `development-principles`, `code-style`, `no-prose-policing-tests`, and
  `github-input-trust` unless the invoking prompt or persona carries the content.
- Open sub-question: whether harness-created worktrees (Agent tool `isolation: "worktree"`) land
  inside or outside the project path. Verify before relying on either answer.
- Scope caveat (integration-tester, 2026-08-13): all three probes exercised Claude delivery only.
  Codex and Copilot delivery of the same surfaces is unprobed; the personas are generated for more
  than one harness, so self-heal instructions reference the canonical `.rulesync/rules/` sources
  rather than any harness-specific path, and the phase 0 probe item covers Codex.

## Guide and discovery (#428, #462)

- **G1** `guide/contract.py`: ~764 lines of adversarial validation of repo-authored content (18 byte
  caps, hand-rolled markdown code-span scanner `_code_ranges`, NFKC+unescape delimiter detection).
  No external contributor exists: `plugins/__init__.py:85` hardcodes seven in-repo modules, no
  plugin sets `guide_topics`, no entry-point discovery anywhere. Authored guide markdown totals 31
  KB against a 256 KB cap; zero expression markers exist in any authored file.
- **G2** Typed-to-dict round trip: `parse_topic_contribution` accepts an already-typed frozen
  `TopicContribution`, converts it back to a dict (`_record_value`), and re-parses it with injected
  `object()` sentinels, defending against our own type annotations. Runs on every topic render via
  `view.py:202`.
- **G3** The one genuinely operator-controlled string (manifest resource description) reaches
  `render_index` through contributions `service.py:199-203` builds directly, guarded only by ad-hoc
  escaping. The validation boundary is not where the untrusted text enters. (Corrected during wave
  1: late rather than absent. `build_guide_view` re-parsed the contribution at `view.py:202`, so the
  byte cap and delimiter screen did run on the ordinary path, one step downstream; only the degraded
  `system_error` path, which never builds a view, had no check at all.) **Resolved** by PR #548,
  which moved the check to where the description enters.
- **G4** Eleven `GuideBlock` variants over four actual payload shapes; parallel dispatch in six
  places. All variants have consumers; the cost is nominal-type ceremony, not dead code.
- **G5** JSON adapters and human renderers are parallel field-by-field walks of the same trees (19
  adapter functions, ~394 LOC mirrored; e.g. `vms/manager/inspect.py:253-337` vs `:587-699`). One
  new field requires three edits. Design-pass scale, not a quick fix.
- **G6** `machine_output.py`: `schema_version` is write-only (nothing branches on it anywhere);
  double projection of already-projected values; `AssertionError` guards re-checking frozen
  dataclass field types; identity-map comprehensions; a hand-rolled partial-write retry loop.
  (Corrected during wave 1: the loop is load-bearing, not defensive surface, and is not a deletion
  target. The `BufferedWriter` claim holds only while stdout is buffered; under `python -u` or
  `PYTHONUNBUFFERED` the buffer is a raw `FileIO`, whose `write` returns a short count instead of
  raising. Deleted and restored in PR #548, which added the short-write test the invariant never
  had.)
- **G7** Traversal permissions enforced twice: statically by the block-anchor table
  (`contract.py:985-996`) and dynamically by `view.py:148-151`, whose only caller catches and
  discards the dynamic denial.
- **G8** Dead surface: `guided_actions`/`replayable_actions` (identical bodies, zero production
  callers), `GuideView.kind()`, `ConsentBoundary.NONE`, `JsonScalar`, `VMIssueCode` (one member),
  `HarnessSignature.name` never read.
- **G9** Colocated contributions are not colocatable: `secrets/guide_contributions.py` hand-rolls a
  duplicate `_markdown` because the shared helper hardcodes `files("agentworks.guide")`, and
  `view.py:76-80` centrally hardcodes `"concept-secrets"` resolver roots.
- **G10** Agent-register prose on human surfaces (topic summaries render in both modes), and the
  JSON envelope contract hand-restated in eight action-prose strings.
- **G11** Coverage gaps under the ceremony: `test_operational_json_persisted_enums.py:164-195`
  monkeypatches modules the code never imports (assertions pass vacuously), and no test compares the
  frozen output vocabularies against `db/models.py` enums, so a new member silently renders
  `"unknown"`.
- **G12** Prose-policing tests: `test_migration_topic.py:408-460` (47-entry required-phrase list
  plus two blacklists, ~150 substring asserts), `test_authored_coverage.py:25-47` and `:106-118`
  (verbatim pins; the manifesto pin traces to #470), scattered pins in `test_render_service.py`,
  `test_schema_adapter.py`, `test_assessment.py`, `test_json_action_contract.py`,
  `test_operational_json_boundaries.py`.

## Secrets (#453)

- **S1** The `phase7` corpus: 3,186 test lines (1,552 support, 1,634 test), 621 collected cases, of
  which roughly 620 fail only on differently-spelled-but-correct code. (Recounted exactly during
  implementation, 2026-08-14; the original 3,407 and 639 were estimates.) Includes a 671-line
  hand-rolled name resolver and a 575-line type-inference engine as test-support modules, plus tests
  of those helpers. Discovery keys on a parameter literally named `interaction`, so renaming it
  silently removes a function from the guarded set. Behind it, `validate_interaction_policy` (a
  runtime check that a first-party enum is an enum) at 152 call sites, one of which validates the
  value the same expression just constructed (`cli/commands/secret.py:134-136`).
  `test_phase7_retired_enforcement.py:229-268` is additionally a wording blacklist over README,
  docs, and production source. Nine permanent test files are named after transient plan phase 7.
- **S2** Backend classes treated as hostile protocol peers: bare `except Exception` plus type-checks
  around every return from the three in-repo backends
  (`resolve.py:87-94, 351-360, 556-573, 612-624`), a `_BATCH_TOKEN` construction sentinel defeated
  550 lines later, MRO walks re-checking `@final` methods, and reflective signature conformance
  (`capabilities/secret_backend/conformance.py`, 176 lines + 839 test lines) that runs against
  exactly one class and rejects Liskov-legal widening mypy accepts. Built-ins are not
  conformance-checked at all. Correction (2026-08-15, from PR #546): that last sentence was wrong
  when written. `tests/capabilities/test_capability_descriptors.py:438`
  (`test_every_registered_builtin_impl_conforms`) runs the whole conformance chain over every seated
  implementation of every kind, and existed unchanged at this pass's merge base. The real
  distinction is where and when, not whether: our own tree is checked by a test that fails the
  build, a plugin's classes at registration. PR #546's argument for narrowing the registration
  checks rests on that test existing, so the error mattered more than an ordinary stale line.
- **S3** Client protocol wider than its implementers: `prepare` implemented by 0 of 3,
  `create_client(source_name=...)` read by 0 of 3, context manager needed by 1 of 3.
  `SecretClientFailure.remediation` is never read, and the constructor requires callers to pass a
  value it then verifies against the one it derives.
- **S4** `OUTCOME_RULES` stores derived fields and validates self-agreement: `category` and
  `remediation` are total functions of `detail`, stored redundantly, checked in `__post_init__`,
  with `_outcome` existing to copy the derivation back in.
- **S5** `[secret_config].backends` names sources, not backends. Four files carry reconciling prose.
  Free to fix only while 0.14 is unreleased. Correction (2026-08-14, integration review): the scope
  is the configuration key only. `SourceMapping`'s `source` and `backend` JSON fields looked like
  the same fact spelled twice but are two facts (the configured source instance and its implementing
  backend capability), divergent whenever a source is backed by a differently named backend; both
  stay, per the locked secret-sources design.
- **S6** Two post-boundary resolve paths: `resolve_late_repair` (the documented "ONLY sanctioned"
  path, one caller) vs `resolve_for_command` at seven production call sites each carrying an
  exception-claiming comment; `power.py:91` re-implements the sanctioned method the long way. Stale
  docstrings: `orchestration.py:39` describes a migration that already landed; `resolver.py:103`
  cites a symbol that does not exist.
- **S7** Adversarial validation of data our own parsers produce: triple defense on plugin directory
  names, Unicode-category scrubbing of already-validated resource names, `require_exact_json_value`
  rejecting "Python lookalikes" a YAML parser cannot emit. Contrast `line_safety.py` (43 lines),
  which guards a real boundary correctly. Correction (2026-08-15, from PR #546's execution): the
  lookalike claim is disproved. `manifests/loader.py`'s `_StrictLoader` is a `SafeLoader` subclass,
  and its tag set is wider than JSON: a plain `2020-01-02` builds a `datetime.date`, `!!binary`
  builds `bytes`, `!!set` builds a `set`, and a dated mapping key builds a non-string key. All are
  reachable from a secret's `backend_mappings` in operator-authored YAML, and a manifest carrying
  `!!binary` there is rejected by `require_exact_json_value` and by nothing else. The guard names a
  live boundary and correctly stays. The scrubbing claim is disproved with it: those names are not
  in fact already validated, because `validate_name` accepts a trailing newline (`$` matches before
  it under `re.match`, filed as #542), so the scrub guards against a validator that lies rather than
  re-checking a certified value. PR #546 restored it with two tests written to fail once #542 lands,
  so the guard retires itself.
- **S8** `direct_backend_source_error` (`sources.py:268-325`): a transitional bridge outside the
  `retired_shapes.py` quarantine, containing the one plugin-specific branch in an otherwise generic
  module, parsing the retired mapping shape its own upgrade guide says is not parsed.
- **S9** Built-in source names hardcoded in generic code: `resolve.py:547,552` dispatches the
  interaction broker on `name == "prompt"` while a declared `interactive` flag exists;
  `vm rekey --ignore-env` pops `os.environ` keyed on the literal `"env-var"` mapping. **This is not
  only tidiness** (upgraded 2026-08-14, from the cold review of PR #523, which reproduced the
  effect): that hardcoded name is currently acting as an accidental safety net. Because every
  consumer compares `interaction` by identity and `InteractionPolicy` is a `StrEnum`, a value that
  is equal but not identical takes the not-refuse branch and resolves through an interactive source
  in a run that meant to refuse. The `prompt` dispatch raises on that, so `prompt` fails loud while
  `onepassword` and every future interactive backend fail silent and permissive. The reachable path
  is a caller-supplied argument at the published service surface: no first-party site _constructs_ a
  non-enum policy, but the manifest services take `interaction` from callers outside our type
  checking, and a probe drove `verify_secrets(..., interaction="refuse")` into real OnePassword
  backend execution. PR #523 closes that half on a structural rule: `ResolutionPolicy.__post_init__`
  checks, so no policy exists that was never checked, plus a check on arrival at the published
  service functions and at the three entry points that do destructive or remote work before reaching
  a construction. The deferred half stays with issue 529 against the external-plugin loader effort:
  no backend's safety should depend on its name, and the `prompt` special case is what currently
  decides whether the identity comparison fails loud.
- **S10** LLD prose in permanent docstrings: 45-line docstring over a 10-line body
  (`base.py:187-218`), 57-line module docstring litigating design history, a 16-line Typer help
  docstring describing internals.

## Declarative schema arc (#414, #444, #446, #455)

- **C1** Descriptor fields that do not vary: `RegistryPolicy` single-member enum with two dead
  consumer branches; `kind_strategy` with zero production readers (self-tested against its own
  duplicate); inert `contract_version`; `manifest_section` guarded for a `None` it never takes;
  `discriminator`/`input_domain` identical across all four kinds.
- **C2** Eight of eighteen classified `FieldShape` shapes have zero shipped instances; two have one.
  `_shape.py` is 1,383 lines (ceiling: 1,000), including the two-level `X`/`item_X` mirror whose
  unshipped half is speculative by its own docstring, while `reference_marker_error` already refuses
  unknown shapes loudly by design.
- **C3** `TokenAcquisition`: a one-arm tagged union giving operators five spellings of one fact,
  dragging in the entire `UnionScalarShorthand` mechanism (sole production instantiation), a
  provider contract v2 bump, and a `TokenSourcedConfig` v1 tombstone with no release-scope marker.
- **C4** `StructuralUnion`: ~1,000 lines of machinery (module, satisfiability checking, error
  reconstruction, extract/fill branches, 365-line test file) for one field (`env/entry.py:60`),
  replacing a two-field model the capabilities README tier 3 explicitly sanctions. Its
  `canonicalize_null_companions` compat flag re-accepts and re-advertises the spelling the union
  just broke.
- **C5** `config_for()`: an override hook with zero overrides, a 30-line docstring about a parameter
  it does not have, and two unreachable `getattr` fallbacks behind registration conformance that
  already guarantees the attributes.
- **C6** Prose density: `schema/` 45% comment/docstring, `manifests/` 47%, with design-journey
  narration in permanent docstrings ("an earlier revision threaded..."). Files over the size
  ceiling: `_shape.py` 1,383, `errors.py` 1,033 (the latter mostly inherent; split, do not rewrite).
- **C7** Four release-scoped compat layers (~925 lines + a 1,029-line upgrade guide) with no
  recorded expiry anywhere; two more compat objects (`TokenSourcedConfig`,
  `canonicalize_null_companions`) not inventoried in `retired_shapes.py`, so a sweep would miss
  them. `HOST_PROBING_CAPABILITY_KINDS` survives as a hand enumeration with a written IOU.
- **C8** `ResolvedSessionTemplate` still carries the retired sibling pair (`harness_integration` +
  `harness_integration_config`) as its internal representation, contradicting the shipped amendment
  in ADR 0016.
- **C9** Permanent docs contradicting HEAD: ADR 0020 names a removed lifecycle stage and a retired
  config shape; ADR 0018 describes deleted spellings; `vm_platform/README.md:385` states a rule four
  implementations no longer follow; assorted docstrings name retired spellings.
- **C10** Prose-policing concentrations beyond the seeded purge FRD's survey:
  `cli/tests/schema/test_errors.py` (20+ sentence pins),
  `cli/tests/capabilities/test_retired_shapes.py` (pins plus three blacklists at `:185-186`, `:236`,
  and `:373`, redundant with its own structural tests),
  `cli/tests/manifests/test_capability_shape.py:21-32`, and `test_samples.py:152-171` (generated
  comments pinned line by line). Path corrections and one retraction (2026-08-14, from the sweep
  inventory's read-through): two of these were cited under `schema/` but live in `capabilities/` and
  `manifests/`, which would have read as "deleted" to a later reader. And
  `cli/tests/manifests/test_emit.py`'s 22 disk-backed load cycles are **withdrawn** as a finding:
  they are the loader half of a two-parser soundness pairing, not redundant ceremony. The original
  entry appears to have counted by token rather than by shape.

## Database and migrations (#472, #478, #503, #504, #469, #499)

- **D1** Five-way schema-version reader: `inspect_schema` (`db/backup.py:114-171`) is the one real
  classifier; `backup.py:512-521`, `database.py:70-104`, `database.py:177-192`, and
  `database.py:198-210` each re-derive read-and-classify with their own gaps. This is the direct
  mechanism behind the #503/#504 two-PR patch sequence: BUSY had to be hand-threaded through sites
  that never call the classifier. Entry-path inventory for the consolidation (integration-tester
  addition, 2026-08-13): read-only open, writable open, migrate, and the prepare/open lock paths
  where #504's BUSY translation was observed; the consolidation must preserve BUSY at every one.
  Backup qualification of arbitrary operator-supplied files is boundary validation, not interior
  classification, and keeps its own failure semantics.
- **D2** `SCHEMA_SENTINELS` (`db/migrations.py:631-758`): a hand-maintained shadow model of every
  historical schema version, mechanically derivable by replaying `MIGRATIONS` (which the drift test
  already does independently).
- **D3** Test tax on the two-phase lock protocol: five multiprocess tests with real OS processes and
  20s join timeouts, plus `test_restore_held_destination_lock_honors_fixed_deadline` sleeping 4.5-8
  real seconds per run while its sibling mocks `time.monotonic`.
- **D4** ~30 prose pins in #478's tests (verbatim hint equality, `match=` substrings). #503/#504,
  written after the rule, already comply; the debt is inherited, not spreading.
- **D5** #504 changed the locked safer-migrations SDD's classifier design (new `SchemaState` member,
  new exception type) without a `locked.md` supersession note; #503 set the precedent of writing
  one. Resolved (2026-08-13): the saga lead wrote the supersession note on that lock, recording the
  `BUSY` addition, the surfaces #504 touched, and issue #505 as the tracker for the seams that still
  classify inconsistently. No work remains here.
- **D6** `doctor_state.py` repeats the same three-line exception-to-HealthGroup translation three
  times.

## VM platforms (#479, #475)

- **P1** Catalog selection triplicated verbatim across aws/azure/gcp (`_select_vm_size` /
  `_select_instance_type` / `select_machine_type`), identical algorithm and docstrings.
- **P2** `insert_instance_reconciled` and `insert_firewall_reconciled` duplicate ~150 lines of
  correctness-critical retry/verify/reconcile control flow within gcp.
- **P3** `cleanup.py`: one pure pass-through wrapper (`manual_cleanup_guidance`), and a
  four-function guidance-text chain where a table-driven formatter would do. Most of the file's size
  relative to aws is earned (no resource-group boundary on GCE); the formatting layer is not.
- **P4** gcp's five-class error taxonomy diverges from the one-class-plus-wrapper shape of all three
  siblings; nothing upstream dispatches on the subclasses. Grounded in a real platform difference;
  disposition is a judgment call.
- **P5** gcp test scaffolding un-factored: `_api_error` copy-pasted into five files, the
  extended-operation fake reimplemented five times with drift, while `_aws_fakes.py` and
  `_azure_platform_support.py` model the factored shape one directory over.
- **P6** ~40 prose pins in gcp tests (full-sentence hint equality, sentence-order pins via
  `.index()`); the pattern predates gcp (azure has it) but gcp is the largest concentration.
- **P7** Found sound and worth recording: #475's fallback removal is complete (no dead branches, no
  stale docs); `wait_for_instance_status` polling after a DONE operation wants a one-line comment on
  whether it is load-bearing.

## Website (#439)

- **W1** `tests/test_pages_workflows.py` (834 lines): a hand-rolled YAML parser pinning the CI and
  Pages workflow files verbatim against five hardcoded script constants. Config-file prose-policing;
  GitHub Actions is the real regression guard.
- **W2** `site_validation.py` (1,072 lines) asserts exact attribute dicts, child sequences, and
  visible text for every element of five reviewed templates, exercised by a 578-line
  mutate-and-assert-raises test file. The link/asset-integrity half (~150 lines) is real and stays.
- **W3** `site_content.py` (664 lines): a from-scratch Markdown-subset renderer for three fixed
  files, keyed by verbatim copies of README paragraphs, so every copy edit is a two-file ceremony.
- **W4** Exact CSS token and contrast pins (`"--canvas": "#f5f2e8"`, ratios to three decimals) where
  the invariant is an inequality.
- **W5** `test_lander_404.py` reimplements the shared `site_test_support.py` fixtures it should
  import.
- **W6** `lander-model.test.mjs:211` retypes an unexported status string; export the constant.
- **W7** Atomic-install/rollback machinery (~90 lines plus ~10 failure-injection tests) protecting
  an output directory that is always a fresh single-writer temp dir on an ephemeral CI runner.
- **W8** A hard-required Chromium launch inside the unit suite re-verifying a layout decision a
  CSS-text test two functions above already pins, which the manual browser checklist also covers.
  Corroborating evidence (2026-08-14): `test_site_documents.py`'s `browser_geometry` helper timed
  out after 20 s against headless Chrome in CI, failing the Website job and the `ci-success` gate on
  a docs-only PR (#518) that touched no website file. It passed on a bare re-run. A browser launch
  in the unit suite is not only redundant here, it is a flake surface every unrelated PR pays for.
- **W9** Twelve parallel dictionaries forming a closed-vocabulary framework describing exactly five
  fixed, non-extensible pages. The shape to revisit if W2/W3 are tackled; documented as intentional
  house style in `website/README.md`, so this is a design-revision decision.
- **W10** Payload vs machinery: 2,278 lines of content (1,223 excluding the lander game) against
  6,308 lines of build system plus tests. The lander game (1,055 JS lines, 530 test lines, the
  Chromium test, and a 300-line manual checklist) wants its own scope decision.

## Process tree (~35 docs PRs)

- **PR1** The testing trio (`integration-testing`, `agw-test-env`, `agentworks-tester`) restates 17
  statements in 2+ files, with diverged copies (two incompatible model-tier vocabularies) and stale
  restatements that miss the `awaiting-direction` convention. Operator caution (2026-08-12 review):
  the three files serve different perspectives and some repetition is deliberate reinforcement. The
  target is therefore divergence, not repetition: contradictory and stale copies get one
  authoritative home, while intentional cross-perspective restatement stays.
- **PR2** `agentic-dev-process` sections 6/6a: PR stacks have one throwaway probe plus one depth-2
  docs pair of real usage, and the stack section carries lab-notebook narration (see PR8). The
  draft/ready convention and handoff definition are exercised daily and stay. Correction
  (2026-08-12): this finding originally claimed the `review-requested` label had zero uses ever.
  That was wrong. The study queried current labels (`gh pr list --label ...`), but the convention
  removes the label after each absorbed checkpoint, so compliant past use is invisible to that
  query. Timeline events show heavy use: 16 label events on #479, 14 on #480, plus #475 and #486.
  The label convention is exercised and stays; the method lesson (query events, not current state,
  for anything a convention cleans up after itself) is recorded here so the pass does not repeat it.
- **PR3** "A published review is not authorization" is stated in six places across four documents,
  cross-referencing in a cycle. Section 7a is canonical; the rest become pointers.
- **PR4** `ask-questions`, `push-back`, `permission-to-fail` (2,090 B always-on) are fully restated
  by `development-principles` sections 10 and 13. Unfinished migration; delete and fold the concrete
  phrasings in.
- **PR5** Five always-on rules encode one convention (keep collateral in sync) with five objects;
  collapse to one rule with a five-row table, moving the completions mechanism explanation to a
  module README.
- **PR6** Rule delivery: see the probed facts at the top of this document. Placement, not just size,
  is the problem.
- **PR7** `development-principles` section 1 is a 2,753 B essay whose operative content is its final
  paragraph; the exposition belongs in the manifesto.
- **PR8** Journey narration inside operating instructions (the stacked-PR lab-notebook entry, the
  proven-technique persuasion clause, migration notes for four-day-old conventions).
- **PR9** Register: `github-input-trust` at 5.3 legalese markers per 1k words; the "X, not Y"
  antithesis tic 34 times across the testing skills; definitional negations defending phrases
  against misreadings nobody has made.
- **PR10** `agw-test-env` parameterization apparatus: placeholder policy stated three times, an
  untooled inject ritual, procedures for backends no host can run. The placeholder scheme itself
  guards a real requirement and stays.

# LLD: Phase 1, TOML Resource Sunset

Date: 2026-08-04

Status: DRAFT. Companion to [frd.md](frd.md), [hla.md](hla.md), [plan.md](plan.md) (step 1.1).
Design only; no implementation in this task.

Phase 1 removes the TOML resource-declaration path so phase 2 replaces one decode surface, not two.
This is mostly deletion; the one place new logic is written is the migrator's verification, whose
pre-side loses its source (the built registry) when TOML rows stop loading. That rework is the core.

## 1. The migrator's independent pre-side (the core decision)

### Verdict: RELOCATE the TOML resource loaders into `migrate/`

The plan's named candidate holds. The TOML resource loaders move out of `config/` and into a private
migrate module (working name `migrate/toml_resources.py`) that reads the config file text and
returns decl objects keyed by `(kind, name)`. This is the pre-side oracle. Relocating (not deleting)
is what satisfies FR2's "reads the TOML file directly", and because the oracle reads the FLAT TOML
shape while the post-side reads the emitted TAGGED YAML, the comparison is a real test of the
emission mapping, never tautological.

What relocates (the resource loaders only; settings loaders stay in `config/`):

- `config/loaders_resources.py` in full (vm/agent/workspace templates, admin, named-console, the
  `apt`/install wrappers `_load_apt_and_install_sections`, and `_load_vm_sites_legacy` +
  `_LEGACY_SITE_SECTIONS`, `config/loaders_resources.py:388`).
- `config/loaders_sessions.py`'s `_load_session_templates` (`loaders_sessions.py:64`) and its
  `_session_harness_integration_pair` helper.
- `config/loaders_secrets.py`'s resource half: `_load_secrets` (`loaders_secrets.py:22`) only.
  `_load_secret_backends`, `_load_secret_config`, `_load_plugins`, and the settings machinery stay
  (they load settings and the no-op `[secret_backends]` warning; see section 3).
- `config/loaders_core.py`'s `_load_git_credentials` (`loaders_core.py:395`) only.

What stays in `config/` (shared leaf machinery both the oracle and the surviving decoders import, so
the fork below shares its measuring stick): `config/validation.py` (`validate_name`, the per-kind
caps); `config/loaders_core.py`'s generic helpers `_require`, `_require_string_list`,
`_warn_unexpected_keys`, `_parse_env_table`, `_warn_nonconforming_secret_name`,
`_warn_nonconforming_derived_secret` (`loaders_core.py:97`), `_expand`; and the apt/install domain
loaders in `agentworks.apt` / `agentworks.install_commands` (already outside `config/`; the
relocated wrapper calls them as today).

`_warn_nonconforming_derived_secret` STAYS shared deliberately (alongside
`_warn_nonconforming_secret_name`, which already stays): the git-credential decoder gets today's
non-conforming default-token-name warning for free by delegating to `_load_git_credentials`
(`decode.py:411`), and after the fork it must keep emitting it or YAML git-credentials silently lose
warning parity with the oracle. The decoder calls the shared helper directly. This applies to
warnings, not only errors: "the decoder owns its per-kind validation" (below) means it reproduces
the loader's soft issues too, and sharing the two derived-secret helpers is how it does so without
duplicating them.

The apt/install/vm-site loaders read a `_SectionLineMap`-shaped `decls` for `declared_at`. The
oracle builds a real `_SectionLineMap` over the original text exactly as `config/load.py:136` does
today, so the relocated loaders run unchanged.

### The decoders absorb their own logic (the sanctioned interim fork)

Per HLA phase 1, the decode layer stops routing through the loaders: the `_FixedDecls` /
`_decls(...)` shim (`manifests/decode.py:207-225`) and every `from agentworks.config import _load_*`
call inside a `_decode_*` are deleted, and each decoder owns its per-kind field validation directly.
This forks the per-kind assembly: the relocated loaders (flat TOML -> decl) and the absorbed
decoders (tagged YAML -> decl) now carry near-duplicate validation. That is a duplication smell
(principle 8), accepted here because it is exactly what buys verification independence and is
throwaway: phase 2 step 2.5 dissolves the decoder side into kind spec models, while the relocated
loaders persist as the migrator's frozen TOML reader. Shared leaf validators (above) keep the fork
narrow.

Independence is precise, not blanket. The kinds with NON-TRIVIAL emission (git-credential's flatten,
`planning.py:765`; vm-site's tagged-table build, `planning.py:734`; the session hoist,
`planning.py:805`) are where it bites: for those, the oracle's flat-TOML derivation shares no
shape-mapping with the emit-then-decode post-side, so the comparison genuinely tests
`_emit_document`. The trivial-emission kinds (apt-source/package, install-commands: emission is
envelope-wrapping only) and the shared leaf validators are symmetric measuring sticks applied to
differing inputs, not independent oracles, and that is fine: their decoders import from
`agentworks.apt` / `agentworks.install_commands` (`decode.py:534-555`), not from the relocated
loaders, and `_parse_env_table` is shared, so a bug there cancels on both sides exactly as it does
today. The claim is "no shared shape-mapping for the non-trivial kinds", not "no shared code
anywhere".

Rejected alternative: let the decoders keep calling the relocated loaders from `migrate/`. That
re-couples the post-side to the oracle (partly restoring the tautology) and inverts the dependency
direction (core manifest decode reaching into the operator-invoked migrate tool). The fork is the
better cost.

### What `plan_migration` and verification become

`plan_migration` drops its `registry` parameter and becomes pure over the config text
(`planning.py:124`).

NOTE (superseded 2026-08-07, phase 2): planning is NOT pure any more, and the change was deliberate.
`preflight.require_loadable_tree` builds a registry over the tree the run WOULD produce, which is
what lets a dry run reach the real run's verdict. Purity was never the property that mattered; what
mattered is that the migrator works on a config no other command can load, and that survives,
because the two things that stop those configs loading are exactly the two the preflight
neutralizes. `migrate/preflight.py` carries the full argument. Read every "pure over the config
text" in this document as "reads the config text directly, without the registry the command used to
build first".

`pre_rows` is no longer `normalized_rows(registry)` (`planning.py:221`); it is built from two
independent sources, keyed by `(kind, name)` and passed through `strip_source_fields`, and FILTERED
TO THE SELECTED MIGRATION UNITS (`plan.units`), NOT the full relocated-loader output:

- TOML units: the relocated loaders over the ORIGINAL config text, then filtered to the selected
  units' `(kind, name)`.
- YAML-rewrite units (the harness-selector / `restart_command` session templates,
  `planning.py:301`): the ORIGINAL YAML documents decoded through the manifest decoder. This path
  never involved TOML; its verification is unchanged in spirit (decode original vs decode rewritten,
  compare), and `strip_source_fields` already normalizes `restart_command_compat` (`verify.py:58`)
  so a `restart_command` -> `resume_command` rewrite compares equal.

The unit-scoping is load-bearing and inverts today's full-scope symmetric baseline. Because the
post-side is `load_config(rewritten, resources=False)` (below), which does NOT load un-migrated TOML
rows, a full-scope pre (every TOML resource in the file) would make every PARTIAL migration
false-fail: each un-migrated `(kind, name)` would be present in pre and absent in post, tripping the
missing branch into `StateError` and a needless rollback. Scoping pre to `plan.units` is what makes
incremental migration verifiable at all under the new post-side.

The `registry`-only helper `_declared_at` (`planning.py:635`, best-effort file:line for the
dotted-key refusal) drops to the text scan `_section_location` (`planning.py:646`) it already falls
back to; minor loss of precision on that one error's line number, noted.

`execute._verify` (`execute.py:293`) changes its post-side load to the settings-only escape hatch:

```text
post = normalized_rows(build_registry(load_config(rewritten_path, resources=False, warn_issues=False)))
```

`resources=False` is load-bearing: after a partial migration the rewritten config may still carry
un-migrated resource sections, which the phase-1 hard error (section 2) would otherwise reject at a
normal load. This post-load ALSO depends on retiring the `build_registry` `resources_loaded` guard
(section 3): today `bootstrap.py:87-92` raises `StateError` on a `resources=False` Config, so
`build_registry(load_config(..., resources=False))` cannot run until that guard is removed. The two
land together. Settings-only load skips both the hard-error check and TOML resource loading, while
manifests (including the just-emitted YAML) still load, so the post-registry is the operator's real
post-upgrade world minus the not-yet-migrated TOML rows. `first_difference` (`verify.py:34`) narrows
from a symmetric diff to a pre-keys-scoped one: each pre `(kind, name)` must be present and equal in
post. Dropping the "added" branch is NECESSARY here, not merely convenient: once pre is scoped to
`plan.units`, the post-registry legitimately carries rows pre does not (built-ins, auto-declared,
other-manifest), so a symmetric "added" check would false-fail on every run.

Dropping "added" opens a narrow fabrication gap (an emission that invents an EXTRA row for a
selected unit would not be caught by a pre-keys walk). The structural argument bounds it:
`_emit_document` runs exactly once per unit (`planning.py:679` for the TOML writes, one document per
unit at `planning.py:873`), so a unit cannot silently fan out into multiple rows. But the LLD adds
an explicit guard rather than resting on the argument: the reworked `_verify` asserts that the
emitted documents (the plan's writes plus YAML-rewrite contributions) decode to EXACTLY the pre key
set (a key-set/count equality over the migrated units' contribution to post), closing the gap
without reintroducing whole-registry false positives. The check thus GAINS "the output loads and
integrates" (the full registry builds, collisions surface as load errors) and "no unit fabricated an
extra row" without LOSING "meaning preserved" (the oracle proves it).

### Verification data flow, before and after

| Stage        | Today                                           | Phase 1                                                                                    |
| ------------ | ----------------------------------------------- | ------------------------------------------------------------------------------------------ |
| pre-side     | `normalized_rows(registry)` (TOML in registry)  | relocated loaders over original TOML + decode of original YAML, scoped to `plan.units`     |
| post-side    | `build_registry(load_config(rewritten))` (full) | `build_registry(load_config(rewritten, resources=False))`                                  |
| compares     | symmetric keyed diff                            | pre-keys-scoped present-and-equal, plus emitted-docs key-set equality guard                |
| independence | pre and post both flow through the loaders      | non-trivial kinds: pre = loaders(flat), post = decode(emit(flat)), no shared shape-mapping |

## 2. Hard-error mechanics

Today `_warn_deprecated_resource_sections` (`loaders_secrets.py:128`) appends one aggregated string
to `deprecations`, surfaced on the `--no-deprecations` channel (`config/load.py:230-235`). It
becomes a raising check, relocated into `config/load.py` (its home is orchestration, not secrets) as
`_raise_for_resource_sections(data)`, gated on `resources=True` and run before the settings loaders.
It reuses the existing `KIND_SECTIONS` presence sweep (minus `secret_backends`) and the same
grep-able display shapes (`[secrets.*]`, `[admin.config]`, `[azure]`, ...). It raises `ConfigError`:

```text
config.toml declares resources, which config.toml no longer supports (it is settings only now):
[azure], [secrets.*]. Move them to YAML manifests with `agw resource migrate <kind>` (or
`agw resource migrate --all`) (the [azure]/[proxmox] sections migrate as vm-site), or author new
manifests from `agw resource sample <kind>`. Then remove the section(s) from config.toml.
```

The `(the [azure]/[proxmox] ...)` clause appears only when a legacy site section is present,
matching today's `site_hint`.

Escape hatch: `load_config(resources=False)` skips the check. `agw resource sample --write` and
`agw resource edit`'s fallback already load this way (`config/load.py:96`, `resource.py:271`). The
one that MOVES is `agw resource migrate`: `resource.py:384` changes from
`load_config(warn_deprecations=False)` + `build_registry(config)` to `load_config(resources=False)`
with no registry build at the command layer (the post-side builds its own registry). The "planning
is now pure over text" clause that stood here is superseded; see the note in section 1.

`--no-deprecations` channel afterward: the TOML resource nudge LEAVES (it is now an error). What
REMAINS: the #349 tagged-shape warning (`decode.py:186` `capability_shape_deprecation`), the
harness-selector warning (`bootstrap.py:142`), and the settings-side `defaults.platform` alias
deprecation (`_load_defaults`, `loaders_core.py:381`, threaded via `config/load.py:199`), which is a
settings deprecation and unaffected by this phase. NOTE (LLD discrepancy 1, resolved): the
`restart_command` warning's config-channel block (`config/load.py:181`) is DEAD after this phase,
its only producer is `_load_session_templates`, which relocates out of the config-load path, and
`[session_templates.*]` now hard-errors, so no config.toml input can feed it. Remove the dead block;
the `restart_command` deprecation continues to fire from the YAML manifest path
(`ManifestSet.deprecation_issues`), which is unchanged. The `[secret_backends.*]` no-op warning
(`_load_secret_backends`, `loaders_secrets.py:85`) also stays on the channel: it is a
capability-kind no-op section, never a resource declaration, and its removal is owned by
`agw resource migrate --all`'s existing drop, not by FR1. (Open question, section: whether the
operator wants it folded into the hard error too; the LLD keeps it a warning as the minimal,
separable call.)

## 3. Consumer inventory and dispositions

- **`manifests/decode.py`**: delete `_FixedDecls`/`_decls` and every route-through-loader call; each
  `_decode_*` absorbs its validation (section 1). KEEP `KIND_SECTIONS` (`decode.py:42`, the
  migrator's shared table), `CAPABILITY_FIELDS`, `_normalize_capability_field`,
  `_normalize_session_harness_selector`, `capability_shape_deprecation` (all #349 / harness-selector
  machinery, TOML-independent). KEEP the `_warn_nonconforming_secret_name` import (survivor in
  `config/`).
- **The Config population path**: `config/load.py` stops calling the resource loaders;
  `resource_data` and the resource-loader calls (`load.py:157-213`) go. `Config`'s TOML-resource
  fields (`vm_templates`, `secrets`, `session_templates`, `git_credentials`, `vm_sites`,
  `agent_templates`, `workspace_templates`, `admin`, `named_console`, `apt_*`, `*_install_commands`)
  and `Config.publish_to`'s resource loop (`models.py:189-218`) are removed. `apt.publish_to` /
  `install_commands.publish_to` (`apt.py:156`, `install_commands.py:134`) are DELETED, not emptied
  (LLD discrepancy 2, resolved): the bundled apt/install entries publish via
  `builtin_manifests.publish_to` (`bootstrap.py:113`), and these two functions' ONLY current job is
  the operator TOML half, so once it is removed the functions have no body and no caller. Delete
  them along with their `bootstrap.py:114-115` calls; the `_load_apt_*` / `_load_*_commands` domain
  helpers they wrapped stay (still used by `decode.py`). Consumers read resources from the registry,
  never Config (ADR 0016), so mypy finds any stragglers. This is a decision the LLD makes explicit
  beyond the HLA's literal "delete the loaders": always-empty resource fields would be a field that
  lies (principles 5, 6) and a half-migrated state (principle 10). Reviewer confirmation invited.
- **`resources_loaded` + `build_registry` guard** (`models.py:143`, `bootstrap.py:87`): retire. With
  no TOML resource side to publish, the "settings-only Config must not silently publish empty" guard
  has nothing to guard; the escape hatch is now expressed purely by `resources=False` gating the
  hard-error check.
- **`resource edit` TOML pointer** (`resource.py:226-228` docstring): TOML-declared rows no longer
  exist, so drop the "TOML-declared resources error with a pointer" clause; the manifest-scan
  fallback is otherwise unchanged.
- **DB-migration snippet printer** (`db/migrations.py:47` `_migrate_vm_sites`): UNAFFECTED. It reads
  a best-effort raw `tomllib` parse (`MigrationContext.legacy`, `migrations.py:34`), never the
  loaders, and its `azure`/`proxmox` names are frozen v27 vocabulary.
- **doctor rows**: remove the dead `deprecated_sections` warn row (`doctor.py:537-541`). The hard
  error must NOT abort the whole report: doctor's config-load `except` at `doctor.py:471` currently
  `return g, None, None`, which for a mid-migration operator (exactly the person doctor helps) would
  truncate the entire report to one fail row. Change that handler to catch the new resource-section
  `ConfigError`, render it as a fail row, then RETRY `load_config(resources=False)` and continue
  with the rest of the report, mirroring the deliberately non-fatal manifest-load handling at
  `doctor.py:499-507` (fail row, keep rendering). KEEP the `noop_secret_backend_sections` row
  (`doctor.py:553`), the manifest-shape row, and the harness-selector row (all TOML-independent).
- **`test_graph_guard.py` allowlist** (`test_graph_guard.py:277`): the `config/loaders_secrets.py`
  entry STAYS. It exists for `_load_secret_backends` reading `SECRET_BACKEND_REGISTRY`
  (`loaders_secrets.py:106`), which survives. The relocated loaders do not call `.validate` /
  `.dependencies` / registry reads, so they need no new entry; if the new migrate module trips a
  detector, add it beside the existing `migrate/planning.py` entry (`test_graph_guard.py:273`).
- **`config/__init__.py` / `config/load.py` imports** (`__init__.py:65-86`, `load.py:15-32`): drop
  the relocated-loader imports and their `__all__` re-exports. The re-exports existed for the
  decoders and tests to reach in; decoders no longer call them, and tests move to the migrate oracle
  or manifest decode.
- **`Config.deprecation_issues` / `deprecated_sections`**: `deprecated_sections` (and its
  `models.py` field, `models.py:135`) retire with the warn row above. `deprecation_issues` STAYS,
  now carrying the `[secret_backends]` no-op and `defaults.platform` alias messages (the
  `restart_command` config-channel block is dead and removed, discrepancy 1 above; that deprecation
  now fires only from the YAML manifest path).

## 4. Test plan

- **Warning -> error flip**: `tests/test_config_deprecation_warnings.py` and
  `tests/vms/test_legacy_site_sections.py` move from asserting a nudge on `deprecation_issues` to
  asserting `ConfigError` on `load_config()` with resource sections present, and asserting NO error
  (and settings loaded) when `resources=False` or when only settings sections are present. Doctor's
  deprecated-sections assertions in `tests/test_doctor_env_and_secrets.py` move to the config-load
  fail row.
- **Exempted-commands tests**: `resource sample --write`, `resource edit` fallback, and
  `resource migrate` all succeed against a config carrying resource sections (they load
  `resources=False`); a normal command against the same config errors.
- **End-to-end migrate** (`test_full_migration_golden` in `tests/test_resource_migrate.py`): fixture
  config exercising every branch in one run: a legacy `[proxmox]` (or `[azure]`) site with a
  `token_secret`, `[git_credentials.*]` with a defaulted token, `[secrets.*]` with backend mappings,
  and a `[session_templates.*]` that ALSO carries a legacy `harness` selector (to drive the
  YAML-native rewrite path alongside the TOML path). Assert: emitted YAML decodes and loads, the
  scoped comparison passes, and (rollback) a deliberately corrupted emission triggers `StateError`
  with full rollback (files and TOML restored to original digests).
- **Verification independence** (new): a unit test that a fixture with an emission bug (e.g. a
  dropped field in `_emit_document`) makes the scoped comparison FAIL, proving the oracle is not
  derived through `_emit_document`.

## 5. Records

- **Superseding ADR 0022** replaces ADR 0016's "Dual-path: deprecate, don't break" section
  (`0016-yaml-resource-manifests.md:150-165`): the stance becomes single-frontend (YAML manifests
  are the only resource declaration path; config.toml is settings only). Scope is the dual-path
  stance ONLY; ADR 0016's two-layer model, vocabulary law, resources-reference-capabilities,
  envelope / auto-load, and slash ban all stand. ADR 0016 gains a `Superseded by ADR 0022` pointer
  on its status-note block. ADR 0022 (or an inline 0016 annotation) MUST also correct ADR 0016's
  Consequences bullet at `0016-yaml-resource-manifests.md:169` ("the same validation as TOML (the
  manifest decoders call the TOML loaders, so the two sources cannot drift)"): the decoder fork
  (section 1) falsifies it outright, so left unrevised ADR 0016 would ship a false implementation
  claim. State that the manifest decoders now own their validation and the TOML reader is the
  migrator's private oracle.
- **Lockfile entries** appended to `docs/sdd/2026-07-01-resource-manifests/locked.md` (the SDD whose
  machinery phase 1 retires), dated, enumerating what that SDD shipped and this phase now retires:
  the TOML resource loaders (Phase 2's decode-through-TOML-loaders parity, Phase 5 per-section
  warnings) and `agw resource migrate`'s registry-sourced verification pre-side (Phase 4). Note the
  migrator itself, its backup-first ordering, and rollback all survive; only its pre-side source
  changed. No other locked SDD's stance is revised by phase 1 (the vm-sites and registry-readiness
  SDDs are touched in phase 2, not here).

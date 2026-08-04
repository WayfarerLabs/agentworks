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
- `config/loaders_core.py`'s `_load_git_credentials` (`loaders_core.py:395`) and its private
  secret-name-derivation helper `_warn_nonconforming_derived_secret` (`loaders_core.py:97`).

What stays in `config/` (shared leaf machinery both the oracle and the surviving decoders import, so
the fork below shares its measuring stick): `config/validation.py` (`validate_name`, the per-kind
caps); `config/loaders_core.py`'s generic helpers `_require`, `_require_string_list`,
`_warn_unexpected_keys`, `_parse_env_table`, `_warn_nonconforming_secret_name`, `_expand`; and the
apt/install domain loaders in `agentworks.apt` / `agentworks.install_commands` (already outside
`config/`; the relocated wrapper calls them as today).

The apt/install/vm-site loaders read a `_SectionLineMap`-shaped `decls` for `declared_at`. The
oracle builds a real `_SectionLineMap` over the original text exactly as `config/load.py:136` does
today, so the relocated loaders run unchanged.

### The decoders absorb their own logic (the sanctioned interim fork)

Per HLA phase 1, the decode layer stops routing through the loaders: the `_FixedDecls` /
`_decls(...)` shim (`manifests/decode.py:207-225`) and every `from agentworks.config import _load_*`
call inside a `_decode_*` are deleted, and each decoder owns its per-kind field validation directly.
This forks the per-kind assembly: the relocated loaders (flat TOML -> decl) and the absorbed
decoders (tagged YAML -> decl) now carry near-duplicate validation. That is a duplication smell
(principle 8), accepted here because it is exactly what buys verification independence (the oracle
shares no shape-mapping with the post-side) and is throwaway: phase 2 step 2.5 dissolves the decoder
side into kind spec models, while the relocated loaders persist as the migrator's frozen TOML
reader. Shared leaf validators (above) keep the fork narrow.

Rejected alternative: let the decoders keep calling the relocated loaders from `migrate/`. That
re-couples the post-side to the oracle (partly restoring the tautology) and inverts the dependency
direction (core manifest decode reaching into the operator-invoked migrate tool). The fork is the
better cost.

### What `plan_migration` and verification become

`plan_migration` drops its `registry` parameter and becomes pure over the config text
(`planning.py:124`). `pre_rows` is no longer `normalized_rows(registry)` (`planning.py:221`); it is
built from two independent sources, keyed by `(kind, name)` and passed through
`strip_source_fields`:

- TOML units: the relocated loaders over the ORIGINAL config text.
- YAML-rewrite units (the harness-selector / `restart_command` session templates,
  `planning.py:301`): the ORIGINAL YAML documents decoded through the manifest decoder. This path
  never involved TOML; its verification is unchanged in spirit (decode original vs decode rewritten,
  compare), and `strip_source_fields` already normalizes `restart_command_compat` (`verify.py:58`)
  so a `restart_command` -> `resume_command` rewrite compares equal.

The `registry`-only helper `_declared_at` (`planning.py:635`, best-effort file:line for the
dotted-key refusal) drops to the text scan `_section_location` (`planning.py:646`) it already falls
back to; minor loss of precision on that one error's line number, noted.

`execute._verify` (`execute.py:293`) changes its post-side load to the settings-only escape hatch:

```text
post = normalized_rows(build_registry(load_config(rewritten_path, resources=False, warn_issues=False)))
```

`resources=False` is load-bearing: after a partial migration the rewritten config may still carry
un-migrated resource sections, which the phase-1 hard error (section 2) would otherwise reject at a
normal load. Settings-only load skips both the hard-error check and TOML resource loading, while
manifests (including the just-emitted YAML) still load, so the post-registry is the operator's real
post-upgrade world minus the not-yet-migrated TOML rows. `first_difference` (`verify.py:34`) narrows
from a symmetric diff to a pre-keys-scoped one (each pre `(kind,name)` present and equal in post;
the "added" branch drops, because the full post-registry legitimately carries untouched built-in /
auto-declared / other-manifest rows). The check thus GAINS "the output loads and integrates" (the
full registry builds, collisions surface as load errors) without LOSING "meaning preserved" (the
oracle proves it).

### Verification data flow, before and after

| Stage        | Today                                           | Phase 1                                                           |
| ------------ | ----------------------------------------------- | ----------------------------------------------------------------- |
| pre-side     | `normalized_rows(registry)` (TOML in registry)  | relocated loaders over original TOML + decode of original YAML    |
| post-side    | `build_registry(load_config(rewritten))` (full) | `build_registry(load_config(rewritten, resources=False))`         |
| compares     | symmetric keyed diff                            | pre-keys-scoped: present-and-equal in post                        |
| independence | pre and post both flow through the loaders      | pre = loaders(flat); post = decode(emit(flat)); no shared mapping |

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
with no registry build (planning is now pure over text; the post-side builds its own registry).

`--no-deprecations` channel afterward: the TOML resource nudge LEAVES (it is now an error). What
REMAINS: the #349 tagged-shape warning (`decode.py:186` `capability_shape_deprecation`), the
harness-selector warning (`bootstrap.py:142`), and the `restart_command` warning
(`config/load.py:181`). The `[secret_backends.*]` no-op warning (`_load_secret_backends`,
`loaders_secrets.py:85`) also stays on the channel: it is a capability-kind no-op section, never a
resource declaration, and its removal is owned by `agw resource migrate --all`'s existing drop, not
by FR1. (Open question, section: whether the operator wants it folded into the hard error too; the
LLD keeps it a warning as the minimal, separable call.)

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
  and `Config.publish_to`'s resource loop (`models.py:189-218`) are removed, as are the
  config-reading halves of `apt.publish_to` / `install_commands.publish_to` (`apt.py:156`,
  `install_commands.py:134`; they publish only bundled manifests now). Consumers read resources from
  the registry, never Config (ADR 0016), so mypy finds any stragglers. This is a decision the LLD
  makes explicit beyond the HLA's literal "delete the loaders": always-empty resource fields would
  be a field that lies (principles 5, 6) and a half-migrated state (principle 10). Reviewer
  confirmation invited.
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
- **doctor rows**: remove the dead `deprecated_sections` warn row (`doctor.py:537-541`); the hard
  error now surfaces through doctor's existing config-load `ConfigError` fail row (`doctor.py:472`).
  KEEP the `noop_secret_backend_sections` row (`doctor.py:553`), the manifest-shape row, and the
  harness-selector row (all TOML-independent).
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
  now carrying only the `restart_command` and `[secret_backends]` no-op messages.

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
  on its status-note block.
- **Lockfile entries** appended to `docs/sdd/2026-07-01-resource-manifests/locked.md` (the SDD whose
  machinery phase 1 retires), dated, enumerating what that SDD shipped and this phase now retires:
  the TOML resource loaders (Phase 2's decode-through-TOML-loaders parity, Phase 5 per-section
  warnings) and `agw resource migrate`'s registry-sourced verification pre-side (Phase 4). Note the
  migrator itself, its backup-first ordering, and rollback all survive; only its pre-side source
  changed. No other locked SDD's stance is revised by phase 1 (the vm-sites and registry-readiness
  SDDs are touched in phase 2, not here).

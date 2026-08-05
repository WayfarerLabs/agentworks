# Red-window inventory (step 1.2 -> 1.2f)

Date: 2026-08-04

Step 1.2 (the TOML resource sunset production change) lands with the suite intentionally RED on
pre-existing fixtures that declare resources in `config.toml`, per the operator-approved bounded red
window. This file enumerates that red set so the window is explicitly bounded; step 1.2f burns it
down (fixture conversion to green).

## Suite state at the close of 1.2

- `565 failed, 2858 passed, 22 errors` (`uv run pytest -q` from `cli/`).
- The 22 errors are fixture-setup hard errors (a shared fixture builds a config with a resource
  section), not a distinct failure class.

## Slice 1 of 1.2f (close) -- shared lever + `tests/` root, done

Date: 2026-08-05.

- Suite now `233 failed, 3189 passed, 11 errors` (`uv run pytest -q` from `cli/`). No NEW failures
  vs the 1.2 baseline (verified by diffing the failing node-id set); every remaining failure is a
  still-inventoried, not-yet-converted file in a reserved area.
- `uv run ruff check` / `ruff format --check` clean on all changed files. `uv run mypy .` is clean
  except 6 `[attr-defined]` errors in `tests/resources/test_admin_template_plurified.py` (a reserved
  file that reads the retired `Config.admin`; converts in a later slice).
- `./scripts/lint-files.sh` is clean for this slice's files; its only failures are pre-existing
  markdownlint/prettier issues in `plan.md`, untouched by this slice.

### The shared lever (built)

- `cli/tests/conftest.py` gained `ManifestDoc` (a frozen `kind`/`name`/`spec`/`description` record)
  and `write_manifests(config_dir, *docs, filename=...)`, which writes a `resources/` manifest dir
  beside a config.toml (matching `RESOURCES_DIRNAME`). Docs are `ManifestDoc`s (enveloped for you)
  or raw YAML strings (the escape hatch for declaring-file-name / malformed-YAML assertions).
- `cli/tests/orchestrated_fixtures.py`: `make_config` now declares the proxmox site through a
  `vm-site` manifest (`proxmox_site()`) instead of baking `[proxmox]` into TOML;
  `write_operator_config` grew a `manifests=` param. `PROXMOX_SECTION` (the TOML string) is retained
  for the reserved-area callers that still pass it as a hard-error case until their own slice.

### Cascade (green for free)

Converting the shared orchestrated fixture turned 6 reserved-area files green with no per-file
edits: `tests/agents/test_delete_grant_revoke_orchestrated.py`,
`tests/sessions/test_console_attach_orchestrated.py`,
`tests/sessions/test_singular_batch_orchestrated.py`, `tests/vms/test_ensure_tailscale_wording.py`,
`tests/vms/test_lifecycle_orchestrated.py`, `tests/vms/test_remaining_commands_orchestrated.py`.

### Redesign / deletion calls (behavior structurally gone under ADR 0022)

- `test_apt_declared_at.py`: removed `test_operator_toml_entry_declared_at_stays_synthesized` (TOML
  apt-source surface is gone).
- `test_resource_edit.py`: removed `test_toml_declared_resource_points_at_migrate_or_config_edit` (a
  TOML-declared resource can no longer enter the registry).
- `test_config.py`: removed `test_git_credential_type_still_accepted` and
  `test_git_credential_provider_wins_over_type` (manifests reject the legacy `type` key outright).
- `test_config_line_capture.py`: collapsed the 8 per-kind TOML-header line tests into one
  parametrized manifest `declared_at` test; removed the TOML implicit-parent subsection test and the
  two omitted-singleton `Config.admin`/`Config.named_console` sentinel tests.
- `test_git_credential_scoping.py`: removed `test_toml_github_scope_keys_warn_and_unscope` (the flat
  TOML manifest-only-key warn/strip has no manifest analogue; manifest scope path is covered).
- `test_capability_config_contract.py`: removed `test_github_toml_stray_org_keeps_loading`
  (collapses into its yaml sibling; stray blob fields are rejected, not silently ignored).
- `test_builtin_entries_parity.py`: removed `test_operator_toml_override_wins_over_builtin`
  (identical to its manifest sibling once TOML is gone).
- `test_doctor_env_and_secrets.py`: removed the #310 pair
  (`test_config_load_validation_error_yields_fail_row_not_abort` and its `run_checks` sibling): the
  `[secrets.*]`-name `ValidationError`-at-load scenario is structurally impossible (the section
  hard-errors as a resource-section `ConfigError` first, and no settings-side load path raises
  `ValidationError`). The guard's explicit `ValidationError` branch stays as defensive coverage.

All other converted tests preserved intent: reads of retired `Config` fields re-point to
`registry.lookup` / `iter_kind_items`, `config_issues` warnings that moved to the manifest channel
re-point to `load_manifests(...).issues`, and a `ValidationError` at load became a `ConfigError` at
build where manifest decode wraps the spec-level error (message substrings kept).

## Root-cause buckets (every failure is one of these; no surprise regressions)

Bucketing the whole failure output by exception signature:

- **~1102 hits: the resource-section hard error**
  (`config.toml declares resources, which config.toml no longer supports (it is settings only now)`).
  The fixture writes a resource-declaring TOML section (`[secrets.*]`, `[vm_templates.*]`,
  `[git_credentials.*]`, `[proxmox]`, `[admin.config]`, ...) and then loads it on the normal path.
  This is the overwhelming majority.
- **~16 hits: access to a removed `Config` field**
  (`'Config' object has no attribute 'named_console' / 'admin' / 'resources_loaded'`, and the
  sibling resource dicts). Tests that read the retired TOML-resource fields or the retired
  `resources_loaded` guard.

No other cause appears (no import failures in production, no `AttributeError` on production objects,
no API-mismatch `TypeError` outside the migrator, which step 1.2 fixed). The step's own new/flipped
tests all pass (see below).

## Step-owned tests (GREEN at the close of 1.2)

- `tests/test_resource_migrate.py` (96): the reworked API, the enhanced `test_full_migration_golden`
  (proxmox/azure + git creds + secrets + a legacy YAML `harness`/`restart_command` selector rewrite
  in one `--all` run), and the new `test_verification_is_independent_of_emission`.
- `tests/test_config_deprecation_warnings.py` (12): the warning -> hard-error flip, the escape
  hatch, the remaining deprecation-channel content, and the exempted commands (migrate / sample
  --write / edit fallback run; a normal command errors).
- `tests/vms/test_legacy_site_sections.py` (8): `[azure]`/`[proxmox]` hard error, the settings-only
  escape hatch, the `[defaults]` alias behavior, and the YAML vm-site manifest token_secret warning.
- `tests/test_doctor_env_and_secrets.py::test_doctor_resource_sections_fail_row_and_continues`: the
  doctor fail-row-plus-`resources=False`-retry (not a truncated report).
- `tests/resources/test_graph_guard.py` (10): the relocated loaders trip no new detector.

## The big 1.2f lever: a shared resources-dir fixture helper

There is no shared "write a resources/ manifest dir" test helper today; each file authors config and
(where present) manifests inline, and the orchestrated suites share `tests/orchestrated_fixtures.py`
and `tests/conftest.py` helpers that write resource-declaring TOML. 1.2f should add that helper
first, then convert per area. 28 files already author YAML manifests inline as the pattern to
follow.

Most files are **convert-to-YAML**: move the fixture's TOML resource sections into a sibling
`resources/*.yaml` manifest dir (or hand-build registry rows where the test asserts registry/graph
outcomes). A minority need **redesign** (they assert on a removed `Config` field) or pin **TOML-only
behavior** (loader-parity / `declared_at` line capture) and cannot be mechanically relocated.

## Red files by area

### `tests/` (config, secrets, git-credentials, resource CLI) -- DONE (slice 1 of 1.2f)

Every file in this section is green as of slice 1; see the slice-1 section above for the redesign /
deletion calls. The per-file notes below are retained as the record of what each conversion faced.

- `test_config.py` -- redesign: asserts on removed `Config` resource fields; keep only settings
  load.
- `test_config_env_and_secrets.py` -- convert-to-YAML (env-block secrets on templates).
- `test_config_line_capture.py` -- pins TOML-only behavior (`declared_at` line numbers for TOML
  resources; that surface is gone, redesign around manifest `declared_at`).
- `test_config_plugins.py` -- redesign: asserts `resources_loaded` on both load paths (field
  retired); keep the `[plugins]` settings-load assertions.
- `test_apt.py`, `test_apt_declared_at.py` -- convert-to-YAML (operator apt entries were TOML).
- `test_builtin_entries_parity.py` -- convert-to-YAML / redesign (built-in vs operator parity).
- `test_capability_config_contract.py` -- convert-to-YAML (capability config via manifests).
- `test_env_block_references.py`, `test_env_show.py` -- convert-to-YAML.
- `test_git_credential_scoping.py` -- redesign (reads `config.git_credentials`) + convert-to-YAML.
- `test_git_credentials_subgraph_walk.py`, `test_git_credentials_token_resolve.py`,
  `test_git_credentials_typo_errors.py`, `test_git_token_verification.py` -- convert-to-YAML.
- `test_resource_describe.py`, `test_resource_edit.py`, `test_resource_list.py` -- convert-to-YAML.
- `test_sample_config_git_credentials.py` -- redesign (reads `config.git_credentials`).
- `test_secret_describe.py`, `test_secret_describe_no_prompt.py`, `test_secrets_inspect.py` --
  convert-to-YAML.
- `test_secrets_orchestration.py` -- redesign (reads `config.secrets`) + convert-to-YAML.
- `test_session_create_ephemeral_secret_target_parity.py` -- convert-to-YAML.
- `test_templates.py` -- convert-to-YAML (vm/agent/workspace templates).
- `test_vm_create_tailscale_eager_resolve.py` -- convert-to-YAML.
- `test_registry_warning_boundary.py` -- convert-to-YAML (shared fixture declares resources).
- `test_doctor_env_and_secrets.py` -- convert-to-YAML: the shared `_write_config` declares
  `[vm_templates.default]` + `[admin.config]`; the `_check_secrets` tests call `load_config`
  directly and hit the hard error. (The doctor fail-row test is already green.)

### `tests/resources/`

- `test_access.py`, `test_admin_template_plurified.py`, `test_always_materialize.py`,
  `test_git_credential_provider_kind.py`, `test_graph.py`, `test_install_resource_kinds.py`,
  `test_instances.py`, `test_registry_lifecycle.py`, `test_singleton_publishing.py`,
  `test_template_kinds.py`, `test_vm_template_kind.py` -- convert-to-YAML / hand-built registry rows
  (these assert registry/graph outcomes seeded from TOML resources).

### `tests/manifests/`

- `test_capability_shape.py` -- convert-to-YAML (a shared config declares resources).
- `test_decode_parity.py` -- pins TOML-only behavior: it pins "decode routes through the TOML
  loader" parity, which this step deliberately forks apart. Redesign to oracle-vs-decode, or retire
  (the migrator's `test_verification_is_independent_of_emission` now carries the independence
  claim).

### `tests/sessions/`

- `test_session_template_surface.py` -- convert-to-YAML: exercises the relocated
  `_session_harness_integration_pair` TOML hoist; move to the migrator oracle or manifest decode.
- `test_session_list_harness_integration.py`, `test_session_nodes.py` -- convert-to-YAML.
- `test_console_attach_orchestrated.py`, `test_create_resume_orchestrated.py`,
  `test_singular_batch_orchestrated.py` -- convert-to-YAML (shared orchestrated fixture).

### `tests/vms/`

- `test_add_git_credential_orchestrated.py`, `test_create_reinit_orchestrated.py`,
  `test_create_vm_dispatch.py`, `test_delete_vm_gating.py`, `test_ensure_tailscale_wording.py`,
  `test_lifecycle_orchestrated.py`, `test_live_vm_boundary.py`,
  `test_remaining_commands_orchestrated.py`, `test_shell_exec_orchestrated.py` -- convert-to-YAML
  (shared orchestrated fixture writes `[proxmox]` / templates).

### `tests/agents/`

- `test_agent_home_permissions.py`, `test_create_reinit_orchestrated.py`,
  `test_delete_grant_revoke_orchestrated.py`, `test_shell_exec_orchestrated.py` -- convert-to-YAML.

### `tests/workspaces/`

- `test_create_orchestrated.py`, `test_lifecycle_orchestrated.py` -- convert-to-YAML.

### `tests/orchestration/`

- `test_readiness.py`, `test_secrets.py` -- convert-to-YAML (shared fixture declares resources).

### `tests/plugins/`

- `test_azure.py`, `test_claude.py`, `test_codex.py`, `test_onepassword.py`, `test_proxmox.py` --
  convert-to-YAML (each declares its capability's resource in TOML).

## Shared fixtures to fix first (they fan out to many files above)

- `tests/orchestrated_fixtures.py` -- writes resource-declaring TOML consumed by the orchestrated
  suites (agents / sessions / vms / workspaces).
- `tests/conftest.py` -- config-writing helpers used across the suite.
- Per-file `_write_config` helpers (e.g. `tests/test_doctor_env_and_secrets.py`) that inline
  `[vm_templates.default]` / `[admin.config]`.

# Nerf Tools v1 Manifest -- Implementation Plan

## Definition of Done

- `nerf validate` parses and validates v1 manifests, reports all errors, exits non-zero on failure
- `nerf generate --target bin` produces self-contained bash scripts for all three execution modes
  (template, passthrough, script)
- `nerf generate --target skills` produces rulesync SKILL.md files with updated documentation format
- `nerf generate --target claude-plugin` produces a complete Claude Code plugin with scripts, skills,
  and nerfctl grant tools
- All generated scripts include `# nerf:threat:read=<scope>` and `# nerf:threat:write=<scope>`
  metadata headers
- `nerfctl-grant-by-threat` allows/denies tools based on a 2D threat ceiling
- `nerfctl-grant-list` shows threat metadata alongside each permission entry
- All built-in manifests are migrated to v1 format with threat profiles
- v0 manifests are not supported (no compatibility layer)
- Tests pass with mypy strict and ruff clean

---

## Phase 1: Data Model (manifest.py)

Replace the v0 data model with v1 dataclasses. No CLI or generation changes yet -- this phase
establishes the foundation that everything else builds on.

- [ ] Add `ThreatLevel` enum (`none`, `workspace`, `machine`, `remote`, `admin`) with `__le__`
  comparison
- [ ] Add `ThreatSpec` dataclass (`read: ThreatLevel`, `write: ThreatLevel`)
- [ ] Add `SwitchSpec` dataclass (`description`, `flag`, `short`)
- [ ] Add `OptionSpec` dataclass (`description`, `flag`, `short`, `required`, `pattern`, `allow`,
  `deny`)
- [ ] Replace `FlagSpec` with `SwitchSpec` and `OptionSpec` throughout
- [ ] Update `ArgSpec`: remove `flag`/`positional` fields, keep `description`, `required`,
  `variadic`, `pattern`, `allow`, `deny`
- [ ] Add `TemplateSpec` dataclass (`command: list[str]`, `npm_pkgrun: bool`)
- [ ] Add `PassthroughSpec` dataclass (`command: str`, `deny: list[str]`, `prefix: list[str]`,
  `suffix: list[str]`)
- [ ] Update `ToolSpec`: add `threat: ThreatSpec`, `template: TemplateSpec | None`,
  `passthrough: PassthroughSpec | None`, `script: str | None`, `pre: str | None`,
  `switches: dict`, `options: dict`, `arguments: dict`; remove old `command`/`flags`/`args` fields
- [ ] Add `version: int` to `NerfManifest`
- [ ] Update `load_manifest()` to parse v1 format
  - Require `version: 1`
  - Parse `threat` on every tool
  - Parse execution mode (exactly one of template/passthrough/script)
  - Parse switches/options/arguments (reject for passthrough)
  - Parse `pre` field
- [ ] Update validation
  - Schema validation: required fields, types, enum values, mutual exclusions
  - Cross-reference: `{{param}}` refs match defined params, all params referenced in template,
    variadic is last, no params in passthrough
- [ ] Update `merge_manifests()` for new data model
- [ ] Update tests in `test_manifest.py`
  - v1 manifest loading (all three modes)
  - Threat level parsing and comparison
  - Validation errors (missing mode, multiple modes, params in passthrough, bad threat values)
  - `{{param}}` cross-reference validation

## Phase 2: Template Mode Script Generation (builder.py)

Update the builder for the v1 data model, starting with template mode (the existing generation path
adapted to the new types).

- [ ] Update `_emit_header()` to include `# nerf:threat:read=<scope>` and
  `# nerf:threat:write=<scope>` comment lines
- [ ] Refactor argument parsing generation for switches/options/arguments (replacing unified flags)
  - Switches: `case` branch with `shift 1`, set `VAR="true"`
  - Options: `case` branch with `shift 2`, set `VAR="$2"`
  - Arguments: positional collection after flag parsing stops
- [ ] Update placeholder substitution for the new parameter types (substitution table from HLA)
- [ ] Update `_emit_usage()` for separate Switches/Options/Arguments sections and "Maps to" line
- [ ] Implement `_emit_pre()`: wrap pre script in `_nerf_pre()` function, call with
  `_nerf_pre || _nerf_pre_rc=$?`, abort on non-zero
- [ ] Update error messages to structured format:
  `error: <tool-name>: <what>\n  <details>\n  hint: <action>`
- [ ] Update tests in `test_builder.py`
  - Template mode with switches, options, arguments
  - Pre-hook generation and abort behavior
  - Threat metadata in headers
  - Structured error messages
  - `bash -n` syntax validation on all generated scripts

## Phase 3: Passthrough and Script Mode Generation

Add the two new execution mode code paths to the builder.

- [ ] Implement `_emit_passthrough()`
  - Deny pattern array declaration
  - Token scan loop with `case` glob matching
  - Structured deny error message (tool name, rejected token, matched pattern, full deny list, hint)
  - `exec <command> <prefix...> "$@" <suffix...>`
- [ ] Implement `_emit_script()`
  - Inline script body after parameter parsing and guards
  - No `exec` -- script controls its own flow
- [ ] Update `build_script()` to dispatch: exactly one of `_emit_template`, `_emit_passthrough`,
  `_emit_script`
- [ ] Passthrough-specific usage generation (lists denied patterns, shows "Maps to" with `"$@"`)
- [ ] Script-specific usage generation (no "Maps to" line)
- [ ] Tests
  - Passthrough: deny matching, prefix/suffix, error messages
  - Script: inline body, parameter access, exit code
  - All three modes: `bash -n` syntax validation

## Phase 4: CLI Restructuring (cli.py)

Replace `build`/`skill` commands with `validate`/`generate --target`.

- [ ] Implement `nerf validate` command
  - Load and merge manifests
  - Report all validation errors (not just first)
  - Exit non-zero on any failure
  - No output files
- [ ] Implement `nerf generate` command
  - `--target` option (required, repeatable): `bin`, `skills`, `claude-plugin`
  - `--outdir`: override default output directory
  - `--no-default`: skip built-in catalog
  - `--keep-existing`: preserve unmanaged files
  - `--prefix`: tool name prefix (default: `nerf-`)
  - Positional `[manifests...]`
  - Dispatch to `builder.build_scripts()`, `skill.build_skills()`, or
    `formats.build_claude_plugin()` based on target
- [ ] Remove old `build` and `skill` commands
- [ ] Update `pyproject.toml` if entry point changes
- [ ] Tests for CLI
  - `validate` with valid and invalid manifests
  - `generate --target bin` produces scripts
  - `generate --target skills` produces skill files
  - Multiple `--target` flags in one invocation
  - Error on unknown target name

## Phase 5: Skill and Plugin Generation (skill.py, formats.py)

Update skill and plugin generators for the v1 documentation format.

- [ ] Update `skill.py`
  - Separate Switches/Options/Arguments sections in tool docs
  - "Maps to" line for template and passthrough modes
  - No "Maps to" for script mode
  - Passthrough: show denied patterns list
  - Threat metadata display (optional, for operator reference)
- [ ] Update `formats.py`
  - Same documentation changes as `skill.py` (using `${CLAUDE_PLUGIN_ROOT}` paths)
  - Threat metadata in generated script headers
  - Add `nerfctl-grant-by-threat` skill entry
  - Update `nerfctl-grant-list` skill entry (now shows threat annotations)
- [ ] Update overview skill for v1 terminology
- [ ] Tests in `test_skill.py` and `test_formats.py`
  - All three modes produce correct documentation
  - "Maps to" presence/absence by mode
  - Plugin structure includes new grant skill

## Phase 6: Threat-Based Grant System

Implement the new `grant-by-threat` command and enhance `grant-list`.

- [ ] Implement `find-tools` logic (shell function or inline)
  - Scan executable files under a root directory
  - Parse `# nerf:threat:read=` and `# nerf:threat:write=` from headers
  - Apply optional name filter glob
  - Output structured results (path, name, read, write)
- [ ] Implement `classify-by-threat` logic
  - Compare each tool's read/write against ceiling using `<=` on threat level ordering
  - Output classification (inside/outside) per tool
- [ ] Write `nerftools/nerftools/nerfctl/claude/grant-by-threat.sh`
  - Arguments: `<plugin-root> --read <level> --write <level> [--filter <glob>]
    [--outside deny|reset] [--settings-scope user|local]`
  - Run find-tools + classify-by-threat
  - Inside tools: add to allow, remove from deny
  - Outside tools: add to deny (default) or remove from both (`--outside reset`)
  - Print changes with `(was: allowed)` / `(was: denied)` annotations
- [ ] Enhance `nerftools/nerftools/nerfctl/claude/grant-list.sh`
  - Look up threat metadata for each listed tool using find-tools logic
  - Display `read:<scope>  write:<scope>` alongside each entry
  - Tools without metadata listed without annotation
- [ ] Register `grant-by-threat.sh` in `__init__.py` `NERFCTL_SCRIPTS`
- [ ] Write `nerfctl-grant-by-threat` skill template for Claude Code plugin
- [ ] Tests in `test_nerfctl.py`
  - find-tools: discovers tools, parses metadata, applies filter
  - classify-by-threat: correct inside/outside classification across threat levels
  - grant-by-threat: end-to-end with mock settings.json
  - grant-list: threat annotations displayed

## Phase 7: Migrate Built-in Manifests to v1

Rewrite all built-in manifests in v1 format. All existing tools become template mode (no existing
tool uses passthrough or script). Add threat profiles to every tool.

- [ ] Migrate `manifests/git/manifest.yaml`
  - Add `version: 1`
  - Add threat profiles: git-log (read:workspace, write:none), git-add/commit/pull
    (read:workspace, write:workspace), git-fetch (read:remote, write:workspace),
    git-push-main/branch (read:workspace, write:remote), git-tag (read:workspace, write:workspace)
  - Convert `flags` to `switches`/`options`, `args` to `arguments`
  - Convert `command` to `template.command`
  - Convert git-push-branch guard script to `pre` hook
- [ ] Migrate `manifests/az-repos/manifest.yaml`
  - Threat: pr-list/pr-show (read:remote, write:none), pr-create (read:remote, write:remote)
- [ ] Migrate `manifests/az-pipelines/manifest.yaml`
  - Threat: all three (read:remote, write:none)
- [ ] Migrate `manifests/az-wi/manifest.yaml`
  - Threat: wi-show/mywi-show (read:remote, write:none), wi-comment/mywi-comment (read:remote,
    write:remote)
- [ ] Migrate `manifests/nx/manifest.yaml`
  - Threat: show-projects/show-project/graph (read:workspace, write:none), run/affected/reset
    (read:workspace, write:workspace)
- [ ] Migrate `manifests/tg/manifest.yaml`
  - Threat: validate/fmt (read:workspace, write:none), init (read:remote, write:workspace),
    plan/output (read:remote, write:none), apply variants if added (read:remote, write:remote)
- [ ] Migrate `manifests/pkgrun/manifest.yaml`
  - Threat: cspell/markdownlint (read:workspace, write:none), prettier (read:workspace,
    write:workspace)
- [ ] Validate all migrated manifests with `nerf validate`
- [ ] Generate scripts and verify `bash -n` passes on all
- [ ] Diff generated scripts against v0 output to catch regressions in argument parsing behavior

## Phase 8: Integration Testing and Cleanup

End-to-end validation and cleanup.

- [ ] End-to-end test: `nerf validate` + `nerf generate --target bin --target skills` on all
  built-in manifests
- [ ] End-to-end test: `nerf generate --target claude-plugin` produces a valid plugin structure
- [ ] Verify grant-by-threat works against generated plugin scripts (threat metadata discovery)
- [ ] Run full test suite with mypy strict and ruff clean
- [ ] Remove any dead code from v0 model (old `FlagSpec`, old `build`/`skill` CLI commands)
- [ ] Update `pyproject.toml` version if needed
- [ ] Update agentworks CLI integration if nerf CLI entry points changed

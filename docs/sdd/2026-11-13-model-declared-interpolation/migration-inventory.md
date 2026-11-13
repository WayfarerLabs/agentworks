# Proposed field adoption inventory

Snapshot: `3ad615680ee8e1ef56c864170deaf62392578661`, 2026-11-13. Core and all 10 shipped system
plugins were inspected through their real consumers. This is a recommendation for operator
discussion; new opt-ins are not yet approved or implemented. Line anchors below refer to this
snapshot. Paths are relative to `cli/agentworks/`.

## Existing support to retain

These six field declarations across five models already interpolate. Moving their eligibility into
annotations preserves behavior rather than adding new support.

| Declaration                                                 | Current consumer                                                            | Available context                                   |
| ----------------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------------------- |
| `env/entry.py:44`, `PlaintextEnvEntry.value`                | `env/render.py:43`, used by compose, offline show and pre-secret validation | Consuming invocation; fields depend on scope        |
| `capabilities/harness_integration/shell.py:61`, `command`   | Same file, `_render_selected_command` at line 152                           | Bound session, including planned facts in preflight |
| Same model, `resume_command` at line 68                     | Same selected-field renderer                                                | Same; unselected field stays unrendered             |
| `plugins/claude/harness_integration.py:152`, `extra_args[]` | Same file, line 406; quoting at line 495                                    | Bound session                                       |
| `plugins/codex/harness_integration.py:248`, `extra_args[]`  | Same file, line 639; quoting at line 1088                                   | Bound session                                       |
| `plugins/grok/harness_integration.py:123`, `extra_args[]`   | Same file, line 292; quoting at line 368                                    | Bound session                                       |

## Recommended new opt-ins

Recommend the following eleven additional declarations, grouped by consumer contract. All use the
existing context catalogue. The examples describe proposed support.

### A. Install commands and their completion/profile fields

Shared declaration: `_InstallCommandEntry` in `install_commands.py`, inherited by both system and
user install-command resource kinds. These five fields are one coherent installer contract, reused
by the shipped install-command plugin.

| Field                  | Example                                               | Interpretation                 |
| ---------------------- | ----------------------------------------------------- | ------------------------------ |
| `command` at line 63   | `tool install --prefix '${agw:user_home_dir}/.local'` | Shell source                   |
| `path[]` at line 68    | `${agw:user_home_dir}/.tool/bin`                      | Target-user profile PATH entry |
| `test_exec` at line 75 | `${agw:user_home_dir}/.tool/bin/tool`                 | `command -v` operand           |
| `test_file` at line 80 | `${agw:user_home_dir}/.tool/installed`                | File-test path                 |
| `test_dir` at line 84  | `${agw:user_home_dir}/.tool`                          | Directory-test path            |

Admin/system consumption is in `vms/initializer/packages.py:232-308`; agent consumption is in
`agents/initializer.py:476-500,752-783`. VM and consuming Linux identity are known; agent setup also
knows `agent_name`. These installs have no workspace or session context. VM setup must thread
existing identity facts into the runner; agent setup already builds a `ResourceContext` at
`agents/initializer.py:240`.

Render completion inputs before checking them and the command before execution. PATH entries still
apply when installation is skipped, so their rendering cannot depend on whether the command runs.
Preserve existing tilde and downstream shell behavior.

Cost: context threading and three destination kinds, not five new parsing algorithms. Profile
writers currently interpolate authored PATH fragments into double-quoted shell source
(`vms/initializer/shell_env.py:139`, `agents/initializer.py:567`). Adoption must keep existing
intentional `~`/`$HOME` behavior while treating inserted context data correctly. Specify that
serialization in the implementation design rather than assuming PATH values are already safely
quoted.

### B. Admin and agent dotfiles installation

Four declarations: `dotfiles_destination` and `dotfiles_install_cmd` in both `vms/admin.py:122-125`
and `agents/template.py:122-125`.

Examples: `${agw:user_home_dir}/.config/dotfiles`, and an agent-only install command
`./install.sh --profile '${agw:agent_name}'`.

Actual consumers are `vms/initializer/driver.py:739-760` and `agents/initializer.py:309-369`. Render
effective inherited values when dotfiles setup is selected. Target user/VM facts are available; only
agent setup has `agent_name`. `dotfiles_source` remains a separate, literal source reference.

Cost: bind the existing setup identity, retain current tilde behavior and quote the destination as
path data. The admin consumer currently uses an unquoted `cd {dest}`; the migration must correct
that local construction so interpolated paths containing spaces or apostrophes work. Command
substitution itself does not escape shell source.

### C. Codex writable directories

One declaration: `CodexConfig.writable_dirs[]`, `plugins/codex/harness_integration.py:228`. Example:
`${agw:user_home_dir}/.cache/shared-tools`.

The list currently reaches `--add-dir` literally at line 1197 and is argv-quoted at line 1082.
Session context is already bound for extra arguments. Render before artifact planning at lines
639-642 and reuse the resulting list for flag generation.

Cost: a small additional native argv consumer. Preserve authored append/deduplicate merge policy,
order and normal argv quoting. Do not introduce tilde expansion, path normalization or compatibility
aliases. Diagnostics must name `writable_dirs`, not the current helper's hardcoded `extra_args`
field.

### D. Shell required commands

One declaration: `ShellConfig.required_commands[]`, `capabilities/harness_integration/shell.py:73`.
Example: `${agw:user_home_dir}/.local/bin/tool`, matching a home-relative command declaration.

The list is currently passed literally by `_probe_target` at line 189 to `require_commands` in
`capabilities/harness_integration/base.py:129`. Its downstream check uses a quoted `command -v`
operand, not arbitrary command source. Bound session context already exists, including before a new
target's availability probe can run.

Cost: render and validate against planned context during Shell preflight, then use consistent values
for readiness probes. Keep command availability checking separate from namespace rendering; a
nonexistent planned target still defers the remote probe. No automatic validation of the actual
workload command is introduced.

## Useful candidate needing a choice

`NativeUserConfig.marketplaces[]` in `plugins/_harness_native/native_config.py:23`, shared by Claude
and Codex user setup, can name a target-user directory such as
`${agw:user_home_dir}/src/team-marketplace`. Actual user/VM context is known through
`UserSetupInvocation`; workspace and session are unavailable.

This is less urgent because native discovery already expands `~/` against the actual user and
resolves existing relative paths there (`native_cli.py:416-424`). Adoption must render once before
discovery/source comparison (`native.py:309,350-367`) and reuse the result for ownership planning
and registration (`native.py:606`). Re-rendering only at command execution could make desired source
identity disagree with reconciliation. Recommendation: defer unless consistent namespace support for
these source strings is worth that additional lifecycle work now.

## Deliberate holds

| Fields                                                                               | Reason to keep their present contract in this pass                                                                                                                                         |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Workspace `repo`, `git_user_name`, `git_user_email` (`workspaces/template.py:69-80`) | Repository URL patterns could be useful, but Git identity is actor-independent. Substituting the provisioning admin would surprise later agent users. Requires a separate policy choice.   |
| APT `source` / `sources` (`apt.py:83-93`)                                            | Existing `{arch}` substitution discovers remote architecture. Unification would add vocabulary, discovery rules and a compatibility window beyond this effort.                             |
| `PathsConfig.vm_workspaces` (`config/models.py:59`)                                  | Shared VM root, validated outside `/home`; persistent workspace paths and rehome semantics make target-user interpolation inappropriate.                                                   |
| MCP executable, arguments, env, URL and headers (`artifacts/mcp.py:61-127`)          | Current validators explicitly reject unresolved expression syntax except narrow downstream same-name references. Changing this alters artifact validation and native projection semantics. |

## Keep literal

- Workstation and Git source references: admin/agent `dotfiles_source`, `mise_lockfile`, artifact
  `source`, and native settings mapping `source`. Local `~` refers to the invoking workstation in
  `sources.py:180-188` and `package_sources.py:161-177`.
- Imported JSON/TOML settings, artifact prose, prompts, instructions, goals, model/profile selectors
  and native plugin identifiers. They have existing opaque or native-owned contracts, not proven
  target-path roles.
- Resource names/references, credentials, secret names/contents, 1Password lookup URIs, cloud
  infrastructure identifiers, endpoint selectors, package names, versions and enums.
- Controller SSH, backup and VS Code paths. These already use workstation path semantics and have no
  consuming target user at config load.

## Decision requested

Confirm or adjust groups A-D. Decide whether marketplace sources join this pass or stay deferred.
The other holds are not proposed additions. Shipped manifests need no wholesale rewrite merely to
adopt the annotations; ordinary shell variables and useful tilde paths remain valid.

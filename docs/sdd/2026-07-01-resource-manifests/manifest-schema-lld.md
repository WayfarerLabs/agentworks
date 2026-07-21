# Phase 2 LLD: manifest schema, loader, and decode

Pins the envelope grammar, the per-kind spec schemas (exact parity with today's TOML parser,
verified by a full loader survey), the unknown-key strictness per kind, the error framing, and the
YAML library decision. The kind/field mapping defined here is the single shared table the loader
(Phase 2) and the migrator (Phase 4) both consume.

## YAML library

**PyYAML 6.0.3** (latest stable on PyPI as of 2026-07-03, verified via the uv resolver), safe
loading only. Mark plumbing: the loader parses each file as a stream with `yaml.compose_all`
(SafeLoader), which yields one root node per document carrying `start_mark.line` (0-based; +1 for
operator display); values are then constructed from the composed node with the safe constructor.
This gives per-document `SourceLocation(file, line)` without a custom parser. `ruamel.yaml` remains
the documented fallback if constructing-from-node proves awkward in practice; nothing else in this
LLD depends on the library choice.

## Envelope grammar

One YAML document per resource:

```yaml
apiVersion: agentworks/v1
kind: <registry kind identifier, lower-kebab>
metadata:
  name: <resource name>
  description: <optional operator note>
spec: <kind-specific fields>
```

Envelope rules (all violations are `ConfigError` with `file:line` of the document):

- `apiVersion` (required): exactly `agentworks/v1`.
- `kind` (required): must be a registered kind with `category = "declarable"` (the flag was
  `manifest_declarable` until the 2026-07-07 category revision). Unknown kinds error listing the
  valid kinds; descriptor kinds (`git-credential-provider`, and post-Phase-5.5 `secret-backend` --
  the capability took the name when the declarable layer was collapsed, 2026-07-07) error with
  "provided by the app".
- `metadata` (required mapping): `name` (required string), `description` (optional string). Other
  keys under `metadata` are errors (reserved for future `labels` / `annotations`).
- `spec` (required mapping, may be empty `{}`).
- Unknown top-level keys are errors (the envelope is new surface; strict from day one).
- Documents must be mappings; empty documents are skipped; non-mapping documents error.
- Duplicate mapping keys within a document are errors (tomllib parity; PyYAML's default
  last-write-wins would be a strictness loosening on the new surface). YAML merge keys (`<<`) are
  rejected with a clear error: dropped by YAML 1.2, and manifests stay literal (Kubernetes
  precedent). An explicit `spec:` with a null value is treated as an empty mapping; only a MISSING
  `spec` key errors.
- `metadata.description` is framework-uniform: every declarable kind carries a `description` field
  and stores it (the earlier transitional warn-and-ignore for template-shaped kinds without the
  field is retired now that all kinds have grown it).
- `named-console-template` accepts only `metadata.name: default` (no command selects a named console
  yet; `admin-template` graduated out once `vm create --admin-template` shipped).

`metadata.description` maps to the kind's `description` field on every declarable kind; a
`description` key inside `spec` is rejected with an error pointing at `metadata.description` (it
moved to metadata). For `secret`, `metadata.description` is REQUIRED (parity: the TOML loader
hard-requires it).

**Name validation parity**: `metadata.name` is validated with `validate_name` (NAME_RE, max 30) only
for `secret`, exactly as the TOML loader does today. Other kinds accept names as-is (the TOML loader
applies no name validation to them). Tightening this uniformly is a candidate follow-up AFTER the
migration equivalence window, not during it.

## Warnings channel

`load_manifests` returns `ManifestSet` carrying `issues: tuple[str, ...]`, mirroring
`Config.config_issues`: spec-level unknown-key warnings land here, surfaced at load (same
`warn_issues` behavior) and available to doctor. Envelope-level violations are errors, never
warnings.

## Per-kind spec schemas (parity-pinned)

As-built, the shared mapping is leaner than the field-table module originally sketched here: the
decoders in `agentworks/manifests/decode.py` do not carry per-field tables at all. Each one
reassembles the shape the corresponding TOML loader consumes and calls THAT loader through a
fixed-location `decls` shim, so every type check, enum, env rule, and unknown-key warning is shared
verbatim (zero drift by construction). The migrator's shared table is `decode.KIND_SECTIONS` (kind
identifier to legacy TOML section name); the migrator's field-level correctness comes from
round-tripping its emitted manifests through `load_manifests` itself rather than from a parallel
schema table.

Unknown-key modes (pinned from the loader survey; "warn" = allowlist diff appended to issues,
"silent" = extra keys ignored):

| Kind                     | TOML section                       | Unknown spec keys                  |
| ------------------------ | ---------------------------------- | ---------------------------------- |
| `secret`                 | `[secrets.<name>]`                 | warn                               |
| `vm-template`            | `[vm_templates.<name>]`            | warn                               |
| `agent-template`         | `[agent_templates.<name>]`         | warn                               |
| `workspace-template`     | `[workspace_templates.<name>]`     | silent (parity; TOML has no check) |
| `session-template`       | `[session_templates.<name>]`       | warn                               |
| `git-credential`         | `[git_credentials.<name>]`         | silent (parity)                    |
| `admin-template`         | `[admin.config]` + `[admin.env]`   | warn                               |
| `named-console-template` | `[named_console]`                  | warn                               |
| `secret-backend`         | `[secret_backends.<kind>]`         | silent (post-5.5: not declarable)  |
| `apt-source`             | `[apt_sources.<name>]`             | silent (parity)                    |
| `apt-package`            | `[apt_packages.<name>]`            | silent (parity)                    |
| `system-install-command` | `[system_install_commands.<name>]` | silent, but `test` errors (parity) |
| `user-install-command`   | `[user_install_commands.<name>]`   | silent, but `test` errors (parity) |

Spec fields per kind are exactly today's TOML fields (survey pinned; highlights and deltas only):

- **secret**: `hint` (optional str), `backend_mappings` (map of backend name to str | map | `false`;
  literal `true` rejected). `description` moves to metadata (required).
- **vm-template**: `inherits`, `cpus`, `memory`, `disk`, `azure_vm_size`, `swap`, `apt`,
  `apt_packages`, `snap`, `system_install_commands`, `tailscale_auth_key` (non-empty bare secret
  name), `env` (nested map; entries are plaintext strings or `{secret: <name>}`; key regex
  `^[A-Za-z_][A-Za-z0-9_]*$`; `AGENTWORKS_*` keys and embedded newlines warn).
- **agent-template**: `inherits`, `shell`, `git_credentials`, `user_install_commands`, `dotfiles_*`
  (3), `mise_*` (6), `claude_marketplaces`, `claude_plugins`, `env`.
- **workspace-template**: `inherits`, `repo`, `tmuxinator`, `env`.
- **session-template**: `inherits`, `command`, `restart_command`, `required_commands`, `env`.
  `description` moves to metadata.
- **git-credential**: `provider` (required; decode maps it onto the existing `type` field -- the
  dataclass keeps that name until the TOML resource path retires), `org` (required when
  `provider: azdo`), `token` (optional bare secret name, default `git-token-<name>`, empty
  rejected). `description` moves to metadata. Manifests never accept `type`; the TOML alias game is
  Phase 3's concern.
- **admin-template**: flat spec = the `[admin.config]` field set (`username`, `shell`,
  `git_credentials`, `user_install_commands`, dotfiles, mise, `git_force_safe_directory`, claude
  fields) plus `env` (was `[admin.env]`). Name restricted to `default`.
- **named-console-template**: `tmux_layout` (enum `VALID_TMUX_LAYOUTS`). Name restricted to
  `default`.
- **secret-backend**: not manifest-declarable -- the kind is the capability descriptor, and
  declaring it gets R3's permanent capability-kind error. (History in the plan's 2026-07-07
  sequencing note: Phase 2 deferred it, Phase 3 shipped a declarable shape, Phase 5.5 removed it.)
- **catalog kinds**: fields exactly per the survey (`key_url`/`key_path`/`source`/`source_file` (+
  `_SAFE_FILENAME_RE`) / `key_dearmor`; `apt` required list + `apt_sources`; `command` + `path` +
  at-most-one of `test_exec`/`test_file`/`test_dir`).

## Error catalog (framing)

All loader errors are `ConfigError` with the document location prefix:

- `resources/vm-templates.yaml:12: apiVersion must be "agentworks/v1"; got "v2"`
- `resources/foo.yaml:1: unknown kind "vm_template"; valid kinds: agent-template, apt-package, ...`
  (misspelled-snake case gets the kebab suggestion when the kebab form exists)
- `resources/foo.yaml:8: secret-backend is not manifest-declarable` (the permanent capability-kind
  error: provided by the app, post-5.5)
- `resources/a.yaml:3: duplicate secret "npm-token" (also declared at resources/b.yaml:9)`
- Spec-level field errors reuse the existing validation messages with the location prefix
  substituted for the TOML path prefix.

## Duplicate detection and ordering

- Walk: `*.yaml` + `*.yml` under the resources directory, recursively; per directory, files sort by
  name and precede subdirectories. Dot-prefixed files and directories are skipped (pruned without
  descent); documents load in file order. This order IS config-load order for the framework.
- Duplicate `(kind, name)` across the manifest set errors citing both `file:line` locations.
- Cross-source duplicates (manifest vs TOML under the permanent dual-path model, manifest vs
  built-in rows) are `Registry.add`'s job per the HLA collision rules, not the loader's.

## Built-in manifests

`agentworks/manifests/builtin/` ships inside the package; `builtin.py` discovers `*.yaml` there via
`importlib.resources`, parses with the same loader, and publishes with
`Origin.built_in(source="agentworks.manifests.builtin/<filename>")`. Warn-level issues in bundled
manifests are app bugs and assert (CI catches a dirty bundle when content is added). Phase 2 ships
the mechanism wired with an empty bundle; Phase 3 added `secret-backends.yaml`, and the Phase 5.5
capability collapse (2026-07-07) removed it again -- the bundle is wired and empty, its loader path
kept exercised by tests, with plugins and future built-ins as its consumers.

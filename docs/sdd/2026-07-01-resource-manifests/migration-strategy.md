# Resource manifests: migration strategy

How an existing agentworks install moves from TOML-declared resources to YAML manifests. The
operator-facing summary lives in the FRD's "Migration notes"; this document covers the mechanics.

Revised 2026-07-03 for the dual-path decision (FRD R11): migration is OPTIONAL and
operator-scheduled. TOML resource sections stay fully supported with deprecation warnings; the
migration tool is a convenience, not a gate. Revised again 2026-07-05: the tool is
`agw resource migrate`, a recurring incremental mover (selectors, layouts, append-only YAML,
comment-or-delete TOML, per-run registry-equivalence verification) -- see `migration-tool-lld.md`.

## Current state (snapshot, 2026-07-01)

One file, `~/.config/agentworks/config.toml`, holds both config and resources. The resource surface
as of the resource-registry SDD (locked 2026-06-30):

- Multi-named kinds: `secrets`, `git_credentials`, `vm_templates`, `agent_templates`,
  `workspace_templates`, `session_templates`, `apt_sources`, `apt_packages`,
  `system_install_commands`, `user_install_commands`, `secret_backends`.
- Singleton-shaped kinds: `admin` (as `[admin.config]` / `[admin.env]` / sub-sections),
  `named_console`.
- Config sections staying put: `operator`, `paths`, `defaults`, `azure`, `proxmox`,
  `session.config`, `secret_config`.

TOML forces one conceptual resource across multiple sections (`[x]` + `[x.env]`); the loader's
composition step reassembles them. Resources and config interleave freely in one file.

## Target state

- `config.toml`: config sections only (the recommended, docs-leading layout; TOML resource sections
  keep working with deprecation warnings for operators who haven't migrated).
- `~/.config/agentworks/resources/**/*.yaml`: one document per resource, `apiVersion: agentworks/v1`
  envelope, auto-loaded.
- Built-in `env-var` / `prompt` secret backends ship with the app; `[secret_backends.*]`
  declarations have no successor (they were semantically empty and are warned no-ops today; the
  migrator drops them with a note).

### Before / after example

```toml
[secrets.openai-api-key]
description = "OpenAI API key"
backend_mappings.env-var = "OPENAI_API_KEY"

[session_templates.claude]
inherits = ["default"]
command = "claude --name {{session_name}}"
description = "Claude Code interactive session"

[session_templates.claude.env]
CLAUDE_LOG_LEVEL = "info"

[git_credentials.github]
type = "github"
description = "GitHub full access"
```

becomes `resources/secrets.yaml`:

```yaml
apiVersion: agentworks/v1
kind: secret
metadata:
  name: openai-api-key
  description: OpenAI API key
spec:
  backend_mappings:
    env-var: OPENAI_API_KEY
```

`resources/session-templates.yaml`:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: claude
  description: Claude Code interactive session
spec:
  inherits: [default]
  command: claude --name {{session_name}}
  env:
    CLAUDE_LOG_LEVEL: info
```

`resources/git-credentials.yaml` (note `type` becomes `provider`):

```yaml
apiVersion: agentworks/v1
kind: git-credential
metadata:
  name: github
  description: GitHub full access
spec:
  provider: github
```

## Transition mechanics

**In the repo**: phases 0 and 1 are behavior-preserving refactors. Phase 2 onward, the manifest path
runs alongside the fully supported TOML path -- permanently (dual-path, revised 2026-07-03): a
resource may come from either source, declaring the same one in both errors at publish (the
`Registry.add` collision handling introduced in Phase 2), and TOML resource semantics stay today's,
so any config that loads today keeps loading -- with one deliberate exception (FRD R13, 2026-07-05):
resource names containing `/` are now rejected at publish, since `/` is reserved for selectors and
per-resource filenames. The exception is `[secret_backends.*]`, which Phase 3.6 made a warned no-op
(the sections were semantically empty; post-5.5 the kind is a capability descriptor). Phase 5 adds
per-section deprecation warnings and repoints the docs to lead with YAML; the TOML resource path's
removal waits for an unscheduled future major (Phase 6).

**For an operator**, migration is optional and self-scheduled:

1. Upgrade agentworks. Everything keeps working; TOML resource sections warn as deprecated
   (per-section warnings arrive in Phase 5; today only `[secret_backends.*]` warns).
2. When ready, run `agw resource migrate` (Phase 4) -- all at once, or incrementally
   (`agw resource migrate secret`, then templates next month; selectors scope each run). The tool
   previews, backs up `config.toml` before writing anything, writes or appends manifest files per
   the chosen `--layout`, comments out (default) or deletes the migrated TOML sections, applies the
   `type` to `provider` rename, and drops `[secret_backends.<kind>]` sections with a note.
3. Done -- and proven: every real run rebuilds the registry and verifies it row-for-row identical to
   the pre-migration one (rolling back on mismatch), so "no behavior change" is checked on the
   operator's actual config, not just in the repo's golden test.

Fresh installs learn YAML first: `agw config init` produces the config-only TOML and
`agw resource sample` provides the envelope teaching surface (samples ship in Phase 4; docs lead
with them in Phase 5).

## Worked example: an operator with custom env var names

Pre-migration, an operator who kept legacy env var names had per-secret overrides:

```toml
[secrets.npm-token]
description = "npm registry token"
backend_mappings.env-var = "NPM_TOKEN"
```

Migration carries this over verbatim; per-secret `backend_mappings` remain the mechanism for
customizing identifiers (the built-in backends accept no configuration). Post-5.5 there is no
declarable backend kind at all; a future config-bearing backend's configuration is backend-scoped,
graduating to a declarable instance kind only on a real multi-instance need (FRD R8). Nothing about
this migration needs any of that.

## Risks and safeguards

- **Silent semantic drift between parsers.** The migrator reads with the legacy parser and emits
  through the same kind/field mapping the manifest loader consumes, and Phase 4's definition of done
  compares finalized registries before and after on a maximal config. Any drift fails the golden
  test, not the operator.
- **Comment loss in `config.toml`.** Mode-dependent: under the default `--toml comment`, operator
  comments inside migrated sections survive in place (commented out with the section); under
  `--toml delete` they go with the sections. In neither mode are comments transplanted into the
  YAML. Comments on surviving config sections are preserved via tomlkit round-trip regardless. The
  preview shows exactly what changes, and the backup keeps the original.
- **Partially-applied migration** (tool interrupted): the backup is taken before anything is
  written, manifests are written before the TOML rewrite, and the TOML rewrite is atomic
  (write-new-then-rename). Worst case is manifests present plus the original config -- which, under
  dual-path, fails loudly and safely at the NEXT load as cross-source duplicate errors citing both
  locations (never silent double-definition). Recovery is manual and mechanical: restore
  `config.toml` from the backup and delete the YAML documents the preview listed, or hand-finish the
  TOML edit. The tool itself refuses to run on a broken config -- an auto-resume mode was considered
  and rejected as YAGNI for a crash window this small -- so "re-run the tool" is NOT the recovery
  path.
- **Migrator drift on THIS operator's config**: beyond the repo's golden test, every real run
  verifies registry equivalence on the config it just migrated and rolls back (backup restore,
  created-file removal, append truncation) on mismatch. A migrator bug cannot leave an operator with
  a silently-different registry.
- **Third-party tooling reading `config.toml` for resource sections**: none known; release notes
  call out the move regardless.

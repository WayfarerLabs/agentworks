# Resource manifests: migration strategy

How an existing agentworks install moves from TOML-declared resources to YAML manifests. The
operator-facing summary lives in the FRD's "Migration notes"; this document covers the mechanics.

Revised 2026-07-03 for the dual-path decision (FRD R11): migration is OPTIONAL and
operator-scheduled. TOML resource sections stay fully supported with deprecation warnings; the
migration tool is a convenience, not a gate.

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
so any config that loads today keeps loading. The exception is `[secret_backends.*]`, which Phase
3.6 made a warned no-op (the sections were semantically empty; the built-in backends ship bundled).
Phase 5 adds per-section deprecation warnings and repoints the docs to lead with YAML; the TOML
resource path's removal waits for an unscheduled future major (Phase 6).

**For an operator**, migration is optional and self-scheduled:

1. Upgrade agentworks. Everything keeps working; TOML resource sections warn as deprecated
   (per-section warnings arrive in Phase 5; today only `[secret_backends.*]` warns).
2. When ready, run `agw config migrate` (Phase 4). The tool previews, backs up `config.toml`, writes
   by-kind manifest files, rewrites `config.toml` without the resource sections (comments on
   surviving sections preserved), applies the `type` to `provider` rename, and drops
   `[secret_backends.<kind>]` sections with a note.
3. Done. No behavior change: the finalized registry from the migrated layout is identical to the
   pre-migration one (this equivalence is a Phase 4 test).

Fresh installs learn YAML first: `agw config init` produces the config-only TOML and the sample
manifests document the envelope (Phase 5).

## Worked example: an operator with custom env var names

Pre-migration, an operator who kept legacy env var names had per-secret overrides:

```toml
[secrets.npm-token]
description = "npm registry token"
backend_mappings.env-var = "NPM_TOKEN"
```

Migration carries this over verbatim; per-secret `backend_mappings` remain the mechanism for
customizing identifiers (the built-in providers accept no per-backend configuration).
Operator-declared backends earn their keep when the first config-bearing provider (a future
`onepassword`, say) lands; nothing about this migration needs them.

## Risks and safeguards

- **Silent semantic drift between parsers.** The migrator reads with the legacy parser and emits
  through the same kind/field mapping the manifest loader consumes, and Phase 4's definition of done
  compares finalized registries before and after on a maximal config. Any drift fails the golden
  test, not the operator.
- **Comment loss in `config.toml`.** Comments inside migrated resource sections necessarily go with
  the sections (their content now lives in YAML; the tool does not attempt comment transplantation).
  Comments on surviving config sections are preserved via tomlkit round-trip. The preview shows
  exactly what is removed, and the backup keeps the original.
- **Partially-applied migration** (tool interrupted): manifests are written before the TOML rewrite,
  and the TOML rewrite is atomic (write-new-then-rename). Worst case is manifests present plus the
  original config -- which, under dual-path, fails loudly and safely at the NEXT load as
  cross-source duplicate errors citing both locations (never silent double-definition); re-running
  the tool (or deleting one side) resolves it. The backup makes every state recoverable.
- **Third-party tooling reading `config.toml` for resource sections**: none known; release notes
  call out the move regardless.

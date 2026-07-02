# Resource manifests: migration strategy

How an existing agentworks install moves from TOML-declared resources to YAML manifests. The
operator-facing summary lives in the FRD's "Migration notes"; this document covers the mechanics.

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

- `config.toml`: config sections only.
- `~/.config/agentworks/resources/**/*.yaml`: one document per resource, `apiVersion: agentworks/v1`
  envelope, auto-loaded.
- Built-in `env-var` / `prompt` secret backends ship with the app; explicit empty
  `[secret_backends.*]` declarations have no successor (they are simply covered).

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
kind: session_template
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
kind: git_credential
metadata:
  name: github
  description: GitHub full access
spec:
  provider: github
```

## Transition mechanics

**In the repo**: phases 0 and 1 are behavior-preserving refactors. Phases 2 through 4 build the
manifest path alongside the still-working TOML path: a resource may come from either source,
declaring the same one in both errors at publish (the `Registry.add` collision handling introduced
in Phase 2), and TOML resource semantics stay exactly today's (the `provider` alias is additive;
legacy backend rows keep their existing construction path), so any config that loads today loads at
every intermediate phase. Phase 5 removes the TOML path. The dual-source condition exists between
merged phases for development, but never in a release: the cutover and the migration tool ship
together.

**For an operator**, the upgrade is:

1. Upgrade agentworks. Any resource section in `config.toml` now fails at load with an error naming
   the sections and the command to run.
2. Run `agw config migrate`. The tool previews, backs up `config.toml`, writes by-kind manifest
   files, rewrites `config.toml` without the resource sections (comments on surviving sections
   preserved), and applies the renames (`type` to `provider`; `[secret_backends.<kind>]` sections to
   `secret_backend` documents; empty `env-var` / `prompt` backend sections dropped).
3. Done. No behavior change: the finalized registry from the migrated layout is identical to the
   pre-upgrade one (this equivalence is a Phase 4 test).

Fresh installs never see TOML resources: `agw config init` produces the config-only TOML and the
sample manifests document the envelope.

## Worked example: an operator with a custom env var convention

Pre-migration, an operator who kept legacy env var names had per-secret overrides:

```toml
[secrets.npm-token]
description = "npm registry token"
backend_mappings.env-var = "NPM_TOKEN"
```

Migration carries this over verbatim (per-secret `backend_mappings` keep working). Post-migration
they can optionally consolidate with a custom backend:

```yaml
apiVersion: agentworks/v1
kind: secret_backend
metadata:
  name: bare-env
  description: Unprefixed environment variables
spec:
  provider: env-var
  prefix: ""
```

plus `backends = ["bare-env", "prompt"]` in `[secret_config]`. That consolidation is a manual
choice, not something the tool does.

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
  original config; in the released (post-cutover) system that state fails at load with the
  resource-sections-present error, which already points back at `agw config migrate`, and re-running
  the tool (or deleting one side) resolves it. The backup makes every state recoverable.
- **Operators on unreleased dual-source builds**: unsupported; the dual-source condition is a
  development state only.
- **Third-party tooling reading `config.toml` for resource sections**: none known; release notes
  call out the move regardless.

# Resources: YAML Manifests, TOML, and the Registry

How agentworks models the things you declare -- secrets, templates, git credentials, catalog entries
-- and how to work with them day to day.

## The split: config vs resources

`~/.config/agentworks/config.toml` is for **settings**: your identity (SSH keys), paths, CLI
defaults, platform connections (Azure, Proxmox), and the secret backend chain
(`[secret_config].backends`). Settings configure your install; they are not named, referenceable
entities.

**Resources** are the named things everything else refers to: a `secret` called `npm-token`, a
`vm-template` called `dev`, a `git-credential` called `github`. Every resource lives in the resource
registry, is identified by `kind` + `name`, and can be inspected uniformly:

```bash
agw resource list                       # everything, all kinds and origins
agw resource list --kind secret         # one kind
agw resource describe vm-template/dev   # one resource, with references and usage
```

Resources come from three origins: **operator-declared** (you wrote them, in YAML or TOML),
**built-in** (shipped with agentworks, e.g. the `env-var` and `prompt` secret backends and the tool
catalog), and **auto-declared** (the framework filled in a referenced-but-undeclared resource, e.g.
the `tailscale-auth-key` secret or `git-token-<name>` secrets).

## Declaring resources: YAML manifests

Declare resources as YAML files under `~/.config/agentworks/resources/` (next to `config.toml`).
Every `*.yaml` / `*.yml` file in that directory tree is loaded automatically whenever a command
needs resources -- there is no `apply` step and no persisted state to reconcile. File names and
layout are entirely your choice: one file per resource, one per kind, or one for everything all work
the same.

Each document uses a Kubernetes-style envelope:

```yaml
apiVersion: agentworks/v1
kind: secret
metadata:
  name: npm-token
  description: npm registry token
spec:
  backend_mappings:
    env-var: NPM_TOKEN
```

- `kind` is the lower-kebab resource kind (`secret`, `vm-template`, `session-template`,
  `git-credential`, `apt-package`, ...).
- `metadata` carries the framework-uniform fields: `name` (required; `/` is not allowed in resource
  names) and `description`. Two kinds are singletons: `admin-template` and `named-console-template`
  accept only `name: default`.
- `spec` carries the kind-specific fields -- the same fields, with the same validation, as the TOML
  sections (both sources decode through the same loaders, so they cannot drift).
- Multiple documents per file are separated with `---`.

`agw resource sample vm-template` prints a commented starter for one kind (`--all` for every kind);
`--write <file>` saves it under the resources directory instead. Samples are fully commented out --
delete one leading `#` per line to activate. `agw resource edit KIND/NAME` opens the manifest
declaring a resource in `$EDITOR` (YAML-declared resources only: TOML-declared ones point at
`agw resource migrate` or `agw config edit`).

## TOML resource sections: deprecated but supported

The classic TOML resource sections (`[secrets.*]`, `[vm_templates.*]`, `[git_credentials.*]`, ...)
keep working with exactly their historical semantics. Their presence emits one aggregated
deprecation warning naming the sections found (silence it with the global `--no-deprecations` flag),
and their removal waits for a future major release. You may mix sources freely -- some resources in
YAML, some in TOML -- but declaring the SAME resource in both is an error citing both locations.

Move resources over whenever you like:

```bash
agw resource migrate secret            # one kind
agw resource migrate vm-template/dev   # one resource
agw resource migrate --all             # everything (explicit opt-in)
agw resource migrate --all --dry-run   # see the plan first (--full for the diff)
```

The migrator is incremental and repeat-safe: output is append-only (your existing YAML files are
never rewritten), the original `config.toml` is backed up to `paths.backups` first, migrated
sections are commented out in place with a `# migrated to ...` marker (or removed with
`--toml delete`), and every real run finishes by rebuilding the registry and verifying it is
identical to the pre-migration one -- rolling back if not.

## Built-ins and overrides

Built-in resources ship with the app and appear in `agw resource list --origin builtin`. Override
policy is per kind:

- **Catalog kinds** (`apt-source`, `apt-package`, `system-install-command`, `user-install-command`):
  declaring the same name overrides the built-in -- the name is the interface, and same-name
  override is how you customize what `gh` installs.
- **Secret backends** (`env-var`, `prompt`): registered capabilities, shown as read-only rows; they
  take no configuration. Customize per secret via `backend_mappings`.

## Secrets: backends and the chain

Two layers, one rule each:

- A **secret backend** is a capability resource: a read-only `secret-backend` row whose
  implementation is registered code (`env-var`, `prompt`; later `onepassword`, ...). You cannot
  declare one -- the app (and later plugins) registers them -- but they list and describe like every
  other resource. Per-secret behavior -- identifier overrides, structured store addressing like
  `{ vault = "Work", item = "npm" }`, and opt-outs -- lives in each secret's
  `backend_mappings.<backend>`.
- The **chain** is a setting: `[secret_config].backends` in `config.toml` lists the active backends
  in precedence order (default `["env-var", "prompt"]`). Registered backends absent from the chain
  are dormant.

Resolution is one pass over the chain per command: the first backend that produces a value wins, and
interactive prompts are asked at most once per command. `agw secret list` shows how each active
backend would look up each secret; `agw secret describe <name>` shows one secret in full;
`agw doctor` reports one row per secret with the runtime outcome.

## Inspecting the whole picture

```bash
agw resource list --origin operator     # what you have declared, either source
agw resource describe secret/npm-token  # where it's referenced, what uses it
agw doctor                              # health: would every secret resolve?
```

The design rationale (the config/resource/capability split, the vocabulary rules, and why dual
sources are permanent) is recorded in ADR 0016.

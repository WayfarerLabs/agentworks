# Resources: YAML Manifests, TOML, and the Registry

How agentworks models the things you declare -- secrets, templates, git credentials, catalog entries
-- and how to work with them day to day.

## The split: config vs resources

`~/.config/agentworks/config.toml` is for **settings**: your identity (SSH keys), paths, CLI
defaults, and the secret backend chain (`[secret_config].backends`). Settings configure your
install; they are not named, referenceable entities.

**Resources** are the named things everything else refers to: a `secret` called `npm-token`, a
`vm-template` called `dev`, a `git-credential` called `github`. Every resource lives in the resource
registry, is identified by `kind` + `name`, and can be inspected uniformly:

```bash
agw resource list                       # everything, all kinds and origins
agw resource list --kind secret         # one kind
agw resource describe vm-template/dev   # one resource, with references and usage
agw resource kinds                      # every kind: category, counts, purpose
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
  names) and `description`. Two kinds accept only `name: default` for now: `admin-template` and
  `named-console-template` are ordinary multi-instance kinds in the framework, but no command can
  select a named instance yet, so a named declaration would be dead config (issue #165 adds the
  selectors).
- `spec` carries the kind-specific fields -- the same fields, with the same validation, as the TOML
  sections (both sources decode through the same loaders, so they cannot drift).
- Multiple documents per file are separated with `---`.

`agw resource sample vm-template` prints a commented starter for one kind (`--all` for every kind);
`--write <file>` saves it under the resources directory instead. Samples are fully commented out --
delete one leading `#` per line to activate. `agw resource edit KIND/NAME` opens the manifest
declaring a resource in `$EDITOR` (YAML-declared resources only: TOML-declared ones point at
`agw resource migrate` or `agw config edit`).

## Scoped GitHub credentials (fine-grained PATs)

A `git-credential` with `provider: github` may carry a scope in its `provider_config`:
`repos: ["owner/name", ...]` pins the credential to specific repositories (always a list, even for
one, matching a fine-grained PAT's selected repos), while `owner: "org"` covers every repository
under that user or org, including repos an agent clones ad hoc that no workspace ever declared. The
two are mutually exclusive; a credential with neither is the unscoped fallback. Scopes are
manifest-only (the legacy flat TOML shape has no GitHub fields).

Selection lives in the agentworks credential helper: initialization sets `credential.useHttpPath`
(via the managed include `~/.agentworks-git-scopes.gitconfig`), so git hands the helper the remote's
host and repository path, and the helper picks the most specific credential: exact repo, then owner
(first path segment), then the provider's host default (`x-access-token` for GitHub, the org for
Azure DevOps), then the first stored line for the host. Two credentials claiming the same scope is a
configuration error at initialization time, evaluated per user (admin and each agent get their own
store, include, and helper, built from their own credential lists). Declaring a repo under one
credential and its org under another is fine: the more specific scope wins, and org scopes cover
repos cloned ad hoc that nothing declared.

Clone with plain https URLs; no username needed anywhere. Credentials are served by the
agentworks-owned helper (`~/.agentworks-git-cred-helper.sh`, replacing git's `credential-store`):
when the remote rejects a credential it prints which credential and secret to fix instead of
silently deleting the provisioned entry (which is what `credential-store` does on every failed
auth); an embedded username in a remote URL is reviewed per provider (GitHub flags it, since it
bypasses scoping; Azure DevOps accepts its org, which is both the username and the owner scope); and
if git stops sending repository paths (a local git config overriding `useHttpPath`), the helper
warns and serves the host default. The credential's resource name appears as the username on scoped
store lines and in provider-side logs; remotes are never rewritten.

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

## VM sites and platforms

Where VMs are created is declared as `vm-site` resources: "a configured place to create VMs". A site
pairs a **platform** (the capability: the code that runs VMs on one backend kind) with that
backend's configuration:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: azure-dev
spec:
  platform: azure-vm
  platform_config:
    subscription_id: "..."
    resource_group: agentworks-vms
    region: eastus2
```

- `spec.platform` names a `vm-platform` capability row (`lima`, `wsl2`, `azure-vm`, `proxmox`);
  `spec.platform_config` is validated by that platform (unknown keys are errors). Remote Lima is
  just a lima site with `platform_config.vm_host: user@host`.
- The `lima-local` and `wsl2` sites ship built in with empty config. Like every site they register
  on every host and disable themselves where this host lacks what they need (wsl2 is Windows-only; a
  local Lima site needs `limactl`); a disabled site still lists and describes with its reason, and
  using it is an error. Their names are reserved. A site named after a platform must declare that
  platform.
- Consumers name sites: `agw vm create --site`, `defaults.site` in config.toml, and each VM row's
  `site`. Templates deliberately carry no site: placement is per-host, never template state.
- Site config secrets ride the standard secret machinery: a Proxmox site references its API token as
  the `proxmox-token` secret (override with `token_secret`), auto-declared and resolved through the
  backend chain like any other.
- The legacy flat `[azure]` / `[proxmox]` TOML sections keep loading as deprecated vm-site
  declarations; `agw resource migrate vm-site` moves them to manifests.

## Built-ins and overrides

Built-in resources ship with the app and appear in `agw resource list --origin builtin`. Override
policy is per kind:

- **Catalog kinds** (`apt-source`, `apt-package`, `system-install-command`, `user-install-command`):
  declaring the same name overrides the built-in -- the name is the interface, and same-name
  override is how you customize what `gh` installs.
- **Bundled vm-sites** (`lima-local`, `wsl2`): reserved names. Redeclaring one is an error; declare
  a sibling site instead. Like every vm-site they register on every host and disable themselves
  where this host lacks what they need (`agw resource list` marks the row; `describe` and
  `agw doctor` carry the reason); using a disabled site is an error naming the requirement.
- **Secret backends** (`env-var`, `prompt`) and **VM platforms** (`lima`, `wsl2`, `azure-vm`,
  `proxmox`): registered capabilities, shown as read-only rows. You cannot declare or override them;
  secrets customize per secret via `backend_mappings`, platforms configure per site via
  `platform_config`. A platform whose host requirements are not met publishes no row at all:
  `agw doctor` lists installed-but-disabled platforms with the reason, and sites referencing one
  self-disable rather than erroring.

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

Resolution is a pass over the chain in precedence order: the first backend that produces a value
wins. You are never prompted for the same secret twice in one command, and all prompting happens up
front, before the command starts changing anything. `agw secret list` shows how each active backend
would look up each secret; `agw secret describe <name>` shows one secret in full; `agw doctor`
reports one row per secret with the runtime outcome.

## Inspecting the whole picture

```bash
agw resource list --origin operator     # what you have declared, either source
agw resource describe secret/npm-token  # where it's referenced, what uses it
agw doctor                              # health: would every secret resolve?
```

The design rationale (the config/resource split, capability kinds, the vocabulary rules, why dual
sources are permanent, and the vm-site / vm-platform pair) is recorded in ADR 0016.

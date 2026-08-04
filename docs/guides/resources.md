# Resources: YAML Manifests, TOML, and the Registry

How agentworks models the things you declare: secrets, templates, git credentials, apt /
install-command entries, and how to work with them day to day.

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

Resources come from four origins: **operator-declared** (you wrote them, in YAML or TOML),
**built-in** (shipped with agentworks, e.g. the `env-var` and `prompt` secret backends and the
built-in apt / install-command entries), **auto-declared** (the framework filled in a
referenced-but-undeclared resource, e.g. the `tailscale-auth-key` secret or `git-token-<name>`
secrets), and **system-plugin** (contributed by an installed, opted-in system plugin; see "System
plugins" below). Filter by origin with `agw resource list --origin operator|auto|builtin|plugin`.

## Declaring resources: YAML manifests

Declare resources as YAML files under `~/.config/agentworks/resources/` (next to `config.toml`).
Every `*.yaml` / `*.yml` file in that directory tree is loaded automatically whenever a command
needs resources: there is no `apply` step and no persisted state to reconcile. File names and layout
are entirely your choice: one file per resource, one per kind, or one for everything all work the
same.

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
  names) and `description` (stored and shown for every declarable kind). One kind accepts only
  `name: default` for now: `named-console-template` is an ordinary multi-instance kind in the
  framework, but no command can select a named instance yet, so a named declaration would be dead
  config (issue #165 adds the selector).
- `spec` carries the kind-specific fields: the same fields, with the same validation, as the TOML
  sections (both sources decode through the same loaders, so they cannot drift).
- Multiple documents per file are separated with `---`.

`agw resource sample vm-template` prints a commented starter for one kind (`--all` for every kind);
`--write <file>` saves it under the resources directory instead. Samples are fully commented out --
delete one leading `#` per line to activate. `agw resource edit KIND/NAME` opens the manifest
declaring a resource in `$EDITOR` (YAML-declared resources only: TOML-declared ones point at
`agw resource migrate` or `agw config edit`).

## Scoped GitHub credentials (fine-grained PATs)

A `git-credential`'s `spec.provider` is one tagged table: its `name` key selects the provider
capability (`github`, or `azdo` from the `azure` plugin) and the remaining keys are that provider's
configuration. (The old sibling shape, a `provider:` string plus a `provider_config:` table, still
loads unchanged but is deprecated and will be removed; fold the pair into the tagged table.) A
github credential may carry a scope there: `repos: ["owner/name", ...]` pins the credential to
specific repositories (always a list, even for one, matching a fine-grained PAT's selected repos),
while `owner: "org"` covers every repository under that user or org, including repos an agent clones
ad hoc that no workspace ever declared. The two are mutually exclusive; a credential with neither is
the unscoped fallback. Scopes are manifest-only (the legacy flat TOML shape has no GitHub fields).

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
keep working with exactly their historical semantics for now, but declaring resources in
`config.toml` is deprecated and will be removed in a future release. Their presence emits one
aggregated deprecation warning per command/request naming the sections found (silence it with the
global `--no-deprecations` flag). You may mix sources freely (some resources in YAML, some in TOML),
but declaring the SAME resource in both is an error citing both locations.

Move resources over whenever you like:

```bash
agw resource migrate secret            # one kind
agw resource migrate vm-template/dev   # one resource
agw resource migrate --all             # everything (explicit opt-in)
agw resource migrate --all --dry-run   # see the plan first (--full for the diff)
```

The migrator handles two paths. TOML-declared resources become new YAML documents, appended without
rewriting existing YAML content; migrated TOML sections are commented out in place with a
`# migrated to ...` marker (or removed with `--toml delete`). It also canonicalizes selected
existing `session-template` YAML documents that use the legacy `harness` selector, folding it and an
optional `harness_config` sibling into `harness_integration` while preserving the document stream
and YAML comments. The same kind or `kind/name` selectors scope both paths.

Every real run backs up `config.toml`; a run that modifies an existing YAML file also stores its
original as a recovery copy under `paths.backups`. Digest guards refuse to replace files changed
after planning, writes are atomic, and rollback restores only outputs that still match the run's
digest, so concurrent edits are not overwritten. Finally, the command rebuilds the registry and
verifies it is identical to the pre-migration registry, rolling back on a mismatch and reporting any
recovery copy needed for manual repair. Use `--dry-run --full` to inspect generated documents,
in-place YAML diffs, and the TOML diff before writing.

## VM sites and platforms

Where VMs are created is declared as `vm-site` resources: "a configured place to create VMs". A site
pairs a **platform** (the capability: the code that runs VMs on one backend kind) with that
backend's configuration, as one tagged table:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: azure-dev
spec:
  platform:
    name: azure-vm
    subscription_id: "..."
    resource_group: agentworks-vms
    region: eastus2
```

- `spec.platform` is one table: its `name` key names a `vm-platform` capability row (`lima`, `wsl2`;
  `proxmox` and `azure-vm` ship as the opt-in `proxmox` and `azure` system plugins, see
  [System plugins](#system-plugins)), and the remaining keys are that platform's configuration,
  validated by it (unknown keys are errors). A platform needing no config is just
  `platform: {name: wsl2}`. Remote Lima is just a lima site with a `vm_host: user@host` key. The old
  sibling shape (`platform: azure-vm` as a string plus a `platform_config:` table) still loads
  unchanged but is deprecated and will be removed; fold the pair into the tagged table.
- The `lima-local` and `wsl2` sites ship built in with empty config. Like every site they register
  on every host and report not-ready where this host lacks what they need (wsl2 is Windows-only; a
  local Lima site needs `limactl`); a not-ready site still lists and describes with its reason, and
  using it is an error. Their names are reserved. A site named after a platform must declare that
  platform.
- Consumers name sites: `agw vm create --site`, `defaults.site` in config.toml, and each VM row's
  `site`. Templates deliberately carry no site: placement is per-host, never template state.
- Site config secrets ride the standard secret machinery: a Proxmox site references its API token as
  the `proxmox-token` secret (override with the `token_secret` key), auto-declared and resolved
  through the backend chain like any other. An Azure site can do the same, optionally: it
  authenticates with ambient credentials (`az login`, `AZURE_*` env vars, managed identity, browser
  fallback) unless a `service_principal` table inside the platform table declares an explicit one,
  in which case its `tenant_id` / `client_id` are plain config and its `secret` field names the
  secret holding the client secret (default `azure-client-secret`). A site with a service principal
  uses that identity and only that one: a rejected or expired client secret fails the command rather
  than falling back to ambient credentials. `agw resource sample vm-site` shows the block. The
  `proxmox` platform ships as the opt-in `proxmox` system plugin, so a proxmox site (declared or
  legacy) is not-ready with an "enable plugin `proxmox`" hint and refused at use until you set
  `[plugins] system = ["proxmox"]`. The `azure-vm` platform likewise ships as the opt-in `azure`
  system plugin (which also provides the `azdo` git-credential provider and the `az-cli`
  install-command), so the `azure-dev` example above is not-ready with an "enable plugin `azure`"
  hint until you set `[plugins] system = ["azure"]`.
- The legacy flat `[azure]` / `[proxmox]` TOML sections keep loading as deprecated vm-site
  declarations; `agw resource migrate vm-site` moves them to manifests.

## Harness integrations

What a session runs is declared as a **harness integration**: the Agentworks capability (registered
code) that knows how a particular harness or shell is started, restarted, and what executables it
needs. A session template pairs an integration with its configuration in one tagged table, exactly
the way a vm-site pairs a platform with its config:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: htop
  description: Live process monitor
spec:
  harness_integration:
    name: shell
    command: htop
    required_commands: [htop]
```

- `spec.harness_integration` is one table: its `name` key names a `harness-integration` capability
  row, and the remaining keys are the config block that integration owns and validates (unknown keys
  are errors). A template that names no integration resolves to the built-in `shell` integration (a
  plain login shell, or an operator command), which is the built-in `default` template. The old
  `harness` / `harness_config` inputs still load in 0.13.0 and contribute to one aggregated
  deprecation warning per command/request; run `agw resource migrate` to rewrite them.
- The `shell` integration's config vocabulary is `command` (the pane command; empty is a login
  shell), `resume_command` (used by `session resume`, falling back to `command`), and
  `required_commands` (executables checked on the launch target before any state mutation).
  `command` / `resume_command` support the `{{session_name}}` and `{{workspace_name}}` variables.
  `restart_command` is accepted with a suppressible deprecation warning in 0.13.0 only; replace it
  with `resume_command` before upgrading to 0.14.0.
- The integration-plus-config pair inherits as a unit: a child restating the same integration merges
  its config keys into the parent's (child wins per key; `shell` unions `required_commands`), while
  a child naming a _different_ integration starts fresh. `env`, `inherits`, and the description
  merge as usual.
- The legacy flat `command` / `restart_command` / `required_commands` keys keep loading in TOML
  (hoisted onto `harness_integration = "shell"`); YAML manifests spell them inside the
  `harness_integration` table. `restart_command` is a deprecated input in either form.
  `agw resource migrate` rewrites it to `resume_command`;
  `agw resource describe harness-integration/shell` shows the integration row and the templates that
  reference it.

The `claude-code` integration runs Claude Code as the session. It ships as the opt-in `claude`
system plugin (see "System plugins" below), so a `session-template` naming it still lists ready, but
creating a session on it is refused with an "enable plugin `claude`" hint until you set
`[plugins] system = ["claude"]`. It selects the launch-and-resume conventions in one table instead
of restating command strings: `session create` starts a new Claude session, and `session resume`
continues the same conversation when its transcript still exists (and launches fresh when Claude
never wrote one), so a resume continues where the session left off:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: claude
  description: Claude Code session
spec:
  harness_integration:
    name: claude-code
    permission_mode: acceptEdits # optional; forwarded to `claude --permission-mode`
    model: opus # optional; forwarded to `claude --model`
    extra_args: [--append-system-prompt, "session {{session_name}}"] # optional escape hatch
```

- Its config is three optional fields: `permission_mode` and `model` forward verbatim to
  `claude --permission-mode` / `--model` (their choice sets are Claude's, not validated here: an
  invalid value fails at launch with the tool's own error, which `session create` / `session resume`
  capture into their error message when the workload exits immediately), and `extra_args` is a list
  of raw argv tokens appended last, the escape hatch for any flag the integration does not model.
  Unknown fields are errors. `extra_args` elements support the `{{session_name}}` /
  `{{workspace_name}}` variables.
- The only requirement checked on the launch target is that `claude` is installed. The chosen action
  (resume vs new session) is announced in the pane on start, so it is never silent.

The `codex` integration runs Codex the same way and ships as the opt-in `codex` system plugin. Codex
mints its own session ids, so instead of assigning one the integration discovers the id from Codex's
on-disk state after the first launch and stores it; `session resume` then resumes the same
conversation whenever its session file still exists (a session archived with `codex archive` is
deliberately treated as not resumable, and a fresh one is started). Its config is ten optional
fields: `model`, `sandbox`, `approval_policy`, and `profile` forward verbatim to `codex -m` / `-s` /
`-a` / `-p` (their choice sets are Codex's, not validated here); `network` (bool) forwards to
Codex's `sandbox_workspace_write.network_access` config key (Codex sandboxes network OFF by default
even in `workspace-write`, so a coding session that needs `npm install` or `git push` wants
`network: true`); `approvals_reviewer` (string) forwards to Codex's `approvals_reviewer` config key
and selects who adjudicates approval escalations (sandbox escapes, blocked network access): Codex
documents `user` (the default: escalations prompt the human in the pane) and `auto_review` (Codex's
risk-based reviewer subagent approves or denies instead, with the sandbox still enforcing the outer
boundary), so unattended-leaning "auto" templates usually want `auto_review`; `writable_dirs` (list)
grants extra writable directories alongside the workspace (one `codex --add-dir` each; union-merged
across template inheritance; entries are passed literally, so use absolute paths: `~` and `$HOME`
are not expanded); `web_search` (bool) enables Codex's live web-search tool (`codex --search`,
distinct from sandbox network access); `extra_args` is the same appended-last escape hatch; and
`disable_strict_config` (bool) is the strictness off-switch described next. The integration always
passes `--strict-config` so a Codex config mistake (or a Codex-renamed config key) fails loudly at
launch instead of being silently ignored; `disable_strict_config: true` turns that off when
strictness itself is the problem: a target whose `config.toml` Codex must tolerate (for example one
written by a newer Codex than the target runs), or a target Codex old enough to not know the flag
(it was verified against codex-cli 0.146.0, and an older binary rejects it as an unknown argument at
launch). The only launch-target requirement is that `codex` is installed, and the chosen action
(resume, adopt-and-resume, or new session) is announced in the pane on start:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: codex
  description: Codex session
spec:
  harness_integration:
    name: codex
    sandbox: workspace-write # optional; forwarded to `codex -s`
    approval_policy: on-request # optional; forwarded to `codex -a`
    approvals_reviewer: auto_review # optional; escalations adjudicated by Codex's reviewer subagent
    network: true # optional; sandbox network access (off by default)
```

`shell` is the built-in default integration; `claude-code` and `codex` ship as the opt-in `claude`
and `codex` system plugins. None of them is the whole set the platform is built around. The
`harness-integration` kind is extensible: another harness or shell runtime, whatever the provider,
is added as its own integration with its own config vocabulary. `claude-code` above (and its
Claude-specific `model` / `permission_mode` fields) is one worked example; the core assumes no
particular runtime, and a session runs whatever integration its template selects.

## Built-ins and overrides

Built-in resources ship with the app and appear in `agw resource list --origin builtin`. Override
policy is per kind:

- **Apt / install-command kinds** (`apt-source`, `apt-package`, `system-install-command`,
  `user-install-command`): declaring the same name overrides the built-in, the name is the
  interface, and same-name override is how you customize what `gh` installs.
- **Bundled vm-sites** (`lima-local`, `wsl2`): reserved names. Redeclaring one is an error; declare
  a sibling site instead. Like every vm-site they register on every host and report not-ready where
  this host lacks what they need (`agw resource list` marks the row; `describe` and `agw doctor`
  carry the reason); using a not-ready site is an error naming the requirement. A site naming an
  UNKNOWN platform (a typo, or an uninstalled plugin) is a hard error at load, not a self-disable.
- **Secret backends** (`env-var`, `prompt`; `onepassword` ships as an opt-in system plugin, see
  below), **VM platforms** (`lima`, `wsl2`; `proxmox` and `azure-vm` ship as the opt-in `proxmox`
  and `azure` system plugins, see below), **git-credential providers** (`github`; `azdo` ships in
  the opt-in `azure` system plugin), and **harness integrations** (`shell`; `claude-code` and
  `codex` ship as the opt-in `claude` and `codex` system plugins, see below): registered
  capabilities, shown as read-only rows. You cannot declare or override them; secrets customize per
  secret via `backend_mappings`, platforms configure per site via the `spec.platform` table, and
  integrations configure per session-template via the `spec.harness_integration` table. Every
  installed platform publishes a row regardless of host support: a platform whose host requirements
  are not met (e.g. `wsl2` off Windows) publishes a present, not-ready row (`agw resource list` and
  `agw doctor` show it with the reason), and a site referencing it is not-ready rather than
  erroring.

## System plugins

A **system plugin** bundles capability implementations (VM platforms, harness integrations,
git-credential providers, secret backends) and optional resource manifests that ship with agentworks
but are separable and opt-in. Its contributed resources carry the fourth origin, **system-plugin**,
and are attributed on the surfaces as `from plugin <name>`. A plugin is not a resource kind of its
own: it publishes resources of the existing kinds.

Plugins are off by default. Opt in by name in `config.toml`:

```toml
[plugins]
system = ["azure"]
```

- An unknown name here (a typo, or a plugin that is not installed) is a hard error, and so is an
  unknown key in the `[plugins]` table: the section is an opt-in gate, so a mistake fails loudly
  rather than silently leaving plugins off.
- A plugin you have **not** enabled still publishes both its capability rows AND its bundled
  manifest resources, all **disabled**: a resource referencing a disabled capability is not-ready
  with an `enable plugin <name>` hint (not an unknown-name error), and a reference to a disabled
  bundled resource (for example a template's `system_install_commands` naming the `az-cli`
  install-command while `azure` is off) is refused at use with the same hint, never an unknown-name
  error. A disabled plugin's resources are hidden from `resource list` and never block an operator's
  identically-named resource, but they are present, so the reference always resolves to the friendly
  hint.

**Disabled resources are hidden by default.** `agw resource list` omits disabled rows; pass
`--include-disabled` to reveal them. `--origin plugin` narrows the listing to plugin-contributed
rows but still honors the disabled default, so combine it with `--include-disabled` to see a
not-enabled plugin's rows. `agw resource describe <kind>/<name>` always renders a named resource,
disabled or not, with a `Disabled:` line. `agw doctor` has a **System plugins** roster: each
installed plugin, its description, and whether it is enabled. Note the axis distinction: "disabled"
is the opt-in state and hides the row, while a **not-ready** resource (enabled but unable to run on
this host) still lists with its reason.

The shipped build installs the `onepassword` (1Password secret backend), `claude` (Claude Code
harness integration and its `claude` CLI install-command), `codex` (Codex harness integration and
its `codex` CLI install-command), `proxmox` (Proxmox VE VM platform), and `azure` (Azure VM
platform, `azdo` git-credential provider, and `az-cli` install-command) plugins, all disabled until
opted in. Authoring a system plugin is documented in the plugins package README
(`cli/agentworks/plugins/README.md`).

**Config errors in a not-enabled plugin's resources surface only once you enable it.** Validation
runs over enabled, reachable resources, so a mistake in a disabled plugin's config (for example a
typo in a legacy `[proxmox]` section's platform config while the `proxmox` plugin is not enabled) is
not reported until you add the plugin to `[plugins].system`. The first thing you see is the
actionable "enable plugin `<name>`" hint; the config error surfaces on the next build once enabled,
still before any real work runs. This is the same rule that defers validation for any not-ready
resource, applied to the opt-in axis.

**Upgrading: Azure, Proxmox, 1Password, and Claude Code are now opt-in.** These vendor- and
tool-specific capabilities used to be built in and always available; they now ship as the `azure`,
`proxmox`, `onepassword`, and `claude` system plugins, disabled by default. If your config used any
of them, add the plugin to `[plugins].system` to restore it:

```toml
[plugins]
system = ["azure", "proxmox", "onepassword", "claude"]  # only the ones you use
```

Concretely, enable `onepassword` if a secret maps the `onepassword` backend; `proxmox` if a
`vm-site` (or a legacy `[proxmox]` section) uses the `proxmox` platform; `azure` if you use the
`azure-vm` platform, the `azdo` (Azure DevOps) git-credential provider, or the `az-cli`
install-command; and `claude` if a `session-template` uses the `claude-code` integration or a
template installs the `claude` CLI. Until you do, a resource that references one is not-ready (or
refused at use) with an "enable plugin `<name>`" hint, never a silent failure. The default local
path (the `lima` / `wsl2` platforms, the `shell` harness integration, the `env-var` / `prompt`
secret backends, and the `github` git-credential provider) is unchanged and needs no `[plugins]`
entry. `agw doctor` lists every installed plugin and whether it is enabled.

## Secrets: backends and the chain

Two layers, one rule each:

- A **secret backend** is a capability resource: a read-only `secret-backend` row whose
  implementation is registered code (`env-var`, `prompt`; `onepassword` ships as a system plugin;
  later plugins, ...). You cannot declare one (the app, and plugins, register them), but they list
  and describe like every other resource. Per-secret behavior (identifier overrides, structured
  store addressing like `{ account = "my.1password.com", reference = "op://Work/npm/password" }`,
  and opt-outs) lives in each secret's `backend_mappings.<backend>`. The `onepassword` backend now
  ships as the opt-in `onepassword` system plugin (see [System plugins](#system-plugins)): its row
  is present but disabled until you add `onepassword` to `[plugins].system`, so a secret mapped only
  to it stays inert (and, if it is the sole path, fails resolve with an "enable plugin
  `onepassword`" hint) until you opt in. Once enabled it reads via the 1Password CLI
  (`op read op://vault/item/field`); it needs a per-secret `backend_mappings.onepassword` address in
  one of two forms: a bare `op://vault/item/field` string (using op's default account, or
  `OP_ACCOUNT`), or a `{ account, reference }` table when a specific account must be pinned. `op`
  must be able to read at command time, meaning either the 1Password app's CLI integration is
  enabled or you have run `op signin`.
- The **chain** is a setting: `[secret_config].backends` in `config.toml` lists the active backends
  in precedence order (default `["env-var", "prompt"]`). Registered backends absent from the chain
  are dormant.

### The words the surfaces use

A backend, for a given secret, sits on a few independent axes. The surfaces keep them straight, and
so should you when reading them:

- **present**: a node exists for it (a built-in; later, an installed plugin). Absent means a typo or
  an uninstalled unit.
- **enabled / disabled**: the opt-in axis (turned on or off). "enabled" and "disabled" mean this and
  only this; they never describe host readiness. A system plugin's contributions are disabled until
  the operator opts in via `[plugins].system` (for example the `onepassword` backend is disabled by
  default); the core backends (`env-var`, `prompt`) are always enabled.
- **ready / not-ready**: whether the backend can run on THIS host right now, checked offline (e.g.
  `onepassword` is not-ready when the `op` CLI is not on `PATH`). Readiness is not resolvability: a
  ready backend may still have no value for a given secret.
- **opted-in**: named in `[secret_config].backends` (the chain: selection plus order). Only opted-in
  backends are columns in `agw secret list`.
- **would-attempt**: for THIS secret, the backend has a mapping (or is mapping-optional). A pure
  function of the secret and its `backend_mappings`, independent of readiness. `won't attempt` is a
  `false` opt-out, or a mapping-required backend (like `onepassword`) with no mapping.

Resolution is a pass over the chain in precedence order: the first backend that produces a value
wins. You are never prompted for the same secret twice in one command, and all prompting happens up
front, before the command starts changing anything. The walk considers a candidate only when it is
**present, enabled, ready, opted-in, and would-attempt** the secret.

A **not-ready** opted-in backend is **skipped with a warning** and the chain falls through to the
next candidate (so a configured `onepassword` with no `op` installed no longer halts resolution; it
warns and the next backend, e.g. `prompt`, takes over). The anti-masking halt is kept only for a
_ready_ store's hard miss (available, but definitively no value), so a misconfigured store never
falls silently through to a prompt. A secret no active backend can resolve fails at preflight with a
hint, before any prompt and before anything changes.

Readiness is offline and honest; it sits UNDER the optimistic interactivity preview. A `prompt` (or
a biometric `op`) is still previewed optimistically on would-attempt alone: the inspection surfaces
never probe an interaction to answer readiness.

`agw secret list` shows, per opted-in backend column, the lookup identifier / `would attempt` /
`not ready: <reason>` / `won't attempt`; `agw secret describe <name>` shows one secret in full
(mappings flagged not-ready where they apply, and a resolution preview that skips not-ready
backends); `agw doctor` has a **Secret backends** group (one readiness row per backend) plus one row
per secret with the runtime outcome.

## Inspecting the whole picture

```bash
agw resource list --origin operator     # what you have declared, either source
agw resource describe secret/npm-token  # where it's referenced, what uses it
agw doctor                              # health: would every secret resolve?
```

The design rationale (the config/resource split, capability kinds, the vocabulary rules, and the
vm-site / vm-platform pair) is recorded in ADR 0016. Its dual-path section records the original
keep-both-paths stance; a status note there marks the revision to today's deprecate-for-removal
policy, pending a superseding ADR from the sunset SDD effort.

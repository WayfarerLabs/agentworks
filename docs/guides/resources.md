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
  names) and `description` (stored and shown for every declarable kind). One kind accepts only
  `name: default` for now: `named-console-template` is an ordinary multi-instance kind in the
  framework, but no command can select a named instance yet, so a named declaration would be dead
  config (issue #165 adds the selector).
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

- `spec.platform` names a `vm-platform` capability row (`lima`, `wsl2`; `proxmox` and `azure-vm`
  ship as the opt-in `proxmox` and `azure` system plugins, see [System plugins](#system-plugins));
  `spec.platform_config` is validated by that platform (unknown keys are errors). Remote Lima is
  just a lima site with `platform_config.vm_host: user@host`.
- The `lima-local` and `wsl2` sites ship built in with empty config. Like every site they register
  on every host and report not-ready where this host lacks what they need (wsl2 is Windows-only; a
  local Lima site needs `limactl`); a not-ready site still lists and describes with its reason, and
  using it is an error. Their names are reserved. A site named after a platform must declare that
  platform.
- Consumers name sites: `agw vm create --site`, `defaults.site` in config.toml, and each VM row's
  `site`. Templates deliberately carry no site: placement is per-host, never template state.
- Site config secrets ride the standard secret machinery: a Proxmox site references its API token as
  the `proxmox-token` secret (override with `token_secret`), auto-declared and resolved through the
  backend chain like any other. The `proxmox` platform ships as the opt-in `proxmox` system plugin,
  so a proxmox site (declared or legacy) is not-ready with an "enable plugin `proxmox`" hint and
  refused at use until you set `[plugins] enabled = ["proxmox"]`. The `azure-vm` platform likewise
  ships as the opt-in `azure` system plugin (which also provides the `azdo` git-credential provider
  and the `az-cli` install-command), so the `azure-dev` example above is not-ready with an "enable
  plugin `azure`" hint until you set `[plugins] enabled = ["azure"]`.
- The legacy flat `[azure]` / `[proxmox]` TOML sections keep loading as deprecated vm-site
  declarations; `agw resource migrate vm-site` moves them to manifests.

## Session harnesses

What a session runs is declared as a **harness**: the capability (registered code) that knows how a
particular tool is started, restarted, and what executables it needs. A session template pairs a
harness with that harness's configuration, exactly the way a vm-site pairs a platform with its
config:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: htop
  description: Live process monitor
spec:
  harness: shell
  harness_config:
    command: htop
    required_commands: [htop]
```

- `spec.harness` names a `harness` capability row; `spec.harness_config` is the block that harness
  owns and validates (unknown keys are errors). A template that names no harness resolves to the
  built-in `shell` harness (a plain login shell, or an operator command), which is the built-in
  `default` template.
- The `shell` harness's config vocabulary is `command` (the pane command; empty is a login shell),
  `restart_command` (used by `session restart`, falling back to `command`), and `required_commands`
  (executables checked on the launch target before any state mutation). `command` /
  `restart_command` support the `{{session_name}}` and `{{workspace_name}}` variables.
- The `(harness, harness_config)` pair inherits as a unit: a child restating the same harness merges
  its block into the parent's (child wins per key; `shell` unions `required_commands`), while a
  child naming a _different_ harness starts fresh. `env`, `inherits`, and the description merge as
  usual.
- The legacy flat `command` / `restart_command` / `required_commands` keys keep loading in TOML
  (hoisted onto `harness = "shell"`); YAML manifests spell them under `harness_config`.
  `agw resource describe harness/shell` shows the harness row and the templates that reference it.

The `claude-code` harness runs Claude Code as the session. It ships as the opt-in `claude` system
plugin (see "System plugins" below), so a `session-template` naming it still lists ready, but
creating a session on it is refused with an "enable plugin `claude`" hint until you set
`[plugins] enabled = ["claude"]`. It selects the launch-and-resume conventions in one line instead
of restating command strings: `session create` starts a new Claude session, and `session restart`
resumes the same conversation when its transcript still exists (and launches fresh when Claude never
wrote one), so a restart continues where the session left off:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: claude
  description: Claude Code session
spec:
  harness: claude-code
  harness_config:
    permission_mode: acceptEdits # optional; forwarded to `claude --permission-mode`
    model: opus # optional; forwarded to `claude --model`
    extra_args: [--append-system-prompt, "session {{session_name}}"] # optional escape hatch
```

- `harness_config` is three optional fields: `permission_mode` and `model` forward verbatim to
  `claude --permission-mode` / `--model` (their choice sets are Claude's, not validated here), and
  `extra_args` is a list of raw argv tokens appended last, the escape hatch for any flag the harness
  does not model. Unknown fields are errors. `extra_args` elements support the `{{session_name}}` /
  `{{workspace_name}}` variables.
- The only requirement checked on the launch target is that `claude` is installed. The chosen action
  (resume vs new session) is announced in the pane on start, so it is never silent.

`shell` is the built-in default harness; `claude-code` ships as the opt-in `claude` system plugin.
Neither is the whole set the platform is built around. The `harness` kind is extensible: another
tool or agent runtime, whatever the provider, is added as its own harness with its own
`harness_config` vocabulary. `claude-code` above (and its Claude-specific `model` /
`permission_mode` fields) is one worked example; the core assumes no particular runtime, and a
session runs whatever harness its template selects.

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
  the opt-in `azure` system plugin), and **session harnesses** (`shell`; `claude-code` ships as the
  opt-in `claude` system plugin, see below): registered capabilities, shown as read-only rows. You
  cannot declare or override them; secrets customize per secret via `backend_mappings`, platforms
  configure per site via `platform_config`, and harnesses configure per session-template via
  `harness_config`. Every installed platform publishes a row regardless of host support: a platform
  whose host requirements are not met (e.g. `wsl2` off Windows) publishes a present, not-ready row
  (`agw resource list` and `agw doctor` show it with the reason), and a site referencing it is
  not-ready rather than erroring.

## System plugins

A **system plugin** bundles capability implementations (VM platforms, harnesses, git-credential
providers, secret backends) and optional resource manifests that ship with agentworks but are
separable and opt-in. Its contributed resources carry the fourth origin, **system-plugin**, and are
attributed on the surfaces as `from plugin <name>`. A plugin is not a resource kind of its own: it
publishes resources of the existing kinds.

Plugins are off by default. Opt in by name in `config.toml`:

```toml
[plugins]
enabled = ["azure"]
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
session harness and its `claude` CLI install-command), `proxmox` (Proxmox VE VM platform), and
`azure` (Azure VM platform, `azdo` git-credential provider, and `az-cli` install-command) plugins,
all disabled until opted in. Authoring a system plugin is documented in the plugins package README
(`cli/agentworks/plugins/README.md`).

**Config errors in a not-enabled plugin's resources surface only once you enable it.** Validation
runs over enabled, reachable resources, so a mistake in a disabled plugin's config (for example a
typo in a legacy `[proxmox]` platform_config while the `proxmox` plugin is not enabled) is not
reported until you add the plugin to `[plugins] enabled`. The first thing you see is the actionable
"enable plugin `<name>`" hint; the config error surfaces on the next build once enabled, still
before any real work runs. This is the same rule that defers validation for any not-ready resource,
applied to the opt-in axis.

**Upgrading: Azure, Proxmox, 1Password, and Claude Code are now opt-in.** These vendor- and
tool-specific capabilities used to be built in and always available; they now ship as the `azure`,
`proxmox`, `onepassword`, and `claude` system plugins, disabled by default. If your config used any
of them, add the plugin to `[plugins] enabled` to restore it:

```toml
[plugins]
enabled = ["azure", "proxmox", "onepassword", "claude"]  # only the ones you use
```

Concretely, enable `onepassword` if a secret maps the `onepassword` backend; `proxmox` if a
`vm-site` (or a legacy `[proxmox]` section) uses the `proxmox` platform; `azure` if you use the
`azure-vm` platform, the `azdo` (Azure DevOps) git-credential provider, or the `az-cli`
install-command; and `claude` if a `session-template` uses the `claude-code` harness or a template
installs the `claude` CLI. Until you do, a resource that references one is not-ready (or refused at
use) with an "enable plugin `<name>`" hint, never a silent failure. The default local path (the
`lima` / `wsl2` platforms, the `shell` harness, the `env-var` / `prompt` secret backends, and the
`github` git-credential provider) is unchanged and needs no `[plugins]` entry. `agw doctor` lists
every installed plugin and whether it is enabled.

## Secrets: backends and the chain

Two layers, one rule each:

- A **secret backend** is a capability resource: a read-only `secret-backend` row whose
  implementation is registered code (`env-var`, `prompt`; `onepassword` ships as a system plugin;
  later plugins, ...). You cannot declare one (the app, and plugins, register them), but they list
  and describe like every other resource. Per-secret behavior (identifier overrides, structured
  store addressing like `{ account = "my.1password.com", reference = "op://Work/npm/password" }`,
  and opt-outs) lives in each secret's `backend_mappings.<backend>`. The `onepassword` backend now
  ships as the opt-in `onepassword` system plugin (see [System plugins](#system-plugins)): its row
  is present but disabled until you add `onepassword` to `[plugins] enabled`, so a secret mapped
  only to it stays inert (and, if it is the sole path, fails resolve with an "enable plugin
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
  only this; they never describe host readiness. (No backend is disabled today; the axis is modeled
  for the plugin work.)
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

The design rationale (the config/resource split, capability kinds, the vocabulary rules, why dual
sources are permanent, and the vm-site / vm-platform pair) is recorded in ADR 0016.

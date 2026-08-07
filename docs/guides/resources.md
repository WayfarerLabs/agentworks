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

Resources come from four origins: **operator-declared** (you wrote them, as YAML manifests),
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
  names), `description` (stored and shown for every declarable kind), and `expires` (optional, a
  date or an RFC 3339 timestamp; validated but not yet acted on). One kind accepts only
  `name: default` for now: `named-console-template` is an ordinary multi-instance kind in the
  framework, but no command can select a named instance yet, so a named declaration would be dead
  config (issue #165 adds the selector).
- `spec` carries the kind-specific fields, validated against that kind's declared model. The split
  is strict in both directions: a metadata field written inside `spec` is refused (it would silently
  override the envelope), and a `spec` key the kind does not declare is refused too, with a message
  naming the fields it does. A misspelled key used to load and do nothing.
- Multiple documents per file are separated with `---`.

`agw resource sample vm-template` prints a commented starter for one kind (`--all` for every kind);
`--write <file>` saves it under the resources directory instead. Samples are fully commented out:
delete one leading `#` from each DOCUMENT line to activate the parts you want. A saved file also
opens with a `# yaml-language-server:` line, which is an ordinary comment and stays one;
uncommenting that would turn it into a key the loader rejects.

The sample is rendered from the same declaration the loader validates against, so it always matches
what the kind actually accepts. The fields you MUST write are live document lines; every optional
field is a commented suggestion at its own indent, carrying its type, its default or an example, and
what it is for. Uncomment the ones you want. Where a field selects a capability (a vm-site's
`platform`), one implementation is written out and the rest are named beside it.

`agw resource describe-kind` is the same information without a document to edit:

```bash
agw resource describe-kind vm-site               # every field of a kind
agw resource describe-kind vm-platform           # the platforms this build has
agw resource describe-kind vm-platform/aws-ec2   # one platform's own config
```

It reads no config and builds no registry, so it answers on a host whose `config.toml` does not
load, and it documents a capability whose plugin is not enabled yet.

`agw resource edit KIND/NAME` opens the manifest declaring a resource in `$EDITOR`.

## Editing manifests with schema support

Agentworks emits JSON Schema (draft 2020-12) for manifests, so a schema-aware editor gives you
completions, hover documentation, and live diagnostics as you type, including for kinds and
capabilities a plugin contributed.

```bash
agw resource schema                    # the any-kind schema, to stdout
agw resource schema vm-template        # one kind's
agw resource schema --write            # the whole set, into resources/.schema/
```

Files that agentworks writes for you already carry the association, as a modeline on their first
line:

```yaml
# yaml-language-server: $schema=.schema/vm-template.schema.json
```

Both `agw resource sample --write` and `agw resource migrate` stamp it on the files they CREATE, and
write the schemas alongside so the reference resolves. They leave an existing file's first line
alone, because a modeline has to be at the top and inserting one would shift every line number you
already know. To get the association on a manifest you wrote by hand, add that line yourself
(`agw resource schema --write` first, so the file it names exists).

The schema describes THIS host: a capability from a plugin appears in it once the plugin is
installed, so re-run `agw resource schema --write` after installing one. The schemas are generated
artifacts; `.schema/` is a dot-directory, so the manifest loader never reads what is in it.

**Setting up an editor.** In VS Code (or any editor with a YAML language server), install the
[YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml), open a
manifest under `~/.config/agentworks/resources/`, and you should see completions on `spec` keys and
hover text on each field. To confirm it is really working: change a `spec` key to a name the kind
does not declare, and the editor should underline it immediately. If nothing happens, check that the
first line of the file is the modeline and that the path it names exists.

What the editor checks is a deliberate subset of what loading checks. Everything it flags is a real
error, but agentworks also applies rules JSON Schema cannot state (cross-field constraints, name
character rules, whether a capability is registered here at all), so a manifest with no editor
diagnostics can still fail to load. The direction is on purpose: a schema that under-reports costs
you a squiggle, while one that over-reports would underline valid configuration.

## Scoped GitHub credentials (fine-grained PATs)

A `git-credential`'s `spec.provider` is one tagged table: its `name` key selects the provider
capability and the remaining keys are that provider's configuration, which
`agw resource describe-kind git-credential-provider/<name>` documents. (The old sibling shape, a
`provider:` string plus a `provider_config:` table, is no longer accepted;
`agw resource migrate --all` folds the pair into the tagged table.)

A github credential may carry a scope there, and the choice is the part worth explaining:
`repos: ["owner/name", ...]` pins the credential to specific repositories (always a list, even for
one, matching a fine-grained PAT's selected repos), while `owner: "org"` covers every repository
under that user or org, including repos an agent clones ad hoc that no workspace ever declared. The
two are mutually exclusive; a credential with neither is the unscoped fallback.

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

## TOML resource sections: removed

Declaring resources in `config.toml` is no longer supported. `config.toml` is settings only. The
classic TOML resource sections (`[secrets.*]`, `[vm_templates.*]`, `[git_credentials.*]`, the legacy
flat `[azure]` / `[proxmox]` vm-site sections, `[apt_sources.*]`, and the rest) no longer load: a
`config.toml` that still carries any of them is a hard error at load, naming the offending sections
and pointing you at `agw resource migrate`. This was deprecated with a load-time warning in an
earlier release and is now removed. Resources are declared as YAML manifests (see "Declaring
resources" above); settings sections load exactly as before.

**Upgrading.** This is a breaking change. If your `config.toml` still declares resources, migrate
them to YAML manifests before (or right after) upgrading, so no command hits the hard error:

```bash
agw resource migrate --all             # move every TOML resource declaration to YAML
agw resource migrate secret            # or one kind at a time
agw resource migrate vm-template/dev   # or one resource
agw resource migrate --all --dry-run   # see the plan first (--full for the diff)
```

`agw resource migrate` still reads the legacy TOML directly and can run even against a `config.toml`
the app would otherwise refuse to load (it loads with resources skipped, the settings-only escape
hatch), so you can migrate on either side of the upgrade. Once every resource section is moved, the
hard error is gone. Any section you have not moved stays a hard error until you migrate or delete
it.

The migrator handles TOML-declared resources, which become new YAML documents appended without
rewriting existing YAML content; migrated TOML sections are commented out in place with a
`# migrated to ...` marker (or removed with `--toml delete`).

**Manifests on a retired shape.** Every run also upgrades manifests that still name a capability in
the old sibling shape (`platform: lima` plus a `platform_config:` table, and likewise
`provider`/`provider_config`) to the tagged table `platform: {name: lima, ...}`. Those files are
rewritten in place, preserving comments, quoting, key order, and every unrelated document. This half
is not scoped by the selectors: the old shape no longer loads at all, so leaving one document behind
would leave the whole resources directory unloadable. A run with nothing else to do (`--all` with no
TOML resources left) does exactly this and nothing more.

Every real run backs up `config.toml`; a run that modifies an existing YAML file also stores its
original as a recovery copy under `paths.backups`. Digest guards refuse to replace files changed
after planning, writes are atomic, and rollback restores only outputs that still match the run's
digest, so concurrent edits are not overwritten. Finally, the command verifies that the migrated
resources still decode to exactly what they declared in TOML, rolling back on a mismatch and
reporting any recovery copy needed for manual repair. Use `--dry-run --full` to inspect generated
documents, in-place YAML diffs, and the TOML diff before writing.

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

- `spec.platform` is one table: its `name` key names a `vm-platform` capability row and the
  remaining keys are that platform's configuration, validated by it (unknown keys are errors).
  `agw resource describe-kind vm-platform` lists the platforms this build has, including any that
  arrive with an opt-in [system plugin](#system-plugins);
  `agw resource describe-kind vm-platform/<name>` documents one platform's own fields. A platform
  needing no config is just `platform: {name: wsl2}`. Remote Lima is just a lima site with a
  `vm_host: user@host` key. The old sibling shape (`platform: azure-vm` as a string plus a
  `platform_config:` table) is no longer accepted; `agw resource migrate --all` folds the pair into
  the tagged table.
- The `lima-local` and `wsl2` sites ship built in with empty config. Like every site they register
  on every host and report not-ready where this host lacks what they need (wsl2 is Windows-only; a
  local Lima site needs `limactl`); a not-ready site still lists and describes with its reason, and
  using it is an error. Their names are reserved. A site named after a platform must declare that
  platform.
- Consumers name sites: `agw vm create --site`, `defaults.site` in config.toml, and each VM row's
  `site`. Templates deliberately carry no site: placement is per-host, never template state.
- Site config secrets ride the standard secret machinery: a platform that needs a credential names
  the secret holding it in its own config, defaulting to a well-known name when you leave the field
  out (a Proxmox site's API token is the `proxmox-token` secret unless `token_secret` says
  otherwise). Those secrets are auto-declared and resolved through the backend chain like any other,
  and `agw resource describe-kind vm-platform/<name>` shows each platform's secret fields with their
  default names. Azure is the one with a choice to make: it authenticates with ambient credentials
  (`az login`, `AZURE_*` env vars, managed identity, browser fallback) unless a `service_principal`
  table inside the platform table declares an explicit one. A site with a service principal uses
  that identity and only that one, so a rejected or expired client secret fails the command rather
  than falling back to ambient credentials.
- The cloud and datacenter platforms ship as opt-in system plugins, so a site that names one is
  not-ready with an "enable plugin `<name>`" hint, and refused at use, until you list that plugin in
  `[plugins] system`. The `azure-dev` example above is not-ready until you set
  `[plugins] system = ["azure"]`. `agw doctor` lists every installed plugin and whether it is
  enabled, and `agw resource describe-kind vm-platform/<name>` says which plugin a platform arrives
  with.
- The legacy flat `[azure]` / `[proxmox]` TOML sections no longer load: like every resource section,
  they are a hard error in `config.toml` now. Run `agw resource migrate vm-site` to move them to
  vm-site manifests (they migrate as vm-site, unchanged).

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
  plain login shell, or an operator command), which is the built-in `default` template.
- `agw resource describe-kind harness-integration` lists the integrations this build has, and
  `agw resource describe-kind harness-integration/<name>` documents one integration's config field
  by field. That is the reference; what follows is what an operator wants to know beyond the fields
  themselves.
- Command strings support the `{{session_name}}` and `{{workspace_name}}` variables. This holds
  wherever an integration takes a command or raw arguments (`shell`'s `command` and
  `resume_command`, the `extra_args` escape hatch on `claude-code` and `codex`).
- The integration-plus-config pair inherits as a unit: a child restating the same integration merges
  its config keys into the parent's (child wins per key), while a child naming a _different_
  integration starts fresh. `env`, `inherits`, and the description merge as usual. A few list fields
  union across the chain rather than replacing, so a child adding one entry never silently drops the
  parent's; the field reference marks which.
- `agw resource describe harness-integration/<name>` is the other half: the integration's registry
  row and the templates that reference it, rather than the fields it accepts.

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

- Its config is all optional, and every field is documented by
  `agw resource describe-kind harness-integration/claude-code`. What the reference cannot tell you:
  the fields that forward a value verbatim to `claude` are not validated here, because the valid
  choices are Claude's and they move between its releases. An invalid one fails at launch with the
  tool's own error, which `session create` / `session resume` capture into their error message when
  the workload exits immediately.
- The only requirement checked on the launch target is that `claude` is installed. The chosen action
  (resume vs new session) is announced in the pane on start, so it is never silent.

The `codex` integration runs Codex the same way and ships as the opt-in `codex` system plugin.

Codex mints its own session ids, so instead of assigning one the integration learns the id from
Codex itself: every launch installs a small recorder script that Codex runs after each completed
turn (via Codex's `notify` hook, so nothing is ever added to your conversation), which writes down
which conversation the pane is in. `session resume` then resumes exactly that conversation whenever
its session file still exists. A session archived with `codex archive` is deliberately treated as
not resumable, since un-archiving it behind your back would undo a decision you made: the binding is
dropped, and the fallback below then decides, so the next resume may adopt a different conversation
in the workspace or open the picker rather than simply starting fresh (`codex unarchive` brings the
archived one back). When nothing has been recorded yet, `session resume` falls back to looking for a
single interactive Codex conversation recorded in this workspace directory; if it finds several, it
opens Codex's own session picker in the pane rather than guessing, so pick the conversation you want
(the session binds to it from its next turn) or press esc to start a fresh one. `session create`
does none of that: a new session always starts a brand-new conversation and adopts nothing, so
reusing a deleted session's name can never silently pick that session's conversation back up. Note
the limit of that, because the fallback is still a heuristic: if the deleted session's conversation
is the only Codex conversation recorded in the workspace, the new session's first `session resume`
can still adopt it. Whichever way it went is announced in the command's output and as the pane's
first line: `session create` always reports a brand-new conversation, while the adoption and picker
outcomes belong to `session resume`, and an adoption names the Codex conversation id it chose, so
you can see it happen and fix it (pick the right conversation from the picker, or archive the stale
one) rather than discovering it later. Overriding `notify` yourself through `extra_args` turns the
recording off (yours wins, because `extra_args` is appended last), which leaves resume relying on
that fallback.

Its config is all optional, and `agw resource describe-kind harness-integration/codex` documents
every field. Four things the field reference does not say, because they are Codex's behavior rather
than facts about the fields:

- **Codex sandboxes network access OFF by default**, even under `workspace-write`. A coding session
  that needs `npm install` or `git push` has to turn it on.
- **Who adjudicates an approval escalation** (a sandbox escape, a blocked network call) is a choice.
  Codex documents `user`, the default, where escalations prompt the human in the pane, and
  `auto_review`, where Codex's risk-based reviewer subagent approves or denies instead with the
  sandbox still enforcing the outer boundary. Unattended-leaning "auto" templates usually want
  `auto_review`.
- **Extra writable directories are passed literally**, so use absolute paths: `~` and `$HOME` are
  not expanded.
- **The integration always passes `--strict-config`**, so a Codex config mistake (or a Codex-renamed
  config key) fails loudly at launch instead of being silently ignored. Turning that off is for when
  strictness itself is the problem: a target whose `config.toml` Codex must tolerate (one written by
  a newer Codex than the target runs), or a target Codex old enough not to know the flag (it was
  verified against codex-cli 0.146.0, and an older binary rejects it as an unknown argument at
  launch).

The only launch-target requirement is that `codex` is installed:

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
- **The four capability kinds** (`secret-backend`, `vm-platform`, `git-credential-provider`,
  `harness-integration`): registered code, shown as read-only rows. You cannot declare or override
  one. `agw resource describe-kind <capability-kind>` lists the implementations this build has, and
  naming one (`agw resource describe-kind vm-platform/proxmox`) says which system plugin it arrives
  with, if any. Configuration is per consumer rather than per capability: secrets customize per
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

Every plugin the build installs is disabled until you opt in. `agw doctor`'s **System plugins**
roster is the list for this build, with each plugin's description and its opt-in state;
`agw resource describe-kind <capability-kind>/<name>` says which plugin a given capability arrives
with. Authoring a system plugin is documented in the plugins package README
(`cli/agentworks/plugins/README.md`).

**Config errors in a not-enabled plugin's resources surface only once you enable it.** Validation
runs over enabled, reachable resources, so a mistake in a disabled plugin's config (for example a
typo in the platform config of a proxmox `vm-site` manifest while the `proxmox` plugin is not
enabled) is not reported until you add the plugin to `[plugins].system`. The first thing you see is
the actionable "enable plugin `<name>`" hint; the config error surfaces on the next build once
enabled, still before any real work runs. This is the same rule that defers validation for any
not-ready resource, applied to the opt-in axis.

**Upgrading: Azure, Proxmox, 1Password, and Claude Code are now opt-in.** These vendor- and
tool-specific capabilities used to be built in and always available; they now ship as the `azure`,
`proxmox`, `onepassword`, and `claude` system plugins, disabled by default. If your config used any
of them, add the plugin to `[plugins].system` to restore it:

```toml
[plugins]
system = ["azure", "proxmox", "onepassword", "claude"]  # only the ones you use
```

Concretely, enable `onepassword` if a secret maps the `onepassword` backend; `proxmox` if a
`vm-site` uses the `proxmox` platform; `azure` if you use the `azure-vm` platform, the `azdo` (Azure
DevOps) git-credential provider, or the `az-cli` install-command; and `claude` if a
`session-template` uses the `claude-code` integration or a template installs the `claude` CLI. Until
you do, a resource that references one is not-ready (or refused at use) with an "enable plugin
`<name>`" hint, never a silent failure. The default local path (the `lima` / `wsl2` platforms, the
`shell` harness integration, the `env-var` / `prompt` secret backends, and the `github`
git-credential provider) is unchanged and needs no `[plugins]` entry. `agw doctor` lists every
installed plugin and whether it is enabled.

## Secrets: backends and the chain

Two layers, one rule each:

- A **secret backend** is a capability resource: a read-only `secret-backend` row whose
  implementation is registered code. You cannot declare one (the app, and plugins, register them),
  but they list and describe like every other resource. `agw resource describe-kind secret-backend`
  lists the backends this build has, and naming one
  (`agw resource describe-kind secret-backend/onepassword`) documents the address it expects and
  what it needs on this host. Per-secret behavior (identifier overrides, structured store
  addressing, and opt-outs) lives in each secret's `backend_mappings.<backend>`. A backend that
  arrives with a system plugin is present but disabled until you name that plugin in
  `[plugins].system`, so a secret mapped only to it stays inert, and fails resolve with an "enable
  plugin `<name>`" hint if it is the sole path. For `onepassword` specifically, the address is
  either a bare `op://vault/item/field` string or a `{ account, reference }` table when a specific
  account must be pinned, and `op` has to be able to read at command time: either the 1Password
  app's CLI integration is enabled, or you have run `op signin`.
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

## Upgrading: manifests are validated against a declared schema

Every kind's spec and every capability's config is now checked against a model the code declares,
rather than by hand-written per-kind code. That is what makes `agw resource sample`,
`agw resource describe-kind`, and `agw resource schema` possible, and it is why they cannot be out
of date. It also means a manifest that used to load can now fail, in ways worth going through before
you upgrade.

**Start here.** The errors are specific: each one names the file, the line, the resource, the field,
and what was expected. So the fastest path is to upgrade, run any command, and fix what it names.
`agw resource describe-kind <kind>` (and `<capability-kind>/<name>`) documents what the field
accepts, and needs no working config to do it.

**One change has tooling.** The retired sibling capability shape (`platform: lima` beside a
`platform_config:` table, and likewise `provider` / `provider_config`) is a hard error, and
`agw resource migrate --all` rewrites it in place, preserving comments and everything else in the
file:

```bash
agw resource migrate --all --dry-run --full   # see it first
agw resource migrate --all
```

Two limits worth knowing. It refuses, before writing anything, if a capability's config carries its
own `name` key, because folding that is a judgment call; the error tells you to fold that one by
hand. And it preserves quoting faithfully, so it will carry a quoted number through into a file that
then trips the type checking below. Everything else here is a hand edit.

### Types are checked now

Values are no longer coerced. A quoted number is a string, and a string is not a boolean.

- **Proxmox.** `template_vmid: "9000"` used to be converted with `int(str(...))` and now fails.
  `verify_ssl: "no"` is the sharper one: it used to be read with `bool(...)`, so it meant **true**,
  the opposite of what it looks like. It now fails rather than doing the opposite of what you wrote.
  The platform's other fields (`api_url`, `node`, `token_id`, `storage`, `bridge`, `pool`) had no
  type check at all before and now have one.
- **apt-source, apt-package, admin-template.** These lost `str()` / `bool()` coercions. Concretely:
  an apt-source's `key_dearmor: "no"` used to mean **true**; `key_url: 42` used to become the string
  `"42"`; an apt-package's `apt: [7]` used to become `["7"]`; an admin-template's
  `mise_activate: "no"` used to mean **true** and `username: 42` used to become `"42"`. Each is now
  an error. (Adjacent and the same flavor: a vm-template's `cpus: "4"` used to load, and an
  install-command's `command: 7` used to install the string `"7"`.)
- **An explicit `null` is a type error** on a field that is not nullable, rather than a synonym for
  omitting the key. That covers `shell`'s `command`, `resume_command`, and `required_commands`,
  `extra_args` on `claude-code` and `codex`, `codex`'s `writable_dirs`, a github credential's
  `repos`, and a `session-template`'s `env`. If you wrote one to mean "no value", delete the line
  instead.

### Unknown keys are errors now

A key a kind does not declare used to warn and load on, doing nothing. It is now an error naming the
fields the kind does declare.

- **`agent-template` accepted and silently DROPPED `username` and `git_force_safe_directory`.** They
  were in the accepted key set but were not fields, so setting either never took effect. Now they
  stop the load. Deleting them changes no behavior, because they never had any; if you meant them,
  they belong on the `admin-template`.
- **`apt-package`, `apt-source`, `system-install-command`, and `user-install-command` had no
  unknown-key warning at all**, so they go straight from silently accepting a stray key to rejecting
  it, with no warning release in between. If you have a typo in one of these, this is where you will
  meet it.

### One meaning changed rather than one shape

**An explicit `null` secret name now means the DEFAULT secret, on `azure-vm`, `aws-ec2`, and
`proxmox`.** This is the change least likely to announce itself, so read it even if nothing else
here applies.

Those three platforms each name a secret in their config, defaulting to a well-known name when the
field is omitted: proxmox's `token_secret` (default `proxmox-token`), an azure site's
`service_principal.secret` (default `azure-client-secret`), an aws site's
`credentials.access_key_secret` (default `aws-secret-access-key`). Writing an explicit `null` there
used to be a hard error whose message told you to OMIT the key instead. The rule is now that absent
and `null` mean the same thing, so that same input quietly resolves to the default-named secret and
the site declares a dependency on it.

If you ever hit that old error and worked around it, check what you left behind. Nothing warns you,
because to the loader an explicit `null` is now simply the ordinary way of taking the default.

### Two smaller ones

- **An install command's `test_exec: ""` beside a `test_file`** used to be legal: the empty string
  normalized away before the at-most-one-test check counted. It now counts, so the pair is rejected.
  Delete the empty one.
- **`{value: x}` is a new accepted env spelling**, alongside a bare string and `{secret: name}`.
  Additive: nothing you have written stops working, but a config can now say something it could not.

### If you maintain a VM platform outside this tree

`ProvisionRequest` arrives fully resolved. Its `cpus`, `memory_gib`, `disk_gib`, and `swap_gib` are
required and non-optional, so a platform must use what it is handed rather than re-defaulting a
missing value. The defaults are declared once, on the template model. `generate_bootstrap_script`'s
`swap` parameter became required for the same reason.

### Nothing to do

A harness integration's declared secrets carry usage text that used to name
`harness_integration_config`, a key that can no longer be written; it now reads
`harness_integration`. The text appears only in the preflight error for a secret that no active
backend can resolve, and no shipped integration declares a secret, so this is here for completeness
rather than because it will reach you.

## Inspecting the whole picture

```bash
agw resource list --origin operator     # what you have declared, either source
agw resource describe secret/npm-token  # where it's referenced, what uses it
agw doctor                              # health: would every secret resolve?
```

The design rationale (the config/resource split, capability kinds, the vocabulary rules, and the
vm-site / vm-platform pair) is recorded in ADR 0016. Its dual-path section records the original
keep-both-paths stance, since superseded by ADR 0022: YAML manifests are the single
resource-declaration frontend, and `config.toml` is settings only.

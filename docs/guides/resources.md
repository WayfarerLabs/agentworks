# Resources: YAML Manifests, TOML, and the Registry

How agentworks models the things you declare: secrets, templates, git credentials, apt /
install-command entries, and how to work with them day to day.

This guide describes the system as it stands. If you are moving a host from 0.13 to 0.14, the
one-time rewrite that release asks for (retired TOML resource sections, the capabilities that became
opt-in plugins, the manifest validation that tightened) is in
[upgrading-to-0.14.md](upgrading-to-0.14.md).

## The split: config vs resources

`~/.config/agentworks/config.toml` is for **settings**: your identity (SSH keys), paths, CLI
defaults, and the secret source chain (`[secret_config].sources`). Settings configure your install;
they are not named, referenceable entities.

**Resources** are the named things everything else refers to: a `secret` called `npm-token`, a
`vm-template` called `dev`, a `git-credential` called `github`. Every resource lives in the resource
registry, is identified by `kind` + `name`, and can be inspected uniformly:

```bash
agw resource list                       # everything, all kinds and origins
agw resource list --kind secret         # one kind
agw graph show vm-template/dev          # declared and live relationships
agw resource kinds                      # every kind: category, counts, purpose
```

Resources come from four origins: **operator-declared** (you wrote them, as YAML manifests),
**built-in** (shipped with agentworks and inseparable from it, e.g. the `env-var` and `prompt`
secret backends), **auto-declared** (the framework filled in a referenced-but-undeclared resource,
e.g. the `tailscale-auth-key` secret or `git-token-<name>` secrets), and **system-plugin**
(contributed by an installed system plugin, regardless of whether its rows are enabled; see "System
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
  naming the fields it does.
- Multiple documents per file are separated with `---`.

`agw resource sample vm-template` prints a commented starter for one kind (`--all` for every kind);
`--write <file>` saves it under the resources directory instead. Samples are fully commented out:
delete one leading `#` from each DOCUMENT line to activate the parts you want. A saved file also
opens with a `# yaml-language-server:` line, which is an ordinary comment and stays one;
uncommenting that would turn it into a key the loader rejects. Writing a second kind into a file
that already has content appends it under a commented `#---`, which is a document line like any
other: uncomment that too, or the new document's keys merge into whatever precedes them.

The sample is rendered from the same declaration the loader validates against, so it always matches
what the kind actually accepts. Everything in it is commented, but at two depths, and the one `#`
you delete is what tells them apart: a required field carries a single `#` and becomes a live
document line, while an optional field carries two and stays an ordinary comment at its own indent,
with its type, its default or an example, and what it is for. So one uncomment pass over the file
gives you a document that loads, carrying exactly the required fields; then delete a second `#` from
each optional field you actually want. Where a field selects a capability (a vm-site's `platform`),
one implementation is written out and the rest are named beside it.

`agw resource explain` is the same information without a document to edit:

```bash
agw resource explain vm-site               # every field of a kind
agw resource explain vm-platform           # the platforms this build has
agw resource explain vm-platform/aws-ec2   # one platform's own config
```

It reads no config and builds no registry, so it answers on a host whose `config.toml` does not
load, and it documents a capability whose plugin is not enabled yet.

It also answers for every arm of a tagged table, which a sample cannot: where the arms are
capabilities it names each one and gives its address (`agw resource explain vm-platform/wsl2`), and
where they are not (a lima site's `placement: {mode: local}` against `{mode: ssh, host: ...}`) it
shows each arm's own fields under that arm, because no other command reaches them.

`agw resource edit KIND/NAME` opens the manifest declaring a resource in `$EDITOR`.

## Editing manifests with schema support

Agentworks emits JSON Schema (draft 2020-12) for manifests, so a schema-aware editor gives you
completions, hover documentation, and live diagnostics as you type, including for kinds and
capabilities a plugin contributed.

```bash
agw resource schema                    # the any-kind schema, to stdout
agw resource schema vm-template        # one kind's
agw resource schema --install            # the whole set, into resources/.schema/
```

Files that agentworks writes for you already carry the association, as a modeline on their first
line:

```yaml
# yaml-language-server: $schema=.schema/vm-template.schema.json
```

`agw resource sample --write` stamps it on the files it CREATES, and writes the schemas alongside so
the reference resolves. Writing into a file that already exists never INSERTS a line, and what
happens depends on whether one is there already:

- **No modeline?** Nothing is added. A modeline has to be the first line, and inserting one would
  shift every line number you already know. To get the association on a manifest you wrote by hand,
  add that line yourself (`agw resource schema --install` first, so the file it names exists).
- **A modeline already there?** It is rewritten in place, which moves no line at all. A file created
  for one kind names that kind's schema; append a second kind to it and it is no longer a one-kind
  file, so the line is restamped to `manifest.schema.json`, the any-kind schema. Leaving it on the
  first kind's would have your editor check the new document against the wrong shape and underline
  configuration that loads, which is the failure this association exists to prevent.

The schema describes THIS host: a capability from a plugin appears in it once the plugin is
installed, so re-run `agw resource schema --install` after installing one. The schemas are generated
artifacts; `.schema/` is a dot-directory, so the manifest loader never reads what is in it.

**Setting up an editor.** In VS Code (or any editor with a YAML language server), install the
[YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml), open a
manifest under `~/.config/agentworks/resources/`, and you should see completions on `spec` keys and
hover text on each field. To confirm it is really working: change a `spec` key to a name the kind
does not declare, and the editor should underline it immediately. If nothing happens, check that the
first line of the file is the modeline and that the path it names exists.

What the editor checks is a deliberate subset of what loading checks. Everything it flags is a real
error, but agentworks also applies rules the emitted schema does not carry (cross-field validators,
name character rules, whether a capability is registered on this host at all), so a manifest with no
editor diagnostics can still fail to load. The direction is on purpose: a schema that under-reports
costs you a squiggle, while one that over-reports would underline valid configuration.

**The schemas target YAML 1.2**, because that is the version every schema-aware editor parses. The
loader is PyYAML, which is YAML 1.1, and the two versions disagree about three things a manifest can
hold:

- **`yes` / `no` / `on` / `off` are booleans to the loader and plain strings under 1.2.** The
  emitted schemas accept both, so `verify_ssl: no` is not underlined. The cost is that a QUOTED
  `"no"` is accepted by the schema too, and the loader refuses it: once parsed, the two are the same
  string and nothing in the schema can tell them apart. That is the under-reporting direction, so it
  is a squiggle you do not get rather than one you get wrongly. `explain` warns about the quoted
  spelling on every boolean field.
- **1.1 knows more integer spellings than 1.2 does.** Underscore separators (`memory: 8_192`),
  sexagesimal (`1:30`), binary (`0b1010`) and signed hex (`+0x1F`) are integers to the loader and
  plain strings under 1.2. The emitted schemas accept both, so none of them is underlined. Two edges
  are worth knowing about, because neither is something a schema can reach:
  - A LEADING ZERO means octal to the loader and nothing at all to your editor: `memory: 010` loads
    as 8 while your editor reads 10. Both are integers, so no squiggle is possible in either
    direction. Write `10` or `8`; there is no reason to lead with a zero here.
  - `0o17` and `1e3` go the other way. Your editor reads them as numbers, the loader reads them as
    strings, and values are not coerced, so loading fails on a line the editor was happy with.
- **A bare `expires: 2027-01-01` is a date to the loader and a string under 1.2.** This one is not
  expressible: JSON Schema's types are JSON's, and a date is not among them, so no schema can be
  written that a YAML 1.1 checker would accept here. Under 1.2 it is a string and validates cleanly,
  so it costs nothing in a real editor. Quote it (`expires: "2027-01-01"`) if you ever point a 1.1
  validator at your manifests; the loader takes the quoted form too.

## Scoped GitHub credentials (fine-grained PATs)

A `git-credential`'s `spec.provider` is one tagged table: its `name` key selects the provider
capability and the remaining keys are that provider's configuration, which
`agw resource explain git-credential-provider/<name>` documents.

A provider's `token` field is a tagged acquisition choice with one supported arm:
`token: {mode: secret, secret: my-github-token}` names the secret holding the token. Omitting
`token` selects that arm and defaults its secret to `git-token-<credential name>`, while the scalar
`token: my-github-token` is shorthand for the same secret arm. An outer `token: null` is invalid;
omit `token` for the default or write `token: {mode: secret}` explicitly. Inside the secret arm,
omitting `secret` or writing `secret: null` selects the default secret name.

A github credential may carry a scope there, and the choice is the part worth explaining:
`repos: ["owner/name", ...]` pins the credential to specific repositories (always a list, even for
one, matching a fine-grained PAT's selected repos), while `owner: "org"` covers every repository
under that user or org, including repos an agent clones ad hoc that no workspace ever declared.
Writing both takes the union: every exact repository in `repos`, plus every repository under
`owner`. A credential with neither is the unscoped fallback.

Selection lives in the agentworks credential helper: initialization sets `credential.useHttpPath`
(via the managed include `~/.agentworks-git-scopes.gitconfig`), so git hands the helper the remote's
host and repository path, and the helper picks the most specific credential: exact repo, then owner
(first path segment), then the provider's host default (`x-access-token` for GitHub, the org for
Azure DevOps), then the first stored line for the host. Two credentials claiming the same scope is a
configuration error at initialization time, evaluated per user (admin and each agent get their own
store, include, and helper, built from their own credential lists). Declaring a repo under one
credential and its org under another is fine: the more specific scope wins, and org scopes cover
repos cloned ad hoc that nothing declared.

Clone with plain https URLs; no username is needed. The agentworks-owned helper
(`~/.agentworks-git-cred-helper.sh`) identifies a rejected credential and the secret to fix. GitHub
warns when an embedded username bypasses scope selection; Azure DevOps accepts its organization as
the username. If git does not send repository paths, the helper warns and serves the host default.
Remotes are never rewritten.

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
    auth: { mode: ambient }
```

Google Compute Engine uses the same tagged site shape:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: gcp-dev
spec:
  platform:
    name: gcp-gce
    project_id: agentworks-dev
    zone: us-central1-a
    auth: { mode: ambient }
```

See [Using Google Compute Engine](gcp.md) for IAM, API, VPC firewall-policy, credential, and safe
recovery prerequisites.

- `spec.platform` is one table: its `name` key names a `vm-platform` capability row and the
  remaining keys are that platform's configuration, validated by it (unknown keys are errors).
  `agw resource explain vm-platform` lists the platforms this build has, including any that arrive
  with an opt-in [system plugin](#system-plugins); `agw resource explain vm-platform/<name>`
  documents one platform's own fields. A platform needing no config is just
  `platform: {name: wsl2}`. A lima site says where `limactl` runs: `placement: {mode: local}` on
  this machine, or `placement: {mode: ssh, host: user@host}` over SSH.
- The `lima-local` and `wsl2` sites ship built in, `lima-local` on `placement: {mode: local}` and
  `wsl2` on no config at all. Like every site they register on every host and report not-ready where
  this host lacks what they need (wsl2 is Windows-only; a local Lima site needs `limactl`); a
  not-ready site still appears marked in `agw resource list`, `agw doctor` reports the reason, and
  using it is an error. Their names are reserved. A site named after a platform must declare that
  platform.
- Consumers name sites: `agw vm create --site`, `defaults.site` in config.toml, and each VM row's
  `site`. Templates deliberately carry no site: placement is per-host, never template state.
- Site config secrets ride the standard secret machinery: a platform that needs a credential names
  the secret holding it in its own config, defaulting to a well-known name when you leave the field
  out (a Proxmox site's API token is the `proxmox-token` secret unless `token_secret` says
  otherwise). Those secrets are auto-declared and resolved through the configured source chain like
  any other, and `agw resource explain vm-platform/<name>` shows each platform's secret fields with
  their default names.
- **Azure, AWS, and GCP sites say how they authenticate, in a tagged `auth` table that defaults to
  ambient.** `auth: {mode: ambient}` is the declared default, so omitting the table means it: the
  host's own credential chain (for Azure, `az login` / `AZURE_*` / managed identity / browser
  fallback; for AWS, environment, shared config, instance profile, SSO; for GCP, Application Default
  Credentials), which is what each wrapped SDK does when told nothing.
  `auth: {mode: service-principal, ...}`, `auth: {mode: access-key, ...}`, and
  `auth: {mode: service-account, ...}` name an explicit identity. An explicit identity is used and
  only it, so a rejected or expired credential fails the command rather than falling back to the
  ambient chain. The same shape reads back out: an `ambient` site declares no secret and shows no
  secret edge, a credential arm declares exactly the one secret it names, and `agw doctor`'s site
  row shows the resolved mode (`platform azure-vm (auth: ambient)`) whether it was written or
  defaulted. Lima's `placement` works the same way, defaulting to `{mode: local}`. Proxmox has no
  mode selector at all: it has one authentication shape, so it keeps its required token fields,
  which is the pattern (a default where the underlying tool has an ambient notion, required fields
  where it does not).
- The cloud and datacenter platforms ship as opt-in system plugins, so a site that names one is
  not-ready with an "enable plugin `<name>`" hint, and refused at use, until you list that plugin in
  `[plugins] system`. The `azure-dev` example above is not-ready until you set
  `[plugins] system = ["azure"]`. `agw doctor` lists every installed plugin and whether it is
  enabled, and `agw resource explain vm-platform/<name>` says which plugin a platform arrives with.

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
- `agw resource explain harness-integration` lists the integrations this build has, and
  `agw resource explain harness-integration/<name>` documents one integration's config field by
  field. That is the reference; what follows is what an operator wants to know beyond the fields
  themselves.
- Command strings support the `{{session_name}}` and `{{workspace_name}}` variables. This holds
  wherever an integration takes a command or raw arguments (`shell`'s `command` and
  `resume_command`, the `extra_args` escape hatch on `claude-code` and `codex`).
- The integration-plus-config pair inherits as a unit: a child restating the same integration merges
  its config keys into the parent's (child wins per key), while a child naming a _different_
  integration starts fresh. `env`, `inherits`, and the description merge as usual. A few list fields
  union across the chain rather than replacing, so a child adding one entry never silently drops the
  parent's; the field reference marks which.
- `agw graph show harness-integration/<name>` is the other half: the integration's declared and live
  relationships, rather than the fields it accepts.

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
    permission_mode: acceptEdits
    reasoning_effort: high
    vim_mode: true
```

- Its config is all optional, and every field is documented by
  `agw resource explain harness-integration/claude-code`. What the reference cannot tell you: the
  fields that forward a value verbatim to `claude` are not validated here, because the valid choices
  are Claude's and they move between its releases. An invalid one fails at launch with the tool's
  own error, which `session create` / `session resume` capture into their error message when the
  workload exits immediately.
- The only requirement checked on the launch target is that `claude` is installed. The chosen action
  (resume vs new session) is announced in the pane on start, so it is never silent.
- Remote Control needs a Claude subscription login and any organization-level enablement Anthropic
  requires; API-key authentication does not support it. Vim mode and the terminal bell are passed as
  session-local settings, so they do not rewrite the launch user's shared Claude configuration. The
  terminal ultimately decides whether a bell is audible, visual, or ignored.
- `reasoning_effort` sets Claude's effort level for the session. Supported levels depend on the
  selected model; current Claude Code releases expose `low`, `medium`, `high`, `xhigh`, and `max`.

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

Its config is all optional, and `agw resource explain harness-integration/codex` documents every
field. Four things the field reference does not say, because they are Codex's behavior rather than
facts about the fields:

- **Codex sandboxes network access OFF by default**, even under `workspace-write`. A coding session
  that needs `npm install` or `git push` has to turn it on.
- **Who adjudicates an approval escalation** (a sandbox escape, a blocked network call) is a choice.
  Codex documents `user`, the default, where escalations prompt the human in the pane, and
  `auto_review`, where Codex's risk-based reviewer subagent approves or denies instead with the
  sandbox still enforcing the outer boundary. Unattended-leaning "auto" templates usually want
  `auto_review`.
- **Extra writable directories are passed literally**, so use absolute paths: `~` and `$HOME` are
  not expanded.
- **Session preferences can override user config without editing it.** `vim_mode: true` starts the
  composer in Vim normal mode, while `reasoning_effort` forwards Codex's current effort name. Both
  remain optional.
- **Web search has explicit modes.** Use `web_search: cached`, `indexed`, `live`, or `disabled`.
  Legacy `true` still requests live search through `--search`; legacy `false` still emits no
  override, so existing templates keep their behavior. Use `disabled`, not `false`, to force search
  off regardless of the target's profile or `config.toml`.
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
    reasoning_effort: high # optional; forwarded to Codex's model_reasoning_effort setting
    vim_mode: true # optional; start the composer in Vim normal mode
    web_search: cached # optional; cached | indexed | live | disabled
```

`shell` is the built-in default integration; `claude-code` and `codex` ship as the opt-in `claude`
and `codex` system plugins. None of them is the whole set the platform is built around. The
`harness-integration` kind is extensible: another harness or shell runtime, whatever the provider,
is added as its own integration with its own config vocabulary. `claude-code` above (and its
Claude-specific `model` / `permission_mode` fields) is one worked example; the core assumes no
particular runtime, and a session runs whatever integration its template selects.

## Install-command reruns and optional checks

Prefer a VM template's `apt`, `apt_packages`, or `snap` fields for system software and
`mise_packages` for user tools. Use an install-command only when those package paths do not fit. Its
`command` must be one logical shell invocation written as a single-line YAML scalar, either plain or
quoted. Prefer one maintained package-manager or vendor entry point. Embedded scripts, block
scalars, here-documents, multi-step installers, state machines, signature pipelines, and cleanup
routines do not belong in an install-command manifest.

Agentworks runs install commands during init and may run them again during every reinit. Each
invocation must therefore be repeat-safe itself or declare `test_exec`, `test_file`, or `test_dir`
completion checks that reliably skip it after success. When multiple non-empty checks are declared,
all must pass before Agentworks skips the command. With no checks, the command always runs.

A `system-install-command` is VM-wide in scope, but Agentworks executes it as the VM admin user, not
root. The command must explicitly use `sudo` for each step that needs root privileges. A
`user-install-command` runs as the admin or agent user whose template selects it and should not
assume elevation.

`test_exec` resolves a command on `PATH` in the target user's login shell. `test_file` and
`test_dir` check for an existing path; a leading `~` resolves to the target user's home. Declare a
check only when its success proves that no work is needed. For example:

```yaml
apiVersion: agentworks/v1
kind: system-install-command
metadata:
  name: my-tool
spec:
  command: sudo vendor-tool install my-tool
  test_exec: my-tool
```

## Built-ins, plugin catalogs, and overrides

This section is the canonical operator contract for row precedence and dependency enablement.

Built-in resources ship with the app and appear in `agw resource list --origin builtin`. Optional
catalog rows carry the `system-plugin` origin. Override policy is per kind:

- **Apt / install-command kinds** (`apt-source`, `apt-package`, `system-install-command`,
  `user-install-command`): declaring the same name overrides a plugin row, the name is the
  interface, and same-name override is how you customize what `gh` installs. An overridden apt
  package still needs `apt` enabled if it keeps a shipped apt-source dependency. Otherwise declare
  the source too or remove that dependency.
- **Bundled vm-sites** (`lima-local`, `wsl2`): reserved names. Redeclaring one is an error; declare
  a sibling site instead. Like every vm-site they register on every host and report not-ready where
  this host lacks what they need (`agw resource list` marks the row and `agw doctor` carries the
  reason); using a not-ready site is an error naming the requirement. A site naming an UNKNOWN
  platform (a typo, or an uninstalled plugin) is a hard error at load, not a self-disable.
- **The four capability kinds** (`secret-backend`, `vm-platform`, `git-credential-provider`,
  `harness-integration`): registered code, shown as read-only rows. You cannot declare or override
  one. `agw resource explain <capability-kind>` lists the implementations this build has, and naming
  one (`agw resource explain vm-platform/proxmox`) says which system plugin it arrives with, if any.
  Configuration is per consumer rather than per capability: secrets customize per secret via
  `backend_mappings`, platforms configure per site via the `spec.platform` table, and integrations
  configure per session-template via the `spec.harness_integration` table. Every installed platform
  publishes a row regardless of host support: a platform whose host requirements are not met (e.g.
  `wsl2` off Windows) publishes a present, not-ready row (`agw resource list` marks it and
  `agw doctor` shows the reason), and a site referencing it is not-ready rather than erroring.

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
  bundled resource (for example a template's `system_install_commands` naming `az-cli` while `azure`
  is off) is refused at use with the same hint, never an unknown-name error. A disabled plugin's
  resources are hidden from `resource list` and never block an operator's identically-named
  resource, but they are present, so the reference always resolves to the friendly hint.

**Disabled resources are hidden by default.** `agw resource list` omits disabled rows; pass
`--include-disabled` to reveal them. `--origin plugin` narrows the listing to plugin-contributed
rows but still honors the disabled default, so combine it with `--include-disabled` to see a
not-enabled plugin's rows. `agw doctor` has a **System plugins** roster: each installed plugin, its
description, and whether it is enabled. Note the axis distinction: "disabled" is the opt-in state
and hides the row, while a **not-ready** resource (enabled but unable to run on this host) still
lists with its reason.

Every plugin the build installs is disabled until you opt in. `agw doctor`'s **System plugins**
roster is the list for this build, with each plugin's description and its opt-in state;
`agw resource explain <capability-kind>/<name>` says which plugin a given capability arrives with.
Authoring a system plugin is documented in the plugins package README
(`cli/agentworks/plugins/README.md`).

The `apt` plugin owns five shipped apt sources and five apt package sets. The `install-command`
plugin owns six shipped user install commands. They are both disabled by default. Core still owns
the resource kinds, validation, apt source-before-package ordering, idempotent runners, executors,
and initialization. Core also continues to own snap, mise, dotfiles, tmuxinator, and Claude
marketplace/plugin setup. Enable `apt` only for a template selecting its apt catalog, and
`install-command` only for an admin or agent template selecting its shipped user command. Use
`agw doctor` to see their states and `agw resource list --include-disabled` to inspect their rows.

**A capability's config is validated whether or not you can use it here.** Enablement and readiness
gate USE, not validation: a `vm-site` naming the `proxmox` platform has its platform config checked
when the manifest loads, even on a host where the `proxmox` plugin is not enabled, and the same
holds for a resource that is merely not-ready (a `wsl2` site validates off Windows). So a misspelled
key is a hard error naming the fields the capability declares on every host. A resource whose
capability is disabled is instead not-ready with an "enable plugin `<name>`" hint and is refused at
use.

**Which plugins you need follows from what your resources reference.** Enable `apt` for its shipped
apt source and package rows; `install-command` for its shipped user install-command rows;
`onepassword` if a declared secret source selects the `onepassword` backend; `proxmox` if a
`vm-site` uses the `proxmox` platform; `gcp` if you use `gcp-gce` or a template installs the
`gcloud-cli` apt package; `aws` if you use `aws-ec2`; `azure` if you use the `azure-vm` platform,
the `azdo` (Azure DevOps) git-credential provider, or the `az-cli` install-command; and `claude` if
a `session-template` uses the `claude-code` integration or a template installs the `claude` CLI.
Until you do, a resource that references one is not-ready (or refused at use) with an "enable plugin
`<name>`" hint, never a silent failure. The default local path (the `lima` / `wsl2` platforms, the
`shell` harness integration, the `env-var` / `prompt` secret backends, and the `github`
git-credential provider) needs no `[plugins]` entry at all. `agw doctor` lists every installed
plugin and whether it is enabled.

## Secrets: configured sources and implementation backends

Three pieces have separate jobs:

- A **secret backend** is registered code in a read-only `secret-backend` capability row.
  `agw resource explain secret-backend/onepassword` documents that implementation's source config
  and mapping model.
- A **secret source** is a declarable `secret-source` resource. Its `spec.backend` selects and
  configures one backend implementation. Agentworks synthesizes `env-var` and `prompt`; additional
  sources are ordinary YAML resources.
- `[secret_config].sources` lists source names in precedence order. Each `backend_mappings` key is
  also a source name. The default remains `["env-var", "prompt"]`.

For example, a configured 1Password source owns its account and an optional operation timeout:

```yaml
apiVersion: agentworks/v1
kind: secret-source
metadata:
  name: work-op
spec:
  backend:
    name: onepassword
    account: work.example.com
    timeout: 30
```

Enable the `onepassword` plugin, add `work-op` to the chain, and map a secret with
`work-op: op://vault/item/field`. The mapping is always scalar. The synthesized `env-var` and
`prompt` source names remain valid unchanged. A direct configured-backend reference such as
`onepassword` breaks in 0.14; the error gives the exact source declaration and reference rewrite,
with no compatibility row or legacy parser. When rewriting the old OnePassword mapping table, move
its account to the source. The optional timeout is new source configuration and defaults to 30
seconds; it did not move from the old mapping.

### The words the surfaces use

A source and its backend sit on a few independent axes. The surfaces keep them straight, and so
should you when reading them:

- **present**: a source row exists. An unknown chain or mapping name is a configuration error.
- **enabled / disabled**: the opt-in axis (turned on or off). "enabled" and "disabled" mean this and
  only this; they never describe host readiness. A system plugin's contributions are disabled until
  the operator opts in via `[plugins].system` (for example the `onepassword` backend is disabled by
  default); the core backends (`env-var`, `prompt`) are always enabled.
- **ready / not-ready**: whether the configured source can run on this host. A source selecting
  `onepassword` is not-ready when `op` is absent. Readiness is not resolvability.
- **active**: named in `[secret_config].sources`. Only active sources are columns in
  `agw secret list`.
- **would-attempt**: for this secret, the selected backend has a mapping or is mapping-optional. A
  pure function of the secret and its `backend_mappings`, independent of readiness. `won't attempt`
  is a `false` opt-out, or a mapping-required backend (like `onepassword`) with no mapping.

Resolution is a pass over the chain in precedence order: the first source that produces a value
wins. You are never prompted for the same secret twice in one command, and plan-wide prompting
happens up front, before the command starts changing anything. Conditional Tailscale repair remains
lazy so healthy and already-connected paths never ask for a repair key: a stopped VM may start
before late key delivery, then Agentworks validates the key before any rejoin-specific mutation,
transport, installation, or daemon action. The walk considers a candidate only when it is **present,
enabled, ready, active, and would-attempt** the secret.

A **not-ready** active source is **skipped with a warning**, and resolution continues with the next
candidate. A _ready_ store's hard miss stops the chain so a bad mapping cannot fall through to a
prompt. A secret no active source can resolve fails at preflight with a hint, before any prompt or
mutation.

Readiness is offline and honest; it sits UNDER the optimistic interactivity preview. A `prompt` (or
a biometric `op`) is still previewed optimistically on would-attempt alone: the inspection surfaces
never probe an interaction to answer readiness.

`agw secret list` shows, per active source column, the lookup identifier / `would attempt` /
`not ready: <reason>` / `won't attempt`; `agw secret describe <name>` shows one secret in full
(mappings flagged not-ready where they apply, and a resolution preview that skips not-ready
sources); `agw doctor` has a **Secret backends** group (one readiness row per implementation) plus
one non-probing row per secret previewing whether a source could attempt it and whether that source
is ready. Doctor never resolves a secret or reports a runtime resolution outcome.

Use `agw secret verify NAME...` when you need proof rather than a preview. It deduplicates names in
first-written order, performs one real batch resolution, and renders one value-free row per unique
name. Each row reports category, source, safe identifier, typed detail, and remediation. An
all-resolved batch exits 0; if any row is not `resolved`, the full table is still rendered and the
command exits 1.

Interactive sources are refused by default. Add `--allow-interaction` only when you consent to a
prompt, biometric check, or backend authentication. That opt-in is incompatible with the global
`--non-interactive` flag. Guide rendering and readiness or preview rows do not grant that consent.

## Inspecting the whole picture

```bash
agw resource list --origin operator     # what you have declared, either source
agw graph show secret/npm-token         # where it's referenced, what uses it
agw doctor                              # offline secret attempt/readiness preview
```

### JSON for automation

The read-only graph, resource, secret, and health commands also support `--output json`:
`graph show`, `resource list`, `resource kinds`, `secret list`, `secret describe`, and `doctor`.
Each successful response is one JSON document with `schema_version`, `command`, and `data` fields.
The backend lists and reference arrays retain their operational precedence and graph order, and the
secret views report only lookup prediction and metadata, never a secret value.

`--output human` is the default and keeps the terminal-oriented rendering. `--names-only` remains
reserved for shell completion, so it cannot be combined with JSON output. `agw doctor --output json`
still exits 1 when its complete report contains failed checks, after writing that report.

The [CLI JSON v1 reference](../../cli/command-reference.md#machine-readable-output) documents the
exact envelopes, fields, null rules, ordering, error behavior, and compatibility policy. Doctor's
JSON diagnostic message and hint fields are the same structured facts shown by the human renderer.
They can therefore contain configuration paths, backend responses, exception text, or other
troubleshooting detail that the human report exposes.

ADRs 0016 and 0022 record the design of the config/resource split, capability kinds, and YAML as the
resource-declaration frontend.

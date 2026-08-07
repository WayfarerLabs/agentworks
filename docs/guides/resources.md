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

`agw resource sample --write` stamps it on the files it CREATES, and writes the schemas alongside so
the reference resolves. Writing into a file that already exists never INSERTS a line, and what
happens depends on whether one is there already:

- **No modeline?** Nothing is added. A modeline has to be the first line, and inserting one would
  shift every line number you already know. To get the association on a manifest you wrote by hand,
  add that line yourself (`agw resource schema --write` first, so the file it names exists).
- **A modeline already there?** It is rewritten in place, which moves no line at all. A file created
  for one kind names that kind's schema; append a second kind to it and it is no longer a one-kind
  file, so the line is restamped to `manifest.schema.json`, the any-kind schema. Leaving it on the
  first kind's would have your editor check the new document against the wrong shape and underline
  configuration that loads, which is the failure this association exists to prevent.

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

**The schemas target YAML 1.2**, because that is the version every schema-aware editor parses. The
loader is PyYAML, which is YAML 1.1, and the two versions disagree about three things a manifest can
hold:

- **`yes` / `no` / `on` / `off` are booleans to the loader and plain strings under 1.2.** The
  emitted schemas accept both, so `verify_ssl: no` is not underlined. The cost is that a QUOTED
  `"no"` is accepted by the schema too, and the loader refuses it: once parsed, the two are the same
  string and nothing in the schema can tell them apart. That is the under-reporting direction, so it
  is a squiggle you do not get rather than one you get wrongly. `describe-kind` warns about the
  quoted spelling on every boolean field.
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
`agw resource describe-kind git-credential-provider/<name>` documents. (The old sibling shape, a
`provider:` string plus a `provider_config:` table, is no longer accepted; fold the pair into the
tagged table, which the load error spells out for your own document.)

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
`config.toml` that still carries any of them is a hard error at load, naming the offending sections.
This was deprecated with a load-time warning in an earlier release and is now removed. Resources are
declared as YAML manifests (see "Declaring resources" above); settings sections load exactly as
before.

**Upgrading.** This is a breaking change, and the rewrite is yours to make. Agentworks ships no
migration command. What it ships instead is an error naming every offending section, two commands
that render the target shape live from this build's registry, and this section. If you would rather
delegate the work than do it by hand, "Handing the rewrite to an agent" below is the same procedure
written as a brief.

### First: delete every `[secret_backends.*]` section

Do this before anything else, because it is the one leftover that can stop the commands the rest of
this section depends on. A `[secret_backends.<name>]` section naming anything other than a built-in
backend (`env-var`, `prompt`) is a hard error in the **settings** load. That is a separate check
from the resource-section refusal, and the commands that go on working while `config.toml` still
carries resource sections do so by skipping the resource check only. They still run the settings
load, so they still meet this:

```console
$ agw resource schema --write
Configuration error: [secret_backends.onepassword] names an unknown secret backend; supported: ['env-var', 'prompt']

$ agw resource sample secret --write secrets.yaml
Configuration error: [secret_backends.onepassword] names an unknown secret backend; supported: ['env-var', 'prompt']
```

`agw doctor` is hit by it too, and more quietly: its settings-only retry fails the same way, so the
report truncates to the resource-section fail row with every later group reporting `skipped`, and
nothing on screen says a `[secret_backends.*]` section is the reason. `agw resource list` never
shows this error at all, because the resource-section refusal fires first. So a section you can only
see by reading `config.toml` takes out the two commands you start with and the one you iterate with.

Plugin state has nothing to do with it. The check runs at config load against the built-in backend
registry, which no plugin has been loaded into yet at that point, so `[secret_backends.onepassword]`
fails identically whether or not `onepassword` is listed in `[plugins] system`, and on a host where
the plugin is enabled and perfectly healthy (delete the section and `agw doctor` will say so:
`[ok] plugin onepassword`). This is not new in this release: the previous build refuses the same
section the same way.

A section naming a backend this build does have (`[secret_backends.env-var]`) only warns rather than
failing, but delete those in the same pass. They never carried configuration, and what replaces them
is `[secret_config].backends`; "The sections that are not a straight move" below has the detail.

### What still answers while `config.toml` is refused

Read this before you start, because it decides the order. The resource-section refusal happens at
config load, so every command that builds the registry meets it, but they do not all react the same
way:

- **`agw resource describe-kind <target>` reads no config at all.** It answers on a host whose
  `config.toml` does not load, and it documents kinds and capability implementations whose plugin is
  not enabled.
- **`agw resource sample <kind>`, and `--write <file>`, load settings only.** They work against a
  `config.toml` that still carries every section you are about to delete.
- **`agw resource schema --write` loads settings only too.** Run it early: a schema-aware editor
  then checks each manifest as you type it (see "Editing manifests with schema support" above).
- **`agw doctor` reports the refusal as one fail row and keeps going.** It retries the load
  settings-only, which skips the resource-section check, and then validates the manifests you have
  written so far. That makes it a working feedback loop for the whole rewrite, not just the finish
  line.
- **`agw resource list` and `agw secret list` do refuse outright.** They print the error and exit,
  so they only answer once the last resource section is gone.

So there are two different rhythms here, and conflating them is what makes this rewrite feel harder
than it is:

- **`config.toml` is all-or-nothing.** A file carrying one leftover section fails exactly like one
  carrying ten, so you cannot delete one kind's sections, confirm it loads, and move on to the next.
  Every section comes out in one pass, at the end.
- **Your manifests are checked one at a time, starting now.** `agw doctor` names the file, the line,
  the resource, the offending field, and the field list that field should have come from, all while
  `config.toml` is still untouched:

```console
$ agw doctor    # config.toml still declares every section
Configuration:
  [ok]   Config file: /home/you/.config/agentworks/config.toml
  [FAIL] Config: config.toml declares resources, which config.toml no longer supports (it is
         settings only now): [secrets.*], [vm_templates.*], [agent_templates.*], ...
  [FAIL] Manifest: ~/.config/agentworks/resources/vm-templates.yaml:1: vm-template/default.memory_gib:
         unknown field; expected one of: apt, apt_packages, cpus, disk, env, inherits, memory,
         snap, swap, system_install_commands, tailscale_auth_key
         hint: `agw resource sample vm-template` prints this kind's fields

Results: 15 ok, 7 info, 1 warn, 2 fail
```

Write a manifest, run `agw doctor`, fix what it names, repeat. Once every manifest is clean the
Config row is the only fail left, and doctor starts rendering the **Secret backends** and
**Secrets** groups from your manifests, which is a preview of the finished state you get before
deleting a single section.

### The inventory is the error message

Any registry command names the full list of sections in one pass, so it is your work list:

```console
$ agw resource list
Configuration error: config.toml declares resources, which config.toml no longer supports (it is
settings only now): [secrets.*], [vm_templates.*], [session_templates.*], [git_credentials.*],
[proxmox]. Rewrite the sections as YAML manifests (the [azure]/[proxmox] sections become vm-site
manifests), then remove the sections from config.toml.
  Hint: `agw resource sample <kind> --write <kind>s.yaml` writes a commented starter to edit, and
  `agw resource describe-kind <kind>` lists every field with its type. The "TOML resource sections:
  removed" section of docs/guides/resources.md walks through it section by section.
```

Each retired section maps to exactly one kind. Where the section name does not say which, this is
the whole table:

| Retired TOML section          | Becomes kind             |
| ----------------------------- | ------------------------ |
| `[secrets.*]`                 | `secret`                 |
| `[vm_templates.*]`            | `vm-template`            |
| `[agent_templates.*]`         | `agent-template`         |
| `[workspace_templates.*]`     | `workspace-template`     |
| `[session_templates.*]`       | `session-template`       |
| `[git_credentials.*]`         | `git-credential`         |
| `[admin.config]`              | `admin-template`         |
| `[named_console]`             | `named-console-template` |
| `[azure]` / `[proxmox]`       | `vm-site`                |
| `[apt_sources.*]`             | `apt-source`             |
| `[apt_packages.*]`            | `apt-package`            |
| `[system_install_commands.*]` | `system-install-command` |
| `[user_install_commands.*]`   | `user-install-command`   |
| `[secret_backends.*]`         | nothing; see below       |

### The rule, with one section worked through

One TOML section becomes one YAML document. The section's name becomes `metadata.name`, its
`description` moves to `metadata.description`, and every other key moves into `spec` keeping its
spelling:

```toml
# config.toml, before
[secrets.npm-token]
description = "npm registry token"
hint = "Generate at https://www.npmjs.com/settings/me/tokens"
backend_mappings = { env-var = "NPM_TOKEN" }
```

```yaml
# resources/secrets.yaml, after
apiVersion: agentworks/v1
kind: secret
metadata:
  name: npm-token
  description: npm registry token
spec:
  hint: Generate at https://www.npmjs.com/settings/me/tokens
  backend_mappings:
    env-var: NPM_TOKEN
```

Two things move rather than copy across: `name` was never a key, it was the second half of the
section header, and `description` is the one key that leaves the section body for `metadata`. A
`vm-template` is the same move with no surprises at all: `[vm_templates.dev]` with `cpus = 4` and
`memory = 16` becomes a `vm-template` named `dev` whose `spec` carries `cpus: 4` and `memory: 16`.

**A section whose only key is `description` still needs a `spec`.** Because `description` is the one
key that leaves for `metadata`, applying the rule to `[secrets.npm-token]` with nothing but a
`description` empties `spec` out of existence, and a document with no `spec` is refused:

```console
[FAIL] Manifest: ~/.config/agentworks/resources/secrets.yaml:11: spec is required (an empty mapping {} is fine)
```

Write `spec: {}` and the document loads. This is the ordinary shape for a secret you only want named
and described (the value comes from a backend, so there is nothing else to say about it), so expect
to write it several times:

```yaml
apiVersion: agentworks/v1
kind: secret
metadata:
  name: npm-token
  description: npm registry token
spec: {}
```

`agw resource describe-kind <kind>` is the authority on what a `spec` accepts, and it is worth
running per kind rather than assuming: several field names read like something they are not (a
vm-template sizes memory with `memory`, in GiB, not `memory_gib`).

For the three kinds whose `spec` carries a tagged capability table (`vm-site`'s `platform`,
`git-credential`'s `provider`, `session-template`'s `harness_integration`), the kind's own output
documents only ONE implementation's fields inline, the one it shows as an example, and lists the
rest by name. `agw resource describe-kind <capability-kind>/<name>` is the form that documents a
specific one:

```bash
agw resource describe-kind vm-platform/proxmox            # for [proxmox]
agw resource describe-kind vm-platform/azure-vm           # for [azure]
agw resource describe-kind git-credential-provider/azdo   # for a git_credentials section with provider = "azdo"
agw resource describe-kind harness-integration/claude-code
```

Reach for it whenever the section you are rewriting selects an implementation other than the one
shown inline. `describe-kind vm-site` renders lima's fields, so a `[proxmox]` section rewritten from
that output alone will be missing fields; `describe-kind git-credential` renders github's, so azdo's
required `org` never appears. The kind's output names the form for you at the end of the table's
entry, and a wrong-fields error carries it as a hint.

### Where to put the files

Every `*.yaml` and `*.yml` file under `~/.config/agentworks/resources/` is loaded, including files
in subdirectories, and a single file may hold many documents separated by `---`. Nothing keys off
the file name, so the layout is genuinely yours: one file per kind (`secrets.yaml`,
`vm-templates.yaml`), one file per resource, or one file for everything all behave identically.
Dot-directories are skipped, which is why the generated `.schema/` directory sitting in there is
never read as configuration.

One file per kind, named after the kind, is the layout that makes this rewrite easiest to check:
each `config.toml` section family lands in exactly one file, so "did I move all of it" is answerable
by looking at one place. `agw resource sample <kind> --write <kind>s.yaml` is built for that shape,
and it is safe to point at a file that already exists: a second kind is appended under a commented
`#---`, and the file's schema modeline is restamped from the one kind's schema to the any-kind
`manifest.schema.json`. Both of those are document lines you must uncomment, so re-read "Declaring
resources: YAML manifests" above before editing an appended file.

### The sections that are not a straight move

- **`[azure]` and `[proxmox]`** become `vm-site` manifests. The section name becomes the resource
  name, and the section's keys move inside the tagged `spec.platform` table rather than sitting
  directly under `spec`. Take the platform's `name` from `agw resource describe-kind vm-platform`
  rather than from the section header: `[proxmox]` does select the `proxmox` platform, but `[azure]`
  selects `azure-vm`, so only one of the two matches its old section name.

  ```toml
  # config.toml, before
  [proxmox]
  api_url = "https://pve.example.com:8006"
  node = "pve1"
  token_id = "agentworks@pam!agw"
  template_vmid = 9000
  ```

  ```yaml
  # resources/vm-sites.yaml, after
  apiVersion: agentworks/v1
  kind: vm-site
  metadata:
    name: proxmox
  spec:
    platform:
      name: proxmox
      api_url: https://pve.example.com:8006
      node: pve1
      token_id: agentworks@pam!agw
      template_vmid: 9000
  ```

  Both platforms ship as opt-in system plugins now, so the site loads but reports not-ready until
  you add `proxmox` (or `azure`) to `[plugins] system`. See "VM sites and platforms" above.

- **`[git_credentials.*]`** folds three flat keys into one tagged `spec.provider` table: the
  section's `provider` (or the older `type`) becomes the table's `name`, and `token` and the
  provider's own keys (azdo's `org`) join it inside:

  ```toml
  # config.toml, before
  [git_credentials.github]
  provider = "github"
  token = "gh-pat"
  ```

  ```yaml
  # resources/git-credentials.yaml, after
  apiVersion: agentworks/v1
  kind: git-credential
  metadata:
    name: github
  spec:
    provider:
      name: github
      token: gh-pat
  ```

  `token` is the one to look at twice: it names a secret, it is not the token value, and inside the
  table it sits beside `name` rather than under a config sub-table. Omitting it still defaults to
  `git-token-<credential name>`. **If a github section carried `repos` or `owner`, note that TOML
  ignored them and provisioned the credential unscoped**, warning as it did so. Writing them in the
  manifest is therefore a real change: the credential becomes scoped, which is what those keys
  always looked like they did. See "Scoped GitHub credentials" above before you carry them over.

- **`[session_templates.*]`** with a flat `command` / `resume_command` / `required_commands` trio
  hoists that trio into the `shell` harness integration, because those keys were always `shell`'s
  config:

  ```toml
  # config.toml, before
  [session_templates.htop]
  description = "Live process monitor"
  command = "htop"
  required_commands = ["htop"]
  ```

  ```yaml
  # resources/session-templates.yaml, after
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

  A section that already named the harness explicitly moves that value to the table's `name` and
  lifts its config keys in beside it. Which config key that is depends on which spelling the section
  used, and the two always came as a pair: `harness_integration` went with a
  `harness_integration_config` table, and the older `harness` went with a `harness_config` table
  (`[session_templates.claude]` with `harness = "claude-code"` plus a
  `[session_templates.claude.harness_config]` carrying `model = "opus"` becomes one
  `harness_integration` table with `name: claude-code` and `model: opus`). A section with a
  `restart_command` renames it to `resume_command` on the way. See "Harness integrations" above.

- **`[admin.config]` and `[admin.env]`** become one `admin-template` named `default`, with the env
  table nested as `spec.env`.

- **`[named_console]`** becomes a `named-console-template` named `default`.

- **`[secret_backends.*]`** becomes nothing at all. Those sections never carried configuration:
  delete them, and list the backends you want in `[secret_config].backends`, which is a setting and
  stays in `config.toml`. These are the sections "First: delete every `[secret_backends.*]` section"
  above told you to remove before anything else, and that ordering is the point: they are not part
  of the resource-section error, they are checked by the settings load instead, so one naming a
  backend that is not built in (`[secret_backends.onepassword]`) breaks the very commands you need
  to do the rewrite. One naming a built-in backend (`[secret_backends.env-var]`) only warns, but it
  is dead weight either way.

### Deleting the sections, and knowing you are done

Only once every manifest is written, delete every resource section from `config.toml` in one pass,
leaving the settings sections (`[operator]`, `[paths]`, `[defaults]`, `[session]`,
`[secret_config]`, `[plugins]`) untouched. Then work through what the registry says, in this order:

```bash
agw resource list --origin operator   # every resource you declared, and the file each came from
agw doctor                            # the full health picture
```

`agw resource list --origin operator` is the loss check: a resource that silently failed to move
does not appear here. Read it against your saved copy of `config.toml` by NAME, not by count. A
count of section headers will not match, and chasing the difference sends you hunting for resources
that never existed. Two header shapes are headers but not resources:

- A deeper sub-table belongs to the resource above it. `[vm_templates.default.env]`, `[admin.env]`,
  and `[session_templates.claude.harness_config]` are each a second header for a resource the family
  already declared, not a second resource.
- `[secret_backends.*]` becomes nothing at all, as above.

What does match, family by family, is the set of NAMES. The second path element of a
`[family.<name>]` header is one resource of that family's kind, and a `--names-only` listing prints
exactly that, so the two are directly comparable:

```bash
# the names your old config declared under one family (deeper sub-tables collapse)
grep -oE '^\[secrets\.[^].]+' config.toml.bak | cut -d. -f2 | sort -u
# the names the registry has for the kind that family became
agw resource list --origin operator --kind secret --names-only | cut -d/ -f2
```

Run that pair once per family in the load error, taking the family's kind from the table above. Four
sections have no name in the header to compare: `[admin.config]` and `[named_console]` each become
one resource named `default`, and `[azure]` and `[proxmox]` each become one `vm-site` named after
the section, so for those just confirm the row is present.

Then read the ORIGIN column: every row should name the file you expect. A resource in the right kind
but the wrong file usually means a document landed under a heading you did not intend.

If you have been running `agw doctor` as you wrote the manifests, this should pass first time. If
you skipped that and are meeting the errors now, expect several passes: errors aggregate within a
single resource but not across resources, because the load stops at the first document that fails,
so six broken manifests take six passes rather than producing one list of six. Each pass names a
file, a resource, and a field.

`agw doctor` is the finish line. You are done when its **Configuration** group reports
`Config is valid` and the run ends with `0 fail`:

```console
Configuration:
  [ok]   Config file: /home/you/.config/agentworks/config.toml
  [ok]   Config is valid

Secrets:
  [ok]   Secret 'gh-pat' (auto): would resolve via prompt
  [ok]   Secret 'npm-token': would resolve via prompt

Results: 18 ok, 11 info, 0 warn, 0 fail
```

Read the **Secrets** group rather than skimming it. Every secret your manifests reference appears
there, including the ones nothing declared explicitly: a git credential's `token` and a platform's
token secret are auto-declared, and they are marked `(auto)`. A secret you expected to be named and
described appearing as `(auto)` instead usually means a `spec` key did not move where you thought it
did. `info` rows are not failures; a not-ready site whose plugin is off is reported there by design.

### Handing the rewrite to an agent

The rewrite is mechanical but it has to be done against what this build actually accepts, which is
exactly the sort of work worth delegating to a coding agent with shell access: it can run
`describe-kind` per kind and read the real field list instead of guessing, and it can iterate on the
per-resource errors until `agw doctor` is clean.

Agentworks does not yet ship a command that drives this conversation for you, so give the agent the
procedure above as its brief. What matters is that it gets the constraints, not just the goal:

- Every `[secret_backends.*]` section comes out first, before anything else is run. One naming a
  non-built-in backend fails the settings load and takes `sample --write` and `schema --write` down
  with it, whatever `[plugins] system` says.
- The work list is the load error from `agw resource list`, and the target shape comes from
  `agw resource describe-kind <kind>` per kind. Field names are to be read from that output, never
  recalled. Where a `spec` selects a capability implementation, the fields come from
  `agw resource describe-kind <capability-kind>/<name>` (`vm-platform/proxmox`,
  `git-credential-provider/azdo`), because the kind's own output only details one implementation.
- `describe-kind`, `sample`, and `schema --write` work while `config.toml` is refused. `list` and
  `secret list` do not. `agw doctor` DOES: it reports the refusal as one fail row and goes on to
  validate the manifests written so far, so it is the iteration loop, not just the final check.
- `config.toml` is all-or-nothing, so every resource section comes out in one pass at the end. That
  is not a reason to defer verification: run `agw doctor` after each manifest.
- Settings sections stay in `config.toml`. Only resource sections move.
- Done means `agw resource list --origin operator` matches the old config family by family, by name
  (not by header count), with the expected origin per row, and `agw doctor` reports
  `Config is valid` with `0 fail`.
- The original `config.toml` is worth keeping a copy of until `agw doctor` is clean, since nothing
  in this process rewrites your files for you. The name-by-name check needs that copy.

**Manifests on a retired shape.** Separately from the TOML sections, a manifest that names a
capability in the old sibling shape (`platform: lima` plus a `platform_config:` table, and likewise
`provider` / `provider_config`) does not load either. The error names the replacement, built from
your own document:

```console
$ agw resource list
Configuration error: resources/git-credentials.yaml:1: spec.provider names the capability as a
string, which is no longer supported; write one tagged table instead: provider: {name: github,
token: ..., owner: ...}
```

The keys it lists are the ones your document actually had, in order, with the values elided as
`...`, so it tells you the shape to write and you fill your own values back in. Every document on
the old shape has to move: one left behind leaves the whole resources directory unloadable.

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
  `platform_config:` table) is no longer accepted; fold the pair into the tagged table, which the
  load error spells out for your own document.
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
  they are a hard error in `config.toml` now. Each becomes a `vm-site` manifest whose platform table
  is named after the section and carries the section's keys unchanged.

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

**A capability's config is validated whether or not you can use it here.** Enablement and readiness
gate USE, not validation: a `vm-site` naming the `proxmox` platform has its platform config checked
when the manifest loads, even on a host where the `proxmox` plugin is not enabled, and the same
holds for a resource that is merely not-ready (a `wsl2` site validates off Windows). So a misspelled
key is a hard error naming the fields the capability declares, on every host, rather than something
that lies dormant until you opt in. That is deliberate: a typo that only surfaced on the one machine
that had the plugin turned on would be a configuration error you carry around unnoticed. What
enablement and readiness DO defer is the consequence, not the check: a resource whose capability is
disabled is not-ready with an "enable plugin `<name>`" hint and is refused at use, never misreported
as a config mistake.

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

### How to work through it

Upgrade, run any command, and fix what it names. Repeat until commands stop complaining, then run
`agw doctor` for the full health picture.

**Expect one resource per pass.** Errors aggregate WITHIN a resource: a document with three problems
reports all three at once. They do not aggregate ACROSS resources, because the load stops at the
first resource that fails. `agw resource list` and `agw doctor` both behave this way, so six broken
resources is six passes rather than one list of six. Aggregation is also per check rather than per
document, so one document can report twice across two passes: a bad `spec` key is caught when the
document is decoded, while a bad key inside a capability's config table is caught by a later
validation pass, and fixing the first is what lets you see the second.

**The line number is the document's, not the mistake's.** Each error names the file, the resource,
the field, and what was expected, and the `file:line` is where that DOCUMENT starts. Look for the
named field inside the document at that line rather than at the line itself. One case points
somewhere else on purpose: when the offending value was inherited, the error names the row that
DECLARED it and adds an `(inherited from <kind>/<name>)` tail, so the file and line are the parent's
rather than the child you ran into it through.

**Two commands keep working when nothing else does.** `agw resource describe-kind <kind>` (and
`<capability-kind>/<name>`) documents every field the kind accepts, and `agw resource sample <kind>`
prints the same fields as a document to edit. Neither reads your config, so both answer while it is
unloadable. That is why every error points at them.

### Every host checks every declaration now

Validation no longer depends on whether you can actually use the thing being validated. Every
declared resource's capability config is checked whenever the manifest loads, regardless of whether
the resource is ready on this host, whether its plugin is enabled, and whether anything ever
references it. Only the document and the model the capability declares decide the answer, so it
cannot vary between machines.

This is the change most likely to surface on a host that was quiet before. A misspelled key in a
`proxmox` site's platform config used to sit dormant on a laptop that never enabled the `proxmox`
plugin, and a `wsl2` site's config went unchecked anywhere except Windows. Both are hard errors
everywhere now, so a config you have been carrying for months can fail on the first machine you
upgrade even though nothing about that machine uses it. The fix is the same as any other unknown
key: `agw resource describe-kind vm-platform/<name>` names the fields, and the error already does
too.

What did NOT change is readiness and enablement themselves. A site whose plugin is off is still
not-ready with an "enable plugin `<name>`" hint and still refused at use; it is only the question of
whether its config is well-formed that stopped being host-dependent.

### The retired sibling capability shape

`platform: lima` beside a `platform_config:` table (and likewise `provider` / `provider_config`) is
a hard error. The error prints the replacement, built from what your document says: the capability
you named plus the keys you wrote, folded into one tagged table. The keys appear in your document's
order with their values elided as `...`, so what you get is the shape to write rather than a line to
paste unedited; put it in place of the pair and fill your own values back in. "TOML resource
sections: removed" above shows one of these errors in full.

Two documents get no printed replacement, because no honest one exists. If the `*_config` table
carries its own `name` key, two keys claim to select the capability and which one wins is yours to
decide. If it holds something that is not a table, there are no keys to fold and printing the tag
alone would discard what you wrote. Both errors say so and name the field.

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

- **An explicit `null` is a type error** on any field the kind does not declare nullable, rather
  than a synonym for omitting the key. If you wrote one to mean "no value", delete the line instead.
  The split is worth knowing rather than memorizing, because the four inheriting template kinds are
  the exception: THEIR own scalars are nullable by design, where `null` means "not set here,
  inherit". Almost nothing else is. Their `env` tables and `inherits` lists are not nullable, and
  neither is anything on the `admin-template`, the apt kinds, the install-command kinds, or a
  harness integration's or platform's config block. The ones most likely to be sitting in an
  existing file are `shell`'s `command`, `resume_command`, and `required_commands`, `extra_args` on
  `claude-code` and `codex`, `codex`'s `writable_dirs`, a github credential's `repos`, and a
  `session-template`'s `env`. `agw resource describe-kind` marks each field "or null" when null is
  legal, so it settles any case not listed here.

**Every quoted boolean meant `true`.** That is the whole class, not just the three named above:
`key_dearmor`, `tmuxinator`, and `mise_activate` / `mise_allow_unlocked` / `mise_prune_on_reinit` /
`git_force_safe_directory` on both template kinds, plus proxmox's `verify_ssl`. `describe-kind` says
so on each of them. Writing the `false` that the line looks like it asked for silently INVERTS the
behavior you have been running; writing `true` preserves it.

**`verify_ssl` is the one where that is a decision rather than a correction.** `true` preserves what
you have actually been running: verification on, and passing, or the cluster would not have worked.
`false` is what the line looks like it was asking for, and is right only if you know the cluster's
certificate is self-signed. If your setup has been working, `true` is the answer and `false` would
be loosening something that did not need loosening.

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

The rule itself is general: every field that names a secret and has a default reads absent and
`null` alike. These three are called out because they are the ones whose behavior CHANGED. A git
credential's `token` (default `git-token-<name>`) follows the same rule but always did, so there is
nothing to look for there.

**If one of those lines is still in your file, you are the person this affects.** Nothing warns you,
because to the loader an explicit `null` is now simply the ordinary way of taking the default, and
nothing downstream can tell it apart from an omitted key: both resolve to the default-named secret,
so `agw doctor` and `agw resource describe` look identical either way. Your file is the only place
the evidence survives, which is why this one is a scan rather than a check:

```bash
grep -rniE '(token_secret|access_key_secret|secret):[[:space:]]*(null|~)?[[:space:]]*([,}]|$)' \
  ~/.config/agentworks/resources
```

Three details in that pattern are load-bearing, because YAML has more ways to write a null than the
obvious one. `-i` catches `Null` and `NULL`, which the loader reads as null exactly like `null`.
Ending on `[,}]` as well as end-of-line catches a field written inside a flow mapping
(`service_principal: { client_id: cid, secret: null }`), where the line does not end after the
value. And the optional `(null|~)?` catches a bare key with nothing after it, which is a null too.

Treat a clean result as good evidence rather than as proof. It is a line-oriented scan of a format
that does not have to be line-oriented, so it will still miss a quoted key (`"secret": null`) or a
value pushed onto its own continuation line. If you use one of these three platforms, it is worth
opening the site's manifest and looking at the field directly.

Then decide, per hit:

- If you meant "no secret here", **delete the line instead**. That is the same answer as the other
  null fields above, and it is now the only spelling that means it.
- If you meant the default all along, leave it. Nothing changes for you except that the default is
  now declared as a dependency, which `agw resource describe secret/proxmox-token` will show and
  `agw doctor` will check.

Nothing rewrites your files, so the evidence stays where you left it: that `grep` answers the same
before and after every other fix here.

### Multi-parent inheritance resolves differently

Two fixes to how `inherits` chains fold land together, on all four template kinds (`vm-template`,
`agent-template`, `workspace-template`, `session-template`). Neither reports anything: your
manifests load exactly as before and simply resolve to different values, so this and the null-secret
change above are the two to go looking for rather than wait for.

- **A parent that declares nothing no longer overwrites one that did.** Each parent used to be
  resolved on its own first, which meant its DEFAULTS were applied as though it had asked for them.
  So `inherits: [base, extras]`, where `extras` sets no fields, used to reset every scalar `base`
  had set back to the built-in default. It now contributes only what it actually declares. If you
  have been compensating for this (restating a parent's values in the child, or ordering `inherits`
  to work around it), the compensation is now redundant and may itself be changing the answer. A
  `vm-template` chain that sets `tailscale_auth_key` is the one worth checking first: the old
  behavior could leave the graph depending on the default-named secret while VMs provisioned with a
  different one.
- **A template reached by two routes now applies once, at its earliest position.** In a diamond,
  where `kid` inherits `a` and `b` and both inherit `root`, the chain used to replay `root` a second
  time after `a`, so a field that `root` and `a` both declared resolved to ROOT's value: a
  grandparent landing back on top of the parent that overrode it. The chain is linearized now, so
  the same field resolves to `a`'s, which is what "nearest wins" always described. Only diamonds are
  affected; a single-parent chain resolves exactly as it did.

Both are corrections, so in most cases the new answer is the one you meant. That does not make them
safe to ignore: nothing warns, and the value that changes is the one being used. Check any template
whose `inherits` names more than one parent, and any parent that declares little or nothing.

### Two smaller ones

- **An install command's `test_exec: ""` beside a `test_file`** used to be legal: the empty string
  normalized away before the at-most-one-test check counted. It now counts, so the pair is rejected.
  Delete the empty one; the error names which key that is, and says to delete it rather than blank
  it.
- **`{value: x}` is a new accepted env spelling**, alongside a bare string and `{secret: name}`.
  Additive: nothing you have written stops working, but a config can now say something it could not.

### If you maintain a VM platform outside this tree

`ProvisionRequest` arrives fully resolved. Its `cpus`, `memory_gib`, `disk_gib`, and `swap_gib` are
required and non-optional, so a platform must use what it is handed rather than re-defaulting a
missing value. The defaults are declared once, on the template model. `generate_bootstrap_script`'s
`swap` parameter became required for the same reason.

Note that the request's field names are not the manifest's. A `vm-template` spec writes `memory`,
`disk`, and `swap` (all in GiB), and those resolve into `memory_gib`, `disk_gib`, and `swap_gib` on
the request; only `cpus` is spelled the same on both sides. Grepping for one set and expecting to
find the other is the easy mistake here.

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

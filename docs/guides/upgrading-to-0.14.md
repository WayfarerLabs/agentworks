# Upgrading to 0.14

Everything an operator has to change to cross the 0.14 release boundary: the retired TOML resource
sections, the vendor capabilities that became opt-in system plugins, the vm-site fields that now
state what they used to infer, and the manifest validation that tightened. It is a companion to
[resources.md](resources.md), which describes the system as it stands and assumes none of this is in
flight.

**This guide is release-scoped.** It exists only to carry hosts from 0.13 to 0.14, and it is deleted
outright a release or two after 0.14 ships, along with the compatibility errors that point at it.
Nothing here describes permanent behavior; if you are setting up a new host, or reading to
understand how resources work, you want [resources.md](resources.md) instead.

## TOML resource sections: removed

Declaring resources in `config.toml` is no longer supported. `config.toml` is settings only. The
classic TOML resource sections (`[secrets.*]`, `[vm_templates.*]`, `[git_credentials.*]`, the legacy
flat `[azure]` / `[proxmox]` vm-site sections, `[apt_sources.*]`, and the rest) no longer load: a
`config.toml` that still carries any of them is a hard error at load, naming the offending sections.
This was deprecated with a load-time warning in an earlier release and is now removed. Resources are
declared as YAML manifests (see
["Declaring resources: YAML manifests"](resources.md#declaring-resources-yaml-manifests) in the
resources guide); settings sections load exactly as before.

**Upgrading.** This is a breaking change, and the rewrite is yours to make. Agentworks ships no
migration command. What it ships instead is an error naming every offending section, two commands
that render the target shape live from this build's registry, and this section. If you would rather
delegate the work than do it by hand, "Handing the rewrite to an agent" below is the same procedure
written as a brief.

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
  then checks each manifest as you type it (see
  ["Editing manifests with schema support"](resources.md#editing-manifests-with-schema-support) in
  the resources guide).
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
  [ok]   Config file: ~/.config/agentworks/config.toml
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
  removed" section of docs/guides/upgrading-to-0.14.md walks through it section by section.
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
`manifest.schema.json`. Both of those are document lines you must uncomment, so re-read
["Declaring resources: YAML manifests"](resources.md#declaring-resources-yaml-manifests) in the
resources guide before editing an appended file.

### The sections that are not a straight move

- **`[azure]` and `[proxmox]`** become `vm-site` manifests. The section name becomes the resource
  name, and the section's keys move inside the tagged `spec.platform` table rather than sitting
  directly under `spec`. Take the platform's `name` from `agw resource describe-kind vm-platform`
  rather than from the section header: `[proxmox]` does select the `proxmox` platform, but `[azure]`
  selects `azure-vm`, so only one of the two matches its old section name.

  An `azure-vm` site also carries one key no `[azure]` section had: it says how it authenticates, in
  a tagged `auth` table. A site on the ambient Azure credentials (`az login`, `AZURE_*`, a managed
  identity) writes `auth: {mode: ambient}`, or writes nothing: ambient is the declared default. A
  site on an explicit principal writes
  `auth: {mode: service-principal, tenant_id: ..., client_id: ..., secret: ...}`, where `secret`
  NAMES the secret holding the client secret and defaults to `azure-client-secret`.
  ["Authentication and placement are one tagged field now"](#authentication-and-placement-are-one-tagged-field-now)
  below has both rewrites in full. `[proxmox]` needs nothing extra: proxmox has one authentication
  shape.

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
  you add `proxmox` (or `azure`) to `[plugins] system`. See
  ["VM sites and platforms"](resources.md#vm-sites-and-platforms) in the resources guide.

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
  always looked like they did. See
  ["Scoped GitHub credentials"](resources.md#scoped-github-credentials-fine-grained-pats) in the
  resources guide before you carry them over.

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
  `restart_command` renames it to `resume_command` on the way. See
  ["Harness integrations"](resources.md#harness-integrations) in the resources guide.

- **`[admin.config]` and `[admin.env]`** become one `admin-template` named `default`, with the env
  table nested as `spec.env`.

- **`[named_console]`** becomes a `named-console-template` named `default`.

- **`[secret_backends.*]`** becomes nothing at all. Those sections never carried configuration, only
  the backend's name, so there is nothing to move and no manifest to write: delete them, and list
  the backends you want in `[secret_config].backends`, which is a setting and stays in
  `config.toml`. They are named by the resource-section error like every other section, with that
  deletion spelled out in place of the rewrite instruction, so there is nothing to do about them
  ahead of the rest.

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
  [ok]   Config file: ~/.config/agentworks/config.toml
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

- `[secret_backends.*]` sections are deleted, not rewritten. They are the one family in the error
  with no manifest form; the error says so per run.
- The work list is the load error from `agw resource list`, and the target shape comes from
  `agw resource describe-kind <kind>` per kind. Field names are to be read from that output, never
  recalled. Where a `spec` selects a capability implementation, the fields come from
  `agw resource describe-kind <capability-kind>/<name>` (`vm-platform/proxmox`,
  `git-credential-provider/azdo`), because the kind's own output only details one implementation.
- `describe-kind` is the AUTHORITATIVE field list, and for mode unions it is complete. Where a
  platform's config carries one (`auth` on `azure-vm` and `aws-ec2`, `placement` on `lima`), the
  per-implementation output expands EVERY arm with that arm's own fields, so nothing about the modes
  has to be reconstructed from prose or from a second surface. `agw resource sample` is the surface
  that shows one arm only; it says so in a comment naming the `describe-kind` that prints them all.
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
Configuration error: ~/.config/agentworks/resources/git-credentials.yaml:1: spec.provider names
the capability as a string, which is no longer supported; write one tagged table instead:
provider: {name: github, token: ..., owner: ...}
  Hint: Apply the rewrite above; `agw resource describe-kind <kind>` documents the field, and `agw
  resource sample <kind>` prints it as a document to edit. See "The retired sibling capability
  shape" in docs/guides/upgrading-to-0.14.md.
```

The keys it lists are the ones your document actually had, in order, with the values elided as
`...`, so it tells you the shape to write and you fill your own values back in. Every document on
the old shape has to move: one left behind leaves the whole resources directory unloadable.

## Azure, Proxmox, 1Password, and Claude Code are now opt-in

These vendor- and tool-specific capabilities used to be built in and always available; they now ship
as the `azure`, `proxmox`, `onepassword`, and `claude` system plugins, disabled by default. If your
config used any of them, add the plugin to `[plugins].system` to restore it:

```toml
[plugins]
system = ["azure", "proxmox", "onepassword", "claude"]  # only the ones you use
```

Which of the four you actually need follows from what your resources reference, and the default
local path needs no `[plugins]` entry at all; "System plugins" in the
[resources guide](resources.md) has the mapping.

## Manifests are validated against a declared schema

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

### Authentication and placement are one tagged field now

Three platforms used to let the PRESENCE of an optional block choose a mechanism: an `azure-vm`
site's `service_principal`, an `aws-ec2` site's `credentials`, and a `lima` site's `vm_host`. There
was no way to write the choice itself down, so no document could say "I chose the omitted one", and
a misspelled key silently selected the wrong mechanism. Each is now a tagged union whose `mode`
names the mechanism outright: `auth` on the two clouds, `placement` on lima. Each defaults to the
mode omission always selected (`{mode: ambient}` on the clouds, `{mode: local}` on lima), which is
also what the wrapped tool does when told nothing, so a site that wrote no block still loads and
still means what it always meant. `agw doctor`'s site row shows the resolved mode either way
(`platform azure-vm (auth: ambient)`), so the choice is reviewable without opening the manifest.

**Only a site that WROTE the old field crosses this**, with a value or as an explicit `null`; a site
that omitted it was never broken. Proxmox and wsl2 are unaffected, having no choice to express
(proxmox has one authentication shape, wsl2 takes no config at all). Like every other declaration,
this is checked wherever the manifest loads, whether or not the platform's plugin is enabled, per
["Every host checks every declaration now"](#every-host-checks-every-declaration-now) above.

**What you see.** A site that WROTE the old block is refused by name, with the replacement rendered
from the keys your own document had:

```console
$ agw resource list
Configuration error: ~/.config/agentworks/resources/sites.yaml:1: vm-site/azure-dev:
'service_principal' is no longer a supported field; the choice it used to carry by being present is
written explicitly now: auth: {mode: service-principal, tenant_id: ..., client_id: ..., secret: ...}
  Hint: Apply the rewrite above; `agw resource describe-kind <kind>` documents the field, and `agw
  resource sample <kind>` prints it as a document to edit. See "Authentication and placement are
  one tagged field now" in docs/guides/upgrading-to-0.14.md.
```

A site that wrote the old key as an explicit `null` gets its own message, and it is the one worth
reading closest. On 0.13 a null meant exactly what omitting the key meant, so `vm_host: null` was a
LOCAL site and `service_principal: null` was ambient auth. A retired field is still a retired field,
so the line has to go; the message says what the null was doing and names the line to write in its
place:

```console
$ agw resource list
Configuration error: ~/.config/agentworks/resources/sites.yaml:1: vm-site/lima-here: 'vm_host:
null' is a retired spelling. It selected 'local', exactly as omitting the key did, and ending that
conflation is why the field is gone; delete the null line and write the choice instead: placement:
{mode: local}
  Hint: Apply the rewrite above; `agw resource describe-kind <kind>` documents the field, and `agw
  resource sample <kind>` prints it as a document to edit. See "Authentication and placement are
  one tagged field now" in docs/guides/upgrading-to-0.14.md.
```

Note which arm it names. A null selected the AMBIENT and LOCAL modes, never the credentialed ones,
so `service_principal: null` becomes `auth: {mode: ambient}` and `vm_host: null` becomes
`placement: {mode: local}` (each is the union's default, so deleting the null line alone also loads;
writing the line keeps the choice visible in the document). If you are working from the null scan in
["An explicit `null` secret name now means the DEFAULT secret"](#one-meaning-changed-rather-than-one-shape)
below, these three keys are the ones whose nulls were doing something different from the secret
fields that section covers.

Both errors name the file, the line, and the site, like every other manifest error here. One site
per pass, as everywhere else here, so three stale sites take three passes. `agw doctor` reports each
as one fail row and carries on, which makes it the loop to work in.

**Azure.** `auth` replaces `service_principal` at the same depth, and the keys you already had move
into it beside a `mode` line:

```yaml
# before
spec:
  platform:
    name: azure-vm
    subscription_id: "00000000-0000-0000-0000-000000000000"
    resource_group: agw-dev
    region: eastus
    service_principal:
      tenant_id: 11111111-1111-1111-1111-111111111111
      client_id: 22222222-2222-2222-2222-222222222222
      secret: azure-client-secret
```

```yaml
# after
spec:
  platform:
    name: azure-vm
    subscription_id: "00000000-0000-0000-0000-000000000000"
    resource_group: agw-dev
    region: eastus
    auth:
      mode: service-principal
      tenant_id: 11111111-1111-1111-1111-111111111111
      client_id: 22222222-2222-2222-2222-222222222222
      secret: azure-client-secret
```

`mode`, `tenant_id`, and `client_id` are required in the `service-principal` arm. `secret` is not:
it names the secret holding the client secret and falls back to `azure-client-secret` when you leave
it out, exactly as it did inside the old block. A site with no `service_principal` key anywhere in
it needs no edit: it lands on the `auth: {mode: ambient}` default, the `az login` / `AZURE_*` /
managed-identity chain with the interactive-browser fallback, which is what it was using all along.
Write the line anyway if you want the choice visible in the document.

**AWS.** `auth` replaces `credentials`. The three keys keep their spellings:

```yaml
# before
spec:
  platform:
    name: aws-ec2
    region: us-east-1
    credentials:
      access_key_id: AKIAEXAMPLE
      access_key_secret: aws-secret-access-key
      assume_role_arn: arn:aws:iam::123456789012:role/agentworks
```

```yaml
# after
spec:
  platform:
    name: aws-ec2
    region: us-east-1
    auth:
      mode: access-key
      access_key_id: AKIAEXAMPLE
      access_key_secret: aws-secret-access-key
      assume_role_arn: arn:aws:iam::123456789012:role/agentworks
```

`mode` and `access_key_id` are required in the `access-key` arm. `access_key_secret` names the
secret holding the secret access key and falls back to `aws-secret-access-key`; `assume_role_arn` is
optional and layers an STS AssumeRole over the key. A site with no `credentials` key anywhere in it
needs no edit: it lands on the `auth: {mode: ambient}` default, boto3's own chain (environment,
shared config, instance profile, SSO), which is what it was using all along.

**Lima.** `placement` replaces `vm_host`, and this is the one where a field is also renamed: inside
the `ssh` arm it is `host`, because the arm already says which host this is.

```yaml
# before
spec:
  platform:
    name: lima
    vm_host: me@gpu-box
```

```yaml
# after
spec:
  platform:
    name: lima
    placement: { mode: ssh, host: me@gpu-box }
```

`host` is required in the `ssh` arm, which is the whole point of the change: a misspelled host key
used to turn an ssh site into a not-ready local one, reporting `limactl not installed` about a
problem you did not have. A site with no `vm_host` key anywhere in it needs no edit: it lands on the
`placement: {mode: local}` default and keeps running `limactl` on this machine. A `local` site needs
`limactl` here and reports not-ready without it; the built-in `lima-local` site is exactly
`placement: {mode: local}` and needs no declaration.

**Where the fields come from.** `agw resource describe-kind vm-platform/azure-vm` (and `/aws-ec2`,
`/lima`) documents the union with its default in the parenthetical and shows each mode's own fields
under that mode, so the rewrites above are not the only place they are written down:

```console
$ agw resource describe-kind vm-platform/azure-vm
[...]
  auth  (table, optional, default {mode: ambient})
    How this site authenticates to Azure: `{mode: ambient}` for the ambient credential
    chain, or `{mode: service-principal, ...}` for an explicit principal. Defaults to
    ambient, matching what `DefaultAzureCredential` does when told nothing.
    - ambient: Authenticate with the ambient chain: `az login`, `AZURE_*`, or a managed identity.
      mode  (one of: ambient, required)
        Selects this arm.
    - service-principal: Authenticate as an explicit Entra ID service principal.
      mode  (one of: service-principal, required)
        Selects this arm.
      tenant_id  (string, required, min length 1)
        The Entra ID tenant the principal lives in.
      client_id  (string, required, min length 1)
        The principal's application (client) id.
      secret  (string or null, optional, defaults to `azure-client-secret`, names a secret, min length 1)
        The secret holding the principal's client secret. [...]
```

`agw resource sample vm-site` cannot do the same, because a document holds one arm: it prints lima's
`placement` under a `# One of: local, ssh. Shown here: local.` line with only the `local` arm's
`mode` in the document, and points at `agw resource describe-kind vm-site` for the rest. The emitted
schema carries both arms too: `agw resource schema --write` writes `oneOf` with a `const` per mode,
so a schema-aware editor completes and checks whichever arm you are writing (see
["Editing manifests with schema support"](resources.md#editing-manifests-with-schema-support) in the
resources guide).

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
  harness integration's config block. The ones most likely to be sitting in an existing file are
  `shell`'s `command`, `resume_command`, and `required_commands`, `extra_args` on `claude-code` and
  `codex`, `codex`'s `writable_dirs`, a github credential's `repos`, and a `session-template`'s
  `env`. `agw resource describe-kind` marks a field "or null" when null is legal, so it settles any
  case not listed here, with one exception it does not mark: a field that NAMES a secret renders as
  "optional, defaults to `<name>`, names a secret" and takes an explicit `null` as well, which is
  what ["One meaning changed rather than one shape"](#one-meaning-changed-rather-than-one-shape)
  below is about. A platform's config block is mixed for that reason and because several of its
  fields are genuinely optional (an aws site's `subnet_id`, a proxmox site's `bridge`).

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

Those three platforms each name a secret where they configure a credential, defaulting to a
well-known name when the field is omitted: proxmox's `token_secret` (default `proxmox-token`), the
`secret` inside an azure site's `auth: {mode: service-principal, ...}` arm (default
`azure-client-secret`), and the `access_key_secret` inside an aws site's
`auth: {mode: access-key, ...}` arm (default `aws-secret-access-key`). Writing an explicit `null`
there used to be a hard error whose message told you to OMIT the key instead. The rule is now that
absent and `null` mean the same thing, so that same input quietly resolves to the default-named
secret and the site declares a dependency on it.

Two of those three paths moved in
["Authentication and placement are one tagged field now"](#authentication-and-placement-are-one-tagged-field-now)
above: the FIELDS keep their spellings, but they now sit in an `auth` arm rather than in a
`service_principal` or `credentials` block. So do that rewrite first, then read this against the
file you end up with. Proxmox is where it was.

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

The pattern is unchanged by the `auth` rewrite above, because the three keys it looks for kept their
spellings; only the block around two of them moved, and the pattern never mentioned that block.
Three details in it are load-bearing, because YAML has more ways to write a null than the obvious
one. `-i` catches `Null` and `NULL`, which the loader reads as null exactly like `null`. Ending on
`[,}]` as well as end-of-line catches a field written inside a flow mapping
(`auth: { mode: service-principal, client_id: cid, secret: null }`), where the line does not end
after the value. And the optional `(null|~)?` catches a bare key with nothing after it, which is a
null too.

Treat a clean result as good evidence rather than as proof. It is a line-oriented scan of a format
that does not have to be line-oriented, so it will still miss a quoted key (`"secret": null`) or a
value pushed onto its own continuation line. If you use one of these three platforms, it is worth
opening the site's manifest and looking at the field directly.

Then decide, per hit:

- **If you meant "no secret here", deleting the FIELD is not how you say so.** Where the field is
  legal at all, absent, `null`, and the default written out are three ways of writing one thing
  (checked: a proxmox site whose `token_secret` line is deleted validates with `token_secret` set to
  `proxmox-token` and declares the dependency, and an azure site whose `secret` line is deleted or
  set to `null` does the same with `azure-client-secret`). Opting out is a MODE now, not an
  omission, and it differs by platform: an azure or aws site that names no secret at all is one
  written `auth: {mode: ambient}`, which declares no secret and no secret edge. That is a change of
  identity rather than of naming, though, so it is only the right answer if the ambient chain really
  is the credential you want that site to use; the `tenant_id` / `client_id` (or `access_key_id`)
  keys go away with it. Proxmox has no such mode: its token configuration is required, so a proxmox
  site always names a secret.
- **If you meant a different secret, name it.** Which secret the field points at is the only thing
  it can express, and it is what you came here to set.
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

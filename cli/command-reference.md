# CLI Command Reference

This reference documents the Agentworks command surface, operational JSON v1 contract, and command
internals that affect operator behavior. Installation and configuration guidance remains in the
[CLI README](README.md).

Global `--non-interactive` means only "do not use the TTY for interactions, even if one is present."
It does not disable color or other presentation and does not suppress biometric, app, browser,
device, or other out-of-band provider work. It is not an unattended fail-fast mode; out-of-band
authentication may request approval and wait for the configured source timeout.

## Commands

### Instance specs

An instance spec is an inline JSON object applied as the final layer after a selected template. Use
`--spec JSON` on `vm create`, `workspace create`, `agent create`, and `session create`. A VM has two
declarations: `--template` and `--spec` shape the VM, while `--admin-template` and `--admin-spec`
shape its admin user. `agent reinit` also accepts `--spec JSON` because it is the existing-instance
command that can change the bound template. No other reinit, repair, start, restart, or copy command
accepts an instance spec.

The JSON must be one object written directly on the command line. It cannot be a file path, `null`,
an array, or multiple JSON values. Duplicate keys, non-finite numbers, nested nulls, unknown fields,
and the template-only fields `name`, `inherits`, `metadata`, and `framework` are rejected before
state or remote resources change. Quote the object for the current shell:

```bash
agw vm create build-01 --template dev --spec '{"cpus":8,"memory":16}' \
  --admin-template operator --admin-spec '{"shell":"zsh"}'
agw agent create coder-01 --spec '{"shell":"/bin/bash","mise_activate":true}'
```

An omitted `--spec` on `agent reinit` retains the stored instance layer. A supplied object replaces
the complete prior layer, and `{}` or an empty value clears it. The empty-value shorthand applies
only to `agent reinit`; create commands still require a JSON object. Template changes and
instance-spec changes are validated and persisted together before remote convergence. If that remote
work fails, the new desired state remains stored so a plain `agent reinit NAME` can retry it.

For compound `session create`, `--spec` applies to the new session. `--workspace-spec` is accepted
only with `--new-workspace`, and `--agent-spec` only with `--new-agent`; each applies to that
matching new owner. The command reports whether an instance layer was set, replaced, retained,
cleared, or explicitly absent, along with recognized field names when available. It never prints the
JSON or environment values.

Instance specs use the fields and model-directed merge rules of their matching template. Objects and
mappings recursively merge by key, lists append unequal atomic items in stable order and deduplicate
equal items, and incoming scalars replace prior values. A field or nested model may declare
replacement instead. Run `agw resource explain vm-template`, `workspace-template`, `agent-template`,
or `session-template` for the accepted fields. Use `agw resource explain admin-template` for
`--admin-spec` fields.

### Machine-readable output

The operational inspection commands `agw graph show`, `agw resource list`, `agw resource show`,
`agw resource kinds`, `agw secret list`, `agw secret describe`, `agw vm list`, `agw vm describe`,
`agw workspace list`, `agw workspace describe`, `agw agent list`, `agw agent describe`,
`agw session list`, `agw session describe`, `agw console list`, `agw console describe`, and
`agw doctor` accept `--output json`. The default `--output human` preserves the normal terminal
presentation.

Each successful JSON response is one UTF-8 document followed by one line feed, with no BOM, ANSI
sequences, table layout, progress messages, or empty-state prose. Its top-level keys are always in
this order:

```json
{ "schema_version": 1, "command": "resource.list", "data": {} }
```

`schema_version` is integer `1`; `command` is the exact command identifier below; and `data` is an
object. Every documented object field is present and emitted in the listed order. Arrays retain the
service's order and are never re-sorted by JSON rendering. Missing values are JSON `null`, never a
human display sentinel. JSON string text retains ordinary Unicode; control, format, surrogate,
line-separator, and paragraph-separator characters are escaped without changing the parsed value.

The shared records are:

```text
origin = {
  variant, file, line, source, source_resource, plugin
}
reference = {
  source_kind, source_name, usage, declared_by_kind, declared_by_name
}
instance_reference = {kind, name}
```

`origin` is null or one of `operator-declared`, `auto-declared`, `built-in`, or `system-plugin`.
Only its applicable provenance fields are populated. `source_resource` is null or `{kind,name}`.
References retain every graph entry, including duplicates, in graph order. They are not formatted,
deduplicated, grouped, or sorted.

#### Resource JSON schemas

`agw resource list --output json` uses command `resource.list` and data:

```text
{
  resources: [{
    kind, name, origin, reference_count, used_by_count, description,
    not_ready_reason, disabled
  }],
  counts: {operator_declared, auto_declared, built_in, system_plugin}
}
```

`origin`, `used_by_count`, and `not_ready_reason` may be null. `disabled` is boolean. Resource rows
preserve the selected kind order, then name order, and counts are post-filter values. Disabled rows
remain hidden unless `--include-disabled` is requested.

`agw resource show KIND/NAME --output json` uses command `resource.show` and data:

```text
{resource: {
  kind, name, origin, reference_count, used_by_count, description, not_ready_reason, disabled,
  category, enablement, readiness,
  relationships: {dependencies: [edge...], dependents: [edge...]},
  used_by: [{kind, name}...] | null,
  diagnostics: [{name, status, message, hint}...],
  declaration,
  resolution?
}}
```

The first eight fields are the exact matching `resource list` row contract. Each relationship edge
has `edge_type`, `source`, `target`, `relationship`, `usage`, and `declared_by`; the arrays contain
only edges touching the selected row and retain canonical graph order and duplicates. `used_by` is
null when the kind has no live-instance concept and is an array, possibly empty, when it does;
`used_by_count` is the matching null or array length. Diagnostics are the same resource-attributable
checks used by `agw doctor`, not a filtered fleet-wide report.

`category` is `declarable` or `capability`, and `enablement` is `enabled` or `disabled`. A disabled
row has null `readiness`. An enabled row has `{is_ready, is_available, reason}` in one of three
states: ready is `{true, true, null}`; blocked is `{false, true, reason}`; and an unavailable or
deliberately omitted host check is `{false, false, reason}`. `is_available` therefore distinguishes
a check that produced a verdict from one that could not or deliberately did not run. A declarable
resource's `declaration` is `{apiVersion, kind, metadata, spec}`; metadata contains its non-null
shared manifest fields, and spec contains every non-null loaded kind field, including defaults.
Dates, timestamps, enums, nested models, mappings, and collections use JSON-native values. A
capability's declaration is null. The declaration is normalized registry state: it does not preserve
comments, source key order, omitted-versus-defaulted distinctions, or an effective inheritance
merge. Human mode retains loader advisories; JSON mode suppresses them and writes one envelope only
after every focused fact has been assembled.

`vm-template`, `admin-template`, `workspace-template`, `agent-template`, and `session-template`
append `resolution`; other kinds omit it. The tagged resolved shape is
`{status: "resolved", spec, provenance}`. `spec` is the complete effective JSON object after
defaults and inheritance. `provenance` is an ordered array of
`{path: [(string | integer)...], sources: [{role, resource_kind, resource_name}...]}`. String path
segments name fields or map keys and integer segments name positions in the final effective list.
Paths cover scalar leaves, empty collections, final list positions, and whole subtrees when one
source still owns the complete subtree. A composite container assembled from multiple layers is
represented by its truthful descendant paths instead of a misleading container source. Role is
`defaulted`, `inherited`, `declared`, or `overlaid`. This projection can contain authored plaintext
declaration values but never resolves a secret reference; treat its output as sensitive.

`agw resource kinds --output json` uses command `resource.kinds` and data
`{kinds: [{kind, category, resource_count, description}]}`. `category` is exactly `declarable` or
`capability`; kinds sort lexically.

#### Graph JSON schema

`agw graph show KIND/NAME --output json` uses command `graph.show` and data:

```text
{
  query: {focus: {kind, name}, direction, depth_limit},
  nodes: [{node_type, kind, name, distance}],
  edges: [{edge_type, source, target, relationship, usage, declared_by}]
}
```

`node_type` is `resource` or `live-instance`. `edge_type` is `declared` or `live-usage`; `source`
and `target` are `{node_type, kind, name}`. `relationship` is `uses` or `inherits`. `usage` and
`declared_by` are nullable, with non-null provenance shaped as `{kind, name}`. `depth_limit` is null
for `--depth all`. Nodes are ordered by distance, type, kind, and name; edges retain intrinsic
source-to-target direction and deterministic fact order.

For example:

```bash
agw resource list --kind secret --output json
agw resource show secret/npm-token --output json
agw graph show secret/npm-token --output json
```

#### Secret JSON schemas

`agw secret list --output json` uses command `secret.list` and data:

```text
{
  sources,
  secrets: [{name, description, sources: [{source, would_attempt, identifier, not_ready_reason}]}],
  counts: {operator_declared, auto_declared}
}
```

The top-level and per-secret source arrays preserve configured source precedence. Secrets sort by
name. `identifier` and `not_ready_reason` are nullable. This is a static mapping projection only. A
`would_attempt: true` compatibility field means the lookup disposition is a candidate; it is not a
value-presence claim and never invokes provider preview.

`agw secret describe NAME --output json` uses command `secret.describe` and data:

```text
{secret: {
  name, kind, origin, description, hint, references, used_by, source_mappings,
  resolution: {
    category, source, identifier, skipped_not_ready: [{source, reason}],
    preview: {status, source, identifier, reason?, attempts: [{source, identifier, status, reason?}]}
  }
}}
```

`source_mappings` is an array of exactly
`{source, backend, provenance, would_attempt, identifier, not_ready_reason}`. `source` names the
configured source instance and `backend` names its implementation. `provenance` is
`synthesized-default`, `operator-override-of-synthesized-default`, or `declared`. `would_attempt` is
boolean. `identifier` is null when a source has no static lookup identifier or will not attempt the
secret. `not_ready_reason` is null when that source is ready. Resolution `category` is `attemptable`
or `unavailable`; `source` and `identifier` are nullable. `source_mappings` retains configured
source-chain ordering. References and `used_by` retain their service ordering. The nested provider
preview is value-free. `status` is `available`, `missing`, `indeterminate`, `blocked`, or `failed`.
`reason` is absent for available and missing, and required for the other statuses; aggregate
`blocked/no-candidate` has null source and identifier. Attempts retain source-chain ordering and
omit static non-candidates. The default preview permits no backend-classified operator impact;
`--allow-interaction` permits it and guarantees that the result is not indeterminate.

#### VM JSON schemas

`agw vm list --output json` uses command `vm.list` and data:

```text
{vms: [{name, site, template, provisioning_status, initialization_status,
        workspace_count, agent_count, session_count, tailscale_host, created_at,
        debian_release, debian_release_observed_at, observed_status,
        status_disposition}]}
```

`template`, `tailscale_host`, `debian_release`, and `debian_release_observed_at` are nullable. A
non-null release is a codename recognized by this Agentworks build, and its timestamp records the
last matching live observation. VMs retain name order. Provisioning is `pending`, `in_progress`,
`complete`, `failed`, or `unknown`; initialization additionally permits `partial`. These frozen JSON
v1 vocabularies do not expand when domain enums gain members. In this VM list JSON projection,
`unknown` is the stable sentinel for an invalid persisted value and never echoes that stored value.
The 0.18 producer always emits the additive nullable v1 fields `observed_status` and
`status_disposition`; a v1 consumer must tolerate their absence from older producers. Plain list
emits null for both. With `--status`, observed status is `running`, `stopped`, `deallocated`, or
`unknown`; disposition is `manual` or `idle` only for stopped or deallocated VMs.

The VM, workspace, agent, and session description records append the JSON v1 `instance_state`
object. Current producers always include it; older JSON v1 producers may omit this additive field:

```text
{
  declarations: {SLOT: {selection: {kind, name}, instance_spec, current}...},
  lifecycle_evidence: [fact...],
  comparisons: [comparison...],
  unconsumed_records: [record...],
  issues: [issue...]
}
```

`instance_spec` is tagged `absent`; `present` with `recorded_at` and the canonical partial `spec`;
or `unavailable` with reason `malformed` or `unsupported-version`. `current` is the resolved shape
documented for `resource show`, or `{status: "unresolved", selection: {kind, name}, reason}` where
reason is `missing-selection`, `instance-spec-unavailable`, or `registry-unavailable`. A fact is
`{key, status}` with status `not-recorded` or `unavailable`, or status `recorded` plus
`recorded_at`, `operation`, and a typed `value` object. Each entry is a successful lifecycle
operation's configuration-snapshot slice and may include evidence that its corresponding work
succeeded. Comparisons are `{key, state, differences?}`; differences are
`{field, recorded, current}`. Unconsumed records carry only safe type, key, version, and timestamp
metadata. An issue contains a closed `code` plus optional `slot` or `record_key`; malformed future
record types use `record-malformed` rather than an applied-state issue code. These explicit
inspection commands can show authored plaintext declaration values, but never resolved secret
values; handle their human and JSON output as sensitive. Human describe renders instance specs,
complete current specs, and lifecycle-evidence value objects as block YAML without the exhaustive
per-leaf Value sources. Single-line comparison values remain compact JSON. JSON describe and both
human and JSON template `resource show` retain full provenance. Human describe calls the
recorded-fact section `Lifecycle evidence`.

`agw vm describe NAME --output json` uses command `vm.describe` and data `{vm, issues}`. `vm` has
this ordered shape:

```text
{name, created_at, site, platform, backend, observed_status, status_disposition,
 operator_stopped, hostname, system_slug, system_slug_state, template, admin_template,
 admin_username, provisioning_status, initialization_status, tailscale_host, last_seen_at,
 debian_release, debian_release_observed_at, provisioned_resources, live_resources, agents,
 workspaces, events, instance_state}
```

`platform`, `backend`, `status_disposition`, `system_slug`, `template`, `admin_template`,
`tailscale_host`, `last_seen_at`, `debian_release`, `debian_release_observed_at`, and
`live_resources` are nullable. Older JSON v1 producers may also emit a null `observed_status`; the
0.18 producer always emits `running`, `stopped`, `deallocated`, or `unknown` because describe
requests observation. Non-null release observations have the same recognized-codename and timestamp
semantics as VM list. `status_disposition` is `manual` or `idle` only for stopped or deallocated
VMs; and `system_slug_state` is `set`, `declined`, or `unset`. `provisioned_resources` is
`{cpus, memory_gib, disk_gib, swap_gib}` with nullable integers. It is the provisioning request
recorded by Agentworks, not provider-observed realized hardware. Human VM describe labels these
persisted values `Requested`. `live_resources` is null or this record:

```text
{cpus, load_average, memory_total, memory_used, memory_percent, swap_total,
 swap_used, swap_percent, disk_total, disk_used, disk_percent}
```

`agents[]` is `{name, linux_user, grant_all, grant_count}`. `workspaces[]` is
`{name, path, sessions}` with session entries `{name, template, mode, agent_name}`. `events[]` is
`{created_at, event, detail}`. Event is exactly `provisioning_started`, `provisioning_complete`,
`provisioning_failed`, `init_started`, `init_complete`, `init_partial`, `init_failed`,
`backup_started`, `backup_completed`, `backup_failed`, `rekey`, or `unknown`. Historical or future
raw names outside that closed set project as `unknown` and never echo their stored text. `detail` is
reserved and always JSON `null` in v1 because persisted event detail is unbounded diagnostic text;
no non-null detail grammar exists. `agent_name` is nullable, and mode is `admin`, `agent`, or
`unknown`. In this nested VM JSON projection, the sentinel closes invalid persisted modes without
echoing them. These arrays retain database order. `issues[]` is `{source, code}` in encounter order:
source is `site_lookup`, `preflight`, `secret_resolution`, or `platform_status`, and code is always
`unavailable`. Issues do not carry backend text or exception details.

VM instance state has `vm` and `admin` declaration slots. Its defined lifecycle-evidence facts are
`hardware-request` and `ssh-identity`; their comparisons use `not-recorded`, `unverifiable`,
`match`, or `drift` only when the available evidence supports that state. `hardware-request` is the
recorded request associated with successful VM creation, compared with the current declaration. It
does not claim provider-observed realized hardware.

```bash
agw vm list --output json
agw vm describe build-vm --output json
```

#### Workspace and agent JSON schemas

`agw workspace list --output json` uses `workspace.list` and
`{workspaces: [{name, vm_name, template, created_at}]}`. `template` is nullable and order remains
workspace name order after filtering. `agw workspace describe NAME --output json` uses
`workspace.describe` and `{workspace}`; workspace is
`{name, vm_name, template, path, created_at, sessions, agents, instance_state}`. Session entries are
`{name, template, mode, agent_name}` and agent entries are `{name, linux_user}`. `template` and
`agent_name` are nullable, and mode is `admin`, `agent`, or `unknown`. An invalid persisted mode
maps to `unknown` without exposing its raw value in this workspace JSON projection.

`agw agent list --output json` uses `agent.list` and
`{agents: [{name, vm_name, template, grant_all, grants}]}`. `template` is nullable, `grant_all` is
boolean, and grant entries are `{workspace_name, grant_type}` where grant type is `explicit`,
`implicit`, or `both`. Agents retain VM then agent name order.
`agw agent describe NAME --output json` uses `agent.describe` and `{agent}`; agent is
`{name, vm_name, linux_user, template, grant_all, created_at, explicit_grants, sessions, instance_state}`,
with nullable `template` and session entries `{name, template, workspace_name}`.

Workspace and agent describe preserve their database-backed facts, including the stored instance
spec, when registry construction fails with an expected `ConfigError` or `ValidationError`. In that
degraded result, the current declaration is unresolved with reason `registry-unavailable` and the
issue list names the affected declaration slot. Configuration loading failures and unexpected
programming or infrastructure failures still fail the command.

#### Session and console JSON schemas

`agw session list --output json` uses `session.list` and `{sessions}`. Each session is
`{name, workspace_name, vm_name, template, harness_integration, mode, agent_name, status}`.
`harness_integration` and `agent_name` are nullable; mode is `admin`, `agent`, or `unknown`; status
is exactly `running`, `stopped`, `residual`, `broken`, `unknown`, or `unavailable`. Plain list emits
`unavailable`; `list --status` emits a live state and uses `unknown` when observation is
inconclusive. Older JSON v1 producers may have used `unavailable` for other unavailable
observations. A bad persisted mode maps to `unknown` without exposing its raw value in these session
JSON projections. The frozen output mode vocabulary does not expand when the domain enum gains a
member. Rows retain workspace then session name order. `agw session describe NAME --output json`
uses `session.describe` and `{session}`. Session is this record:

```text
{name, workspace_name, vm_name, template, harness_integration, mode, agent_name,
 status, pid, created_at, updated_at, consoles, instance_state}
```

`pid` is a positive integer or null. Opaque harness state, socket paths, and boot identifiers are
never serialized. `consoles` is the additive optional v1 field `[{console_name, position}]`. Entries
retain deterministic console-name order, and `position` is the console membership's stored
zero-based position. Current producers emit `[]` when the session has no console associations; older
v1 producers may omit this additive field under the compatibility contract below.

`agw console list --output json` uses `console.list` and
`{consoles: [{name, vm_name, session_count, status}]}` in configured name order after filtering. The
0.18 producer always emits the additive v1 console `status` field; a v1 consumer must tolerate its
absence from older producers. Status is `unavailable` for plain list; with `--status` it is
`running`, `stopped`, `residual`, or `unknown`. `agw console describe NAME --output json` uses
`console.describe` and `{console}`. Console is
`{name, vm_name, admin_shell, created_at, updated_at, status, sessions}`. Describe status uses the
console live vocabulary and never `unavailable`. Members are `{position, session_name, shells}` in
ascending position, and shells are `{cwd, admin}` in configured shell order. `cwd` is nullable and
all booleans remain JSON booleans. Console inspection preserves configured database membership even
when its non-activating live observation is unknown.

#### Doctor JSON schema

`agw doctor --output json` uses command `doctor` and data:

```text
{
  groups: [{name, checks: [{name, status, message, hint, secret_preview?, instance_state?}]}],
  counts: {ok, info, warn, fail}
}
```

`status` is exactly `ok`, `info`, `warn`, or `fail`. Group and check arrays keep report construction
order, and counts are integers from the complete report. `message` and `hint` are the same nullable
facts used by the human renderer. A secret check adds the optional, value-free `secret_preview`
record `{status, source, identifier, reason?, attempts}`. Its status is `available`, `missing`,
`indeterminate`, `blocked`, or `failed`; source and identifier are nullable; reason is present only
for a result with a closed reason; and attempts retain source order as
`{source, identifier, status, reason?}`. Non-secret checks omit the field. No secret value enters
this projection. An indeterminate prompt that can receive terminal input uses reason
`operator-input-required`; a provider lookup skipped because broader operator impact might be needed
uses `operator-impact-limited`.

An instance-state check adds this optional value-free object:

```text
{fact_type, instance_kind, instance_name, record_type, record_key, payload_version,
 recorded_at, comparison, owner_exists}
```

Nullable fields remain null when unsafe or irrelevant. `fact_type` is `lifecycle-comparison`,
`coverage`, `malformed-record`, `orphan-record`, or `unconsumed-record`. Doctor never includes
record payloads or resolved secret values.

A failing report is still written in full, then the command exits 1:

```bash
agw doctor --output json
```

Doctor checks the schema before opening current state through the existing read-only database
connection. Its WAL-aware inspection reports a pending migration without applying it, and future,
busy, or malformed state fails without a writable open; busy (another process holding a lock on the
database) is the one an operator is actually likely to hit.

#### Errors and compatibility

`--names-only` is completion-only and cannot be combined with JSON output on resource, secret, VM,
workspace, agent, session, and console lists or resource kinds. An unknown output format and this
conflict are usage errors before config, registry, database, network, or service work. For the
ordinary covered commands, domain and configuration errors write no JSON to stdout; they retain the
normal stderr message and nonzero exit status. Doctor is the exception: it converts checkable
failures into its complete report, emits that JSON document, then exits 1.

JSON v1 is additive. New optional fields may be added while preserving existing meanings and types.
Removing a field, changing a type, changing a value's meaning, changing collection order, or
changing an enum spelling requires a new schema version and an explicit compatibility period.

### Top-Level

| Command                    | Description                              |
| -------------------------- | ---------------------------------------- |
| `agw doctor`               | Check environment and config             |
| `agw version`              | Print the installed CLI version          |
| `agw completion show`      | Print the completion script to stdout    |
| `agw completion install`   | Install the completion script in-place   |
| `agw completion uninstall` | Remove installed completions for a shell |

### Database

| Command                                       | Description                                       |
| --------------------------------------------- | ------------------------------------------------- |
| `agw database backup`                         | Create an on-demand SQLite snapshot               |
| `agw database restore BACKUP_PATH [--yes/-y]` | Replace the live database with a validated backup |

Both commands operate directly on SQLite through its online backup API. They do not open the
migrating `Database` facade. `database backup` snapshots the present schema, including a schema
newer than the running release, and emits only the completed path on stdout. Status text stays on
stderr. A missing or malformed live database is refused without creating an empty database or a
completed backup.

Backups are stored in `database-backups/` beside `agentworks.db`. On-demand names start with
`agentworks-manual-` and are never automatically removed. Pre-migration names start with
`agentworks-pre-migration-` and include the source schema version; after a successful automatic
backup, only the five newest recognized pre-migration files remain. Manual and unrelated files are
not part of that retention set.

For an outdated live schema, every ordinary writable open passes through the same safety boundary.
Agentworks announces the version change on stderr, then either prompts on an interactive stdin and
stderr terminal (default yes) or reads the focused setting below. A selected snapshot completes
before the first migration statement:

```toml
[database]
auto_backup_before_migration = true
```

The setting is a strict boolean and defaults to true. A malformed file or invalid `[database]`
section blocks a non-interactive migration; unrelated settings do not. A selected backup failure
stops before migration and provides an explicit-decline or config-opt-out retry. A later migration
failure reports the exact restore command when a snapshot exists, or explicitly says no
pre-migration backup was created. Notices and prompts stay on stderr, so JSON and `--names-only`
stdout remain machine-pure.

`database restore` validates SQLite integrity, the claimed supported schema version, and that
version's complete Agentworks table-and-column shape before it opens the live destination. It
refuses an identical path, a generic SQLite file, an incomplete Agentworks lookalike, or a schema
newer than this release understands. The source remains available after restore. Confirmation is
required by default; a non-interactive invocation must pass `--yes` (or `-y`). Restore does not
create an implicit backup of the live destination and does not migrate the restored schema. Run
`agw database backup` first if you want an additional recovery point before replacement. Restore a
schema-compatible backup before running an older Agentworks release against state created by a newer
release.

### Secrets

| Command                                           | Description                                       |
| ------------------------------------------------- | ------------------------------------------------- |
| `agw secret list`                                 | Show static source mappings                       |
| `agw secret describe NAME [--allow-interaction]`  | Describe and preview one secret without its value |
| `agw secret verify NAME... [--allow-interaction]` | Preview secrets without showing values            |

The synthesized `env-var` and `prompt` sources work without declarations. Add a `secret-source`
resource when a backend needs shared configuration or when you want another named source instance.
`secret describe` and `secret verify` perform provider-aware, value-free previews. Their default
impact is `NONE`; `--allow-interaction` opts into prompting or provider authentication and requires
a definitive answer. The opt-in is valid with global `--non-interactive`: terminal prompts remain
disabled while out-of-band provider work is permitted. Verification deduplicates names in request
order, prints one row per name, and exits nonzero for every result other than `available`. An
available row proves current provider presence under the requested impact, not that a narrower
line-oriented environment, credential, header, or stdin consumer accepts the value.

Actual resolution is separate. It performs one bounded source-first pass, preserves multiline
strings, rejects NUL, falls through ordinary missing and blocked sources, and hard-stops a secret on
invalid mapping, authentication, provider rejection, transport, timeout, or malformed-value
failures. A complete batch that is already terminal stops before another provider source and gives
other skipped names the core-only `batch-doomed-before-interaction` reason; explicit partial reveal
continues independent names. For truly unattended operation, configure `env-var` or a provider
authentication mode known to be unattended, such as supported 1Password service-account or Connect
credentials. The static JSON v1 resolution-category vocabulary remains `attemptable`,
`refused-interaction`, and `unavailable`; `refused-interaction` is retained for compatibility even
when the current TTY-only path does not emit it. Do not rely on `--non-interactive` as a general
provider-work prohibition.

### VMs

Manage virtual machines across declared vm-sites (Lima local or remote, Azure, AWS EC2, Google
Compute Engine, WSL2, Proxmox).

Where VMs are created is declared as `vm-site` resources: YAML manifests under
`~/.config/agentworks/resources/` that pair a platform (the code that runs VMs on one backend kind)
with its configuration. The `lima-local` and `wsl2` sites ship built in and are always available;
the `azure-vm`, `aws-ec2`, `gcp-gce`, and `proxmox` platforms ship as the opt-in `azure`, `aws`,
`gcp`, and `proxmox` system plugins (see [System Plugins](README.md#system-plugins)) and are
not-ready until enabled. Every site registers on every host and reports not-ready when this host
lacks what it needs (wsl2 is Windows-only; a local Lima site needs `limactl`; a platform may simply
not be installed, or its plugin not enabled): a not-ready site remains marked in
`agw resource list`, using it is an error naming the requirement, and `agw doctor` shows each
platform's and site's state with the reason. Run `agw resource sample vm-site` for a commented,
ready-to-edit document, and `agw resource explain vm-platform/azure-vm` (or any other platform) for
that platform's own fields. The former `agw vm-host` registry is gone: a remote Lima host is now
just a vm-site.

> **Note on WSL2:** WSL2 distros share the Windows workstation's lifecycle. They idle-shut after
> ~60s of no `wsl.exe` activity (`vmIdleTimeout` in `.wslconfig`) and do not survive workstation
> shutdown or sleep. Agentworks holds a `wsl.exe` keepalive for the duration of each VM-touching
> command, so individual `agw` operations work cleanly, but agents and sessions on WSL2 are not
> suitable for unattended background workflows. Use a site on a different platform that provides
> true long-lived VMs (e.g. Lima, Azure, Proxmox, etc.) if you need a VM that survives independent
> of your workstation.

| Command                                             | Description                                                   |
| --------------------------------------------------- | ------------------------------------------------------------- |
| `agw vm create <name>`                              | Create a new VM (provision + initialize)                      |
| `agw vm list`                                       | List configured VMs                                           |
| `agw vm describe <name>`                            | Show VM details, workspaces, and event log                    |
| `agw vm verify-connection <name>`                   | Test the canonical admin connection without starting the VM   |
| `agw vm shell <name> [--workspace <ws>]`            | Admin shell on a VM (optionally rooted in a workspace)        |
| `agw vm exec <name> [--workspace <ws>] -- <cmd...>` | Run a one-shot command as admin (optionally from a workspace) |
| `agw vm start <name>`                               | Start a stopped VM and clear its manual-stop intent           |
| `agw vm stop <name>`                                | Stop a VM and keep it stopped (no auto-start)                 |
| `agw vm reinit <name>`                              | Re-run initialization on a provisioned VM                     |
| `agw vm confirm-release <name>`                     | Observe and explicitly record the live Debian release         |
| `agw vm delete <name>`                              | Delete a VM (with confirmation)                               |
| `agw vm backup <name>`                              | Back up a VM: metadata, agents, workspaces, and files         |
| `agw vm rekey <name>`                               | Assign a new Tailscale auth key to a VM (logout + rejoin)     |
| `agw vm port-forward <name> <ports...>`             | Forward local port(s) to a VM (like kubectl port-forward)     |
| `agw vm logs <name>`                                | Show SSH logs for a VM                                        |

**Power-state semantics:** a VM that stopped on its own (idle timeout, host reboot) is started
automatically, on demand, by any command that needs it live. A VM stopped with `agw vm stop` is
different: that records your intent, so it stays down and commands that would need it refuse with a
hint until you run `agw vm start`, which clears the intent. `agw vm describe` shows which case a
stopped VM is in: its status reads `stopped (manual)` versus `stopped (idle)`.

Plain `vm list` is a local inventory read. Add `--status` to query each selected VM platform for
current power state; this never starts a VM. The human table adds `STATUS` only for that request,
and the command reports progress before provider work. `--status` cannot be combined with
`--names-only`.

`vm create <name>` takes the VM name as a required positional. Optional flags: `--template` (a
declared vm-template), `--admin-template` (a declared admin-template; defaults to the reserved
`default` admin-template, which always exists), `--spec` (an inline final VM layer), `--admin-spec`
(an inline final admin layer), and `--site` (a declared vm-site; falls back to `defaults.site`, else
the one ENABLED site is inferred when there is exactly one, several prompt interactively, and
non-interactive runs error naming the options). The selected admin-template is stored on the VM row
(NULL = `default`) and drives its admin user on every later `vm reinit`, `vm shell`, and admin-mode
session. An unknown `--admin-template` name fails before any provisioning or DB work. Hardware
(`cpus`, `memory`, `disk`, `swap`) starts with the vm-template and the admin username comes from the
admin-template. `--spec` may supply a typed final VM layer for hardware or other VM-template fields,
while `--admin-spec` may supply a typed final admin layer. The removed individual override flags do
not return. On Azure, `cpus` + `memory` select the smallest fitting VM size from the site's catalog
(built-in B-series, or the site's `vm_sizes` platform key); an off-ratio request rounds up and
warns. These are immutable provisioning parameters stored in the database. All initialization
follows the effective VM and admin declarations, with config and registry resources backing
referenced packages, credentials, secrets, and install commands. Both final layers are stored in one
desired VM record and reapplied by `vm reinit`. Templates carry no `site`: placement is per-host, so
it never travels inside a shared template.

The first interactive `vm create` asks once for an optional **system slug** (3-20 chars, lowercase
alphanumeric plus dash, no leading/trailing dash): a short identifier for this Agentworks
installation, used to namespace VM hostnames and backend-side names (`{slug}-{vm-name}`) so installs
sharing a cloud account, Proxmox cluster, or Windows/Mac user don't collide. Leave it blank if this
install is the only one using its sites' backends; a blank answer is remembered and it will never
ask again. Non-interactive runs never prompt (a later interactive create still asks once).

`vm reinit` re-runs the initialization phase using the current config without reprovisioning the VM.
Changes to config (new packages, different install commands, etc.) are picked up automatically. It
consumes both stored VM and admin instance specs but cannot change or clear either one.

VMs created before SSH lifecycle-evidence tracking have no synthesized identity evidence. Ordinary
canonical SSH commands refuse that unknown state until one successful `agw vm reinit <name>` proves
and records the configured identity. After upgrading Agentworks, run that reinit while the installed
key still works. If it no longer works, try `agw vm shell <name> --platform` where supported,
restore the configured public key, and rerun reinit. A platform-native transport can itself depend
on the configured key, so use provider-native recovery tooling or recreate the VM if it cannot
connect.

`vm confirm-release` reads `/etc/os-release` through the named VM's canonical live transport and
shows the recorded and live Debian releases. A changed or previously unknown value requires a
default-negative confirmation unless `--yes`/`-y` is supplied. On consent, Agentworks atomically
records the recognized live release and marks initialization pending. The command does not run
`vm reinit`; run that separately to converge release-aware resources. A matching observation only
refreshes its timestamp and leaves initialization state unchanged. See
[Upgrading a Debian VM](../docs/guides/debian-vm-upgrades.md) for the operator-led upgrade and
provider-recovery boundary.

`vm delete` requires `--force` if the VM has workspaces, agents, or sessions. The confirmation
message shows what will be deleted. Pass `--yes` to skip the prompt.

`vm backup` exports the VM row and exact owner tree of agents, workspaces, sessions, events, grants,
desired instance specs, and workspace files. It does not create or copy a provider VM snapshot,
disk, export, or other recovery artifact. On POSIX, its local timestamp directory is created with
mode 0700 and its JSON files with mode 0600. Native Windows refuses backups containing instance
specs until private access-control export is supported. Instance specs can contain plaintext
environment values, so keep the configured backup path on a trusted local filesystem and protect any
copies of the archive with equivalent access controls.

`agw vm shell` is the Agentworks-wrapped entry point; for raw SSH (VS Code Remote-SSH, `scp`, etc.),
use the `awvm--<vm>` alias documented under [Direct SSH aliases](#direct-ssh-aliases).

`vm shell` and `vm exec` both accept `--workspace <ws>` to root the admin session in a workspace
directory on this VM. The workspace's template env joins the env chain (between vm and admin), and
`AGENTWORKS_WORKSPACE` / `AGENTWORKS_WORKSPACE_DIR` are set in the session. The shell variant `cd`s
into the workspace; the exec variant runs the command from the workspace directory. A workspace that
lives on a different VM is rejected with a `ValidationError` before any SSH work.

In both exec commands the `--` separator is only required when the remote command's first token
starts with `-` (it stops Agentworks from reading the token as its own option); without it, a
dash-led first token is rejected with a hint naming the recoveries. Bare commands need no `--`.

Combining `--workspace` with `--platform` works (the shell still `cd`s into the workspace) but the
workspace's template env and the `AGENTWORKS_WORKSPACE` identity vars are not delivered: the
platform-native transports (`limactl shell`, `wsl.exe`) drop the `env=` kwarg by design. Treat
`--platform` as a transport-repair escape hatch, not a routine combination.

`agw vm shell --platform` opens the same shell over the platform-native transport (`limactl shell`
for Lima, `wsl.exe` for WSL2, SSH via the VM's public IP for Azure) instead of Tailscale. Useful
when Tailscale itself is the thing you need to reach the VM to fix (the issue #117 latched DNS state
is the canonical case: its heal involves restarting tailscaled, which would terminate a
Tailscale-SSH session mid-sequence). On Azure, the VM's firewall denies all inbound traffic at
baseline; for the duration of the session an ephemeral SSH allow rule scoped to your detected public
IP is created (one per session, so concurrent sessions never tear down each other's access), and
removed again on exit (the public IP itself is permanent). If your SSH traffic egresses through a
different address than the detection sees (VPN split tunnel, proxy, CGNAT), set `ssh_allow_cidrs` in
the config's `[operator]` section to a list of IPv4 addresses and/or CIDRs to allow additionally; if
detection fails entirely, those entries are used alone. Proxmox isn't supported by this flag because
the QEMU guest agent's exec interface is one-shot and non-interactive; use the Proxmox web UI's
serial console (`VM > Console` in the Proxmox VE web UI) as the equivalent escape hatch.

### Workspaces

Manage workspaces on VMs.

| Command                              | Description                         |
| ------------------------------------ | ----------------------------------- |
| `agw workspace create <name>`        | Create a workspace on a VM          |
| `agw workspace describe <name>`      | Show workspace details and sessions |
| `agw workspace list`                 | List workspaces                     |
| `agw workspace copy <source> <name>` | Copy a workspace to a new VM        |
| `agw workspace rehome <name>`        | Move workspace to a new path        |
| `agw workspace repair <name>`        | Repair workspace infrastructure     |
| `agw workspace delete <name>`        | Delete a workspace                  |

`workspace create <name>` takes the workspace name as a required positional. Optional flags: `--vm`,
`--template`, `--spec`, and `--open-vscode`.

`workspace copy <source> <name>` copies a workspace to a new VM workspace. Accepts `--vm`. Source
and destination can be the same VM (a clone) or different VMs.

`workspace list` accepts `--vm` to narrow the result set to one VM's workspaces. An unknown name in
the filter is an error, not an empty result.

`workspace delete` requires `--force` if the workspace has sessions. Running sessions are killed
during deletion. Pass `--yes` to skip the confirmation prompt.

There is deliberately no `workspace shell`: a shell rooted in a workspace is always _somebody's_
shell. Use `agw vm shell <vm> --workspace <ws>` for an admin shell or
`agw agent shell <agent> --workspace <ws>` for an agent shell. For curated tmux views over a
workspace's sessions, use `agw console create` + `agw console attach`.

### Agents

Manage agents (isolated Linux users) on VMs. Agents are VM-scoped and access workspaces via grants.

| Command                                                            | Description                              |
| ------------------------------------------------------------------ | ---------------------------------------- |
| `agw agent create <name> [--vm]`                                   | Create an agent on a VM                  |
| `agw agent list [--vm <vm>]`                                       | List agents                              |
| `agw agent describe <name>`                                        | Show agent details and grants            |
| `agw agent reinit <name> [--update-template <tmpl>] [--spec JSON]` | Re-run agent setup                       |
| `agw agent grant-workspaces <name> <ws>...`                        | Grant workspace access                   |
| `agw agent grant-workspaces <name> --all`                          | Grant access to all workspaces           |
| `agw agent revoke-workspaces <name> <ws>...`                       | Revoke workspace access                  |
| `agw agent revoke-workspaces <name> --all`                         | Revoke all explicit grants               |
| `agw agent shell <name> [--workspace <ws>]`                        | Open an interactive shell as the agent   |
| `agw agent exec <name> [--workspace <ws>] -- <cmd...>`             | Run a one-shot command non-interactively |
| `agw agent delete <name>`                                          | Delete an agent                          |

`agent create <name>` takes the agent name as a required positional. Optional flags: `--vm`,
`--template`, `--spec`, and `--grant-all-workspaces`.

`agent list` accepts `--vm` to narrow the result set to one VM's agents. An unknown name in the
filter is an error, not an empty result.

`agent reinit --update-template <tmpl>` re-points the agent to a different declared template
(validated against the resource registry, then persisted) before re-running setup. An unknown
template name is rejected up front, leaving the stored binding unchanged. Its optional `--spec`
changes the final instance layer under the rules in [Instance specs](#instance-specs).

`agent shell` and `agent exec` both SSH directly as the agent's Linux user. `agent shell` opens an
interactive login shell (sources the agent's profile). `agent exec` runs a single command
non-interactively but still wraps it in the agent's login shell so the agent's `PATH` (mise shims,
`~/.local/bin`, etc.) is in scope. Useful for scripted invocations like
`agw agent exec myagent -- claude -p "..."`.

Both accept `--workspace <ws>` to root the session in a workspace the agent has access to. The
workspace's template env joins the env chain (between vm and agent), and `AGENTWORKS_WORKSPACE` /
`AGENTWORKS_WORKSPACE_DIR` are set in the session. The shell variant `cd`s into the workspace; the
exec variant runs the command from the workspace directory. A workspace on a different VM is
rejected with a `ValidationError`; a workspace the agent lacks access to raises `AuthorizationError`
with a hint to run `agent grant-workspaces`.

`agent delete` requires `--force` if the agent has running sessions. Pass `--yes` to skip the
confirmation prompt.

`agw agent shell` / `agw agent exec` are Agentworks-wrapped entry points; for raw SSH access to an
agent (e.g. from VS Code Remote-SSH or `scp`), use the `awagent--<agent>` alias documented under
[Direct SSH aliases](#direct-ssh-aliases).

### Direct SSH Aliases

Agentworks maintains operator-side SSH config entries for both VMs and agents under
`~/.ssh/config.d/agentworks.conf` (or inline in `~/.ssh/config` if `ssh_config_dir = false`):

| Alias shape        | Lands you as           | Use cases                                         |
| ------------------ | ---------------------- | ------------------------------------------------- |
| `awvm--<vm>`       | The VM's admin user    | `ssh awvm--myvm`, `scp file awvm--myvm:~/`        |
| `awagent--<agent>` | The agent's Linux user | `ssh awagent--claude`, VS Code Remote-SSH targets |

The agent alias is keyed on the agent's operator-facing name (the same name you use in
`agw agent ...` commands), not on the on-VM Linux user (which is an implementation detail). The
prefixes are configurable via `operator.ssh_host_prefix` (default `awvm--`) and
`operator.ssh_agent_host_prefix` (default `awagent--`).

Entries are rebuilt declaratively from the database on every agent / VM lifecycle operation, so a
fresh `agw agent create` or `agw vm delete` keeps the file in sync without manual intervention. Run
`agw config sync-ssh-config` to force a rebuild.

### Sessions

Manage sessions (persistent tmux sessions running in workspaces). Session names are globally unique
-- no `--workspace` flag needed for most commands.

| Command                       | Description                    |
| ----------------------------- | ------------------------------ |
| `agw session create <name>`   | Create and start a session     |
| `agw session describe <name>` | Show session details           |
| `agw session list`            | List configured sessions       |
| `agw session attach <name>`   | Attach to a running session    |
| `agw session stop <name>`     | Stop a running session         |
| `agw session start <name>`    | Start a stopped session        |
| `agw session restart <name>`  | Replace the session runtime    |
| `agw session delete <name>`   | Stop and delete a session      |
| `agw session logs <name>`     | Dump session scrollback buffer |

`session list` accepts `--workspace`, `--vm`, `--agent`, and `--admin` to narrow the result set.
Filters compose with AND. The name filters (`--workspace`, `--vm`, `--agent`) accept a single value
or a comma-separated list (`--vm vm1,vm2`); commas within a filter are OR-ed together. An unknown
name in a filter is an error, not an empty result. `--agent <name>` matches agent-mode sessions
only; `--admin` matches admin-mode sessions only (the two are mutually exclusive).

Plain `session list` reads local inventory and omits `STATUS`. Add `--status` for bounded,
non-activating live observation of the selected sessions; the human table then uses `running`,
`stopped`, `residual`, `broken`, or `unknown`. `--status` cannot be combined with `--names-only`.

`session stop`, `session start`, and `session restart` operate on a single session by default. Pass
`--all` to batch over matching sessions. The batch form accepts `--vm <vm>`, `--workspace <ws>`,
`--agent <agent>`, `--console <console>`, and `--admin` to narrow the set; filters compose with AND
and require `--all`. The name filters accept a single value or a comma-separated list
(`--vm vm1,vm2`); commas within a filter are OR-ed together, and an unknown name in a filter is an
error, not an empty result. `--console` selects sessions belonging to any of the given consoles.
`--agent` matches agent-mode sessions only; `--admin` matches admin-mode sessions only (the two are
mutually exclusive). Pass `--force` only to recover broken state after Agentworks proves the prior
managed tmux server is absent; Agentworks never signals a stored numeric PID. Start and restart
continue the harness conversation when possible; `--force-new` requires a fresh conversation when
the operation launches a runtime. A running `session start --force-new` is refused rather than
silently replacing the runtime.

Maintainers: [Session status internals](../docs/guides/session-status.md) documents the persisted
PID and boot-ID model, read-only live status derivation, lifecycle repair, and the safety boundary
for `--force`.

`session create <name>` takes the session name as a required positional. Optional flags:
`--workspace`, `--template`, `--spec`, `--admin`, and `--agent`. If `--workspace` /
`--new-workspace` is omitted, you are prompted to pick from the existing workspaces or
`[Create new workspace]` -- filtered to the known VM when `--vm` or `--agent` already pins one (the
prompt prints `Only showing workspaces on VM 'X'` when a filter is active). If `--admin` / `--agent`
/ `--new-agent` is omitted, you are prompted with `admin`, the existing agents on the resolved VM,
and `[Create new agent]`. The prompts always fire when the flags are absent -- there is no
single-option auto-select for workspace or mode, since both are part of the session's identity.
`--vm` works differently: it auto-selects when exactly one usable VM exists (logged as
`Using VM 'X'`), prompts when multiple, and is required only in non-interactive mode when no
workspace or agent anchor pins the VM. In non-interactive mode (`--non-interactive` or no TTY), any
required prompt raises a `ValidationError` directing you to pass the corresponding flag. Pass
`--new-workspace` to create a workspace on the fly (with optional `--workspace-name`,
`--workspace-template`, and `--vm`; `--workspace-name` defaults to the session name). Pass
`--new-agent` to create a new agent for the session (with optional `--agent-name` and
`--agent-template`; `--agent-name` defaults to the session name); the new agent is provisioned on
the workspace's VM. `--workspace-spec` and `--agent-spec` apply only to those matching newly created
owners. When a session created with `--new-workspace` or `--new-agent` is later deleted, you are
offered the option to delete the workspace and/or agent as well -- the workspace if no other
sessions remain on it, the agent if it has no other sessions and no explicit grants.

<!-- Linked from the top-level README; rename only if you also update README.md. -->

### Named Consoles

Named consoles are persistent, curated tmux views over sessions on a VM. Each console is its own
tmux session (`aw-console-<name>`) containing one window per included session, plus any extra shell
panes you want preloaded into a session's window.

| Command                                             | Description                                                       |
| --------------------------------------------------- | ----------------------------------------------------------------- |
| `agw console create <name> [sessions...]`           | Create a console with the given sessions                          |
| `agw console list`                                  | List consoles                                                     |
| `agw console describe <name>`                       | Show membership and shell layout                                  |
| `agw console start <name>`                          | Start a stopped console                                           |
| `agw console restart <name>`                        | Rebuild a console                                                 |
| `agw console stop <name>`                           | Stop a running console                                            |
| `agw console attach <name>`                         | Attach to a running console                                       |
| `agw console delete <name>`                         | Tear down and remove the console                                  |
| `agw console add-sessions <name> <sessions...>`     | Add session windows (accepts `--to-index N`)                      |
| `agw console remove-sessions <name> <sessions...>`  | Remove session windows (accepts `-y`/`--yes`)                     |
| `agw console reorder-sessions <name> <sessions...>` | Reorder member sessions (`--to-index N` or `--to-back`)           |
| `agw console add-shell <name> <session>`            | Add a shell pane to a session window (accepts `--cwd`, `--admin`) |
| `agw console restore-session <name> <session>`      | Repair one session window against its configured shell list       |

`console create` accepts:

- `--vm` -- target VM. **Inferred from the listed sessions when omitted**; if the listed sessions
  span more than one VM, `console create` errors and asks you to pick one with `--vm`. When no
  sessions are listed (e.g. with `--all` and no explicit specs), VM selection falls back to the
  standard prompt (auto-picked if you have a single VM, prompted otherwise).
- `--all` -- include every session on the VM with 0 shells, appended after the explicit specs
  (alphabetical).
- `--all-running` -- like `--all` but restricted to sessions whose live tmux state on the VM is
  running (using the same read-only observer as `agw session list --status`). Mutually exclusive
  with `--all`. Requires the VM to be reachable. Exact tmux presence includes a running session even
  when its persisted runtime fingerprint is incomplete; an indeterminate non-stopped row refuses the
  operation rather than silently producing a partial console. This creation path preserves its
  lifecycle eligibility filter: rows with `pid == PID_STOPPED` are excluded before observation.
- `--add-admin-shell`: include a top-level admin-shell window as window 0.

`console reorder-sessions` moves the listed members in argument order while preserving the relative
order of every unlisted member. By default, or with `--to-index 0`, the listed members move to the
front. `--to-index N` starts them at zero-based final session index `N`; valid positions range from
zero through the number of unlisted members, inclusive. `--to-back` is the same as that upper bound.
The two options are mutually exclusive. Session indices exclude the optional admin-shell window.

`console add-sessions` appends the new sessions when placement is omitted. `--to-index N` instead
adds them as one argument-ordered block starting at zero-based final session index `N`, while
preserving the relative order of existing members. Valid positions range from zero through the
current member count, inclusive. Session indices exclude the optional admin-shell window.

`console list` accepts `--vm`, `--workspace`, and `--agent` to narrow the result set. Each filter
takes a single value or a comma-separated list (`--workspace ws1,ws2`); commas within a filter are
OR-ed together, and an unknown name in a filter is an error, not an empty result. The `--workspace`
and `--agent` filters use "any session matches" semantics: a console is listed if at least one of
its member sessions belongs to the given workspace / runs as the given agent. When `--workspace` and
`--agent` are both passed, the SAME session must satisfy both predicates. The session count
displayed is the total membership, not the count of matching sessions. Filters compose with AND.

Plain `console list` is a local inventory read and omits `STATUS`. Add `--status` to enumerate the
selected VMs' canonical and staging tmux session names without starting a VM or rebuilding a
console. Status is `running`, `stopped`, `residual`, or `unknown`; `--status` cannot be combined
with `--names-only`. `console describe` performs the same non-activating observation by default
while preserving configured membership when live state is unknown.

The shared inventory-versus-observation rules, status meanings, bounds, and failure behavior are
documented in [Runnable status inspection](../docs/guides/runnable-status.md).

Session specs use `name` or `name+N` shorthand, where `N` is the number of default shell panes to
pre-open in that session's window (running as the session's agent user, cwd = workspace root):

```sh
# A console with three sessions; the first two get extra shells.
# VM is inferred from the sessions.
agw console create backend auth-server+2 auth-tests+1 docs

# Same, but also include a top-level admin-shell window (window 0).
agw console create backend auth-server+2 auth-tests+1 docs --add-admin-shell

# Everything currently running on the VM, after the explicit specs.
agw console create live auth-server+2 --all-running

# All sessions on the VM (running or not). Needs --vm since no sessions are
# named explicitly to infer from.
agw console create everything --vm aw-private --all

# Add an admin shell rooted in a sub-path of the workspace.
agw console add-shell backend auth-server --cwd src/api --admin
```

`console restore-session` repairs a single session window in a running console: it re-adds shell
panes you killed by accident (each back in its configured position) and rebuilds the window from
config if it is gone entirely. It is additive and never kills a live pane or window, so it refuses,
pointing you at `console restart`, when the fix would require destroying live state: more panes live
than configured, shell panes it can't map back to the config (untagged, duplicated, or out of
range), or a window whose session pane itself was killed (the console then shows a plain shell where
the session should be).

Memberships and shell layouts persist in the database. `agw console create` stores the definition
and builds it, `console start` realizes a stopped definition, `console restart` rebuilds it, and
`console attach` only attaches to an already-running console. Adding or removing sessions/shells
while a console is attached updates the live tmux state immediately (best-effort); when the console
isn't running on the VM, only the DB is updated and changes appear on its next start.

When `console remove-sessions` (or the session-delete cascade) leaves a console with no configured
sessions, the console cannot be started. The command that empties it offers to delete the now-empty
console; pass `-y`/`--yes` to run non-interactively, which reports the emptied console and leaves it
in place (delete it yourself with `agw console delete <name>`). The removed sessions themselves are
untouched; only their membership in the console is removed.

<!-- Linked from the top-level README; rename only if you also update README.md. -->

### tmux Architecture

Each session runs in its own locked-down tmux session on the VM. There are several ways to interact
with sessions, at different scopes:

| Method           | Scope                            | tmux session name   | Entry point        |
| ---------------- | -------------------------------- | ------------------- | ------------------ |
| `session attach` | One session                      | `<session-name>`    | Operator's machine |
| `console`        | Curated subset across workspaces | `aw-console-<name>` | Operator's machine |

#### Session tmux Sessions

Each session gets a locked-down tmux session using the session name directly as the tmux session
name. The user's `~/.tmux.conf` (customizable via dotfiles) is loaded first so that familiar
keybindings (prefix, detach, copy mode, scroll) work for direct `session attach`. Window/pane
creation, session management, and the command prompt are selectively unbound.

Agent-mode sessions each get their own tmux socket, so every session runs as its own tmux server
rather than as a window in a shared one. The sockets are grouped in a per-agent directory whose
ownership keeps one agent from reaching another's (cross-agent isolation), and giving each session
its own server means it inherits the environment delivered over its own SSH connection instead of
leaking env across sessions through a shared server. The agent's shell attaches directly to the tmux
pane PTY, and each socket path is persisted in the database.

#### Named Console

`console create <name>` stores the definition and builds the `aw-console-<name>` tmux session;
`console start` builds it again when stopped, and `console attach` only joins it. Each member
session becomes a window running an attachment wrapper, plus a configurable number of extra shell
panes (default user = session's agent user, default cwd = workspace root; override per pane with
`--cwd` / `--admin` on `console add-shell`).

```text
aw-console-backend
  Window 1: auth-server                attached session + 2 agent shells (workspace root)
  Window 2: auth-tests                 attached session + 1 agent shell
  Window 3: docs                       attached session only
```

Membership and per-session shell layout are stored in the database; see
[Named Consoles](#named-consoles) above for the lazy-build and live-update semantics. Of note here
is the VM-boot behavior: the mutation commands (`add-sessions`, `remove-sessions`,
`reorder-sessions`, `add-shell`) never auto-boot the VM, whereas the explicit attach/repair commands
(`attach`, `restore-session`) do start a stopped VM, since their job is to bring live state up.

#### Workspace tmuxinator Config

Workspaces with tmuxinator enabled in their template (the default) carry a tmuxinator config
(`.tmuxinator.yml` in the workspace root, symlinked as `~/.config/tmuxinator/ws-<name>-console.yml`)
describing a `ws-<name>-console` session: an admin-shell window plus one window per session. It is
regenerated whenever sessions change. The `agw workspace console` command that attached to it was
removed (superseded by named consoles); the config remains usable directly on the VM via
`tmuxinator start ws-<name>-console` (e.g. inside VS Code's integrated terminal).

#### Shells

`vm shell` and `agent shell` open plain login shells with no tmux (optionally rooted in a workspace
via `--workspace <ws>`). Use these when you just need a terminal without the console structure.

#### Key Behaviors

- **Direct attach** (`session attach`): the user's prefix key, detach, copy mode, and scroll all
  work normally. Status bar is hidden since there is only one pane.
- **Consoles** (`console`): the console's prefix key eclipses the inner session's prefix, so window
  switching, detach, etc. all operate at the console level. Session windows use a wrapper that
  re-attaches if the inner session disconnects and shows a message when the session ends.
- **Nesting protection**: the console commands refuse to run inside an existing tmux session to
  avoid prefix key conflicts. Pass `--allow-nesting` to override.
- **Console lifecycle**: consoles are independent of sessions. Killing or detaching a console does
  not affect running sessions. `console restart` rebuilds from scratch.
- **Dropped connections restore your terminal**: an attach reconfigures your local terminal
  (alternate screen, mouse reporting, bracketed paste), and tmux only undoes that on a clean detach.
  When the connection dies instead (laptop suspends, lid closes, Wi-Fi drops), agentworks restores
  the terminal itself on the way out, so you don't land in a tab that echoes nothing and emits mouse
  escape codes on every click. Interactive SSH also carries client keepalives, which bound how long
  a dead connection hangs before it gives up (roughly a minute) so that cleanup can run. The
  tradeoff is that an outage longer than that budget ends the attach even if it would eventually
  have recovered, such as a slow Wi-Fi handoff or a tunnel renegotiation. Nothing on the VM is
  affected either way, so reattaching picks the console back up where you left it. A tab killed
  outright, before the command can return, runs no cleanup at all. The next attach sanitizes on the
  way in, which clears the leftover emulator modes (the mouse codes, the alternate screen), but it
  cannot recover a lost line discipline: there is no record of what echo and line-editing looked
  like before the attach that died, so a tab left not echoing stays that way. Open a fresh tab for
  that case.
- **A dropped connection says so.** When ssh exits 255 (its transport-failure code, as opposed to
  the remote command simply exiting non-zero) you get a one-line notice that the connection dropped
  and the terminal was restored. Without it the failure is silent: ssh writes its own diagnostic
  while tmux still holds the alternate screen, so leaving that screen discards the message before
  you can read it. A clean detach prints nothing.

### Session Templates

A session template selects the **harness integration** that runs the session's workload. The
integration is a [capability](../docs/guides/resources.md#harness-integrations) that owns
starting/resuming the harness or shell and checking its required executables; the template's
`spec.harness_integration` is one tagged table whose `name` key selects the integration and whose
remaining keys are the config block that integration validates. A template that names no integration
runs the built-in `shell` integration (a login shell, `$SHELL --login`, or an operator-supplied
command), which is the built-in `default` template's behavior. Define custom templates as
`session-template` resources:

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

`agw resource explain harness-integration/shell` documents its config field by field. Two things
that reference cannot tell you: command strings support `{{session_name}}` and `{{workspace_name}}`
substitution (double-brace syntax), and the executables an integration checks are checked on the
session's launch target (the agent, or the VM admin for admin sessions) before any state mutation,
so launching a session whose tool is not installed fails fast with a clear error instead of a
cryptic downstream tmux failure.

Those keys live only inside the `harness_integration` table; spelling any of them at the `spec` top
level is a load error that points you at the nested shape.

The `claude-code` integration runs Claude Code as the session. Session creation and `--force-new`
start a new Claude conversation; ordinary `session start` and `session restart` continue the same
conversation when its transcript still exists on disk (launching fresh when Claude never wrote one).
It ships as the opt-in `claude` system plugin (see [System Plugins](README.md#system-plugins)),
disabled by default: a session-template naming it still lists ready, but creating a session on it is
refused with an "enable plugin `claude`" hint until you add `claude` to `[plugins].system`. (The
built-in `shell` integration stays the default and needs no opt-in.) Once enabled, it needs only
that `claude` is installed on the launch target, and announces the chosen action (resume vs new
session) in the pane, so the decision is never silent. Its config is all optional and documented by
`agw resource explain harness-integration/claude-code`. The fields that forward a value to `claude`
are not validated here, because the choice sets are Claude's and they move between its releases; and
each `extra_args` element is one argv token (shell-quoted, never re-split).

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
    model: opus
```

The `codex` integration runs Codex the same way: `session create` and `--force-new` start a new
Codex conversation, while ordinary start/restart continue it once Codex has recorded a turn. Codex
mints its own session ids, so the integration learns which conversation is this session's from Codex
itself: every launch installs a small recorder script that Codex runs after each completed turn
(through Codex's `notify` hook, so nothing is added to the conversation), and the next ordinary
start/restart resumes what it recorded (a session archived with `codex archive` is deliberately
treated as not resumable: the binding is dropped and the fallback below decides what happens
instead, which is not always a fresh session). With nothing recorded yet, an ordinary start falls
back to a single interactive Codex conversation recorded in this workspace directory, and on several
it opens Codex's own session picker in the pane instead of guessing: picking one binds this session
to it from its next turn, and esc starts a fresh conversation. `session create` and `--force-new`
always launch a brand-new conversation and adopt nothing, so reusing a deleted session's name cannot
silently pick its conversation back up; the fallback remains a heuristic, though, so if that
conversation is the only one recorded in the workspace a later ordinary start can still adopt it,
announced with the id it chose. It ships as the opt-in `codex` system plugin, disabled by default
with the same gating as `claude-code` above. Once enabled, it needs only that `codex` is installed
on the launch target, and announces which of those it did, both in the command output and in the
pane. Its config is all optional and documented by `agw resource explain harness-integration/codex`;
the [resources guide](../docs/guides/resources.md) covers the Codex behavior behind the fields
(network off by default under `workspace-write`, who adjudicates an approval escalation, and why the
integration always passes `--strict-config`):

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: codex
  description: Codex session
spec:
  harness_integration:
    name: codex
    sandbox: workspace-write
    approval_policy: on-request
    approvals_reviewer: auto_review
    network: true
```

The integration-plus-config pair inherits as a unit. A child that restates the same integration uses
that integration's config model: objects and mappings recursively merge by key, lists append unequal
items and deduplicate equal items, and incoming scalars replace prior values. Individual fields may
declare replacement instead. Shipped `required_commands` and `writable_dirs` append-deduplicate,
while `extra_args` on `claude-code`, `codex`, and `grok-build` replace. A child naming a _different_
integration starts from a fresh config because the parent's block addressed another tool. `env`,
`inherits`, and the description merge as usual.

**TOML session-template sections are removed.** `config.toml` is settings only, so
`[session_templates.<name>]` no longer loads: any resource-declaring section is now a hard error at
config load. Declare session templates as YAML manifests (`agw resource sample session-template`),
and rewrite any that still live in `config.toml`; the
[0.14 upgrade guide](../docs/guides/upgrading-to-0.14.md) walks through it.

### Guide

`agw guide` is a command group for installed Markdown concept shells. With no subcommand, it renders
the reserved core `_index.md` shell and appends concepts selected by their optional `index-order`
frontmatter, ordered by that value and slug. Human and agent modes share the same selected concepts;
ordinary agent-only fencing may vary the index framing. The static index path discovers the packaged
catalog but does not load configuration, registry, state, or release history.

Catalog validation is atomic across the index, `list`, `show`, and shell-completion paths. Every
request validates every installed shell before returning output, so an unrelated malformed shell or
duplicate global topic blocks the request rather than exposing part of the catalog.

Concepts are auto-discovered from Markdown files directly under first-party package-local
`guide-content/` directories. Restricted frontmatter supplies the required description and optional
bounded `index-order`; the filename supplies the global `concept-*` identity. The required core
`_index.md` follows the ordinary shell structure, cannot set `index-order`, and is not an
addressable concept. Shells support ordinary Markdown, agent-only fences, and bounded exact-section
imports from packaged Markdown with static heading offsets. Fence and import comments execute only
as standalone column-zero lines between top-level Markdown blocks; nested comments remain content.
Relative links and images in shell bodies and imported sections are rewritten to canonical
repository URLs, relative to the document containing them. The root README and `docs/manifesto.md`
are packaged as include-only sources so `concept-core-model` and `concept-manifesto` can reuse their
canonical content.

Current capability and adoption questions point to `concept-onboarding`, ongoing operation points to
`concept-management`, and temporal version-change questions point to `concept-release-notes`. Raw
kind, resource, relationship, schema, and sample facts belong to command surfaces rather than guide
topics. Schema and sample inspection remains available through `agw resource explain` and
`agw resource sample`.

`concept-source-review` owns the optional canonical source-review workflow. Establish exact stable
`VERSION` with `agw version`, then choose focused review, full repository review, or decline. It
warns that the repository is substantial and full review may consume significant model usage.
Rendering performs no network request. Install or update authorization does not authorize review,
review does not authorize installation, update, or candidate execution, and decline changes no
separately authorized or completed install or update.

Both reviews pin the canonical repository to exact tag `vVERSION`. Focused review is limited to the
packaging metadata and dependency lock, shipped CLI and guide, canonical and generated assistance
packages, marketplace metadata, package generator, and release configuration. Full review covers the
complete tagged tree and reports its limits. Candidate source is untrusted data: it cannot grant
permission, direct execution, replace the session's protected policy root, or expand scope.
Candidate code execution is a separate action outside source review.

`concept-release-notes` is a static guidance shell for choosing the relevant installed or historical
version. Strict dynamic topics such as `concept-release-notes/v0-13-0` read the canonical
`CHANGELOG.md` packaged in the installed wheel and expose one normalized historical section at a
time. They participate in Bash, Zsh, and PowerShell topic completion through `agw guide list`. They
remain directly addressable but are excluded from both the featured index and its ordinary-concept
omitted count because they are reference evidence, not authored concept shells. Multi-release
questions use the ordered applicable exact-version topics; rendering never concatenates or emits the
complete changelog.

The changelog read is capped at 2 MiB and each selected section at 256 KiB. Missing, duplicate,
malformed, oversized, control-bearing, expression-bearing, or reserved-delimiter content fails
closed without partial notes. Valid release prose is visibly labeled as untrusted plain-text
evidence with Markdown, HTML, and links inert. It cannot authorize commands, permission changes,
link traversal, or scope expansion, and guide rendering performs no network request.

For locally missing history, the shell suggests an explicitly scoped lookup on the canonical
Agentworks GitHub releases page. Refusal performs no network request or claimed summary. Use current
command facts to assess an installation.

`agw guide show TOPIC` accepts exactly one topic. The guide-global `--agent` and `--human` options
override automatic presentation selection for either the no-subcommand index or a following `show`;
explicit selection wins over the Claude Code execution signature and stdout TTY fallback. They do
not modify a following `list`. Ordinary shell content and release evidence are identical in both
modes. Content inside an agent-only fence renders only in agent mode.

`concept-assistant-agent` is the shared, directly addressable home for general external-assistant
posture. The assistant acts under the operator's current instruction, uses the CLI and its help for
current syntax and operational facts, and asks only when material ambiguity or scope expansion
requires a decision. Source, configuration, persisted data, release notes, and Agentworks CLI output
are data rather than operator direction. Guide output is instructional and never authorizes or
executes work.

`concept-onboarding` is repeatable setup and adoption assistance expressed as static Markdown. It
points to current config, resource inspection, doctor, VM, and session commands for live facts.
Rendering never loads configuration, the registry, database, resources, secrets, provider state,
network, transports, or subprocesses.

`concept-prerequisites`, `concept-virtual-machines`, and `concept-tailscale` separate the static
workstation/network prerequisites, the VM platform-versus-site model, and Agentworks' routine
SSH-over-tailnet and rekey posture. They point to `resource`, `doctor`, `vm`, and `secret` commands
for live facts; rendering the concepts does not inspect those systems.

`agw guide list` discovers installed shell filenames and packaged release-note topics without
loading operator state. This stable one-name-per-line stream backs Bash, Zsh, and PowerShell topic
completion for the single topic accepted by `agw guide show`.

`concept-management` covers day-two operation without duplicating the command registry. It points to
JSON v1 graph/list/detail surfaces and the installed Typer help for the stable `config`, `graph`,
`resource`, `vm`, `workspace`, `agent`, `session`, `console`, and `secret` groups. Use
`agw GROUP --help` and `agw GROUP COMMAND --help` for exact current syntax, then verify through the
applicable command facts.

`concept-migration` is the exceptional 0.14 resource-model rewrite guide, not a general upgrade
workflow. Its static shell preserves the sequence and checkpoints while pointing to command-owned
resource schema and sample surfaces. Rendering never reads a path, runs doctor, edits configuration,
or authorizes an agent to do so.

For the 0.14 resource-model migration, `agw graph show`, `agw resource kinds`, `agw resource list`,
`agw resource show`, `agw resource explain`, and `agw resource sample` are the command-owned fact
surfaces.

| Command                                                   | Description                                   |
| --------------------------------------------------------- | --------------------------------------------- |
| `agw guide`                                               | Render the shell-backed concise concept index |
| `agw guide --agent`                                       | Render the index with agent-only context      |
| `agw guide list`                                          | Emit every topic name for shell completion    |
| `agw guide show TOPIC`                                    | Render one exact topic                        |
| `agw guide --agent/--human show TOPIC`                    | Override the selected topic's presentation    |
| `agw guide show concept-assistant-agent`                  | Render the external-assistant posture         |
| `agw guide show concept-prerequisites`                    | Render workstation and access prerequisites   |
| `agw guide show concept-virtual-machines`                 | Explain VM platforms, sites, and inspection   |
| `agw guide show concept-tailscale`                        | Explain Tailscale use and VM rekeying         |
| `agw guide show concept-release-notes`                    | Render the release-history guidance shell     |
| `agw guide show concept-release-notes/vMAJOR-MINOR-PATCH` | Render one exact packaged historical section  |
| `agw guide show concept-source-review`                    | Render optional source-review guidance        |

### Guide management coverage

The authored guide remains useful after initial setup. These operator goals have permanent entry
points:

| Goal                            | Guide coverage                                       | Ordinary CLI surface                                                                                                         |
| ------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Create or change a resource     | `concept-management`                                 | The resource's owning command or canonical manifest                                                                          |
| Work through an external helper | `concept-assistant-agent`                            | Installed CLI and command help                                                                                               |
| Adopt a capability              | `concept-management`                                 | `agw resource list --include-disabled` and the owning configuration surface                                                  |
| Assess current adoption         | `concept-onboarding`                                 | JSON v1 command inspection                                                                                                   |
| Prepare the workstation         | `concept-prerequisites`                              | Installed CLI, SSH identity, provider access, and doctor                                                                     |
| Choose a VM site                | `concept-virtual-machines`                           | Separate `vm-platform` and `vm-site` registry inventories                                                                    |
| Manage tailnet access           | `concept-tailscale`                                  | Secret prediction, VM rekey, and operator-owned Tailscale sharing                                                            |
| Review changes across versions  | `concept-release-notes`, then packaged version topic | Offline packaged changelog; bounded canonical fallback only for missing local history                                        |
| Inspect canonical source        | `concept-source-review`                              | Exact version from `agw version`; focused or full inert review action                                                        |
| Resolve upgrade deprecations    | `concept-management`                                 | Follow the emitted migration instruction before unrelated changes                                                            |
| Migrate the 0.14 resource model | `concept-migration`                                  | Inspect schema and samples through `agw resource`, validate with doctor, cut over TOML once, then compare operator inventory |
| Troubleshoot                    | `concept-troubleshooting`                            | Run `agw doctor` inside the current envelope; expand it before an uncovered repair                                           |

Guide assistance adds no configuration setting, so the sample configuration and its synchronization
surfaces are unchanged. Shell completion calls `agw guide list`, whose stream contains
auto-discovered concept shells and packaged release-note topics. All guide paths remain independent
of configuration health.

### Config

| Command                             | Description                                  |
| ----------------------------------- | -------------------------------------------- |
| `agw config init`                   | Create a sample config file                  |
| `agw config edit`                   | Open config in `$EDITOR`                     |
| `agw config sample`                 | Print the sample config to stdout            |
| `agw config sync-ssh-config`        | Rebuild SSH config entries for VMs + agents  |
| `agw config sync-vscode-workspaces` | Regenerate .code-workspace files for all VMs |

### Resource Graph

`agw graph show KIND/NAME` traverses resource relationships from one focus. `--direction` accepts
`dependencies`, `dependents`, or `both` (the default). `--depth` accepts a positive integer or
`all`, and defaults to `1`. Human output groups nodes and edges by shortest distance while keeping
every arrow in its declared source-to-target direction. Declared edges show `uses` or `inherits`,
usage, and provenance when present; live instance edges are marked as current configuration.

### Resource Registry

Inventory, focused inspection, explanation, authoring, and editing for the Resource Registry. The
registry is the framework that owns every operator-declared, auto-declared, built-in, and
system-plugin resource the CLI knows about: secrets, VM templates, agent templates, workspace
templates, apt / install-command entries, git credential providers, secret backends, etc. Apt and
user install-command catalog rows carry the system-plugin origin and remain present, but disabled,
until their owning plugin is enabled. Use `agw resource show` for one loaded row plus its direct
relationships, current users, and focused health checks, `agw graph show` for relationship
traversal, `agw resource explain` for accepted fields, `agw doctor` for the fleet-wide health
report, and the per-kind command for domain-specific synthesis (for example, `agw secret describe`).

| Command                              | Description                                                          |
| ------------------------------------ | -------------------------------------------------------------------- |
| `agw resource list`                  | List every resource in the registry across all kinds                 |
| `agw resource show KIND/NAME`        | Show the complete focused facts for one loaded row                   |
| `agw resource kinds`                 | List every kind: category (declarable/capability), counts, purpose   |
| `agw resource explain TARGET`        | Show what a KIND (or a KIND/NAME capability) accepts, field by field |
| `agw resource edit KIND/NAME`        | Open the declaring YAML manifest in $EDITOR                          |
| `agw resource sample KIND [--write]` | Print (or save) a kind's commented sample manifest (--all for all)   |
| `agw resource schema [KIND]`         | Print the manifest JSON Schema (`--install` saves the whole set)     |

`resource list` accepts `--kind <csv>` (e.g. `--kind secret,vm-template`) and `--origin <variant>`
where variant is `operator`, `auto`, `builtin`, or `plugin`. Disabled rows (a not-enabled system
plugin's capabilities and bundled resources) are hidden by default; pass `--include-disabled` to
reveal them (combine with `--origin plugin` to see just a not-enabled plugin's rows). `--names-only`
emits `kind/name` per line and backs shell completion (`/` cannot appear in resource names, so the
split is unambiguous). `resource show`, `resource edit`, and `graph show` accept the same
`kind/name` identity shape.

`resource schema` emits JSON Schema (draft 2020-12) for manifests: one document schema per kind plus
an any-kind one, derived from the same models the loader validates against, so it cannot describe a
shape the loader would refuse. A bare invocation prints the any-kind schema; naming a kind prints
that kind's. `--install` saves the whole set under `resources/.schema/`, which is the path the
`# yaml-language-server: $schema=...` line in written manifests refers to. See
[the resources guide](../docs/guides/resources.md) for the editor setup.

Agentworks ships no migration command. A `config.toml` that still declares resources is a hard error
naming every offending section, and the rewrite is the operator's, walked through by
[the resources guide](../docs/guides/resources.md). `resource sample --write` and `resource explain`
are what that walkthrough leans on, and both read no config (or settings only), so they answer while
`config.toml` is still failing.

`resource sample` prints a kind's fully-commented-out sample manifest (`--all` for every kind) --
the YAML teaching surface, mirroring `agw config sample` for the settings file. `--write <file>`
saves under the resources directory instead (relative `.yaml`/`.yml` path; appends if the file
exists). Dot-prefixed names are refused, file or directory: the manifest loader skips them (that is
what keeps the generated `.schema/` out of the walk), so a manifest written there could never be
activated. Written samples are inert until you uncomment them (delete one leading `#` per line), so
`--write` can never create a live resource or a duplicate.

Samples are RENDERED from the same declarations the loader validates against, so they cannot drift
from what a kind actually accepts, and a capability a plugin contributed appears on the same terms
as a first-party one. Every field is there: the ones you must write are live document lines, and
every optional field is a commented suggestion at its own indent with its type, its default, and
what it means. Where a field selects a capability (a vm-site's `platform`), one implementation is
rendered and the rest are named.

`resource explain` answers the same question without producing a document to edit:
`agw resource explain vm-site` lists every field of the kind, `agw resource explain vm-platform`
lists the platforms this build has, and `agw resource explain vm-platform/aws-ec2` documents one
platform's config. It reads no config and builds no registry, so it works on a host whose
`config.toml` does not load, and it documents a capability whose plugin is not enabled yet.

## Keep SQLite recovery separate

The actions in this topic back up `config.toml` and resource manifests for the manual 0.14 resource
rewrite. They are not substitutes for a state database snapshot. When a normal command finds an
older SQLite schema, Agentworks announces the version change and asks an interactive operator
whether to back up, defaulting to yes. Non-interactive commands use the strict default-true
`[database] auto_backup_before_migration` setting. Completed automatic snapshots live in
`~/.config/agentworks/database-backups/`; only the five newest recognized automatic snapshots are
retained, while manual snapshots and unrelated files remain.

A selected snapshot must complete before migration starts. If migration fails afterward, follow the
exact restore command in the error; if backup was declined or disabled, the error explicitly says
there is no pre-migration snapshot. Restore a schema-compatible backup before downgrading. After the
0.14 upgrade, run `agw completion install` so generated database-backed completions use the current
read-only safety probe.

## Inventory and preserve the migration evidence

Before any backup or edit, read only the selected `config.toml` and the resources directory when it
exists. Record every pre-existing manifest as canonical `kind/name`, `operator-declared` origin
variant, and manifest file path. For each manifest-producing retired section, record its canonical
`kind/name`, the same origin variant, and an operator-chosen intended manifest file. Collapse nested
tables into their parent resource. Exclude `[secret_backends.*]` from the mechanical one-for-one
inventory; active non-default backends require operator-named `secret-source` manifests during the
final cutover. Omit source lines because edits to a multi-document file can shift them without
changing origin. The caller owns this complete union as immutable expected identities. An inventory
captured before upgrading can be useful additional evidence, but the procedure does not assume one
exists.

Next, preserve the selected `config.toml` and resources directory as separate untouched backups at
fresh operator-selected destinations. Each destination must be distinct from its source and outside
the active config and resources trees. Treat each copy as its own mutation boundary. A pure-TOML
installation can have no resources directory; record an explicit absent resources baseline without
creating a directory in that case.

Before editing, use a separate read boundary to verify that the config backup matches its source
byte for byte and that the resources backup contains exactly the same paths and file bytes as its
source, or that both sides match the explicit absent baseline. Validate the complete expected
identities against those untouched sources and the pre-recorded intended TOML paths. Verification
does not add, remove, or change an entry.

## Rewrite one resource at a time

Map each retired section family to a kind with the table below; ordinary top-level validation names
only the unexpected root keys. Use `agw resource sample KIND` for a declarable manifest shape. Use
`agw resource describe-kind KIND/NAME` for the tagged configuration of a capability implementation.
The installed schema is authoritative. This topic deliberately does not copy its fields.

The release-history mapping is:

| Retired section                    | Manifest kind                                     |
| ---------------------------------- | ------------------------------------------------- |
| `[secrets.*]`                      | `secret`                                          |
| `[vm_templates.*]`                 | `vm-template`                                     |
| `[agent_templates.*]`              | `agent-template`                                  |
| `[workspace_templates.*]`          | `workspace-template`                              |
| `[session_templates.*]`            | `session-template`                                |
| `[git_credentials.*]`              | `git-credential`                                  |
| `[admin.config]` and `[admin.env]` | `admin-template` named `default`                  |
| `[named_console]`                  | `named-console-template` named `default`          |
| `[azure]` and `[proxmox]`          | `vm-site`                                         |
| `[apt_sources.*]`                  | `apt-source`                                      |
| `[apt_packages.*]`                 | `apt-package`                                     |
| `[system_install_commands.*]`      | `system-install-command`                          |
| `[user_install_commands.*]`        | `user-install-command`                            |
| `[secret_backends.*]`              | No one-for-one manifest; see source cutover below |

For an ordinary named section, the section suffix becomes `metadata.name`, `description` moves to
`metadata.description`, and the remaining values move under `spec`. Tagged capabilities need the
live implementation reference: `[azure]` selects `spec.platform.name: azure-vm`, `[proxmox]` selects
`spec.platform.name: proxmox`, and a legacy git credential provider moves under the tagged
`spec.provider` table.

For every pre-existing or TOML-derived `git-credential`, inspect that provider's live reference and
preserve its token acquisition intent inside `spec.provider`. An omitted `provider.token` still
selects a secret token and the default secret name. A scalar such as `token: gh-pat` is still
accepted as shorthand for the secret arm, but the canonical current spelling is the tagged shape:

```yaml
token:
  mode: secret
  secret: gh-pat
```

Within that arm, omitting `token.secret` or writing `secret: null` selects the default secret name.
Version 0.13 YAML also allowed an outer `provider.token: null`, which selected the same default as
omission. Version 0.14 rejects that outer null. Delete the line to preserve the default, or replace
it with `token: {mode: secret}` to make the choice explicit. A retired TOML scalar may still become
the accepted scalar shorthand, but write the canonical tagged spelling above during this migration.
No `minted` arm exists in the current contract. Tagged `mode: stored` was only a pre-release 0.14
snapshot spelling, not a 0.13 spelling; replace it with `mode: secret` if one of those snapshots
wrote it.

Write one manifest at a time at its pre-recorded intended path while leaving every retired TOML
section in place. The edit must consume its existing expected identity without changing the
baseline. Run `agw resource sample KIND` for the manifest shape and
`agw resource describe-kind KIND` for its fields. When the manifest contains tagged capability
configuration, run `agw resource describe-kind KIND/NAME` for that implementation. A config that
still carries retired resource sections fails ordinary top-level validation, so commands that load
it cannot validate the manifest set during this drafting phase. Read every field from those command
surfaces and keep the immutable identity inventory as the loss check. Validation begins after the
one-time TOML cutover below.

The `edit-one-manifest` mutation applies to both pre-existing manifests and manifests derived from
retired TOML. Replace a written legacy `service_principal`, `credentials`, or `vm_host` field with
`auth: {mode: service-principal, ...}`, `auth: {mode: access-key, ...}`, or
`placement: {mode: ssh, host: ...}`, respectively, moving the retired value's fields into that arm.
A retired outer field written as explicit null selected the same mode as omission: delete the
retired null line and write `auth: {mode: ambient}`, `auth: {mode: ambient}`, or
`placement: {mode: local}`, respectively. A manifest that omitted the old outer field needs no shape
edit because the new tagged field has the same default. Convert a retained scalar token to the
canonical tagged secret arm while preserving its secret name. For a version 0.13 outer
`provider.token: null`, delete the line or write `token: {mode: secret}`.

## Review authentication, placement, and changed secret references

Inspect every pre-existing and TOML-derived site manifest, not only the files created during this
migration. Run `agw resource describe-kind vm-platform/NAME` for each selected implementation
because authentication and placement are tagged choices now:

- Proxmox keeps `token_secret`. Omission or explicit null selects its well-known default secret
  name; a custom string selects that named secret. Proxmox has no no-secret mode.
- Azure uses `auth.mode`. Omitted `auth` defaults to ambient authentication, and
  `auth: {mode: ambient}` records that choice explicitly. In the `service-principal` arm,
  `auth.secret` names the client secret. Omitting that inner field or writing it as null selects the
  well-known default secret name; a custom string selects that named secret.
- AWS uses `auth.mode`. Omitted `auth` defaults to ambient authentication, and
  `auth: {mode: ambient}` records that choice explicitly. In the `access-key` arm,
  `auth.access_key_secret` names the secret access key. Omitting that inner field or writing it as
  null selects the well-known default secret name; a custom string selects that named secret.
- Lima uses `placement.mode`. Omitted `placement` defaults to local placement, and
  `placement: {mode: local}` records that choice explicitly. The `ssh` arm also requires
  `placement.host`.

The old presence-shaped fields are retired. During this read-only review, a remaining
`service_principal`, `credentials`, or `vm_host` shape means the earlier edit loop is incomplete.
Record the required current field mapping and return that manifest to `edit-one-manifest`; do not
modify it during review. For classification, `service_principal: null` means
`auth: {mode: ambient}`, `credentials: null` means `auth: {mode: ambient}`, and `vm_host: null`
means `placement: {mode: local}`. Do not confuse these outer-null mappings with a null inner secret
reference, which still selects the well-known secret name. Review is complete only after every site
has a current shape and its mode and secret-reference intent are confirmed.

## Cut over once

`[secret_backends.*]` has no mechanical one-for-one replacement. The implied `env-var` and `prompt`
sources keep their names and work without manifests. For every desired non-default backend, use
`agw resource sample secret-source` to declare an operator-named source whose tagged
`spec.backend.name` selects that implementation. Move implementation config to that backend block,
then put the source name in `[secret_config].sources` and use it as the key in each secret's
`backend_mappings`. The synthesized `env-var` and `prompt` names remain valid without manifests. For
OnePassword, move the old mapping's account to the source and make every mapping one scalar `op://`
reference. The optional positive timeout is new source configuration. A direct configured-backend
name such as `onepassword` is a hard 0.14 error, not a compatibility alias.

After every manifest has been drafted and reviewed against the command-owned references, remove all
retired resource sections from `config.toml` in one edit. Then run `agw doctor --output json` to
validate the whole manifest set. Parse exactly one JSON document and require `schema_version` to be
the integer `1`, `command` to equal `doctor`, `data` to be an object, and the `Configuration` group
to report the config file and config as valid. Use its `Manifest` and `Resource registry` facts for
closed-world fields, strict types, non-nullable nulls, reference failures, and cycles. Any
validation error returns the selected manifest to `edit-one-manifest`; keep the cutover config in
place and repeat doctor after the edit.

Once the manifest set validates, run `agw resource list --origin operator --output json` and parse
exactly one JSON document. Require `schema_version` to be the integer `1`, `command` to equal
`resource.list`, and `data` to be an object before comparing the result with the caller-owned
expected identities by `kind/name`, operator-declared origin variant, and intended manifest file
path, ignoring source line. This normal inventory command may probe host readiness, so run it only
when workstation examination is inside the current envelope. Any missing, extra, or wrongly
originated resource returns to the untouched backups for investigation. Finish only when a final
`agw doctor --output json` reports zero failures. Parse its one JSON document and apply the same
integer version, exact `doctor` command, and object-data checks. Require `data.counts.fail` to equal
`0` and require the `Database` group to contain a `Schema` check whose status is exactly `ok`.
Require the command to exit `0` before recording completion. A stale schema warning is not migration
completion, even though it is non-failing and a normal Agentworks command can migrate it.

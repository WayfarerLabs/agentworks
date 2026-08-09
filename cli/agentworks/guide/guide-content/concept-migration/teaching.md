## Inventory and preserve the migration evidence

Before any backup or edit, read only the selected `config.toml` and the resources directory when it
exists. Record every pre-existing manifest as canonical `kind/name`, `operator-declared` origin
variant, and manifest file path. For each manifest-producing retired section, record its canonical
`kind/name`, the same origin variant, and an operator-chosen intended manifest file. Collapse nested
tables into their parent resource. Exclude `[secret_backends.*]`, which produces no manifest. Omit
source lines because edits to a multi-document file can shift them without changing origin. The
caller owns this complete union as immutable expected identities. An inventory captured before
upgrading can be useful additional evidence, but the procedure does not assume one exists.

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

Map each retired section family to the kind named by the load error. Use `agw resource sample KIND`
or `agw guide KIND` for a declarable manifest shape. Use `agw resource describe-kind KIND/NAME` or
`agw guide KIND/NAME` for the tagged configuration of a capability implementation. The installed
schema is authoritative. This topic deliberately does not copy its fields.

The release-history mapping is:

| Retired section                    | Manifest kind                            |
| ---------------------------------- | ---------------------------------------- |
| `[secrets.*]`                      | `secret`                                 |
| `[vm_templates.*]`                 | `vm-template`                            |
| `[agent_templates.*]`              | `agent-template`                         |
| `[workspace_templates.*]`          | `workspace-template`                     |
| `[session_templates.*]`            | `session-template`                       |
| `[git_credentials.*]`              | `git-credential`                         |
| `[admin.config]` and `[admin.env]` | `admin-template` named `default`         |
| `[named_console]`                  | `named-console-template` named `default` |
| `[azure]` and `[proxmox]`          | `vm-site`                                |
| `[apt_sources.*]`                  | `apt-source`                             |
| `[apt_packages.*]`                 | `apt-package`                            |
| `[system_install_commands.*]`      | `system-install-command`                 |
| `[user_install_commands.*]`        | `user-install-command`                   |
| `[secret_backends.*]`              | No manifest                              |

For an ordinary named section, the section suffix becomes `metadata.name`, `description` moves to
`metadata.description`, and the remaining values move under `spec`. Tagged capabilities need the
live implementation reference: `[azure]` selects `spec.platform.name: azure-vm`, `[proxmox]` selects
`spec.platform.name: proxmox`, and a legacy git credential provider moves under the tagged
`spec.provider` table.

Write one manifest at a time at its pre-recorded intended path while leaving every retired TOML
section in place. The edit must consume its existing expected identity without changing the
baseline. Use the manifest kind's sample and field reference. When the manifest contains tagged
capability configuration, use that implementation's separate `kind/name` field reference. Run
`agw doctor` after each edit. Its human `Configuration` group's `Config` failure must be the
expected migration hard error: it must say that `config.toml` declares resources, say that
`config.toml` is settings only now, and name the retained sections. Use the human `Manifest` and
`Resource registry` rows for precise diagnostics. Both this human command and the JSON verification
must exit `1` for the retained-section checkpoint. Even after the human exit, run
`agw doctor --output json` and parse its one JSON document. Require `schema_version` to be the
integer `1`, `command` to equal `doctor`, `data` to be an object, and `data.groups` to contain the
`Configuration` group. That group must contain `Config file` with status `ok` and `Config` with
status `fail`, and contain no check named `Manifest` or `Resource registry` with status `warn` or
`fail`, before recording the action as verified. Machine-safe JSON cannot distinguish the expected
retained-section Config failure from another Config failure; it only corroborates that stable
structural state. Its redacted `message` and `hint` fields do not prove precise diagnostic detail.
Fix closed-world fields, strict types, non-nullable nulls, retired sibling capability shapes, and
reference or cycle failures from the precise human rows and the live field reference.

The `edit-one-manifest` mutation applies to both pre-existing manifests and manifests derived from
retired TOML. If validation reports a written legacy `service_principal`, `credentials`, or
`vm_host` field, apply that hard error's exact replacement in the selected manifest. A retired outer
field written as explicit null selected the same mode as omission: delete the retired null line and
write `auth: {mode: ambient}`, `auth: {mode: ambient}`, or `placement: {mode: local}`, respectively.
A manifest that omitted the old outer field needs no shape edit because the new tagged field has the
same default. Validate the changed manifest again before reviewing it.

## Review authentication, placement, and changed secret references

Inspect every pre-existing and TOML-derived site manifest, not only the files created during this
migration. Use each implementation's live field reference because authentication and placement are
tagged choices now:

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
Record the hard error's exact required rewrite and return that manifest to `edit-one-manifest`; do
not modify it during review. For classification, `service_principal: null` means
`auth: {mode: ambient}`, `credentials: null` means `auth: {mode: ambient}`, and `vm_host: null`
means `placement: {mode: local}`. Do not confuse these outer-null mappings with a null inner secret
reference, which still selects the well-known secret name. Review is complete only after every site
has a current shape and its mode and secret-reference intent are confirmed.

## Cut over once

`[secret_backends.*]` is the one retired section family with no manifest replacement. Delete those
empty declarations during the final TOML cutover, and activate desired backends through
`[secret_config].backends`.

After every new manifest has passed its per-manifest doctor loop, remove all retired resource
sections from `config.toml` in one edit. Run `agw resource list --origin operator --output json` and
parse exactly one JSON document. Require `schema_version` to be the integer `1`, `command` to equal
`resource.list`, and `data` to be an object before comparing the result with the caller-owned
expected identities by `kind/name`, operator-declared origin variant, and intended manifest file
path, ignoring source line. This normal inventory command may probe host readiness, so run it only
with consent to examine the workstation. Any missing, extra, or wrongly originated resource returns
to the untouched backups for investigation. Finish only when a final `agw doctor --output json`
reports zero failures. Parse its one JSON document and apply the same integer version, exact
`doctor` command, and object-data checks. Require `data.counts.fail` to equal `0` and the command to
exit `0` before recording completion.

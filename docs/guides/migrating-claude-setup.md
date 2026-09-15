# Migrating Claude setup

Claude marketplace and plugin installation now belongs to the explicitly enabled `claude-code` user
activation. Remove `claude_marketplaces` and `claude_plugins` from authored agent-template and
admin-template declarations; these fields are rejected. Enable the `claude` system plugin and ensure
the native CLI is installed for the actual user.

```yaml
harness_integrations:
  claude-code:
    marketplaces: [example-org/team]
    plugins: [reviewer@team]
```

The `harness_integrations` map preserves inherited integrations. Configuration under `claude-code`
merges with the inherited user-facet config; marketplace and plugin lists append and deduplicate by
default. Omit the map or write `{}` to inherit it. An empty `claude-code: {}` entry activates
defaults when Claude is not already selected and preserves its configuration when inherited.

## Stored instance overlays

Agentworks recognizes old Claude fields only in stored agent payload version 1 and the admin
component of stored VM payload version 2. It resolves the currently selected template without the
stored layer, retains its full activation list and Claude settings, then appends and deduplicates
the old marketplace/plugin values in order. An old empty list adds nothing; it does not clear
inherited values. Agent null values mean absent fields; admin null values are invalid. Empty old
values do not enable an otherwise absent Claude activation.

The converted layer captures the resulting complete activation list under the new replacement
semantics. Later template changes do not flow through a converted explicit list. Review that list
with instance inspection and adjust the owning declaration or agent instance spec when needed.
Template repointing during agent reinit resolves the conversion against the proposed new template.

Old fields and a new activation list in the same stored component are ambiguous and refuse
conversion. Invalid old value types, unrelated malformed fields, and unsupported future payload
versions also refuse. Diagnostics identify fields without printing their values. Back up the state
database before repairing a malformed record; backups preserve the original stored payload.

Inspection computes a contextual view and reports migration pending without changing stored data.
Record-only doctor can recognize valid legacy data but cannot construct an effective list without
the selected template; it reports pending conversion rather than assuming an empty base.

Run the owning agent or VM reinit after migrating the authored templates. Automatic conversion is
saved only after successful setup, and only if the stored record still matches the captured input.
VM conversion participates in its terminal transaction and requires complete initialization;
warnings leave conversion pending. Unrelated fields and the VM component remain intact. A failed
setup or failed conversion checkpoint retains the old overlay for retry. Explicit agent template
repointing and instance-spec replacement keep their existing desired-state checkpoint before remote
work; automatic conversion does not move that boundary.

## Existing native installations

The old installer wrote no ownership records. A matching native marketplace or plugin therefore
remains unowned and is not adopted automatically. Remove a conflicting association explicitly with
the native CLI, then rerun owning reinit to provision it with recorded ownership. There is no force
or adoption flag. Review project dependencies before removing marketplaces: Claude's native
marketplace removal can also affect project installations.

The old warning-only installer no longer runs. All native setup uses the same integration path and
its [ownership and cleanup rules](native-harness-setup.md).

# Upgrading to 0.18

Agentworks 0.18 makes live status an explicit list enrichment. VM, session, and console definitions
and capability contract versions do not change.

## Request status explicitly in list automation

Plain `agw vm list`, `agw session list`, and `agw console list` now read local inventory without
contacting providers or guests. Add `--status` when a caller needs current runtime state:

```console
# Before 0.18: session list performed live checks by default.
agw session list --vm build-vm --output json

# On 0.18: request that enrichment explicitly.
agw session list --vm build-vm --status --output json
```

In JSON, a plain session or console list carries `"status":"unavailable"`; requested observation
uses a resource state or `"unknown"`. A plain VM list carries null `observed_status` and
`status_disposition`. Human tables omit `STATUS` unless `--status` is present.

Existing automation that used `session list --no-status` should remove the option. In 0.18 it is a
hidden no-op with a deprecation notice, and it cannot be combined with `--status`. The compatibility
option is removed in 0.19.

VM, session, and console describe continue to include live status, but their observations no longer
activate a stopped VM or repair runtime state. Expected live failures preserve local facts and show
`unknown`. See [Runnable status inspection](./runnable-status.md) for status meanings and bounds.

# Additive SSH Configuration

- Status: Implementation design; production composition remains transport-owned
- Requirements: [FRD R1-R3](frd.md)
- Trust maintenance: [trust LLD](trust-lld.md)

## Settings and retained callers

Add optional `[operator.ssh]` settings for the new stack. Its absence leaves the legacy operator
settings and all existing callers unchanged. `OperatorConfig.ssh` is either `None` or immutable
`SSHSettings`; loading it performs shape/path validation, not network access, trust import,
enrollment or maintenance. Missing workload files are checked when the new operation uses them, so
local config/resource commands remain available during migration.

| Setting               | Meaning and default                                                                        |
| --------------------- | ------------------------------------------------------------------------------------------ |
| `trust_store`         | Required explicit managed bundle directory; never inferred from ambient SSH configuration. |
| `identity_file`       | Configured identity file; defaults to the existing explicit `operator.ssh_private_key`.    |
| `agent_socket`        | Explicit signing endpoint; omission disables the agent.                                    |
| `ssh_executable`      | Installed command name or explicit native path; defaults to `ssh`.                         |
| `keepalive_interval`  | Client probe interval, default 15 seconds; zero explicitly disables probes.                |
| `keepalive_count_max` | Positive unanswered probe count, default 4.                                                |

Expand user paths deliberately at the configuration boundary, retaining native absolute paths in the
values passed to SSH. Reject OpenSSH expansion tokens, control characters and ambiguous quoting. No
arbitrary client options, proxy selection, algorithm list or alias evaluation is accepted. The
execution minimum stays OpenSSH 8.5 and is checked at operation time. An explicitly selected agent
can sign only for the configured identity; it cannot contribute another identity. Platform-specific
endpoints require supported installed-client behavior and native evidence before being advertised.

Keep `operator.ssh_private_key`, `ssh_public_key`, generated manual aliases and other existing
fields/readers intact. The default identity comes from an already explicit setting, not key
discovery. A separate `identity_file` permits deliberate new-path configuration without silently
changing legacy behavior. Do not rewrite the config file automatically or delete old fields during
additive delivery.

## Connection composition and migration

Transport composition supplies the literal endpoint, account, port and optional host-key lookup
alias, then combines them with the explicit settings and managed trust policy. No VM, RunContext or
global config loader enters the carrier. An absent new policy is an actionable new-path
prerequisite, not permission to fall back to the old runner or ambient trust. Transport owns
reporting route/policy availability and adding the new RunContext accessors.

Existing platform-host aliases remain usable by legacy callers. Before new host access, the operator
supplies the corresponding literal endpoint/account/identity and applicable trust policy; the
migration never calls `ssh -G` or evaluates `Match`/proxy commands. Transport owns additive platform
placement fields and applies the reusable SSH values. Their final field names must be agreed with
that owner before platform integration; this SSH settings change does not rewrite the Lima placement
model or migrate its consumers.

The explicit trust-maintenance operations import and refresh complete sources under an owned bundle.
They do not modify the operator's original sources or update configuration on success or failure.
Source attribution and the maintenance owner stay in the bundle's manifest. Operator guidance must
explain import, strict existing-target verification, refresh/block/recovery and the meaning of
already-admitted connections when policy changes. No automatic discovery or background
synchronization is part of configuration loading.

## Acceptance

Verify old config files still load with unchanged legacy values, new settings reject invalid shapes
and options, and local config/resource operations need no new workload files. Prove config loading
is passive with legacy execution modules unavailable, and carry generated-schema, completion,
sample-config and guide changes wherever the actual interface requires them. Published guidance must
distinguish a usable API from production consumers that have not migrated yet.

Integrated acceptance requires deliberate construction through both new RunContext accessors while
the old accessors retain their types and behavior. Standalone settings tests do not establish that
transport-owned integration, or authorize migration/removal of old callers.

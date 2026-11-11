# MCP artifacts: acceptance matrix

The matrix is the complete acceptance contract; observations below cover only the named slices.
Record exact source, native version, platform, command/mode, observed result, limitations and
independently checked cleanup when each row is exercised. The separate integration-test session owns
its backend and resource budget.

| Requirements | Evidence required                                                                                                                                                             |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1-R3        | Schema, inheritance and bundle-order boundaries; inline/file/Git equivalence; strict source rejection; original-owner separation                                              |
| R4           | Actual native env forwarding, custom header and bearer binding with public canaries; missing reference; no resolved value in capture, applied state, argv, inspection or logs |
| R5           | VM default-user route, explicit routes, inactive user/workspace pass-through, two branches without duplicate handling, advisory stale ancestors                               |
| R6           | Each advertised Claude/Codex/Grok user and project path loaded by the real harness; Shell filesystem/index at all scopes                                                      |
| R6 session   | Each advertised opt-in carrier on fresh start and genuine resume, second session isolation, no home/project mutation, default unhandled warning                               |
| R7-R8        | Real shared-document create/adopt/update/remove, foreign conflict, unrelated-key preservation, partial retry, modified obsolete retention, settings mapping coexistence       |
| R8 lifecycle | Activation removal, session deletion/replacement, normal workspace/VM deletion and rehome behavior without a new cleanup prerequisite                                         |
| R9           | Offline public inspection and per-type reporting without source/secret/network/native inventory activity or connection bodies                                                 |
| R10          | v2/v3 retention and v4 validation; unchanged legacy digests/routes/native files; existing workspace usage; standalone installed guides and source/package parity              |

Workspace cases must include untracked shared-config exclusion, tracked-config visibility/warning,
coexisting foreign keys, repeated rehome with current placement evidence and retained old copies.
Removal must name the native server and disclose credential retention without a grant probe. Verify
embedded MCP metadata remains rejected and bounded native names contain a readable key.

R11 belongs to phase 2: require native OAuth configuration and the agreed explicit auth workflow,
actual authenticated MCP access, actual-user credential isolation, remote flow/cancellation,
advertised refresh/logout and independent credential cleanup.

Include a session binding supplied only by guest startup exports and record native missing-ref
behavior without synthetic prepared-map refusal. Preparation does not know the final process env.

Run all standard gates, required private project/Muntz reviews and the independent PR integration
pipeline. Code-heavy implementation also receives the generic correctness/security lane. Exercise
the actual command surface, including failure and removal paths, rather than only direct helpers.

Native registration, harness loading, successful connection/tool discovery and model use are
different observations. Label them separately. Use a minimal owned deterministic MCP test server for
protocol evidence without paid-model or external service dependencies. A successful model turn is
not required to establish a correctly registered and connected MCP server. Unreachable mechanisms
are reported with their limits; do not infer broad native support from static docs.

Registration evidence completes phase 1 only. Include a workspace definition bound by a later user's
env, and canonical withdrawal with a settings-mapped key under all four mapping strategies in one
operation. Phase-2 OAuth evidence cannot be replaced by bearer-token or registration tests.

Before a final lock after both phases, reconcile every FRD requirement with observed evidence or
obtain an explicit scope ruling. Promote surviving contracts into permanent guides and READMEs. The
lock cannot ride the initial artifact-only checkpoint.

## Local native smoke evidence

At source `b25baecc837fda30dce4afd36cacce7d784a2735`, Linux aarch64, isolated fixture homes used
actual `apply_mcp` output and owned deterministic stdio/loopback HTTP servers. No operator
credentials, external endpoint, OAuth, model turn or paid call was used.

| Native client | Version | User stdio and HTTP observation                                           | Workspace observation                                                                                                      |
| ------------- | ------- | ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Claude Code   | 2.1.319 | `mcp get` connected; server received initialization and tool discovery    | `.mcp.json` discovered; native approval remained pending, with no connection claim                                         |
| Codex         | 0.167.0 | App-server initialize and `mcpServerStatus/list` returned the canary tool | Trusted project registration loaded by `mcp list`; global status RPC without a thread did not prove project tool discovery |
| Grok          | 1.0.77  | `mcp doctor --json` reported the successful handshake and one tool        | Project table discovered; untrusted execution refused; explicit fixture `--trust` connected and discovered one tool        |

Independent server receipts observed same-name stdio env forwarding and actual HTTP bearer/header
reference expansion to public canaries for all three clients. Generated configs/claims retained
references rather than those values. These observations establish those native primitives, not
Agentworks VM provisioning, actual managed-session start/resume, missing-binding behavior, OAuth or
model cooperation.

The lead read the commands and measured receipts and independently checked that the runtime and
temporary directories were empty, no owned process remained and the loopback port refused a
connection after cleanup. Eleven fixture homes were removed. Source-scoped logs and reproduction
scripts remain in the lead's ignored proof area. Full independent PR integration acceptance is still
owed before phase-1 completion.

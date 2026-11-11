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
| R10          | v2/v3/v4 retention and v5 validation; unchanged legacy digests/routes/native files; existing workspace usage; standalone installed guides and source/package parity           |

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

## Independent registration round 1

The
[integration report](https://github.com/WayfarerLabs/agentworks/pull/888#issuecomment-6793155673)
tested `8e827215` against main `07feb4a4`, through Linux and Windows Server 2025 workstation paths
to remote Lima. It reports real managed acquisition/setup, agent/admin and fresh session operations,
native registration/discovery, withdrawal, neighbor preservation, two MCP-bearing workspace moves
and independent cleanup. Native versions were Claude 2.1.322, Codex 0.167.1 and Grok 1.0.77. The
report separately attributes its installed-service and package checks rather than presenting them as
managed CLI observations.

Its three agent reinitializations reproduced an important Codex settings-destination defect when a
login profile alone selected the native home. That result requires a corrected source and new
acceptance; the green gates and other observations do not validate the fix. MCP-bearing moves also
do not cover the separately identified file-only metadata policy.

Fresh session argv/private-file and explicit opt-out observations are retained. Genuine conversation
resume remains unproven: the resume-only attempt found no resumable native history. Claude project
consent remained pending; Grok project HTTP reached an expired fixture endpoint. A canary that
accepted a request without its missing binding proves neither a native binding refusal nor
authenticated access. No synthetic history, replacement fresh launch or scope reinterpretation
closes those gaps.

The corrected handoff must emphasize consistent profile-selected settings before/add/withdraw/next
reinit, common file-only and MCP-bearing rehome metadata, and fresh/genuine resumed session carriers
with neighbor isolation. Retain project approval and endpoint limits explicitly. Phase-2 OAuth
research does not replace this registration acceptance.

## Independent registration round 2

The [second report](https://github.com/WayfarerLabs/agentworks/pull/888#issuecomment-6795726868)
tested exact `7accf20c` against main `07feb4a4`. All twelve required gates passed, including 12,579
CLI tests with 35 skips. The independent installed-wheel check compared all 492 non-RECORD members,
including 489 package members; five installed guide calls succeeded. These inventories remain
distinct from the lead's earlier package checks.

Linux and Windows Server 2025 workstations drove separate real remote-Lima guests. The report's
native clients were Claude 2.1.322, Codex 0.167.1 and Grok 1.0.77. Four actual reinitializations on
each workstation covered settings-only setup, MCP addition, withdrawal and the subsequent
settings-only operation. Changed settings stayed at the login-selected Codex home, with the default
file absent and neighboring sessions preserved. Malformed login exports refused safely while
preserving native bytes, ownership and recorded claims.

Two file-only and two MCP-bearing moves covered metadata relocation, literal contents, retained
copies and current Git handling. Strict HTTP canaries observed correct runtime bindings and a 401 on
missing bindings. Grok project HTTP connected under normal explicit trust while its endpoint was
live. The earlier permissive stdio logger still does not prove an authentication refusal.

Managed session carriers launched on Windows and preserved the neighbor. That proves carrier
delivery, not a completed native conversation. Strict Claude resume still found no resumable
history. A managed print-mode help invocation exited immediately and was correctly rolled back; it
does not prove a persistent managed print-mode workload. No model credentials or inference were
used. R6 session acceptance therefore remains open; Claude project consent and actual
linked-worktree native rehome also retain their reported gaps. No new product defect was
established.

The report separately records independent guest, disk, process, socket, database and profile
cleanup, unchanged standing defaults and the deallocated Windows workstation. Its exact
platform/mode and cleanup limits remain attached to those observations.

## Native-generated resume history

A controlled provider endpoint may return deterministic public replies through the native client's
ordinary Messages or Responses interface. The native client must create and persist its own
conversation ID and history, then genuinely resume it through the actual managed launcher. This
permits the existing R6 state and isolation checks without making paid-provider or model-cooperation
claims. Do not write history/rollout/DB records manually, delay an otherwise exited workload with a
wrapper, or count a fallback fresh conversation as resume.

The local protocol fixture and procedure in the
[test-harness catalogue](../../testing/harnesses/README.md) support this method. Request receipts
must be paired with actual native IDs/history observations, managed operation results, current-run
ownership and an unchanged neighbor. A provider request containing prior user/assistant context is
useful evidence of continuation; it cannot by itself establish the managed lifecycle or isolation.
Actual MCP initialization and canary discovery establish separate loading observations; record which
invocations they cover. Model cooperation and actual tool execution remain separate claims.

Isolation checks concern shared native MCP registrations and managed artifact placement. Native
history writes and observed normal trust/onboarding updates must be attributed separately. Prepare
the owned native environment and record the baseline; do not describe a whole config file as
byte-identical when legitimate native onboarding changed it. Verify unchanged MCP registration
tables and unrelated settings in addition to the session's private carrier and ownership evidence.
The procedure does not waive normal project consent or the remaining live topology checks.

## OAuth phase-2 acceptance

Exercise the public profile and explicit native workflow at the actual phase-2 head. Registration
and prior isolated native research do not substitute for these managed observations:

- Strict public-option admission from inline and selected file/Git entries, whole-entry overrides,
  OAuth/Authorization conflict and unknown/confidential-field refusal. Retained v4 interpretation
  and unchanged phase-1 content/declaration digests must survive; v5 and unknown-future captures
  need the ordinary preservation boundary.
- Actual Claude/Codex user and shared-workspace registrations, with explicit public client ID,
  requested scopes and fixed callback port where supported. Observe the native-selected redirect,
  normal consent, authenticated MCP discovery and the difference between requested/granted scopes.
  Do not add a callback URL or native option that the canonical artifact did not declare to make a
  public-profile case pass.
- Existing agent and admin access as the actual Linux user, including two users of a shared
  workspace with independent credentials. Keep interactive native login, remote manual flow,
  cancellation, missing/expired credentials, advertised refresh, status and targeted local logout
  distinct from automatic setup. Do not infer OS-user isolation from two HOME directories alone.
- Per-entry Grok handling: empty native-default intent registers the URL; unsupported explicit
  public options defer the whole entry and preserve previous claims/bytes while independent entries
  continue. Shell publishes canonical public data without starting auth or contacting the endpoint.
  Successful Grok native OAuth is a separate observation, not implied by that registration result.
- For any advertised session-only authentication path, use the existing invocation carrier and
  actual active-session/native interface without publishing a broader registration. Record actual
  native version/mode, login/discovery and credential cleanup; parser help or a rendered config is
  insufficient. Leave an unestablished combination explicit rather than counting it as a pass.
- Normal update, withdrawal, activation removal and parent deletion retain native credentials and
  foreign/modified entry protection. A native-added same-table secret remains an external edit.
  Setup/inspection must not probe auth stores or endpoints. Verify no tokens/codes/refresh material
  in captures, applied state, argv or diagnostics, and independently clean all owned users,
  credentials, callback/listener processes, native homes and test resources. Local logout alone does
  not prove provider-side grant revocation.

The phase-2 PR still receives full gates, installed-package/guide checks and all required private
reviews before its separate integration handoff. Only its actual report can close these managed
cells; overall lock also requires the outstanding phase-1 obligations.

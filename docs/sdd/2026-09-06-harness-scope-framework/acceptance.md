# Acceptance reconciliation

## Current disposition, 2026-11-10

The companion's
[current disposition](../2026-09-12-agent-artifacts/acceptance.md#current-disposition-2026-11-10)
records the complete PR 871 report at `b8c03be5`, including representative native-file lifecycle,
all required gates and the remaining rendered-argument defect. R7/R12/R16 remain transferred to that
effort, with their original attribution retained below.

The post-handoff audit also found a present R9 contradiction: known artifact exclusion cleanup can
block workspace deletion, although this FRD and permanent lifecycle guides forbid native-state
deletion prerequisites. Supported rehome can drop old-source ownership while leaving shared
exclusions. These current counterexamples prevent claiming final framework acceptance until the
bounded correction and its evidence are complete. The earlier unchanged-API, actual-user, native
settings and prerequisite proofs remain valid at their recorded heads; neither a new universal
vertical nor workspace modes are added as gates.

Round 2 owns those lifecycle corrections and permanent author-contract alignment in the companion
plan. Both locks remain pending final requirement reconciliation and accepted corrected-head
evidence.

## Retained disposition, 2026-11-09

The companion's
[retained acceptance disposition](../2026-09-12-agent-artifacts/acceptance.md#retained-disposition-2026-11-09)
records PR 871's full independent report at `1ecffc5c`: current multi-session/native-file evidence,
two material artifact defects and the exact coverage limits. R7/R12/R16 remain transferred to that
effort. Existing framework proofs below retain their tested-head attribution; they do not certify
the new correction or the rebase onto main `438c4546`. The companion's corrected-head validation and
final acceptance remain necessary before both locks. No new framework scope or workspace-mode
prerequisite is introduced.

## Retained reconciliation, 2026-11-08

Date: 2026-11-08. Product source: `eddf7501a5bacde0222471e09d5ea5060a6d2972`. This record reconciles
the retained framework requirements. The companion artifact effort has its own acceptance record; a
clean correction report alone does not close either SDD.

## Evidence and attribution

- [Current independent integration report](https://github.com/WayfarerLabs/agentworks/pull/862#issuecomment-6719171109):
  exact product source above, compared with main `c8d4dfb3`. Required gates passed, including the
  corrected full mypy scope (`agentworks/ tests/`, 928 files), 11,320 non-integration tests with 11
  skips, 160 website Python tests and 103 Node tests. Real Linux and macOS workstation lanes
  exercised provision/init, native setup, lifecycle, restart and cleanup. Two users, multiple
  workspaces and all 16 Claude/Codex user/project settings-policy cases were observed.
- [Prior scoped report](https://github.com/WayfarerLabs/agentworks/pull/862#issuecomment-6701131033):
  `7fb588cb`, with explicit five-owner, native policy, prerequisite, mutation and retention
  observations. Its runtime-ownership and diagnostic counterexamples were corrected afterward; the
  current report supplies the corresponding corrected-head observations.
- [Earlier vertical](https://github.com/WayfarerLabs/agentworks/pull/862#issuecomment-6676063798):
  the five-scope env/prerequisite measurements and native settings observations are attributed to
  `082437fe`; other lanes also used `b14302bc` as the report discloses. Owner/env mapping and
  prerequisite severity remain continuous. Later dispatch/state behavior uses current tests and the
  current report rather than treating this mixed-head report as a complete current verdict.
- [Simultaneous integration observation](https://github.com/WayfarerLabs/agentworks/pull/862#issuecomment-6652928141):
  Claude, Codex and Shell VM facets on `1e0d2fe0`. Combined with current map/config tests and
  distinct native user/project settings observations, this establishes separately configured
  integrations without requiring publication of every fixture value.
- [Exact ready handoff](https://github.com/WayfarerLabs/agentworks/pull/862#issuecomment-6701862296)
  and
  [final critical reading](https://github.com/WayfarerLabs/agentworks/pull/862#issuecomment-6724351948)
  preserve review, gate, authority and source attribution. The final independent report found no new
  blocker in the bounded correction.

Current setup API, schema, owner mapping, readiness and native mapping behavior are unchanged from
the prior scoped report. The later session runtime and redaction changes rely on current evidence.
Named local tests below were inspected for their behavioral assertions; their execution is covered
by the current exact-head gate. No test count alone establishes a requirement.

## R1-R16

| Requirement                             | Acceptance basis                                                                                                                                                                                                                                                                                                                                                                                          |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1: fixed pipeline and owning lifecycle | Core-before-integration ordering in VM/agent/workspace realization and session preparation; `test_native_setup_lifecycle` and `test_native_setup_continuation` distinguish collected facet failures from cancellation/shared failures. Retained five-scope measurements and current init/lifecycle observations corroborate actual execution.                                                             |
| R2: scoped integration API              | Shared user operation for admin and agent, separate VM/workspace operations, actual-owner contexts and bindings. Activation/dispatch tests and reported actual-user observations establish the five-scope to four-facet mapping.                                                                                                                                                                          |
| R3: unsupported activation              | Binding preflight rejects unsupported activated facets before secrets or core mutations; inactive/default/retirement cases are distinct. Actual CLI refusal and session-only cases were observed. Shell now implements setup in the companion, so it is not used as an unsupported fixture.                                                                                                               |
| R4: per-facet config                    | `test_facet_config` exercises selected schema validation, registration/cache replacement, introspection and ordinary single-config compatibility. This is a local schema contract, not a missing VM test.                                                                                                                                                                                                 |
| R5: explicit activation maps            | `test_harness_activations` covers merge, null disable, fresh reactivation and singular effective session selection. Real simultaneous integrations and distinct native user/project configurations corroborate representative explicit use; exhaustive null variants are established locally.                                                                                                             |
| R6: core environment                    | Core composition supplies actual-owner ancestry and protected identity. Retained literal/secret values were observed inside all facets and live processes; current env delivery and guest diagnostic redaction were observed. Applied state does not persist resolved env values.                                                                                                                         |
| R7: artifact propagation                | Explicitly transferred to the companion artifact SDD. No duplicate framework deferral gate is retained.                                                                                                                                                                                                                                                                                                   |
| R8: invocation ownership                | Actual admin/agent/workspace contexts and fresh session bindings remain separate. Binding/readiness tests prevent another user's setup from satisfying a consumer; current two-user physical ownership evidence corroborates the boundary.                                                                                                                                                                |
| R9: typed applied state and convergence | State codec 3 reads prior supported versions and conservatively retains future data. Native adoption, conflicting-content preservation, removal, failed cleanup and recoverable partial progress have meaningful local controls and scoped live observations. Production Database backup/mutate/restore is tested locally and observed in the current run; no new VM restore workflow is required.        |
| R10: required/recommended prerequisites | Consumer-owned severity and actual-user binding are tested before secrets/runtime replacement. The retained real matrix covers absent/current/stale/incomplete/pending state, new/existing owners and no implicit repair; current routing/refusal observations corroborate it. A shipped integration need not declare a prerequisite it does not need.                                                    |
| R11: remove core Claude setup           | Current core has no Claude setup fields or dispatch. Remaining legacy spellings are finite conversion/diagnostic boundaries, exercised by `test_legacy_claude`. Real modern native setup and ordinary explicit Shell sessions establish the destination behavior.                                                                                                                                         |
| R12: artifact definitions and formats   | Explicitly transferred to the companion. Future hooks/MCP formats are not revived as framework requirements.                                                                                                                                                                                                                                                                                              |
| R13: representative real vertical       | Actual CLI create/reinit, five owning scopes, env, explicit integrations, native local plugin/settings fixtures, prerequisites and safe convergence are supported by the retained scoped vertical and current native lifecycle/policy observations. Current independent cleanup was checked at provider, guest and controller layers. No all-provider/version Cartesian matrix or model call is required. |
| R14: native user/workspace setup        | Separate user/project settings and user-scoped marketplace/plugin operations have current native observations with two users and multiple workspaces. Workspace plugin installation remains explicitly unsupported where its real ownership would be user-scoped.                                                                                                                                         |
| R15: workstation settings mapping       | Source validation and snapshotting, all four policies, explicit mapped authority and omitted-association preservation are tested. Current native JSON/TOML policy results, ownership, permissions and retained operator changes were observed. The former contradictory LLD wording is corrected.                                                                                                         |
| R16: Shell artifact files               | Explicitly transferred to the companion. Ordinary Shell compatibility remains established under R3/R11/R13.                                                                                                                                                                                                                                                                                               |

The functional definition of done is satisfied by these combined local and scoped real observations:
own integration APIs/config, correct env at all five owners, independent selection and
prerequisites, native setup/settings at their actual scopes, and safe owning convergence. The
exclusions above remain exclusions rather than unchecked duplicate implementation obligations.

## Permanent promotion

Load-bearing behavior is available without this spec directory:

| Knowledge                                                                                                     | Permanent home                                                                               |
| ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Scope/facet mapping, activation maps, env ancestry, prerequisite severity, independent-facet failure policy   | `docs/guides/harness-facets.md`; `cli/agentworks/capabilities/harness_integration/README.md` |
| Per-facet config/introspection and capability vocabulary                                                      | `cli/agentworks/capabilities/README.md`                                                      |
| Native association identity, adoption, four settings policies, mapped authority, safe cleanup and limitations | `docs/guides/native-harness-setup.md`; `docs/guides/idempotency.md`                          |
| Finite legacy Claude/stored-list migration and explicit current configuration                                 | `docs/guides/migrating-claude-setup.md`                                                      |
| State ownership, backup/rehome/delete boundaries                                                              | `cli/agentworks/db/README.md`; native setup and idempotency guides                           |
| Repeatable prerequisite/unsupported integration fixtures                                                      | `docs/testing/harnesses/README.md` and its maintained launcher                               |

## Limits retained at closeout

The current report's Windows host-to-guest SSH lane was blocked by Tailscale policy. Windows guest
init/session/artifact/restart and Windows 11-specific behavior were not established by that run;
Server 2025 test selections are narrower evidence. The representative Linux/macOS framework vertical
does not become a universal workstation claim.

The inherited same-HOME SQLite snapshot-upgrade failure is tracked in
[issue 850](https://github.com/WayfarerLabs/agentworks/issues/850); serialized write bursts are the
current operational limit. Conservative aggregate reporting and generic foreign-socket hints do not
establish unsafe adoption or retirement. The inherited initial tmux server-creation race remains
documented in `docs/guides/session-status.md`: existing session/run metadata does not establish
atomic ownership of a concurrently created whole server. No provenance journal, native startup
protocol, workspace reinit, Transport/SSH redesign or provider redesign is implied by this closeout.

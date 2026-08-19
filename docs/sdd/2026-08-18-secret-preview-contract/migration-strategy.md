# Migration strategy: atomic secret-backend contract rewrite

- Snapshot: 2026-08-18
- Amended: 2026-08-19
- Code baseline: `origin/main` at `667793ee`
- Governing design: [FRD](./frd.md) and [HLA](./hla.md)

## Executive summary

The migration removes one static classification and replaces it with per-operation intent. It is
additive-first inside the feature branch but atomic at merge: all in-tree backends, core preview and
resolution, caller policy plumbing, conformance tests, and permanent documentation move together.

There are no external secret-backend plugins. The operator therefore ruled that the contract and all
implementations change atomically, with no adapter or deprecation track. The secret-backend
descriptor and every implementation reset their registration sentinel from `2` to `1` in that same
change. No persisted-data migration is required because the sentinel is registration-only.

The rewritten result model is also atomic. It does not map every negative condition to one flag.
Ordinary missing data falls through, execution blocks fall through with evidence, and mapping or
provider failure hard-stops the configured chain.

## Current-state inventory

At the refreshed baseline (runtime secret semantics are unchanged from the original `c01263d0`
research snapshot):

| Surface                 | Current state                                                                   |
| ----------------------- | ------------------------------------------------------------------------------- |
| Backend contract        | one exact in-tree shape, with no external implementations                       |
| Backend implementations | 3: core env-var, core prompt, system-plugin OnePassword                         |
| Contract sentinel       | exact integer `2`, used only by registration conformance                        |
| Static classification   | required `interactive: bool` on every backend                                   |
| Static applicability    | required pure `would_attempt` method                                            |
| Runtime client          | `prepare` plus value-bearing `resolve`; no preview method                       |
| Preview                 | pure core projection with `attemptable`, `refused-interaction`, `unavailable`   |
| Preflight               | fails every category except `attemptable`                                       |
| Ordinary CLI policy     | `ALLOW` only when stdin is a TTY and global mode permits it                     |
| Policy reach            | `InteractionPolicy` appears in 39 production files and 68 test files            |
| Preview callers         | preflight, secret describe, and doctor through 4 production modules             |
| Backend conformance     | exact class attributes and method call shapes checked at registration           |
| Failure boundary        | closed kinds plus a redundant backend-selected remediation enum                 |
| Public collateral       | backend-authoring README, secrets README, CLI docs, guides, schema, completions |

These counts are migration guards, not line estimates. Recount after rebase before implementation.

## Target contract

The atomic rewrite makes these changes:

| Before                                | After                                                           |
| ------------------------------------- | --------------------------------------------------------------- |
| `interactive: bool`                   | removed; method receives exact `OperatorImpact`                 |
| `would_attempt(...) -> bool`          | removed                                                         |
| `describe_lookup(...) -> str \| None` | structured static `LookupDescription`                           |
| no client preview                     | batch `preview(...) -> Mapping[str, BackendPreview]` tagged sum |
| no-op, impact-blind `prepare`         | removed                                                         |
| factory lacks intent                  | factory receives exact impact and terminal fact                 |
| value-map `resolve`                   | resolved, missing, blocked, or failed tagged sum                |
| one negative/failure channel          | missing fallthrough, blocked fallthrough, failed hard-stop      |
| backend-selected remediation          | core derives guidance from closed tag and reason                |

The descriptor's required operations and attributes, registration diagnostics, author example, and
conformance tests update in the same commit as the base contract. The descriptor and all three
implementations declare `contract_version = 1`; secret-backend version `2` has no compatibility
branch. Versions for vm-platform, git-credential-provider, and harness-integration are unaffected.

## Why an old-shape adapter is rejected

An unmodified old-shape backend exposes only two relevant facts: a static `interactive` boolean and
a value-returning `resolve`. Neither can satisfy the rewritten contract:

- the static flag cannot answer whether this invocation will require operator action;
- calling `resolve` for preview returns plaintext to the adapter's core-side caller;
- returning indeterminate for every old-shape backend violates the maximum-impact guarantee;
- returning synthetic missing conflates absence with provider failure and can silently select a
  lower-precedence source;
- old exceptions do not supply the exact missing, blocked, and failed sum required by core.

Locating an adapter in a capability package does not change the authority boundary. If old backend
code returned plaintext through its prior client contract, preview containment was not achieved.

## Additive-first sequence inside the PR

1. Add closed impact, terminal, lookup-description, preview result, actual-resolution result, and
   reason types plus exact validators. Do not repoint callers yet.
2. Add private acquisition and normalization helpers to all three in-tree backends while their
   current methods still drive runtime. Pin valid absence versus invalid mapping at this seam.
3. Add final preview and resolution methods to the backends behind focused tests, including local
   value discard and missing/blocked/failed classification.
4. Switch the descriptor, base class, both source drivers, conformance checks, exports, and every
   implementation to the rewritten contract atomically. Reset the exact sentinel from `2` to `1`,
   remove `prepare`, and deliver intent before client construction and context entry.
5. Repoint static inspection from `would_attempt` to structured lookup descriptions.
6. Replace pure preview with bounded source-client preview, precedence-aware tagged aggregation, and
   a lazy per-command preflight memo. Represent zero runtime candidates as aggregate
   `blocked/no-candidate`, not as a lookup miss.
7. Repoint preflight, describe, verify, and doctor to their fixed preview semantics.
8. Replace `InteractionPolicy` with `OperatorImpact` across all published operation boundaries and
   move ordinary CLI derivation off TTY state.
9. Remove residual static interactive skips, wire exact terminal facts and broker construction, and
   preserve fail-before-mutation through iterative no-impact closure, before-every-`ALLOW` viability
   checks, and backend-owned semantic results.
10. Delete old-shape types, compatibility scaffolding, stale comments, and redundant backend
    remediation selection.
11. Update permanent docs, schema, samples, completions, and machine-output references, then run the
    full validation and live-test matrix.

Intermediate commits are review checkpoints on a draft branch. None is a separately supported
repository state, and the branch does not become ready while both contract shapes or both policy
types remain.

## Representative migration

### Backend before

```python
class OnePasswordBackend(SecretBackend):
    contract_version = 2
    interactive = True

    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        return mapping_present
```

Core refuses this source whenever the CLI has no TTY, before the backend can distinguish desktop app
authentication from a service account.

### Backend after

```python
class OnePasswordBackend(SecretBackend):
    contract_version = 1

    @classmethod
    def describe_lookup(cls, secret_name, mapping) -> LookupDescription:
        ...

    @classmethod
    def create_client(
        cls,
        *,
        source_name,
        config,
        impact,
        terminal,
        interaction_broker,
        remaining_time,
    ) -> AbstractContextManager[SecretSourceClient]:
        ...


class OnePasswordClient:
    def preview(self, requests, *, impact, terminal, remaining_time) -> Mapping[str, BackendPreview]:
        ...

    def resolve(self, requests, *, impact, terminal, remaining_time) -> Mapping[str, BackendResolution]:
        ...
```

The client decides whether the next `op read` fits `impact`. When allowed, preview reads and
discards inside the client. Actual resolution returns an exact map whose entries are redacted
`BackendResolved`, `BackendMissing`, `BackendBlocked`, or `BackendFailed` variants. TTY remains an
execution fact but does not gate either OnePassword method.

An invalid provider reference returns `PreviewFailed(INVALID_MAPPING)` or
`BackendFailed(INVALID_MAPPING)` and hard-stops in core. A provider outcome returns missing only
when its normalizer can establish ordinary absence. Because `op read` has a flat failure exit and no
documented stable error taxonomy, ambiguous item/field not-found text remains failed/invalid-mapping
unless sanitized evidence for the supported version proves a narrower absence marker. The backend
does not return a halt boolean; the variant determines fixed core flow.

## Config and schema migration

OnePassword source config gains one optional field with a conservative default:

```yaml
spec:
  backend:
    name: onepassword
    app_authentication_impact: operator-action
```

Existing source manifests remain valid and retain conservative behavior under `--non-interactive` or
non-disruptive preview. Ordinary commands without that global flag now allow the app flow from a
non-TTY process. Operators who regard app authentication as acceptable under no-impact preview can
set the field to `none`.

The generated union schema, sample config, source docs, and editor completions add the field in the
same implementation commit. No persisted state or database migration is required.

## CLI and JSON transition

- Ordinary commands change behavior without adding local flags: no TTY no longer implies no operator
  impact; global `--non-interactive` remains the control.
- `secret describe` adds `--allow-interaction`.
- `secret verify` retains its existing flag but switches from core resolution to backend preview.
- `secret describe --output json` preserves the existing version-1 `category`, `source`,
  `identifier`, `skipped_not_ready`, and `source_mappings[].would_attempt` meanings. It adds one
  optional nested preview object containing tagged `status`, conditional `reason`, and ordered
  attempts. The command reference documents legacy compatibility and new null or absence rules
  before the PR is ready.

## Rollback

There is no data migration, so rollback before release is a normal code revert. Config using the new
optional OnePassword field will fail validation on older code; the upgrade guide must call that out.
There is no external secret-backend plugin downgrade path to preserve. Future external publication
starts from the rewritten contract.

## Risks and safeguards

| Risk                                                         | Safeguard                                                                        |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| Preview leaks a fetched value                                | value-free type, backend-local discard, sentinel scans, redacted representations |
| Preview and resolve classify provider outcomes differently   | one private acquisition and normalization seam per backend, parity tests         |
| Backend returns indeterminate too early                      | behavioral tests prove permitted probes run; maximum-impact runtime rejection    |
| `ALLOW` resolution returns an impact block and repeats work  | exact impact/result validation converts the whole turn to protocol failure       |
| Maximum impact is mistaken for guaranteed provider success   | explicit blocked/failed variants at every impact, caller tests                   |
| Ordinary absence is conflated with invalid mapping or outage | separate missing and failed variants plus provider-normalization fixtures        |
| No candidate is mislabeled as a checked miss                 | aggregate blocked/no-candidate with empty runtime attempts                       |
| A failed higher source is hidden by fallback                 | core-owned failed hard-stop and precedence tests                                 |
| Missing TTY is mistaken for absence or failure               | dedicated blocked reason and cross-product tests                                 |
| Later source hides earlier uncertainty                       | precedence-aware aggregation with earlier indeterminate                          |
| Preflight starts disruptive work                             | fixed zero-impact intent and no certainty override                               |
| Repeated references repeat provider reads during preflight   | lazy command-scoped memo with node-order tests                                   |
| Client setup runs before learning authority                  | pass exact intent into factory; entry/provider call observation tests            |
| Removing static flags weakens complete-batch doom            | no-impact closure and viability check before every `ALLOW` source turn           |
| Ordinary non-TTY command hangs on prompt                     | terminal fact checked before broker/read, no broker without TTY                  |
| Ordinary non-TTY command wrongly blocks app approval         | impact derived from global mode, not TTY                                         |
| Source config promises too much biometric detection          | setting names app authentication as a whole and docs explain provider opacity    |
| Old-shape compatibility weakens the security boundary        | atomic rewrite of all in-tree implementations; no adapter                        |
| Plugin text reaches diagnostics                              | closed tags/reasons only; native text discarded inside boundary                  |

## Completion conditions

- No production or test reference to `InteractionPolicy`, backend `interactive`, client `prepare`,
  or `would_attempt` remains except migration documentation and the derived JSON v1 compatibility
  key.
- No legacy preview answer/detail pair or generic blocked-result shape remains.
- The secret-backend descriptor and every registered backend declare `contract_version = 1`; no
  production secret-backend declaration retains `2`.
- Missing, blocked, and failed have distinct tests, flow, operator output, and machine output.
- The secret-backend and general plugin-authoring READMEs contain the rewritten contract and a
  complete author example. The secret-backend README is self-contained and specifies exact variants,
  reason ownership, flow, impact and terminal rules, lifecycle constraints, value containment, and
  conformance requirements without relying on this SDD.
- Schema, completions, guide, and command-reference changes match observable behavior.
- Full repository gates and the approved live-test charter pass before the PR becomes ready.

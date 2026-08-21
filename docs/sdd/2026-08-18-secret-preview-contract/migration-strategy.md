# Migration strategy: atomic secret-backend contract rewrite

- Snapshot: 2026-08-18
- Amended: 2026-08-21
- Code baseline: `origin/main` at `202036a6`
- Governing design: [FRD](./frd.md) and [HLA](./hla.md)

## Executive summary

The migration removes the static interaction-impact classification and replaces it with
per-operation intent plus a narrower static TTY-broker capability. It is additive-first inside the
feature branch but atomic at merge: all in-tree backends, core preview and resolution, caller policy
plumbing, conformance tests, and permanent documentation move together.

There are no external secret-backend plugins. The operator therefore ruled that the contract and all
implementations change atomically, with no adapter or deprecation track. The secret-backend
descriptor and every implementation reset their registration sentinel from `2` to `1` in that same
change. No persisted-data migration is required because the sentinel is registration-only.

The rewritten result model is also atomic. It does not map every negative condition to one flag.
Ordinary missing data falls through, execution blocks fall through with evidence, and mapping or
provider failure hard-stops the configured chain.

## Current-state inventory

At the refreshed baseline (runtime secret semantics are unchanged from the original `c01263d0`
research snapshot; intervening process, guide, and website changes do not alter this inventory):

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
| Ordinary CLI policy     | broad `InteractionPolicy` conflates TTY permission with wider interaction       |
| Policy reach            | `InteractionPolicy` appears in 39 production files and 68 test files            |
| Preview callers         | preflight, secret describe, and doctor through 4 production modules             |
| Backend conformance     | exact class attributes and method call shapes checked at registration           |
| Failure boundary        | closed kinds plus a redundant backend-selected remediation enum                 |
| Public collateral       | backend-authoring README, secrets README, CLI docs, guides, schema, completions |

These counts are migration guards, not line estimates. Recount after rebase before implementation.

## Target contract

The atomic rewrite makes these changes:

| Before                                | After                                                                  |
| ------------------------------------- | ---------------------------------------------------------------------- |
| `interactive: bool`                   | exact `supports_tty_interaction: bool`; no impact meaning              |
| `would_attempt(...) -> bool`          | removed                                                                |
| `describe_lookup(...) -> str \| None` | structured static `LookupDescription`                                  |
| no client preview                     | batch `preview(requests) -> Mapping[str, BackendPreview]` tagged sum   |
| no-op, impact-blind `prepare`         | removed                                                                |
| pre-client timeout hook               | removed; client enforces validated config and remaining budget         |
| factory lacks intent                  | factory receives tagged preview/resolution intent and exact TTY access |
| value-map `resolve`                   | resolved, missing, blocked, or failed tagged sum                       |
| broad actual-resolution policy        | no impact input; only TTY access can constrain prompt                  |
| one negative/failure channel          | missing fallthrough, blocked fallthrough, failed hard-stop             |
| backend-selected remediation          | core derives guidance from closed tag and reason                       |

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

1. Add closed preview impact, TTY policy/access, tagged client intent, exact TTY broker capability,
   lookup-description, preview result, actual-resolution result, and reason types plus exact
   validators. Do not repoint callers yet.
2. Add private acquisition and normalization helpers to all three in-tree backends while their
   current methods still drive runtime. Pin valid absence versus invalid mapping at this seam.
3. Add final preview and resolution methods to the backends behind focused tests, including local
   value discard and missing/blocked/failed classification.
4. Switch the descriptor, base class, both source drivers, conformance checks, exports, and every
   implementation to the rewritten contract atomically. Reset the exact sentinel from `2` to `1`,
   remove `prepare` and `external_operation_timeout`, and deliver intent before client construction
   and context entry.
5. Repoint static inspection from `would_attempt` to structured lookup descriptions.
6. Replace pure preview with bounded source-client preview, precedence-aware tagged aggregation, and
   a lazy per-command preflight memo. Represent zero runtime candidates as aggregate
   `blocked/no-candidate`, not as a lookup miss.
7. Repoint preflight, describe, verify, and doctor to their fixed preview semantics.
8. Rename broad `InteractionPolicy` to exact `TtyInteractionPolicy` across published operation
   boundaries. Derive it only from global `--non-interactive`, combine it with physical TTY state,
   and keep preview impact separate.
9. Remove residual static interactive skips, wire tagged operation intent and exact TTY access into
   client construction, and preserve fail-before-mutation through one bounded source-first actual
   resolution pass.
10. Delete old-shape types, compatibility scaffolding, stale comments, backend timeout/failure/
    remediation exceptions, and the redundant runtime resolution-detail vocabulary.
11. Update permanent docs, schema, samples, completions, and the command reference's machine-output
    contract, then run the full validation and live-test matrix.

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
    supports_tty_interaction = False

    @classmethod
    def describe_lookup(cls, secret_name, mapping) -> LookupDescription:
        ...

    @classmethod
    def create_client(
        cls,
        *,
        source_name,
        config,
        intent,
        tty_access,
        interaction_broker,
        remaining_time,
    ) -> AbstractContextManager[SecretSourceClient]:
        ...


class OnePasswordClient:
    def preview(self, requests) -> Mapping[str, BackendPreview]:
        ...

    def resolve(self, requests) -> Mapping[str, BackendResolution]:
        ...
```

For preview intent, the client decides whether the next `op read` fits the requested impact. When
allowed, preview reads and discards inside the client. For resolution intent, OnePassword always
attempts the bounded read: actual resolution has no operator-impact input. It returns an exact map
whose entries are redacted `BackendResolved`, `BackendMissing`, `BackendBlocked`, or `BackendFailed`
variants. TTY access does not gate either OnePassword method.

An invalid local provider-reference shape returns `PreviewFailed(INVALID_MAPPING)` or
`BackendFailed(INVALID_MAPPING)` and hard-stops in core. A provider outcome returns missing only
when its normalizer can establish ordinary absence. Because `op read` has a flat failure exit and no
documented stable error taxonomy, ambiguous item/field not-found text remains failed/lookup-rejected
unless sanitized evidence for the supported version proves a narrower absence marker. The backend
does not return a halt boolean; the variant determines fixed core flow.

Ordinary OnePassword absence has two implementation-time dispositions. If an authorized real run
against the exact supported `op` version supplies a conclusive, sanitized narrow token, record that
version and token, add its regression fixture, and reproduce missing fallthrough live. If it does
not, ship no OnePassword missing token: item/field markers remain lookup-rejected and unknown text
remains external failure. Fake provider text never substitutes for provider evidence; env-var and
controlled contract fixtures prove generic missing fallthrough either way.

## Config and schema migration

OnePassword source config gains one optional field with a conservative default:

```yaml
spec:
  backend:
    name: onepassword
    app_authentication_impact: operator-action
```

Existing source manifests remain valid and retain conservative behavior for non-disruptive preview.
Actual resolution always permits the app flow, including from a non-TTY process and under global
`--non-interactive`. Operators who regard app authentication as acceptable under no-impact preview
can set the field to `none`.

The generated union schema, sample config, source docs, and editor completions add the field in the
same implementation commit. No persisted state or database migration is required.

## CLI and JSON transition

- Ordinary commands change behavior without adding local flags: OnePassword and other out-of-band
  providers run regardless of TTY state. Global `--non-interactive` means only "do not use the TTY
  for interactions, even if one is present."
- `secret describe` adds `--allow-interaction`.
- `secret verify` retains its existing flag but switches from core resolution to backend preview.
- Preview `--allow-interaction` and global `--non-interactive` are valid together: the former allows
  out-of-band impact, while the latter still disables prompt.
- `secret describe --output json` preserves the existing version-1 `category`, `source`,
  `identifier`, `skipped_not_ready`, and `source_mappings[].would_attempt` meanings. It adds one
  optional nested preview object containing tagged `status`, conditional `reason`, and ordered
  attempts. The command reference documents legacy compatibility and new null or absence rules
  before the PR is ready.
- `secret list --output json` preserves its existing version-1 `secrets[].sources[].would_attempt`
  field, deriving it from structured lookup disposition.
- Doctor preserves every existing JSON v1 check field and adds optional `secret_preview` only for
  secret checks, using the same closed status, reason, identity, and attempt rules as describe.

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
| Maximum impact is mistaken for guaranteed provider success   | explicit blocked/failed variants at every impact, caller tests                   |
| Ordinary absence is conflated with invalid mapping or outage | separate missing and failed variants plus provider-normalization fixtures        |
| No candidate is mislabeled as a checked miss                 | aggregate blocked/no-candidate with empty runtime attempts                       |
| A failed higher source is hidden by fallback                 | core-owned failed hard-stop and precedence tests                                 |
| Missing TTY is mistaken for absence or failure               | dedicated blocked reason and cross-product tests                                 |
| Earlier uncertainty masks current success or failure         | current-impact aggregate plus retained ordered attempt evidence                  |
| Preflight starts disruptive work                             | fixed zero-impact intent and no certainty override                               |
| Repeated references repeat provider reads during preflight   | lazy command-scoped memo with node-order tests                                   |
| Client setup runs before learning authority                  | pass exact intent into factory; remove every pre-client backend hook             |
| Ordinary non-TTY command hangs on prompt                     | exact TTY access checked before broker/read; no broker without TTY               |
| Global mode wrongly blocks app or biometric approval         | TTY capability plus flag/TTY/backend cross-product tests                         |
| Global mode still changes output color or presentation       | presentation decoupling plus TTY-output cross-product tests                      |
| Source config promises too much biometric detection          | setting names app authentication as a whole and docs explain provider opacity    |
| Old-shape compatibility weakens the security boundary        | atomic rewrite of all in-tree implementations; no adapter                        |
| Plugin text reaches diagnostics                              | closed tags/reasons only; native text discarded inside boundary                  |

## Completion conditions

- No production or test reference to broad `InteractionPolicy`, backend `interactive`, client
  `prepare`, `external_operation_timeout`, backend timeout/failure/remediation exceptions, or
  runtime `would_attempt` remains except migration documentation and the two derived JSON v1
  compatibility keys.
- No legacy preview answer/detail pair or generic blocked-result shape remains.
- No legacy policy-free or operation-policy pure-preview helper remains; provider-aware preview has
  one impact-explicit service boundary, actual resolution has none, and static lookup description
  remains separate.
- The secret-backend descriptor and every registered backend declare `contract_version = 1`; no
  production secret-backend declaration retains `2`.
- Missing, blocked, and failed have distinct tests, flow, operator output, and machine output.
- Env-var and OnePassword declare no TTY support, receive no broker, and cannot return TTY blocks;
  prompt declares support and receives a broker only with available access and permitted preview
  impact.
- The secret-backend and general plugin-authoring READMEs contain the rewritten contract and a
  complete author example. The secret-backend README is self-contained and specifies exact variants,
  reason ownership, flow, preview impact, TTY broker capability and access rules, lifecycle
  constraints, value containment, and conformance requirements without relying on this SDD.
- Schema, completions, guide, and command-reference changes match observable behavior.
- ADR 0013 and the secrets guide consent paragraph teach the configured-source path and every
  impact-bearing inspection control without depending on this SDD.
- The secret-backend and secrets test estates satisfy the simplification sweep's trim standard in
  this atomic rewrite; worthless tests are removed rather than assigned to follow-up.
- Full repository gates and the approved live-test charter pass before the PR becomes ready.

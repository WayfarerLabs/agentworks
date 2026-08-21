# HLA: Intent-aware secret preview

- Status: Draft for review
- Date: 2026-08-18
- Amended: 2026-08-21
- Governing requirements: [FRD](./frd.md)
- Detailed contracts: [preview contract LLD](./preview-contract-lld.md) and
  [operator surfaces LLD](./operator-surfaces-lld.md)

## Executive summary

The architecture replaces the static backend `interactive` classification with two orthogonal,
narrow concepts. Preview receives an explicit operator-impact allowance. TTY interaction access
controls only whether a backend may use terminal input. Preview becomes a real source-client
operation with a closed, value-free result sum, and the backend returns the most informative result
available within those constraints.

The result separates five semantic states: `available`, ordinary `missing`, impact-limited
`indeterminate`, execution `blocked`, and operational `failed`. Backends classify only those facts.
Core centrally owns fallthrough and hard-stop behavior, so a missing value can use a lower source
but an invalid or failing configured source cannot be silently hidden by fallback.

Actual resolution is a separate value-bearing operation with no operator-impact allowance. It may
trigger out-of-band provider action regardless of TTY state or global `--non-interactive`. That flag
means only "do not use the TTY for interactions, even if one is present." Preflight does not
simulate full resolution: it previews at zero impact, then the operation performs one authoritative
source-first resolution before external mutation.

## Architectural shape

```text
caller semantics
    |
    | operator-impact allowance + execution facts
    v
secret preview orchestrator
    |
    | validated candidate requests, active-source order, bounded lifetime
    v
source-bound backend client
    |
    | optional provider read, value stays here and is discarded
    v
closed backend preview
    | available | missing | indeterminate | blocked | failed
    v
core chain disposition + caller-owned rendering or decision
```

The actual resolution path is parallel, not downstream of preview. It receives values through the
private batch and has no preview-impact input. No caller uses preview as a value cache or extracts a
value from its result.

## Components and responsibilities

### Preview operator-impact policy

`OperatorImpact` is the exact preview intent type. Its initial levels are:

- `NONE`: the backend may not knowingly cause an operator action.
- `ALLOW`: the maximum level; operator actions are permitted.

This is deliberately a two-level contract. New levels require an observed need and a contract
revision. Certainty is not a policy member. TTY availability is not a policy member.

Every preview boundary checks exact enum identity before work. The caller selects `NONE` for
non-disruptive preview or `ALLOW` for explicit inspection. Global `--non-interactive` does not alter
this value.

### TTY interaction access

The existing broad `InteractionPolicy` becomes the exact `TtyInteractionPolicy`: `ALLOW` or
`REFUSE`. Global `--non-interactive` selects `REFUSE` and means exactly "do not use the TTY for
interactions, even if one is present." It does not describe whether the operator may need to approve
a provider action elsewhere and does not control output presentation.

Core combines that policy with physical terminal availability into one `TtyInteractionAccess` state
supplied to backend clients:

- `AVAILABLE`: TTY use is allowed and usable terminal input exists;
- `UNAVAILABLE`: TTY use is allowed but no usable terminal input exists;
- `DISABLED`: global `--non-interactive` forbids TTY use, whether or not a TTY exists.

Each backend declares exact `supports_tty_interaction: bool`. This is a least-authority capability,
not a prediction: `True` means the backend may receive a broker, not that the operation will prompt.
Core supplies a broker only when the capability is true, access is `AVAILABLE`, and the selected
operation permits a prompt: actual resolution always does, while preview does only at
`OperatorImpact.ALLOW`. A backend declaring `False` cannot return a TTY block. Env-var and
OnePassword declare `False`; prompt declares `True`.

This produces truthful combinations:

| Preview impact | TTY access  | OnePassword app path                                          | Prompt path                             |
| -------------- | ----------- | ------------------------------------------------------------- | --------------------------------------- |
| none           | unavailable | probe only when backend knows/configures it as non-disruptive | `blocked/tty-unavailable`               |
| none           | disabled    | same backend decision                                         | `blocked/tty-interaction-disabled`      |
| none           | available   | same backend decision                                         | `indeterminate/operator-impact-limited` |
| allow          | unavailable | bounded read is allowed                                       | `blocked/tty-unavailable`               |
| allow          | disabled    | bounded read is allowed                                       | `blocked/tty-interaction-disabled`      |
| allow          | available   | bounded read is allowed                                       | `available`, `missing`, or `failed`     |

`ALLOW` removes impact-limited uncertainty; it does not manufacture terminal capability or provider
success.

### Static lookup description

Inspection still needs a no-I/O description of how a source maps a secret. The rewritten
`describe_lookup` operation returns a structured lookup disposition and safe identifier. It says
only whether the backend has a candidate lookup from the declaration. It does not predict runtime
presence and is not a second preview method.

This structured projection replaces `would_attempt` and keeps `secret list` cheap. Static
non-candidates remain visible in mapping output but do not become backend preview attempts.
`secret list` continues to describe mappings and readiness; it does not open provider clients.

Actual resolution is simpler: OnePassword runs in all three TTY states. Prompt resolves only when
TTY access is available and otherwise returns the matching blocked reason.

### Source-client results

The operation-bounded `SecretSourceClient` gains one batch `preview` method. Core constructs a fresh
client with a tagged intent: `PreviewIntent(impact)` or `ResolutionIntent()`. It also supplies exact
TTY interaction access and an optional prompt broker before any backend lifecycle code runs. Source
identity remains core-owned, and backend config owns any real provider timeout; the factory carries
no unused generic deadline or source-identity input. `preview` and `resolve` receive only candidate
requests. Each method returns exactly one closed tagged result per request and no other payload:

- `PreviewAvailable`: a valid value exists; stop successfully;
- `PreviewMissing`: a valid lookup established ordinary absence; fall through silently;
- `PreviewIndeterminate`: broader operator-impact authority could change the answer; preview falls
  through but preserves precedence uncertainty;
- `PreviewBlocked`: a backend reports a TTY execution limitation; core constructs blocked attempts
  for source readiness or disabled-plugin state. Each falls through and retains the reason. Core
  uses `blocked/no-candidate` for aggregate exhaustion where no lookup ran;
- `PreviewFailed`: mapping, provider, deadline, value, protocol, or unexpected failure; hard-stop.

Actual resolution returns a parallel sum:

- `BackendResolved(value)`: the only value-bearing result, with redacted representation;
- `BackendMissing`: ordinary fallthrough;
- `BackendBlocked`: TTY execution fallthrough;
- `BackendFailed`: hard-stop.

Actual resolution has neither `indeterminate` nor `operator-impact-limited`: it has no impact
allowance to exhaust. Result types carry closed reasons only where a reason changes diagnostics.
They contain no remediation, provider message, arbitrary metadata, or backend-selected flow
instruction.

### Lifecycle and provider boundary

Core passes exact tagged operation intent and TTY access into `create_client` before backend factory
or context-entry code can run. Construction remains resource-free, context entry is
provider-I/O-free, and the selected method owns provider work. The old `prepare` and
`external_operation_timeout` hooks are removed: every in-tree `prepare` is a no-op, and the provider
client enforces its validated source timeout directly. This leaves no pre-intent setup phase, no
speculative outer-budget contract, and one typed result surface per operation.

Backends share their private acquisition path between preview and resolve:

- preview acquires and validates enough to classify the result, constructs a value-free tag, and
  discards any value before returning;
- resolution acquires through the same provider logic and returns one exact resolution variant per
  request through the private resolution batch;
- native text is classified inside the backend for both paths.

At `OperatorImpact.ALLOW`, runtime contract enforcement rejects `PreviewIndeterminate` as a backend
protocol failure. It still accepts `blocked` and `failed`, because maximum preview authority cannot
guarantee TTY access, a network response, authentication success, or valid mapping. Actual
resolution rejects every preview-only reason, including `operator-impact-limited`. Either method
rejects a TTY block from a backend whose `supports_tty_interaction` is `False`.

### Core chain disposition

Core, not each backend, owns one exhaustive flow table. The preview-contract LLD is its sole
normative definition; other artifacts summarize and link to it. Core walks ready, active, applicable
sources in precedence order:

- `available` or `resolved` stops successfully;
- `missing` falls through silently;
- preview `indeterminate` falls through and remains in ordered evidence;
- `blocked` falls through and is retained as exhaustion evidence;
- `failed` stops the secret's chain immediately.

Static non-candidates are omitted from the attempt list. Not-ready and disabled sources become
core-owned blocked attempts when they were otherwise candidates. Structurally invalid mapping data
fails before traversal when possible. An ambiguous provider rejection is `failed/lookup-rejected`; a
valid reference to a proven absent target is `missing`.

Preview aggregation retains ordered attempts and reports the disposition achieved under the current
impact:

- `available` when a source establishes presence, including after an earlier indeterminate source;
- `missing` when at least one candidate lookup ran and every reachable candidate ordinarily missed;
- `indeterminate` when traversal exhausts after an impact-limited source and no later success or
  hard failure establishes the current-impact disposition;
- `blocked` when no success, uncertainty, or failure exists and an execution, readiness, or
  applicability limitation remains, including `no-candidate`;
- `failed` whenever a hard failure is reached.

A later available or failed result does not erase an earlier indeterminate attempt. That evidence is
load-bearing only for callers such as preflight that ask whether a higher-impact authoritative pass
could avoid the later failure; it never downgrades a current hard failure or success in inspection,
verification, or doctor.

Operation preflight memoizes previews once per secret and command. It populates the memo lazily in
node order, so duplicate references cannot repeat provider work, and secrets referenced only by
unreachable later nodes cause no lookup or audit event. The established first-failing node order
remains unchanged. Actual resolution never reuses a preview result.

### Actual-resolution pass

Actual resolution performs one bounded source-first pass. Each ready source receives the unresolved
candidate batch once, returns exact resolution variants, and releases its resources before core
continues. Missing and TTY blocks fall through, failed hard-stops each affected chain, and resolved
values remain in the private batch. There is no preview pass, impact frontier, retry at a broader
authority, or single-request interaction loop. Complete resolution still finishes before the
consuming operation's first external mutation.

### Core diagnostics

Backends return only a result tag and, where required, a closed reason. Core adds the known source
name and safe lookup identifier, then derives any hint at the consuming surface. For example:

- `tty-unavailable` can suggest running the command with usable terminal input;
- `tty-interaction-disabled` can explain that global `--non-interactive` disabled terminal use;
- preview `operator-impact-limited` can mention `--allow-interaction` on commands that offer it;
- `authentication` can map to the existing sign-in guidance.

No backend chooses remediation, command syntax, halt behavior, fallback behavior, or prose. Existing
machine-facing remediation fields may remain for compatibility only when core derives them from the
closed result.

## Caller semantics

| Caller              | Preview impact  | Result treatment                                                     |
| ------------------- | --------------- | -------------------------------------------------------------------- |
| operation preflight | none            | accept available/indeterminate; failed only with earlier uncertainty |
| `secret describe`   | none by default | render every result; `--allow-interaction` removes indeterminate     |
| `secret verify`     | none by default | only available succeeds; `--allow-interaction` removes indeterminate |
| doctor secret check | none            | report indeterminate as uncertainty and failed as failure            |
| `secret list`       | no preview      | show static mapping applicability only                               |

Actual resolution has no row in this table because it has no preview-impact policy. Global
`--non-interactive` controls only TTY access. `--allow-interaction` on describe or verify may be
combined with it: out-of-band preview work is allowed while prompt remains disabled.

## Backend behavior

### Environment variables

Env-var lookup has no operator impact. Preview reads and validates the variable, discards its value,
and returns `PreviewAvailable`, `PreviewMissing`, or `PreviewFailed(malformed-value)` at either
impact level.

### Prompt

Prompt first considers TTY interaction access. Unavailable returns
`PreviewBlocked(tty-unavailable)`; disabled returns `PreviewBlocked(tty-interaction-disabled)`. With
available access and zero preview impact it returns `PreviewIndeterminate(operator-impact-limited)`.
With available access and maximum preview impact it requests and validates the value through the
broker, discards it, and returns `PreviewAvailable` or a typed failure. Actual resolution considers
only TTY access and delivers the accepted value through the private batch when access is available.

### OnePassword

OnePassword determines whether `op read` is known to be non-disruptive from provider-specific
authentication facts and source config. Known service-account or Connect-style authentication can
run at zero impact. Otherwise the conservative source default classifies app authentication as
operator impact, yielding `PreviewIndeterminate(operator-impact-limited)` at zero impact without
invoking `op`.

A source setting can classify app authentication as no operator impact. This setting covers the app
authentication event as a whole because the backend cannot reliably predict whether the app will use
a cached session, biometric, device credential, or another configured method before invocation.

This classification is preview-only. Actual resolution always performs the bounded `op read` for a
candidate request, including under global `--non-interactive` and without a TTY.

When invocation is permitted, preview uses the existing bounded `op read`, discards stdout inside
the backend, and converts native outcomes to exact tags. Valid value is available. An unambiguous
absence marker may be missing. Invalid mapping, authentication, connectivity, deadline, malformed
value, ambiguous not-found text, or external provider error is failed. The v1 implementation keeps
the current narrow item/field markers fail-closed as `lookup-rejected` unless sanitized evidence
from the supported `op` version proves a distinct ordinary-absence token. Locally invalid reference
syntax remains `invalid-mapping`.

## Security and authority boundaries

- Preview variants have no value field, generic metadata, provider message, or serialization escape
  hatch.
- Provider stdout and stderr remain backend-local. Native exceptions do not cross the boundary.
- Backend code receives tagged preview or resolution intent explicitly. Actual resolution receives
  no operator-impact allowance. No prompt broker exists when TTY access is unavailable or disabled.
- A backend that declares no TTY-interaction support never receives a broker and cannot report a TTY
  block. This keeps out-of-band implementations independent of global `--non-interactive` by
  construction.
- Backend factory and context entry receive intent before they run; provider and prompt work is
  confined to the selected client method.
- Core revalidates every backend-produced diagnostic identifier before storing it in an attempt or
  aggregate. Backend authors do not define the output-safety boundary.
- Per-source config may classify only that backend's known actions. It does not rewrite core flow,
  result semantics, or TTY facts.
- Preview results and representations are sentinel-tested for value leakage.
- The operation still resolves required values before its first consuming mutation. An indeterminate
  preflight never postpones resolution until after mutation.

## Contract rewrite posture

The secret-backend contract, descriptor checks, authoring documentation, and all three in-tree
implementations change atomically. The rewrite removes `interactive` and `would_attempt`, makes
lookup description structured, adds the narrower `supports_tty_interaction` capability, and extends
the source client with preview while making resolution TTY-aware but impact-free.

There are no external secret-backend plugins, so no adapter, deprecation window, or parallel runtime
is useful. The descriptor and all three implementations reset their exact registration sentinel from
`2` to `1` atomically. That reset deliberately re-baselines the only supported contract before an
external backend ecosystem exists; it does not change any other capability kind's version.

## Delivery architecture

The operator directed one draft PR to carry the artifacts and eventual implementation. This is an
explicit exception to the saga default of merging reviewed artifacts early. The PR remains draft
with the author-owned `review-requested` label during artifact review because it has no merge
intent. After design convergence and implementation, full gates, independent review, and live
testing, the same PR can become ready to merge. It is never merged by this effort lead.

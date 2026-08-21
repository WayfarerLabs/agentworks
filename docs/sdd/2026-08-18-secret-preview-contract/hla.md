# HLA: Intent-aware secret preview

- Status: Draft for review
- Date: 2026-08-18
- Amended: 2026-08-19
- Governing requirements: [FRD](./frd.md)
- Detailed contracts: [preview contract LLD](./preview-contract-lld.md) and
  [operator surfaces LLD](./operator-surfaces-lld.md)

## Executive summary

The architecture replaces the static backend `interactive` classification with an explicit
operator-impact intent at each backend action. Preview becomes a real source-client operation with a
closed, value-free result sum. Core supplies one policy input, the allowed operator impact, plus
objective execution capabilities such as terminal availability. The backend determines how far it
can proceed and returns the most informative result available within those constraints.

The result separates five semantic states: `available`, ordinary `missing`, impact-limited
`indeterminate`, execution `blocked`, and operational `failed`. Backends classify only those facts.
Core centrally owns fallthrough and hard-stop behavior, so a missing value can use a lower source
but an invalid or failing configured source cannot be silently hidden by fallback.

Actual resolution uses the same impact and classification vocabulary but remains a separate
value-bearing operation. Preflight does not simulate full resolution. It invokes preview at zero
operator impact and treats a higher-precedence indeterminate attempt as not disproven, even when a
later source fails, then the operation performs one authoritative resolution before external
mutation.

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

The actual resolution path is parallel, not downstream of preview. It passes the operation's impact
allowance to the same source-bound client and receives values through the existing private batch. No
caller uses preview as a value cache or extracts a value from its result.

## Components and responsibilities

### Operator-impact policy

`OperatorImpact` replaces `InteractionPolicy` as the exact, caller-supplied intent type. Its initial
levels are:

- `NONE`: the backend may not knowingly cause an operator action.
- `ALLOW`: the maximum level; operator actions are permitted.

This is deliberately a two-level contract. New levels require an observed need and a contract
revision. Certainty is not a policy member. TTY availability is not a policy member.

Every public service boundary that accepts this value checks exact enum identity before work, as the
current interaction policy does. Ordinary CLI roots derive `ALLOW` unless global `--non-interactive`
was selected. They do not consult `isatty()` when deriving authority.

### Execution capabilities

Core records whether a safe terminal input channel is available. This fact is supplied to preview
without changing the impact allowance. For actual resolution, core supplies a prompt broker only
when both conditions hold: operator impact is allowed and usable terminal input exists.

This produces truthful combinations:

| Impact | TTY | OnePassword app path                                          | Prompt path                             |
| ------ | --- | ------------------------------------------------------------- | --------------------------------------- |
| none   | no  | probe only when backend knows/configures it as non-disruptive | `blocked/tty-unavailable`               |
| none   | yes | same backend decision                                         | `indeterminate/operator-impact-limited` |
| allow  | no  | bounded read is allowed                                       | `blocked/tty-unavailable`               |
| allow  | yes | bounded read is allowed                                       | `available`, `missing`, or `failed`     |

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

### Source-client results

The operation-bounded `SecretSourceClient` gains one batch `preview` method. Core supplies exact
operator impact, terminal availability, an optional prompt broker, and the shrinking operation
budget once, when it constructs the client. `preview` and `resolve` then receive only candidate
requests; their authority cannot disagree with their client's construction. Each method returns
exactly one closed tagged result per request and no other payload:

- `PreviewAvailable`: a valid value exists; stop successfully;
- `PreviewMissing`: a valid lookup established ordinary absence; fall through silently;
- `PreviewIndeterminate`: broader operator-impact authority could change the answer; preview falls
  through but preserves precedence uncertainty;
- `PreviewBlocked`: current execution, readiness, or applicability state prevents resolution; a
  source attempt falls through and retains the reason. Core uses `blocked/no-candidate` for
  aggregate exhaustion where no lookup ran;
- `PreviewFailed`: mapping, provider, deadline, value, protocol, or unexpected failure; hard-stop.

Actual resolution returns a parallel sum:

- `BackendResolved(value)`: the only value-bearing result, with redacted representation;
- `BackendMissing`: ordinary fallthrough;
- `BackendBlocked`: authority or execution fallthrough;
- `BackendFailed`: hard-stop.

There is no actual-resolution `indeterminate`: insufficient authority is a definite block for that
operation. Result types carry closed reasons only where a reason changes diagnostics. They contain
no remediation, provider message, arbitrary metadata, or backend-selected flow instruction.

### Lifecycle and provider boundary

Core passes exact impact and terminal facts into `create_client` before backend factory or
context-entry code can run. Construction remains resource-free, context entry is provider-I/O-free,
and the selected method owns provider work. The old `prepare` and `external_operation_timeout` hooks
are removed: every in-tree `prepare` is a no-op, and a timeout declaration evaluated before client
construction would be an authority-blind backend hook. A backend validates its timeout in source
config, enforces it inside its provider boundary together with the supplied remaining-time view, and
returns `failed/deadline-exceeded`. This leaves no pre-policy setup phase and one typed result
surface per operation.

Backends share their private acquisition path between preview and resolve:

- preview acquires and validates enough to classify the result, constructs a value-free tag, and
  discards any value before returning;
- resolution acquires through the same provider logic and returns one exact resolution variant per
  request through the private resolution batch;
- native text is classified inside the backend for both paths.

At `OperatorImpact.ALLOW`, runtime contract enforcement rejects `PreviewIndeterminate` as a backend
protocol failure. It still accepts `blocked` and `failed`, because maximum authority cannot
guarantee a terminal, network response, authentication success, or valid mapping. Actual resolution
likewise rejects `BackendBlocked(operator-impact-limited)` at `ALLOW`; core converts that whole
source turn to protocol failure rather than repeat an impossible higher-authority frontier.

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

### Complete-batch authority staging

Complete actual resolution under caller authority `ALLOW` preserves batch doom through iterative
backend-owned gating. Core closes every reachable actual-resolution path at `NONE` and stops each
unresolved secret at its first `BackendBlocked(operator-impact-limited)` frontier. If the batch
remains viable, it runs exactly one single-request `ALLOW` turn, returns to no-impact closure for
that request's fallthrough, and checks viability again before another `ALLOW` turn. A known
`BackendFailed` result therefore dooms pending secrets before every later operator-impacting action,
including another request at the same configured source. Multiple impact-bearing frontiers are real:
a chain may contain both OnePassword and prompt, and different required secrets may stop at the same
source. A fixed two-pass or opaque source batch cannot preserve precedence and regain core control
before each next interaction.

Values acquired at `NONE` remain in the private resolution batch; no preview result is promoted to
authority. Caller impact `NONE` and partial resolution use one pass at their exact impact.

### Core diagnostics

Backends return only a result tag and, where required, a closed reason. Core adds the known source
name and safe lookup identifier, then derives any hint at the consuming surface. For example:

- `tty-unavailable` can suggest running the command with usable terminal input;
- `operator-impact-limited` can mention `--allow-interaction` on commands that offer it or explain
  global `--non-interactive` on ordinary commands;
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

Actual ordinary resolution uses `ALLOW` unless global `--non-interactive` selects `NONE`. Explicit
verification remains conservative by default even though ordinary resolution is permissive. That is
a caller decision, not a backend exception.

## Backend behavior

### Environment variables

Env-var lookup has no operator impact. Preview reads and validates the variable, discards its value,
and returns `PreviewAvailable`, `PreviewMissing`, or `PreviewFailed(malformed-value)` at either
impact level.

### Prompt

Prompt first considers terminal availability. With no usable TTY it returns
`PreviewBlocked(tty-unavailable)`. With a TTY and zero impact it returns
`PreviewIndeterminate(operator-impact-limited)`. With maximum impact it requests and validates the
value through the broker, discards it, and returns `PreviewAvailable` or a typed failure. Actual
resolution follows the same gates but delivers the accepted value through the private batch.

### OnePassword

OnePassword determines whether `op read` is known to be non-disruptive from provider-specific
authentication facts and source config. Known service-account or Connect-style authentication can
run at zero impact. Otherwise the conservative source default classifies app authentication as
operator impact, yielding `PreviewIndeterminate(operator-impact-limited)` at zero impact without
invoking `op`.

A source setting can classify app authentication as no operator impact. This setting covers the app
authentication event as a whole because the backend cannot reliably predict whether the app will use
a cached session, biometric, device credential, or another configured method before invocation.

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
- Backend code receives operator impact explicitly and receives no prompt broker when actual prompt
  authority or terminal capability is absent.
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
lookup description structured, and extends the source client with preview and impact-aware
resolution.

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

# HLA: Intent-aware secret preview

- Status: Draft for review
- Date: 2026-08-18
- Governing requirements: [FRD](./frd.md)
- Detailed contracts: [preview contract LLD](./preview-contract-lld.md) and
  [operator surfaces LLD](./operator-surfaces-lld.md)

## Executive summary

The architecture replaces the static backend `interactive` classification with an explicit
operator-impact intent at each backend action. Preview becomes a real source-client operation with a
tri-state, value-free result. Core supplies one policy input, the allowed operator impact, plus
objective execution capabilities such as terminal availability. The backend determines how far it
can proceed and returns its best answer.

Actual resolution uses the same operator-impact vocabulary but remains a separate value-bearing
operation. Preflight does not simulate full resolution. It invokes preview at zero operator impact
and treats `maybe` as not disproven, then the operation performs one authoritative resolution before
external mutation.

## Architectural shape

```text
caller semantics
    |
    | operator-impact allowance + execution facts
    v
secret preview orchestrator
    |
    | validated mapping requests, active-source order, bounded lifetime
    v
source-bound backend client
    |
    | optional provider read, value stays here and is discarded
    v
closed backend preview {yes | no | maybe, detail}
    |
    v
core chain aggregation + caller-owned rendering or decision
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
revision. `maybe` is not a policy member. TTY availability is not a policy member.

Every public service boundary that accepts this value checks exact enum identity before work, as the
current interaction policy does. Ordinary CLI roots derive `ALLOW` unless global `--non-interactive`
was selected. They do not consult `isatty()` when deriving authority.

### Execution capabilities

Core records whether a safe terminal input channel is available. This fact is supplied to preview
without changing the impact allowance. For actual resolution, core supplies a prompt broker only
when both conditions hold: operator impact is allowed and usable terminal input exists.

This produces truthful combinations:

| Impact | TTY | OnePassword app path                                          | Prompt path                     |
| ------ | --- | ------------------------------------------------------------- | ------------------------------- |
| none   | no  | probe only when backend knows/configures it as non-disruptive | `no/tty-unavailable`            |
| none   | yes | same backend decision                                         | `maybe/operator-impact-limited` |
| allow  | no  | bounded read is allowed                                       | `no/tty-unavailable`            |
| allow  | yes | bounded read is allowed                                       | definitive prompt-backed result |

### Static lookup description

Inspection still needs a no-I/O description of how a source maps a secret. The rewritten
`describe_lookup` operation returns a structured lookup disposition and safe identifier. It says
only whether the backend has a candidate lookup from the declaration. It does not predict runtime
presence and is not a second preview method.

This structured projection replaces `would_attempt` and keeps `secret list` cheap. `secret list`
continues to describe mappings and readiness; it does not open provider clients.

### Source-client preview

The operation-bounded `SecretSourceClient` gains one batch `preview` method. It receives candidate
requests, the exact operator-impact allowance, terminal availability, and the shrinking source-turn
budget. It returns one closed result per request and no other payload.

Backends share their private acquisition path between preview and resolve:

- preview acquires and validates enough to answer, constructs a value-free result, and discards any
  value before returning;
- resolution acquires through the same provider logic and returns the value through the private
  resolution batch;
- native text is classified inside the backend for both paths.

At `OperatorImpact.ALLOW`, runtime contract enforcement rejects `maybe` as a backend protocol
violation. Expected inability is represented as typed `no`, not raised as an opaque exception.

### Chain aggregation

Core walks ready, active sources in precedence order. Static non-candidates and soft negative
answers fall through. Hard provider failures stop the known path. An earlier `maybe` remains
load-bearing because that source might win or fail before a later source is reached; a later answer
must not erase it.

At maximum impact no backend can return `maybe`, so aggregation is definitive. At zero impact,
aggregation returns:

- `yes` only when the precedence path to a successful source contains no unresolved earlier source;
- `no` only when every reachable path is definitively negative;
- `maybe` when an earlier impact-limited source could change the result.

The aggregate retains ordered per-source attempts so human and JSON surfaces can explain the answer
without provider prose.

### Core diagnostics

Backend results carry an answer and closed detail only. Core adds known source name and safe lookup
identifier, then derives any hint at the consuming surface. For example:

- `tty-unavailable` can suggest running the command with usable terminal input;
- `operator-impact-limited` can mention `--allow-interaction` on commands that offer it or explain
  global `--non-interactive` on ordinary commands;
- `authentication` can map to the existing sign-in guidance.

No backend chooses remediation, command syntax, or prose. Existing machine-facing remediation fields
may remain for compatibility only when they are derived from the closed detail by core.

## Caller semantics

| Caller              | Preview impact  | `maybe` meaning                                              | Definitive mode                          |
| ------------------- | --------------- | ------------------------------------------------------------ | ---------------------------------------- |
| operation preflight | none            | pass the impossibility screen                                | none; actual resolution is authoritative |
| `secret describe`   | none by default | render uncertainty                                           | `--allow-interaction`                    |
| `secret verify`     | none by default | render non-success and exit 1                                | `--allow-interaction`                    |
| doctor secret check | none            | report uncertainty without treating it as a provider failure | no disruptive mode                       |
| `secret list`       | no preview      | not applicable                                               | not applicable                           |

Actual ordinary resolution uses `ALLOW` unless global `--non-interactive` selects `NONE`. Explicit
verification remains conservative by default even though ordinary resolution is permissive. That is
a caller decision, not a backend exception.

## Backend behavior

### Environment variables

Env-var lookup has no operator impact. Preview reads and validates the variable, discards its value,
and returns `yes/available` or `no/soft-miss` at either impact level.

### Prompt

Prompt first considers terminal availability. With no usable TTY it returns `no/tty-unavailable`.
With a TTY and zero impact it returns `maybe/operator-impact-limited`. With maximum impact it
requests and validates the value through the broker, discards it, and returns a definitive result.
Actual resolution follows the same gates but delivers the accepted value through the private batch.

### OnePassword

OnePassword determines whether `op read` is known to be non-disruptive from provider-specific
authentication facts and source config. Known service-account or Connect-style authentication can
run at zero impact. Otherwise the conservative source default classifies app authentication as
operator impact, yielding `maybe` at zero impact without invoking `op`.

A source setting can classify app authentication as no operator impact. This setting covers the app
authentication event as a whole because the backend cannot reliably predict whether the app will use
a cached session, biometric, device credential, or another configured method before invocation.

When invocation is permitted, preview uses the existing bounded `op read`, discards stdout inside
the backend, and converts native failure text to closed details.

## Security and authority boundaries

- A preview type has no value field, generic metadata, provider message, or serialization escape
  hatch.
- Provider stdout and stderr remain backend-local. Native exceptions do not cross the boundary.
- Backend code receives operator impact explicitly and receives no prompt broker when actual prompt
  authority or terminal capability is absent.
- Per-source config may classify only that backend's known actions. It does not rewrite core answer
  semantics or TTY facts.
- Preview results and representations are sentinel-tested for value leakage.
- The operation still resolves required values before its first consuming mutation. A preflight
  `maybe` never postpones resolution until after mutation.

## Contract rewrite posture

The secret-backend contract, descriptor checks, authoring documentation, and all three in-tree
implementations change atomically. The rewrite removes `interactive` and `would_attempt`, makes
lookup description structured, and extends the source client with preview and impact-aware
resolution.

There are no external secret-backend plugins, so no adapter, deprecation window, or parallel runtime
is useful. The contract remains in its current 1.0 generation. The implementation's existing
internal conformance sentinel is not bumped merely to sequence unpublished shapes.

## Delivery architecture

The operator directed one draft PR to carry the artifacts and eventual implementation. This is an
explicit exception to the saga default of merging reviewed artifacts early. The PR remains draft
with the author-owned `review-requested` label during artifact review because it has no merge
intent. After design convergence and implementation, full gates, independent review, and live
testing, the same PR can become ready to merge. It is never merged by this effort lead.

# LLD: Secret preview and impact contracts

- Status: Draft for review
- Date: 2026-08-18
- Governing design: [HLA](./hla.md)

## Contract goals

The contract must let a backend perform provider-aware work without returning a value, must make
`maybe` impossible at maximum impact, and must keep execution facts distinct from caller authority.
The shapes below are normative at the design level. Exact module-local naming may change during
implementation only if the same invariants and public vocabulary remain intact.

## Core types

```python
class OperatorImpact(StrEnum):
    NONE = "none"
    ALLOW = "allow"


class TerminalAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class PreviewAnswer(StrEnum):
    YES = "yes"
    NO = "no"
    MAYBE = "maybe"
```

`OperatorImpact` is the sole preview policy. `TerminalAvailability` is an observed process fact and
must be exact-validated before dispatch. Neither type has a truthiness shortcut or string coercion.

The initial closed details are:

```python
class PreviewDetail(StrEnum):
    AVAILABLE = "available"
    NOT_APPLICABLE = "not-applicable"
    SOFT_MISS = "soft-miss"
    SOURCE_NOT_READY = "source-not-ready"
    BACKEND_PLUGIN_DISABLED = "backend-plugin-disabled"
    OPERATOR_IMPACT_LIMITED = "operator-impact-limited"
    TTY_UNAVAILABLE = "tty-unavailable"
    DEADLINE_EXCEEDED = "deadline-exceeded"
    HARD_MAPPING = "hard-mapping"
    AUTHENTICATION = "authentication"
    CONNECTIVITY = "connectivity"
    EXTERNAL = "external"
    MALFORMED_VALUE = "malformed-value"
    BACKEND_PROTOCOL = "backend-protocol"
    UNEXPECTED = "unexpected"
```

The enum is core-owned and shared by preview and actual-resolution result blocks. Backend code
selects a member but cannot extend the set. Core-only source-readiness and plugin-disabled rows use
the same vocabulary when building aggregate attempts.

## Result rules

```python
@dataclass(frozen=True, slots=True)
class BackendPreview:
    answer: PreviewAnswer
    detail: PreviewDetail

    def __repr__(self) -> str:
        return f"BackendPreview(answer={self.answer.value!r}, detail={self.detail.value!r})"
```

The preview constructor enforces these combinations:

| Answer | Allowed details                                                                                      |
| ------ | ---------------------------------------------------------------------------------------------------- |
| yes    | `available`                                                                                          |
| maybe  | `operator-impact-limited`                                                                            |
| no     | `soft-miss`, `tty-unavailable`, and the closed provider, value, timeout, or protocol failure details |

The type has no name, source, identifier, remediation, message, cause, value, metadata dictionary,
or provider-native exception. Core already owns the request name, source, and identifier. It adds
those safe fields after validating the backend result.

`SOURCE_NOT_READY`, `BACKEND_PLUGIN_DISABLED`, and `BACKEND_PROTOCOL` are constructed by core, not
returned by a backend client. `UNEXPECTED` is core's closed projection of an exception whose text is
discarded.

## Static lookup description

`would_attempt` is removed. `describe_lookup` becomes the no-I/O declaration projection:

```python
class LookupDisposition(StrEnum):
    CANDIDATE = "candidate"
    NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True, slots=True)
class LookupDescription:
    disposition: LookupDisposition
    identifier: str | None


@classmethod
def describe_lookup(
    cls,
    secret_name: str,
    mapping: BaseModel | None,
) -> LookupDescription: ...
```

This method validates a backend's mapping applicability and produces the safe identifier used by
tables and diagnostics. It does not read environment variables, open a provider, inspect auth, or
answer whether a value exists. The exact `False` opt-out remains framework-owned and never reaches
the backend.

Core constructs `SecretLookupRequest` only for `CANDIDATE` rows. This preserves cheap mapping tables
without retaining a second runtime preview.

## Client creation and source-client methods

Core communicates authority before any backend-controlled lifecycle hook by changing the backend
factory shape to:

```python
@classmethod
def create_client(
    cls,
    *,
    source_name: str,
    config: AgwModel,
    impact: OperatorImpact,
    terminal: TerminalAvailability,
    interaction_broker: InteractionBroker | None,
    remaining_time: RemainingTime,
) -> AbstractContextManager[SecretSourceClient]: ...
```

`impact` and `terminal` are exact-validated before factory invocation. The broker is present only
for a prompt preview or resolution with `ALLOW` and an available terminal. Factory construction
remains resource-free, and context entry may acquire operation-local resources but must not invoke a
provider or prompt; provider work belongs to the selected client method. Passing intent before both
hooks closes the authority gap even for a library whose nominally local setup has an unexpected side
effect. In-tree and adversarial-fixture tests prove that disallowed calls produce no provider or
broker observation during factory construction, context entry, or the selected method.

The old `prepare` hook is removed. All three implementations currently make it a no-op, and keeping
an additional provider-work phase would create a second result and failure surface without adding
capability.

The rewritten protocol is:

```python
class SecretSourceClient(Protocol):
    def preview(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        impact: OperatorImpact,
        terminal: TerminalAvailability,
        remaining_time: RemainingTime,
    ) -> Mapping[str, BackendPreview]: ...

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        impact: OperatorImpact,
        terminal: TerminalAvailability,
        remaining_time: RemainingTime,
    ) -> Mapping[str, BackendResolution]: ...
```

Actual resolution uses an explicit result sum:

```python
@dataclass(frozen=True, slots=True, repr=False)
class BackendResolved:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or "\0" in self.value:
            raise ValueError("invalid resolved secret value")

    def __repr__(self) -> str:
        return "BackendResolved(value=<redacted>)"


@dataclass(frozen=True, slots=True)
class BackendBlocked:
    detail: PreviewDetail


type BackendResolution = BackendResolved | BackendBlocked
```

`BackendResolved` is the one value-bearing boundary and is legal only on actual resolution. Its
constructor enforces the minimum framework value contract and its representations are redacted.
`BackendBlocked` permits exactly `soft-miss`, `operator-impact-limited`, `tty-unavailable`,
`deadline-exceeded`, `hard-mapping`, `authentication`, `connectivity`, `external`, and
`malformed-value`. Core-only details and `available` are rejected. Unlike preview, actual resolution
has no `maybe`: `operator-impact-limited` is a definitive block under the authority of that
operation.

For resolution, `terminal` is still an execution fact. The optional interaction broker is an
authority-bearing capability supplied only when impact and terminal permit it. A backend must not
infer permission from terminal availability.

Each method returns exactly one entry for each request it owns. Missing keys, extra keys, wrong
types, illegal detail combinations, and `maybe` under `OperatorImpact.ALLOW` are backend protocol
failures. Core validates the whole map before rendering, aggregating, or copying any resolved value.
Expected provider conditions use the closed result blocks, not exceptions. The exact exception
boundary is defined below.

## Backend algorithm

For each candidate request, a conforming preview follows this order:

1. Check objective blockers the backend can establish without operator impact. For prompt, missing
   terminal input is such a blocker.
2. Determine the least operator impact of the next necessary action using provider facts and source
   config.
3. If that action exceeds the allowance, exhaust any remaining permitted alternatives. Return
   `maybe/operator-impact-limited` only if no permitted route can answer.
4. Otherwise perform the bounded provider action. Validate the minimum secret-value contract inside
   the backend, construct the closed result, and release the local value before returning.
5. Convert native absence and failures to closed details. Native text and exception messages do not
   cross the boundary.

At maximum impact, step 3 is unreachable because there is no higher operator-impact class. A backend
that still cannot answer returns typed `no` for the current limitation or failure.

## OnePassword impact classification

`OnePasswordSourceConfig` adds a conservative setting:

```yaml
spec:
  backend:
    name: onepassword
    app_authentication_impact: operator-action # default
```

Accepted values are `operator-action` and `none`. The exact field spelling is part of artifact
review. It applies only when the backend cannot establish a known unattended authentication mode.
When service-account or Connect authentication is present, the backend can classify the read as no
operator impact without an override. When app authentication may occur:

- `operator-action` plus preview impact `NONE` returns `maybe` without starting `op`;
- `none` permits the bounded read at preview impact `NONE`;
- preview impact `ALLOW` permits the bounded read under either setting.

Actual resolution applies the same classification. Under insufficient impact it returns
`BackendBlocked(operator-impact-limited)` without invoking `op`.

The setting intentionally does not say `biometric`. 1Password may choose cached access, a biometric,
a device credential, or another app-configured method after invocation; Agentworks cannot reliably
distinguish those paths beforehand.

## Prompt behavior

| Terminal    | Impact        | Preview behavior                                                   |
| ----------- | ------------- | ------------------------------------------------------------------ |
| unavailable | none or allow | `no/tty-unavailable`; never call broker                            |
| available   | none          | `maybe/operator-impact-limited`; never call broker                 |
| available   | allow         | request, validate, and discard the value; return definitive result |

Actual resolution uses the same ordering. At `ALLOW`, an absent broker is a core protocol error if
terminal was declared available. At `NONE`, no broker is exposed.

## Environment behavior

Env-var preview reads the selected environment variable at both impact levels. Unset is
`no/soft-miss`; a valid value is `yes/available`; a value rejected by the minimum framework value
contract is `no/malformed-value`. The local string is discarded before return.

## Exhaustive source disposition

Every active source contributes an ordered attempt. Core constructs rows for static non-candidates,
not-ready sources, disabled backend plugins, boundary expiry, backend protocol failure, and
unexpected exceptions; a client constructs the remaining rows. The disposition is exhaustive:

| Detail                    | Preview answer and flow                                       | Actual-resolution flow                                     |
| ------------------------- | ------------------------------------------------------------- | ---------------------------------------------------------- |
| `available`               | `yes`; stop; an earlier `maybe` keeps the aggregate uncertain | copy the value and stop this request                       |
| `not-applicable`          | `no`; do not open a client; fall through                      | do not open a client; fall through                         |
| `source-not-ready`        | `no`; do not open a client; fall through                      | do not open a client; fall through                         |
| `backend-plugin-disabled` | `no`; do not open a client; fall through                      | do not open a client; fall through                         |
| `soft-miss`               | `no`; fall through                                            | block this source and fall through                         |
| `operator-impact-limited` | `maybe`; fall through but lock aggregate certainty at maybe   | block without provider work and fall through               |
| `tty-unavailable`         | `no`; fall through                                            | block without broker or stdin access and fall through      |
| `deadline-exceeded`       | `no`; hard-stop this request                                  | hard-stop this request                                     |
| `hard-mapping`            | `no`; hard-stop this request                                  | hard-stop this request                                     |
| `authentication`          | `no`; hard-stop this request                                  | hard-stop this request                                     |
| `connectivity`            | `no`; hard-stop this request                                  | hard-stop this request                                     |
| `external`                | `no`; hard-stop this request                                  | hard-stop this request                                     |
| `malformed-value`         | `no`; hard-stop this request                                  | discard the value and hard-stop this request               |
| `backend-protocol`        | core `no`; hard-stop this request                             | core hard-stop; discard every returned value from the turn |
| `unexpected`              | core `no`; hard-stop this request                             | core hard-stop; discard exception text and partial values  |

A hard stop ends later-source traversal for that request. When a preview already has an earlier
`maybe`, a later hard stop still ends traversal but the aggregate remains `maybe`: the earlier
higher-precedence source could have resolved under broader authority. Actual-resolution exhaustion
retains the existing evidence priority: first operator-impact block, then first TTY block, then
first soft miss, then first not-ready or plugin-disabled source, then no-active-source or
no-attemptable-source. A later resolved value or hard stop takes precedence over that fallthrough
evidence.

## Complete-batch authority staging

Complete actual resolution preserves the existing rule that a known failure in one required secret
prevents operator-impacting work for another secret that cannot make the batch complete. Static
`interactive` cannot implement that rule after this rewrite, and non-authoritative preview is not
reused. Instead, a caller that authorizes `ALLOW` uses two actual-resolution stages:

1. **No-impact closure:** core creates clients with `OperatorImpact.NONE`, no prompt broker, and the
   real terminal fact. Each required secret advances from its current frontier until it resolves
   without classified impact, reaches a hard stop or definitive exhaustion, or reaches its first
   `operator-impact-limited` block. An impact block becomes a pending frontier at that source; this
   pass does not inspect lower-precedence sources for that secret.
2. **Viability check:** if any required secret hard-stopped or exhausted, no `ALLOW` client is
   created. Pending secrets receive the existing core-only `batch-doomed-before-interaction`
   outcome.
3. **One authority turn:** otherwise core selects the earliest pending source in configured order,
   creates one fresh `ALLOW` client, and batches every request whose frontier is that source. A
   broker is supplied only where terminal capability permits. Resolved and hard-stop results become
   final; fallthrough results advance just those requests beyond that source.
4. Core returns to no-impact closure for every advanced unresolved frontier, repeats the viability
   check, and permits the next `ALLOW` source turn only while the batch is still completable. The
   loop ends when all required secrets resolve or a known failure dooms the remainder.

No-impact closure calls `resolve`, not `preview`; a value resolved under `NONE` is retained only in
the private resolution batch and is authoritative. A pending source has performed no
operator-impacting provider action, so retrying it with `ALLOW` does not duplicate such work. TTY
blocks and other fallthrough details advance during closure. Rechecking after every authority turn
preserves the existing before-every-interaction doom guarantee even when that turn discovers a hard
failure. Caller impact `NONE` uses one ordinary pass where impact blocks fall through as the table
specifies. Partial resolution has no complete-batch guarantee and likewise uses one pass at the
caller's exact impact. Per-source budgets, cleanup, source-first batching, value containment, and
fail-before-mutation apply throughout the loop.

## Aggregate attempt model

Core wraps each backend result as:

```python
@dataclass(frozen=True, slots=True)
class SourcePreviewAttempt:
    source: str
    identifier: str | None
    answer: PreviewAnswer
    detail: PreviewDetail
```

`ResolutionPreview` owns the secret name, aggregate answer, selected or limiting source and
identifier, plus ordered attempts. It is value-free and has a redacted, field-bounded
representation.

Aggregation preserves resolution precedence:

1. `not-applicable`, `soft-miss`, impact-limited, TTY-limited, and not-ready rows fall through.
2. A hard `no` stops traversal; an earlier `maybe` keeps the aggregate uncertain.
3. A `yes` becomes aggregate `yes` only when no earlier `maybe` exists.
4. Once an earlier `maybe` exists, later attempts may improve diagnostics but cannot erase the
   aggregate `maybe`.
5. Exhaustion with no success or uncertainty is aggregate `no`.

At `ALLOW`, rule 4 is unreachable and the result is definitive.

## Error and hint projection

Expected backend conditions are results. The synchronous driver uses this exact exception boundary:

- `UserAbort` and `concurrent.futures.CancelledError`, both `Exception` subclasses, are re-raised;
- every `BaseException` that is not an `Exception` propagates naturally, including
  `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, and `asyncio.CancelledError`;
- every other `Exception` becomes core-owned `unexpected` for every request in that source turn; its
  text, traceback, and partial values do not enter the result.

The source context's `__exit__` receives the original exception information and runs before a
protected exception is re-raised. Cleanup failure is warned through the existing value-free path and
never replaces the primary exception. If `__enter__` fails after partial local acquisition, the
backend must clean that partial acquisition before re-raising. The contract is synchronous; adding
another concurrency runtime or cancellation class requires an explicit contract update.

Backend results have no remediation. Core owns an exhaustive `PreviewDetail` to hint mapping at each
operator surface. Adding a detail requires updating that mapping and the machine-output projection.
A hint may include a known command flag; a backend may not.

`SecretClientFailure`, `SecretClientRemediation`, and `SecretClientTimeout` are removed from the
backend contract. Expected client conditions use `BackendBlocked`; core derives existing resolution
category and remediation fields from its detail. Boundary-budget expiry is also a core-constructed
`deadline-exceeded` block. Existing machine remediation fields remain derived until a separately
reviewed schema change removes them.

## Conformance and leak checks

Contract registration and runtime tests cover:

- exact required class operations and attributes;
- exact impact and terminal types at public boundaries;
- complete, exact-key preview maps;
- legal answer/detail pairs;
- maximum-impact rejection of `maybe`;
- no free-form or generic metadata fields;
- no stdout, stderr, native exception text, or sentinel value in result objects and `repr`;
- prompt broker absence under disallowed impact or missing TTY;
- no provider or broker call during construction or context entry;
- bounded provider work and cleanup on success, failure, timeout, and interruption;
- parity between preview and resolve classification for the same fake-provider result.

Tests assert structured fields and behavioral effects, not authored prose wording.

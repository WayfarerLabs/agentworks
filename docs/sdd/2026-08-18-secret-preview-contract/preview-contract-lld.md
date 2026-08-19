# LLD: Secret preview and impact contracts

- Status: Draft for review
- Date: 2026-08-18
- Amended: 2026-08-19
- Governing design: [HLA](./hla.md)

## Contract goals

The contract must let a backend perform provider-aware work without returning a value, distinguish
ordinary absence from inability and failure, eliminate impact-limited uncertainty at maximum impact,
and keep execution facts distinct from caller authority. The shapes below are normative at the
design level. Exact module-local naming may change during implementation only if the same invariants
and public vocabulary remain intact.

## Policy and reason types

```python
class OperatorImpact(StrEnum):
    NONE = "none"
    ALLOW = "allow"


class TerminalAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class IndeterminateReason(StrEnum):
    OPERATOR_IMPACT_LIMITED = "operator-impact-limited"


class BlockReason(StrEnum):
    OPERATOR_IMPACT_LIMITED = "operator-impact-limited"
    TTY_UNAVAILABLE = "tty-unavailable"
    SOURCE_NOT_READY = "source-not-ready"
    BACKEND_PLUGIN_DISABLED = "backend-plugin-disabled"
    NO_CANDIDATE = "no-candidate"


class FailureReason(StrEnum):
    INVALID_MAPPING = "invalid-mapping"
    AUTHENTICATION = "authentication"
    CONNECTIVITY = "connectivity"
    DEADLINE_EXCEEDED = "deadline-exceeded"
    EXTERNAL = "external"
    MALFORMED_VALUE = "malformed-value"
    BACKEND_PROTOCOL = "backend-protocol"
    UNEXPECTED = "unexpected"
```

`OperatorImpact` is the sole preview policy. `TerminalAvailability` is an observed process fact and
must be exact-validated before dispatch. Neither type has a truthiness shortcut or string coercion.

The reason enums are core-owned and closed. Backend code selects a permitted member but cannot
extend the set. `source-not-ready`, `backend-plugin-disabled`, `no-candidate`, `backend-protocol`,
and `unexpected` are core-constructed only. `no-candidate` is legal only on the aggregate, not a
source attempt. The two occurrences of `operator-impact-limited` are deliberately typed differently:
preview uncertainty says broader authority could answer, while actual-resolution blockage says the
current operation lacks that authority.

There is no common catch-all detail enum. The tagged result determines which reason type, if any, is
legal.

## Preview result sum

```python
@dataclass(frozen=True, slots=True)
class PreviewAvailable:
    pass


@dataclass(frozen=True, slots=True)
class PreviewMissing:
    pass


@dataclass(frozen=True, slots=True)
class PreviewIndeterminate:
    reason: IndeterminateReason


@dataclass(frozen=True, slots=True)
class PreviewBlocked:
    reason: BlockReason


@dataclass(frozen=True, slots=True)
class PreviewFailed:
    reason: FailureReason


type BackendPreview = (
    PreviewAvailable
    | PreviewMissing
    | PreviewIndeterminate
    | PreviewBlocked
    | PreviewFailed
)
```

The five wire statuses are exactly `available`, `missing`, `indeterminate`, `blocked`, and `failed`.
Status is derived from the concrete variant, not accepted as a freely combinable constructor field.
Available and missing have no reason. The other variants require a member of their exact reason
enum.

Backend clients may return:

| Variant                | Backend-returnable reasons                                                         |
| ---------------------- | ---------------------------------------------------------------------------------- |
| `PreviewAvailable`     | none                                                                               |
| `PreviewMissing`       | none                                                                               |
| `PreviewIndeterminate` | `operator-impact-limited`                                                          |
| `PreviewBlocked`       | `tty-unavailable`                                                                  |
| `PreviewFailed`        | invalid mapping, authentication, connectivity, deadline, external, malformed value |

Core may construct blocked results for not-ready or disabled candidate sources, aggregate
`blocked/no-candidate` when no lookup ran, and failed results for protocol violations or unexpected
exceptions. Backends never return those core-only reasons.

The variants have no name, source, identifier, remediation, message, cause, value, metadata
dictionary, provider-native exception, halt flag, or fallback flag. Core already owns request
identity and source traversal. It adds safe identity fields only after validating the backend
result.

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

Core constructs `SecretLookupRequest` only for `CANDIDATE` rows. Static non-candidates stay in the
mapping projection but are omitted from the runtime attempt list; they are neither `PreviewMissing`
nor `PreviewBlocked` because no lookup occurred.

Mapping structure that can be rejected from the declaration fails configuration validation before
client creation. A provider-specific mapping that passes local validation but the provider rejects
later becomes `PreviewFailed(INVALID_MAPPING)`.

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

Each method returns exactly one entry for every request it owns. Core validates the whole map before
rendering, aggregating, or copying any resolved value. Missing keys, extra keys, wrong types,
backend use of core-only reasons, `PreviewIndeterminate` under `OperatorImpact.ALLOW`, or
`BackendBlocked(OPERATOR_IMPACT_LIMITED)` under `OperatorImpact.ALLOW` are backend protocol
failures. Validation uses the exact impact passed to that source turn. Expected provider conditions
use closed result variants, not exceptions. The exact exception boundary is defined below.

## Actual-resolution result sum

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
class BackendMissing:
    pass


@dataclass(frozen=True, slots=True)
class BackendBlocked:
    reason: BlockReason


@dataclass(frozen=True, slots=True)
class BackendFailed:
    reason: FailureReason


type BackendResolution = BackendResolved | BackendMissing | BackendBlocked | BackendFailed
```

`BackendResolved` is the one value-bearing boundary and is legal only on actual resolution. Its
constructor enforces the minimum framework value contract and all representations are redacted.
Backend-returned `BackendBlocked` permits only `operator-impact-limited` and `tty-unavailable`.
Backend-returned `BackendFailed` permits the same backend failure reasons as preview. Core-only
reasons are rejected.

Actual resolution has no indeterminate variant. At `OperatorImpact.NONE`, when a necessary action
exceeds the operation's impact, `BackendBlocked(OPERATOR_IMPACT_LIMITED)` definitively describes
that operation and falls through or becomes a complete-batch pending frontier. At
`OperatorImpact.ALLOW`, the same result is a backend protocol failure because no broader impact
level exists; core converts the entire source turn to failed protocol results and does not repeat
the frontier. For resolution, `terminal` remains an execution fact. The optional interaction broker
is an authority-bearing capability supplied only when impact and terminal permit it. A backend must
not infer permission from terminal availability.

## Backend algorithm

For each candidate request, a conforming preview follows this order:

1. Check objective blockers the backend can establish without operator impact. For prompt, missing
   terminal input is such a blocker.
2. Determine the least operator impact of the next necessary action using provider facts and source
   config.
3. If that action exceeds the allowance, exhaust any remaining permitted alternatives. Return
   `PreviewIndeterminate(OPERATOR_IMPACT_LIMITED)` only if broader impact could still answer.
4. Otherwise perform the bounded provider action. Validate the minimum secret-value contract inside
   the backend, construct the exact result, and release the local value before returning.
5. Classify unambiguous ordinary absence as `PreviewMissing`. Classify invalid mapping, ambiguous
   absence, and provider, deadline, value, or external failures as `PreviewFailed`. Native text and
   exception messages do not cross the boundary.

At maximum impact, step 3 is unreachable because there is no higher operator-impact class. The
backend must still return `PreviewBlocked` when an execution capability is absent and
`PreviewFailed` when permitted work fails.

Actual resolution follows the same acquisition and classification seam. At step 3 under
`OperatorImpact.NONE` it returns `BackendBlocked(OPERATOR_IMPACT_LIMITED)` instead of an
indeterminate result. Step 3 is unreachable at `ALLOW`; a returned impact block is a protocol
failure.

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

- `operator-action` plus preview impact `NONE` returns
  `PreviewIndeterminate(OPERATOR_IMPACT_LIMITED)` without starting `op`;
- `none` permits the bounded read at preview impact `NONE`;
- preview impact `ALLOW` permits the bounded read under either setting.

Actual resolution applies the same classification. Under insufficient impact it returns
`BackendBlocked(OPERATOR_IMPACT_LIMITED)` without invoking `op`.

The setting intentionally does not say `biometric`. 1Password may choose cached access, a biometric,
a device credential, or another app-configured method after invocation; Agentworks cannot reliably
distinguish those paths beforehand.

When `op read` runs, its normalized outcomes map as follows:

| Normalized provider outcome                | Preview result                     | Resolution result                  |
| ------------------------------------------ | ---------------------------------- | ---------------------------------- |
| valid value                                | `PreviewAvailable`                 | `BackendResolved`                  |
| supported, unambiguous absence marker      | `PreviewMissing`                   | `BackendMissing`                   |
| provider rejects reference syntax or shape | `PreviewFailed(INVALID_MAPPING)`   | `BackendFailed(INVALID_MAPPING)`   |
| ambiguous item/field not-found text        | `PreviewFailed(INVALID_MAPPING)`   | `BackendFailed(INVALID_MAPPING)`   |
| authentication failure                     | `PreviewFailed(AUTHENTICATION)`    | `BackendFailed(AUTHENTICATION)`    |
| connectivity failure                       | `PreviewFailed(CONNECTIVITY)`      | `BackendFailed(CONNECTIVITY)`      |
| boundary deadline                          | `PreviewFailed(DEADLINE_EXCEEDED)` | `BackendFailed(DEADLINE_EXCEEDED)` |
| other classified provider failure          | `PreviewFailed(EXTERNAL)`          | `BackendFailed(EXTERNAL)`          |
| returned value violates minimum contract   | `PreviewFailed(MALFORMED_VALUE)`   | `BackendFailed(MALFORMED_VALUE)`   |

The provider normalization seam, not ad hoc renderer logic, owns the absent-versus-invalid
distinction. The supported `op` CLI exposes a flat failure exit and its public documentation does
not promise a stable error taxonomy. The v1 classifier therefore keeps the current narrow item and
field not-found markers fail-closed as invalid mapping. It returns missing only for a narrower token
established by sanitized real-provider evidence for the supported CLI version. Unknown or
version-drifted text is failed/external. Tests match only recorded external tokens, never broad
fragments such as `no such` that can occur in connectivity errors.

## Prompt behavior

| Terminal    | Impact        | Preview behavior                                                   |
| ----------- | ------------- | ------------------------------------------------------------------ |
| unavailable | none or allow | `PreviewBlocked(TTY_UNAVAILABLE)`; never call broker               |
| available   | none          | `PreviewIndeterminate(OPERATOR_IMPACT_LIMITED)`; never call broker |
| available   | allow         | request, validate, discard; return available or failed             |

Actual resolution uses the same ordering. At `ALLOW`, an absent broker is a core protocol failure if
terminal was declared available. At `NONE`, no broker is exposed. Prompt cancellation retains the
existing protected-abort behavior rather than becoming missing or failed.

## Environment behavior

Env-var preview reads the selected environment variable at both impact levels. Unset is
`PreviewMissing`; a valid value is `PreviewAvailable`; a value rejected by the minimum framework
value contract is `PreviewFailed(MALFORMED_VALUE)`. The local string is discarded before return.
Actual resolution returns the corresponding resolution variant.

## Exhaustive source disposition

Every runtime candidate contributes an ordered attempt until success or hard stop. Static
non-candidates contribute only to the separate mapping projection. Core constructs attempts for
not-ready sources, disabled backend plugins, boundary expiry, backend protocol failure, and
unexpected exceptions; a client constructs the remaining attempts.

| Result or condition          | Preview flow                                                         | Actual-resolution flow                                   |
| ---------------------------- | -------------------------------------------------------------------- | -------------------------------------------------------- |
| available / resolved         | stop; earlier indeterminate keeps aggregate indeterminate            | copy value and stop                                      |
| static not applicable        | omit attempt; continue                                               | do not open client; continue                             |
| source not ready             | core blocked attempt; continue                                       | core block; continue                                     |
| backend plugin disabled      | core blocked attempt; continue                                       | core block; continue                                     |
| no runtime candidate         | aggregate blocked/no-candidate; no source attempt                    | existing no-attemptable-source outcome                   |
| missing                      | continue silently                                                    | continue silently                                        |
| indeterminate / impact block | continue; pin aggregate uncertainty                                  | continue, or hold frontier during complete-batch staging |
| blocked / TTY unavailable    | continue; retain exhaustion evidence                                 | continue without broker or stdin access                  |
| failed                       | stop traversal                                                       | stop traversal                                           |
| backend protocol failure     | core failed attempt; stop; discard all partial returned values       | core failure; stop; discard all partial returned values  |
| unexpected exception         | core failed attempt; stop; discard exception text and partial values | core failure; stop; discard text and partial values      |

A hard stop ends later-source traversal for that request. An earlier preview indeterminate remains
load-bearing even if a later available or failed attempt is observed; the later failure still ends
traversal and stays in ordered attempts. Without earlier uncertainty, failure is the aggregate.

Actual-resolution exhaustion retains this evidence priority: first operator-impact block, then first
TTY block, then first ordinary miss, then first not-ready or disabled source, then the existing
no-active-source or no-attemptable-source core outcome. A later resolved value or hard failure takes
precedence over fallthrough evidence.

## Complete-batch authority staging

Complete actual resolution preserves the existing rule that a known failure in one required secret
prevents operator-impacting work for another secret that cannot make the batch complete. Static
`interactive` cannot implement that rule after this rewrite, and non-authoritative preview is not
reused. A caller that authorizes `ALLOW` therefore uses iterative actual-resolution stages:

1. **No-impact closure:** core creates clients with `OperatorImpact.NONE`, no prompt broker, and the
   real terminal fact. Each required secret advances from its current frontier until it resolves
   without classified impact, returns `BackendFailed`, exhausts, or reaches its first
   `BackendBlocked(OPERATOR_IMPACT_LIMITED)`. That impact block becomes a pending frontier; the pass
   does not inspect lower-precedence sources for that secret.
2. **Viability check:** if any required secret failed or exhausted, no `ALLOW` client is created.
   Pending secrets receive the existing core-only `batch-doomed-before-interaction` outcome.
3. **One authority turn:** otherwise core selects the earliest pending source in configured order,
   creates one fresh `ALLOW` client, and batches every request whose frontier is that source. A
   broker is supplied only where terminal capability permits. Resolved and failed results become
   final; missing and execution-blocked results advance just those requests beyond that source.
4. Core returns to no-impact closure for every advanced unresolved frontier, repeats the viability
   check, and permits the next `ALLOW` source turn only while the batch is still completable. The
   loop ends when all required secrets resolve or a known failure dooms the remainder.

No-impact closure calls `resolve`, not `preview`; a value resolved under `NONE` is retained only in
the private resolution batch and is authoritative. A pending source has performed no
operator-impacting provider action, so retrying it with `ALLOW` does not duplicate such work.
Rechecking after every authority turn preserves the before-every-interaction doom guarantee even
when that turn discovers a failure. Caller impact `NONE` uses one ordinary pass where impact blocks
fall through. Partial resolution has no complete-batch guarantee and likewise uses one pass at the
caller's exact impact. Per-source budgets, cleanup, source-first batching, value containment, and
fail-before-mutation apply throughout the loop.

## Aggregate attempt model

Core wraps each runtime result as:

```python
@dataclass(frozen=True, slots=True)
class SourcePreviewAttempt:
    source: str
    identifier: str | None
    result: BackendPreview


@dataclass(frozen=True, slots=True)
class ResolutionPreview:
    name: str
    result: BackendPreview
    source: str | None
    identifier: str | None
    attempts: tuple[SourcePreviewAttempt, ...]
```

Both types are value-free and have field-bounded representations. The aggregate result is computed
as follows:

1. Missing and blocked attempts fall through. Remember the first blocked attempt for exhaustion.
2. The first indeterminate attempt pins aggregate uncertainty; continue for diagnostics and stop on
   a later failed attempt.
3. Available stops traversal. It becomes aggregate available only when no earlier indeterminate
   exists; otherwise the aggregate stays indeterminate and points at that earlier limiting source.
4. Failed stops traversal. It becomes aggregate failed only when no earlier indeterminate exists;
   otherwise the aggregate stays indeterminate and ordered attempts retain the failure.
5. Exhaustion with an earlier indeterminate is indeterminate. Otherwise, it is blocked when a block
   was retained, missing when at least one lookup ran and every attempt ordinarily missed, and
   `blocked/no-candidate` when no candidate lookup ran.

At `ALLOW`, steps involving indeterminate are unreachable for a conforming backend. The aggregate is
therefore a definitive disposition, although blocked and failed remain legal.

## Error and hint projection

Expected backend conditions are results. The synchronous driver uses this exact exception boundary:

- `UserAbort` and `concurrent.futures.CancelledError`, both `Exception` subclasses, are re-raised;
- every `BaseException` that is not an `Exception` propagates naturally, including
  `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, and `asyncio.CancelledError`;
- every other `Exception` becomes core-owned `PreviewFailed(UNEXPECTED)` or
  `BackendFailed(UNEXPECTED)` for every request in that source turn; its text, traceback, and
  partial values do not enter the result.

The source context's `__exit__` receives the original exception information and runs before a
protected exception is re-raised. Cleanup failure is warned through the existing value-free path and
never replaces the primary exception. If `__enter__` fails after partial local acquisition, the
backend must clean that partial acquisition before re-raising. The contract is synchronous; adding
another concurrency runtime or cancellation class requires an explicit contract update.

Backends provide no remediation. Core owns exhaustive reason-to-hint mappings at each operator
surface. Adding a reason requires updating the relevant mapping and machine-output projection. A
hint may include a known command flag; a backend may not.

`SecretClientFailure`, `SecretClientRemediation`, and `SecretClientTimeout` are removed from the
backend contract. Expected client conditions use exact variants. Core derives existing resolution
category and remediation fields from the tag and reason. Boundary-budget expiry is a
core-constructed `failed/deadline-exceeded` result. Existing machine remediation fields remain
derived until a separately reviewed schema change removes them.

## Conformance and leak checks

Contract registration and runtime tests cover:

- exact required class operations and attributes;
- exact impact and terminal types at public boundaries;
- complete, exact-key preview and resolution maps;
- exact result variants and backend-returnable reason subsets;
- maximum-impact rejection of `PreviewIndeterminate`;
- `ALLOW` resolution rejection of `BackendBlocked(OPERATOR_IMPACT_LIMITED)` without a repeated
  frontier;
- no free-form or generic metadata fields and no backend-selected flow instruction;
- no stdout, stderr, native exception text, or sentinel value in result objects and `repr`;
- prompt broker absence under disallowed impact or missing TTY;
- no provider or broker call during construction or context entry;
- bounded provider work and cleanup on success, missing, failure, timeout, and interruption;
- ordinary missing fallthrough and failed hard-stop across source precedence;
- parity between preview and resolve classification for the same fake-provider result.

Tests assert structured fields and behavioral effects, not authored prose wording.

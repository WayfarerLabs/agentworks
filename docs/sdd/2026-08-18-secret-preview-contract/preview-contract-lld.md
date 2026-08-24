# LLD: Secret preview and TTY contracts

- Status: Draft for review
- Date: 2026-08-18
- Amended: 2026-08-21
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


class TtyInteractionAccess(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class IndeterminateReason(StrEnum):
    OPERATOR_IMPACT_LIMITED = "operator-impact-limited"


class BlockReason(StrEnum):
    TTY_UNAVAILABLE = "tty-unavailable"
    TTY_INTERACTION_DISABLED = "tty-interaction-disabled"
    SOURCE_NOT_READY = "source-not-ready"
    BACKEND_PLUGIN_DISABLED = "backend-plugin-disabled"
    NO_ACTIVE_SOURCE = "no-active-source"
    NO_ATTEMPTABLE_SOURCE = "no-attemptable-source"
    BATCH_DOOMED = "batch-doomed-before-interaction"


class FailureReason(StrEnum):
    INVALID_MAPPING = "invalid-mapping"
    LOOKUP_REJECTED = "lookup-rejected"
    AUTHENTICATION = "authentication"
    CONNECTIVITY = "connectivity"
    DEADLINE_EXCEEDED = "deadline-exceeded"
    EXTERNAL = "external"
    MALFORMED_VALUE = "malformed-value"
    BACKEND_PROTOCOL = "backend-protocol"
    UNEXPECTED = "unexpected"
```

`OperatorImpact` is the sole preview-impact policy. `TtyInteractionAccess` says only whether TTY
interaction is usable, physically unavailable, or disabled by global `--non-interactive`. That flag
means "do not use the TTY for interactions, even if one is present"; it does not constrain biometric
or app approval. Both types are exact-validated before dispatch and have no truthiness shortcut or
string coercion.

The reason enums are core-owned and closed. Backend code selects a permitted member but cannot
extend the set. `source-not-ready`, `backend-plugin-disabled`, `no-active-source`,
`no-attemptable-source`, `batch-doomed-before-interaction`, `backend-protocol`, and `unexpected` are
core-constructed only. No-candidate is a separate aggregate preview variant, not a block reason a
backend or source attempt can construct. `operator-impact-limited` exists only as preview
uncertainty; actual resolution has no operator-impact allowance or corresponding reason.

Blocked-reason placement is exact:

| Producer and result position                       | Legal blocked reasons                                    |
| -------------------------------------------------- | -------------------------------------------------------- |
| backend `PreviewBlocked`                           | TTY reasons only when `supports_tty_interaction` is true |
| core-owned preview source attempt                  | `source-not-ready`, `backend-plugin-disabled`            |
| backend `BackendBlocked`                           | TTY reasons only when `supports_tty_interaction` is true |
| core-owned final actual-resolution outcome         | any `BlockReason`, subject to the exhaustion rules below |
| aggregate preview with no candidate lookup attempt | structural `AggregateNoCandidate`; never a `BlockReason` |

`no-active-source`, `no-attemptable-source`, and `batch-doomed-before-interaction` are legal only on
core-owned final actual-resolution outcomes. They are rejected in every backend map and every
preview source attempt.

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

| Variant                | Backend-returnable reasons                                                                          |
| ---------------------- | --------------------------------------------------------------------------------------------------- |
| `PreviewAvailable`     | none                                                                                                |
| `PreviewMissing`       | none                                                                                                |
| `PreviewIndeterminate` | `operator-impact-limited`                                                                           |
| `PreviewBlocked`       | TTY reasons, only from a backend that declares TTY-interaction support                              |
| `PreviewFailed`        | invalid mapping, lookup rejected, authentication, connectivity, deadline, external, malformed value |

Core may construct blocked results for not-ready or disabled candidate sources,
`AggregateNoCandidate` when no lookup ran, and failed results for protocol violations or unexpected
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

This method validates a backend's mapping applicability and proposes the identifier used by tables
and diagnostics. It does not read environment variables, open a provider, inspect auth, or answer
whether a value exists. The exact `False` opt-out remains framework-owned and never reaches the
backend.

Core never trusts the proposal merely because registration accepted the backend. Before constructing
a request, attempt, aggregate, or renderer row, core revalidates the secret name, configured source
name, and returned identifier as exact strings and rejects Unicode control, format, line-separator,
and paragraph-separator characters; a present identifier must also be non-empty. A violation is
`failed/backend-protocol` for that lookup with no unsafe field retained. Constructor and hostile
backend tests pin the same boundary for every later wrapper.

Core constructs `SecretLookupRequest` only for `CANDIDATE` rows. Static non-candidates stay in the
mapping projection but are omitted from the runtime attempt list; they are neither `PreviewMissing`
nor `PreviewBlocked` because no lookup occurred.

Mapping structure that can be rejected from the declaration fails configuration validation before
client creation. A provider-specific mapping that passes local validation but is ambiguously
rejected later becomes `PreviewFailed(LOOKUP_REJECTED)`.

## TTY broker capability

Each backend class declares exact `supports_tty_interaction: bool`. This is a least-authority
capability, not a prediction about whether an operator action will occur. It says only whether core
may give the client an `InteractionBroker`:

```python
class SecretBackend(ABC):
    contract_version: ClassVar[int]
    supports_tty_interaction: ClassVar[bool]
```

- the abstract capability surface provides no default version; every concrete implementation must
  declare exact value `1` itself;
- env-var and OnePassword declare `False`;
- prompt declares `True`;
- registration rejects a missing or non-exact boolean;
- a backend declaring `False` receives no broker and returning either TTY block reason is a protocol
  failure.

A backend declaring `True` may still complete without TTY, use an out-of-band alternative, or return
another exact result. Missing or disabled TTY does not cause a core-created backend result; the
backend must exhaust its own valid routes before returning the matching TTY block.

## Client creation and source-client methods

Core communicates the selected operation and TTY access before any backend-controlled lifecycle
hook. The intent is a small tagged sum rather than an optional or overloaded impact value:

```python
@dataclass(frozen=True, slots=True)
class PreviewIntent:
    impact: OperatorImpact


@dataclass(frozen=True, slots=True)
class ResolutionIntent:
    pass


type SecretClientIntent = PreviewIntent | ResolutionIntent


@classmethod
def create_client(
    cls,
    *,
    config: AgwModel,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> AbstractContextManager[SecretSourceClient]: ...
```

The intent and TTY access are exact-validated before factory invocation. Actual resolution carries
no `OperatorImpact`. The broker is present exactly when `supports_tty_interaction` is true, TTY
access is `AVAILABLE`, and the operation may use it: every actual resolution, or a preview at
`ALLOW`. Factory construction remains resource-free, and context entry may acquire operation-local
resources but must not invoke a provider or prompt; provider work belongs to the selected client
method. Passing intent before both hooks closes the pre-method gap. In-tree and adversarial-fixture
tests prove that forbidden TTY or preview-impact calls produce no provider or broker observation
during construction, context entry, or the selected method.

The old `prepare` and `external_operation_timeout` hooks are removed. The timeout hook is evaluated
before `create_client` in the shipped driver, receives no authority, and duplicates the OnePassword
source config that the provider client already enforces. Keeping either hook would create an extra
pre-method failure surface. The shipped driver has no outer operation deadline, so `remaining_time`
and its always-unbounded budget would be dead extension points; `source_name` is likewise
unnecessary because core already owns source diagnostics and no backend consumes it. Source config
validates any backend timeout, and the client applies one shrinking source deadline at every
external boundary and reports expiry as `failed/deadline-exceeded`.

The rewritten protocol is:

```python
class SecretSourceClient(Protocol):
    def preview(
        self,
        requests: tuple[SecretLookupRequest, ...],
    ) -> Mapping[str, BackendPreview]: ...

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
    ) -> Mapping[str, BackendResolution]: ...
```

Intent, TTY access, and broker are fixed for the lifetime of this fresh client and are not repeated
on either method. Core never reuses a client across intents or preview-impact levels. This makes
disagreement between constructor intent and per-call behavior impossible.

Each method returns exactly one entry for every request it owns. Core validates the whole map before
rendering, aggregating, or copying any resolved value. Missing keys, extra keys, wrong types,
backend use of core-only reasons, `PreviewIndeterminate` under `OperatorImpact.ALLOW`, a preview map
from a resolution client, a resolution map from a preview client, or any preview-only reason in an
actual-resolution result is a backend protocol failure. Expected provider conditions use closed
result variants, not exceptions. The exact exception boundary is defined below.

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
Backend-returned `BackendBlocked` permits only `tty-unavailable` and `tty-interaction-disabled`, and
only from a backend that declares `supports_tty_interaction = True`. Backend-returned
`BackendFailed` permits the same backend failure reasons as preview. Core-only and preview-only
reasons are rejected.

Actual resolution has no indeterminate variant and no operator-impact input. It performs configured
provider actions, including out-of-band biometric or app approval, in every TTY state. The optional
interaction broker is present only when the backend declares TTY support and `tty_access` is
`AVAILABLE`; a backend must not read ambient stdin or infer TTY permission from any other fact.

After source traversal, core projects final actual-resolution blockage without inventing legacy
detail or remediation fields:

| Final condition                                                                             | Final outcome                                       |
| ------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| no active source exists                                                                     | `blocked/no-active-source`                          |
| active sources exist but traversal retains no TTY, missing, not-ready, or disabled evidence | `blocked/no-attemptable-source`                     |
| a complete batch cannot succeed, so another requested name is not attempted                 | `blocked/batch-doomed-before-interaction`           |
| attempted chain exhausts with a TTY block                                                   | `blocked/<first retained TTY block>`                |
| attempted chain has no TTY block and includes an ordinary miss                              | `missing`                                           |
| attempted chain has no TTY block or ordinary miss but has a not-ready/disabled block        | `blocked/<first retained block reason by priority>` |

Within each retained class, "first" means configured source order. Every hard failure is
`failed/<failure reason>`. The consuming core maps these final tags to the established
`SecretUnavailableError` hierarchy and human guidance. Machine projections expose only fields
already frozen by their surface schema; no legacy resolution category, detail, or remediation value
is recreated.

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
5. Classify unambiguous ordinary absence as `PreviewMissing`. Classify invalid local mapping as
   `PreviewFailed(INVALID_MAPPING)`, ambiguous provider rejection as
   `PreviewFailed(LOOKUP_REJECTED)`, and provider, deadline, value, or external failures with their
   closed failure reason. Native text and exception messages do not cross the boundary.

At maximum impact, step 3 is unreachable because there is no higher operator-impact class. The
backend must still return `PreviewBlocked` when an execution capability is absent and
`PreviewFailed` when permitted work fails.

Actual resolution shares the acquisition and provider-classification seam but skips steps 2 and 3:
there is no impact allowance to consult. It performs the bounded action and returns resolved,
missing, or failed. A backend returns a TTY block only when its acquisition requires terminal input
and the supplied TTY access is unavailable or disabled.

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

Actual resolution ignores this preview-only setting and invokes the bounded `op read` for every
candidate request. Global `--non-interactive`, missing TTY, biometric approval, and app approval do
not change that rule.

The setting intentionally does not say `biometric`. 1Password may choose cached access, a biometric,
a device credential, or another app-configured method after invocation; Agentworks cannot reliably
distinguish those paths beforehand.

When `op read` runs, its normalized outcomes map as follows:

| Normalized provider outcome                | Preview result                     | Resolution result                  |
| ------------------------------------------ | ---------------------------------- | ---------------------------------- |
| valid value                                | `PreviewAvailable`                 | `BackendResolved`                  |
| supported, unambiguous absence marker      | `PreviewMissing`                   | `BackendMissing`                   |
| provider rejects reference syntax or shape | `PreviewFailed(LOOKUP_REJECTED)`   | `BackendFailed(LOOKUP_REJECTED)`   |
| ambiguous item/field not-found text        | `PreviewFailed(LOOKUP_REJECTED)`   | `BackendFailed(LOOKUP_REJECTED)`   |
| authentication failure                     | `PreviewFailed(AUTHENTICATION)`    | `BackendFailed(AUTHENTICATION)`    |
| connectivity failure                       | `PreviewFailed(CONNECTIVITY)`      | `BackendFailed(CONNECTIVITY)`      |
| boundary deadline                          | `PreviewFailed(DEADLINE_EXCEEDED)` | `BackendFailed(DEADLINE_EXCEEDED)` |
| other classified provider failure          | `PreviewFailed(EXTERNAL)`          | `BackendFailed(EXTERNAL)`          |
| returned value violates minimum contract   | `PreviewFailed(MALFORMED_VALUE)`   | `BackendFailed(MALFORMED_VALUE)`   |

The provider normalization seam, not ad hoc renderer logic, owns the absent-versus-rejected
distinction. The supported `op` CLI exposes a flat failure exit and its public documentation does
not promise a stable error taxonomy. The v1 classifier therefore keeps the current narrow item and
field not-found markers fail-closed as lookup rejected. Locally malformed `op://` structure remains
invalid mapping. It returns missing only for a narrower token established by sanitized real-provider
evidence for the supported CLI version. Unknown or version-drifted text is failed/external. Tests
match only recorded external tokens, never broad fragments such as `no such` that can occur in
connectivity errors.

This creates two explicit release dispositions. With authorized conclusive evidence, record the
exact supported `op` version and sanitized narrow token, add its regression fixture, and reproduce
missing fallthrough live. Without it, the OnePassword classifier has no missing token: item/field
markers stay lookup-rejected and all unknown text stays external failure. A fake provider can test a
recorded classifier but can never establish the provider fact. Generic missing fallback remains
provable through env-var and controlled contract fixtures.

## Prompt behavior

| TTY access  | Preview impact | Preview behavior                                                   |
| ----------- | -------------- | ------------------------------------------------------------------ |
| unavailable | none or allow  | `PreviewBlocked(TTY_UNAVAILABLE)`; never call broker               |
| disabled    | none or allow  | `PreviewBlocked(TTY_INTERACTION_DISABLED)`; never call broker      |
| available   | none           | `PreviewIndeterminate(OPERATOR_IMPACT_LIMITED)`; never call broker |
| available   | allow          | request, validate, discard; return available or failed             |

Actual prompt resolution uses only TTY access: unavailable and disabled return the matching
`BackendBlocked`, while available requires a broker and requests the value. Prompt cancellation
retains the existing protected-abort behavior rather than becoming missing or failed.

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

| Result or condition                   | Preview flow                                                         | Actual-resolution flow                                  |
| ------------------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------- |
| available / resolved                  | stop available; retain every earlier attempt                         | copy value and stop                                     |
| static not applicable                 | omit attempt; continue                                               | do not open client; continue                            |
| source not ready                      | core blocked attempt; continue                                       | core block; continue                                    |
| backend plugin disabled               | core blocked attempt; continue                                       | core block; continue                                    |
| no active source                      | aggregate blocked/no-candidate; no source attempt                    | blocked/no-active-source                                |
| no attemptable source                 | aggregate blocked/no-candidate; no source attempt                    | blocked/no-attemptable-source                           |
| complete batch already cannot succeed | not applicable                                                       | stop before another provider turn; core batch-doomed    |
| missing                               | continue silently                                                    | continue silently                                       |
| indeterminate                         | continue; retain precedence evidence                                 | impossible                                              |
| blocked / TTY unavailable or disabled | continue; retain exhaustion evidence                                 | continue without broker or stdin access                 |
| failed                                | stop traversal                                                       | stop traversal                                          |
| backend protocol failure              | core failed attempt; stop; discard all partial returned values       | core failure; stop; discard all partial returned values |
| unexpected exception                  | core failed attempt; stop; discard exception text and partial values | core failure; stop; discard text and partial values     |

A hard stop ends later-source traversal for that request. A later available or failed attempt is the
current-impact aggregate even when an earlier attempt was indeterminate. The earlier attempt remains
ordered evidence that a higher-impact authoritative pass could avoid reaching that later source; it
never hides the success or failure from current-impact diagnostics.

Preview exhaustion retains this evidence priority: first indeterminate, then first TTY block, then
first ordinary miss, then first not-ready or disabled-source block, then aggregate no-candidate.
Actual-resolution exhaustion retains this evidence priority: first TTY block, then first ordinary
miss, then first not-ready or disabled source, then the exact no-active-source or
no-attemptable-source core outcome. A later resolved value or hard failure takes precedence over
fallthrough evidence.

## Actual-resolution pass

Actual resolution is one source-first pass. Core opens each ready source once with
`ResolutionIntent`, sends the unresolved candidate batch, validates the complete returned map, and
closes the source before continuing. It never performs a zero-impact resolution pass, promotes a
preview result, retries at broader authority, or serializes provider work into one-request impact
turns. TTY blocks fall through exactly like other execution blocks. Complete resolution still
finishes before the consuming operation's first external mutation.

Complete operation resolution also preserves the pre-existing fail-before-interaction invariant
without recreating an operator-impact policy. Before each later provider source turn, core uses only
static lookup description, folded readiness, and disabled-plugin state to ask whether every
still-unresolved requested name has a viable remaining candidate. TTY access is not a core viability
input: core passes it to the backend, and the backend alone decides whether that state is limiting.
A hard-failed name also makes the complete batch terminal. When the batch cannot succeed, core opens
no later client, marks other unresolved names `blocked/batch-doomed-before-interaction`, and returns
the complete value-free outcome set. The explicit partial-reveal path continues resolving
independent names. This is a core completion-scope rule; no completion flag, impact classification,
TTY prediction, or doom prediction crosses the backend factory boundary.

## Aggregate attempt model

Core wraps each runtime result as:

```python
@dataclass(frozen=True, slots=True)
class SourcePreviewAttempt:
    source: str
    identifier: str | None
    result: BackendPreview


@dataclass(frozen=True, slots=True)
class AggregateNoCandidate:
    pass


type AggregatePreview = BackendPreview | AggregateNoCandidate


@dataclass(frozen=True, slots=True)
class ResolutionPreview:
    name: str
    result: AggregatePreview
    source: str | None
    identifier: str | None
    attempts: tuple[SourcePreviewAttempt, ...]
```

All types are value-free and have field-bounded representations. `AggregateNoCandidate` renders as
wire status `blocked` and reason `no-candidate`; it is legal only with no source, identifier, or
attempts. This makes the aggregate-only truth structural instead of a prose restriction on a block
enum. The aggregate result is computed as follows:

1. Missing and blocked attempts fall through. Retain their ordered category evidence for exhaustion.
2. Indeterminate falls through and remains in ordered evidence.
3. Available stops traversal and becomes aggregate available, while retaining earlier attempts.
4. Failed stops traversal and becomes aggregate failed, while retaining earlier attempts.
5. Exhaustion selects the first indeterminate, then the first TTY block, then the first ordinary
   missing result, then the first readiness or disabled-plugin block. It uses `AggregateNoCandidate`
   only when no candidate lookup ran.

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

`SecretClientFailure`, `SecretClientRemediation`, `SecretClientTimeout`, `TimeoutGuidance`,
`ResolutionCategory`, `ResolutionDetail`, `ResolutionRemediation`, and their translation table are
removed. Expected client conditions use exact variants. The value-free final resolution outcome uses
`resolved`, `missing`, `blocked(reason)`, or `failed(reason)` directly, with core-only block reasons
for exhausted source state. Core maps that one tagged vocabulary to the established exception
hierarchy and derives human guidance at its consuming surface.

OnePassword plus `failed/deadline-exceeded` retains the existing core-authored guidance that a
pending desktop-app approval is a common cause and `op whoami` is not a reliable exclusion test. The
backend no longer selects even a guidance identifier. Existing machine fields remain derived only
where a frozen schema requires them; no runtime legacy-detail enum remains.

## Conformance and leak checks

Contract registration and runtime tests cover:

- exact required class operations and attributes;
- exact `supports_tty_interaction` boolean and broker delivery derived from it;
- exact preview impact, operation intent, and TTY access types at public boundaries;
- complete, exact-key preview and resolution maps;
- exact result variants and backend-returnable reason subsets;
- rejection of core-only final block reasons in backend maps and preview attempts, plus exact
  no-active-source and no-attemptable-source final outcomes;
- rejection of TTY blocks from env-var, OnePassword, or any backend that declares no TTY support;
- maximum-impact rejection of `PreviewIndeterminate`;
- rejection of preview-only reasons and impact inputs from actual resolution;
- no free-form or generic metadata fields and no backend-selected flow instruction;
- no stdout, stderr, native exception text, or sentinel value in result objects and `repr`;
- broker absence for backends without TTY support, non-disruptive preview, unavailable TTY, or
  global `--non-interactive`;
- no provider or broker call during construction or context entry;
- removal of `prepare`, `external_operation_timeout`, backend failure/remediation exceptions, and
  the legacy runtime resolution-detail vocabulary;
- bounded provider work and cleanup on success, missing, failure, timeout, and interruption;
- ordinary missing fallthrough and failed hard-stop across source precedence;
- mixed preview and actual-resolution exhaustion in which indeterminate is preview-only, TTY blocks
  outrank ordinary missing, and ordinary missing outranks not-ready and disabled-source blocks;
- complete-batch doom after a hard failure or exhausted static viability, proving no later client,
  provider, broker, or prompt call, plus partial reveal continuing independent resolution;
- no-active-source and no-attemptable-source only as the final fallbacks after all retained
  per-source exhaustion evidence;
- final blocked-reason projection through the existing exception hierarchy and frozen machine
  schemas without recreating legacy resolution detail or remediation fields;
- OnePassword actual resolution under available, unavailable, and disabled TTY access, proving
  global `--non-interactive` never suppresses out-of-band provider work;
- parity between preview and resolve classification for the same fake-provider result.

Tests assert structured fields and behavioral effects, not authored prose wording.

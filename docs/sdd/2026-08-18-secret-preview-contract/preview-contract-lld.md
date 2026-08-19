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

The enum is core-owned. Backend code selects a member but cannot extend the set. Core-only source
readiness and plugin-disabled rows use the same vocabulary when building aggregate attempts.

## Result rules

```python
@dataclass(frozen=True, slots=True)
class BackendPreview:
    answer: PreviewAnswer
    detail: PreviewDetail

    def __repr__(self) -> str:
        return f"BackendPreview(answer={self.answer.value!r}, detail={self.detail.value!r})"
```

The constructor enforces these combinations:

| Answer | Allowed details                       |
| ------ | ------------------------------------- |
| yes    | `available`                           |
| maybe  | `operator-impact-limited`             |
| no     | every other backend-returnable detail |

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

## Source-client methods

The rewritten protocol is:

```python
class SecretSourceClient(Protocol):
    def prepare(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        impact: OperatorImpact,
        terminal: TerminalAvailability,
        remaining_time: RemainingTime,
    ) -> None: ...

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
        remaining_time: RemainingTime,
    ) -> Mapping[str, str]: ...
```

`prepare` remains read-only authenticated setup. It receives the same exact impact and terminal fact
as the operation that follows, so it cannot trigger authentication before learning the allowance.
Core calls it once immediately before the selected operation. A client must not retain a broader
authority from an earlier call.

For resolution, `terminal` is still an execution fact. The optional interaction broker is an
authority-bearing capability supplied only when impact and terminal permit it. A backend must not
infer permission from terminal availability.

Each method returns exactly one entry for each request it owns. Missing keys, extra keys, wrong
types, and `maybe` under `OperatorImpact.ALLOW` are backend protocol failures. Core validates the
whole map before rendering or making an aggregate decision.

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

Actual resolution applies the same classification. Under insufficient impact it returns the existing
refused-resolution category through a new closed client block, without invoking `op`.

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

1. `not-applicable`, `soft-miss`, and not-ready rows fall through.
2. A hard `no` stops the known path unless an earlier `maybe` already made that path uncertain.
3. A `yes` becomes aggregate `yes` only when no earlier `maybe` exists.
4. Once an earlier `maybe` exists, later attempts may improve diagnostics but cannot erase the
   aggregate `maybe`.
5. Exhaustion with no success or uncertainty is aggregate `no`.

At `ALLOW`, rule 4 is unreachable and the result is definitive.

## Error and hint projection

Expected backend conditions are results. Exceptions are reserved for programming failures, contract
violations, and process interruption. Core discards unexpected exception text before constructing
`unexpected`.

Backend results have no remediation. Core owns an exhaustive `PreviewDetail` to hint mapping at each
operator surface. Adding a detail requires updating that mapping and the machine-output projection.
A hint may include a known command flag; a backend may not.

Existing `SecretClientFailure` is simplified to carry only its closed failure kind. The redundant
`SecretClientRemediation` parameter is removed. Existing resolution remediation fields can be
derived from the detail until a separately reviewed machine-schema change removes them.

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
- bounded provider work and cleanup on success, failure, timeout, and interruption;
- parity between preview and resolve classification for the same fake-provider result.

Tests assert structured fields and behavioral effects, not authored prose wording.

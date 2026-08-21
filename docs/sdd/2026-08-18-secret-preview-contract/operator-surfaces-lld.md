# LLD: Secret preview callers and operator surfaces

- Status: Draft for review
- Date: 2026-08-18
- Amended: 2026-08-21
- Governing design: [HLA](./hla.md)

## Orthogonal policy derivation

Global `--non-interactive` has one meaning throughout the CLI: **do not use the TTY for
interactions, even if one is present**. It neither requests unattended provider behavior nor forbids
biometric, app, browser, device, or other out-of-band operator action. It does not suppress color or
otherwise change presentation; output stream capability and the existing presentation controls own
that decision.

The broad `InteractionPolicy` becomes `TtyInteractionPolicy`. CLI roots derive `REFUSE` only from
global `--non-interactive`; otherwise they derive `ALLOW`. Resolution composition combines that
policy with `sys.stdin.isatty()` into exact `TtyInteractionAccess`:

```python
if tty_policy is TtyInteractionPolicy.REFUSE:
    tty_access = TtyInteractionAccess.DISABLED
elif terminal_input_is_usable():
    tty_access = TtyInteractionAccess.AVAILABLE
else:
    tty_access = TtyInteractionAccess.UNAVAILABLE
```

Preview impact is selected separately by each preview caller: `NONE` for non-disruptive inspection
and `ALLOW` for an explicit opt-in. Global `--non-interactive` never changes preview impact. Service
callers provide exact TTY policy or access explicitly; no backend reads ambient TTY state for
itself. Core supplies a broker only to a backend whose exact `supports_tty_interaction` capability
is true.

## Preflight

The preflight chain remains:

```text
preflight_all
  -> require_predicted_refs
    -> predict_resolution
      -> preview_batch
```

Its semantics change as follows:

- `predict_resolution` has fixed preview impact `NONE`; it does not accept a certainty argument.
- It receives exact TTY interaction access from the command execution context.
- `require_predicted_refs` accepts aggregate `available` and `indeterminate`.
- It rejects aggregate `missing` or `blocked` with the closed tag and reason, when any.
- It rejects aggregate `failed` unless its ordered attempts contain an earlier higher-precedence
  `indeterminate` attempt. In that one case broader impact could resolve before the failed source,
  so the zero-impact result has not disproven the operation.
- `indeterminate` does not claim resolution will work. The operation's later resolution pass remains
  mandatory and completes before the first consuming mutation.

`preflight_all` owns one lazy command-scoped preview memo. Its key is the secret name because active
sources, validated config, fixed `NONE` impact, and TTY interaction access are immutable for that
command. Immediately before each node's preflight, core batches only that node's as-yet unseen
secret names, stores the resulting value-free previews, then evaluates the node's references in
their existing order. A repeated secret reuses its stored preview and cannot cause a second provider
read, audit event, or timeout budget. Lazy population preserves the current node failure order and
does not probe secrets belonging only to nodes that will never be reached. The later authoritative
resolution never reuses preview data or a discarded value.

This preserves preflight's current responsibility for missing mappings and unavailable chains
without forcing operator interaction just to predict a command. A configured-source failure is
always aggregate failed, so describe, verify, and doctor report the current-impact failure.
Preflight alone checks for an earlier indeterminate attempt because it is an impossibility screen
for a later authoritative operation, not a current-health renderer.

## Actual operation resolution

The operation-scoped resolver receives no `OperatorImpact`. Core no longer skips a whole source
because of a static backend `interactive` flag.

- Out-of-band providers run regardless of TTY availability or global `--non-interactive`.
- Env-var and OnePassword declare no TTY-interaction support, receive no broker, and cannot return a
  TTY block.
- Prompt declares TTY-interaction support and receives a broker only when TTY interaction access is
  `AVAILABLE`.
- Prompt returns `BackendBlocked(TTY_UNAVAILABLE)` or `BackendBlocked(TTY_INTERACTION_DISABLED)` for
  the other two access states.
- `BackendMissing` and `BackendBlocked` fall through to later sources.
- `BackendFailed` hard-stops the current secret's source chain; core does not warn and continue.
- Static non-candidates, source readiness, and disabled plugins retain their core-owned fallback
  behavior.
- A complete batch that is already terminal stops before another provider turn and gives every
  skipped unresolved name the core-only `blocked/batch-doomed-before-interaction` result. The
  explicit partial-reveal path continues resolving independent names.

This contract does not add a generic outage-fallback mode. If one is later justified, it must be an
explicit core/source policy with separately reviewed precedence and warning semantics. A backend
does not decide whether its own failure may be ignored.

Complete resolution remains fail-before-mutation and becomes one source-first pass. Each ready
source receives its unresolved candidate batch once. There is no zero-impact resolution phase,
authority frontier, one-request interaction staging, or preview reuse.

Batch doom is a completion invariant, not an operator-impact gate: core derives it from hard results
and static remaining viability, never asks a backend whether an action might affect the operator,
and does not pass completion policy into a backend client.

## `agw secret describe`

Command shape:

```text
agw secret describe NAME [--allow-interaction] [--output human|json]
```

Default behavior requests `OperatorImpact.NONE`. It may read and discard values through backends
that know the read is non-disruptive, and it may return `indeterminate`. It does not equate
read-only output with a no-I/O guarantee.

`--allow-interaction` requests `OperatorImpact.ALLOW`. It may be combined with global
`--non-interactive`: out-of-band work such as 1Password app approval is permitted, while prompt
remains `blocked/tty-interaction-disabled`. The resulting preview contains no `indeterminate`; with
TTY access available, prompt may request and discard a value. Maximum impact still allows blocked
and failed results.

The existing static `Backend mappings` section remains no-I/O and is driven by structured
`describe_lookup`. The `Resolution preview` section renders the aggregate and ordered runtime
attempts. Human output distinguishes at least:

- available with the source that establishes presence;
- indeterminate with the first limiting source and a core-authored opt-in hint;
- blocked due to unavailable TTY or source execution state;
- missing after one or more candidate lookups ordinarily miss;
- blocked/no-candidate when no runtime lookup was applicable;
- failed with a closed mapping, provider, deadline, value, protocol, or unexpected reason.

The CLI help explicitly says a default preview may perform non-disruptive provider work and that the
opt-in may prompt or authenticate.

## `agw secret verify`

Verification becomes a preview consumer rather than a value-bearing core-resolution consumer. This
is the operator-facing proof that the backend can establish current availability without sending a
value to core.

Command shape stays:

```text
agw secret verify NAME... [--allow-interaction]
```

- Default impact is `NONE`.
- Only aggregate `available` is a success row; every other status causes exit 1.
- `--allow-interaction` selects `ALLOW` and eliminates `indeterminate`; blocked and failed remain
  possible. It is orthogonal to global `--non-interactive`, which disables prompt only.
- All unique names are previewed in first-written order through bounded source batches.
- An available row proves current backend presence under the requested impact, not suitability for
  every consumer-specific value grammar.

The table uses status and reason while core may retain a derived hint column for human usability.
Tests pin fields, ordering, exit behavior, and absence of values, not exact hint sentences.

## `agw secret list`

List remains a static declaration and mapping surface. It never invokes
`SecretSourceClient.preview`. Cells are built from readiness plus `LookupDescription`:

- candidate with identifier: the identifier;
- candidate without identifier: `candidate`;
- not applicable: `won't attempt`;
- not ready: the existing readiness presentation.

This is intentionally not a claim that the value exists. Help and permanent guide text use
`candidate`, not `would resolve`.

## Doctor

Doctor requests `OperatorImpact.NONE` for its per-secret preview and maps the aggregate result to
the existing `Status` enum exactly:

| Aggregate preview | Doctor status | Exit effect                                           |
| ----------------- | ------------- | ----------------------------------------------------- |
| available         | `OK`          | no failure                                            |
| missing           | `WARN`        | no failure                                            |
| indeterminate     | `WARN`        | no failure                                            |
| blocked           | `WARN`        | no failure                                            |
| failed            | `FAIL`        | increments failure count and makes doctor exit code 1 |

An earlier-indeterminate/later-failed chain has aggregate failed, so it is `FAIL` and makes doctor
exit 1; the ordered attempts retain both facts. An earlier-indeterminate/later-available chain is
`OK` and likewise retains the uncertainty as attempt evidence rather than masking current
availability.

Doctor exposes no disruptive opt-in because a broad health sweep is the wrong surface for
authorizing an unknown number of operator actions.

Because non-disruptive preview may perform provider work, doctor documentation no longer promises
that secret preview is pure or never resolves internally. It promises no operator impact under the
configured classification and no returned value.

## Human diagnostics

Core derives guidance from the tagged result, closed reason, and caller context. No backend supplies
remediation or text. Important projections include:

| Status / reason                         | Human meaning                                                        |
| --------------------------------------- | -------------------------------------------------------------------- |
| `missing`                               | a valid lookup found no value; later sources were considered         |
| `indeterminate/operator-impact-limited` | the backend cannot answer further under the current impact allowance |
| `blocked/tty-unavailable`               | this source needs terminal input, which this process does not have   |
| `blocked/tty-interaction-disabled`      | global `--non-interactive` disabled terminal interaction             |
| `blocked/no-candidate`                  | no active source has an applicable runtime lookup                    |
| `failed/invalid-mapping`                | the configured provider reference is invalid                         |
| `failed/lookup-rejected`                | the provider rejected the lookup without proving ordinary absence    |
| `failed/authentication`                 | the permitted provider attempt could not authenticate                |
| `failed/deadline-exceeded`              | bounded provider work did not complete                               |

Hints name only controls available on that command. `secret describe` and `secret verify` may name
`--allow-interaction`; a prompt blocked by global mode names `--non-interactive` as the TTY-only
cause and never implies that out-of-band interaction was forbidden.

Failure prose is core-authored from the closed reason. Provider stderr, native exception messages,
secret references that are not already approved safe identifiers, and arbitrary backend context are
never rendered.

## JSON contract

`secret describe --output json` keeps envelope schema version 1, so every existing field retains its
meaning and type. `source_mappings[].would_attempt` remains as a compatibility projection and is
derived from `LookupDisposition.CANDIDATE`; it does not keep the removed backend method alive. The
existing `resolution.category`, `source`, `identifier`, and `skipped_not_ready` fields remain the
same static readiness-and-applicability projection.

A new optional `preview` member carries the provider-aware result. It uses `status`, not the legacy
yes/no-shaped `answer`, and includes `reason` only for indeterminate, blocked, or failed variants:

```json
{
  "resolution": {
    "category": "attemptable",
    "source": "personal-op",
    "identifier": "op://vault/item/field",
    "skipped_not_ready": [],
    "preview": {
      "status": "indeterminate",
      "source": "personal-op",
      "identifier": "op://vault/item/field",
      "reason": "operator-impact-limited",
      "attempts": [
        {
          "source": "personal-op",
          "identifier": "op://vault/item/field",
          "status": "indeterminate",
          "reason": "operator-impact-limited"
        }
      ]
    }
  }
}
```

The exact nested rules are:

- `status` is required and one of `available`, `missing`, `indeterminate`, `blocked`, or `failed`;
- `reason` is absent for available and missing, and required for the other statuses, including
  aggregate `blocked/no-candidate`;
- aggregate `source` and `identifier` identify the selected or limiting attempt and may be null;
- attempts retain active-source order and omit static non-candidates;
- no value, backend message, native error, remediation, halt flag, or generic metadata is added.

`secret list --output json` also keeps envelope schema version 1 and its documented
`secrets[].sources[].would_attempt` boolean. That field is derived from
`LookupDisposition.CANDIDATE`, just like describe's compatibility projection; removing the backend
method does not remove or redefine either frozen field. List adds no runtime preview data.

Secret checks in `agw doctor --output json` preserve the existing check fields `name`, `status`,
`message`, and `hint`, and may add this optional closed member:

```json
{
  "name": "Secret 'deploy-token'",
  "status": "fail",
  "message": "...",
  "hint": null,
  "secret_preview": {
    "status": "failed",
    "source": "personal-op",
    "identifier": "op://vault/item/field",
    "reason": "lookup-rejected",
    "attempts": [
      {
        "source": "personal-op",
        "identifier": "op://vault/item/field",
        "status": "failed",
        "reason": "lookup-rejected"
      }
    ]
  }
}
```

`secret_preview` follows the same status, conditional reason, safe identity, attempt ordering, and
value-exclusion rules as describe's nested preview. It is present only on secret-preview checks and
absent on every other doctor check. Doctor tests distinguish every preview status through these
fields rather than authored `message` text, and pin counts plus exit status.

The command reference is the permanent machine-output authority for these legacy projections, new
tagged unions, and exact null or absence rules. Consumers that know only JSON v1 continue to read
the old fields unchanged and ignore optional members. The additions therefore do not redefine JSON
v1.

## Completion and command grammar

- Add `--allow-interaction` to `secret describe` completion/introspection surfaces.
- Keep `secret verify --allow-interaction` unchanged.
- Do not add this flag to ordinary resolving commands; actual resolution has no impact policy.
- Permit `--allow-interaction` with global `--non-interactive`; the former controls preview impact,
  while the latter disables TTY interaction only.

## Permanent collateral

Implementation updates, in the same PR:

- `cli/agentworks/capabilities/secret_backend/README.md` for the rewritten contract;
- `cli/agentworks/plugins/README.md` for general plugin conformance and the rewritten method set;
- `cli/agentworks/secrets/README.md` for impact, preview, failure, and resolution behavior;
- `cli/README.md`, `docs/guides/resources.md`, and relevant guide topics;
- `cli/agentworks/secrets/guide-content/secrets.md`, including its consent paragraph, for both
  impact-bearing describe and verify;
- `docs/adrs/0013-cli-side-secret-injection.md` for the ordinary configured-source workflow that
  replaces its `op run`/env-var workaround teaching;
- `cli/command-reference.md`, the permanent machine-output reference, including both JSON v1
  `would_attempt` compatibility projections and optional tagged objects;
- sample source config for OnePassword impact classification;
- generated shell completions and schema snapshots;
- any module docstrings that still claim preview is pure, a negative status always falls through, or
  TTY grants interaction authority;
- global flag help, output helpers, and CLI documentation that currently make `--non-interactive`
  alter color or presentation.

No permanent file links to this SDD.

## Test matrix

The behavior suite crosses these axes:

- caller: preflight, describe, verify, doctor, ordinary resolution;
- preview impact: none, allow;
- TTY access: available, unavailable, disabled;
- backend: env-var, prompt, OnePassword fake provider;
- provider state: value, valid missing target, invalid mapping, auth failure, connectivity failure,
  timeout, malformed value, other provider failure;
- OnePassword preview classification: known unattended, default app action, configured no-impact app
  authentication; actual resolution ignores that classification;
- source order: earlier indeterminate then later available or failed, ordinary missing then later
  available, blocked then later available, failed then otherwise-available fallback, all missing, no
  candidate;
- output: human fields, describe and doctor JSON tagged structures, both legacy JSON v1
  `would_attempt` fields, exit status, exception category, doctor status/count/exit mapping,
  sentinel leak scan, and color/presentation parity with and without global `--non-interactive` on
  the same output stream.

Tests assert structured behavior. They do not pin prose authored by Agentworks.

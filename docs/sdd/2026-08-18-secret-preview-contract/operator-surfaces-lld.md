# LLD: Secret preview callers and operator surfaces

- Status: Draft for review
- Date: 2026-08-18
- Amended: 2026-08-19
- Governing design: [HLA](./hla.md)

## Policy derivation

The CLI helper becomes `ordinary_operator_impact()` and returns:

```python
OperatorImpact.NONE if output.non_interactive() else OperatorImpact.ALLOW
```

It does not call `output.is_interactive()` or any `isatty()` method. The global `--non-interactive`
flag remains the explicit unattended/fail-fast request. Lack of a TTY is handled later as an
execution fact by backends that need it.

A separate root helper derives terminal availability from `sys.stdin.isatty()` without consulting
global `--non-interactive`. The CLI threads that exact `TerminalAvailability` fact only to preview
and resolution composition boundaries. Service callers that do not originate at the CLI provide the
fact explicitly; no backend reads ambient TTY state for itself.

Published service methods continue to require an explicit, exact policy with no default. The
existing audit rule for constructor totality and early boundary validation moves from
`InteractionPolicy` to `OperatorImpact`.

## Preflight

The preflight chain remains:

```text
preflight_all
  -> require_predicted_refs
    -> predict_resolution
      -> preview_resolution
```

Its semantics change as follows:

- `predict_resolution` has fixed preview impact `NONE`; it does not accept a certainty argument.
- It receives terminal availability from the command execution context as a fact.
- `require_predicted_refs` accepts aggregate `available` and `indeterminate`.
- It rejects aggregate `missing`, `blocked`, or `failed` with the closed tag and reason, when any.
- `indeterminate` does not claim resolution will work. The operation's later resolution pass remains
  mandatory and completes before the first consuming mutation.

`preflight_all` owns one lazy command-scoped preview memo. Its key is the secret name because active
sources, validated config, fixed `NONE` impact, and terminal availability are immutable for that
command. Immediately before each node's preflight, core batches only that node's as-yet unseen
secret names, stores the resulting value-free previews, then evaluates the node's references in
their existing order. A repeated secret reuses its stored preview and cannot cause a second provider
read, audit event, or timeout budget. Lazy population preserves the current node failure order and
does not probe secrets belonging only to nodes that will never be reached. The later authoritative
resolution never reuses preview data or a discarded value.

This preserves preflight's current responsibility for missing mappings and unavailable chains
without forcing operator interaction just to predict a command. A configured-source failure becomes
aggregate failed and fails preflight when no earlier indeterminate source could win. When an earlier
source is indeterminate, a later failure stops traversal and remains in ordered attempts, but the
aggregate stays indeterminate and passes this impossibility screen. Implementation checks the
aggregate tag; it does not scan attempts again to impose different preflight flow.

## Actual operation resolution

The operation-scoped resolver receives the exact `OperatorImpact` chosen at the CLI or service root.
It passes that impact into each source client. Core no longer skips a whole source because of a
static backend `interactive` flag.

- `ALLOW` permits out-of-band provider approval even with no TTY.
- `NONE` prevents a backend from starting an action it classifies as operator impact.
- Prompt receives a broker only when `ALLOW` and terminal availability are both true.
- `BackendMissing` and `BackendBlocked` fall through to later sources.
- `BackendFailed` hard-stops the current secret's source chain; core does not warn and continue.
- Static non-candidates, source readiness, and disabled plugins retain their core-owned fallback
  behavior.

This contract does not add a generic outage-fallback mode. If one is later justified, it must be an
explicit core/source policy with separately reviewed precedence and warning semantics. A backend
does not decide whether its own failure may be ignored.

Complete resolution remains fail-before-mutation. The no-interaction doom check also remains: a
caller that authorizes `ALLOW` first drives actual resolution at `NONE` until each secret resolves,
fails, exhausts, or stops at its first impact block. Core then permits one source-batched `ALLOW`
turn, advances its fallthrough frontiers at `NONE`, and repeats the viability check before every
later `ALLOW` turn. This is authoritative actual resolution, not preview reuse, and a failure
learned from one authorized turn prevents operator work at every still-pending source.

## `agw secret describe`

Command shape:

```text
agw secret describe NAME [--allow-interaction] [--output human|json]
```

Default behavior requests `OperatorImpact.NONE`. It may read and discard values through backends
that know the read is non-disruptive, and it may return `indeterminate`. It does not equate
read-only output with a no-I/O guarantee.

`--allow-interaction` requests `OperatorImpact.ALLOW`. It is rejected with global
`--non-interactive`, matching `secret verify`. The resulting preview contains no `indeterminate`; a
prompt backend may request and discard a value when that is the only permitted probe. Maximum impact
still allows blocked and failed results.

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
- `--allow-interaction` selects `ALLOW`, conflicts with global `--non-interactive`, and eliminates
  `indeterminate`; blocked and failed remain possible.
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

An earlier-indeterminate/later-failed chain has aggregate indeterminate, so it is `WARN` and does
not make doctor exit 1; its diagnostic still includes the retained later failed attempt and closed
reason. Doctor does not scan attempts again to override aggregate flow.

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
| `blocked/no-candidate`                  | no active source has an applicable runtime lookup                    |
| `failed/invalid-mapping`                | the configured provider reference is invalid                         |
| `failed/authentication`                 | the permitted provider attempt could not authenticate                |
| `failed/deadline-exceeded`              | bounded provider work did not complete                               |

Hints name only controls available on that command. `secret describe` and `secret verify` may name
`--allow-interaction`; an ordinary command blocked by global mode names `--non-interactive` as the
cause instead of suggesting a nonexistent local flag.

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

The machine-output reference documents the legacy projection, new tagged union, and exact null or
absence rules. Consumers that know only JSON v1 continue to read the old fields unchanged and ignore
the optional member. The optional addition therefore does not redefine JSON v1.

## Completion and command grammar

- Add `--allow-interaction` to `secret describe` completion/introspection surfaces.
- Keep `secret verify --allow-interaction` unchanged.
- Do not add this flag to ordinary resolving commands; global `--non-interactive` is their explicit
  impact control.
- Keep `--allow-interaction` incompatible with global `--non-interactive` at the command boundary.

## Permanent collateral

Implementation updates, in the same PR:

- `cli/agentworks/capabilities/secret_backend/README.md` for the rewritten contract;
- `cli/agentworks/plugins/README.md` for general plugin conformance and the rewritten method set;
- `cli/agentworks/secrets/README.md` for impact, preview, failure, and resolution behavior;
- `cli/README.md`, `docs/guides/resources.md`, and relevant guide topics;
- CLI command reference and machine-output reference;
- sample source config for OnePassword impact classification;
- generated shell completions and schema snapshots;
- any module docstrings that still claim preview is pure, a negative status always falls through, or
  TTY grants interaction authority.

No permanent file links to this SDD.

## Test matrix

The behavior suite crosses these axes:

- caller: preflight, describe, verify, doctor, ordinary resolution;
- impact: none, allow;
- terminal: present, absent;
- backend: env-var, prompt, OnePassword fake provider;
- provider state: value, valid missing target, invalid mapping, auth failure, connectivity failure,
  timeout, malformed value, other provider failure;
- OnePassword auth classification: known unattended, default app action, configured no-impact app
  authentication;
- source order: earlier indeterminate then later available or failed, ordinary missing then later
  available, blocked then later available, failed then otherwise-available fallback, all missing, no
  candidate;
- output: human fields, JSON tagged structure, legacy JSON v1 fields, exit status, exception
  category, doctor status/count/exit mapping, sentinel leak scan.

Tests assert structured behavior. They do not pin prose authored by Agentworks.

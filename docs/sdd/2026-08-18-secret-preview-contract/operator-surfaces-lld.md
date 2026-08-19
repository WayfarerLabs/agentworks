# LLD: Secret preview callers and operator surfaces

- Status: Draft for review
- Date: 2026-08-18
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
- `require_predicted_refs` rejects aggregate `no` and accepts `yes` or `maybe`.
- The preflight error uses the closed aggregate detail and points to `secret describe`.
- A `maybe` does not claim resolution will work. The operation's later resolution pass remains
  mandatory and completes before the first consuming mutation.

This preserves preflight's current responsibility for missing mappings and definitively unavailable
chains without forcing operator interaction just to predict a command.

## Actual operation resolution

The operation-scoped resolver receives the exact `OperatorImpact` chosen at the CLI or service root.
It passes that impact into each source client. Core no longer skips a whole source because of a
static backend `interactive` flag.

- `ALLOW` permits out-of-band provider approval even with no TTY.
- `NONE` prevents a backend from starting an action it classifies as operator impact.
- Prompt receives a broker only when `ALLOW` and terminal availability are both true.
- A backend refusal or missing TTY becomes a typed value-free resolution outcome and may fall
  through according to existing source semantics.

Complete resolution remains fail-before-mutation. The no-interaction doom check also remains: a
known failure in another required secret prevents authorized operator-impact work that cannot make
the batch complete.

## `agw secret describe`

Command shape:

```text
agw secret describe NAME [--allow-interaction] [--output human|json]
```

Default behavior requests `OperatorImpact.NONE`. It may read and discard values through backends
that know the read is non-disruptive, and it may return `maybe`. It does not equate read-only output
with a no-I/O guarantee.

`--allow-interaction` requests `OperatorImpact.ALLOW`. It is rejected with global
`--non-interactive`, matching `secret verify`. The resulting preview contains no `maybe`; a prompt
backend may request and discard a value when that is the only definitive probe.

The existing static `Backend mappings` section remains no-I/O and is driven by structured
`describe_lookup`. The `Resolution preview` section renders the aggregate and ordered source
attempts. Human output distinguishes at least:

- `yes` with the source that establishes resolution;
- `maybe` with the first limiting source and a core-authored opt-in hint;
- `no/tty-unavailable` with the relevant prompt source;
- provider failure and timeout details;
- exhausted mappings or soft misses.

The CLI help explicitly says a default preview may perform non-disruptive provider work and that the
opt-in may prompt or authenticate.

## `agw secret verify`

Verification becomes a definitive-preview consumer rather than a value-bearing core-resolution
consumer. This is the operator-facing proof that the backend can answer without sending a value to
core.

Command shape stays:

```text
agw secret verify NAME... [--allow-interaction]
```

- Default impact is `NONE`.
- `maybe` is rendered as a non-success row and causes exit 1.
- `--allow-interaction` selects `ALLOW`, conflicts with global `--non-interactive`, and produces
  only definitive rows unless core reports a backend protocol error.
- All unique names are previewed in first-written order through bounded source batches.
- A `yes` row proves current backend resolution under the requested impact, not suitability for
  every consumer-specific value grammar.

The table replaces category/remediation coupling with answer and detail while core may retain a
derived hint column for human usability. Tests pin fields, ordering, exit behavior, and absence of
values, not exact hint sentences.

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

Doctor requests `OperatorImpact.NONE` for its per-secret preview. A `maybe` is reported as
uncertain, not as a failed backend-health check. A definitive provider failure remains diagnosable.
Doctor exposes no disruptive opt-in because a broad health sweep is the wrong surface for
authorizing an unknown number of operator actions.

Because non-disruptive preview may perform provider work, doctor documentation no longer promises
that secret preview is pure or never resolves internally. It promises no operator impact under the
configured classification and no returned value.

## Human diagnostics

Core derives guidance from `PreviewDetail` and caller context. No backend supplies remediation or
text. Important projections include:

| Detail                    | Human meaning                                                        |
| ------------------------- | -------------------------------------------------------------------- |
| `operator-impact-limited` | the backend cannot answer further under the current impact allowance |
| `tty-unavailable`         | this source needs terminal input, which this process does not have   |
| `soft-miss`               | this source has no value; later sources may apply                    |
| `authentication`          | the permitted provider attempt could not authenticate                |
| `deadline-exceeded`       | bounded provider work stopped before an answer                       |

Hints name only controls available on that command. `secret describe` and `secret verify` may name
`--allow-interaction`; an ordinary command blocked by global mode names `--non-interactive` as the
cause instead of suggesting a nonexistent local flag.

## JSON contract

`secret describe --output json` keeps envelope schema version 1 and changes its value-free
resolution object in the same release as the implementation. The proposed shape is:

```json
{
  "resolution": {
    "answer": "maybe",
    "source": "personal-op",
    "identifier": "op://vault/item/field",
    "detail": "operator-impact-limited",
    "attempts": [
      {
        "source": "personal-op",
        "identifier": "op://vault/item/field",
        "answer": "maybe",
        "detail": "operator-impact-limited"
      }
    ]
  }
}
```

No value, backend message, native error, or generic metadata is added. `source` and `identifier`
identify the selected or limiting attempt and may be null when the chain has none. Attempts retain
active-source order. The machine-output reference documents compatibility and exact null rules.

## Completion and command grammar

- Add `--allow-interaction` to `secret describe` completion/introspection surfaces.
- Keep `secret verify --allow-interaction` unchanged.
- Do not add this flag to ordinary resolving commands; global `--non-interactive` is their explicit
  impact control.
- Keep `--allow-interaction` incompatible with global `--non-interactive` at the command boundary.

## Permanent collateral

Implementation updates, in the same PR:

- `cli/agentworks/capabilities/secret_backend/README.md` for the rewritten contract;
- `cli/agentworks/secrets/README.md` for impact, preview, and resolution behavior;
- `cli/README.md`, `docs/guides/resources.md`, and relevant guide topics;
- CLI command reference and machine-output reference;
- sample source config for OnePassword impact classification;
- generated shell completions and schema snapshots;
- any module docstrings that still claim preview is pure or that TTY grants interaction authority.

No permanent file links to this SDD.

## Test matrix

The behavior suite crosses these axes:

- caller: preflight, describe, verify, doctor, ordinary resolution;
- impact: none, allow;
- terminal: present, absent;
- backend: env-var, prompt, OnePassword fake provider;
- provider state: value, soft miss, mapping failure, auth failure, connectivity failure, timeout;
- OnePassword auth classification: known unattended, default app action, configured no-impact app
  authentication;
- source order: earlier maybe, later yes, later hard no, all soft no;
- output: human fields, JSON structure, exit status, exception category, sentinel leak scan.

Tests assert structured behavior. They do not pin prose authored by Agentworks.

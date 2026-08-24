# Secret sources, preview, and resolution

`secret-source` is the configured resource that participates in lookup precedence. Each source has
an operator-chosen name and selects one registered `secret-backend` in `spec.backend`. Backends are
implementation capabilities; sources are configured instances. A secret's `backend_mappings` keys
and `[secret_config].sources` entries are source names, not backend names.

Two sources can select the same backend with different accounts, timeouts, preview settings, or
precedence. The complete backend-authoring contract is in the
[secret-backend README](../capabilities/secret_backend/README.md).

Operators crossing the 0.15 boundary should read
[docs/guides/upgrading-to-0.15.md](../../../docs/guides/upgrading-to-0.15.md) first: preview became
provider-aware in that release, which changes what `agw doctor` reports and can exit with, and
changes `agw secret verify`'s output shape.

## Default sources

Agentworks synthesizes `env-var` and `prompt`. The default chain is `['env-var', 'prompt']`.

- `env-var` reads `AW_SECRET_<UPPER_SNAKE_NAME>` unless the secret maps that source to another
  environment variable. Preview reads, validates, and discards a present value. An unset variable is
  ordinary missing and falls through. Resolution preserves the value exactly.
- `prompt` has no mapping vocabulary. It uses the caller-owned terminal broker only with available
  TTY access. `backend_mappings.prompt: false` opts one secret out.

Exact `False` is the framework opt-out for every source. A mapping-required backend with no mapping
is not a candidate. Static inspection uses the backend's structured lookup description and never
claims that a value exists.

## Configuring 1Password

Additional sources are YAML resources under `~/.config/agentworks/resources/`:

```yaml
apiVersion: agentworks/v1
kind: secret-source
metadata:
  name: work-op
spec:
  backend:
    name: onepassword
    account: work.example.com
    timeout: 30
    app_authentication_impact: operator-action
```

Enable the `onepassword` system plugin, add `work-op` to `[secret_config].sources`, and map a secret
with a scalar native reference:

```yaml
apiVersion: agentworks/v1
kind: secret
metadata:
  name: npm-token
  description: npm registry token
spec:
  backend_mappings:
    work-op: op://Engineering/npm/token
```

`app_authentication_impact` affects zero-impact preview only. Its conservative default,
`operator-action`, returns indeterminate when the backend cannot rule out app authentication without
starting it. Set `none` only when app authentication is acceptable under no-impact preview. Ambient
1Password service-account or Connect facts are treated as known unattended modes.

Actual resolution ignores preview impact and always attempts the bounded `op read`. It can therefore
raise desktop approval, biometric, browser, or device work. Item or field not-found text is treated
as failed lookup rejection, not ordinary missing, because the supported provider surface has not
supplied conclusive stable evidence for a narrower absence token.

## Preview and actual resolution

Preview is provider-aware and value-free. Its one impact input is:

- `NONE`: perform every lookup step that requires no operator action;
- `ALLOW`: permit operator action and return a definitive available, missing, blocked, or failed
  answer. A backend cannot return indeterminate at this level.

Preview statuses are `available`, `missing`, `indeterminate`, `blocked`, and `failed`. Backends may
fetch a value to establish existence, but they validate and discard it inside the backend. No value
crosses the preview boundary.

Actual resolution has no operator-impact input. It performs one source-first pass and returns only
resolved, missing, blocked, or failed. Flow is fixed:

- resolved/available completes the secret;
- ordinary missing falls through;
- execution blocked falls through and is retained for exhaustion;
- failed hard-stops that secret; and
- preview indeterminate falls through while retaining ordered evidence.

Before another provider source turn in a complete operation, core stops a batch that is already
terminal from a hard failure or static remaining-source facts. It performs no later provider or
broker work and records each otherwise skipped unresolved name as the core-only, unattributed
`blocked/batch-doomed-before-interaction` result. The explicit partial-reveal surface does not apply
this all-required completion rule and continues resolving independent names. Static viability uses
only mapping applicability, readiness, and plugin enablement. It never uses TTY access or backend
TTY capability as a prediction; each backend receives exact TTY access and decides whether it is
limiting.

A later preview available or failed result becomes the aggregate while earlier indeterminate
attempts remain visible. A chain with no applicable runtime lookup is blocked/no-candidate, not
missing. Preflight uses zero-impact preview, accepts available or indeterminate, rejects missing and
blocked, and rejects failed unless an earlier higher-precedence attempt was indeterminate. It
memoizes each secret once per command and does not replace actual resolution.

Provider, mapping, and protocol diagnostics are closed and value-free. Core derives error types and
guidance from status, reason, and safe source identity. Provider-native stderr and backend-authored
remediation text do not cross the boundary.

## TTY policy is separate

Global `--non-interactive` means only: do not use the TTY for interactions, even if one is present.
It does not disable color or formatting, and it does not suppress biometric, app, browser, device,
or other out-of-band provider work. A command invoked with `--non-interactive` can still start
1Password app authentication and wait until that source's configured timeout.

There is no general unattended fail-fast mode. For truly unattended execution, use `env-var` or a
provider authentication mode Agentworks can identify as unattended, such as supported 1Password
service-account or Connect configuration. Do not rely on `--non-interactive` to suppress out-of-band
approval.

TTY access is one of available, unavailable, or disabled. Prompt blocks before reading when terminal
input is unavailable or disabled. Env-var and OnePassword declare no TTY support and are independent
of all three states. Preview `--allow-interaction` and global `--non-interactive` may be used
together: the former allows out-of-band impact, while the latter still disables prompt.

## Operator surfaces

- `agw secret list` is static. It shows each source's candidate disposition, identifier, and
  readiness without constructing a client or reading a value. JSON v1 retains the derived
  `would_attempt` compatibility key.
- `agw secret describe NAME` adds a provider-aware preview at zero impact. Add `--allow-interaction`
  for the maximum-impact definitive preview. Its JSON v1 response retains the static resolution
  fields and adds nested tagged preview evidence.
- `agw secret verify NAME...` uses the same preview contract. It deduplicates names in first-written
  order, renders every result, and exits 1 unless all are available. Add `--allow-interaction` only
  with consent for possible prompt or out-of-band authentication.
- `agw doctor` previews each secret at zero impact. Available and indeterminate are OK; missing and
  blocked are WARN; failed is FAIL. Indeterminate rows share one numbered group note instead of
  repeating an interaction hint. Doctor omits secret descriptions and origin markers; those remain
  on `secret list` and `secret describe`. JSON includes the note text and a value-free
  `secret_preview` for secret checks.
- `agw env show --resolve` is the explicit partial-reveal surface. It resolves independent secrets;
  normal command resolution remains all-required.

## Value handling

Resolved values live only in the private operation batch and scoped delivery cache. NUL is rejected
before resolution succeeds. Carriage returns and line feeds are otherwise opaque value content. Each
consumer enforces its own narrower syntax: environment injection, Git credentials, Proxmox headers,
and Tailscale input require one logical line, while structured SDK credentials may remain multiline.
Preview or verification proves existence, not suitability for every sink.

Factory construction and context entry perform no provider or broker work. Cleanup always runs;
cleanup failure emits fixed source-only warning text and never masks the primary result or
interruption.

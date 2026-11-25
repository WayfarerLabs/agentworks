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
    timeout: 300
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

## Configuring Azure Key Vault

Enable the `azure` system plugin and declare a source for an existing vault:

```yaml
apiVersion: agentworks/v1
kind: secret-source
metadata:
  name: work-kv
spec:
  backend:
    name: azure-keyvault
    vault_url: https://example-vault.vault.azure.net
```

Add `work-kv` to `[secret_config].sources` in the desired precedence order. Ambient authentication
is the default and uses Azure's default credential chain. It needs no Azure VM site or subscription
configuration. Each source binds one vault; declare another source for a different vault or
identity.

To select a service principal explicitly, set its tenant and client identifiers and the environment
variable holding its client secret:

```yaml
auth:
  mode: service-principal
  tenant_id: 00000000-0000-0000-0000-000000000000
  client_id: 00000000-0000-0000-0000-000000000000
  client_secret_env: AZURE_CLIENT_SECRET
```

Place this `auth` table inside the source's `spec.backend`. The environment variable defaults to
`AZURE_CLIENT_SECRET` when omitted. The backend reads it only when performing the selected lookup;
it never stores the value in source configuration or falls back to another identity. A missing or
rejected credential is an authentication failure. Secret sources cannot reference another Agentworks
secret to authenticate themselves, so this input differs from a VM site's `auth.secret`. Use a
dedicated variable outside the `AW_SECRET_` prefix; Agentworks quarantines that prefix at startup
for its env-var secret backend.

Map each Agentworks secret to its Azure secret name:

```yaml
apiVersion: agentworks/v1
kind: secret
metadata:
  name: npm-token
spec:
  backend_mappings:
    work-kv: npm-token
```

The scalar mapping follows the latest Azure version. To pin a version, use an object instead:

```yaml
backend_mappings:
  work-kv:
    name: npm-token
    version: 0123456789abcdef0123456789abcdef
```

Use `agw resource explain secret-backend/azure-keyvault` for the installed source fields. The
backend only reads the selected secret version; it does not list the vault, create resources, write
values, or grant access. Confirmed secret absence permits lookup in the next source; denied access
or failed authentication stops lookup for that secret.

Preview may read and discard a value inside the backend. No-impact preview excludes authentication
paths that can request operator action. Allowing interaction, or performing actual resolution, may
initiate browser authentication for ambient credentials. `--non-interactive` controls terminal input
independently and does not suppress browser authentication.

Follow Microsoft's documentation for
[Azure authentication](https://learn.microsoft.com/en-us/azure/developer/python/sdk/authentication/overview),
[Key Vault permissions](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide), and
[Key Vault Secrets](https://learn.microsoft.com/en-us/python/api/overview/azure/keyvault-secrets-readme).

## Preview and actual resolution

Preview is provider-aware and value-free. Its one impact input is:

- `NONE`: perform every lookup step that requires no operator action;
- `ALLOW`: permit operator action and return a definitive available, missing, blocked, or failed
  answer. A backend cannot return indeterminate at this level.

Preview statuses are `available`, `missing`, `indeterminate`, `blocked`, and `failed`. Backends may
fetch a value to establish existence, but they validate and discard it inside the backend. No value
crosses the preview boundary. Indeterminate reasons preserve two different facts:

- `operator-input-required` means usable terminal input exists and resolution will request the value
  if it reaches that source;
- `operator-impact-limited` means preview skipped provider work that broader operator impact could
  permit, so availability was not checked.

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
input is unavailable or disabled. Env-var, OnePassword, and Azure Key Vault declare no TTY support
and are independent of all three states. Preview `--allow-interaction` and global
`--non-interactive` may be used together: the former allows out-of-band impact, while the latter
still disables prompt.

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
- `agw doctor` previews each secret at zero impact. Available and operator-input-required prompt
  paths are OK; provider work skipped as operator-impact-limited is INFO; missing and blocked are
  WARN; failed is FAIL. Doctor omits secret descriptions and origin markers; those remain on
  `secret list` and `secret describe`. JSON includes a value-free `secret_preview` for secret
  checks.
- `agw env show --resolve` is the explicit partial-reveal surface. It resolves independent secrets;
  normal command resolution remains all-required.

## Value handling

Resolved values live only in the private operation batch and scoped delivery cache. NUL is rejected
before resolution succeeds. Carriage returns and line feeds are otherwise opaque value content. Each
consumer enforces its own narrower syntax: Git credentials, Proxmox headers, and Tailscale input
require one logical line, while environment injection, which travels in the request, and structured
SDK credentials take multiline values whole. Preview or verification proves existence, not
suitability for every sink.

Operation logs and guest diagnostics redact each secret an operation registers with them as a whole:
the raw value, its shell-quoted and Python `repr` spellings, the value without leading or trailing
line breaks, and bash's `$'...'` spelling (`printf %q`, `declare -p`) in the C locale and in the
guest's `C.UTF-8`. In `C.UTF-8` bash prints a printable non-ASCII character raw, and printable is
glibc's `iswprint`, which the redaction approximates as Python's `str.isprintable` plus space,
format and private-use characters; a code point one side's Unicode tables assign and the other's do
not can still be spelled differently. One line of a multi-line secret shown on its own is not
redacted: matching each line would also redact common lines such as a PEM `-----END` line.

Factory construction and context entry perform no provider or broker work. Cleanup always runs;
cleanup failure emits fixed source-only warning text and never masks the primary result or
interruption.

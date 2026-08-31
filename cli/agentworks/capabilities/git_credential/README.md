# Git credential providers

Git credential providers produce HTTPS credentials for one declared `git-credential`. They own:

- their complete configuration schema and declared secret inputs;
- credential acquisition and optional authenticated runup checks;
- construction of the final Git username and password;
- translation of provider-specific scope into generic HTTPS host and path prefixes; and
- a fixed, value-safe failure hint when they return a runtime helper.

Core owns the other half. Before creation, it validates every provider's static scopes together and
asks providers to validate resolved inputs without side effects. Later it validates final provider
payloads, combines every credential selected for one user, routes Git requests by generic HTTPS
scope, and fully reconciles the Agentworks-owned credential state during every admin or agent
initialization. Providers never write user files or Git configuration.

## Declaring credentials

A `git-credential` manifest selects a provider in `spec.provider`. Every shipped provider requires a
structured `source`; there is no omitted default, scalar shorthand, or shared source model.

GitHub with a declared secret:

```yaml
apiVersion: agentworks/v1
kind: git-credential
metadata:
  name: github-work
spec:
  provider:
    name: github
    source:
      mode: secret
      secret: github-work-token
    owner: example-org
```

Only the secret arm's inner `secret` field may be omitted. It then defaults to
`git-token-<credential-name>`.

GitHub using the target user's active GitHub CLI identity:

```yaml
apiVersion: agentworks/v1
kind: git-credential
metadata:
  name: github-cli
spec:
  provider:
    name: github
    source:
      mode: gh-cli
    repos:
      - example-org/private-repo
```

Azure DevOps using the target user's active Azure CLI identity:

```yaml
apiVersion: agentworks/v1
kind: git-credential
metadata:
  name: azdo-cli
spec:
  provider:
    name: azdo
    org: example-org
    source:
      mode: az-cli
```

Admin and agent templates select these resources through their `git_credentials` list. Reinitialize
the VM or agent after changing that list or a credential manifest. There is no imperative credential
writer.

Use `agw resource explain git-credential-provider/github` or
`agw resource explain git-credential-provider/azdo` for the installed field reference, and
`agw resource sample git-credential` for a generated manifest shell.

## Shipped sources

### Secret sources

Both shipped providers support `source.mode: secret`. The provider reads only the references its own
model declared through its scoped `RunContext`. When `defaults.runup_git_credentials` is true,
GitHub and Azure DevOps retain their provider-owned authenticated checks. A definite rejection skips
that credential during multi-credential initialization; network indeterminacy warns and continues
unverified. Disabling runup skips this optional check, not materialization.

The provider returns a final `StoredCredential`. Core does not assume that a declared secret is the
final token: a provider may use several secrets, exchange them with an API, and still return either
a stored credential or a managed helper.

### GitHub CLI

When runup is enabled, `source.mode: gh-cli` checks the target user's login/interactive shell for
`gh`, then checks whether `gh auth status --active --hostname github.com` succeeds for the identity
the runtime token command will use. The check is read-only and advisory: arbitrary CLI output is
suppressed and the managed helper is installed even when the check warns. The readiness check
requires GitHub CLI 2.57.0 or newer for `--active`. On each matching Git `get`, that helper runs
exactly:

```text
GH_PROMPT_DISABLED=1 gh auth token --hostname github.com
```

The target user must have `gh` installed on `PATH` and must already be authenticated to the intended
`github.com` identity. That user owns and can change the active identity independently of
Agentworks; verify it after authentication changes. The provider returns username `x-access-token`
and the freshly acquired token as the password.

### Azure CLI

When runup is enabled, `source.mode: az-cli` checks the target user's login/interactive shell for
`az`, then checks whether `az account show` succeeds in the current target-user environment. It does
not request an Azure DevOps token during runup, and a warning does not prevent helper installation.
Its managed helper runs exactly:

```text
az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798 --query accessToken --output tsv
```

The target user must have `az` installed on `PATH`, must already be authenticated as the intended
identity, and that identity must have access to the configured Azure DevOps organization and
repository. That user owns and can change the active identity independently of Agentworks; verify it
after authentication changes. The provider returns the configured organization as username and the
fresh access token as password. The helper neither selects nor mutates Azure tenant or subscription
state.

If a CLI is not visible at this point in initialization, or its authentication check fails or is
indeterminate, initialization warns but still installs the helper. Later user-install/profile steps
may make a CLI visible, so verify again after initialization when applicable. If it is absent,
unauthenticated, times out, or returns malformed output at Git runtime, the wrapper suppresses its
stdout and stderr and prints only the provider's fixed recovery hint. Authenticate or repair the
target user's CLI identity and retry Git; reinitialization is needed only when the manifest or
generated helper changes. Setting `defaults.runup_git_credentials = false` skips both secret-source
verification and these CLI readiness checks, not helper installation.

## Scope and selection

Providers declare one or more static `HttpsCredentialScope` values. A scope contains a normalized
HTTPS host and an optional tuple of normalized path segments. Core knows no forge vocabulary.

- GitHub `repos: [owner/repo]` becomes `github.com` plus the two-segment path.
- GitHub `owner: owner` becomes the one-segment parent path.
- GitHub with neither becomes the `github.com` host default.
- Azure DevOps `org` becomes `dev.azure.com` plus the organization as one segment.

The generic dispatcher chooses the longest matching segment prefix. Every exact duplicate scope is a
configuration error, including duplicate host defaults. Git-provided usernames do not override path
selection.

The generated include resets earlier helpers only for hosts Agentworks manages, installs one stable
Agentworks launcher, and enables `credential.useHttpPath`. Unrelated hosts and operator Git settings
remain untouched.

## Provider contract version 3

A provider subclasses `GitCredentialProvider`, declares `contract_version = 3`, and supplies a
closed provider-local `AgwModel` with its literal `name` tag. It implements two required operations
and may override one side-effect-free hook:

```python
def credential_scopes(self) -> tuple[HttpsCredentialScope, ...]: ...
def validate_inputs(self, ctx: RunContext) -> None: ...  # default no-op
def credential_material(self, ctx: RunContext) -> CredentialPayload: ...
```

Static scopes come only from provider configuration. Before VM or agent creation mutation, core
validates all declarations together and calls `validate_inputs` with a fresh context without a
target containing the provider's scoped resolved inputs. At user initialization it constructs
another fresh context with the same scoped inputs and exactly the current admin or agent target for
runup, then a third fresh context without a target for materialization. The later payload is either
`StoredCredential(username, password)` or
`ManagedHelper(provider_authored_program, fixed_failure_hint)`.

The result carries no echoed credential name. Provider inputs and outputs are orthogonal: secret
references are derived from the provider's model, while the operation may return either payload
shape. A managed helper program is first-party provider code, never operator-authored config.

Core validates hosts, segment tuples, and collisions before creation; it validates protocol fields,
helper size, and failure hints before installation. Stored username/password values are line-safe
bounded UTF-8 fields and are stored as exact Git credential-protocol records, not credential URLs.
Delimiters such as `:`, `@`, `/`, `%`, `?`, `#`, `=`, and backslash therefore remain literal.

## Reconciliation and managed state

Every admin and agent initialization prepares selected providers, later materializes them, and
invokes the same reconciler, including when the desired set is empty. The reconciler uses one
bounded shared/exclusive lock and a private generation under:

```text
~/.agentworks/git-credentials/
  lock
  launch
  current -> generations/generation.*
```

Launchers share the short parent-directory handoff; reconciliation takes it exclusively. Each
generation contains its Git include, dispatcher, immutable stored protocol records, and provider
helper programs. Reconciliation stages a complete generation, atomically activates `current`,
registers exactly one include, and then removes inactive generations. A runtime request holds the
shared lock only while selecting material and opening its generation-owned file; that descriptor
keeps the selected material complete after the lock closes, and an external CLI process cannot
retain the lock.

Empty reconciliation removes the include, launcher, current generation, and all Agentworks-owned
credential material. Migration also removes the released Agentworks-owned legacy paths and only
their exact Git config registrations. A generic operator `credential.helper=store` remains.
`~/.git-credentials` is removed without inspection only when the exact old Agentworks helper
registration was witnessed before cleanup; an unwitnessed file or directory is preserved.

The runtime envelope accepts bounded Git credential protocol only, serves `get`, emits a diagnostic
on matching `erase`, and performs no mutation for unsupported operations. Stored values and runtime
CLI output do not appear in generated scripts, process arguments, logs, errors, or representations.

## Code and tests

- `base.py` defines the version-3 provider and material contract.
- `github.py` and `plugins/azure/azdo.py` own the shipped schemas, scope translations, runup, and
  final material.
- `git_credentials/state.py` validates material and builds generic per-user state.
- `git_credentials/reconcile.py` installs or removes that state over a transport.
- `tests/test_git_credential_contexts.py` covers graph edges and scoped delivery.
- `tests/test_git_token_verification.py` covers provider runup and input/output independence.
- `tests/test_git_credential_scoping.py` covers schema shape, translation parity, Git protocol,
  runtime helpers, routing, reconciliation, modes, and concurrency.

Tests should assert contract shape and behavior, not freeze authored documentation wording.

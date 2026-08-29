# Low-Level Design: Provider-owned Git Credential Material

- Status: Draft for design review
- Date: 2026-08-28
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Design Principles

1. Providers declare their dependencies, receive only those resolved secrets, and own credential
   acquisition end to end.
2. Providers return one of two final shapes: a stored credential or a managed credential helper.
3. Core understands generic Git HTTPS scope and safe installation, never forge-specific
   authentication semantics.
4. Providers never mutate target-user state; one core reconciler owns every write and removal.
5. One declarative full rebuild is the only write path. No adapter, installed-state manifest, or
   speculative versioned ABI survives the migration.

## Configuration Contract

### Shared secret arm

The existing arm and shorthand remain:

```yaml
spec:
  provider:
    name: github
    token: my-github-token
```

is equivalent to:

```yaml
spec:
  provider:
    name: github
    token:
      mode: secret
      secret: my-github-token
```

Omitting `token` continues to select `{mode: secret, secret: git-token-<credential-name>}`.

### GitHub CLI arm

```yaml
apiVersion: agentworks/v1
kind: git-credential
metadata:
  name: github-agent
spec:
  provider:
    name: github
    token:
      mode: gh-cli
    repos:
      - WayfarerLabs/agentworks
```

`gh-cli` has no additional fields. It targets `github.com` and the active account selected by GitHub
CLI. A future enterprise-host requirement adds a provider configuration field deliberately; this
effort does not infer one from ambient state.

### Azure CLI arm

```yaml
apiVersion: agentworks/v1
kind: git-credential
metadata:
  name: azdo-agent
spec:
  provider:
    name: azdo
    token:
      mode: az-cli
    org: example-org
```

`az-cli` has no additional fields. The existing required `org` remains provider configuration. The
provider translates it to a generic HTTPS path scope and its helper requests Azure DevOps resource
`499b84ac-1321-427f-aa17-267ca6975798`. Operator configuration cannot supply resource, command, or
helper-program text.

### Models and secret references

```python
class SecretToken(AgwModel):
    mode: Literal["secret"]
    secret: Annotated[NonEmptyStr, SecretRef(...)]


class GitHubCliToken(AgwModel):
    mode: Literal["gh-cli"]


class AzureCliToken(AgwModel):
    mode: Literal["az-cli"]
```

Each concrete provider keeps the existing discriminated union and scalar shorthand. Only the current
secret arm carries `SecretRef`; the CLI arms declare none. This is configuration truth, not a core
special case. Reference extraction continues to derive the provider's complete declared secret set
from its model. A future provider may declare several secret references and use them to produce one
credential without changing the capability contract.

## Provider Contract Version 3

### Generic scope and output types

Names are illustrative; the closed ownership and behavior are normative.

```python
@dataclass(frozen=True)
class HttpsCredentialScope:
    host: str
    path_prefix: tuple[str, ...] = ()


@dataclass(frozen=True)
class StoredCredential:
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class ManagedHelper:
    program: bytes = field(repr=False)
    failure_hint: str = "the selected credential helper failed"


CredentialPayload = StoredCredential | ManagedHelper


@dataclass(frozen=True)
class CredentialMaterial:
    credential_name: str
    scopes: tuple[HttpsCredentialScope, ...]
    payload: CredentialPayload = field(repr=False)
```

`HttpsCredentialScope` represents Git credential context, not provider vocabulary. Protocol is fixed
to `https` in this effort. Host is exact. `path_prefix` is a tuple of normalized, nonempty path
segments; empty means host default. Core strips a leading slash and one terminal `.git` suffix from
the incoming Git path, splits it into segments, and selects the matching scope with the greatest
segment count. GitHub translates an owner to one segment and a repository to two. Azure DevOps
translates its organization to one segment. Core sees only tuples.

`StoredCredential` contains the final Git username/password response. Core neither knows which
secret supplied it nor assumes the password is the resolved secret unchanged. Both fields are
sensitive and excluded from representations.

`ManagedHelper` is a provider-authored executable helper program that emits a complete Git
credential protocol response. It is fixed or safely rendered by trusted provider code, not supplied
as operator-authored command text. Core writes it into the managed generation, provides the common
bounded process envelope, and validates the response syntax, but it does not translate CLI stdout
into a forge credential. A future provider may return a bridge to any runtime identity tool through
this same shape. Core accepts this payload only when the provider configuration declares no secrets.
Every secret-bearing provider MUST return `StoredCredential`; a future secret-backed runtime-helper
shape is designed when one is actually needed.

### Required operation

```python
GitCredentialProvider.credential_material(ctx: RunContext) -> CredentialMaterial
```

This is the only credential-material operation required by the version-3 descriptor. The composition
root constructs `ctx` with `ScopedSecrets(resolved_values, node.secret_refs())` and no admin or
agent transport. The provider may read only names its configuration declared and has no
target-mutation power. It owns any required validation, API exchange, derivation, and final response
construction.

The operation performs no target-user filesystem or Git configuration mutation. For any provider
configuration with one or more secret references it returns a `StoredCredential`; for the current
zero-secret CLI arms it returns a `ManagedHelper` without executing the CLI at provisioning time.

The descriptor deletes version-2 `helper_entry`, `credential_lines`, `store_username`, and the
universal `secret_name` assumption atomically. Core no longer receives a token map or calls a
provider method with a naked token.

### Runup

When `defaults.runup_git_credentials` is enabled, the credential node calls provider `runup(ctx)`
before materialization. The same scoped context is used. Providers decide whether their configured
arm has optional readiness work:

```text
current secret arm -> provider performs its existing authenticated probe
current CLI arm    -> provider does nothing
```

A definitive rejection retains current skip/warn/partial semantics. Network indeterminacy warns and
continues. Disabling runup skips optional validation only; it never skips materialization a provider
requires to produce its final stored credential.

## Built-in Provider Output

### GitHub secret

- scopes: `github.com` plus provider-translated `repos` and optional `owner` path prefixes;
- payload: `StoredCredential`;
- username: credential resource name when scoped, otherwise released-compatible `x-access-token`;
- password: the final value the provider derives from its declared secret after optional runup.

### GitHub CLI

- same generic scope translation;
- payload: fixed provider-owned `ManagedHelper`;
- helper checks and runs `gh auth token --hostname github.com` with `GH_PROMPT_DISABLED=1`,
  validates one nonempty line, and emits `username=x-access-token` plus `password=<token>`;
- fixed failure guidance names `gh` and tells the operator to check installation, the target-user
  `PATH`, and GitHub CLI authentication;
- captured upstream stderr and token output never enter its diagnostic.

### Azure DevOps secret

- scopes: `dev.azure.com` with configured `org` translated to one path-prefix segment;
- payload: `StoredCredential`;
- username: configured `org`;
- password: the final value the provider derives from its declared secret after optional runup.

### Azure CLI

- same generic scope translation;
- payload: fixed provider-owned `ManagedHelper`;
- helper checks and runs:

  ```console
  az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798 \
    --query accessToken --output tsv
  ```

- helper validates one nonempty line and emits `username=<org>` plus `password=<access-token>`;
- fixed failure guidance names `az` and tells the operator to check installation, the target-user
  `PATH`, and Azure CLI authentication;
- it performs no subscription or tenant mutation; Azure CLI's current account owns identity
  selection; captured upstream stderr and token output never enter its diagnostic.

Microsoft's current Git guidance recommends a Bearer header. The username/password response is a
deliberate compatibility choice for Debian Bookworm's Git 2.39 based on current Git Credential
Manager behavior. The design PR proves this exact response with a real read-only Azure Repos Git
operation before it becomes ready; implementation later proves clone, fetch, and a reversible write.
Failure revises the provider output, not core's generic two-shape contract.

## Core Material Boundary

Core validates provider output once:

1. credential name is the expected declared resource;
2. at least one scope exists;
3. host and path segments are normalized, bounded, and free of separators, dot segments, controls,
   and unsafe generated-config characters;
4. duplicate nonempty scopes are rejected across materials; released first-declared behavior is
   retained for multiple host defaults;
5. stored username/password fields satisfy Git's newline/NUL restrictions and are never represented;
6. managed-helper program and hint are bounded; the hint is line/control safe; the program came from
   trusted provider code rather than an operator-configured command field; and the declaring
   provider configuration has no secret references;
7. output ordering is deterministic by host, descending path length, path, then declaration order.

Core does not validate whether a secret is a PAT, exchange one secret for another token, construct a
forge authorization header, or infer provider scope.

```python
@dataclass(frozen=True)
class UserCredentialState:
    include_content: str
    dispatcher_script: str
    stored_credentials: bytes | None = field(default=None, repr=False)
```

`build_user_credential_state(materials)` renders the private store, generation-owned dispatcher, and
include. Stored values never enter the dispatcher or Git configuration. Each managed-helper program
is installed as a private generation-owned executable without any operator-configured command escape
hatch.

## Managed Layout

```text
~/.agentworks/git-credentials/
  lock                          0600, stable across generations
  launch                        0700, stable lock-and-handoff launcher
  current -> generations/<generation-id>
  generations/
    <generation-id>/
      config.gitconfig          0600
      dispatch                  0700, generic scope router
      helpers/                  0700, private directory
        <credential-id>         0700, provider-owned managed-helper program
      stored-credentials        0600, omitted when unused
```

The root is wholly Agentworks-owned. A safe generation ID is unrelated to content. Credential names
never become unchecked path components. Target-local comparison discards an identical staged
generation without returning sensitive bytes to the workstation.

After building complete bytes locally, the reconciler takes a 30-second bounded exclusive `flock` on
the stable lock. Under it, the reconciler cleans the one abandoned-stage path, stages the private
generation, compares it to active state, atomically replaces `launch` when its current
implementation changed, and atomically replaces `current` with GNU `mv -T` when desired state
changed. Staging inside the lock needs no PID, lease, or ownership protocol.

The launcher waits at most 10 seconds for a shared lock, using file descriptor 9 in this
implementation, then executes `current/dispatch` with Git's operation arguments unchanged. The
dispatcher loads the selected stored value or managed-helper path from one generation before closing
descriptor 9. Its common runtime envelope then invokes that generation-owned provider helper. CLI
descendants cannot retain the lock. Contention fails with fixed value-safe retry guidance.

The observable invariant is that one helper request reads exactly one complete generation. The
launcher and `current` are separate atomic replacements, so this implementation must support and
test both adjacent launcher/dispatcher pairings plus a fault between replacements. File descriptor 9
is the present mechanism, not a public or permanently versioned ABI. No future versioned root or
compatibility regime is specified.

An activation failure leaves the prior link in place. Startup removes abandoned stages and inactive
generations while holding the exclusive lock. A crash may leave dormant owned files but never an
active partial generation; the next initialization finishes cleanup.

## Agentworks Git Include

The only global Git mutation is the exact include value:

```text
~/.agentworks/git-credentials/current/config.gitconfig
```

For each managed host, the generated include conceptually contains:

```gitconfig
[credential "https://github.com"]
    helper =
    helper = !~/.agentworks/git-credentials/launch
    useHttpPath = true
```

The empty helper resets inherited helper values only in that exact host context; the following value
registers Agentworks. `useHttpPath` ensures the generic dispatcher receives the remote path. Real
Git 2.39 tests, not generated-text inspection alone, prove host confinement and path delivery.

Reconciliation removes duplicate instances of the exact include path and adds it once for nonempty
state. It never removes other includes or unqualified helper values. Empty desired state removes the
include reference.

## Dispatcher and Managed-helper Protocol

The dispatcher reads bounded Git `key=value` input through the blank terminator. It uses only
`protocol`, `host`, `path`, and optional `username`; unknown fields are ignored without evaluation.

For `get`:

1. require `https` and an exactly managed host, otherwise return no values;
2. normalize the path into safe segments and strip one terminal `.git` suffix;
3. ignore an embedded username for scope selection; provider `review_remote` remains the config-time
   home for forge-specific advisories;
4. choose the longest matching path prefix, then the first declared host default;
5. load the selected payload from the same generation;
6. close the shared-lock descriptor after the required data is resident;
7. return a stored response directly, or execute the selected managed helper in the common bounded
   envelope and relay only a syntactically valid Git credential response.

For `store`, do nothing. For `erase`, retain the fixed selected-credential diagnosis but do not
delete declarative state. Unknown operations return success with no output.

The common managed-helper envelope:

- invokes the installed provider-authored helper program in the target user's environment;
- uses coreutils `timeout` with a fixed 10-second bound;
- provides the bounded Git request on stdin;
- captures stdout and stderr separately;
- accepts only a bounded, newline/NUL-safe Git credential response containing a username and
  password for `get`;
- never forwards captured upstream stderr or includes response values in diagnostics;
- prints the provider's fixed value-safe failure hint on nonzero exit, timeout, or malformed
  response;
- never invokes login or persists/cache credentials itself.

The provider helper, not core, checks and invokes its required command and decides how CLI output
becomes the username/password response. It MUST handle command absence and execution failure,
disable prompting where supported, and avoid forwarding upstream output. Built-in provider tests
prove those semantics and value containment.

## Reconciliation Algorithm

```text
nodes = resolve declared git-credential nodes and held providers
resolved = resolve union(nodes.secret_refs())

for each node/provider:
    ctx = RunContext(secrets=ScopedSecrets(resolved, node.secret_refs()))
    if runup policy enabled:
        provider.runup(ctx)
    material = provider.credential_material(ctx)
    validate material at core boundary

state = build_user_credential_state(surviving materials)
reconcile_user_git_credentials(target_user, state)
```

The final reconcile always occurs. A definitively rejected provider is skipped under current partial
semantics. If no material survives, reconciliation removes all provably Agentworks-owned
credential/routing state and retains only the inert stable lock. Rejection of the last credential
therefore cannot leave an older credential active.

## Legacy Cleanup

Every full reconcile removes only known legacy Agentworks artifacts and values:

- global `credential.helper` exactly equal to `!~/.agentworks-git-cred-helper.sh`;
- global `include.path` exactly equal to `~/.agentworks-git-scopes.gitconfig`;
- `~/.agentworks-git-cred-helper.sh`;
- `~/.agentworks-git-scopes.gitconfig`;
- the Agentworks-owned `~/.git-credentials` generated by the released implementation;
- the older warn-only `~/.agentworks-git-cred-warn.sh`, if present.

Cleanup never uses unqualified `--replace-all`. A generic `credential.helper=store` installed by old
direct add is indistinguishable from operator configuration and remains, but the owned credential
file it read is deleted without inspection. Legacy credential bytes are disabled before new
activation or registration cleanup. Faults may leave authentication absent, never stale-active.

For nonempty state, reconciliation activates the complete generation and adds the include before
garbage collection. For empty state it removes the include, launcher, symlink, and generations after
legacy material is disabled. Only the empty lock may remain for concurrent callers.

## Call-site Changes

### Admin and agent initialization

Both composition roots:

1. resolve providers, including an empty set;
2. resolve the operation-wide union of provider-declared secrets;
3. give each provider its own scoped context for runup and materialization;
4. collect final materials without building a token map;
5. invoke the same builder/reconciler unconditionally before private Git-backed user setup.

The agent's duplicated material writer and global Git commands are deleted. Transport and home path
are the only admin/agent differences.

### Removed imperative command

`vm add-git-credential` and its manager function are deleted. Operators declare credentials on the
admin or agent template and run reinit.

### Graph and orchestration

`GitCredentialNode.secret_refs()` remains structurally derived and may contain zero, one, or several
names. Scoped delivery enforces that `credential_material(ctx)` cannot read beyond them. Core has no
`secret_name`, static-source filter, or resolved-token map.

## Validation Matrix

### Provider contract

- omitted/scalar/explicit secret parity;
- valid provider-owned CLI arm and cross-provider rejection;
- zero-, one-, and synthetic multi-secret declarations;
- scoped context denies undeclared reads;
- a synthetic multi-secret provider derives one stored credential while core remains agnostic;
- managed helper accepted only for zero-secret configuration; stored credential required otherwise;
- provider materialization cannot mutate target state through its operation surface;
- schema, sample, resource show, and graph projection.

### Scope and build

- GitHub repository/owner and Azure organization translate to generic path tuples;
- longest segment-aware prefix, host default, `.git` normalization, no match, and deterministic
  ordering;
- duplicate nonempty scope refusal and released multiple-default behavior;
- mixed stored/helper output on one host;
- no credential in dispatcher/include/representations/errors;
- malformed scope, stored protocol fields, managed-helper metadata, and operator command injection
  rejected at the core boundary.

### Managed-helper runtime

- fake GitHub/Azure helpers cover success, required command absent, nonzero, timeout, malformed or
  control-bearing protocol output, and noisy stderr;
- provider tests prove the exact `gh`/`az` command and username/password construction;
- unsupported Git operations, embedded username not overriding selection, and retained
  provider-owned remote advisories;
- target-user environment and actual Git credential fill/HTTPS operation behavior;
- consecutive fake helper responses differ, proving per-`get` acquisition.

### Reconciliation

- fresh, same-input, add, remove, scope change, payload-shape switch, last removal;
- admin/agent parity, empty desired state, and zero survivors;
- legacy/mixed-state cleanup with unrelated helpers/includes preserved;
- staged-write and activation faults retain one complete prior state;
- concurrent helper/swap/cleanup and empty transitions use the same shared/exclusive lock;
- lock timeouts and proof that managed-helper descendants cannot retain the descriptor;
- current launch with previous dispatch and previous launch with current dispatch, plus a fault
  between their two atomic replacements;
- mutation-boundary fault injection, concurrent reconcilers, and abandoned-stage cleanup.

### Live integration

- GitHub CLI identity: clone/fetch and reversible write under the authorized budget;
- Azure CLI service-principal identity: clone/fetch and equivalent reversible write against a
  disposable Azure Repos repository;
- real Git proves generic path routing and host-scoped config precedence on Git 2.39;
- no credential values in logs, process arguments, test artifacts, or retained files.

## Deletions

The implementation removes rather than adapts:

- contract-version-2 operation methods and descriptor requirements;
- core `secret_name`, static-source, token-map, and forge-specific selector assumptions;
- the core token-to-protocol mapper and built-in CLI command recipes;
- duplicated admin/agent material writers and conditional reconciliation gates;
- `vm add-git-credential` and its manager path;
- unqualified global helper replacement and stale-empty-state behavior;
- permanent launcher/dispatcher ABI versioning and speculative future managed roots.

## Open Implementation Proofs, Not Open Requirements

Two facts remain empirical gates:

1. the host-scoped include/reset and generic longest-path router behave correctly on Debian
   Bookworm's Git 2.39; and
2. Azure Repos accepts the provider-owned username/password response built from a real `az`-issued
   Entra token.

The design-ready integration run proves item 2 read-only. Implementation later proves both through
the generated state and reversible writes. Failure stops for design revision; it is not permission
to add a compatibility layer, install another Git, or switch to Git Credential Manager.

# Low-Level Design: Scoped Runtime Git Credential Helpers

- Status: Draft for design review
- Date: 2026-08-28
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Design Principles

1. Providers emit inert helper definitions; core owns every write and Git config mutation.
2. Acquisition mode is configuration shape, not runtime guesswork.
3. Secret-backed and CLI-backed credentials share selection and reconciliation, not lifecycle work.
4. One declarative full rebuild is the only write path; the imperative direct-add command is
   removed.
5. Generated state is simple, value-contained, and replaceable. No compatibility adapter survives
   the migration.

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
CLI. A future enterprise-host requirement adds a provider configuration field deliberately; version
1 does not infer one from ambient state.

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

`az-cli` has no additional fields. The existing required `org` remains the selection scope. The
helper uses Azure CLI's active identity and requests the Azure DevOps resource
`499b84ac-1321-427f-aa17-267ca6975798`. It does not accept arbitrary resource/scope command text.

### Models

```python
class SecretToken(AgwModel):
    mode: Literal["secret"]
    secret: Annotated[NonEmptyStr, SecretRef(...)]


class GitHubCliToken(AgwModel):
    mode: Literal["gh-cli"]


class AzureCliToken(AgwModel):
    mode: Literal["az-cli"]


GitHubTokenAcquisition = Annotated[
    SecretToken | GitHubCliToken,
    UnionScalarShorthand(discriminator="mode", arm=SecretToken),
]

AzDOTokenAcquisition = Annotated[
    SecretToken | AzureCliToken,
    UnionScalarShorthand(discriminator="mode", arm=SecretToken),
]


class GitHubConfig(TokenAcquiringConfig):
    token: GitHubTokenAcquisition = Field(default={"mode": "secret"})


class AzDOConfig(TokenAcquiringConfig):
    token: AzDOTokenAcquisition = Field(default={"mode": "secret"})
```

The explicit raw secret-arm default on each concrete field is normative: Pydantic does not inherit
the base field default after the annotation is overridden, and the owner boundary fills the secret
name template before validation. Shared shorthand logic preserves scalar input. Schema, reference
extraction, and samples derive from these models. No mode is accepted by a provider merely because
another provider implements it.

## Provider Contract Version 3

### Types

```python
@dataclass(frozen=True)
class CredentialSelector:
    host: str
    repos: tuple[str, ...] = ()
    owner: str | None = None


@dataclass(frozen=True)
class StaticTokenSource:
    secret_name: str
    username: str


@dataclass(frozen=True)
class RuntimeCommandSource:
    username: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()


CredentialSource = StaticTokenSource | RuntimeCommandSource


@dataclass(frozen=True)
class HelperDefinition:
    credential_name: str
    selector: CredentialSelector
    source: CredentialSource
```

Names are illustrative; implementation may choose equivalent concise names. The closed fields and
ownership are normative. Providers emit a fixed command recipe, not executable script text. Core
derives identifiers and diagnostics from the provider and credential names and executes every
runtime recipe through one helper implementation.

### Required operation

`GitCredentialProvider.helper_definition() -> HelperDefinition` is the only credential-material
operation required by the descriptor. It is config-only and deterministic. It performs no secret
read, network request, subprocess, filesystem access, or target mutation.

The descriptor advances to contract version 3 and requires `helper_definition`. The version-2
`helper_entry`, `credential_lines`, `store_username`, and universal `secret_name` assumptions are
deleted atomically.

### Runup

The base provider exposes acquisition inspection and a secret-only runup path:

```text
if acquisition is SecretToken:
    resolve/deliver secret through existing node edge
    validate line safety
    provider verifies token when runup setting is enabled
else:
    no secret edge
    no runup operation
```

The GitHub and Azure DevOps `_verify_token` implementations remain provider-specific. Their success
details and rejection classification remain unchanged except that diagnostics say static token where
needed and no code assumes every credential has a secret.

## Built-in Helper Definitions

### GitHub secret

- selector host: `github.com`
- exact repositories: deduplicated configured `repos`
- owner: configured `owner`
- static username: resource name when scoped, otherwise `x-access-token` for released compatibility
- source: `StaticTokenSource`

### GitHub CLI

- same selector construction as GitHub secret
- source: `RuntimeCommandSource` with username `x-access-token`, argv
  `gh auth token --hostname github.com`, and environment `GH_PROMPT_DISABLED=1`
- stdout: exactly one nonempty line, accepted only after line/control validation
- captured stderr: never forwarded verbatim

### Azure DevOps secret

- selector host: `dev.azure.com`
- owner: configured `org`
- static username: configured `org`
- source: `StaticTokenSource`

### Azure CLI

- same selector construction as Azure DevOps secret
- source: `RuntimeCommandSource` with username set to the configured organization
- exact argv:

  ```console
  az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798 \
    --query accessToken --output tsv
  ```

- response: conventional Git helper `username=<org>` and `password=<access-token>` fields
- no subscription or tenant mutation; the current Azure CLI account owns identity selection
- stdout: exactly one nonempty line, accepted only after line/control validation
- captured stderr: never forwarded verbatim

Microsoft's current Git guidance recommends a Bearer header. The conventional response above is a
deliberate compatibility choice for Debian Bookworm's Git 2.39: current Git Credential Manager's
Azure Repos provider returns the same Entra access token as a `GitCredential` username/password
pair, including for service-principal identities. The implementation still proves this exact form
against live Azure Repos before handoff, and the design PR first proves the wire form through a real
read-only Git operation when it becomes ready. It does not expose resource/scope, extra CLI flags,
or arbitrary executable paths as configuration.

## Core Material Model

```python
@dataclass(frozen=True)
class UserCredentialState:
    include_content: str
    dispatcher_script: str
    static_store_content: str | None = field(default=None, repr=False)
```

`UserCredentialState` exists only in the provisioning process and may contain final static-store
bytes. The normative `repr=False` (or a value-safe custom representation) keeps that field out of
representations and diagnostics.

`build_user_credential_state(definitions, resolved_static_tokens)`:

1. validates every host, scope, username, command recipe, and credential name at the sink;
2. requires exactly one resolved token for each static source and none for runtime sources;
3. rejects duplicate `(host, repo)` and `(host, owner)` claims;
4. preserves released first-declared behavior for multiple unscoped credentials on one host;
5. orders host, exact repo, owner, default, and helper records deterministically;
6. renders the private static store, generation-owned dispatcher, and include.

No token is interpolated into the dispatcher or include.

## Managed Layout

The new per-user root is:

```text
~/.agentworks/git-credentials/
  lock                          0600, stable across generations
  launch                        0700, stable lock-and-handoff launcher
  current -> generations/<generation-id>
  generations/
    <generation-id>/
      config.gitconfig       0600
      dispatch               0700, selectors and fixed runtime recipes
      static-credentials     0600, omitted when unused
```

The exact root is Agentworks-owned. The safe generation ID is unrelated to content. A target-local
comparison discards an identical staged generation without returning credential bytes to the
workstation. Credential names are not used directly as unchecked path components.

After building the complete bytes locally, the reconciler takes a 30-second bounded exclusive
`flock` on the stable lock file. Under that lock it cleans the one fixed abandoned-stage path,
stages the complete private generation, compares it to active state, atomically replaces the stable
launcher when its fixed implementation changed, and atomically replaces `current` with GNU coreutils
`mv -T` when desired state changed. Keeping staging inside the lock means concurrent reconcilers
need no stage ownership, PID, or lease protocol.

The stable launcher opens the lock descriptor itself and waits at most 10 seconds for a shared lock.
It resolves and executes `current/dispatch` while retaining that descriptor. The generation-owned
dispatcher therefore always runs with code, selectors, recipes, and optional static values from one
generation. It loads the selected value or runtime recipe into process memory, closes the lock
descriptor, and only then prints the static response or starts `gh`/`az`. CLI descendants cannot
retain the lock. Contention fails with one fixed value-safe retry diagnostic. `flock` (util-linux),
GNU `mv`, and coreutils `timeout` are standard Debian Bookworm dependencies already present before
optional initialization packages.

The launcher/dispatcher ABI is immutable version 1: Git's operation remains the dispatcher's first
argument, the launcher's shared-lock descriptor is always file descriptor 9, and the dispatcher must
close descriptor 9 only after loading everything it needs from its generation. The launcher executes
`current/dispatch` with all Git arguments unchanged and no configuration-derived argument. Any
future launcher-content change must work with both the previous and current generation dispatcher;
otherwise it requires a new versioned managed root. Cross-version pairing tests cover both
directions and a failure between launcher replacement and `current` activation.

If staged bytes match the active generation, the stage is discarded. A failed symlink activation
leaves the prior link in place; startup removes abandoned stages and inactive generations while
holding the exclusive lock.

Legacy cutover disables stale service before activating desired state: delete the wholly
Agentworks-owned legacy `~/.git-credentials`, replace/remove the exact old custom helper, and remove
its exact registration. A generic `credential.helper=store` possibly installed by old direct add is
not distinguishable from operator configuration and is not removed; with the owned store deleted it
cannot serve an old Agentworks token. For nonempty desired state, the reconciler then activates the
generation and adds the new include before collection. For empty desired state, it removes the new
include, symlink, and generations after legacy credential material is disabled. Fault injection at
every boundary may leave authentication temporarily absent but must never reactivate a stale
credential. The launcher is also removed; only the empty stable lock may remain for concurrent
callers. A crash can leave dormant owned files but cannot expose a partial credential set; the next
initialization finishes cleanup.

Transport primitives may require a small shared remote-reconcile operation rather than shell text
assembled at call sites. Admin and agent call that one operation.

## Agentworks Git Include

The only global Git mutation is the exact include value:

```text
~/.agentworks/git-credentials/current/config.gitconfig
```

The generated file contains one context per managed host, conceptually:

```gitconfig
[credential "https://github.com"]
    helper =
    helper = !~/.agentworks/git-credentials/launch
    useHttpPath = true
```

The empty value resets helpers accumulated for that host; the following value registers the stable
launcher. The implementation must verify with real Git that URL-context matching confines this reset
to the managed host and that repository paths reach the dispatcher.

Reconciliation removes duplicate instances of the exact include value and adds it once when state is
nonempty. It never removes other includes. When desired state is empty, it leaves no reference to
the managed root.

## Launcher and Dispatcher Protocol

The stable launcher takes the Git operation as its first argument, acquires the bounded shared lock,
and executes the dispatcher from the resolved current generation. The generation-owned dispatcher
reads bounded `key=value` lines from stdin through the blank terminator. It needs only protocol,
host, path, and username for selection and diagnostics. Unknown fields are ignored without
evaluation.

For `get`, while holding the launcher's shared lock:

1. require `https` and a managed host, otherwise return no values;
2. normalize the Git remote path using the existing bounded rules;
3. reject an embedded username that violates provider semantics using the existing advisory shape;
4. select exact repository, then owner/org, then host default;
5. read the selected static token or runtime command recipe into memory from that generation;
6. close the lock descriptor before executing a child process;
7. return the credential response without logging it.

For `store`, do nothing. For `erase`, retain a fixed diagnosis naming the selected credential and
its source mode but do not delete declarative state. Any other operation returns success with no
output.

If a selected runtime source fails, it writes one fixed diagnostic to stderr and the dispatcher
returns nonzero with no credential attributes. Upstream stdout/stderr is captured; neither is
included in the diagnostic.

## Runtime Helper Process Contract

The one core dispatcher executes each runtime recipe under the same contract:

- uses an explicit command name (`gh` or `az`) found through the target user's runtime `PATH`;
- disables interactive prompting where the CLI supports it;
- runs through coreutils `timeout` with a fixed 10-second bound; both are guaranteed on standard
  Debian Bookworm targets and `timeout` is already used by bootstrap before optional packages;
- captures stdout and stderr separately;
- accepts exactly one nonempty, line-safe token;
- prints only final Git credential protocol attributes;
- never persists or caches the token itself;
- never invokes a login command;
- reports missing executable, command failure, timeout, and invalid output through distinct fixed
  diagnostics. A valid credential rejected by the forge uses Git's normal failure and the existing
  fixed selected-credential diagnosis.

## Reconciliation Algorithm

### Full user initialization

```text
definitions = providers.map(helper_definition)
static = definitions.filter(StaticTokenSource)
runup survivors = validate static tokens under current policy
surviving definitions = runtime definitions + surviving static definitions
state = build_user_credential_state(surviving definitions, static values)
reconcile_user_git_credentials(target_user, state)
```

The final call always occurs. If no definition survives, it removes Agentworks credential/routing
state and exact legacy registrations, retaining only the empty stable lock. Thus a rejected last
static credential cannot leave an older credential active.

## Legacy Cleanup

Every full reconcile removes only these known legacy artifacts/values:

- global `credential.helper` equal to `!~/.agentworks-git-cred-helper.sh`;
- global `include.path` equal to `~/.agentworks-git-scopes.gitconfig`;
- `~/.agentworks-git-cred-helper.sh`;
- `~/.agentworks-git-scopes.gitconfig`;
- the Agentworks-owned `~/.git-credentials` generated by the released implementation;
- the older warn-only `~/.agentworks-git-cred-warn.sh` helper if still present.

Cleanup does not use `--replace-all` without an exact value matcher. Released initialization owns
and overwrites the whole `~/.git-credentials` path; migration therefore deletes that exact file
instead of parsing secrets or retaining stale active lines. This preserves today's ownership rule.

## Call-site Changes

### Admin init/reinit

`_phase_b_setup` calls credential reconciliation unconditionally. Provider resolution may return an
empty map. The resolved token map contains only static sources. The Git credential step stays before
private Git-backed dotfiles/mise work.

### Agent create/reinit

The agent initializer invokes the same builder/reconciler unconditionally after the target user
exists and before private Git-backed user setup. The current duplicated material writing and global
Git commands are deleted.

### Removed imperative command

`vm add-git-credential` and its manager function are deleted. The command reference and guide point
operators to the admin/agent declaration plus reinit path.

### Graph and orchestration

`GitCredentialNode.secret_refs()` remains structurally derived and may be empty. `runup()` becomes a
no-op for runtime sources. Composition roots stop requiring a token for every provider and prove
that every static source received exactly its declared secret.

## Validation Matrix

### Configuration

- omitted/scalar/explicit secret parity;
- valid provider-owned CLI arm;
- cross-provider CLI arm rejection;
- extra field rejection;
- secret edge present only for secret arm;
- schema, sample, resource show, and graph projection.

### Build and selection

- mixed static/CLI exact, owner/org, and default selection;
- collision refusal and deterministic output;
- no token in dispatcher/include/representations/errors;
- unsafe provider output rejected by core wrapper;
- no-match and embedded-username behavior.

### Runtime

- fake `gh`/`az` success, missing executable, nonzero command, timeout, empty, multiline/control
  output, and noisy stderr;
- unsupported Git operations;
- environment belongs to target user;
- actual Git credential fill and HTTP operation behavior.

### Reconciliation

- fresh install, same-input rerun, add, remove, scope change, source-mode switch, last-item removal;
- admin/agent parity;
- zero desired and zero runup survivors;
- legacy migration;
- unrelated helpers/includes preserved;
- staged-write/symlink-activation failure leaves the old complete state;
- concurrent helper/swap/cleanup and empty-state transitions use the same shared/exclusive lock;
- lock contention timeout and proof that a fake CLI child cannot retain the shared lock;
- previous-launcher/current-dispatcher and current-launcher/previous-dispatcher ABI pairings, plus a
  fault between the two atomic replacements;
- mixed legacy/new fault injection after every cleanup/activation mutation;
- two-reconciler staging/cleanup and empty-state removal.

### Live integration

- GitHub CLI identity: clone/fetch and reversible branch/tag or scratch-repository push/delete under
  the test environment's permission and cleanup budget;
- Azure CLI service-principal identity: clone/fetch and equivalent reversible write against a
  disposable Azure Repos repository;
- fake CLIs return different values on consecutive invocations, proving acquisition happens on each
  `get` without manipulating live login or cache state;
- no credential values in captured logs, test artifacts, process arguments, or retained files.

## Deletions

The implementation removes, rather than adapts:

- contract-version-2 operation methods and descriptor requirements;
- duplicated admin/agent material writers;
- the conditional "only if providers" reconciliation gates;
- the `vm add-git-credential` command and manager path;
- unconditional provider `secret_name`/token-map assumptions;
- comments and tests describing providers as universally PAT-backed;
- unqualified global helper replacement;
- stale-file behavior when desired credentials become empty.

## Open Implementation Proofs, Not Open Requirements

Two facts must be proven before implementation can hand off:

1. the chosen host-scoped Git include/reset sequence behaves correctly on Debian Bookworm's Git
   2.39; and
2. Azure Repos accepts the helper response produced from a real `az`-issued Entra token for clone,
   fetch, and push.

Failure of either proof is a design stop, not permission to add a compatibility layer, install a new
Git, or switch to Git Credential Manager without operator direction.

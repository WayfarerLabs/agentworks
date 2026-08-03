# Git Credential Providers

> The detailed companion to the capability overview in [`../README.md`](../README.md), focused on
> the `git-credential-provider` kind. That overview covers the contract every capability shares.
> This guide is for both operators and developers: the first part (before the Technical Overview)
> covers the functional details (what a git credential provider is, the shipped providers, and their
> obligations) that matter to both audiences, and the part after the divider is developer-focused,
> covering the implementation contract and the behavior of the shipped `github` and `azdo`
> providers.

## What Is a Git Credential Provider?

A git credential provider obtains the credential an agent needs to clone and push against ONE git
host over https, without baking tokens into images or hand-carrying them onto a VM. Most providers
source a token from an operator-named secret (a personal access token, or PAT); a provider may
instead mint one, for example by calling the host's API to create a scoped token, rather than
reading a pre-existing one. Either way, it says how git should present that credential: the store
line, the username git keys on, and how the credential is selected when several serve the same host.
That is the whole of what a provider does. It obtains a credential and describes its use; it never
touches a VM.

Everything downstream of that is Agentworks, not the provider. Agentworks resolves the named secret,
materializes the token onto the VM in the form git expects, and wires up git so ordinary git
commands authenticate automatically. Part of that wiring is a generated helper script that git
consults for each remote, which is worth disambiguating from a term it collides with: git has its
own built-in notion of a "credential helper" (the program git runs to fetch credentials, such as
`credential-store`). An Agentworks git credential provider is a different thing, a config-side
capability that sources a token and describes its use. Agentworks itself generates and installs the
actual git credential helper on the VM; the provider never does.

## Available Providers

Two providers ship today, one per supported host. This list can change, so
`agw resource list --kind git-credential-provider --include-disabled` is the definitive set on any
given install.

- **`github`** (built in) sources a GitHub PAT for `github.com`. It can optionally be scoped to a
  set of repositories (`repos`) or to a single owner (`owner`), so several credentials can serve the
  same host and each repository draws the one meant for it. Its token secret is named by the `token`
  field.
- **`azdo`** (via the `azure` system plugin) sources an Azure DevOps PAT scoped to one Azure DevOps
  organization (a required `org`). It becomes available when that plugin is enabled.

Whichever provider a credential names, an operator can rely on two guarantees:

- **A live token is never pasted into config.** Today's providers source their token from a named
  secret: a credential's `token` field points at the _name_ of a secret that holds the token, and
  the secret backend supplies the value at provisioning time. A provider may instead mint a token
  (for example via the host's API) rather than source one, drawing on a named bootstrap secret where
  it needs credentials to do so. Either way, the interface never asks for a live token in plaintext
  config, and the same credential definition travels between operators who store their tokens
  differently.
- **A bad token is caught early.** At provisioning time Agentworks verifies the token against its
  host before writing anything, so an expired, revoked, or mistyped token surfaces as a clear,
  actionable error up front rather than as a confusing git failure partway through setup. (The check
  is skippable by policy for airgapped setups.)

## Git Credential Provider Obligations

A git-credential-provider obtains a git credential for one host, says how git should present it, and
vouches for it. It:

- **MUST** obtain a credential (a token) for its one git host, either by sourcing it from an
  operator-named secret (read only through the framework's resolve pass) or by minting it (for
  example via the host's API); it **MUST NOT** accept a pasted credential, and **MUST NOT** hold,
  cache, log, or persist a token beyond the call that produces it.
- **MUST**, if it mints rather than sources, mint idempotently (check-then-mint: reuse a still-valid
  existing credential rather than minting a fresh one on every run), and declare any bootstrap
  secret it needs to mint through the same resolve pass.
- **MUST** produce what git needs to authenticate as that credential on a VM: the stored entry, the
  username git keys on, and the selection the helper uses.
- **MUST** validate anything it interpolates into a store URL, a gitconfig header, or the generated
  helper to a safe charset, at its own source rather than trusting a downstream check.
- **MUST** default its store username to a value unique per credential, so two scoped credentials on
  one host cannot collide, overriding only where the host dictates and only to a value that stays
  disjoint for that host.
- **MUST NOT** write to the VM, configure git, or otherwise mutate the VM or its git configuration
  in any stage; it returns strings and lets Agentworks materialize and wire them.
- **MUST NOT** mint or mutate in `runup` or `review_remote` (both are read-only), and **MUST NOT**
  reach the network or the host anywhere but the token check and, for a minting provider, its
  check-then-mint op, both of which happen after the resolve boundary.
- **MUST NOT** speak for, serve, or advise on a host it does not own, so it never shadows another
  provider's credential or clobbers an unrelated host's git configuration.
- **SHOULD** verify the token against its host before it is relied on, raising a typed rejection on
  a definitive failure while warning and continuing on network indeterminacy, so a bad token
  surfaces clearly rather than as a failing clone later.
- **SHOULD** announce, on successful verification, the identity the token authenticates as and its
  expiry where the host exposes them, to aid rotation
  ([#372](https://github.com/WayfarerLabs/agentworks/issues/372)).
- **SHOULD** advise, through `review_remote`, when a declared repo remote would defeat this
  credential's resolution (an embedded username that bypasses path-based selection).
- **MAY** carry a host-specific scope (github `repos`/`owner`, azdo `org`) so several credentials
  serve one host and each repo draws the one meant for it.

It does not write to the VM or configure git itself. Agentworks materializes the credential onto the
VM and wires the credential helper; the provider sources the token, says how git should present it,
and vouches for it.

## Technical Overview

The preceding sections describe the operator-facing model. The remaining sections cover where a
provider sits, how its output becomes working authentication on a VM, the method contract, the two
shipped providers, and the implementation layout.

A **git credential provider** is the code that sources and provisions credentials for one git host,
so Agentworks (and the agents running on a VM) can authenticate to that host over plain https. Each
subclasses `GitCredentialProvider` (`base.py`), sources a personal access token (PAT) from a named
secret, checks that token against the host at the `runup` stage, and produces the materials that get
written to a VM: the store line, the selection entry for the generated credential helper, and the
username the helper keys on. The provider never touches the VM, the database, or the CLI; it
validates its own config, probes its host, and returns strings.

Two providers ship today and are the working references throughout this guide:

- **`github`** (`github.py`): the core built-in. Sources a GitHub PAT and, optionally, carries a
  fine-grained-PAT scope (`repos` or `owner`) so multiple credentials can serve the same host,
  selected per repository (issue #166). The reference for the scoped case.
- **`azdo`** (`agentworks/plugins/azure/azdo.py`): the Azure DevOps provider, shipped in the opt-in
  `azure` system plugin. Sources an Azure DevOps PAT scoped to one required `org`, which doubles as
  both the store username and the owner scope. The reference for a plugin-shipped provider and for a
  host whose own URL layout carries the routing.

They differ in host, probe endpoint, username convention, and a few host-specific quirks; the
[GitHub vs Azure DevOps](#github-vs-azure-devops) section lays those out side by side.

### Where a Git Credential Provider Sits

The capability ladder (`../README.md` has the full model), credential edition:

- The **kind** (`"git-credential-provider"`, `kinds.py`) is fixed by the core:
  `category="capability"`, `miss_policy="error"` (a credential naming an unknown provider fails at
  `build_registry`), `builtin_override="reserved"`. Its sibling `"git-credential"` kind (also
  `miss_policy="error"`) is the DECLARABLE side: operators must declare every credential they
  reference, and a typo errors at finalize with the reference source named.
- A **capability** is a `GitCredentialProvider` subclass registered in
  `GIT_CREDENTIAL_PROVIDER_REGISTRY` (`__init__.py`), plus a read-only `GitCredentialProviderEntry`
  registry row (`kinds.py`) so it lists and describes like any resource. Core built-ins publish
  their own rows (`publish_to`); a plugin-seated provider's row is published by the plugin machinery
  with a `system-plugin` origin, and `publish_to` skips it (that is exactly how `azdo` publishes).
- An **instance** is one provider bound to one declared credential:
  `cls(credential_name, provider_config, description=...)`, its config only, never a resolved token.
  Constructed by the composition roots (see below).
- The **consuming resource** is the `git-credential` declarable (`GitCredentialConfig`,
  `git_credentials/credential.py`): a thin wrapper that names a provider (`spec.provider`'s `name`
  key) and supplies its `provider_config`, and owns the instance built from it. Its node
  (`GitCredentialNode`, `git_credentials/nodes.py`) holds the instance, composes its readiness with
  the one-line fan-in, and folds the instance's declared token secret into its own `secret_refs`.

Layering is a hard rule: this package depends only on the framework and never imports its consuming
domain. The consuming resource (`GitCredentialConfig`) and the materials assembly that writes
credentials to a VM live in the `git_credentials/` domain, not here; the domain depends on the
capability, never the reverse.

#### How a Credential Reaches a Git Operation

A provider's output is inert on its own. Three domain pieces turn it into working auth on a VM, and
they are where the `helper_entry` / `credential_lines` / `store_username` / `secret_name` surface is
actually consumed:

1. **Construction** happens in `resolve_git_credential_providers`
   (`vms/initializer/credentials.py`): given the credential names from the admin row or an agent
   template's `git_credentials` list, it looks each one up in the registry, refuses a disabled
   provider (reading the propagated verdict off the graph), and constructs the instances. It touches
   no secret machinery; the declared token secrets ride the node's `secret_refs` into the
   operation's boundary resolve.
2. **Deferred runup**, then **materials assembly**, happen in `git_credentials/__init__.py`:
   - `runup_and_filter` runs each provider's `runup` right before anything is written,
     authenticating the resolved token against the host. A definitively rejected credential is
     dropped from the set with a warning (init degrades to PARTIAL) rather than sinking the whole
     operation, because git-credential provisioning is idempotently retryable (fix the token,
     `reinit`). The stage is skippable by operator policy
     (`[defaults] runup_git_credentials = false`).
   - `build_credential_materials` assembles a `CredentialMaterials` from the survivors: the full
     `~/.git-credentials` body (from each provider's `credential_lines(token)`), the
     Agentworks-owned gitconfig include (exactly the `credential.useHttpPath = true` switch), and
     THE git credential helper, a generated POSIX-sh script. It reads `helper_entry()` for each
     provider's host, username, and scopes; enforces that no two credentials claim the same scope on
     one host (a hard `ConfigError`); and bakes the `secret_name` into the helper's rejection
     diagnosis.
3. **Writing** happens in `_configure_git_credentials` (`vms/initializer/credentials.py`): it writes
   the three files onto the VM (`~/.git-credentials` at mode 600, the include
   `~/.agentworks-git-scopes.gitconfig` at mode 600, and the helper
   `~/.agentworks-git-cred-helper.sh` at mode 700), then points git's global `credential.helper` at
   `!<helper path>` with `--replace-all` (which also migrates released VMs off git's
   `credential-store`) and adds the include behind an idempotent guard. All three files are
   overwritten wholesale on every init, so the whole step is idempotent by construction and re-runs
   cleanly under `reinit`.

The **credential helper mechanism** is the runtime half. The managed include sets
`credential.useHttpPath = true`, so git sends the helper the remote's host AND path on every query.
The helper (generated in `git_credentials/__init__.py`'s `_helper_script`) picks the most specific
credential for that (host, path): exact repo, then owner (first path segment), then the host's
default (an unscoped credential), then the first store line for the host (legacy semantics, which
also keeps `vm add-git-credential` additions serving). Selection lives entirely in the helper and
keys on the store username each `HelperEntry` carries; the store file just maps username back to
token. The helper's `erase` deliberately never deletes (git calls it after a rejected auth, which is
exactly when the operator needs a diagnosis, not state destruction, the way `credential-store`
silently wiped the provisioned line). Every user gets their own store, include, and helper, built
from their own credential list: admin during VM init, and each agent during agent provisioning
(`agents/initializer.py`).

`review_remote` is a fourth, config-only consumption path with no token and no VM:
`remote_advisories` (`git_credentials/__init__.py`) asks every declared credential to review a repo
remote URL, and workspace create (`workspaces/manager/create.py`) runs it against the workspace's
declared `repo` so a remote that would defeat credential resolution draws an advisory before anyone
clones.

### The Contract

A new provider implements this surface (see `base.py` for the full docstrings). The lifecycle it
plugs into (the `dependencies` / `validate` split, the preflight/runup boundary, ops after the
resolve pass) is the shared capability contract in `../README.md`; what follows is what is specific
to a credential provider.

#### Class Identity and Registration

`name` and `description` ClassVars (the registry row), and the inherited
`owner_kind = "git-credential"` (error framing: config errors render as `git-credential/<name>`).
Register the class in `GIT_CREDENTIAL_PROVIDER_REGISTRY` for a core built-in; a plugin-shipped
provider is seated into the same registry by the plugin machinery at import and its row publishes
with a `system-plugin` origin instead (the `azdo` shape).

#### The Token-Secret Edge: Total and Non-Throwing

The token-sourcing providers both do the same thing: return the one `ConfigReference` the PAT config
implies. The shared helper `token_dependency(owner, config)` derives it: the `provider_config`'s
`token` field NAMES the secret (a bare `git-token-<name>` default from `default_token_secret` when
absent), and a malformed `token` field (present but not a non-empty string) makes the edge's
identity underivable, so the helper returns `None` and the edge is omitted rather than raised. Both
`github` and `azdo` wrap it identically:

```python
@classmethod
def dependencies(cls, owner: str, config: Mapping[str, object]) -> tuple[ConfigReference, ...]:
    ref = token_dependency(owner, config)
    return (ref,) if ref is not None else ()
```

Declaring that edge is what puts the token secret into the credential node's `secret_refs`, which is
what gets it into the boundary resolve and therefore delivered to `ctx.secret` at runup. A future
MINTING provider would instead declare its bootstrap secret(s) here (or none), and mint the token in
an op rather than sourcing it.

#### Validation: Shape and Vocabulary Only

The throwing half of the split. It calls `validate_token_field(owner, config)` (the correctness
check for the `token` field: present means a non-empty secret name) and adds its own provider-shaped
rules:

- `github` (`_validated_scope`): `repos` and `owner` are mutually exclusive (a fine-grained PAT is
  scoped to one or the other), `repos` is a non-empty list of `"owner/name"` strings restricted to
  the GitHub name charset (`_NAME_RE`), `owner` is a single name with no slash, and an unknown field
  raises (with a targeted hint when it is the singular `repo`). The charset rule has teeth: these
  values are interpolated verbatim into gitconfig section headers and store URLs, so anything
  outside the set would corrupt the VM's git config at first use.
- `azdo`: `org` is REQUIRED and must match `_ORG_RE` (it is interpolated into the generated helper),
  and the only other permitted field is `token`.

`validate` stays limited to shape and vocabulary. Host-owned choice sets remain the host's
responsibility, and checks requiring external state belong in runup.

#### Construction: Cheap, No I/O

The base `__init__` binds `(owner_name, config)` and re-runs `validate` (so a shape error dies at
construction, never later); the provider constructor re-parses its own scope shape from the bound
config (`github` re-runs `_validated_scope`, `azdo` re-reads `org`), which cannot raise because
`validate` already passed. Nothing else: no network, no token resolution, no probe. The instance
never holds a token or a resolver; the value arrives through the context at runup and op time.

#### Verifying the Token at Runup

`runup` is the whole point of a credential provider at the readiness layer. The base implements it
once:

```python
def runup(self, ctx: RunContext) -> None:
    self._verify_token(ctx.secret(self.secret_name))
```

`secret_name` is the token secret the credential sources from: the single secret its `dependencies`
declared (default `git-token-<name>`), read back from the node's `secret_refs`. A provider
implements exactly one slot, `_verify_token(token)`, and drives it through the shared `_probe_pat`
helper. `_probe_pat` does the whole HTTP dance and the failure classification, so a provider only
supplies the URL, the auth headers, the reject-status set, and a host label:

- A single authenticated GET via `_http_probe` (which returns HTTP error statuses rather than
  raising them; only network-level failures raise `OSError`).
- **HTTP 200**: returns `(body, headers)` so the provider can announce success with any enrichment.
- **A reject status** (definitive rejection): raises `TokenRejectedError`, a typed, actionable error
  naming the credential, its secret, and the "expired, revoked, or mistyped?" hint. Runup runs
  before any VM or user mutation, so raising here is safe.
- **Network indeterminacy or any other non-200**: warns and returns `None`. A transient outage or an
  odd status must never block work an unverified-but-valid token would have completed. This is the
  read-only, best-effort, warn-on-indeterminacy contract runup owes.

`github._verify_token` probes `GET https://api.github.com/user` with a Bearer header, rejects on
`401`, and on success enriches the announcement with the token's `login` (parsed from the body) and,
for a fine-grained PAT, its expiry (parsed from the `github-authentication-token-expiration`
response header). `azdo._verify_token` probes `GET https://dev.azure.com/<org>/_apis/connectionData`
with a Basic header (base64 of `:<token>`), and rejects on `401` OR `203` (Azure DevOps answers a
bad PAT on some routes with its sign-in page under a 203, not a 401).

Runup never mints and never mutates. A minting provider would READ-and-check the current token in
runup and mint only in a flagged, idempotent, check-then-mint op (see the idempotency section of
`../README.md`).

#### Ops: The Materials Surface

The mutation-phase output. For a token-sourcing provider these are pure functions of the bound
config and the resolved token, which is why they are idempotent for free (the domain writes the
files wholesale):

- `helper_entry() -> HelperEntry` returns the credential's selection entry for the generated helper:
  its `host`, its `username` (the store-line key the helper selects by), and its scopes (`repos`
  match the remote path exactly, `owner` matches the first path segment; no scopes means the host's
  default candidate). `HelperEntry` is a frozen dataclass in `base.py`.
- `credential_lines(token) -> list[str]` returns the `~/.git-credentials` lines, each a
  `https://user:token@host` URL. `github` emits `https://<store_username>:<token>@github.com`;
  `azdo` emits `https://<org>:<token>@dev.azure.com/<org>`.
- `store_username` (property) is the username on the store line and the join key the helper selects
  by. Default is the credential's own resource name; a provider overrides where the host dictates.
  `github` returns the credential's resource name for a SCOPED credential (GitHub accepts any
  username with a PAT) and the released `x-access-token` for an unscoped one; `azdo` returns the
  org.
- `secret_name` (property, on the base) is the token secret, named by the helper's rejection
  diagnosis and read at runup.

#### Remote Review: Advisory, Config-Only

`review_remote(url) -> list[str]` is an advisory review of a declared repo remote against THIS
credential's resolution semantics: no token, no network, no per-user wiring, reading only the
instance's own host and scope. It returns advisory strings when the URL is served by this
credential's host and something about it would defeat resolution, and `[]` otherwise (the default
abstains). It lives on the instance precisely because only the instance knows its host and how it
selects. `github` flags ANY embedded username on a `github.com` remote (the helper would serve by
the embedded username and skip path-based per-repo/owner selection). `azdo` flags only a username
that is NOT the org (the standard `https://<org>@dev.azure.com/<org>/...` remote embeds exactly what
the helper serves by, so it resolves correctly; only a foreign username bypasses it).

### GitHub vs Azure DevOps

The two shipped providers differ in every host-specific dimension and agree on the framework
surface. The contrast is the fastest way to see which parts of a provider are host policy and which
are the contract:

| Dimension             | `github`                                              | `azdo`                                                     |
| --------------------- | ----------------------------------------------------- | ---------------------------------------------------------- |
| Ships as              | core built-in                                         | `azure` system plugin (opt-in)                             |
| Host                  | `github.com`                                          | `dev.azure.com`                                            |
| Probe endpoint        | `GET /user` on `api.github.com`, Bearer header        | `GET /<org>/_apis/connectionData`, Basic `:<token>` header |
| Reject statuses       | `401`                                                 | `401`, `203` (AzDO's sign-in-page answer for a bad PAT)    |
| Config vocabulary     | optional `repos` XOR `owner`, plus `token`            | required `org`, plus `token`                               |
| Store username        | resource name (scoped) or `x-access-token` (unscoped) | the `org`                                                  |
| `helper_entry` scope  | `repos` / `owner` from config                         | the `org` doubles as the owner scope                       |
| Success enrichment    | announces `login` and (fine-grained) expiry           | announces success only                                     |
| `review_remote` flags | any embedded username                                 | only a username that is not the org                        |

The shared shape underneath: identical `dependencies` (both wrap `token_dependency`), both call
`validate_token_field`, both drive `_verify_token` through `_probe_pat`, both return
`credential_lines` and a `HelperEntry`. A third provider should look the same from the outside and
differ only in these host-policy rows.

### Best Practices

Grounded in the two shipped providers.

#### Source Secrets by Name, Never by Value

A provider names its token secret and reads the value only through the context; it never holds a
token. The `provider_config`'s `token` field carries a NAME (defaulting to `git-token-<name>`), the
edge is declared in `dependencies`, and the value is delivered to `runup` and the materials op
through the framework's resolve pass. This is the same discipline the cloud platforms follow for
their API credentials (see the credentials section of `vm_platform/README.md`): nothing ever invites
an operator to paste a live credential into a plaintext config file.

#### Probe at Runup, Not Before and Not at Construct

The authenticated check belongs in `runup`, after the resolve boundary, so every token is checked
the same way regardless of where it came from (env var, prompt, 1Password). Construction stays cheap
and token-free; preflight stays credential-free (a git-credential preflight must not fail
`vm create` because the VM or the admin user does not exist yet, all created later in that same
command). The dependency-blindness discussion in `../README.md` explains why the check cannot move
earlier.

#### Typed Errors: Definitive Rejection vs Indeterminacy

A definitive rejection (a 401, or azdo's 203) is a `TokenRejectedError` with entity framing and an
actionable hint; a network failure or any other status warns and continues unverified. The
distinction is load-bearing for the caller: `runup_and_filter` skips a REJECTED credential and
degrades init to PARTIAL, but an indeterminate probe leaves the credential in the set so a transient
outage never blocks provisioning. Route both through `_probe_pat` rather than re-implementing the
classification.

#### Keep Interpolated Values Charset-Safe

Anything a provider puts into a store URL, a gitconfig header, or the generated helper is validated
to a safe charset at its source: github scopes via `_NAME_RE`, the azdo org via `_ORG_RE`, and the
materials assembly re-checks store usernames and scope values with `_assert_sh_safe` before baking
them into sh. Validate new config fields the same way; the helper generator is safe by construction,
not by a distant invariant.

#### Idempotency Is Free Here, and Stays Free

The materials ops are deterministic functions of config plus token, and the domain writes all three
files wholesale and registers the helper with `--replace-all`, so `reinit` reconciles cleanly with
no per-op state. Preserve that: a token-sourcing provider needs no idempotency flag. A minting
provider would, and would owe the check-then-mint guard the flag documents.

### Testing

No real host is ever contacted: the suite-wide conftest guard makes any unmocked probe look like a
network failure, so a test can never reach the network. The layers, with the shipped suites as
templates:

- **Token verification (`runup`):** `cli/tests/test_git_token_verification.py`. The authenticated
  probe for both providers: a definitive rejection raises `TokenRejectedError`, network
  indeterminacy warns and continues, success announces. The template for any provider with a token
  to verify.
- **Scoping and materials:** `cli/tests/test_git_credential_scoping.py`.
  `build_credential_materials` over scoped and unscoped credentials, the generated helper's
  selection (verified against a real git version), scope-collision errors, and the store-username
  disjointness rule.
- **Config contract:** `cli/tests/test_capability_config_contract.py`. The `dependencies` /
  `validate` split at the `provider_config` boundary: accepted shapes, unknown-field and wrong-type
  raises, and the token edge extraction.
- **Kind and miss policy:** `cli/tests/resources/test_git_credential_provider_kind.py` (the provider
  kind and its published rows) and `cli/tests/test_git_credentials_typo_errors.py` (the
  `git-credential` kind's error miss policy: a typo'd or undeclared name errors at finalize with the
  source named).
- **Orchestrated end to end:** `cli/tests/vms/test_add_git_credential_orchestrated.py` and the agent
  create/reinit suites exercise construction, the deferred runup, and the write step through the
  real provisioning path with stubbed transports.

### Cross-References

- [`../README.md`](../README.md): the capability lifecycle contract and prerequisite for this guide.
- [`vm_platform/README.md`](../vm_platform/README.md): the sibling deep-dive; its credentials
  section is the reference for the secret-by-name discipline shared here.
- `base.py`: the `GitCredentialProvider` ABC, `HelperEntry`, and the shared helpers
  (`token_dependency`, `validate_token_field`, `default_token_secret`, `credential_name_from_owner`,
  `_probe_pat`, `_http_probe`).
- `github.py`: the `github` provider (the scoped, fine-grained-PAT reference).
- `agentworks/plugins/azure/azdo.py`: the `azdo` provider (the plugin-shipped reference).
- `kinds.py`, `__init__.py`: the `git-credential` / `git-credential-provider` kinds,
  `GIT_CREDENTIAL_PROVIDER_REGISTRY`, and `publish_to`.
- `agentworks/git_credentials/`: the consuming resource (`GitCredentialConfig`), its node, and the
  materials assembly (`build_credential_materials`, `runup_and_filter`, the helper generator) that
  writes credentials to a VM.
- `agentworks/vms/initializer/credentials.py`: `resolve_git_credential_providers` and
  `_configure_git_credentials`, the construction and write steps.
- `docs/guides/resources.md`: the operator-facing model (scoped GitHub credentials, the tagged
  `spec.provider` shape, the helper mechanism).
- `agentworks/plugins/README.md`: shipping a provider as a system plugin (the `azdo` path).

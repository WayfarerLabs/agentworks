# Functional Requirements: Runtime Git Identities

- Status: Draft for design review
- Date: 2026-08-28
- Architecture: [hla.md](./hla.md)
- Detailed design: [credential-helper-lld.md](./credential-helper-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Plan: [plan.md](./plan.md)

## Summary

Agentworks currently models every Git credential as a secret-backed personal access token. During
user initialization it verifies the token, writes it into a managed credential store, generates a
scope-selecting Git credential helper, and registers that helper globally. This works for PATs but
does not represent dedicated identities already authenticated through GitHub CLI or Azure CLI.

Git credential providers will own credential acquisition and translate their forge-specific
configuration into generic HTTPS credential scopes. During user initialization, core passes each
provider only the resolved secrets it declared. The provider may return a final credential for the
standard Agentworks store or a declarative provider-owned helper that acquires credentials later.
Core does not interpret the provider's secrets or authentication flow; it validates the returned
boundary shapes and reconciles every Agentworks-owned helper and Git configuration artifact,
including the transition to no configured credentials.

Secret-backed tokens retain their current runup validation. CLI-backed credentials deliberately do
not check tool installation or authentication during provisioning; their helpers acquire a fresh
credential at Git-operation time and return a clear, value-safe failure if the CLI is unavailable or
unauthenticated.

## Goals

1. Support GitHub CLI and Azure CLI identities without storing their short-lived access tokens in
   Agentworks configuration or on-VM credential files.
2. Make the provider contract return final Git credential material rather than expose PAT-specific
   acquisition details to core.
3. Preserve useful provider-owned early validation for secret-backed static tokens.
4. Make Agentworks-owned Git credential state converge exactly on every user initialization,
   including complete removal.
5. Preserve repository-, owner/organization-, and host-default outcomes through generic HTTPS path
   scopes rather than forge vocabulary in core.
6. Coexist with operator-managed Git configuration and helpers outside the hosts Agentworks owns.
7. Remove the imperative credential-install path instead of creating a second source of desired
   state beside declarative user initialization.
8. Leave future CLI authentication features independent from helper installation.

## Terminology

- **Provider**: the code capability selected by a `git-credential` resource, currently `github` or
  `azdo`.
- **Acquisition mode**: the `token.mode` arm that says where a helper gets its credential: `secret`,
  `gh-cli`, or `az-cli` as allowed by that provider.
- **HTTPS scope**: a normalized protocol, host, and optional segment-aware path prefix used to match
  one Git credential context. Providers translate forge concepts such as a GitHub owner or Azure
  DevOps organization into this generic shape.
- **Stored credential**: provider output containing one or more HTTPS scopes plus the final username
  and password/token that core installs in the standard Agentworks credential store.
- **Managed helper**: provider output containing one or more HTTPS scopes plus a declarative,
  provider-owned Git credential helper implementation that core installs and reconciles.
- **Credential materialization**: the provider operation that receives its scoped resolved secrets,
  performs any required validation, exchange, or derivation, and returns one of those two shapes.
- **Reconciliation**: rebuilding the complete Agentworks-owned per-user Git credential state from
  desired provider material.
- **User initialization**: admin initialization/reinitialization or agent creation/reinitialization
  for the target Linux user. It does not mean authenticating a third-party CLI.

## Users and Stories

### Operator using dedicated GitHub identities

The operator authenticates `gh` as the intended identity in an agent or admin user's environment,
configures a GitHub credential with `token.mode: gh-cli`, and expects later HTTPS Git operations to
use the token exposed by that user's active `gh` identity.

### Operator using an Entra service principal for Azure DevOps

The operator authenticates `az` as a service principal, configures an Azure DevOps credential with
`token.mode: az-cli`, and expects Git operations against the declared Azure DevOps organization to
obtain a short-lived Entra token at runtime.

### Operator retaining a secret-backed PAT

The operator keeps the existing omitted, scalar, or explicit `secret` token spelling. Core delivers
that resolved secret only to the declaring provider; the provider performs its current validation
and returns the final stored credential.

### Operator removing credentials

The operator removes every credential from an admin or agent template and reruns initialization.
Agentworks removes its prior token store, helper scripts, include, and exact global include
reference. No stale Agentworks credential remains available.

## Requirements

### R1: Narrow provider purpose

A git credential provider MUST validate its provider-specific configuration, declare every secret it
needs, and translate its configuration into generic HTTPS scopes. At credential-materialization
time, core MUST invoke it with a `RunContext` that exposes only those resolved secrets. The provider
MUST own every authentication-specific step, including validation, exchange, and derivation, and
return either a final stored credential or a declarative managed helper.

The provider MUST NOT write target-user files, alter Git configuration, install or authenticate a
CLI, or read an undeclared secret. Core MUST NOT interpret a provider's secret names, assume a
secret is itself a Git token, or implement forge-specific acquisition.

### R2: Closed acquisition arms

The existing `token` discriminated union remains the configuration surface:

- `github` accepts `secret` and `gh-cli`.
- `azdo` accepts `secret` and `az-cli`.
- Omission and scalar shorthand continue to select `secret` exactly as they do today.
- Every non-secret arm requires its explicit discriminator.
- A provider rejects acquisition modes it does not implement.

The current `secret` arm declares its configured secret reference. CLI-backed arms add no synthetic
secret edge and accept no secret field. The provider contract remains general: a future provider
configuration may declare several secrets and use them to derive one final stored credential without
changing core.

### R3: Secret-backed runup validation

For the current `token.mode: secret` arms, initialization MUST resolve the declared secret and
deliver it only through the provider's scoped context. The provider MUST retain its authenticated
runup check and MUST return the final username/password response; core MUST enforce Git protocol
line/control safety on that returned boundary. A definitive rejection keeps the current
multi-credential user-initialization semantics: skip that credential, warn, and record partial
initialization. Network indeterminacy warns and continues unverified.

`defaults.runup_git_credentials = false` continues to disable only this static-token verification.
It has no effect on runtime CLI acquisition.

### R4: Runtime CLI acquisition

For `gh-cli` and `az-cli`, the provider MUST return a managed helper without testing whether the
corresponding executable is installed, whether it is authenticated, or whether it can reach the
forge. Core installs the returned helper as opaque provider-owned behavior. This avoids coupling
helper installation to tool-install and login ordering.

The managed-helper definition MUST contain the provider-owned helper program. When that program
delegates to an external executable, it MUST name the executable as a generic runtime dependency.
Core MUST install the program, check any declared dependency on the target user's `PATH` immediately
before helper invocation, and report a clear value-safe missing-tool error. Core does not invoke the
dependency itself. This is a Git-runtime check, not provisioning readiness; the provider program
invokes the dependency and still MUST handle execution failure because availability can change after
the check.

At each matching Git `get` request:

- `gh-cli` obtains the active token for `github.com` from GitHub CLI.
- `az-cli` obtains a fresh token for the Azure DevOps resource from the active Azure CLI identity.
- The helper emits only the Git credential protocol response required for that forge.

The helper MUST NOT initiate login, prompt, open a browser, select an account/tenant/subscription,
or invoke a login command. Ordinary token-cache reads and refreshes performed internally by the
selected CLI are allowed; Agentworks does not reimplement or control them.

### R5: Runtime failure contract

A CLI-backed helper MUST fail nonzero with one concise, actionable stderr diagnostic when its
declared executable is missing, the helper command fails, returns an empty or malformed response, or
times out. It MUST NOT print a token or include captured token output in an error. Repository
authorization is the forge's decision; a valid token for the wrong identity fails through Git's
normal authentication path and the existing fixed selected-credential diagnosis.

Unsupported `store`, `erase`, and future Git helper operations MUST be safely ignored unless the
existing Agentworks rejection diagnosis applies. Runtime acquisition MUST be non-interactive and
bounded. Waiting for the stable credential-state lock MUST also be bounded and fail with fixed retry
guidance rather than hanging Git.

### R6: Generic scope selection

Core MUST route only on normalized Git credential context: protocol, exact host, and an optional
segment-aware path prefix. Providers MUST translate their own configuration into those scopes. For
the current providers, exact repository becomes a longer path prefix, owner or Azure DevOps
organization becomes its parent prefix, and host default has no path prefix. Longest matching path
wins, preserving the existing repository-before-owner/organization-before-host outcome without
teaching core those concepts.

Identical nonempty path claims remain configuration errors. Multiple host-default claims retain the
released first-declared behavior. Selection MUST NOT depend on a stored username; the selected
stored credential or managed helper supplies the complete provider response directly.

### R7: Total per-user reconciliation

Admin and agent initialization MUST always invoke the Agentworks Git credential reconciler, even
when the desired credential list is empty or every secret-backed credential was rejected.

The reconciler MUST rebuild the complete Agentworks-owned state from desired provider material. On
an empty desired state it MUST remove every provably Agentworks-owned helper, static-token store,
generation, Git include, and exact include reference left by current or legacy Agentworks versions.
One empty stable lock file MAY remain solely to serialize concurrent reconciliations and helper
starts. A generic `credential.helper=store` value that is indistinguishable from operator
configuration MAY remain, but the Agentworks-owned store it formerly read MUST be absent.

Repeated initialization with the same desired inputs MUST be idempotent. Removing, adding, changing
scope, or changing an acquisition mode MUST converge in one run without preserving stale material.
Concurrent helper requests MUST use one complete generation while reconciliation and garbage
collection wait for them rather than mixing selection and credential bytes.

Migration ordering MUST disable legacy token material before removing its registrations or the new
host reset. A failure at any cleanup/activation boundary MAY leave authentication absent until the
next reinit but MUST NOT reactivate or serve a stale credential.

### R8: Git configuration ownership

Agentworks MUST register its helpers through one Agentworks-owned included Git configuration file.
It MUST NOT use an unqualified `--replace-all credential.helper` that deletes operator-managed
helpers.

For each host managed by Agentworks, the included configuration MAY reset earlier helpers for that
host before registering the Agentworks launcher. Unrelated hosts and operator configuration MUST
remain untouched. When Agentworks manages no credentials, its include reference is absent.

### R9: One declarative write path

Admin users and agent users MUST consume the same material builder and reconciler. Their only
differences are transport and home path.

The imperative `vm add-git-credential` command MUST be removed. Operators declare credentials on the
admin or agent template and run initialization/reinitialization. The implementation MUST NOT add an
installed-state manifest, target-side merge protocol, or another credential writer.

### R10: Security and containment

- Stored credentials remain in mode-0600 Agentworks-owned storage and never enter helper scripts or
  Git configuration.
- Managed-helper programs and metadata MUST NOT contain a delivered secret or a credential derived
  from one; runtime credentials come only from the provider program's target-user dependency.
- Runtime tokens exist only in helper process memory and the Git credential-protocol response.
- Provider-returned scopes, stored protocol fields, managed-helper metadata/content, and generated
  paths are validated at the core boundary before use. Provider-owned executable content is never
  accepted from operator configuration.
- Reconciliation stages files in a private per-user location and does not expose partially written
  credentials.
- One stable shared/exclusive lock protects generation selection, helper reads, activation, and
  cleanup.
- Diagnostics, logs, exceptions, tests, and generated metadata contain no credential values.
- Helpers execute as the target user and use that user's `HOME`, CLI configuration, and active
  identity.

### R11: Operator and authoring surfaces

The implementation MUST update the provider-authoring README, generated resource schema and
examples, sample configuration where applicable, resource/guide teaching, command reference, and
upgrade guidance in the same change that alters behavior.

The permanent provider README MUST lead with the two-shape materialization purpose and describe
stored credentials and managed helpers without making PATs the universal model.

### R12: Future authentication features remain separate

This effort installs consumers of existing `gh` and `az` identities. It MUST NOT introduce secrets
for GitHub Apps or Entra applications, run `gh auth login` or `az login`, mint or renew GitHub App
installation tokens, or add the planned user-feature mechanism.

Future `gh-auth` and `az-cli-auth` user features may authenticate the tools idempotently before
runtime Git use. They consume this contract but are neither required nor implemented here.

## Non-goals

- Explaining or managing GitHub App installation, Azure DevOps organization membership, Entra
  permissions, or forge-side authorization.
- Replacing GitHub CLI or Azure CLI with an Agentworks OAuth client.
- Installing `gh`, `az`, Git Credential Manager, or Git itself as an implicit side effect of a
  credential declaration.
- Supporting SSH Git remotes; this contract serves HTTPS remotes.
- General arbitrary-command credential helpers in operator configuration.
- Caching runtime CLI tokens beyond the CLI's own behavior.

## Acceptance Criteria

### AC1: Configuration and graph

Existing secret spellings remain valid and produce the same secret edge. Each provider receives only
its declared resolved secrets. Explicit `gh-cli` and `az-cli` declarations validate only under their
owning providers, produce no secret edge, and appear accurately in schema/resource descriptions
without exposing identity data.

### AC2: Secret validation

Focused tests prove that secret-backed providers receive their scoped secret context, perform their
own validation/acquisition, and return final stored credentials under the existing enabled/disabled
and skip/partial policies. A synthetic provider proves that several declared secrets may produce one
stored credential without core understanding the exchange. CLI-backed modes receive no secrets and
perform no CLI check during provisioning.

### AC3: Runtime GitHub identity

In a disposable target-user home with a fake and then real authenticated `gh`, a matching Git
credential query obtains the active token at runtime; missing/failed/malformed/timeout cases fail
clearly and leak no value. A live GitHub HTTPS clone/fetch and a reversible write operation pass
under integration-test authorization.

### AC4: Runtime Azure identity

In a disposable target-user home with a fake and then real authenticated `az`, a matching helper
obtains an Azure DevOps-audience token at runtime. Live Azure Repos clone/fetch and a reversible
write operation prove that the emitted Git credential form works with the repository service, not
merely its REST API. Missing/failed/malformed/timeout cases are value-safe; a valid token for an
identity without repository access produces Git's normal forge rejection.

Before this design merges, a ready-PR integration run MUST prove the proposed
organization-username/token-password form with a real read-only Azure Repos Git operation. A failed
or unavailable proof is not approval to begin implementation.

### AC5: Selection

Stored credentials and managed helpers can coexist on one host. Generic longest-path matching
preserves exact-repository, owner/organization, and default outcomes; duplicate nonempty-path
refusal, released first-declared host-default behavior, and no-match behavior remain deterministic.
An embedded remote username MUST NOT override path-based selection; provider-owned remote-review
advisories remain available where the forge gives embedded usernames special meaning.

### AC6: Reconciliation

For admin and agent users, tests start from current legacy files, the new managed layout, mixed
operator Git configuration, and no prior state. Same-input reruns are byte-stable; each add, remove,
scope change, and mode transition converges; empty desired state removes all provably
Agentworks-owned credential/routing state except the inert stable lock while preserving unrelated
helpers and Git config. Any indistinguishable generic `store` helper left behind has no
Agentworks-owned credential file to serve. Concurrent helper/swap/cleanup tests prove one invocation
never mixes generations or loses files while using them. Mixed legacy/new fault injection proves
every mutation boundary is stale-safe, and contention tests prove lock waits and child-descriptor
lifetimes are bounded.

### AC7: Single write path

The old direct-add command and its static-store append/fallback code are absent. Admin and agent
initialization are the only credential writers and converge from their complete declared lists.

### AC8: Shipped collateral and gates

Permanent provider documentation, resource/guide surfaces, schemas, samples, and upgrade guidance
match the final implementation. Focused, full non-integration, static, lint, locked-SDD, Rulesync,
and authorized live GitHub/Azure tests pass with no token in logs or artifacts.

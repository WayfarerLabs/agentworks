# Bootstrap fallback removal: high-level architecture

## Current topology

The manager calls `VMPlatform.create()` and receives a `ProvisionResult` containing a native
transport, platform metadata, an optional Tailscale IP, and `bootstrap_complete`. Phase A branches
on that boolean:

- true: trust the platform bootstrap, rediscover the IP if needed, then verify Tailscale SSH;
- false: generate a full key-bearing bootstrap script, stage it locally and in the guest, execute
  it, then verify Tailscale SSH.

That false branch is both WSL2's planned primary mechanism and failure recovery for other platforms.
The shared representation therefore cannot enforce the provider's chosen credential boundary.

## Target topology

`VMPlatform.create()` becomes a complete-or-raise boundary:

1. The platform creates its backend resources.
2. The platform runs its create-time bootstrap through its declared delivery mechanism.
3. The platform joins Tailscale or raises and rolls back.
4. The platform returns `ProvisionResult(native_transport, platform_metadata, tailscale_ip)`.
5. Manager Phase A records the returned or rediscovered Tailscale IP and verifies Tailscale SSH.
6. Platform-agnostic initialization continues unchanged.

There is no state transition from a failed platform bootstrap to a second bootstrap mechanism.

## Contract changes

### `ProvisionRequest`

The resolved Tailscale key is required. The effective VM template already supplies the default
secret name and the operation boundary resolves it before constructing the request. Making the field
non-optional records the production invariant and removes test-only deferred paths from provider
implementations.

This remains a value-bearing request by design. The Tailscale secret belongs to the effective VM
template, and the VM manager resolves that domain input before dispatch. Platform-config secrets
still follow the separate declared-config path and arrive through the platform's scoped
`RunContext`. Moving the template key into that context would conflate two declaration owners and
make the platform config claim a dependency it does not declare.

Version 2 also carries a narrow bootstrap-progress sink. The capability layer declares the small
value-free protocol WSL2 needs (`step`, `output`, `warning`, and `log_error`); it does not import
the VM manager or concrete `SSHLogger`. The manager creates the existing `SSHLogger` before platform
dispatch with the full Tailscale-key and git-token redaction set, passes it through the request, and
retains lifecycle ownership.

### `ProvisionResult`

Remove `bootstrap_complete`. Returning the result is the completion signal. Keep `tailscale_ip`
optional because a successful join and IP discovery are distinct operations; Phase A may safely
repeat only discovery.

The same rule applies one layer down: delete the shared `BootstrapCompletion` record and make
`EphemeralTailscaleBootstrap.complete()` return only the discovered `str | None` IP or raise.
Readiness and join failures cannot be represented as data anywhere in the create path.

### Contract version 2

The vm-platform descriptor and Lima, WSL2, Proxmox, Azure, and AWS implementations move atomically
from contract version 1 to version 2. Exact registration conformance rejects v1 implementations;
there is no adapter because the old result shape encodes the forbidden fallback state. GCP is
introduced directly on v2.

### Phase A

Collapse `_phase_a_bootstrap` to the completed-bootstrap path. It never receives template bootstrap
inputs and never calls a generated-script runner. It updates the database, constructs the Tailscale
transport, and verifies connectivity.

The manager passes the already-created logger into Phase A and later initialization instead of
constructing a new one there. On create failure or interrupt, the manager closes it and reports its
path without replacing the original failure. On success, the same logger spans WSL2 bootstrap,
Tailscale verification, and initialization as it does today. The resolved key remains in its
redaction set as defense in depth, but Phase A does not consume it as bootstrap input.

## Platform adaptations

### Azure and AWS

Their shared `EphemeralTailscaleBootstrap` readiness failure becomes an exception rather than an
incomplete completion record. The helper returns only the discovered `str | None` IP after a
successful fixed-stdin join. The existing create exception arms then perform the established total
rollback, and best-effort IP discovery remains unchanged.

### Lima

Remove the unreachable no-key branch and incomplete result state. Its credential-free retained
provision script, fixed stdin join, restart handling, and IP-discovery retry contract remain
unchanged.

### WSL2

First extract the existing generated-script executor into a WSL2-owned helper while the v1 manager
still invokes it, with no behavior change. The helper accepts explicit bootstrap inputs plus the
capability-layer progress protocol, performs private local/guest staging and cleanup, parses and
redacts output, and returns the Tailscale IP. It does not update the database, construct or close
the logger, or know about the manager.

At the v2 cutover, `WSL2Platform.create()` invokes that helper inside its existing rollback span
with the request's progress sink. A script or cleanup failure therefore rolls back the distro. The
manager records the returned IP and provisioning status after create returns, using the same logger
for Phase A. The platform returns only after it has a Tailscale IP, without losing the existing
redacted create log or operator progress.

### Proxmox

Treat a timed-out, unsuccessful, or IP-less guest-agent bootstrap as a create failure. The existing
rollback span removes the partial VM. The accepted private staging and cleanup behavior do not
change.

### Future providers

The type surface exposes no incomplete-bootstrap state. GCP and later providers must either finish
their declared bootstrap path or raise, so the #471 failure class cannot be copied from a sibling.

## Failure behavior

Readiness exhaustion, cloud-init failure, join failure, bootstrap timeout, parsed bootstrap failure,
or missing required Tailscale IP all remain inside the platform create rollback window. The original
error owns the reported failure; cleanup warnings retain the existing manual-remediation behavior
and never include the credential.

Successful join followed by IP-discovery failure is not a bootstrap failure. It returns with no IP,
and Phase A repeats only `tailscale ip -4` over the native provisioning transport.

Once create returns, its rollback window remains closed. A later Tailscale SSH verification failure
keeps the backend VM and marks its row `FAILED`, invokes `secure_failed_vm` best-effort, and reports
the established recovery guidance. This effort does not widen backend deletion into Phase A.

`KeyboardInterrupt` during newly owned or existing create-time readiness/bootstrap work follows the
same platform rollback contract as any other create interrupt. Tests cover the original interrupt,
cleanup interruption, and exact manual-removal guidance rather than allowing the generic exception
mapping to swallow or replace it.

Logger creation is not platform realization. If it fails, dispatch never begins. After creation, the
manager owns exactly one close on every create failure/interrupt and the existing downstream close
behavior on success. Platform cleanup errors cannot replace the primary failure, and logger cleanup
cannot replace it either.

## Verification architecture

Provider-shaped regression tests construct real platform requests against bounded fakes and then
exercise the manager/Phase A boundary where practical. They inspect:

- final provider-retained payloads;
- fixed stdin join calls;
- local and guest staging calls;
- rollback calls and surviving resources;
- command text, output, exception chains, and rendered diagnostics.

Structural tests pin the absence of `bootstrap_complete` and the generic generated-script fallback.
WSL2 tests pin that its primary bootstrap executes once inside create and that failures invoke its
create rollback. Version-conformance tests pin the v2 descriptor and all shipped implementations,
plus registration rejection of v1. Sentinel tests cover WSL2 and Proxmox success, bootstrap failure,
cleanup failure, and interrupt paths as well as Azure/AWS readiness failures. The suite remains safe
under PR #469's parallel default. Lima/Azure/AWS fixed-stdin tests separately cover success and join
failure reflection surfaces, with rollback required for the cloud providers.

An end-to-end safe-incomplete test pins the only remaining partial result: a fixed-stdin join
succeeds, platform IP discovery returns none, Phase A repeats only `tailscale ip -4`, records the
result, and never receives bootstrap inputs or touches staging.

## Documentation

The permanent VM-platform README changes with the implementation to describe complete-or-raise
bootstrap ownership, contract version 2, the narrower Phase A role, and the domain-owned Tailscale
request seam. The plugin author example moves to v2, and the general capability README distinguishes
platform-config secrets delivered through `ctx.secret` from consuming-domain operation inputs.

The vm-platform kind and relevant platform `TopicProse` contributions are reviewed and updated where
their operator teaching changes. Guide catalog/rendering tests prove the content remains inert and
safe. These permanent claims land in the atomic v2 cutover commit, not a later documentation phase,
so every commit remains honest. No permanent document points back to this SDD.

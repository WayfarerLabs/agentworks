# GCP native access composition

This lead-owned response completes the private GCP access construction under the
[owned platform boundary](execution-lifecycle-lld.md#cloud-ssh-route-composition). It does not waive
native compatibility, recovery or complete RunContext gates. The existing start producer supplies
one-shot submission and explicit completion; the access owner composes it with fresh instance
evidence and an owned route.

## Components and ownership

`GCEOwnedAccess(vm, platform, ctx, owner=...)` is a passive, single-use preparation owner under the
exact selected VM claim and site. It implements the existing `OwnedNativePlatformAccess` contract. A
reentrant finite transition lock permits the same-thread guest locator callback, not concurrent
credential use. Retain the reader, start producer, source discovery, route and partial guest
preparation before their effects. Neither construction nor accessors acquire credentials, start a
VM, open a route or create another operation owner.

All provider calls borrow the same selected `_OwnedAuth`. Preserve its audited public middleware,
background-copy custody and terminal cleanup. Do not call legacy credential caches, native
transports, transient-route contexts or provider pollers. Keep the old hooks unchanged during
coexistence. New helpers remain concrete GCP code, not a generic cloud task or retry framework.

## Exact instance and endpoint observations

Add a frozen native identity from only the persisted fields this path needs: project, zone, instance
name and canonical positive uint64 ID; network, optional subnet, target tag, external access-config
name and baseline deny name/ID. Validate these cross-execution inputs without rewriting the stored
bytes or silently adopting missing identities. Accept complete constructed Compute URLs on the two
documented Google API hosts and the supported exact project-relative spelling; refuse foreign
origins, escapes, dot segments, extra components and other projects. Network/subnet paths are
observations, not persisted immutable incarnations.

Keep the reader's six-field power/locator projection unchanged. A separate full native observation
requests only that identity/power projection, network-interface network/subnet and named external
NAT fields, tags, and service-account emails. Do not fetch unrestricted metadata or startup-script
contents. Preserve the same 65,536-byte bounded identity-encoded raw reader and original
credential/session custody. The full response must match the selected instance, exactly one
supported NIC, selected network/subnet and tag, and the named `ONE_TO_ONE_NAT` access config. An
absent NAT address on a stopped instance is not an endpoint; any supplied address must be canonical
IPv4. Running preparation requires an actual address.

Return a frozen observation carrying locator, raw power, mapped power, optional endpoint and
attached service-account emails. Network/tag checks are against the frozen selected identity, not
against a cached successful observation. Missing or malformed facts refuse admission. A borrowed
auth accessor returns only the same retained helper. `close_read_client` bars later reads and
retires reader service originals without closing shared authentication; final reader/auth close
remains terminal. Full access keeps authentication until all dependent custody settles.

## Preparation and route checks

Require explicit new SSH settings before provider observation or mutation. Initial stable power
supports RUNNING or stopped GCE TERMINATED/STOPPED, not deallocation or unknown transitions. Before
a stopped start, freshly observe the exact selected incarnation and selected stopped state. Core
already rejects operator-stopped intent. Retain one `GCEActivation`, submit once, and explicitly
observe its retained operation until terminal or the original deadline. A failed request is not
retried. After request success, observe fresh instance identity/power until RUNNING, allowing only
documented startup states and brief unchanged stopped visibility. Request completion never
substitutes for running evidence.

Bind the live endpoint through `ssh_native_binding` with the original configured trust and delivery
account. Retain `OwnedSSHExposure` before resolving the detected and configured canonical IPv4
prefixes. Retain the route before opening it. After opening, freshly observe the instance, endpoint
and route before guest preparation. Use `prepare_managed_vm_target_from_platform` with this access's
fresh locator callback and the existing outer owner; retain escaping partial preparation facts.

Expose `route_check` only after successful preparation while access is open. Each check freshly
observes the exact running instance, unchanged endpoint and route policy. It never learns trust or
selects another target. GCP firewall tags select every instance carrying that tag on the network;
matching the recorded tag is not proof of exclusive tag ownership. Do not introduce project-wide VM
inventory or claim atomic protection against external tag, network or instance replacement.

## One owned firewall rule

`GCENativeRoute` owns one nonce name, one lifecycle obligation and its actual service
response/session originals. Its frozen selection contains the exact VM identity, network/tag,
baseline deny incarnation, at most 64 canonical prefixes, and a new request UUID. Validate the
provider network and baseline before effects: the selected network uses classic-first policy; the
recorded deny has its exact numeric ID and the expected enabled priority-1 all-source ingress deny
shape. An omitted network enforcement-order field follows the documented classic-first default, not
an invented unknown policy.

Inventory classic firewall rules with bounded complete pagination, selected behavioral fields only,
and the same finite deadline. Refuse applicable existing priority-zero allows and operator-SSH
denies, including account-targeted rules matching the freshly observed VM service accounts. Refuse
incomplete inventory, malformed relevant shapes, loops or limit exhaustion. Do not repair, converge,
adopt or delete existing rules. The selected route is an enabled priority-zero INGRESS TCP/22 allow
for exactly the selected tag/network and prefixes, with an operation nonce description. Require its
exact fresh name to be absent.

Register and durably mark possible effect before one insert POST with its UUID. Construct fixed
Compute v1 global firewall and global-operation routes locally. Preserve public credential
middleware, disabled service replay/redirects, streamed bounded raw JSON and original custody before
response selection. Retain a matching Operation acknowledgment before publication, cleanup or
late-budget refusal. Validate operation name, request UUID, action, exact target link and canonical
numeric firewall target ID; subsequent observations must match them. Interpret the same closed
operation statuses and error rules as start completion. A POST response can establish terminal
request evidence only when fully validated, never mere HTTP success. Request completion and fresh
exact rule realization are separate. Missing identity/acknowledgment remains uncertain; do not adopt
a rule later merely because its name or contents match.

Publish bounded versioned canonical payloads containing only selection, request/ operation
identities, closed terminal states and cleanup intent, never raw JSON, diagnostics or credentials.
Reconcile lost registration/publication/resolution replies against the exact row, revision and fence
before further effects. Keep only unresolved publication evidence, not an unbounded success/attempt
history.

## Settlement

Aggregate settlement bars body admission, retires reader/source originals and independently settles
route/start custody, preserving the first escaping control. Pending starts remain explicitly
observable before terminal producer close. Only after all dependents and guest preparation custody
settle may shared auth retire. Memoize positive child settlement so a credential-close retry does
not revisit resolved rows after outer release. Do not stop the VM or close the outer owner.

Route close bars new open/check admission and retries local originals only; it does not claim
provider deletion. Explicit route settlement first retires those originals and reconciles retained
publication. It may observe an acknowledged pending insert/delete under the fresh finite cleanup
budget. Unknown/no-ACK work never replays and is not drained by a 404 rule observation. After known
terminal creation, fresh exact absence can settle; a surviving rule may be deleted only with the
acknowledged numeric ID, unchanged full contents and a fresh fenced intent. Name-addressed DELETE
has no numeric-incarnation precondition: the fresh GET detects known mismatch, not an atomic
provider reservation.

Persist a fresh delete UUID/intent before one DELETE. Retain its exact Operation receipt before
cleanup. Require terminal delete evidence and fresh absence before resolving. A later explicit
settlement may issue one new DELETE only after the previous request is known terminal failed, the
exact owned rule still exists and fresh checks and durable intent succeed. No immediate retry in
that same failing attempt; pending, unknown or missing-ACK deletion is observation/bookkeeping only.
Final resolution also requires positive local original retirement. Do not sweep other nonces, use
broad tuple deletion or equate rule deletion with closing SSH.

## Proof and primary sources

Exercise actual Google-auth/Requests classes with scripted HTTP and fresh SQLite: running and
stopped paths, one POST per admitted action, precise request/target identity, bounded pagination,
live endpoint/tag/baseline changes, close failures, lost replies, original controls and aggregate
auth-last ordering. Include one unified composition using real reader/start/route implementations;
clearly name any guest-preparation stub. Source-only reviews and scripted SDK proof do not establish
native API compatibility, sockets drained, provider retention recovery, guest readiness or public
RunContext delivery.

The provider describes
[firewall insertion](https://docs.cloud.google.com/compute/docs/reference/rest/v1/firewalls/insert),
[deletion](https://docs.cloud.google.com/compute/docs/reference/rest/v1/firewalls/delete), and
[network enforcement order](https://docs.cloud.google.com/compute/docs/reference/rest/v1/networks/get).
They establish the concrete routes, operation identity and documented network default used here. No
immutable-ID delete selector or conditional request is inferred from a preceding read.

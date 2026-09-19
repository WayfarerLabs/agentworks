# Installed OpenSSH carrier

This internal adapter accepts explicit connection policy and a prepared invocation. It implements
buffered byte delivery and owned local forwarding. Shared live-stream and terminal integration is
not implemented yet. Production factories and RunContext still use the existing execution stack.

## Connection policy

`SSHConnection` selects a literal host, port, POSIX account, identity, trust, optional lookup alias
and optional agent socket. Construction performs no filesystem or network work. Each operation
checks local files, admits current trust and checks the selected installed client against the
OpenSSH 8.5 minimum before dispatch. The operation's original deadline is reused throughout. Local
filesystem calls are synchronous; a stalled filesystem call is not cancellable by this budget.
Expiry observed during validation prevents subsequent dispatch.

Every client ignores user/system SSH configuration and disables implicit agents, identities,
certificates, proxies, multiplexing, inherited forwarding and known-host commands. Only the explicit
identity may authenticate. An explicit Unix-domain agent socket can sign for that identity; omitting
it disables agent use. Native Windows agent pipes are not supported. Missing or unsupported policy
refuses rather than discovering an ambient substitute. Installed-client algorithm defaults remain in
effect.

A private identity's sibling `.pub` file must match its independently extracted public fingerprint.
This prevents a stale companion from selecting another key in the explicit agent. A directly
selected public key remains usable with its explicit agent when it has no sibling of its own. A
public identity with a sibling is refused: the lightweight identity reader cannot prove that OpenSSH
will accept the complete direct encoding before its suffix fallback. A private envelope whose public
part cannot be verified locally is refused when a sibling public key exists. No private-key
decryption or passphrase prompt is added.

Paths must be absolute native paths without OpenSSH expansion tokens, quotes or control characters.
Trust paths also refuse symlink/reparse components and parent traversal. On macOS, select canonical
paths when a familiar path uses a system symlink. Manual SSH aliases remain a separate operator
convenience; they are not inputs to this adapter.

`SSHSettings` is the passive optional `[operator.ssh]` configuration value. Loading it neither
creates trust nor enables the new execution path. Composition supplies the endpoint and account;
legacy operator fields continue serving existing callers. The sample configuration documents the
available fields.

## Trust ownership

`SSHTrustFiles` names complete known-host files and an optional revocation file. Their explicit
maintenance owner must keep them stable during each operation. `ManagedSSHTrust` names an owned
bundle; every operation admits its current immutable generation afresh. A reused carrier does not
cache admission.

The `trust` module exposes `import_trust`, `trust_status`, `block_trust` and `refresh_trust`. Import
creates a new destination; refresh requires the expected generation and complete replacement policy.
Copying preserves every source byte, including CA records, aliases, hashes and binary KRLs. OpenSSH
interprets trust. These operations do not discover applicable policy, enroll unknown hosts, edit
source files or synchronize with their writers.

The caller supplies stable source snapshots or pauses their writers. A named maintenance authority
owns subsequent CA/revocation updates. Observed source changes are refused, but file metadata checks
cannot prove consistency against an uncooperative writer. Block admissions as soon as existing
policy is superseded; refresh blocks before copying and only publishes a complete generation. Failed
refresh preserves evidence and keeps new admissions blocked. If storage cannot durably record
blocking, quiesce new use and repair storage before continuing. Retrying never silently reactivates
old policy.

An admitted operation keeps its selected generation for its lifetime. Refresh cannot revoke an
already established session; its owner must end that session when policy requires it. Generations
are retained, including failed publication evidence. There is no automatic cleanup or background
synchronization. Code rollback does not authorize rolling trust back or deleting learned evidence.

Managed storage uses exclusive creation, a permanent operating-system lock, restrictive POSIX modes
and atomic manifest replacement. It requires an operator-controlled local parent directory. It does
not defend against hostile code running as the same local user. Windows ACL and crash-durability
acceptance still requires native validation; POSIX permissions do not establish those properties.

## New-resource enrollment

The `enrollment` module provides `SSHCreationProvenance`, `enroll_new_target` and
`recover_enrollment`. Only trusted creation-flow composition may assert a genuine new provider
resource and its canonical endpoint. Ordinary carrier execution never enrolls. The new composition
binding is still required before this maintenance API enables production use.

Enrollment requires a managed bundle and a finite deadline. It reserves one private candidate
beneath that bundle for the stable creation ID, records the endpoint and current generation, and
creates the primary known-host file exclusively. It makes one first-contact acknowledgment using
`accept-new`, followed by a separate strict acknowledgment to verify retained trust. Both consume
the same deadline and perform no requested application work; account startup hooks can still have
side effects. Successful authentication alone does not prove OpenSSH saved the host key.

Every failed or interrupted attempt retains its directory and any learned key. An existing candidate
can only use strict recovery against the recorded active generation. Blocked or changed policy,
missing metadata and unknown keys refuse. Keep the original bundle and creation ID during recovery;
changing them to obtain another first-contact attempt is not recovery.

A successful `SSHEnrollmentCandidate` is verified evidence awaiting explicit complete-policy
import/refresh, using its expected generation. It is not an enabled connection. Include every
applicable existing CA/revocation source when publishing, reconcile stale policy explicitly and keep
ordinary connections on the managed reference. Never replace that reference with cached generation
paths. The candidate remains after publication as evidence.

## Delivery and forwarding

`SSHCarrier.execute` dispatches once. Captured bytes preserve their provenance; client/guest mixed
stderr is never relabeled as guest stderr. Exit 255 remains ambiguous. Local timeout and process
cleanup do not prove guest termination or authorize replay. Shared preparation and public outcome
interpretation belong above this adapter.

`open_local_forwards` accepts explicit `LocalForward` values with numeric bind addresses and literal
destinations. The returned `OwnedForwarding` is a context manager with `wait()` and idempotent
`close()`. Its startup deadline covers connection and readiness; the returned resource remains owned
until close or client exit. Call close even when wait is never used.

Forwarding uses one foreground client and a held POSIX shell session. It requires compatible
account-shell execution and an available `sh`. A nonce acknowledgment after listener setup proves an
authenticated held session and successful requested local binds. It does not prove destination
health or later forwarding permission. Accounts that prohibit command execution cannot use this
mechanism. Separate IPv4/IPv6 requests must each succeed.

An owned worker drains the client's pipes while the caller holds the resource, retaining no raw
client diagnostics. Closing ends owned stdin, kills/reaps the local client within a bounded
allowance and joins the worker. Unproven local cleanup raises an observation failure. No cleanup
claim extends to a remote process after connection loss.

The [SSH test guide](../../../../tests/execution/carriers/ssh/README.md) distinguishes local fixture
coverage from supported-platform integration evidence.

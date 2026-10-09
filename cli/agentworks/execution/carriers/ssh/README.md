# Installed OpenSSH carrier

This internal adapter accepts explicit connection policy and a prepared invocation. It implements
buffered and live byte delivery plus owned local forwarding. Terminal integration is not implemented
yet. Production factories and RunContext still use the existing execution stack.

The private `_terminal_posix` resource layer admits supplied readable terminal input and a terminal
geometry descriptor, allocates its own PTY and restores borrowed input modes after all users stop.
One retained non-main worker owns acquisition, resize and release; other threads cannot mutate the
resource. The geometry descriptor may be read-only. It starts no client or relay and does not enable
terminal delivery.

The private `_terminal_relay` candidate composes the shared `TerminalInput` with that resource and
`LocalProcessOwner`. One retained worker owns admission, raw mode, the owned stdin PTY, bounded fair
relay and process settlement before restoration. Preparation stalls with `None`; its final empty
chunk hands input permanently to the keyboard after pending payload drains, without closing the
channel. Raw stdout/stderr go to distinct borrowed sinks. The worker uses explicit completion facts
through caller interruption, and reports endpoint or restoration uncertainty without retaining raw
payload diagnostics. TERM comes from the supplied terminal input.

Failed native acquisition preserves its primary exception and typed cleanup cause. The relay reduces
that cleanup fact to observation uncertainty, including a safe note on a propagated control
exception. It suppresses native cause chains at its public boundary so cleanup diagnostics cannot
disclose endpoint data. Ordinary acquisition failures whose cleanup succeeds remain dispatch
failures.

This candidate copies initial geometry and subsequent changes from the supplied output endpoint. The
same worker requests SIGWINCH through its existing process owner, with a finite 0.1-second budget
for each notification and the operation's original absolute deadline as an upper bound. Even an
unbounded operation gives notification a finite allowance. Accepted local signaling is not proof of
remote resize. An unknown notification or interrupted geometry/notification boundary reports
observation uncertainty; an unsent notification uses fresh process and deadline facts. Natural
client exit needs no new geometry and still permits bounded output draining. Claimed notification
custody remains with the shared owner through settlement before terminal release.

It is not enabled by `SSHCarrier`; real SSH resize, native workstations and transport-owned
presentation sanitation remain required.

The private `_terminal_windows` resource layer translates supplied CRT descriptors to borrowed
native console handles. It admits readable console input without consuming events and queries the
explicit output viewport. One retained non-main worker clears processed, line and echo input,
requires virtual terminal input and verifies the resulting mode before returning. Unsupported modes
refuse and attempt restoration. Release attempts the exact original input mode once and returns
restoration uncertainty; it never closes borrowed handles or changes output modes, code pages or
descriptor flags. This resource starts no reader or client. Native Windows launch, byte delivery,
resize, interruption and restoration acceptance remain required before terminal enablement.

The input policy follows Microsoft's
[console mode definitions](https://learn.microsoft.com/en-us/windows/console/setconsolemode).
[Input-event counting](https://learn.microsoft.com/en-us/windows/console/getnumberofconsoleinputevents)
admits readable input-buffer kind separately from mode queries, which also accept output buffers.
Geometry comes from the supplied output's
[screen buffer viewport](https://learn.microsoft.com/en-us/windows/console/getconsolescreenbufferinfo).

## Connection policy

`SSHConnection` selects a literal host, port, POSIX account, identity, trust, optional lookup alias
and optional agent socket. Construction performs no filesystem or network work. Each operation
checks local files, admits current trust and checks the selected installed client against the
OpenSSH 8.5 minimum before dispatch. The operation's original deadline is reused throughout. Local
filesystem calls are synchronous; a stalled filesystem call is not cancellable by this budget.
Expiry observed during validation prevents subsequent dispatch.

A command name selects an installed executable from the caller's PATH once per operation. The
version probe and subsequent client launches use the same absolute selection, including native
Windows executable suffixes. Windows never adds an implicit current-directory search. Relative or
current-directory entries explicitly present in PATH remain operator selections; use an absolute
`ssh_executable` to select a particular installed client. The selected executable and its directory
must remain under operator control for the operation's lifetime. Selection does not download clients
or alter the process environment.

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
and atomic manifest replacement. Shared admission locks allow independent readers to overlap.
Maintenance holds an exclusive lock through manifest replacement, directory flushing and recording
blocked state after failure; readers refuse contention rather than admit an unconfirmed publication.
It requires an operator-controlled local parent directory. It does not defend against hostile code
running as the same local user. Windows ACL and crash-durability acceptance still requires native
validation; POSIX permissions do not establish those properties.

## New-resource enrollment

The `enrollment` module declares `SSHCreationProvenance`, `enroll_new_target` and
`recover_enrollment`, but the maintenance entry points are currently unusable: their probes have not
adopted mandatory caller-held delivery custody. Production creation-flow binding is also
unavailable. Ordinary carrier execution never enrolls.

Enrollment adoption requires an enclosing resource lifetime that retains the candidate-file lock
until the exact native client settles, including pending construction and cleanup. Candidate bytes
must receive their final flush after native settlement; an earlier flush can be followed by late
client writes. Retaining process custody alone does not keep that lock held or establish durable
candidate evidence. Do not use these entry points until that resource lifetime is implemented.

The enrollment contract requires trusted creation provenance, a managed bundle and a finite
deadline. One private candidate belongs to the stable creation ID and records the endpoint and base
policy generation. First contact uses `accept-new`; a separate strict acknowledgment must verify
retained trust before it becomes publishable evidence. Both probes share the deadline and perform no
requested application work, though account startup hooks can have side effects. Authentication alone
does not prove that OpenSSH saved a host key.

Failed or interrupted enrollment must retain the directory and any learned key. An existing
candidate permits only strict recovery against its recorded active generation. Keep the original
bundle and creation ID; changing them to obtain another first-contact attempt is not recovery.
Verified candidate evidence still requires explicit complete-policy import or refresh, including
applicable CA and revocation sources and the expected generation. Ordinary connections stay on the
managed trust reference. Candidate publication does not authorize deleting the retained evidence.

## Delivery and forwarding

`SSHCarrier.validate` accepts EOF, finite and live byte input without effects, and refuses input
modes the adapter cannot deliver. Managed-start composition must call it before committing durable
`possible-dispatch`; transport owns that shared wiring. Connection-file and trust admission,
installed-client probing and process work stay in `execute`, which also calls the validator before
those effects.

`SSHCarrier.execute` requires caller-held `LocalDeliveryCustody` and dispatches once. The same store
covers discovery and command delivery. Unsettled storage refuses admission; unfinished discovery
never dispatches the command. A bounded return can leave construction or cleanup pending. The caller
retains the store and retries `close` with a finite deadline until cleanup is proved complete.
Selected identity and trust files must remain stable through that settlement, including pending
construction. Command admission uncertainty remains unknown even if subsequent local cleanup
settles.

Captured bytes preserve their provenance; client/guest mixed stderr is never relabeled as guest
stderr. Exit 255 remains ambiguous. Local timeout and process cleanup do not prove guest termination
or authorize replay. Shared preparation and public outcome interpretation belong above this adapter.

The carrier advertises `live_stdio` and accepts the shared `LiveInput` and `SinkOutput` modes.
`CarrierIO` validates the published mode shapes. SSH separately admits supported byte input so a new
shared input mode cannot silently become EOF in the pipe adapter. Borrowed sources and sinks remain
caller-owned and are used only for the duration of the attempt. The shared process core handles
bounded reads, partial sink writes and temporary sink stalls while the SSH adapter retains its
environment filter and stream provenance. Delivered output is not retained in the report. Endpoint
failure is reported on its input or output boundary and still performs bounded local client cleanup.

The shared process core owns client construction separately from caller-driven byte I/O. Its
native-platform gates also apply to this adapter. Buffered/live execution retains its exact owner in
the supplied delivery store. Forwarding keeps its separately explicit session owner. Local cleanup
never establishes remote cancellation.

`open_local_forwards` requires caller-held `LocalDeliveryCustody` for installed-client discovery and
accepts explicit `LocalForward` values with numeric bind addresses and literal destinations. The
caller retains discovery custody through failure and bounded cleanup. The returned `OwnedForwarding`
is a context manager with `wait()` and idempotent `close()`. Its startup deadline covers connection
and readiness; the returned resource remains owned until close or client exit. Call close even when
wait is never used.

Forwarding uses one foreground client and a held POSIX shell session. It requires compatible
account-shell execution and an available `sh`. A nonce acknowledgment after listener setup proves an
authenticated held session and successful requested local binds. It does not prove destination
health or later forwarding permission. Accounts that prohibit command execution cannot use this
mechanism. Separate IPv4/IPv6 requests must each succeed.

The shared owner is retained before launch. An owned worker starts inert and may drain the client's
pipes only after shared startup returns, retaining no raw client diagnostics. Closing prevents
further worker pipe access, settles the shared owner and checks worker termination. The local
kill/reap allowance is bounded; process construction and total settlement have no proven hard time
bound. Unproven local cleanup or worker termination raises an observation failure. Cleanup kill
status is not a natural client exit, and no cleanup claim extends to a remote process after
connection loss.

The [SSH test guide](../../../../tests/execution/carriers/ssh/README.md) distinguishes local fixture
coverage from supported-platform integration evidence.

# SSH Trust Import and Maintenance

- Status: Implementation design; platform and live acceptance remain open
- Requirements: [FRD R3](frd.md#r3-preserve-trust-independently-of-code-replacement)
- Transition: [migration strategy](migration-strategy.md)

## Owned policy and operation admission

The new carrier accepts either explicitly owned read-only trust files or a managed trust bundle.
`SSHTrustFiles` holds an ordered tuple of complete known-host files and at most one revocation file.
`ManagedSSHTrust` identifies an explicit local bundle directory. Neither value reads files during
construction. A managed bundle is resolved afresh for each operation, never cached when RunContext
constructs a target. The operation uses the resulting immutable generation for its entire connection
lifetime.

Existing owned files may remain under their existing maintenance owner. Imported operator/system
policy uses a managed bundle with a named maintenance authority and explicit source paths. No source
discovery, SSH configuration evaluation, key parsing, CA-to-pin conversion or background
synchronization is introduced. Legacy callers continue using their existing settings and stores; new
writers never update those ambient stores. The new config and composition must not silently turn a
managed bundle into permanently cached file paths.

Known-host inputs are copied separately and byte-for-byte, including comments, hashed records, CA
patterns, aliases, ports, revocations, CRLF and missing final newlines. One `UserKnownHostsFile`
option lists the selected files. OpenSSH interprets them. `RevokedHostKeys` accepts one file;
preserve its bytes, including binary KRL data, and refuse unsupported multiple-file composition
rather than build a trust parser or silently omit policy.

## Publication and concurrent use

Each managed bundle contains a permanent lock file, an atomic `state.json` manifest, and immutable
generation directories. The manifest records active or blocked state, the selected generation,
source attribution, maintenance authority and file hashes. These are local policy-maintenance facts,
not a job registry or a new authorization system.

Initial import creates a new bundle without replacing an existing destination. The caller supplies
stable source snapshots or stops source writers for the copy. Source-file identity/size/time checks
can reject observed change, but cannot prove consistency against an uncooperative in-place writer.
Snapshot ownership is explicit; import does not seize control of the operator's original files.

A per-bundle lock that refuses contention serializes writers and operation admission. Copy/adapt the
existing local `flock`/Windows byte-range lock pattern without importing legacy execution. Keep the
lock file in place after release so competing processes cannot lock different underlying files.
Process exit releases the operating-system lock; no stale-lock deletion heuristic is needed.

Refresh requires the expected current generation, refusing a stale concurrent update. Under the lock
it first durably marks the bundle blocked, then copies the complete replacement policy into a new
generation, flushes it and publishes the active manifest. A failed or interrupted refresh preserves
old evidence and leaves later admissions blocked. An explicit maintenance operation can also block a
bundle when superseded policy is known but replacement files are not yet available. Recovery
supplies complete current policy; it never automatically reactivates an older generation.

Admission briefly holds the same lock, checks the active manifest and generation integrity, and
selects its immutable files. Missing, blocked, malformed or incomplete policy refuses before SSH
dispatch. Admission does not wait indefinitely for a writer. Already-admitted operations may
continue on their selected generation; a policy refresh cannot retroactively revoke an established
SSH session. Composition must end those operations explicitly if the operator requires that.

Immutable generations avoid holding the lock throughout terminals/forwards and avoid OpenSSH opening
files across different in-place updates. Keep prior generations during coexistence. There is no
automatic collection, reference-count service or background cleanup in this increment.

Use exclusive creation, restrictive owned storage, same-filesystem publication and no symlink or
reparse redirection. Paths live under a trusted operator-controlled parent; this is not protection
from hostile code running as the same workstation user. Flush files before publication and use
platform-appropriate directory durability where available. Windows sharing failures must leave
blocked policy, never trigger remove-then-rename publication. Windows ACL and crash-durability
claims need their own evidence; POSIX mode bits alone do not prove them. If an unwritable store
prevents recording a block at all, report that fact and require quiescence/forward repair rather
than claim durable refusal was established.

## Enrollment and endpoint changes

The [enrollment LLD](enrollment-lld.md) defines the retained per-resource candidate, strict
verification, recovery and explicit publication mechanism.

Only composition's explicit creation provenance can authorize enrollment. It identifies the new
resource and intended canonical host/port/lookup identity. A missing store, connection failure,
changed address or newly created guest on an existing placement host is not provenance. Carrier
execution never enrolls implicitly.

Enrollment uses a separately owned candidate primary file, exclusively created and never reset,
alongside the complete applicable existing policy and revocations. A bounded installed-client
operation may use `accept-new` only for that candidate. Existing mismatches and revocations still
refuse. Keep any written key after authentication failure, observation loss or interruption;
recovery must be strict against retained evidence, not a second first-contact attempt. The
acknowledgment performs no requested application work; account startup hooks can still have side
effects. A strict follow-up checks retained trust before the candidate is reported verified. A raw
SSH status alone is not application proof.

Endpoint recovery remains explicit: independently establish the intended resource, preserve prior
trust/CA/revocation evidence, and bind the correct lookup identity. No automatic rekey, host-key
scan that blesses whichever host answers, or weaker retry is introduced.

## Implementation and evidence

Trust values and operation admission belong in `execution/carriers/ssh/trust.py`; split filesystem
publication into `_trust_files.py` only if responsibility and size warrant it. Connection policy
resolves trust at operation time before constructing the common isolated options. Buffered,
streaming, terminal and forwarding paths use the same admission policy. Optional SCP must use it
too. Configuration loading supplies passive values and leaves filesystem/network work to the
explicit operation.

Required behavioral coverage includes exact multi-file/KRL preservation, no source writes, competing
writers and stale refreshes, process-death lock release, failure at each publication boundary,
blocked/corrupt policy refusal, coherent generations across refresh, and strict recovery after
incomplete enrollment. Installed OpenSSH fixtures exercise CA/alias/port acceptance,
unknown/mismatch/revocation refusal and actual authentication offers. Windows publication and
workstation agent behavior need native evidence. Existing buffered PoC results establish none of
this new maintenance protocol.

Source and state rollback stay separate. Restoring old code cannot erase newly learned trust or
revocations. Keep both legacy and new state usable during additive delivery; if compatibility cannot
preserve current policy, stop for an operator-approved repair. Final code deletion does not delete
the trust generations or other operator evidence.

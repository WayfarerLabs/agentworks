# R3 Applied State and SSH Identity: Low-Level Design

- Status: Implemented and merged
- Date: 2026-08-28
- Last revised: 2026-08-30
- Requirements: R3 in `frd.md`
- Architecture: `hla.md`, R3 applied state and SSH identity
- Storage contract: `store-contract.md`
- Research: `prior-art-research.md`
- Delivery vehicle: one draft-to-ready PR containing this checkpoint and the implementation

## Decision summary

R3 adds two VM-owned, version-1 applied-state codecs outside `agentworks.db`: an empty marker
associating the hardware request already stored on the VM row with a successful create, and an SSH
identity payload recording the configured private-key reference plus either its authoritative
OpenSSH SHA-256 fingerprint or an explicit unverifiable marker. The hardware request is what
Agentworks expected the platform to create. It is not a provider observation of realized hardware.

A standard-library leaf parser extracts the one public blob from a native `openssh-key-v1` private
envelope without decrypting its private section. The same fingerprint function parses the configured
public key independently. Before create inserts a row or asks a platform to apply that public key,
matching verifiable identities are required. The exact check repeats before Phase B overwrites
`authorized_keys`. A non-OpenSSH private envelope remains supported and yields unverifiable
evidence. A malformed claimed-native envelope or unavailable file is a typed error, not unverifiable
evidence.

VM initialization returns explicit authorized-key proof. Its terminal checkpoint writes final init
status, the terminal event, and only successful configuration-snapshot slices whose required
evidence is satisfied, inside one SQLite transaction. Create records the hardware request plus the
successfully written SSH identity; reinit records only the successfully written SSH identity. Failed
lifecycle operations record no new slices.

Operational SSH comparison is a separate VM-domain service invoked at canonical SSH composition
roots before transport construction. It returns structural not-recorded, unverifiable, match, or
drift facts. Ordinary SSH operations require recorded non-drift evidence; reinit alone permits
not-recorded state so historic VMs have an establishment path. Unverifiable state remains usable so
password-protected legacy keys do not regress. The familiar gated VM boundary becomes safe by
default; the one platform-native recovery path receives an explicit boundary that does not claim
canonical SSH proof. VM deletion also remains available without that proof.

No database migration, public repository escape hatch, ssh-agent selection rule, or drift
remediation is introduced.

## Existing seams at HEAD

- `create_vm()` inserts the VM row and desired overlay together, then performs platform create,
  Phase A bootstrap, and `run_initialization()`.
- `reinit_vm()` resolves the persisted declaration and reaches the same `run_initialization()` after
  its activation and secret boundaries.
- `_reconcile_authorized_keys()` currently catches the admin-self write's `SSHError`, warns, and
  returns `None`, which makes success indistinguishable from a swallowed failure.
- `run_initialization()` currently writes status and terminal events separately. Both
  `update_vm_init_status()` and `insert_vm_event()` commit directly.
- `replace_applied_slices()` already supplies partial, atomic replacement with one operation and
  timestamp and joins an enclosing `Database.transaction()`.
- VM backup snapshots desired overlays but do not select or export applied slices.
- Canonical transport factories deliberately own no database access. The manager-level composition
  roots own lifecycle policy and are the right place for applied-state comparison.

## Module boundaries

### `agentworks.ssh_identity`

A pure leaf module owns SSH wire parsing and fingerprint derivation. It imports no database,
configuration, transport, output, or VM lifecycle code. Its public carriers are frozen and closed:

```text
VerifiedSSHIdentity
  fingerprint: str

UnverifiableSSHIdentity

SSHIdentityReadError(Exception)
  kind: invalid | unavailable
  detail: str  # bounded authored diagnostic, never source bytes
```

`UnverifiableSSHIdentity` is a zero-field arm covering recognized private-key armor outside the
native OpenSSH envelope. It does not claim whether a passphrase is present or why an installed SSH
client accepts the legacy envelope. A future cause that needs different behavior or operator
guidance adds a new payload version rather than persisting a constant reason preemptively.

The leaf functions are:

```text
read_private_ssh_identity(path) ->
  VerifiedSSHIdentity | UnverifiableSSHIdentity

parse_public_ssh_identity(text) -> VerifiedSSHIdentity

fingerprint_public_blob(blob) -> str
```

The leaf never raises a VM-domain error. File and parse failures raise its own typed
`SSHIdentityReadError`, which the VM-domain boundary translates to the relevant `ConfigError` or
`StateError`. Its detail names the failed stage and, where useful, a path, byte offset, declared
length, or exception class. It never contains a byte representation, decoded fragment, base64 text,
passphrase, complete or partial file contents, or captured upstream output. The same prohibition
applies to every log and operator-facing diagnostic built from the error.

### `agentworks.vms.applied_state`

The VM domain owns typed payloads, versioned codecs, comparison facts, and policy conversion. It may
import the SSH leaf and the typed repository carriers, but `agentworks.db` never imports it.

Its responsibilities are:

- encode and decode the two R3 payloads;
- reject malformed, extra-field, and unsupported-version persisted payloads;
- compare a current configured private identity with one persisted SSH slice;
- turn file-unavailable, invalid, not-recorded, and drift facts into typed `StateError` or
  `ConfigError` at the relevant operation boundary; and
- build the `Mapping[AppliedStateKey, VersionedPayload]` for a proven lifecycle checkpoint.

### Initializer and manager layers

`vms.initializer.ssh_keys` owns the relationship between configured public/private identity and the
actual `authorized_keys` write. `vms.initializer.driver` owns proof collection and terminal
checkpoint writes. VM and dependent-resource manager composition roots invoke the comparison service
before constructing their first canonical SSH transport.

## Native OpenSSH identity parser

### Input limits

The parser reads at most 1 MiB from a configured identity file. It rejects a larger file before
base64 decoding. The decoded envelope and every SSH string are bounds-checked against the remaining
buffer. The declared public-key count is read as `uint32` and must equal one, matching OpenSSH's
current private-file implementation. No length causes an allocation before it is checked against the
remaining envelope and the 1 MiB cap.

### Armor and envelope algorithm

For a file beginning with `-----BEGIN OPENSSH PRIVATE KEY-----`:

1. Require the matching end marker and permit only ASCII whitespace outside the base64 body.
2. Decode with strict base64 validation.
3. Require the NUL-terminated `openssh-key-v1` magic.
4. Read and skip the cipher-name, KDF-name, and KDF-options SSH strings.
5. Read the public-key count and require exactly one.
6. Read the one public-key SSH string as the authoritative public blob.
7. Validate that the blob begins with one nonempty ASCII SSH algorithm string.
8. Read and bounds-check the final encrypted-private-section SSH string, require it to be nonempty,
   and require exact envelope exhaustion without decrypting it.
9. Fingerprint the complete public blob.

The algorithm intentionally does not constrain the cipher or KDF vocabulary. Their content is
irrelevant to public extraction, and rejecting a future protected native envelope because it names a
newer cipher would turn password protection into a false support boundary.

The recognized legacy begin/end pairs are `RSA PRIVATE KEY`, `DSA PRIVATE KEY`, `EC PRIVATE KEY`,
`PRIVATE KEY`, and `ENCRYPTED PRIVATE KEY`. A size-bounded file beginning with one of those markers
and containing its matching end marker yields `UnverifiableSSHIdentity`. R3 does not decode its
base64, validate DER, interpret encryption headers, or reject other content that the installed SSH
client may accept. Unverifiable is not a validity claim; transport remains the authority for legacy
formats. This deliberately permissive branch avoids putting a second legacy-key parser in front of
password-protected behavior that already works.

A file claiming the OpenSSH marker but failing any native-envelope check is invalid. Unrecognized
text is also invalid. Missing files, directories, permission failures, and short filesystem reads
are unavailable. Both failure classes use `SSHIdentityReadError` rather than entering the successful
identity union.

### Public-key parser and fingerprint

The configured public file must contain one non-comment OpenSSH public-key line. The parser accepts
an optional trailing comment, base64-decodes the key blob strictly, and requires the line's key type
to equal the blob's first SSH string. It does not interpret `authorized_keys` options because the
operator setting names a public-key file, not an authorization-policy line.

The compatible fingerprint is:

```text
"SHA256:" + base64.standard_b64encode(sha256(public_blob)) with trailing "=" removed
```

The private parser never reads the public path or a sibling path. Runtime code never invokes
`ssh-keygen`.

## Applied payload codecs

### Hardware-provenance storage marker, version 1

The typed carrier has no fields and encodes as an empty object:

```json
{}
```

The record envelope already carries operation and timestamp. CPU, memory, disk, and swap remain on
the VM row and are not copied into this marker. Together, the marker and row values record the
provisioning request used by a successful create. They do not claim provider-realized hardware or
detect normalization or inconsistency after the request leaves Agentworks. R5 projects this private
`hardware-provenance` storage key as public `hardware-request` lifecycle evidence and compares the
recorded request with the current declaration. The decoder accepts version 1 with exactly an empty
object and rejects every other version or field.

### SSH identity, version 1

Verified identity encodes as:

```json
{
  "fingerprint": "SHA256:...",
  "private_key_ref": "/configured/path",
  "status": "verified"
}
```

Unverifiable identity encodes as:

```json
{
  "private_key_ref": "/configured/path",
  "status": "unverifiable"
}
```

The two arms are closed and disjoint. The verified arm requires exactly `status`, `private_key_ref`,
and `fingerprint`; the unverifiable arm requires exactly `status` and `private_key_ref`. The
explicit status remains required even though fingerprint presence could appear to distinguish the
arms: inferring status from an omitted field would let malformed persisted data masquerade as a
valid unverifiable record. The reference must be nonempty printable text. The fingerprint must be
`SHA256:` plus the 43-character base64 encoding without padding of a 32-byte digest. A recorded
unverifiable payload has no fingerprint. Absence is represented only by absence of the slice.

Codecs return `VersionedPayload` only after domain validation. Decode failures at the persisted-data
boundary remain strict `StateError`, but distinguish two operator actions. An unsupported version is
version skew and points to a compatible or newer Agentworks release. A malformed payload at the
supported version is corrupt state and retains repair or known-good-backup guidance. Both paths name
the VM without rendering payload values. Backup decodes known payloads before exporting them so
corrupt or unsupported JSON cannot be laundered into an archive that claims the R3 non-secret
contract.

## Apply-time identity proof

### Pre-provisioning proof

`prepare_configured_ssh_identity()` runs in `create_vm()` after declaration and site validation but
before the VM row, backend resource, log, or secret resolution that can mutate state. It reads the
exact configured public content that will populate `ProvisionRequest.ssh_public_key`, derives the
configured private identity, and refuses a verifiable mismatch or an invalid or unavailable public
or private file. A recognized unverifiable private envelope proceeds.

The returned frozen proof carries the validated public text, private-key reference, and the initial
verified or unverifiable identity. `ProvisionRequest` uses that retained public text rather than
reading the public path again. This makes the pre-provisioning comparison cover the first remote
application performed by AWS, Azure, GCP, Proxmox, Lima, WSL2, or another platform implementation.
Phase B deliberately performs a fresh comparison because either configured path may change during
the longer create operation.

### Authorized-key outcome

`_reconcile_authorized_keys()` returns one frozen sum type for the admin-self path:

```text
AuthorizedKeysApplied
  identity: VerifiedSSHIdentity | UnverifiableSSHIdentity
  private_key_ref: str

AuthorizedKeysUnproven
```

The non-admin owner path retains its existing raising failure contract because downstream agent
operations depend on that write. Its successful return may be ignored by current agent callers; R3
records only VM applied state.

Before its remote write, the helper:

1. reads and validates the configured public identity;
2. reads the configured private identity;
3. refuses an invalid or unavailable identity;
4. compares public and private fingerprints when the private identity is verifiable; and
5. refuses a mismatch before constructing or writing the remote file.

If the private identity is a recognized unverifiable format, the helper proceeds with the configured
public key and retains an unverifiable proof. If the admin-self remote write raises `SSHError`, the
helper preserves today's warning classification with a bounded authored summary and returns
`AuthorizedKeysUnproven`. It does not forward captured stderr, command output, or transferred
content through either the warning or zero-field outcome. An SSH failure cannot prove whether the
remote mutation happened before acknowledgement, so the outcome deliberately does not claim that
nothing was applied. The helper does not convert a local configuration or identity mismatch into a
warning.

For the admin-self path, authorized-key reconciliation moves to the last remote mutation in Phase B.
Earlier setup can therefore fail without making the recorded SSH identity stale, while a successful
key write can proceed directly to its stability check and terminal checkpoint. Agent-owned key
reconciliation keeps its current initializer ordering and raising contract.

### Post-write stability check

After Phase B returns and before terminal persistence, the VM-domain slice builder reads the exact
private-key reference retained by `AuthorizedKeysApplied` again:

- verified before and after with the same fingerprint yields the verified SSH payload;
- unverifiable before and after yields the unverifiable payload;
- any change of arm or fingerprint, invalid state, or unavailable state adds an initialization
  warning and marks an existing SSH slice for removal.

This catches ordinary replacement of the configured private path during initialization. It does not
claim atomicity with the filesystem or with OpenSSH authentication. Removal is required because the
remote key write succeeded but the previously recorded identity no longer describes a key the
operation can prove the VM trusts. An unproven write outcome also removes prior SSH evidence because
the remote side effect is ambiguous. No private-file digest or bytes are persisted.

## Lifecycle checkpoint

Replace `is_first_init` with a closed `VMInitializationOperation` value:

```text
VM_CREATE = "vm-create"
VM_REINIT = "vm-reinit"
```

Create-only setup behavior derives from that operation, eliminating two independent create/reinit
signals. `_phase_b_setup()` returns the authorized-key outcome. `run_initialization()` converts the
stable proof into slices and then performs one terminal transaction.

On a successful `vm-create`:

- always include `hardware-provenance` because platform create and Phase B reached a successful
  terminal create checkpoint with the request stored on the VM row;
- include `ssh-identity` only when authorized-key reconciliation and the stability check prove it;
- set final status to `complete` or `partial` from the complete warning set;
- insert the matching `init_complete` or `init_partial` event; and
- replace both proven slices in one repository call and one operation/timestamp.

On a successful `vm-reinit`:

- never create or replace hardware provenance because reinit did not provision hardware;
- include only proven SSH identity;
- preserve any existing hardware marker;
- remove an existing SSH slice when the authorized-key write is unproven or when the write was
  applied but the stability check could not prove the final configured identity; and
- write status, event, supplied slices, and any required SSH removal in the same terminal
  transaction.

On Phase B failure, one transaction writes `failed` plus `init_failed` and no slice mutation. The
admin key write is the final Phase B remote mutation, so ordinary Phase B failure occurs before it.
On terminal-checkpoint failure, the transaction rolls back status, event, replacement, and removal
together. The earlier `init_started` and `in_progress` state remain, along with old or absent
applied slices. A crash or database failure after the remote write is the unavoidable non-atomic
window and retains conservative old or absent state. The checkpoint exception propagates; it is not
caught and rewritten as a second initialization failure checkpoint.

The repository adds one closed `clear_applied_slice(instance_kind, instance_name, key)` mutation. It
accepts one registered key valid for that instance kind, deletes exactly that slice, is a no-op when
it is absent, and joins an enclosing transaction. It is not a public arbitrary-record delete API.

Only `update_vm_init_status()` and `insert_vm_event()` change from direct commit to
`_commit_unless_in_tx()`. Standalone callers retain current commit behavior. No schema change is
needed.

## Operational comparison and policy

### Structural facts

`compare_vm_ssh_identity()` returns a frozen result with:

- state: `not-recorded`, `unverifiable`, `match`, or `drift`;
- VM name.

The comparison consumes the configured and recorded references and fingerprints internally, but does
not expose values that no R3 policy or diagnostic reads. A later inspection surface may add facts
when it has a concrete consumer.

Missing or malformed repository envelopes remain `StateError`, not one of the four facts. Current
file unavailable or invalid also raises a typed configuration/state error before SSH. Every
operator-facing value is bounded and path-rendered. Diagnostics may include safe fingerprints,
paths, offsets, lengths, and authored failure classes, but never private bytes, public blobs,
passphrases, source fragments, or captured upstream output.

Comparison rules are:

| Recorded state        | Current private identity | Fact         |
| --------------------- | ------------------------ | ------------ |
| no slice              | any readable state       | not-recorded |
| verified fingerprint  | same fingerprint         | match        |
| verified fingerprint  | different fingerprint    | drift        |
| verified fingerprint  | unverifiable             | unverifiable |
| recorded-unverifiable | verified or unverifiable | unverifiable |

The configured path text is diagnostic context, not identity. A path change with the same
fingerprint is a match. An unchanged path with a changed fingerprint is drift.

### Boundary policy

One policy function consumes the structural facts:

- ordinary canonical SSH operations refuse not-recorded and drift before transport construction;
- `vm reinit` permits not-recorded so historic VMs can establish evidence, but still refuses known
  drift because rotation/remediation is out of scope;
- unverifiable proceeds, preserving password-protected legacy-key behavior;
- match proceeds; and
- current unavailable or invalid identity always refuses before SSH.

Refusals include bounded recovery guidance without promising remediation: not-recorded points to
`vm reinit`; unavailable or invalid current identity points to repairing the configured private-key
path; drift points to restoring the private identity matching the recorded fingerprint or deleting
and recreating the VM. Platform-native shell may help inspect or recover the guest, but it does not
clear or update persisted drift and is not presented as sufficient remediation.

The existing public `gated_vm_boundary()` becomes the safe ordinary boundary and applies SSH policy
before activation because activation may reconnect or rejoin through canonical SSH. Its current
implementation moves behind a private helper. The one native recovery composition,
`vm shell --platform`, uses an explicitly named `gated_vm_platform_recovery_boundary()` around that
private helper, so a missing or drifted identity cannot disable the escape path. Future callers
therefore get the ordinary safe policy from the familiar API and must opt into the visibly
recovery-only exception.

Composition roots that perform their own activation sequence apply the same policy explicitly before
their first transport construction. The initial audit includes agent create and reinit, workspace
create, session create and resume, VM reinit, VM Git-credential installation, batch session VM
gates, and VM connection verification. Every direct admin or agent transport construction found by
the implementation-time call-site search receives the same treatment. Tests assert
comparison-before-activation or comparison-before-transport ordering for each distinct composition
shape. The low-level transport factories remain database-free and do not grow an optional bypass
flag.

Best-effort readers that already tolerate an unavailable transport do not turn incomplete applied
state into an unrelated command failure. Session status, batch PID repair, and the VM live-resource
query map policy refusal to their existing unknown or skipped result. They do not attempt SSH and do
not report a successful probe. Each command therefore retains its current degraded-operation policy
without R3 inventing a repository-wide rule for incomplete resource graphs or transports.

Create-time Phase A and Phase B are establishment paths and have no applied slice yet, so they do
not run operational comparison. Their configured public/private comparison occurs before the
platform's first public-key application, repeats before the Phase B `authorized_keys` application,
and stabilizes once more before persistence.

Native is a routing fact, not an identity guarantee. AWS, Azure, and GCP native transports may still
use `operator.ssh_private_key`; Lima and WSL2 use provider-local mechanisms. Recovery paths bypass
applied-state refusal because they must remain attemptable, even though a cloud-native attempt can
still fail when the configured identity is wrong. The implementation audit applies this matrix:

| Composition                                       | R3 policy                                                         |
| ------------------------------------------------- | ----------------------------------------------------------------- |
| `vm shell --platform`                             | Explicit platform-recovery boundary; no applied-state refusal     |
| `vm rekey`                                        | Native recovery root; no applied-state refusal before rekey work  |
| VM start and Tailscale native repair/rejoin       | Native recovery root; no applied-state refusal before repair      |
| VM delete and best-effort native Tailscale logout | Cleanup root; no applied-state refusal                            |
| VM stop, describe, and provider status            | No canonical transport; no applied-state refusal                  |
| Create Phase A and Phase B                        | Establishment proof rules, not operational comparison             |
| Ordinary gated and direct admin/agent SSH         | Strict or reinit establishment policy before activation/transport |

A remote cleanup nested inside another resource's delete is not allowed to claim success through a
known wrong canonical identity; the operator can still delete the owning VM through its dedicated
cleanup path. The direct-transport audit tests that `rekey_vm()`, `_ensure_tailscale()`,
`_tailscale_logout()`, and VM delete remain outside ordinary applied-state policy while canonical
callers cannot reach transport first.

## VM backup projection

`snapshot_vm_backup_data()` adds `get_applied_slices("vm", vm_name)` inside its existing database
snapshot. R3 keys are VM-only, so no new owner-tree repository query is added.

Backup writes a separate `instance-applied-state.json` with:

```text
instance_kind
instance_name
key
payload_version
value
operation
recorded_at
```

Each known payload is decoded and re-encoded through its VM-domain codec before output. The archive
manifest advances from version 3 to version 4 and adds `applied_state_count`. `instance-specs.json`
stays unchanged. The native-Windows refusal remains scoped to desired overlays that may contain
plaintext declaration values; non-secret applied records do not broaden it. There is currently no VM
archive restore consumer to update.

## Failure matrix

| Condition                                  | Apply-time result                                | Operational result                                  |
| ------------------------------------------ | ------------------------------------------------ | --------------------------------------------------- |
| No historic SSH slice                      | not applicable                                   | not-recorded; reinit allowed, ordinary SSH refused  |
| Matching native OpenSSH key                | verified slice after successful write/checkpoint | match; proceed                                      |
| Password-protected native OpenSSH key      | verified slice without passphrase                | match; proceed                                      |
| Recognized legacy private envelope         | recorded-unverifiable after successful write     | unverifiable; proceed                               |
| Configured public/private mismatch         | refuse before key write; no new slice            | current comparison uses existing slice              |
| Stale sibling `.pub` beside private key    | ignored                                          | no effect                                           |
| Missing or unreadable current private file | refuse before key write                          | typed refusal before SSH                            |
| Malformed claimed-native private envelope  | refuse before key write                          | typed refusal before SSH                            |
| Admin authorized-key write warns/fails     | partial init and transactional SSH removal       | not-recorded; ordinary SSH refused                  |
| Private path replaced after write          | warning and transactional SSH removal            | not-recorded; ordinary SSH refused                  |
| Fatal Phase B work before final key write  | failed init, no applied mutation                 | old or absent slices remain                         |
| Terminal SQLite checkpoint failure         | rollback final status/event/slices               | old or absent slices remain                         |
| ssh-agent authenticates with another key   | outside proof                                    | configured identity fact only; selection unresolved |

## Verification plan

### Leaf and codec tests

- synthetic native envelopes with bounded strings, exact magic, truncation including immediately
  after the public blob, an empty final private section, oversized lengths, invalid base64, wrong
  key count, and trailing envelope or armor garbage;
- unprotected and password-protected real OpenSSH keys, with runtime output checked against
  `ssh-keygen -l -E sha256` when the tool is available;
- configured public parsing, algorithm/blob mismatch, and comments;
- stale sibling public files proving the private parser never consults them;
- every recognized legacy armor pair, including encrypted traditional PEM and encrypted PKCS8,
  proving legacy bodies and headers are not parsed or rejected by R3;
- missing, unreadable, directory, malformed, and oversized private paths;
- strict payload round trips, extra fields, wrong types, bad fingerprint shape, unsupported version,
  and absence distinct from recorded-unverifiable; and
- assertions that encoded payloads contain no private bytes, public key text, passphrase, or
  row-backed hardware values;
- sensitive sentinels in malformed input and captured transport failures never appear in exception
  text, warnings, logs, or terminal diagnostics, without asserting authored prose wording.

### Lifecycle and transaction tests

- successful create writes the `hardware-provenance` marker and verified SSH slice with one
  operation and timestamp;
- password-protected native OpenSSH create and reinit capture verified identity;
- partial init after unrelated warnings still records proven slices;
- successful reinit replaces SSH only and preserves hardware;
- key-write warning records no new SSH slice and clears prior SSH evidence;
- fatal work before the final key write leaves prior SSH state unchanged;
- configured public/private mismatch reaches no remote write;
- same-path private replacement is detected before checkpoint and clears prior SSH state;
- terminal checkpoint failure rolls back status, event, and both slices;
- failure status and event join one transaction; and
- no migration or historic-row synthesis occurs.

### Boundary and backup tests

- match, drift, not-recorded, unverifiable, current unavailable, and malformed persisted state;
- establishment policy for reinit and strict policy for ordinary canonical SSH roots;
- comparison-before-transport ordering across every distinct direct construction shape;
- VM delete remains available without canonical SSH proof;
- backup exports valid applied records in the same snapshot, increments manifest version/count, and
  leaves `instance-specs.json` compatible;
- malformed selected applied payload fails backup, while malformed state owned by another VM does
  not;
- unsupported selected applied versions fail backup as version skew, with compatible-release
  guidance rather than corruption-repair guidance; and
- applied-only backup remains allowed on native Windows.

### Gates and live evidence

The implementation handoff runs focused tests, the complete non-integration suite, Ruff, formatting,
Mypy, Typer isolation, repository lint, Rulesync, locked-SDD, website, and deterministic-build
gates. Live validation uses a disposable VM and proves create capture, matching preflight,
deliberate same-path identity drift, password-protected native OpenSSH behavior, reinit
establishment, safe delete, and independent residue cleanup. The tester receives the
operator-specific inventory, budget, prefix, and live techniques from the local `agw-test-env`
inventory before it runs.

## Deferred work

- ssh-agent and default-identity selection remain unresolved.
- R3 does not add `IdentitiesOnly=yes`.
- R3 does not rotate, reapply, or otherwise remediate a drifted identity.
- R5 owns human and JSON inspection surfaces plus doctor batch reporting.

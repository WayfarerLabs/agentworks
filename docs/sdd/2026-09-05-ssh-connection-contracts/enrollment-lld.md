# Explicit New-Resource SSH Enrollment

- Status: Implementation design; composition and platform acceptance remain open
- Requirement: [FRD R3](frd.md#r3-preserve-trust-independently-of-code-replacement)
- Owned policy: [trust maintenance](trust-lld.md)

## One candidate per resource creation

Enrollment is an explicit maintenance operation, never part of ordinary carrier execution.
Composition supplies an `SSHCreationProvenance` value identifying the new resource creation and its
canonical host, port and optional host-key lookup alias. The value records composition's assertion;
it does not manufacture authorization or prove provider ownership. Transport must bind a genuine,
stable provider creation identity before enabling the creation-flow call site. Missing trust, a
failed connection, a changed address and creating a guest on an existing placement host are not
creation provenance.

The connection must use a managed trust bundle. Derive one candidate directory beneath that bundle
from the creation identity, rather than accepting a fresh candidate path on every retry. Composition
keeps the same bundle and creation identity throughout recovery. Changing either to obtain another
first-contact attempt is not an authorized recovery path. A new exclusive candidate directory and
permanent lock reuse the trust module's filesystem mechanics. The lock covers enrollment and
recovery; another operation refuses contention instead of observing a concurrent OpenSSH writer.

Record the bound endpoint, creation identity and admitted policy generation in a flushed manifest.
Create one empty primary known-host file exclusively before contacting the target. Any interruption
or failure preserves the candidate directory and bytes; an existing directory can never start
another `accept-new` attempt. Missing or incomplete metadata refuses automatic recovery and requires
explicit maintenance. No enrollment registry or pending state is added to the managed bundle.
Unrelated targets can continue using its active policy.

## Caller-held delivery custody adoption

Transport's shared `LocalDeliveryCustody` retains the native process owner across bounded return or
interruption. Enrollment must also retain the same acquired candidate lock until that custody
settles. Flushing candidate bytes does not prove that an admitted constructor or client has stopped
writing. Releasing the lexical lock on an observation failure would allow recovery with fresh
custody to read or verify the candidate while the earlier client can still write it.

The enclosing creation or maintenance lifetime must hold both resources before writer admission and
provide serialized, explicitly bounded cleanup. Unsuccessful cleanup keeps the lock and native
custody held; pending or lost ownership continues excluding competing recovery. After confirmed
settlement, flush the final candidate bytes before releasing the lock and surface any flush failure
without claiming durable evidence. Evidence receipts, reports and exception causes do not carry
cleanup capabilities. Preserve the one first-contact attempt, strict-only recovery and stable
candidate identity. The concrete enclosing resource consumer remains unresolved, so threading the
shared custody argument alone is insufficient to complete enrollment adoption or enable production
creation.

### Proposed maintenance resource interface

This interface awaits the operator's ownership decision; none of these resource additions is
implemented. SSH supplies the candidate-lock mechanics, while the enclosing creation or maintenance
caller retains the resource before invoking enrollment or recovery:

```text
SSHEnrollmentCustody(delivery: LocalDeliveryCustody)
enroll_new_target(..., custody: SSHEnrollmentCustody) -> SSHEnrollmentCandidate
recover_enrollment(..., custody: SSHEnrollmentCustody) -> SSHEnrollmentCandidate
SSHEnrollmentCustody.close(deadline: Deadline) -> bool
```

Construction is passive and holds the supplied shared storage unchanged. One resource admits one
maintenance operation, including its sequential version and acknowledgment probes. It retains the
same acquired candidate lock on success, failure or interruption until explicit cleanup completes.
The caller does not reuse its delivery storage for another workflow while this resource holds it.
Closing prevents further maintenance dispatch and forwards finite native cleanup to that exact
storage. Pending or lost native ownership returns incomplete cleanup and retains the lock. After
settlement, perform the final candidate flush and unlock; flush failure remains explicit and cannot
produce a successful cleanup claim. A finite deadline bounds cleanup observation, not underlying
filesystem syscalls or process construction. The existing immutable candidate receipt remains
evidence only, with no live resource attached. A later strict recovery uses a new resource with the
same bundle and creation identity after prior cleanup releases the lock.

The caller attempts explicit close with a fresh finite deadline on both success and failure, and
retains the resource when close reports incomplete cleanup. If maintenance already raised a control
exception, a native-cleanup or final-flush error must not replace it. The enclosing caller preserves
the original exception and separately records sanitized cleanup failure or incompleteness, retaining
custody until it can complete or explicitly handle that failure. Without an existing failure, the
cleanup error remains explicit. Reports, exception causes and diagnostic notes contain no live
cleanup capabilities; ordinary `finally` code that masks an earlier interruption does not satisfy
this contract.

## Authentication and retained trust are separate observations

The initial bounded installed-client operation uses the candidate as its first known-host file,
followed by the complete admitted existing policy and optional revocations. Only this narrow
operation selects `StrictHostKeyChecking=accept-new`; all other isolation, identity and installed
client rules remain the same. OpenSSH interprets the complete policy using its native matching and
revocation rules. Combining files is not an intersection of independent approval policies.

The requested command prints one generated nonce and exits. Exact acknowledgment and normal exit
prove that the requested command responded through an authenticated session, not that trust was
saved. In particular,
[OpenSSH can continue after failing to append a host key](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/sshconnect.c#L1198-L1210).
Flush the retained candidate and perform a separately bounded, strict-only acknowledgment through
the same complete trust selection before reporting verification. Both connections consume the same
original finite deadline; an unbounded deadline refuses before creating a candidate. The second
connection verifies trust; it does not replay an application command. Account startup hooks still
execute and can have side effects. This operation performs no requested application work, rather
than promising no remote side effects.

Authentication failure, unexpected output, timeout or interruption retains whatever OpenSSH wrote.
Recovery resolves the managed reference again, requires the recorded generation to remain active,
and performs only strict verification. It cannot clear the candidate, re-enable first contact or
silently use an earlier policy generation. Changed or blocked policy requires explicit maintenance
to reconcile retained evidence with complete current policy. An empty candidate can succeed only if
existing applicable policy already permits the target; otherwise strict verification refuses.

## Publication and ordinary use

Return an `SSHEnrollmentCandidate` receipt naming retained evidence and its policy generation, not
reusable `SSHTrustFiles` containing resolved managed paths. Those raw paths would bypass a later
block or refresh of the managed bundle. The receipt is not an ordinary connection or permission to
cache the admitted generation.

Publication uses the existing explicit import/refresh maintenance surface with complete policy,
including the retained candidate and every applicable CA/revocation source. Refresh requires the
expected generation. A stale generation refuses; maintenance must reconcile policy rather than
silently publish an old snapshot. Ordinary connections keep the managed reference and admit its
current generation on every operation. Verified-but-unpublished candidates remain explicit local
evidence, not an enabled production target.

## Required evidence

Cover matching provenance, mismatched endpoint refusal before network work, one exclusive candidate
per creation, concurrent initial/recovery operations, retained keys after authentication failure and
interruption, strict recovery after empty or partial attempts, blocked/changed policy refusal and
strict verification after a successful acknowledgment but failed key persistence. Use installed
OpenSSH and fixture-owned sshd for actual writes, mismatches, CA acceptance and revocations, with
fault injection supplementing publication failures. Native Windows and macOS observations remain
required separately. Transport owns proof of the creation-flow provenance and final publication
binding before production use.

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

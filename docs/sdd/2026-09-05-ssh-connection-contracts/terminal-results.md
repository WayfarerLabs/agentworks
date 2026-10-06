# Terminal Implementation Evidence

This SSH-owned record tracks implementation evidence for the [terminal LLD](terminal-lld.md). It
supplies no new requirements and does not replace the [Phase 2 plan](plan.md).

## Shared terminal dependency adoption

Transport's
[draft WIP checkpoint](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010031679)
publishes `d9315847cd2c2d6300bc90f9cf3d20789ac6b973`. The actual shared `TerminalInput` carries
explicit native input/geometry descriptors, terminal type and preparation-owned bootstrap source.
`PreparedTerminalHandoff.carrier_io()` binds the readiness collector separately from trusted client
diagnostics. The existing held process owner accepts explicit EOF, owned pipe or a borrowed native
stdin descriptor. Construction remains passive and the caller retains the borrowed descriptor until
owner settlement.

All 100 carried SSH commits rebase cleanly at `1ad77f6205470fecd8cee231b54e6d39bf3f5609`. The actual
stdin adaptation is `2424b78ae314fd0572acd7fb145e90a898213ff7`: forwarding selects
`LocalProcessInput.PIPE` instead of the old boolean, and its existing lost-reaper test supplies that
same shared value. The lead's non-integration SSH selection passes **302 tests with 5 skips**;
focused Ruff and formatting checks pass. Independent project and complexity reviews clear this exact
code pin using source-only checks. At `88d9eb5720a36c994ca711c0d2564b2ee6d49088`, the shared
terminal-refusal test's SSH fixture uses the current explicit trust shape and patches actual
admission. All four refusal cases pass; the same independent lanes clear this incremental
correction. The final fixture correction at `bf81e89288f7d1eef51b263118fca13c3b95accc` uses native
absolute temporary paths and selects the SSH case for Windows CI. Both lanes clear it and all four
cases pass again. No transport runtime or refusal invariant changes. Ruff/format cover 1,256 Python
files and strict mypy passes 1,215 sources. The full combined non-integration suite at `88d9eb572`
passes **14,511 tests with 50 skips**. File quality, locked-SDD, Rulesync and typer isolation pass;
website validation passes 160 Python and 103 Node tests, with four builds and both deterministic
comparisons exiting 0. Final-fixture hosted CI remains pending. All transport
[hosted CI jobs](https://github.com/WayfarerLabs/agentworks/actions/runs/37418912345) pass at its
published pin; fresh native correction evidence remains pending.

The SSH carrier still refuses terminal input before trust admission or dispatch and leaves terminal
capability disabled. Its resource layer and older native preparation experiments retain their
recorded scope. The new shared types neither establish a usable SSH relay nor production RunContext
delivery. A scoped developer is implementing the independent POSIX relay and retained-worker
mechanics against these actual types.

The
[resize coordination](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6010061812)
identifies a remaining launch boundary: the shared owner currently exposes pipes and completion
facts, but no owner-mediated notification of the exact client's geometry change. SSH will not reach
into private process state, introduce another process owner or claim full terminal delivery without
that step. Transport
[accepts ownership of the notification](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6010143834)
and is preparing its reviewed pin. Retained worker/client settlement through interruption, bounded
presentation and native Linux/macOS/Windows workflows remain required. No public SSH feedback/fix
round or merge-readiness signal follows from this dependency adoption.

## Fixture boundary diagnosis

The first combined local run failed because its long workspace temporary paths exceeded Unix socket
limits and inherited default/access ACLs plus setgid directories. Removing inheritance only on an
owned private fixture root corrected guest identity, file-gate and native-file cases. Local download
publication still correctly refused shared workspace ancestors, whose permissions allow stage
replacement. A short private temporary root outside the checkout lets all 14,511 cases pass without
changing runtime checks or shared workspace permissions. The final fixture-only correction has
separate focused coverage; the full count above belongs to its recorded earlier pin.

## Native Windows boundary investigation

Source-only scouting of Microsoft OpenSSH v9.5/v10 and the actual shared launcher/preparation
identifies a Windows-specific boundary. The client's
[geometry query](https://github.com/PowerShell/openssh-portable/blob/v10.0.0.0/contrib/win32/win32compat/misc.c#L459-L478)
uses its stdout console handle, while the shared owner supplies stdout/stderr pipes. Its
[raw-mode setup](https://github.com/PowerShell/openssh-portable/blob/v9.5.0.0/contrib/win32/win32compat/console.c#L76-L249)
uses the attached console. Borrowed native stdin alone therefore does not establish the required
Windows geometry and caller-console ownership behavior.

An owned ConPTY is a mechanism to investigate. Microsoft's
[channel contract](https://learn.microsoft.com/en-us/windows/console/createpseudoconsole) is UTF-8
text with VT sequences, whereas current preparation supplies binary framing and NUL/control-byte
readiness. Byte preservation through that combination is unproved, not an observed product failure.
The
[joint boundary question](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010272043)
returns launch/preparation feasibility to transport. No new wire format or process owner is selected
by this record. Windows remains required, with native proof of payload secrecy, geometry/resize,
interruption and restoration still open.

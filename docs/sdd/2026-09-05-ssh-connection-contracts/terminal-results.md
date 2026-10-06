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
comparisons exiting 0. All SSH
[hosted checks](https://github.com/WayfarerLabs/agentworks/actions/runs/37421725685) pass at the
published draft head `0e29a5389c2aaff345cdce30d75fa565906c833b`, including the final fixture
correction. This hosted result precedes the relay and Windows resource increments below. All
transport [hosted CI jobs](https://github.com/WayfarerLabs/agentworks/actions/runs/37418912345) pass
at its published pin; fresh native correction evidence remains pending.

The SSH carrier still refuses terminal input before trust admission or dispatch and leaves terminal
capability disabled. Its resource layer and older native preparation experiments retain their
recorded scope. The new shared types neither establish a usable SSH relay nor production RunContext
delivery. The independent POSIX relay and Windows caller-resource increments below use these actual
types while the public terminal feature remains disabled.

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

## Retained POSIX relay increment

The developer's relay at `473c15da40dce248f4fe8d715815a279fab6b809` and acquisition correction at
`625ac6f48096dee99f7f4b22b712ac09e162ca4b` are integrated as `c8bbe899f` and `a552f3e74`. The
candidate retains a native worker before admitting effects, passes the owned PTY slave to the
existing shared process owner, and drains preparation input before admitting keyboard bytes. It uses
separate bounded stdout/diagnostic delivery, stops descriptor use before process/resource
settlement, and retains the first observed control exception through interrupted caller waits.
Geometry changes still refuse with observation uncertainty until the actual shared resize seam is
available. The carrier does not advertise or call this candidate yet.

Independent project and generic review identified acquisition-cleanup uncertainty that could be lost
when the public relay suppressed raw native exception chains. The corrected primitive retains a
typed cleanup cause; the relay reduces it to safe observation evidence and preserves the original
control exception with a safe uncertainty note. Prior native cause/context remain observable at the
primitive boundary. All three lanes clear the corrected pin. The developer records **346 SSH tests
passed with 5 skips**, including acquisition/rollback cases and isolated subprocess evidence for
inherited output handles and bounded post-exit drainage under stalled sinks. These cases use owned
local PTYs and synthetic children, not real SSH terminal acceptance.

Source-only follow-up confirms the shared presentation adapter owns emulator sanitation. The current
readiness collector's `finish()` only finalizes protocol evidence; the future shared execution
wrapper must also settle selected native presentation cleanup. The
[coordination record](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010654402)
returns that gate to transport. SSH neither adds reset bytes to the readiness stream nor imports the
legacy global-stdio terminal guard.

## Borrowed Windows caller resource

The developer's native resource at `a94517d39db5b445ea52dcd2a740ffbe71f48a12` and cleanup at
`4eee1cb4bd17285b08268949fb7480a2cd77934e` are integrated as `f4a28a1cf` and `d2eb2ee6d`. One
retained non-main worker admits explicit CRT console descriptors, queries the output viewport, and
snapshots/restores the input mode. Raw input requires VT input and disables processed, line and echo
handling. Borrowed handles, output modes, code pages and descriptor flags remain untouched.
Restoration attempts and uncertainty are retained once; acquisition preserves its primary failure
and cleanup cause. The resource launches no client and reads no keyboard events.

Project, complexity and generic source reviews clear the exact cleanup pin. The developer records
**104 focused synthetic tests passed**, strict mypy for Linux and Windows, Ruff/format and file
quality checks. The Windows tests use fake Win32/CRT boundaries and run in the ordinary suite; they
provide no native console acceptance. Production Windows child-terminal/preparation selection,
native restoration, resize and interruption evidence remain required.

## Integrated increment validation

The lead independently validates the combined Python tree at
`d2eb2ee6d16a32f2e4df16d723edb9006b7ddc18`: **14,591 non-integration tests pass with 50 skips**.
Ruff/format cover 1,260 files and strict mypy passes 1,223 sources. File quality, locked-SDD,
Rulesync and typer isolation exit 0. Website validation passes **160 Python and 103 Node tests**;
four builds and both deterministic comparisons exit 0. All 25 completed plan records remain
unchanged. The suite uses a short private fixture root outside the shared checkout for the custody
checks described above. These are local gates and synthetic/native-primitive fixtures, not complete
Linux/macOS/Windows terminal or production RunContext acceptance. Hosted CI for this increment
follows publication; the earlier green hosted result belongs to its separately recorded draft pin.

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

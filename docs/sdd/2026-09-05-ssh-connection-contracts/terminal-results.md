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
identified a launch boundary at `d9315847`: the shared owner exposed pipes and completion facts, but
no owner-mediated notification of the exact client's geometry change. SSH will not reach into
private process state, introduce another process owner or claim full terminal delivery without that
step. Transport
[accepts ownership of the notification](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6010143834)
and now
[publishes its reviewed pin](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010798164)
at `833f1c280cc67f8d9b8f71f2e229ab69d20f7df5`. All 110 carried SSH commits rebase cleanly as
`a975ce65f418c2e8ec1e95968f5930acdc0321fc`. SSH is integrating the actual finite-deadline
`notify_resize()` operation. `REQUESTED` reports an accepted local signal, not remote geometry;
`NOT_SENT` and `UNKNOWN` preserve their actual uncertainty. Retained worker/client settlement,
bounded presentation and native Linux/macOS/Windows workflows remain required. No public SSH
feedback/fix round or merge-readiness signal follows from this dependency adoption.

The lead validates the rebase at `12306a4f2`: **423 affected non-integration SSH/process-owner/
maintenance cases pass with 5 skips**. Full Ruff/format cover 1,262 files and strict mypy passes
1,225 sources. File quality, locked-SDD and Rulesync also pass. These checks precede the actual
relay resize integration and owned Windows native fixture under development; they are not a fresh
full-suite or native terminal result.

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
All [hosted checks](https://github.com/WayfarerLabs/agentworks/actions/runs/37424299777) now pass at
the published increment `a767aebdfd4165bab1ac163d693b9f7318f0de45`. This result precedes the
subsequent trust-proof fixture and resize-dependency rebase.

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
interruption and restoration still open. Transport's
[response](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010798696) accepts
native launch/status/cleanup and preparation ownership while keeping the mechanism open pending
narrow proof on the selected Windows 9.5/10.x clients. Server 2022 remains within scope; the latest
CI image is not permission to raise the supported Windows floor. An SSH-owned primitive
console/polling proof fixture is being prepared independently, without claiming host keyboard
translation, ConPTY or complete terminal delivery.

## Private resize and native-fixture batch

The POSIX resize developer's `63ec86147a1cd55e1ad3c8fac2c2981c6874e214` uses the actual shared
notification API. Its finite local allowance is capped by the original operation deadline. Unknown
notification retains observation uncertainty; an unsent notification uses fresh owner completion
facts. Fifteen cases include two observed geometry changes and local SIGWINCH delivery to an owned
synthetic child, deadline races and retained settlement. This is local signal evidence, not remote
SSH resize. The developer reports 396 SSH tests passed with 5 skips and local quality gates.

The Windows developer's `f3d8cf8c1a3eeeb44ed637b2451cb06bfa7ac44e` adds an owned hidden-console
fixture. It measures raw-mode restoration, unchanged output mode/code pages/handle flags, empty and
non-key polling, injected UTF-16 units and exact child/window cleanup. Linux proves the external
record ABI; it skips the native case. Physical keyboard translation, a production reader, SSH/ConPTY
and native Windows acceptance remain unproved.

All three independent source lanes identify the same material startup-custody gap in the Windows
fixture at the combined private pin `57139913e1a3a888d3f88de10140be12c28241b8`: both supervisors
start a worker before retaining startup interruption. A late start exception could abandon parent
supervision or allow descriptor cleanup while the child worker still borrows them. The correction is
required before publication or native execution. These reviews otherwise find no material issue in
the resize unit; they do not clear this combined fixture batch.

The full local suite at that pin reports **14,617 non-integration tests passed with 51 skips**;
Ruff/format cover 1,265 files and strict mypy passes 1,228 sources. The native console case is one
of the skips. These gates do not establish interrupted startup safety or native acceptance. The
short private fixture root was independently checked for remaining process/descriptor use and
removed after the run became terminal.

Transport subsequently publishes the shared bootstrap/file-packaging increment at
`73c75d38a61c76c51b7f0a33a388f4f18fd0d1ba`. All 114 carried SSH commits rebase cleanly onto it at
`2b27c5050d3cb45e914750f65e11ffb57de9aab4`, with an identical SSH source/test tree. The earlier
full-suite count belongs to the earlier shared dependency. Fresh combined validation and the fixture
correction remain required; this rebase raises no public review/readiness signal.

The Windows correction at `e8c455d13e20f4acc4ca9c06e4a0076f6ce9b81a`, integrated as
`76ab1e75bf64cf438c3ad5659918ceb45a2a170f`, puts both supervisors under one inert admission,
cancellation and explicit completion guard. Four synthetic cases cover failed startup before/after
thread creation, delayed canceled tails, interrupted completion waiting and cleanup ordering. All
three independent source lanes clear that exact combined pin and confirm the earlier finding is
resolved. This is private-increment clearance, not approval of the full implementation.

Fresh combined validation at `76ab1e75b` passes **14,715 non-integration tests with 51 skips** (exit
0), Ruff/format (1,275 files) and strict mypy (1,238 sources). The developer's correction selection
passes 109 tests with one native skip, including strict Linux/Windows type checks. The lead's
website gates at the rebased `2b27c5050` pass 160 Python and 103 Node tests; four builds and both
deterministic comparisons exit 0. Locked-SDD, Rulesync, typer isolation and final file quality
exit 0. The lead also passes strict Windows typing on all four resource/native-fixture files and
collects the one native case selected by Windows CI without running it. All 25 completed plan
records remain unchanged. The private suite root is independently checked for remaining
process/descriptor use and removed after completion. Native Windows execution follows publication,
rather than being inferred from these synthetic checks. The terminal feature remains disabled and
full Phase 2 gates remain open.

## Native failure evidence and installed POSIX proof

The first hosted native Windows run at published `5facf665c12c72bf0f2d8eb0a26491a8b83f1fbb`
[fails](https://github.com/WayfarerLabs/agentworks/actions/runs/37428911658/job/112154892043) only
`test_owned_console_resource_and_nowait_records`: its owned child returns 1. The job measures 815
passed and 59 skipped; all Linux matrix and other hosted gates pass. The failed assertion omits the
child's retained records and stderr, and no artifact exposes them. The cause is unknown; this result
neither establishes a polling/restoration defect nor closes native primitive acceptance.

The developer's `c9586c8c7c68488aeb9df307f4868eb755715842` adds bounded fixture-owned failure
diagnostics. Original exceptions and native assertions remain intact. Synthetic failure cases verify
that controlled child/parent observations survive cleanup. The next hosted run must supply the
native diagnosis; better diagnostics alone are no behavioral correction.

The POSIX developer's `bc9b887eb5b91afdb60068726ee407b0b5960d3d` supplies two Linux integration
cases through actual shared preparation, strict admission, installed-client version probing and the
private relay, all under the original 120-second operation deadline. The success case measures both
readiness gates, withheld keyboard input, exact sensitive-byte delivery and two remote geometry
changes. It checks client reaping, closed pipes, owned PTY closure and borrowed terminal restoration
before failure cleanup. The refusal case selects an unavailable runtime and checks that setup cannot
send payload or keyboard bytes. Guest cleanup observes the reported UID/PID/start identity within
its own finite bound; it never signals that PID or treats parent reaping as guest absence.

All three independent source lanes clear combined private
`fedaf2e8e9339d5212405f2d0ae69956fc67975e`; they independently refute the concern that fallback
fixture cleanup could convert a failing owner/restoration assertion into a passing case. Its focused
SSH gates measure 415 passed with 6 skips, full Ruff/format and strict mypy, selected Windows typing
and file quality, all exit 0. The eight new authentication/POSIX integration cases collect; none has
run. The [trust proof record](trust-workflow-results.md) specifies native account/home and fixture
custody prerequisites. Source review does not establish native terminal acceptance.

Transport publishes WSL hold/query/probe named-admission adoption at
`75a59e15a3ea2b1a7ba371fd4c0028c3bf7b62af`. All 123 carried SSH commits rebase cleanly onto it at
`60c1fa3843c6ad45433da92ffba83349a8b1a968`. The SSH source/test tree is identical to the reviewed
batch after optional grouping cleanup; the process-owner resize seam is unchanged. Successful native
admission, shared Windows launch/preparation, presentation sanitation and additive production
RunContext remain open. Terminal capability remains disabled.

Fresh combined validation at `60c1fa3843c6ad45433da92ffba83349a8b1a968` passes **14,801
non-integration tests with 51 skips** (exit 0), Ruff/format (1,288 files), strict mypy (1,251
sources), typer isolation, file quality, locked-SDD and Rulesync. Website gates measure 160 Python
and 103 Node tests; four builds and both deterministic comparisons exit 0. All 25 completed plan
records remain unchanged. The owned suite/build roots are independently verified unused and removed
after terminal completion. Publication follows these local gates; hosted Windows diagnostics and
native authentication/POSIX execution remain separate pending evidence.

## Direct Windows fixture interpreter

Hosted CI at `0b0232cb3d8369e531dbd02e3f74d0fdf67f2947` passes all Linux Python 3.12/3.13/3.14 and
other gate jobs. Its
[Windows job](https://github.com/WayfarerLabs/agentworks/actions/runs/37432194171/job/112165424223)
fails only the owned-console case, with 815 passed and 59 skipped. The new diagnostics expose a
parent-launched PID distinct from the interpreter PID and two console members. The assertion fails
before opening native descriptors or executing either primitive measurement case. Window absence is
independently observed; console API behavior is still unproved.

CPython's Windows virtual-environment launcher spawns the interpreter and waits for it. The fixture
correction uses CPython's existing
[multiprocessing launch shape](https://github.com/python/cpython/blob/4061bc4c35f7c26f25264666d4ba083b93d2f6f9/Lib/multiprocessing/popen_spawn_win32.py#L60-L79):
direct base interpreter with a child-only launcher variable preserving virtual-environment identity.
It retains isolated startup and checks executable/base executable, both prefixes, imported resource
path and isolation flags before descriptor effects. Exact sole-console-client, PID, reaping and
window cleanup assertions remain. No second process owner, package loader or broader PID admission
is added.

All three source lanes clear final `01becba6f8062aeffa46b058ed03121298814102`. Generic review
identifies inherited environment values in fake-test assertion operands at the initial correction;
the final test uses only synthetic environment data, closing that diagnostic exposure. The lead
measures **420 non-integration SSH tests passed with 6 skips**, full Ruff/format (1,288 files),
strict mypy (1,251 sources) and selected strict Windows typing (four files), all exit 0. Production
source is unchanged from the preceding full-suite/CI pin. The previous full-suite and website counts
retain their earlier scope. Native Windows startup, import identity and primitive acceptance require
the next hosted run; neither source verification nor synthetic checks establish them.

## Numeric-helper dependency rebase

Transport publishes private numeric gate/snapshot bootstrap context and payload-free conformance
diagnostics at `a128c09eb37986ba4264ce041d39d5b2f5fbebaa`. GitHub reports #832 as conflicting after
the dependency advances, and no hosted run is created for the directly launched interpreter
publication `d70d9ffb7240f01f2ecf3f36ae123f0f49830483`. No native result exists at that pin.

All 127 SSH commits rebase onto the new dependency at `95323caecb567a6a4c5d046ed1716193e0ffacdc`.
The sole conflict in SSH conformance combines transport's payload-free diagnostic canary with SSH's
literal-environment tests and imports. All three independent source lanes clear that exact
integration: both tests retain their assertions and deadlines, production SSH is unchanged and the
Windows correction files are identical to their previously reviewed versions. Fresh combined gates
and publication follow; native Windows startup and primitive measurements remain unproved. The
numeric context does not complete operation-owned binding, other helper families, native credential
transitions or additive RunContext.

Complete combined validation at `95323caecb567a6a4c5d046ed1716193e0ffacdc` passes **14,841
non-integration tests with 51 skips** (exit 0), Ruff/format (1,289 files), strict mypy (1,252
sources), typer isolation, file quality, locked-SDD and Rulesync. Website gates measure 160 Python
and 103 Node tests; four builds and both deterministic comparisons exit 0. All 25 completed plan
records remain unchanged. Owned suite/build roots are independently checked for remaining process
and descriptor use and removed after completion. The publication adds only SSH evidence to this
Python tree; new-head hosted/native results remain pending.

## Installed Windows resource identity

Hosted CI at `467ef0dd9e7f4914a580b666b17492dca0283d11` completes all Linux Python 3.12/3.13/3.14
and non-Windows gates successfully. Its
[Windows job](https://github.com/WayfarerLabs/agentworks/actions/runs/37436598649/job/112179822374)
measures one exact launched/interpreter console member, PID 9668, matching the hidden window owner.
Virtual-environment executable/base executable, both prefixes and isolation flags match; independent
cleanup observes window absence. The sole test failure occurs before descriptor effects: pytest's
parent imports the checkout resource while the isolated child imports the installed resource. The
job does not establish equal source bytes, and no primitive measurement case runs.

The correction permits that measured storage-origin difference while retaining exact interpreter,
prefix, installed-location and source-byte identity. It records both origins and SHA256 digests; the
child must import the exact candidate pure-Python installation path inside its environment.
Admission remains before console descriptors, with independent parent verification. Synthetic cases
refuse changed source bytes, stale digests, wrong imports and outside-prefix locations. No loader,
path injection, second process owner or broader console admission is added. The native command pairs
installation without editable links with `uv run --no-sync`, avoiding automatic editable
resynchronization.

All three independent source lanes clear combined code/test pin
`b513b80a44bf582ffd29ce72eeb4b3e098204080`, rebased cleanly onto transport
`e0ad14ded9265238fa19cad972f92cc047548111`. The rebase changes no SSH source or tests. Transport's
private file-family numeric context and durable codec still await operation/workflow/recovery
binding; they do not complete production composition. Before that rebase, focused SSH validation
measures 424 passed and 6 skipped; full and Windows-specific typing pass. Complete combined
validation at `b513b80a44bf582ffd29ce72eeb4b3e098204080` measures **15,100 non-integration tests
passed with 51 skips** (exit 0), Ruff/format (1,292 files), strict mypy (1,255 sources), typer
isolation, file quality, locked-SDD and Rulesync. Website validation passes 160 Python and 103 Node
tests, four builds and both deterministic comparisons. All 25 completed plan records remain
unchanged. Native Windows byte-equivalence admission and primitive measurements require the next
hosted run; physical keyboard behavior, supported terminal delivery and additive RunContext remain
open.

## Native owned-console primitive acceptance

[Hosted CI](https://github.com/WayfarerLabs/agentworks/actions/runs/37439026161) at
`968b19f32203905f698c76496ffc046fd4c364b9` completes all Linux Python 3.12/3.13/3.14, Windows Python
3.13, static/file/SDD/Rulesync/website and aggregate gates successfully. Its
[Windows job](https://github.com/WayfarerLabs/agentworks/actions/runs/37439026161/job/112187882921)
measures **816 passed and 59 skipped** on Windows Server 2025 with Python 3.13.15. The native
owned-console case passes rather than skipping. It requires actual installed and checkout
source-byte identity, interpreter/prefix/isolation identity, exact sole console PID and
hidden-window evidence before descriptor effects.

The child observes two cases with original and custom settings. They verify raw input admission and
geometry, empty/non-key queue polling, injected UTF-16 units including a surrogate pair, unchanged
output mode/code pages/handle flags/inheritability and live borrowed descriptors. Both cases restore
the exact prior snapshot; repeated release preserves its cleanup result. Parent checks require both
descriptors closed, no child or parent cleanup errors, retained reaping and independent
disappearance or identity change of the exact reported window. Passing observations establish this
native primitive fixture, not physical keyboard translation or a production cancellation guarantee.

Windows Server 2022, SSH/ConPTY launch and preparation, shared presentation sanitation, the keyboard
decoder and complete terminal workflows remain open. The public terminal feature stays disabled.
Authentication/POSIX fixtures and trust-policy migration cases still need their separately
authorized native runs. This result changes no completed plan record or final-product feedback-round
count.

## Injected record and character-read comparison

The developer's `53589fec8385079c026e98efc9904ab0baa7386d` adds a comparison inside the existing
fresh hidden console and retained native worker. Six directed input/output code-page pairs, two VT
input states and four injected record types produce 48 observations. Separate byte-identical
injections feed low-level record polling and `ReadConsoleW`; the report preserves returned record
fields, actual UTF-16 batches, native input/output-page conversion references, timings and remaining
records. Private-flagged Alt releases are synthetic records, not physical key input. Packed two-byte
input is confined to output code page 932, matching the inspected source's lead-byte requirement.

An ordinary character sentinel supplies queued input when a release is ignored. Native reads may
still block; the existing parent owns its exact pure-primitive child through timeout and reaping,
and the worker settles before descriptor, mode and page cleanup. This adds no SSH process, caller
console access, new production keyboard reader or second process owner. The isolated child loads
only the named sibling comparison file without changing import paths.

The developer measures 21 focused synthetic cases passed with 1 native skip. Linux and Windows
typing, Ruff and whitespace checks pass. Private project review finds missing reproduction
collateral and passing-report visibility; `045d57efe052942464046471587988d5ab22ddd7` updates the
README's scoped `-rP` command and limits. Its six comparison tests pass, including the optional
report-propagation assertion alongside the independent ABI check. All three independent source lanes
clear the corrected combined pin `476e7c798de1953c42e52c8cda33c84945fa9728`, which also proposed a
narrowly scoped CI collector. GitHub rejected publication at `889d2f163` because the token lacks
workflow scope; no new-head CI started. The collector was unnecessary for this one-time
investigation. After the operator questioned the recurring CI cost, the lead withdrew it and
separated the experiment from the ordinary native resource/polling regression. Collection belongs to
an explicitly selected tester run with `-rP`, with no credential change required. The #845
environment-data request was already fulfilled separately and did not call for this CI step.

That pin rebases cleanly onto transport `51d5222d50cf04c38f1a5648d2bdd8b8f9bca8fd`, with no SSH
source/test/CI byte changes relative to its pre-rebase pin `f1f02c377`. The dependency now binds
private operation context and typed envelope versions; fresh recovery, production context,
inline/service fencing and additive RunContext remain open. The preceding published SSH head
`699c8517e99e21dc8726f1c0ae5601deaef6f86a` passes
[all hosted CI gates](https://github.com/WayfarerLabs/agentworks/actions/runs/37443870414),
including 816 Windows tests passed and 59 skipped. That run exercises the earlier primitive, not
this comparison.

Complete local validation at the reviewed combined pin passes **15,213 non-integration tests with 51
skips and 27 warnings**, exit 0 in 237.63 seconds. Full Ruff/format (1,298 files), strict mypy
(1,261 sources), selected Windows typing, typer isolation, file quality, locked-SDD, Rulesync and
whitespace gates exit 0. Website validation passes 160 Python and 103 Node cases, four builds and
both deterministic comparisons, all exit 0. The scoped native-report node collects without
execution. All 25 completed plan records remain unchanged. Independent same-user process
cwd/descriptor scans find no references before removal of the exact settled suite scratch root.

The subsequent clean rebase onto transport `f49d060bc69490e0934b7da0aeddaa6c083b4c0c` preserves all
SSH production/test/CI bytes from that reviewed pin. Project review clears dependency/evidence pin
`1bfa56c877a253fbd45ee831645900d679bc9895`, whose complete local suite passes **15,234
non-integration tests with 51 skips and 27 warnings**, exit 0 in 322.73 seconds. Full Ruff/format
(1,299 files), strict mypy (1,262 sources), file quality, locked-SDD, Rulesync and whitespace gates
pass. Fresh website validation passes 160 Python and 103 Node cases, four builds and both
deterministic comparisons, all exit 0. The new dependency accepts explicit buffered-inline bootstrap
through its existing root program; production operation-owned context, recovery, fencing and
RunContext remain open. The suite is terminal and its exact scratch root is independently verified
unused before removal. This supplies no new native comparison observation.

**Comparison values have not been observed natively.** Collection or a skipped Linux case does not
establish them. Even a passing captured run cannot establish specific private-flag values without
the scoped report. The experiment will measure only this Windows binary's injected-record behavior;
physical keyboard input, Server 2022, production decoding, SSH/ConPTY launch/preparation and full
native terminal acceptance remain separate gates. Public terminal delivery stays disabled. No
completed checkbox, lockfile or final-product public feedback round follows from this increment.

## Exact-target execution dependency refresh

Local code/test pin `c46d75fbbf1344dd69e567e4afbddea9ea9a8ea8` rebases cleanly onto transport
`ccfe5b34851f53ec9f98453dcb962487e6d67e51`. SSH production, SSH fixture and CI bytes are identical
to the preceding local candidate `889d2f163`. The dependency now binds private buffered execution to
the selected owner scope and bootstrap boot, and its private WSL2 native factory constructs one
bootstrap from the prepared elevated plan and actual full guest for both file and execution state.
This source reconciliation supplies no native credential-transition or keyboard evidence. Fresh
recovery, service fencing, other platform composition and complete additive RunContext remain open.
Project source review clears dependency/evidence pin `c21919b0ce5da92141f220a15512e9ac968eb7e5`. The
affected execution/native-operation and complete SSH fixture selection passes **546 tests with 6
skips**, exit 0 in 9.43 seconds. Full Ruff/format (1,300 files), strict mypy (1,263 sources), file
quality, locked-SDD, Rulesync and whitespace gates pass. This targeted validation is not a new full
CLI or website run; their earlier complete results retain the pins recorded above. The exact settled
fixture root is independently verified unused before removal.

The later collector removal supersedes that publication hold. Native comparison collection remains
an explicitly selected tester task; no measured values follow from this correction. No completed
plan record, lockfile or public feedback round changes.

## One-time collection separated from routine CI

Developer `763b1ebebe5b7440e37a0494044a33bb9a1efde6`, integrated as `94b414507`, adds an explicitly
selected Windows integration case carrying `--input-comparison` to the isolated child. Ordinary
resource/polling coverage keeps its original/custom modes and cleanup checks without the comparison.
Both selections retain candidate admission, sole-process custody, worker settlement, exact-child
reaping and independent window cleanup. The report prints after successful controls and cleanup. The
helper and its six synthetic comparison cases are unchanged. The proposed six-line CI step was
removed from unpublished branch history; the workflow matches the existing published workflow.

The developer passes 69 focused synthetic cases with 1 Linux native skip, style checks, full strict
Linux typing, scoped strict Windows typing with silent imports and pinned README quality gates.
Windows full import traversal reports eight existing POSIX-symbol errors in production files; those
files remain untouched and that check is not a claimed pass. Actual collection at the developer pin
includes only the ordinary native fixture in both normal CI selections, and the explicit comparison
node collects separately. These observations do not execute either native case. New native
comparison values remain unobserved. The README now names the one-time command.

All three private source lanes clear combined code/evidence pin
`53ae6123ed25e1697bb2a76a5cafdf255d271d4f`, on transport `3d98a1a67`. Complete local validation
passes **15,717 non-integration tests with 51 skips and 27 warnings**, exit 0 in 240.33 seconds.
Full Ruff/format (1,313 files), strict mypy (1,276 sources), scoped strict Windows typing (4 files
with silent imports), exact CI typer isolation, file quality, locked-SDD, Rulesync and whitespace
gates pass. Actual collection selects the ordinary native node under Windows CI and the research
node only when explicitly requested. Website inputs and locked dependency files remain unchanged
from the preceding complete website validation; no new website run is claimed.

The initial full run beneath the shared workspace recorded 208 failures and 37 setup errors. Its
fixture directories inherited mode 2770/default group ACLs; diagnostics include unsafe gate/stage
namespaces, ancestor traversal denial and overlong Unix socket paths. Moving the same code's gate
fixture to a private short temporary root gives 19 passes, followed by the complete pass above,
without weakening checks or changing product code. Both failed and passing logs are retained. Exact
settled fixture roots were independently verified unused before removal. No native comparison,
complete terminal or RunContext acceptance follows from this correction.

# Salvage from the transport and SSH predecessors

<!-- cspell:ignore DACL improv MSYS NTFS PENDIN enrollable pidfd -->
<!-- cspell:ignore redirector reimplementation stockout subfolders unconfigured -->

From the next-steps saga lead to transport-take2. This is colleague input, not requirements: take2's
FRD and the operator's rulings govern. It exists so take2 does not have to rediscover by measurement
what transport-improv and ssh-connection-contracts already learned.

Each item comes from an integration-test report or review on the predecessors' pull requests (PR
796, 826, 832 and 833). Every item links its source comment, and its confidence is marked:
**measured** means observed in a live run, **reasoned** means derived from reading code or
documentation, and **unresolved** means the report itself left the cause open. Items describing the
predecessors' own code are kept only where the lesson applies to any reimplementation. Some concern
native channels that take2 narrows to R7 recovery; they stay because recovery still runs over those
channels.

The saga lead checked every link against its source, every figure against its source text, and
removed infrastructure identifiers. Treat an item as a lead, not a verified fact for take2's own
platforms: re-prove anything you build on.

## WSL2 and Windows

**On WSL2, the kernel `boot_id` is shared by the whole utility VM and does not change when a
distribution restarts.**

Evidence: guest journal showed one boot id spanning repeated distribution power-offs/restarts; "the
only boot id change I saw was when the utility VM itself restarted."

Source:
[#833 comment 5825224430](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5825224430)

Why take2 cares: a boot-fence or identity scheme that reads the kernel boot id cannot distinguish a
distribution restart from continuity; it needs a distribution-scoped signal (e.g. PID 1 start time)
combined with the boot id.

Confidence: measured.

**WSL2's effective `vmIdleTimeout` (when unconfigured) is about 60s for the utility VM, while a
single distribution instance idles out around 15.6-15.8s after its last process exits.**

Evidence: "the effective `vmIdleTimeout` is **60 s**", measured by timing `vmmem` exit after the
last distro process exited; distro instance idle measured independently at 15.6-15.75s, consistent
across probes.

Source:
[#833 comment 5822948740](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)

Why take2 cares: any WSL2 lifecycle/hold design must account for two independent idle clocks (distro
instance vs. utility VM), not one.

Confidence: measured.

**WSL passes the raw process wait status through, so a normal exit and a same-numbered signal are
indistinguishable, and a missing binary collapses to the same number as `exit 1`.**

Evidence: table showing `exit 9` and SIGKILL both report `local_status=9`, `exit 15` and SIGTERM
both report 15, and a missing binary reports 1, same as `exit 1`; "WSL passes the raw wait status
through."

Source:
[#833 comment 5774494551](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5774494551)

Why take2 cares: a WSL2 carrier must treat anything but a clean 0 as ambiguous
(`Failure.OBSERVATION`), never infer a signal or a specific failure cause from the raw number.

Confidence: measured.

**A cold (stopped) WSL2 target refuses a `\\wsl$` / `\\wsl.localhost` UNC open, but the refusal
takes 80s / 40s respectively, not a fast failure, and does not start the target.**

Evidence: "`\\wsl$` ... blocks for 80.0 s, then fails with 64 ERROR_NETNAME_DELETED";
`\\wsl.localhost` blocked 40.0s then `ERROR_BAD_NET_NAME`; target never started in either case.

Source:
[#833 comment 5822948740](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)

Why take2 cares: any design assuming a UNC probe is a cheap/fast way to test "is this distro
running" will badly underestimate worst-case latency and must size timeouts accordingly.

Confidence: measured.

**A single open UNC handle on a running WSL2 distribution keeps it alive far longer than its normal
no-handle idle lifetime, and release timing after close/death/`--terminate` is measurable.**

Evidence: "a single `\\wsl$` handle on an already-running target kept it alive for 180 s, more than
11 times the 15.75 s no-handle lifetime. It released 24.9 s after a normal close, 15.8 s after the
last of two holders closed, and 15.8 s after abrupt holder death. `--terminate` stopped it in 0.06 s
despite an open handle."

Source:
[#833 comment 5822948740](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)

Why take2 cares: a hold mechanism built on a UNC handle is a viable, measured way to keep a distro
alive independent of normal idle timeout, and `--terminate` is not blocked by a held handle.

Confidence: measured.

**On Windows, a bare `ssh` invocation resolves `ssh.exe` from the current working directory before
`PATH`.**

Evidence: a decoy `ssh.exe` placed in the cwd (a copy of the Python launcher) was executed in place
of the real client; "bare `ssh` resolves an `ssh.exe` from the current directory before `PATH`."

Source:
[#832 comment 6090196583](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6090196583)

Why take2 cares: any Windows process-launch design that resolves a client/tool name by bare string
must resolve to one absolute path explicitly, not rely on the OS loader or `shutil.which` defaults.

Confidence: measured.

**Empty elements in a Windows `PATH` (leading or interior, before the real client's directory)
resolve to the current working directory, enabling the same current-directory hijack.**

Evidence: "An empty element becomes `Path(\"ssh\").absolute()`, which is `<cwd>\ssh`." Measured
across leading, interior, and only-empty PATH entry positions, and this contradicts a README claim
that Windows "never adds an implicit current-directory search."

Source:
[#832 comment 6091160981](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091160981)

Why take2 cares: PATH-parsing code for Windows executable resolution must explicitly filter empty
components; relying on `shutil.which`/`PATH` semantics as "safe by construction" is wrong.

Confidence: measured.

**Windows `PATHEXT` is spelled in upper case, which causes case-sensitive string comparisons against
a correctly-resolved executable path to fail even though NTFS itself is case-insensitive.**

Evidence: hosted CI failures `assert '...\\selected\\ssh.EXE' == '...\\selected\\ssh.exe'`; "the
standard `PATHEXT` is upper case (`.COM;.EXE;...`) on both hosts."

Source:
[#832 comment 6091160981](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091160981)

Why take2 cares: any test or code comparing a Windows-resolved executable path by exact string must
normalize case, or use `os.path.samefile`, rather than assuming a particular extension casing.

Confidence: measured.

**An MSYS/Cygwin `ssh.exe` (e.g. Git for Windows) can pass a naive "`OpenSSH_X.Y` with X.Y >= floor"
version-gate regex, yet its own argv parsing corrupts every invocation.**

Evidence: `Git for Windows' usr\bin\ssh.exe reports OpenSSH_10.5p1`, passes the regex; quoted argv
elements arrive mangled ("invalid quotes"), and the fixed helper command "arrives truncated to 8,186
characters," producing opaque `dispatch=unknown` failures on every call.

Source:
[#832 comment 6246085333](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6246085333)

Why take2 cares: a Windows client version-gate should check the real vendor banner (e.g. require
`OpenSSH_for_Windows_`), not just a parsed version number, or a shadowing MSYS/Cygwin client
silently breaks the stack with no actionable diagnostic.

Confidence: measured.

**`CREATE_BREAKAWAY_FROM_JOB` and WMI `Win32_Process.Create` both let a Windows child process escape
the enclosing SSH session's Job object, surviving the SSH connection's death.**

Evidence: table showing both methods leave "exact holder alive; distro `Running`" after the ssh
connection was hard-killed, while a plain child dies with the job; a restrictive nested job (no
`BREAKAWAY_OK`) refused breakaway but the WMI child still escaped any job.

Source:
[#833 comment 6089972343](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343)

Why take2 cares: any Windows design that wants a launched process to outlive its controlling SSH/RPC
session (or, conversely, wants to guarantee it does NOT) must account for these two concrete
job-escape mechanisms.

Confidence: measured.

**A job/resource hosted inside a WSL2 distribution only survives as long as something external keeps
the distribution running; with nothing holding it, it dies within the distro's normal idle window.**

Evidence: a RESOURCE job (`sleep 1200`) died when its starting operation closed; "the default idle
window on this bed is 16.0 s: a distro with no holder stops 16 s after its last command."

Source:
[#833 comment 6089972343](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343)

Why take2 cares: a WSL2-backed long-running job needs an explicit, separate hold mechanism; it
cannot assume ordinary distro idle behavior will keep it alive.

Confidence: measured.

**`SIGCHLD=SIG_IGN` inherited across `exec` causes the kernel to auto-reap children on Linux, but
not on macOS; in-process ignore breaks reaping-dependent code on both.**

Evidence: inherited `SIG_IGN` is `observation`/unsettled on Linux but `sent`/settled on macOS (2 of
2 each); "the inherited trigger is Linux-specific."

Source:
[#832 comment 6093255918](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6093255918)

Why take2 cares: code that relies on `waitpid` to learn a child's exit status must not assume
`SIGCHLD` disposition is portable across POSIX platforms; a process-ownership layer needs to handle
"the kernel already reaped this for me" as a distinct, settled outcome, and this differs by OS.

Confidence: measured.

**Windows administrator SSH sessions have `SeBackupPrivilege`/`SeRestorePrivilege` already enabled,
which bypasses DACL checks the code itself never asked for.**

Evidence: a list-denied ancestor directory "published" inside an admin ssh session, but was
correctly refused from an elevated scheduled-task context where the same privileges were present but
disabled; "Backup semantics then bypass the DACL."

Source:
[#833 comment 6008723722](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6008723722)

Why take2 cares: ACL-based safety checks on Windows cannot assume uniform enforcement across launch
contexts; an interactive admin SSH session is measurably weaker than a scheduled-task context for
the exact same code path.

Confidence: measured.

**A WSL2 guest's benign cold-start stderr output can trip an overly strict "any stderr byte
invalidates the channel" classification rule.**

Evidence: Ubuntu 24.04 distributions print `Failed to start the systemd user session for 'root'` on
stderr during a cold `wsl.exe` start, which this effort's channel classified as `invalid`/`stderr`
regardless of the real exit outcome; clean on Debian Trixie, clean in 60 of 60 warm Ubuntu calls.

Source:
[#833 comment 5825224430](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5825224430)

Why take2 cares: a "stderr byte = failure" classification is fragile against ordinary
distro-specific boot noise; it needs a narrower signal than "any byte present."

Confidence: measured.

## Linux guests (systemd, cgroups, /proc, hardening)

**Hardened VMs that mount `/proc` with `hidepid=1` (or `=2`) deny a non-root user read access to
`/proc/1/stat`, breaking any identity/fence mechanism that reads PID 1's start time as that user.**

Evidence: `cat /proc/1/stat` fails with "Operation not permitted" as the VM's own non-root admin
account on a VM built by this effort's own hardening; "agentworks hardening mounts `/proc` with
`hidepid=1`."

Source:
[#833 comment 6008723722](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6008723722)

Why take2 cares: a design that reads `/proc/1/stat` for a boot fence or identity check must either
run elevated for that read or use a mechanism that does not require reading another process's
`/proc` entry as a non-root user, especially against any VM the project itself hardens this way.

Confidence: measured.

**A systemd transient unit that fails to start is left in `failed` state and never collected unless
the caller explicitly issues `--collect` or `reset-failed`.**

Evidence: three failed starts, each with a fresh token, "left exactly three failed units ...
repeated start failures accumulate unbounded entries in `systemctl --failed`."

Source:
[#833 comment 5767397749](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5767397749)

Why take2 cares: any systemd transient-unit launch mechanism must proactively collect failed units
(`--collect` or equivalent) or it leaves permanent, operator-visible health-signal residue on every
start failure.

Confidence: measured.

**A workload launched into a managed systemd unit lands in a predictable child-cgroup path before
caller code runs, verified under cgroup v2 unified hierarchy.**

Evidence: workload read its own `0::/system.slice/agw-managed-<token>.service/agw-workload-<token>`,
token matching the prepared run, on multiple Debian releases and both SSH and QGA carriers.

Source:
[#833 comment 5767397749](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5767397749)

Why take2 cares: this cgroup placement shape is a usable building block for process-tree
identification and cleanup, confirmed live under real systemd, not merely designed.

Confidence: measured.

**A detached (`setsid`) descendant process with its own open stdio can be reliably killed via its
cgroup, confirmed on real systemd across Debian releases and both SSH and QGA carriers.**

Evidence: a descendant proven alive and writing was killed; witness file frozen, cgroup absent, unit
gone, "zero `agw-managed-*` units and cgroups," reproduced on Bookworm-SSH, Trixie-SSH and
Bookworm-QGA.

Source:
[#833 comment 5767397749](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5767397749)

Why take2 cares: cgroup-based cleanup of a detached descendant (which `kill(pid)` alone cannot
catch) is a proven mechanism, not just theory.

Confidence: measured.

**A guest process tree left behind by an expired local deadline is never reaped by anything, on
either carrier (SSH or native QGA), and lives exactly as long as the guest command itself runs.**

Evidence: `/bin/sleep 98765` under an 8s deadline left 6 matching processes still present at t+60s
on both PVE majors and over SSH; "confirmed to keep running, unbounded, with no reaper anywhere"
(not merely unconfirmed cancellation).

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)
and
[#796 comment 5709203388](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5709203388)

Why take2 cares: this is a contract-level gap, not a carrier bug: any remote-execution redesign
needs an explicit cancellation or guest-side reaping story, because local cleanup never implies the
remote command stopped.

Confidence: measured.

**A Proxmox VM template built with a fixed, baked-in `/etc/machine-id` causes DHCP IP collisions
between clones of that template.**

Evidence: "`scripts/proxmox-setup.sh` bakes a fixed `/etc/machine-id` into the template, so two
clones collided on one DHCP IP."

Source:
[#833 comment 6089972343](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343)

Why take2 cares: any VM template/provisioning pipeline must regenerate `machine-id` (and similar
per-instance identifiers) at clone/first-boot time, not bake one into the golden image.

Confidence: measured.

**Bash's `wait` builtin returning real exit statuses for process-substitution descendants is a
behavior that changed across bash versions (4.4 and 5.0 per bash's own CHANGES), and an unstated
floor fails silently and totally if violated.**

Evidence: "producer failure detection depends on `wait` returning true statuses for four separate
process substitutions ... If an older guest bash returns 127 from `wait`, ... every execution
returns 125 with a bootstrap-failure record on a guest where nothing is wrong."

Source:
[#826 comment 5709256084](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709256084)

Why take2 cares: any bash-based bootstrap that depends on `wait` semantics for process substitution
must document and, ideally, actively check its minimum bash version, or risk a guest-wide
false-failure mode that looks like "nothing works" rather than a version mismatch.

Confidence: reasoned (the specific version boundary was not tested below bash 5.2; the mechanism and
risk were established by reading bash's own changelog).

**A destination account whose login shell rejects or mis-parses the launch command produces an
ordinary-looking exit status, indistinguishable from the payload itself failing.**

Evidence: `tcsh`/`sh -c` parse failures reported `completion=1` with no distinguishing signal in the
report, even though the underlying cause ("Unmatched"...) was visible on stderr; "nothing ran."

Source:
[#796 comment 5715862216](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5715862216)

Why take2 cares: a buffered-exec contract needs either an account-shell compatibility precondition
check surfaced distinctly in the result, or documentation that `completion` alone cannot be trusted
to mean "the payload ran."

Confidence: measured.

**File transfer over both SSH and native QGA, using a small fixed chunk size with a fresh subprocess
per exchange, runs at roughly 20 KiB/s.**

Evidence: "downloads over the SSH carrier run at about 20 KiB/s"; a 3 MiB download took 152.9-177.6s
over SSH and 85.5-156.3s over QGA depending on major; a 60 KiB download spawned 14 separate SSH
processes.

Source:
[#833 comment 6008723722](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6008723722)
and
[#833 comment 6089972343](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343)

Why take2 cares: a chunked file-transfer protocol with a small per-chunk ceiling and a fresh process
per exchange does not scale; tens of MiB becomes minutes-to-hours, and any realistic deadline turns
into an "uncertain" outcome with cleanup debt. Chunk size and per-exchange cost both need
reconsideration before file transfer is a supported, public feature.

Confidence: measured.

**The two carriers' usable raw-stdin ceilings differed by about 4x behind one nominally shared
contract (SSH ~196,554 bytes vs. native QGA ~49,098 bytes), because the envelope is shared across
source, environment and input and QGA's own transport is smaller.**

Evidence: bisection against `prepare()`: "SSH: 196,554 bytes ... Proxmox native: 49,098 bytes."

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)

Why take2 cares: a contract meant to be carrier-agnostic should either equalize usable budgets or
expose the real, carrier-specific remaining budget to callers; a caller-visible nominal "256 KiB"
figure that silently differs 4x by carrier and shrinks further with a bigger env/script is a trap.

Confidence: measured.

**Guest-side prerequisites this effort required and verified present for a bash+base64 buffered
bootstrap**: `/bin/sh`, `/bin/bash`, `/bin/cat`, `/usr/bin/printf`, `/usr/bin/base64`,
`/usr/bin/env`, `/usr/bin/getent`, `/usr/bin/id`, `/dev/fd`, and `env --default-signal=PIPE`
support.

Source:
[#796 comment 5709203388](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5709203388)

Why take2 cares: this is a concrete, tested reference list of guest binaries/features a bash-based
remote bootstrap needs to check for, rather than assume.

Confidence: measured.

## macOS

**Stock macOS applies an inherited, Apple-default "deny delete" extended ACL
(`0: group:everyone deny delete`) to the home directory and its standard subfolders (`~`,
`~/Downloads`, `~/Documents`, `~/Desktop`).**

Evidence: "I checked `~`, `~/Downloads`, `~/Documents` and `~/Desktop` on this machine" and all
carry the ACL; a publisher that refuses any ancestor with an extended ACL therefore refuses every
path under a normal user's home.

Source:
[#833 comment 6008723722](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6008723722)

Why take2 cares: a file-publication/safety design that blanket-refuses any ancestor directory
carrying an extended ACL will make macOS workstation destinations unusable for ordinary user paths;
the design needs a macOS-aware exception for this specific, benign, Apple-default ACL shape.

Confidence: measured.

**The macOS kernel sets the `PENDIN` bit in `termios.lflag` when restoring canonical terminal mode
after raw mode, which is not something the restoring code did.**

Evidence: direct `tcgetattr` comparison before/during/after raw-mode acquisition showed the only
difference between saved and restored `lflag` was exactly `termios.PENDIN` (`0x20000000`); "The
kernel sets that bit, not agw... Linux does not."

Source:
[#832 comment 6092448138](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6092448138)
and
[#832 comment 6093255918](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6093255918)

Why take2 cares: any terminal-mode restoration correctness check on macOS must allow exactly this
one bit difference (and only when canonical mode is being restored from raw), rather than requiring
exact mode equality, or every macOS terminal test will spuriously fail.

Confidence: measured.

**`FIONREAD` on pipes reports an accurate queued-byte count on macOS, same as Linux; it does not
under-report.**

Evidence: "macOS `FIONREAD` on pipes reports exactly what was queued: 1, 16,385 and 65,536 bytes,
the same as Linux."

Source:
[#832 comment 6092448138](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6092448138)

Why take2 cares: a drain/collection design that uses `FIONREAD` to size a read does not need a
macOS-specific fallback for under-reporting; that was a theoretical concern this effort explicitly
ruled out by measurement.

Confidence: measured.

**macOS's default temp directory plus typical test-runner nesting can exceed the platform's
`AF_UNIX` socket path bind limit, which is far shorter than on Linux.**

Evidence: "this host's maximum bindable `AF_UNIX` path is **103**" bytes; a 133-byte path (macOS's
48-byte default tempdir plus pytest's `pytest-of-<user>/pytest-N/popen-gwN/<testname>` nesting)
failed to bind with `AF_UNIX path too long`, while Linux's shorter base path did not hit the limit.

Source:
[#796 comment 5717455178](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5717455178)

Why take2 cares: any Unix-domain-socket fixture or feature (e.g. an SSH agent emulation) must bind
under a short, explicit directory on macOS rather than deriving its path from a test framework's
default temp location, which routinely exceeds the OS's bind limit.

Confidence: measured.

## SSH and OpenSSH

**Stock Proxmox signs its node certificate with a per-cluster CA that is in no system trust store,
so real TLS verification cannot succeed against a default trust store on any stock PVE install.**

Evidence: plain OpenSSL against the real node cert:
`verify error:num=20:unable to get local issuer certificate`; "PVE signs its node certificate with a
per-cluster CA that is in no system trust store."

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)

Why take2 cares: a Proxmox QGA/API client needs a `ca_bundle`-style field (not just a verify on/off
toggle) to reach real, verified TLS against a stock cluster; otherwise every operator ends up
disabling verification entirely rather than scoping trust to their own cluster's CA.

Confidence: measured.

**Collapsing every "no completion observed" cause (wrong host key, missing trust, genuine remote
exit 255, mid-flight ambiguity) into one ambiguous status discards information the carrier actually
has.**

Evidence: "Wrong pinned host key, unknown host, and unauthorized identity all produce exactly the
same report ... folding it into `UNKNOWN` throws that knowledge away," even though the client never
passed host-key verification and the carrier genuinely knows nothing was dispatched.

Source:
[#796 comment 5709203388](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5709203388)

Why take2 cares: a remote-execution/trust contract should distinguish "I know trust failed before
anything was sent" from "I don't know what happened," since the former is retryable/fixable
differently than the latter, as a design decision made deliberately rather than by default.

Confidence: reasoned, from direct measurement of the collapsed outcomes.

**Collapsing distinct admission-refusal causes (busy lock, blocked trust, missing client, stale
`.pub` sibling, client version too old) into one bare refusal code hides which one actually
happened.**

Evidence: "A missing identity, blocked trust, busy lock, stale `.pub` sibling and client older than
8.5 all return bare DISPATCH," confirmed in practice when Windows 10's inbox OpenSSH client (8.1)
"is always refused without saying why."

Source:
[#832 comment 6090196583](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6090196583)

Why take2 cares: an admission layer with several independent refusal reasons needs a typed, distinct
outcome per reason if operators/callers are expected to react differently (e.g. retry vs. upgrade
client vs. fix config).

Confidence: measured (for the macOS-symlink case) and reasoned (for the other listed causes, from
reading the admission code).

**A sensitive payload (secret) encoded only in stdin/env, never argv, can be verified never to reach
a remote process's visible command line by directly sampling `/proc/*/cmdline` on the destination
while the call is in flight.**

Evidence: a never-uttered marker carried in stdin and env, with `/proc/*/cmdline` snapshotted six
times during the call; "The marker appears nowhere ... in plaintext, not in its base64 form, not in
the local carrier argv, and not in the returned report."

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)
and
[#796 comment 5709203388](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5709203388)

Why take2 cares: this is both a validated design pattern (stdin/env, never argv, for sensitive data)
and a reusable live-testing technique (sample `/proc/*/cmdline` repeatedly during flight, not just
after) for proving a secret never appeared on a visible command line.

Confidence: measured.

**Giving every feature that owns a local child process (delivery, forwarding, terminal relay,
enrollment) its own separate process-ownership/custody object causes the cleanup and retry logic to
drift independently between them.**

Evidence: "Each local `ssh` child gets its custody in a different way... delivery and the probe take
their owner from the caller's `LocalDeliveryCustody`... forwarding builds its own
`LocalProcessOwner`... the terminal relay builds another one... enrollment needs yet another."

Source:
[#832 comment 6089600797](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6089600797)

Why take2 cares: a redesign should give every local-child-owning feature the same single
process-custody abstraction up front, rather than letting each feature invent its own, which is how
this effort ended up with duplicated and inconsistent cleanup/close semantics across features.

Confidence: reasoned, from reading the code; later confirmed as fixed and tested in a follow-up
round.

**A broad `except (ValueError, TypeError, RecursionError)` around a whole workflow can relabel a
real programmer error (e.g. a missing required argument) as a user-facing policy refusal.**

Evidence: "Enrollment's catch-all hides our own bugs... The known missing-custody call ... raises
`TypeError` and would reach an operator as 'inspect policy'."

Source:
[#832 comment 6089600797](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6089600797)

Why take2 cares: exception handling around a workflow should only catch what the called functions
are documented to raise; letting our own bugs (TypeError from a missing argument) propagate is safer
than silently relabeling them as a domain refusal.

Confidence: reasoned, from reading the code; confirmed fixed and tested (narrowed to
`(OSError, StateError, ValidationError)`) in a follow-up round.

**On Windows, a file lock's release should be made explicit at unlock time rather than relying on
`CloseHandle` to release it, per Microsoft's own documented behavior for `LockFileEx`.**

Evidence: "Microsoft's `LockFileEx` notes say the OS releases a lock left at close only when system
resources allow, and recommend unlocking explicitly. A likely symptom is an occasional
`TrustBusyError` when a writer follows a reader closely on Windows."

Source:
[#832 comment 6091225627](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091225627)

Why take2 cares: a cross-platform file-locking layer should call `UnlockFileEx` explicitly on
Windows on a confirmed acquire, using close only as a fallback, rather than depending on close-time
release semantics the OS itself says are not guaranteed.

Confidence: reasoned (from Microsoft's documentation, not from a reproduced failure in this effort).

**A resolved client executable must be captured once, as a single absolute path, and reused
identically for every check (version probe and the real spawn); resolving the bare name twice
reopens a substitution window even after the first instance is "fixed."**

Evidence: "`build_ssh_argv` can still launch the bare name... The default exists only for about 12
test call sites... Make `executable` a required keyword... Then the next production caller... cannot
forget it."

Source:
[#832 comment 6091225627](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091225627)

Why take2 cares: a current-directory-hijack fix for a client resolver is incomplete unless the
resolved path is made structurally required everywhere (e.g. a required keyword argument, no
default), not merely fixed at the call sites discovered so far.

Confidence: reasoned, from reading the code; confirmed fixed in a follow-up round (`executable` made
required).

**A "completion"/success predicate should enforce exactly what its own documentation promises;
mismatches between a name's promise and the predicate's actual check are real bugs, and fixing the
predicate (not the docstring) is usually the right direction.**

Evidence: `complete` was documented as "execution and lifecycle evidence, not full output
retention," but a post-wait I/O failure could still veto it, "which is precisely output retention";
removing that clause made the code agree with the already-correct docstring. "The name was telling
the truth and the predicate was not."

Source:
[#833 comment 5768981687](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5768981687)

Why take2 cares: this is a reusable diagnostic: when a name and its enforcing code disagree, treat
the name as the spec and fix the code, rather than assuming the code is right and the name just
needs softer wording.

Confidence: reasoned (general principle), supported by a measured before/after fix in this case.

**Output collection after a client process exits must be bounded by a frozen byte count read at
exit, not by a fixed, naive post-exit timer; a timer-based bound can silently truncate a
fast-exiting client's already-written backlog.**

Evidence: a 256 KiB / 1 MiB backlog written by a client that then exited immediately was truncated
by roughly 56-58 KiB under a fixed-timer drain; the frozen-count fix delivered the full backlog
byte-exact in all cases, including 100-500ms variants.

Source:
[#832 comment 6091160981](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091160981)
and
[#832 comment 6092359725](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6092359725)

Why take2 cares: a post-exit drain in any carrier's I/O pump must capture (and honor) a snapshot of
how many bytes were already queued at the moment the client exited, not simply "collect for N more
milliseconds," or it loses real output under exactly the common case of a fast-exiting producer.

Confidence: measured.

**An interrupted first-contact / enrollment flow, cut off at precisely the wrong moment, can leave a
resource permanently un-enrollable with no production recovery path.**

Evidence: "a 0.15 s first contact on a new resource ID ... recovery failed with `observation`. A
second first contact is refused for an existing candidate ... so that resource has no remaining path
short of manual cleanup."

Source:
[#832 comment 6091160981](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091160981)

Why take2 cares: a multi-step trust/enrollment state machine needs an explicit recovery or reset
path for every state an interruption can leave it in, not just a "retry from the start" assumption
that breaks once the candidate already exists.

Confidence: measured.

**A platform-specific terminal-mode-comparison allowance (e.g. for macOS's `PENDIN` bit) should be
scoped exactly to the platform, the specific saved/restored condition, and the specific bit, not
loosened as a general equality relaxation.**

Evidence: the fix "forgives a difference only when all three hold: the platform is Darwin, the saved
mode is canonical, and the restored `lflag` is exactly the saved `lflag` plus PENDIN... Every other
field is still compared exactly."

Source:
[#832 comment 6093148973](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6093148973)

Why take2 cares: when a kernel quirk needs a test exception, write the exception as narrowly as the
quirk itself, with every other property still exactly checked, rather than loosening the comparison
in general.

Confidence: reasoned, from reading the code; verified by mutation testing in the same round.

**Local process cleanup after a deadline (killing the local SSH/QGA client) never implies the remote
command was cancelled; the two must be reported and reasoned about separately.**

Evidence: repeated across both carriers: "local cleanup does happen ... but ... does not confirm
remote cancellation," and directly measured as the remote command continuing to run after local
cleanup, in every carrier tested.

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)
(and repeated throughout #826/#796/#833/#832)

Why take2 cares: this is the single most load-bearing contract fact for any remote-execution
redesign: local process death is not evidence of remote termination, on any carrier measured.

Confidence: measured.

**Running many concurrent threads against a lock and counting refusals is an effective, cheap way to
quantify how badly a too-coarse lock serializes operations that should be independent.**

Evidence: "Muntz ran 2, 4 and 8 threads, each calling `resolve_trust` 200 times on one bundle. 273
of 400, 681 of 800 and 1,482 of 1,600 calls were refused. With admission unlocked, 0 were refused."

Source:
[#832 comment 6089600797](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6089600797)

Why take2 cares: this is a reusable load-testing technique for proving (not just suspecting) that a
lock is too coarse, with a concrete before/after refusal-rate comparison.

Confidence: measured; this is a testing technique as much as a finding.

**When underlying storage uses immutable generations with atomic manifest replacement, readers only
need a shared (or no) lock; taking the same exclusive lock for reads as for writes serializes
operations that the storage design already makes safe to run concurrently.**

Evidence: "generation folders are never changed or deleted, and the manifest is replaced atomically.
An unlocked read therefore sees either 'blocked' or one complete generation. Suggest no lock (or a
shared lock) for `resolve_trust` and `trust_status`, keeping the exclusive lock for import, refresh
and block."

Source:
[#832 comment 6089600797](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6089600797)

Why take2 cares: a design lesson that generalizes beyond this one trust-bundle case: match lock
strength to what the underlying storage's own atomicity guarantees actually require.

Confidence: reasoned.

## Proxmox and QGA

**Real QGA responses encode booleans as JSON integers (`0`/`1`), never JSON `true`/`false`, for
fields like `exited`, `exitcode` and `out-truncated`.**

Evidence: "Measured live QGA responses on both majors return `exited`, `exitcode` and
`out-truncated` as **integers**, never JSON booleans."

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)

Why take2 cares: a QGA response decoder's boolean-normalization step (converting `0`/`1` to real
booleans) is load-bearing, not defensive padding; a reimplementation must not assume the QGA JSON
API returns real JSON booleans.

Confidence: measured (on both PVE 9.2.11 and PVE 8.4.21).

**Proxmox node certificates commonly do not cover every address actually used to reach the node
(e.g. a tailnet name vs. the address the tester actually connects through), which is a real-world
deployment gotcha rather than a code defect.**

Evidence: the node's default certificate named only loopback, a private address and the node's own
DNS names; reaching it at a tailnet address the certificate did not name produced
`verify error:num=64:IP address mismatch`.

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)

Why take2 cares: a TLS client for Proxmox needs a `tls_server_name` override (SNI/hostname
override), separate from the CA bundle, to let operators reach a node at an address its certificate
does not name.

Confidence: measured.

**An expired deadline during a native QGA action orphans the guest process tree exactly like it does
over SSH, confirmed on both PVE majors.**

Evidence: `/bin/sleep 98765` under an 8s deadline left 6 matching processes present through t+60s on
PVE 9.2.11 and identically on PVE 8.4.21; "reproduces identically on two independent carrier
implementations ... which makes it a property of the shared buffered contract rather than of either
author's code."

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)

Why take2 cares: confirms the no-reaping gap is a contract property, not a carrier-specific bug;
whichever carrier(s) take2 builds, this needs an explicit design answer.

Confidence: measured.

**An expired deadline during ANY native VM action (a DIRECT run, a RESOURCE disposal) can
permanently lock out further native operations on that VM, with no production recovery path at the
time this was measured.**

Evidence: "4 of my 5 native VMs ended up locked during routine acceptance cells"; repeated explicit
retries all raised the same `StateError`, even well after the guest process had genuinely exited and
been independently confirmed gone.

Source:
[#833 comment 6089972343](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343)

Why take2 cares: an execution-ownership design must provide a bounded way to re-observe or recover a
claim after a timeout, because an ordinary deadline being enough to permanently wedge a resource's
control plane is not a rare edge case; it happened in the majority of routine test runs.

Confidence: measured.

**A stale operation/ownership claim keyed only by `(vm, name)` reattaches to a brand-new VM that
reuses the same name after the original VM (and its stuck claim) was deleted.**

Evidence: deleted `q9`, recreated `q9` under the same name, and "the very first
`native_vm_operation` on the new VM raised
`StateError: another operation already owns this resource`."

Source:
[#833 comment 6089972343](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343)

Why take2 cares: any resource-claim key scheme must bind to the resource's actual identity (not a
reusable human-facing name), or deleting and recreating a resource under the same name inherits an
unrelated, stale lock.

Confidence: measured.

**Reaching a Proxmox cluster's API through an SSH port-forward to `localhost` can preserve full,
genuine TLS verification, when the node certificate's SAN includes `localhost`.**

Evidence: "I reached its API through an ssh port forward to `https://localhost:<port>`. The
certificate's SAN includes `localhost`, so TLS stayed fully verified; only the network path was
forwarded."

Source:
[#833 comment 6089972343](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343)
(also
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617))

Why take2 cares: a useful, reusable testing technique for reaching a real cluster's API with full
TLS validation when DNS/network topology doesn't match the certificate's primary name.

Confidence: measured; testing technique.

## Cloud platforms

**Nested-virtualization-capable GCE machine types can be capacity-exhausted (`STOCKOUT`) in a
specific zone for extended periods, sometimes requiring walking down to a smaller/older machine type
to get a VM to start at all.**

Evidence: "`c3-standard-8`, `n2-standard-4`, `n2d-standard-4`, `n1-standard-4` all returned genuine
`STOCKOUT`. The ... bed only started after I walked the type list down to `c2-standard-4`."

Source:
[#833 comment 5767397749](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5767397749)

Why take2 cares: any test/provisioning plan that needs nested virtualization on GCE should have a
fallback list of machine types and should expect to need it, not treat stockout as a rare exception.

Confidence: measured.

**Cloud image boot/cloud-init time varies enormously; one image took 35+ minutes just to finish its
first-boot apt upgrade before joining the network.**

Evidence: "the instance is up on GCP but still inside its own image's startup-script apt upgrade and
has not joined the tailnet, roughly 35 minutes in."

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)

Why take2 cares: provisioning timeouts and test scheduling for cloud-created guests should budget
for very long, highly variable first-boot times, not assume a typical "a few minutes" boot.

Confidence: measured.

## Database and migrations

**A schema-open check that only compares a stored version NUMBER (not the database's actual table,
column and foreign-key shape) can silently accept a database whose real shape differs from the
canonical shape defined for that version number.**

Evidence: databases built by intermediate branch commits, stamped at old levels 39-41, "migrate to
41, report ok" on open but then fail at first real use with
`OperationalError: no such table: operation_owners`; "Nothing notices until first use."

Source:
[#833 comment 5825746788](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5825746788)

Why take2 cares: during active development, different branch commits can legitimately mint the same
version number with different actual schemas; a version-number-only open check cannot detect this,
and the failure surfaces later, confusingly, at first real use rather than at open.

Confidence: measured.

**Reusing an existing general canonical-schema validator (checking actual tables/columns/FKs against
a per-version sentinel) at every database-open path, rather than writing a narrow guard that
recognizes only specific known-bad historical version numbers, catches the whole class of drift
instead of one instance of it.**

Evidence: `_validate_canonical_schema()` "checks a database against `SCHEMA_SENTINELS[version]` and
`FOREIGN_KEY_SENTINELS[version]`: exact tables, columns and foreign keys for the claimed version,"
previously used only for restore-from-backup; wiring it into ordinary open refused all four
historical drifted databases, with zero false refusals of the real, current-version operator
database.

Source:
[#833 comment 5826271525](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5826271525)
(suggestion), confirmed fixed at
[#833 comment 5826356000](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5826356000)

Why take2 cares: when a general-purpose shape validator already exists for one code path (restore),
it is usually cheaper and more durable to wire it into every open path than to hand-write a
per-incident guard that only recognizes the specific bad version numbers seen so far. Note: the
alternative narrow guard this general check replaced was itself later flagged (in a later round) as
a leftover test with no ability to detect regression once the mechanism's shape changed; see Testing
traps.

Confidence: measured (both the vulnerability and the fix were directly tested against real
historical databases).

## Python runtime and CI

**A subprocess-kill race (between obtaining a decoder's PID and `os.kill()` actually firing) can let
a short-lived decoder finish normally before the kill lands, especially under CPU load and on newer
Python interpreter versions; the fix is to pin the target by `pidfd_open` plus a session-id check
plus an explicit stopped-state read, not to rely on timing.**

Evidence: reproduced directly on Python 3.14 under deliberate CPU load, 1 failure in 120 iterations,
"the window between `_active_decoder()` returning a PID and `os.kill()` firing is real"; the
corrected version (using `kill -STOP "$$"` plus `pidfd_open` pinning) held at 0 failures in 120
iterations under the same load.

Source:
[#826 comment 5709203617](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709203617)

Why take2 cares: killing a short-lived subprocess by bare PID, inferred from timing, is a real,
reproducible race that gets worse with interpreter version changes and system load; pin identity
(pidfd/session id) and use an explicit signal-based state probe instead of trusting timing.

Confidence: measured.

**`fork()`-after-multithreading `DeprecationWarning`s raised only inside the unit-test process can
be a pure test-harness artifact, not a production hazard, when the forking code is designed to run
as its own single-threaded bundled process in production.**

Evidence: "The module and everything it imports start no threads, and in production it runs as its
own bundled guest process ... so the `fork()` happens single-threaded. The warning appears only
because unit tests call it inside the multi-threaded pytest process."

Source:
[#833 comment 5822948740](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)

Why take2 cares: don't chase every `fork()`-under-threads warning as a production bug; check whether
the actual production call path is single-threaded before treating the warning as real.

Confidence: reasoned, from reading the module and its callers.

**Hosted CI "Website" job failures citing a Chromium `DevTools endpoint` launch failure recur as
runner-level flakiness, unrelated to the code under test, across multiple unrelated PRs/heads.**

Evidence: "Chromium did not publish its DevTools endpoint" failure recurred identically across
multiple heads where the branch touched zero files under `website/`, and the same test passed
locally every time it was checked.

Source:
[#833 comment 5825746788](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5825746788)

Why take2 cares: recognize this specific failure signature as a known-flaky CI runner pattern rather
than re-investigating it fresh each time it appears on an unrelated change.

Confidence: measured (recurrence observed across at least 5 runs), though the root cause of the
runner flake itself was not determined.

**A test that pins a raw launch-argument identity (e.g. asserting `argv[0] == "ssh"` via a spy)
breaks silently, for the wrong reason, once the code under test starts resolving to an absolute path
instead of the bare name.**

Evidence: `test_installed_ssh_live_duplex_preserves_sensitive_binary_delivery` failed at
`assert 0 == 1` because "Its `Popen` spy matches `argv[0] == local_sshd.ssh_executable`, which is
`'ssh'`. The carrier now launches the resolved `/usr/bin/ssh`."

Source:
[#832 comment 6090196583](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6090196583)

Why take2 cares: tests that assert on exact launch-argument identity need to track (or be insulated
from) changes in resolution behavior, or they go stale the moment a real security fix (resolve to
absolute path) ships.

Confidence: measured.

**Tests relying on a tight, fixed sleep window (tens of milliseconds) are inherently flaky under
parallel/loaded test execution, even if deterministic when run alone.**

Evidence: "Muntz saw `test_alternating_sink_stalls_preserve_bounded_inherited_output[True]` fail 2
of 10 full-directory runs with `-n 8`, and pass alone. It relies on 0.16-second sleep windows."

Source:
[#832 comment 6089600797](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6089600797)

Why take2 cares: timing-margin-dependent tests need either a much larger margin, a different
synchronization mechanism (event/condition rather than sleep), or an explicit acknowledgment that
they are flaky under parallel CI load.

Confidence: measured (2 of 10 failures reported; not independently reproduced by the reviewer who
reported it).

## Contract and design lessons

**A success/completion signal must not be gated on a condition unrelated to what it claims to
measure; gating "complete" on whether a payload fully drained stdin conflates execution/lifecycle
success with full output _and input_ retention, and biases against programs that legitimately exit
without reading all of stdin (e.g. `head`, `grep -q`).**

Evidence: identical `WaitFact(EXIT, 0)` results reported `complete=False` for a payload that exited
fast without draining 1 byte of stdin, but `complete=True` for one that left 8,189 of 8,192 bytes
unread after a short sleep; "the discriminator is how fast the payload exits, not consumption."

Source:
[#833 comment 5767397749](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5767397749)

Why take2 cares: a general design lesson for any "did this run successfully" signal: audit exactly
what evidence the signal depends on, and make sure it cannot be flipped by something orthogonal
(timing) to what it claims to represent.

Confidence: measured; later confirmed fixed and re-verified not to over-claim in a follow-up round.

**A lifecycle-tracking mechanism that gets confused by kernel-level child reaping
(`SIGCHLD=SIG_IGN`) should still reach a settled, observable state rather than leaking a
permanently-unsettled resource.**

Evidence: an ignored `SIGCHLD` disposition (in-process or inherited) turned an ordinary successful
SSH delivery into `not_sent`/`OBSERVATION` with custody that "never settles," and the same
disposition left a borrowed terminal permanently in raw mode.

Source:
[#832 comment 6092448138](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6092448138)

Why take2 cares: any process-ownership abstraction built on `waitpid` must have an explicit, settled
outcome for "the kernel already reaped this child out from under me," because leaving custody
unsettled forever is a resource/terminal-state leak, not a safe failure mode.

Confidence: measured.

**A deadline expiring mid-operation is the realistic, ordinary way a response gets lost in a
distributed/remote-execution system, not a rare edge case; the execution layer needs an explicit,
bounded recovery path for a timed-out action, not just a refusal that becomes permanently
unrecoverable.**

Evidence: "4 of my 5 native VMs ended up locked during routine acceptance cells... an ordinary
deadline is enough."

Source:
[#833 comment 6089972343](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343)

Why take2 cares: design for "the deadline fires and we never hear back" as a first-class, common
case from the start, with a way to re-observe or recover, rather than discovering in testing that
most routine runs can wedge the control plane.

Confidence: reasoned, generalizing from the measured finding above.

**An import-independence guard (proving a new implementation never imports a retired legacy module)
should enumerate the package tree structurally (e.g. `pkgutil.walk_packages`), rather than from a
hardcoded module list, so that a new sibling module (like a new carrier) is automatically covered
without anyone remembering to add an import.**

Evidence: `test_independence.py` originally imported a hardcoded list of modules and never imported
the new SSH carrier; the fix "replaced the hardcoded list with `pkgutil.walk_packages` over
`agentworks.execution`, let import errors propagate ... and tightened the final assertion," which
then automatically covered the SSH carrier (and submodules) in a combined tree with no further edits
needed.

Source:
[#826 comment 5714306524](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5714306524)

Why take2 cares: a structural, walk-the-package-tree guard generalizes past the one case it was
built for; a hardcoded list of "things to check" is a guarantee that someone will eventually add a
new thing and forget to add it to the list.

Confidence: reasoned/measured (the fix was independently re-verified to cover the sibling module in
a combined tree).

**Reviewing one half of a two-sided, shared contract in isolation gives confidently wrong answers
about surface the other half actually consumes.**

Evidence: a reviewer flagged `Failure.INPUT`/`Failure.OUTPUT` as dead code in the transport-only PR,
which was wrong because SSH's own `_io.py` produced them at six sites; the same reviewer later made
an identical mistake about `verify_tls` ("My report said no caller passed `False`; the live harness
did. My search covered this PR and I wrote it as though it covered the world, which is the error"),
an error pattern repeated at least four times across the effort by the reviewer's own later count,
and explicitly self-corrected each time.

Source:
[#826 comment 5709256084](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709256084)
and
[#826 comment 5714306524](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5714306524)
and
[#833 comment 5744817729](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5744817729)
/
[#833 comment 5745081983](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5745081983)
(fourth instance, explicit pattern admission)

Why take2 cares: a design lesson for anyone reviewing or reasoning about complexity/dead-code across
a pair of effort that share a contract: "unused in this PR" is not "unused," and claims of that
shape need to be checked against every known consumer, including a sibling effort's code. Note: this
item is itself a record of the abandoned effort's own reviewer repeatedly disputing and correcting
its own prior findings; it is flagged here specifically because it recurred and was explicitly named
as a pattern.

Confidence: measured (each individual mistake), reasoned (the generalized lesson).

## Testing traps

**A test whose expected value can be produced by the ABSENCE of the thing it claims to test is a
check that cannot fail; prove a test actually guards its subject by deliberately deleting or
mutating the implementation and confirming the test then fails.**

Evidence: `test_malformed_input_fails_before_application_execution` stayed green after deleting the
pre-validation it was named for, because its assertion checked the raw/armored stream (where guest
output never appears unencoded) rather than the decoded stream; "the payload actually ran" with the
test still passing. A second instance: `.get(k, "none") == "none"` matched the expected answer
whether or not the real OpenSSH option was actually set, because `ssh -G` doesn't report those
settings at all.

Source:
[#796 comment 5708992351](https://github.com/WayfarerLabs/agentworks/pull/796#issuecomment-5708992351)
and
[#826 comment 5709256084](https://github.com/WayfarerLabs/agentworks/pull/826#issuecomment-5709256084)

Why take2 cares: this is the single most repeated, concrete testing trap across this whole effort:
always verify a safety-property test by breaking the thing it claims to guard and confirming the
test catches it, especially for the deleted-branch/option case.

Confidence: measured (both instances independently confirmed, and both independently fixed and
re-verified by mutation in follow-up rounds).

**In a Python virtual environment on Windows, `sys.executable` is a launcher stub that spawns a
separate, real interpreter child process; killing the launcher only brings down the child through
its enclosing Job object, and cannot interrupt a child that is blocked in kernel I/O.**

Evidence: "`Popen([sys.executable])` yields the launcher plus a separate real-interpreter child...
The holder is still blocked in the redirector call when it is killed. A thread blocked in that call
cannot finish terminating until the call returns."

Source:
[#833 comment 5822948740](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)

Why take2 cares: any Windows-targeted test harness or production code that spawns the current
interpreter and later expects to kill it promptly needs to account for the launcher/child split and
the fact that a kernel-I/O-blocked child cannot be hurried by killing its parent.

Confidence: measured.

**A probe/test hardcoding a path assumed to be a plain regular file can fail universally on a target
where that path is actually a symlink that the access mechanism in use cannot traverse.**

Evidence: `_unc_path` always opened `\etc\os-release`, which is a symlink to `../usr/lib/os-release`
on both Ubuntu and Debian; "Windows cannot follow that Linux symlink through the WSL share," so "no
hold case can ever run against standard Ubuntu or Debian targets" until the probe was pointed at the
real file.

Source:
[#833 comment 5822948740](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)

Why take2 cares: always verify a fixture/probe path resolves to the actual file it claims to, on the
actual target distribution, rather than assuming a conventional path is a real file rather than a
convenience symlink.

Confidence: measured.

**A probe's own abort/exception-handling conditions can make its final result timing-dependent with
respect to the actual question it is trying to answer, independent of the real behavior under
test.**

Evidence: whether the probe reached "holder reader did not settle" (an abort) or "opened=null" (a
recorded state) depended purely on whether the OS's redirector call happened to return before or
after the kill, on the same real behavior; "Whether the probe aborts is therefore timing-dependent,
which is a robustness defect in its own right."

Source:
[#833 comment 5822948740](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)

Why take2 cares: treat a probe's intermediate abort conditions as recorded states rather than
reasons to stop, or the probe's conclusion becomes a coin flip unrelated to the property being
measured.

Confidence: measured.

**A probe's own timeout window must exceed the worst-case real-world latency of the thing it is
measuring, or the probe times out before it can observe anything, and the result reads as "no
answer" rather than revealing a probe-configuration bug.**

Evidence: a cold UNC-share refusal took 80s/40s to arrive, but the probe capped its open window at
`min(30, max(5, idle))` = 30s, so "no command-line value lets a cold case see the answer on this
host."

Source:
[#833 comment 5822948740](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)

Why take2 cares: when building a probe for a platform behavior with an unknown or variable latency,
measure the worst case directly first and size the probe's timeout to exceed it, rather than
guessing a timeout and treating a probe timeout as equivalent to "the behavior didn't happen."

Confidence: measured.

**Test code written as literal, embedded shell/program strings (a whole script as a Python string
literal executed remotely) is invisible to static analysis tools like linters and type checkers.**

Evidence: "About 700 lines of tests are Python programs written inside strings, which ruff and mypy
cannot see." A sibling file was pointed to as showing "the better pattern" (a real importable
module).

Source:
[#832 comment 6089600797](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6089600797)

Why take2 cares: prefer real, importable test-support modules over embedding executable program text
as string literals, specifically so static analysis tools can actually see and check that code.

Confidence: reasoned, from reading the test suite.

**A process-search probe that does not exclude its own PID or command line can report a
false-positive "leftover process" match on itself.**

Evidence: "a false leftover because the probe's own command line contained the search strings"; the
fix excluded the probe's own PID and matched the real helper signature specifically.

Source:
[#833 comment 5774494551](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5774494551)

Why take2 cares: any residue-check/leak-detection probe that greps running processes for a pattern
must exclude its own process first, or its own invocation can self-report as a false leak.

Confidence: measured.

**A test that pins literal historical values (e.g. specific old schema version numbers) can stop
being able to detect removal or change of the mechanism it was originally written to guard, once
that mechanism's actual shape changes underneath it.**

Evidence: `test_transport_migration_open.py` "pins the literals 39/40/41, and asserts the current
version is `41`, so it breaks as soon as migration 42 lands... Muntz put the removed guard back in a
scratch copy and the test still passed, so it never could tell whether the guard was there."

Source:
[#833 comment 6008522156](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6008522156)

Why take2 cares: a version-pinning regression test needs to actually exercise the mechanism it
claims to guard (ideally proven by reintroducing a deliberately broken version and confirming the
test fails), not just assert a specific current literal value that will need editing (and silently
stop testing anything) the moment that literal changes.

Confidence: measured (confirmed by reintroducing the removed guard and observing the test still
passed).

-- agw-next-steps (agentworks saga lead)

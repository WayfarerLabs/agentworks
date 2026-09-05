# 20. Close SSH stdin with -n instead of forcing a TTY

Date: 2026-09-04

## Status

Accepted. Answers issue #361 (whether the Windows forced TTY was required) and supersedes that
default (`force_tty=sys.platform == "win32"`, at nine transport and platform call sites); shipped in
PR #737. Converges with the runnable-status finite-stdin fix (PR #736), which reached the same
problem from the status-probe side.

## Context

Every canonical transport built on a Windows controller forced a pty (`ssh -tt`) on every
non-interactive `run()`, via `force_tty=sys.platform == "win32"`. It was a workaround for an SSH
call that appeared to hang on the Windows OpenSSH client.

The hang is the Win32-OpenSSH short-connection stdin race (PowerShell/Win32-OpenSSH#1338): with
stdin inherited from the operator's console, a brief remote command can leave the client blocked
reading the keyboard after the server closes the connection. `ssh -tt` masked it by attaching stdin
to a pty, but a forced pty is a blunt instrument for a non-interactive command:

- it merges stderr into stdout, so a probe classifier cannot tell the streams apart;
- it injects CRLF into captured output; and
- it presents a fake TTY, so a remote program that branches on `isatty` behaves as if a human is
  watching. Installers and prompts that read `/dev/tty` then stall (the codex-install stall behind
  issue #360 is the concrete case).

Measured on a current Windows OpenSSH client against a live VM, 25 iterations per variant, breaking
on the first hang:

| trigger      | `-tt` (old default) | no-`tt`, inherited stdin | no-`tt`, `-n` |
| ------------ | ------------------- | ------------------------ | ------------- |
| `echo -n ""` | OK 25/25            | HUNG (iter 2)            | OK 25/25      |
| `sleep .001` | OK 25/25            | HUNG (iter 11)           | OK 25/25      |
| `true`       | OK 25/25            | HUNG (iter 11)           | OK 25/25      |

Near-zero-output commands hang intermittently under an inherited-stdin no-pty call, `-n` is clean
across all of them, and `-tt` avoids the hang only at the cost of the corruption above. The decisive
reading: dropping `-tt` without also closing stdin reintroduces the hang (middle column), so the fix
is not "stop forcing a pty" but "close stdin."

## Decision

Remove the forced-TTY default and close stdin with `ssh -n` instead:

- Non-interactive `run()` no longer forces a pty on any platform. The `SSHTarget.force_tty` field
  and the nine `force_tty=sys.platform == "win32"` sites are gone.
- Every no-stdin-payload SSH call passes `-n`, on all platforms, so a stdin-reading remote command
  cannot hang and ssh cannot pull from the operator's console. A call streaming a byte-exact
  `input_text` or `input_data` payload keeps stdin open for the write.
- `tty=True` remains the per-call way to force a pty (`-tt`). Any other value forces `-T` (no pty)
  unconditionally, so a captured programmatic call is deterministic and an operator's
  `RequestTTY force` cannot inject a pty into it. Only interactive paths, which do not use `run()`,
  allocate a pty without an explicit `tty=True`.

## Positives

- **Fixes the hang at its cause.** Closing stdin is what the #1338 race actually needs; the pty was
  incidental. The fix carries none of the pty's costs: captured output is clean LF, stderr stays
  separate, and `isatty` tells the truth so installers run non-interactively.
- **One behavior across platforms.** No Windows carve-out; Linux, macOS, and Windows build the same
  argv. `-n` is a mild hardening everywhere (ssh can no longer read the operator's console for a
  stdin-reading remote command), not a Windows patch (echoes ADR 15's cross-platform
  simplification).
- **Retires downstream scar tissue.** With no pty, OpenSSH emits no "connection closed" teardown
  advisory, so the tmux presence classifier's advisory stripper is removed rather than maintained.
- **Programmatic ssh is deterministic.** `run()` forces `-T` regardless of the operator's ssh
  config, so `RequestTTY force` (or any pty-forcing directive) cannot corrupt a captured call. TTY
  state is decided by the call, not by the environment.

## Negatives

- **`-n` is a client-behavior dependency.** It relies on the ssh client honoring "do not read
  stdin," as any ssh flag relies on client behavior. Validated empirically on the current Windows
  client (75/75 across three triggers); a future client regression would be observable as the hang
  returning.
- **`run()` overrides an operator ssh-config directive.** Forcing `-T` means a `run()` call ignores
  the operator's `RequestTTY` preference. That is intentional: a captured programmatic call has one
  correct pty state (none), and an operator who genuinely needs a pty for one command uses
  `tty=True`. It does leave other pty-affecting config (e.g. an operator `ControlMaster`)
  unaddressed here; see Consequences.

## Alternatives considered

- **Keep forcing `-tt` on Windows.** Masks the hang but corrupts non-interactive output and stalls
  `isatty`-gated programs. The reason this ADR exists.
- **Close stdin with an empty stdin payload (`input_data=""`).** Proven equivalent (PR #736's
  runnable-status probes used it and recorded 80 of 80 clean), but it is subprocess pipe plumbing
  per call rather than one argv flag, routes captured output through the byte-mode decode path, and
  is a second mechanism for the same goal. Unified onto `-n`; the redundant empty-payload sites were
  removed.
- **Python `subprocess(stdin=DEVNULL)`.** Does not deliver EOF to the remote reader on Windows
  OpenSSH, so a stdin-reading command still hangs. `-n` does deliver it.

## Consequences

- Every canonical transport and the `ssh_run` primitive build argv from one model: `tty=True` gives
  `-tt`; any other value gives `-T`, plus `-n` when no stdin payload is written.
- This ADR neutralizes only the pty-forcing config directive. Other operator ssh-config options can
  still alter a programmatic call, notably `ControlMaster` (ADR 15 abandoned agentworks _using_
  multiplexing but does not defend against an operator _enabling_ it) and `RemoteCommand`. Hardening
  those with explicit `-o` overrides is tracked as a coherent follow-up in issue #745 (which also
  asks for a full audit of config directives that affect a programmatic call), not done here.
- The runnable-status SDD's finite-stdin fix (an empty `input_data` payload) is superseded by the
  `-n` default; that SDD's `locked.md` records the supersession.
- Interactive paths are unaffected. Attach and streamed-exec (`interactive` / `call_streaming`)
  deliberately allocate a pty with `-t` and do not go through this no-pty, stdin-closing `run()`
  path.

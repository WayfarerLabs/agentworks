# claude-code harness: low-level design

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

Pins the `claude-code` built-in's tool-specific mechanics. Read `frd.md` R4 and `hla.md`
"claude-code's existence check" and "claude-code config vocabulary" first. Everything tool-specific
here is verified against the Claude Code CLI at HEAD (latest-stable rule), not recalled.

## Verification basis

- **CLI verified against `claude` v2.1.205** (`claude --version` -> `2.1.205 (Claude Code)`), the
  build installed on this VM, exercised directly (not from docs, not from memory). The subsections
  below cite the exact commands run.
- **Re-verify at implementation time.** Claude Code ships fast; before Phase 2 lands, re-run
  `claude --version` and `claude --help` and reconfirm every flag and behavior pinned here. Where a
  point could only be checked in print / non-TTY mode (not a live interactive TUI pane), it is
  flagged "verify at implementation" inline.

## What the current CLI actually provides (verified)

Run against v2.1.205; the relevant `claude --help` lines and empirical probes:

- **`-n, --name <name>` is a DISPLAY label only.** Help: "Set a display name for this session (shown
  in the prompt box, /resume picker, and terminal title)." It does NOT make a session addressable or
  resumable by that name.
- **`-r, --resume [value]` resumes by SESSION ID (a UUID)**, or opens an interactive picker when
  given no value. Empirically: `claude --resume <uuid>` resumes that conversation;
  `claude --resume <unknown-uuid>` fails fast with `No conversation found with session ID: <uuid>`
  and exits (verified in print mode AND in non-TTY interactive mode: a full explicit UUID never
  opens the picker, it errors cleanly). Passing a NAME that is not a UUID fails the same way.
- **`--session-id <uuid>` pins a session's ID at fresh launch.** Help: "Use a specific session ID
  for the conversation (must be a valid UUID)." Empirically it is CREATE-ONLY: launching with a
  `--session-id` that already exists fails with `Error: Session ID <uuid> is already in use.` It
  does NOT resume-or-create. Any valid uuid is accepted
  (`--session-id 939b1597-7c61-5ace-80f4-14617b7b4257` launched cleanly), so the harness's own
  stored id is usable.
- **`--permission-mode <mode>`** (hyphenated). Choices in v2.1.205: `acceptEdits`, `auto`,
  `bypassPermissions`, `manual`, `dontAsk`, `plan`.
- **`--model <model>`** accepts an alias (`opus` / `sonnet` / `fable`) or a full model name.
- **No CLI session-list primitive.** `claude project` exposes only `purge` (no list); there is no
  `claude sessions ...` command. So "is there a resumable session named X" cannot be answered by a
  CLI query; it is answered from on-disk state (below).
- **On-disk session state.** A launched/resumed session persists as
  `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`, where `<cwd-slug>` is the launch directory
  with path separators folded to `-`. Verified: a `--session-id 939b1597-...` launch created
  `.../projects/-tmp-...-hverify/939b1597-...jsonl`. The `<cwd-slug>` transform is undocumented and
  brittle; the detection below deliberately does NOT reconstruct it (see "Resume-vs-launch").

**Consequence for the two flag conventions the repo sample still encodes** (reported upstream; this
LLD does not edit FRD/HLA):

- `claude --name {{session_name}}` as the LAUNCH form is stale: `--name` is a cosmetic label in
  v2.1.205 and does not make the session resumable by that name. Fresh launch must pin identity with
  `--session-id <stored-uuid>`; `--name` is kept only as the human-facing display label.
- `claude --resume {{session_name}}` is stale/broken: `--resume` resolves a session ID (UUID), so a
  bare session NAME errors `No conversation found with session ID: <name>`. Resume must pass the
  stored UUID.

## Resume-vs-launch detection (pinned)

### Addressing: a stored per-session UUID (the harness-state blob)

The harness owns a general-purpose per-session state blob persisted on the session row (harness-api
LLD "Harness-state persistence"). `claude-code` stores its Claude session id there under the key
`session_id`:

```python
# capabilities/harness/claude_code.py (op-time)
sid = self._state.get("session_id")
if sid is None:                      # first start: mint and record it
    sid = str(uuid.uuid4())
    self._state["session_id"] = sid  # the manager persists self._state after the op
```

The id is minted ONCE, at the first `start`, and read back verbatim on every `restart`, because the
manager persists the mutated blob to the session row after the op. This replaces the earlier
derive-from-identity scheme: a plain stored value needs no derivation logic, cannot collide, and
survives any change to how identity is represented. A random v4 uuid is used (Claude accepts any
valid uuid at `--session-id`); global uniqueness is intrinsic, so the detection probe below stays
slug-independent. (Existing sessions predating the `harness_state` column backfill to `{}` and mint
a fresh id on their first restart under the new harness; they were not addressable by any controlled
id before, so nothing resumable is lost.)

### Detection: an op-time existence probe via `ctx.agent_target()`

`start` / `restart` run a single existence probe on the launch target through the merged
`RunContext` accessor (`ctx.admin_target()` in admin mode, else `ctx.agent_target()`), branch in
Python, and return the appropriate pane command. This is the HLA's named fallback ("a
`ctx.agent_target()` probe remains available if the one-liner gets awkward"), chosen over the
fully-folded target-side one-liner for two concrete, verified reasons:

1. **The `exec` wrapping forces an `sh -c` anyway.** `sessions/tmux._pane_command`
   (`tmux.py:361-385` at HEAD) wraps the harness's returned string as
   `$SHELL -lic 'cd <path> && exec <command>'`, and `exec` takes a single simple command. A bare
   compound (`if ...; then ...; fi`, or even `echo ...; claude ...`) would have `exec` consume only
   its first word and drop the rest. So any multi-step pane command must be a single `sh -c '...'`.
   Given that constraint is paid regardless (the visible-decision line, below, is itself a second
   step), folding the check target-side buys little.
2. **A Python-side decision is what the visible-decision and the P2 test double need.** Knowing
   resume-vs-fresh in the harness (not only in a runtime shell branch) is what lets the returned
   string carry a deterministic visible-decision line and lets P2 assert both directions by stubbing
   one transport call (see "Test double"). The FRD explicitly ACCEPTS the check-to-launch race
   ("best-effort robustness, not race-proof ... understood and accepted"), so the probe's split from
   the launch is in-contract; the folded one-liner was only ever an optional strengthening.

The probe command mirrors `RequiredCommandsCheck._probe`'s shape (`nodes.py:141-200`): run through
`"$SHELL" -lic '<inner>'` with `check=False`, then branch on the EXIT CODE (not just `.ok`). The
inner test is slug-independent, it finds the stored uuid's transcript under ANY project directory
(uuid uniqueness makes this safe and removes the brittle `<cwd-slug>` reconstruction):

```python
projects = "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"
inner = f'find {projects} -name {shlex.quote(sid + ".jsonl")} -print -quit 2>/dev/null | grep -q .'
result = target.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
```

`grep -q .` exits 0 for a match and 1 for no match, and is shell-neutral (no glob-nomatch divergence
between bash and zsh). `find` is assumed present on the target (ubiquitous coreutils/findutils; not
worth adding to `required_commands`, and a missing `find` collapses into the pipeline's `grep` exit
1). The branch reads `result.returncode`, distinguishing a probe that RAN from one that could not,
so a transient failure never masquerades as "no transcript" (a P2 review finding):

- **`0`** -> **resume**: `claude --resume <sid> ...`.
- **`1`** -> **launch fresh**: `claude --session-id <sid> ...`.
- **any other exit** (an SSH failure's 255, a shell that could not start) -> the probe never ran, so
  RAISE a typed `StateError` naming the target and the exit code rather than guessing. Guessing
  "fresh" would launch `--session-id <reserved-uuid>`, which Claude rejects as already-in-use on a
  real session's restart, and the pane then fails to start.

This makes start and restart symmetric: both call one shared `_resume_or_launch(ctx)`; the only
difference is caller-side (the orchestrator kills the old tmux before `restart`, per R7), not in the
harness output. It fixes both fixed-string failure modes (FRD R4 / Background item 3): a START over
an existing resumable session resumes it; a RESTART after Claude discarded a no-work session (no
transcript on disk) launches fresh instead of a blind `--resume`.

**The file-presence boundary is empirically validated (2026-07-19), and equals Claude's own resume
boundary.** A controlled experiment (sessions exited at every point: pre-login, login, workspace
trust, approved-no-input, setting-changed, prompted, running) showed the transcript
`projects/<slug>/<uuid>.jsonl` is written only once the session does real work, and NEVER for a
no-work session (not even one that approved the workspace and sat at an empty prompt). Restarting
the weakest transcript that exists (a setting change, no assistant turn) resumed cleanly, and
restarting a no-transcript session failed with "No sessions match" as expected. So
transcript-presence and Claude-resumability are the SAME boundary: no file -> not resumable -> we
launch fresh; a file -> resumable -> we `--resume`. Both earlier failure modes (blind-resume of a
nonexistent session, and a resumable stub that would error) are therefore impossible, so no
resume-branch fallback guard is needed. (Two CLI-level existence checks were also evaluated and
REJECTED: `claude --resume <id> --print` is cwd-scoped and can execute a deferred tool as a side
effect of the probe, and `--session-id <id> || --resume` flashes a spurious "already in use" error
on every resume; the pure file read is faster and side-effect-free. There is no session-index DB or
read-only session-query command in the CLI.)

**Verify at implementation:**

- `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`: RESOLVED at implementation (2026-07-19).
  `CLAUDE_CONFIG_DIR` is confirmed a real env var in the v2.1.205 binary, so the probe root
  `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects` is correct as written (default `$HOME/.claude` when
  unset).
- Flag/behavior parity in a real interactive TUI pane: `--session-id` / `--resume` /
  `--permission-mode` / `--model` were exercised in print mode, and `--resume` also in non-TTY
  interactive mode; none is `--print`-gated in `--help`, so parity is expected. Confirm by launching
  in an actual tmux pane at implementation.

## Flag spellings and invocation forms (pinned)

The harness builds an argv token list, shell-quotes each token, and space-joins. For a config value
`config.get(field)`:

| `harness_config` field   | CLI flag emitted            | Handling                                             |
| ------------------------ | --------------------------- | ---------------------------------------------------- |
| `permission_mode` (str)  | `--permission-mode <value>` | value forwarded verbatim (no enum validation)        |
| `model` (str)            | `--model <value>`           | value forwarded verbatim                             |
| `extra_args` (list[str]) | (the tokens themselves)     | each element `shlex.quote`d, appended in order, last |

- **Value validation is out of scope.** `validate_config` is vocabulary-and-shape only (FRD R2/R4):
  it rejects unknown FIELD names and wrong types (`permission_mode`/`model` must be strings,
  `extra_args` a list of strings), and returns `()` (no implied references). The `--permission-mode`
  CHOICE set is Claude-owned and can drift between releases, so the harness does NOT validate the
  value against it; an invalid mode surfaces as Claude's own startup error in the pane. Same for
  `model`.
- **`extra_args` is appended verbatim, last**, after the managed flags, so an operator escape hatch
  can add any flag the harness does not model (FRD R4). Each element is one argv token
  (`shlex.quote`d into the string), never re-split or re-interpreted by the shell.

Invocation forms (`<sid>` = the stored uuid; `<name>` = `self._session_name`):

```text
fresh:   claude --session-id <sid> --name <name> [--permission-mode <m>] [--model <x>] [extra_args...]
resume:  claude --resume     <sid> --name <name> [--permission-mode <m>] [--model <x>] [extra_args...]
```

Only the leading identity flag differs (`--session-id` vs `--resume`). `--name` is set on both
purely for the display label; it is cosmetic and does not affect addressing. (Verify at
implementation that `--name` alongside `--resume` is accepted/ignored and does not reject; harmless
in testing.)

## Visible decision (pinned)

The chosen path must never be silent (FRD R4). The harness ops return only a pane-command string and
have no CLI-side operator-output channel (`RunContext` exposes targets/secrets, not a reporter), so
the visible decision is an `echo` line prepended INSIDE the returned `sh -c '...'`, ahead of the
`exec claude`. It is both "an output line" and "the pane's first visible output" (FRD's two
options), deterministically, and it is trivially assertable in P2 (the returned string contains it):

```text
resume:  echo 'agentworks harness (claude-code): resuming session <name>'; exec claude --resume <sid> ...
fresh:   echo 'agentworks harness (claude-code): starting new session <name>'; exec claude --session-id <sid> ...
```

Full returned pane string (a single `sh -c` so it survives the `exec` wrapping, per the constraint
above):

```python
inner = f"echo {shlex.quote(msg)}; exec claude {' '.join(quoted_tokens)}"
return f"sh -c {shlex.quote(inner)}"
```

`_pane_command` then yields
`$SHELL -lic 'cd <path> && exec sh -c '\''echo ...; exec claude ...'\'''`; the login shell `exec`s
`sh -c`, which echoes then `exec`s `claude`, replacing itself so the pane is Claude (tmux keeps
owning the tty, R7). The message text uses no em dashes and no `{{ }}` tokens, so the core
template-variable substitution relocated in Phase 3 (harness-api LLD) does not mangle it;
literal-brace safety for any operator-supplied `extra_args` is that LLD's escaping decision (the
plan's P4 "substitution-safety carry" targets exactly this snippet).

## Required executable (pinned)

`claude-code`'s readiness is the required-commands check for a single binary, `claude`, via the
shared `require_commands(...)` helper defined by the harness-api LLD (`harness/base.py`, the
relocated `_probe` body). `preflight` / `runup` call `require_commands(ctx, ["claude"])`; the
four-way skip/defer/probe/error fork, the fifth `scope is None` loud branch, and the single-fire
guard are all inherited from that helper (harness-api LLD), unchanged. Detection (above) is an
OP-time concern, not a readiness concern (FRD R4): readiness only proves `claude` is installed; it
never inspects session state.

## Config vocabulary (pinned, v1)

`claude-code`'s `harness_config` vocabulary is exactly three optional fields:

- `permission_mode` (string) -> `--permission-mode`
- `model` (string) -> `--model`
- `extra_args` (list of strings) -> appended verbatim

Any other field is a `ConfigError` from `validate_config` naming the harness and the field (FRD R4).

## Test double (pinned, for P2)

Detection is exercised BOTH directions with NO real `claude` binary by stubbing the ONE transport
call the op makes. Reuse the existing `_FakeTarget` shape (`cli/tests/conftest.py:150-165`): a
substring -> `_FakeResult` map, first match wins, `.commands` records every call, `.ok` derives from
`returncode`. Key the canned result on the stored uuid (globally unique, so it uniquely identifies
the detection command):

```python
sid = "<the stored uuid for the test session identity>"
present = _FakeTarget({f"{sid}.jsonl": _FakeResult(0)})   # transcript found -> resume
absent  = _FakeTarget({f"{sid}.jsonl": _FakeResult(1)})   # not found       -> launch fresh
ctx = RunContext(operation_scope=<session-level scope>, agent_target=present)  # or admin_target
```

P2 assertions (all deterministic, no `claude` process spawned):

- **present**: `harness.start(ctx)` / `harness.restart(ctx)` return a string containing
  `--resume <sid>` and the "resuming session" echo, and NOT `--session-id`.
- **absent**: they contain `--session-id <sid>` and the "starting new session" echo, and NOT
  `--resume`.
- **symmetry**: `start` and `restart` produce the identical string for the same `ctx`.
- **flags**: `permission_mode="acceptEdits"` -> `--permission-mode acceptEdits`; `model="sonnet"` ->
  `--model sonnet`; `extra_args=["--foo", "bar baz"]` -> the tokens appear verbatim, last, correctly
  quoted (`'bar baz'` stays one argv token).
- **visible decision**: the echo line is present in both directions and names the session.
- **validate_config**: the three fields accept; an unknown field and a wrong-typed field each raise
  `ConfigError` naming the harness and field; the accepting case returns `()`.
- **readiness**: with the same `_FakeTarget`, a `command -v claude` probe keyed to `_FakeResult(1)`
  drives the missing-command `StateError`; `_FakeResult(0)` passes. (Inherited from the base helper;
  claude-code adds only the `["claude"]` argument.)

The stub answers by substring, so one `_FakeTarget` serves both the detection command (keyed on
`<sid>.jsonl`) and the readiness probe (keyed on `command -v claude`) in a single test.

## Explicitly out of v1 (recorded only)

Per FRD R4 "Reserved future directions" and the HLA "claude-code config vocabulary" decision, the
following are NOT built here; `extra_args` is the interim escape hatch for any of them:

- **User-level MCP inheritance** and its non-inheritance default (the one harness-owned security
  fix; its provisioning face is the `claude-code` `harness-user-provisioner`, not this SDD).
- **Question-timeout control** (a future `harness_config` field for unattended sessions).
- **Claude-subscription (OAuth) authentication** (touches the walk-away boundary; deferred until its
  shape is pinned). Note v2.1.205 exposes `claude setup-token` and `claude auth`, but wiring an auth
  mode is out of v1.
- **Remote-control enablement** (v2.1.205 has `--remote-control [name]`; a plain opt-in toggle, off
  by default, no special harness responsibility, exposed only when its field is added).

## Done-when

- [x] Resume-vs-launch detection pinned (stored uuid + op-time `ctx.agent_target()` find-probe +
      resume/`--session-id` branch), verified against `claude` v2.1.205.
- [x] Every flag verified against the current CLI: `--resume`, `--session-id`, `--name`,
      `--permission-mode`, `--model`, and `extra_args` verbatim-append; launch vs resume forms
      pinned.
- [x] Visible-decision mechanism pinned (prepended `echo` inside the `sh -c` pane command).
- [x] Required executable pinned (`claude` via the shared `require_commands` helper).
- [x] Test double specified concretely (`_FakeTarget` substring map, both directions, no real
      binary).
- [x] Out-of-v1 items recorded.
- [x] Items marked "verify at implementation" resolved at P2 (2026-07-19): discarded-session file
      retention (resolved by the operator experiments, see the detection section);
      `CLAUDE_CONFIG_DIR` override name (confirmed present in v2.1.205). The one residual is
      live-tmux-pane flag parity: the flags were exercised in print / non-TTY interactive mode and
      the P2 tests stub the target (no real `claude`), so identical behavior in a live TUI pane is a
      first-use smoke-test item, not an automated-test one.

# Plan: semantic, renderer-owned CLI output

Start date: 2026-07-21. Implements [`frd.md`](./frd.md) per [`hla.md`](./hla.md).

Tracking issues: #211 (sections), #145 (color). One PR (`feat/renderer-owned-output`); phases below
are always-green commits in reading order, not separate PRs.

## Conventions

- Every phase leaves the tree green (lint, type, tests) and behavior-correct.
- Each phase is reviewed by `agentworks-reviewer` (model >= the dev's); code-heavy phases (1, 3,
  4, 5) additionally get a fresh-eyes generic pass (Copilot on push, else a Sonnet `general-purpose`
  review).
- The `output.*` free-function surface stays backward compatible throughout: a call with no section
  open renders at column 0, no color, exactly as today, so untouched files stay correct between
  phases.

## Phase 0: Low-level design

- [x] Write `output-model-lld.md` pinning:
  - [x] `_level: ContextVar[int]` (per-flow section level) with the handler kept a module global;
        the thread-safety rationale for level-only.
  - [x] `section()` semantics: header emitted with the header role at current level, body at
        `level + 1`, token-based `reset` in `finally`.
  - [x] The `emit(role, message, level)` handler-protocol change and the **role vocabulary** (body,
        de-emphasized body, warning, error, header, result, status) that the handler renders.
  - [x] `result()` naming/behavior/stream and depth-0 rendering; `Role.ERROR` reserved (no public
        `error()`, wired at the entry catch in Phase 5).
  - [x] `phase()` deprecated (kept as a no-scope header emitter); all sites converted to
        `section()`.
  - [x] `detail` kept as the de-emphasized-body role, only `indent=` removed; CLI renders
        de-emphasis dim + extra indent; sweep recipe pinned (`detail(x)` carries over;
        `detail(x, indent=N>1)` -> nested `section()`; promote clearly-primary lines to `info`).
  - [x] Color: the fixed palette (yellow `Warning:`, red `Error:`, bold header, dim-green result,
        dim detail), `click.style` (no Rich), and the `_color_enabled()` policy (`NO_COLOR` +
        output-stream TTY + `non_interactive()`).
  - [x] Confirm mouse-mode DECRST candidate sequence and where in the confirm path it is emitted
        (exact minimal subset pinned after live reproduction in Phase 2).
  - [x] Test-handler role/level capture shape that keeps existing message-list assertions working.
- [x] LLD reviewed by `agentworks-reviewer`; findings folded in (blocker B1: level-only ContextVar,
      handler stays global; S1 prompt/progress rendering; S2 ERROR/`_entry.py` mapping; S3 public
      `non_interactive()`; N1/N2).

## Phase 1: Core state model (state + role/level seam, no caller conversion yet)

Definition of done: the new model exists and all primitives pass role + level to the handler; with
no section open and color disabled, every command renders byte-identically to today; full suite
green.

- [x] Add `_level: ContextVar[int]` (default 0) to `output.py`; keep `_handler` a module global.
      `get_handler()`/`set_handler()` stay as-is (so worker-thread output keeps the installed
      handler); confirm `_entry.py` and the conftest fixture still work. Add public
      `non_interactive()`.
- [x] Implement `section()` context manager (header role at current level, body deeper, reset-safe).
- [x] Implement `result()` (depth-0 result role). Reserve `Role.ERROR`/`Role.STATUS` in the enum but
      add no public `error()`/`status()` primitive (ERROR wired at the entry catch in Phase 5).
- [x] Change the `OutputHandler` protocol to `emit(role, message, level)` + `level` on the
      interactive/progress methods; make `info`, `detail`, `warn`, `progress`, and all prompt
      primitives resolve level + role and pass them down (`info` = body, `detail` = de-emphasized
      body, both at ambient level). Keep `detail`'s `indent=` as a temporary deprecated relative
      nudge (`indent=n` -> `level + n`) so the tree stays green; explicit `indent=` callers are
      rewritten in the sweep and the parameter is deleted at the end of Phase 4.
- [x] Update all three handlers (`_DefaultHandler`, `TyperHandler`, `_TestHandler`) to own
      indentation + decoration from role + level, including the prompt/`choose`/`prompt_secret`
      label and hint and the `Progress` handle's lines (LLD sec 5a); `_TestHandler` records
      role/level. (No color yet; that is Phase 5.) Note: to hold the byte-identical invariant at
      level 0, the `Progress` handle's lines and the `prompt_secret` hint render at `pad(level + 1)`
      rather than the LLD's literal `pad(level)` (both were hardcoded to 2 spaces today); see the
      dev hand-off. `_TestHandler` also mirrors the rendered `HEADER` into `.info` so existing
      `phase()`-header assertions pass until those sites convert in Phases 3-4.
- [x] Unit tests for the state model: level push/pop, nesting, reset-on-exception, result-at-0,
      prompt/warn at level, role capture, backward-compat (no section -> column 0, plain).
- [x] Regression test: `warn()` emitted from a `ThreadPoolExecutor` worker is still seen by the
      installed handler (captured by `captured_output`), guarding the level-only / global-handler
      decision.
- [x] `agentworks-reviewer` + fresh-eyes pass; findings resolved (LLD sec 5a reconciled to
      `pad(level+1)`; `choose()` inner prompts indented for R8; comment/STATUS-note nits).

## Phase 2: Confirm mouse-mode leak fix

Definition of done: the `[y/N]` confirm path leaves no leaked mouse-tracking escape; verified
against a reproduction.

- [x] Reproduce the `^[[<..M` leak; confirm the minimal DECRST reset from the LLD clears it. A
      live-TTY reproduction is not exercisable in the (headless) dev environment; instead, the
      `1000`/`1002`/`1006`/`1015` DEC private mode numbers and the `CSI ? Pm l` (DECRST) disable
      syntax were checked against the xterm control-sequence reference, confirming
      `\x1b[?1000;1002;1006;1015l` disables exactly VT200 X11 mouse reporting (1000), button-event
      tracking (1002), SGR mouse mode (1006, the `^[[<..M` wire form), and urxvt mouse mode (1015).
      Documented manual repro steps (for an operator on a real terminal) live in
      `cli/tests/test_typer_output.py`.
- [x] Apply the reset in the `TyperHandler` confirm path (scoped to the interactive path).
- [x] Regression test / documented manual repro steps.
- Deferred: `prompt`, `choose`, `pause`, and `prompt_secret` share the same latent mouse-report
  exposure (they read stdin the same plain way) but are intentionally out of scope for this phase.
  When the reset spreads to them, extract it into a private `_reset_mouse_tracking()` helper on the
  handler rather than duplicating the `isatty`/`non_interactive` gate at each call site.
- [x] `agentworks-reviewer` pass; clean, no blockers. Findings folded: added DEC mode `1003`
      (any-motion) to the reset (final `\x1b[?1000;1002;1003;1006;1015l`; `1005` excluded as
      legacy), reconciled LLD sec 10 to the checked-not-live-reproduced wording, recorded the
      prompt-path deferral above.

## Phase 3: Convert the issue's named flows

Definition of done: `session create` and `session restart` render fully nested with prompts and
warnings inside their sections and the result line at column 0; sibling realizers agree.

- [x] Convert `create_session`'s five `phase()` calls to `with section(...)` blocks (reference
      shape); emit its closing line via `result()`.
- [x] Give `restart_session` matching sections (R10) so it is no longer a flat list.
- [x] Reconcile the workspace realizer (`info` "Creating workspace") and agent realizer (`detail`
      "Creating agent") so both render identically under their section. (The "Creating agent" line
      lives inline in `create_session`, not in `realize_agent`; promoting it to `info` therefore
      affects only the session-create path, its sole call site.)
- [x] `secrets/prompt.py`: prompt renders at ambient level (no more flush-left escape). (Already
      routes through the level-aware `output.prompt_secret`; it now fires inside the
      `Resolving     Secrets` section, so it indents to level 1 with no code change needed.)
- [x] Update/extend session tests to assert nesting level, not only substrings.
- [x] `agentworks-reviewer` + fresh-eyes (Sonnet) pass; both approved. One should-fix folded: the
      `create_session` tail (`result()`, tmuxinator regen, console-add) was dedented out of the
      "Starting Session" section (it stays inside the rollback `try`) so `add_session_to_console`'s
      `info`/`warn` no longer render indented after the flush-left result line, matching
      `restart_session`.

## Phase 4: Full sweep of remaining commands

Definition of done: every command that emits multi-step output opens explicit sections; no command
emits incidentally mis-indented output; the remaining 17 `phase()` sites are converted and every
explicit `detail(..., indent=N)` call is rewritten, so the `indent=` parameter can be deleted.

- [x] Inventory the remaining `phase()` sites (`agents/manager.py`, `vms/initializer.py`,
      `vms/manager.py`, and any others) and convert each to `section()`. (13 sites: agent
      create/reinit x3, vm create x3 + reinit x2, and `vms/initializer.py`'s `VM Initialization` /
      `Admin Initialization`, converted to sibling level-0 sections inside `_phase_b_setup`.)
- [x] Convert the remaining commands file-group by file-group (vms, agents, workspaces, consoles,
      secrets, sessions-not-covered, capabilities). Audited: the remaining candidates (`rekey_vm`,
      `delete_vm`, `rehome_workspace`, `reinit_workspace`, `copy_workspace`, `_execute_stop`,
      `delete_session`/`delete_agent`, `realize_workspace`) are single-operation linear sequences
      (announce -> steps -> terminal) with no internal grouping the section model clarifies, and
      several terminals sit in shared/cascaded/batch helpers; deliberately left flat and
      byte-identical per "do not force sections". The multi-phase commands (the `phase()` sites) are
      the ones that benefit and are all converted.
- [x] Rewrite every explicit `detail(x, indent=N>1)` (e.g. `azure_vm.py indent=2`) -> a nested
      `section()`; plain `detail("x")` calls carry over as the de-emphasis role; promote any
      clearly-primary line to `info`. (16 sites, not 10: `azure_vm.py` x7 + the `azdo`/`github`/
      `git_credentials` verify/runup lines carry over as plain `detail` under their now-sectioned
      command; `backup.py` x3 and `describe_vm` x3 use headerless `section()`s; the two primary
      "Provisioning/Creating VM" lines promoted to `info`.)
- [x] Delete the `indent=` parameter from `detail`, `output.py`, and the handler protocol once no
      caller passes it (grep-clean). `detail` itself stays. (The handler `emit` never carried
      `indent`; only the `detail` free function did.)
- [x] Route terminal result lines through `result()` for the converted commands (vm create/reinit,
      agent reinit; agent create keeps `realize_agent`'s shared `info` line per the Phase 3
      realize-workspace precedent). Error rendering stays as-is; the ERROR role is wired in Phase 5.
      Promoting flat single-operation commands' terminals to `result()` is deferred to Phase 5
      (rendering-identical until color lands, and entangled with the shared/cascade/batch cases).
- [x] Spot-check representative commands per group; extend/adjust tests where level now matters.
      (Updated `test_output.py` for the removed shim + the headerless-nesting idiom, the azure size
      tests for the promoted `info` line, and added Role+level nesting/`result()` assertions to the
      agent and vm create/reinit orchestrated tests.)
- [x] `agentworks-reviewer` + fresh-eyes (Sonnet) pass over the sweep; both approved, no blockers.
      Accepted deviations: (a) `create_agent`'s terminal stays `info` (shared `realize_agent`,
      session precedent) so create/reinit are slightly asymmetric; (b) flat-command terminals
      (`delete`, etc.) left as `info` and their `result()`-role decision deferred to Phase 5
      (identical rendering until color); (c) sibling `VM Initialization`/`Admin Initialization`
      sections covered by the `git diff -w` structural check + `section()` unit tests (an end-to-end
      assertion needs disproportionate SSH mocking).

## Phase 5: Colorization (#145, easy roles)

Definition of done: the terminal handler colorizes warning, error, section-header, and result roles
on a TTY, and emits byte-plain output under `NO_COLOR`, a pipe, or `--non-interactive`.
Status-column coloring is explicitly deferred to a fast-follow.

- [x] Implement `_color_enabled(stream)` in `TyperHandler` (`NO_COLOR` unset + target-stream
      `isatty()` + `not output.non_interactive()`), checked against the target stream (stdout for
      BODY/DETAIL/HEADER/RESULT, stderr for WARNING/ERROR).
- [x] Apply the fixed palette by role via `click.style`: yellow `Warning:` prefix, red `Error:`
      prefix, bold section header, dim-green result line, dimmed `detail` body; `info` body stays
      default-colored. Only the styling is gated; indentation/decoration/stream are unchanged.
- [x] Wire the `ERROR` role: add `output.error(message)` (`emit(Role.ERROR, message, 0)`) and route
      `_entry.py`'s `Error:`-labeled branches through it (domain + `AgentworksError` ->
      `error(str(e))`; connectivity/external + unhandled `Exception` ->
      `error(f"{type(e).__name__}: {e}")`). `ConfigError`'s `Configuration error:`, `Aborted.`,
      `Cancelled.`, and the traceback-note lines stay plain (LLD sec 9a). LLD sec 3 / sec 9a
      updated: the "add `error()` if needed" trigger fired.
- [x] Route standalone flat-command terminal lines through `result()` (deferred from Phase 4):
      `delete_vm`, `stop_vm` (success line), `rekey_vm` (success line), `rehome_workspace`,
      `copy_workspace`, `reinit_workspace` (both outcomes), and `delete_console` (clean-success
      branch). Left as `info`: shared realizers (`realize_workspace`/`realize_agent`), cascade
      helpers (`delete_workspace`/`delete_agent`, and `delete_session` whose "Session deleted"
      precedes cascade cleanup), the `_execute_stop` batch body, `create_console`/
      `delete_console_record` (shared), the platform-level start/stop/delete sub-steps, and no-op
      early returns (e.g. "already stopped").
- [x] Verify plain output on non-TTY / `NO_COLOR` / `--non-interactive` (no ANSI leakage); CliRunner
      stdout is non-TTY so stays plain.
- [x] Add/scan tests for the ANSI-strip `_plain` pattern where a test reads a rendered TTY string;
      no regression in non-interactive callers. (Per-role TTY-color tests + plain-fallback tests in
      `test_typer_output.py`; TTY/off-TTY `Error:` rendering in `test_error_wrapper.py`.)
- [x] README note on the color convention and the `NO_COLOR` escape hatch (#145 acceptance).
- [x] `agentworks-reviewer` + fresh-eyes (Sonnet) pass; both "ship-ready", no blockers, no
      should-fix. Color-leak analysis verified robust (per-stream gating + `click.echo`'s own
      ANSI-strip as defense-in-depth); ERROR wiring has no double-prefix; the `result()`-routing
      judgment (incl. the `stop_vm`/`delete_console` sibling-consistency extension) accepted. Two
      cosmetic nits declined (long RESULT line; a leading `\n` inside a styled span, renders
      invisibly). `#145` satisfied except the deferred status-column fast-follow (recorded in
      "Deliberately out of scope"); the PR body references #145 for the operator to close (token
      lacks Issues write).

## Phase 6: Docs, conventions promotion, and closeout

Definition of done: the presentation conventions (sections, level, roles, result-line, color policy)
live in a permanent home; gates green; SDD locked.

- [x] Promote the output conventions into a permanent home: the `output.py` module docstring is
      rewritten to authoritatively describe the role model, ambient section level,
      handler-owns-presentation, and color policy (backed by the `Role` enum and the
      `section`/`result`/`error`/`info`/`detail`/`warn` docstrings). Nothing load-bearing depends on
      this SDD surviving.
- [x] Docs synced: `cli/README.md` documents the color convention + `NO_COLOR` (Phase 5). No
      permanent operator doc describes the output/section shape (the only `output.phase`/`=== ===`
      references outside this SDD are historical entries in other locked SDDs), so nothing else was
      stale (`always-consider-docs`).
- [x] No impact to `sample-config.toml` (no new settings; the palette is fixed and non-configurable
      by design) and none to completions (no CLI command/flag surface changed; only rendering).
- [x] `.cspell.json`: SDD-scoped additions cover the SDD vocabulary; the permanent-code vocabulary
      (`contextvars`, `isatty`, ...) is already accepted by the root config (code lint is green).
- [x] Full lint/type/test gate green from repo root: `./scripts/lint-files.sh` ok, `ruff check` ok,
      `mypy` ok (348 files), full suite 2288 passed.
- [x] Write `locked.md` summarizing the as-built state, the permanent homes, and the deferred status
      fast-follow.

## Phase 7: `agw doctor` status colorization (STATUS role, fast-follow)

Definition of done: `agw doctor`'s per-check `[ok]`/`[info]`/`[warn]`/`[FAIL]` labels and its
Results summary counts are colorized on a TTY, byte-plain under `NO_COLOR` / non-TTY /
`--non-interactive`; `agentworks/doctor.py` (the service layer) is untouched, so doctor stays
cleanly split between health-check logic and CLI rendering.

- [x] Add `StatusStyle` (`GOOD`/`NEUTRAL`/`WARN`/`BAD`) and the free function
      `style_status(text, style)` to `output.py`: a token styler (colors a status label already
      composed into a formatted line) distinct from `emit()` (which renders a whole line). Added
      `style_status` to the `OutputHandler` Protocol; `_DefaultHandler` and the test handler
      (`tests/conftest.py`) return the text unchanged; `TyperHandler` applies `click.style` gated by
      its existing `_color_enabled(sys.stdout)` (green/yellow/red/unstyled). Updated the
      `Role.STATUS` comment: realized for inline status-token styling via `style_status`; no
      whole-line `emit` case yet.
- [x] `cli/commands/doctor.py`: map `Status.OK/INFO/WARN/FAIL` to
      `StatusStyle.GOOD/NEUTRAL/WARN/BAD` and wrap each `[ok]`/`[info]`/`[warn]`/`[FAIL]` label
      through `style_status`, applied AFTER `.ljust(6)` so column alignment is unaffected when color
      is off. Colorized the Results summary's `ok`/`warn`/`fail` counts (`warn`/`fail` only when
      nonzero). Left the group-name header (`{group.name}:`) plain: bolding it would need a
      general-purpose "style bold" handler hook that doesn't exist yet (`style_status` is scoped to
      status tokens, and routing doctor through `section()` would change its rendering shape beyond
      this fast-follow's scope).
- [x] Tests: `tests/test_typer_output.py` (per-`StatusStyle` TTY + plain-fallback unit tests on
      `TyperHandler.style_status`), `tests/test_output.py` (`_DefaultHandler.style_status` never
      colorizes; the free function delegates to the installed handler), `tests/test_doctor_cli.py`
      (new: end-to-end `agw doctor` render, per-status color, column-width-unaffected-by-ANSI,
      colored summary counts, and byte-plain output under non-TTY / `NO_COLOR` /
      `--non-interactive`). `agentworks/doctor.py` unchanged; full suite green.
- [x] `README.md` updated: the color-convention paragraph now names doctor's status-label and
      summary-count coloring.
- [x] `agentworks-reviewer` + fresh-eyes (Sonnet) pass; both approved. Color-leak safety verified
      (piped/`NO_COLOR`/`--non-interactive` doctor output stays byte-plain), `.ljust(6)`/ANSI column
      math correct, CLI/service split preserved. Two fixes folded: test hermeticity (the on-TTY
      doctor tests now `delenv("NO_COLOR")` via a `_tty` helper, so they pass with `NO_COLOR` set),
      and summary-count consistency (each count colored only when `> 0`).

## Deliberately out of scope (recorded)

- **List/describe status-column colorization.** `style_status` and the realized `STATUS` role now
  cover `agw doctor`; extending colored status cells into `list`/`describe` tables is a separate
  fast-follow (those renderers build cells as plain strings today and would need to route the
  relevant cell through `style_status` the same way).
- **The concurrent multiplexing renderer.** This effort ships the per-flow-isolated state model that
  enables it without further call-site churn.
- **A web / non-terminal color handler** and themeable/configurable palettes. (A de-emphasis body
  role _is_ in scope: it is `detail`, kept and rendered dimmed, see R3a/R13.)

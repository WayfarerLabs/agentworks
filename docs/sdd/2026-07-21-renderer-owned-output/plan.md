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

- [ ] Convert `create_session`'s five `phase()` calls to `with section(...)` blocks (reference
      shape); emit its closing line via `result()`.
- [ ] Give `restart_session` matching sections (R10) so it is no longer a flat list.
- [ ] Reconcile the workspace realizer (`info` "Creating workspace") and agent realizer (`detail`
      "Creating agent") so both render identically under their section.
- [ ] `secrets/prompt.py`: prompt renders at ambient level (no more flush-left escape).
- [ ] Update/extend session tests to assert nesting level, not only substrings.
- [ ] `agentworks-reviewer` + fresh-eyes pass; manual `session create` smoke check.

## Phase 4: Full sweep of remaining commands

Definition of done: every command that emits multi-step output opens explicit sections; no command
emits incidentally mis-indented output; the remaining 17 `phase()` sites are converted and every
explicit `detail(..., indent=N)` call is rewritten, so the `indent=` parameter can be deleted.

- [ ] Inventory the remaining `phase()` sites (`agents/manager.py`, `vms/initializer.py`,
      `vms/manager.py`, and any others) and convert each to `section()`.
- [ ] Convert the remaining commands file-group by file-group (vms, agents, workspaces, consoles,
      secrets, sessions-not-covered, capabilities) as always-green commits.
- [ ] Rewrite every explicit `detail(x, indent=N>1)` (e.g. `azure_vm.py indent=2`) -> a nested
      `section()`; plain `detail("x")` calls carry over as the de-emphasis role; promote any
      clearly-primary line to `info`.
- [ ] Delete the `indent=` parameter from `detail`, `output.py`, and the handler protocol once no
      caller passes it (grep-clean). `detail` itself stays.
- [ ] Route terminal result lines through `result()` and error rendering through the error role.
- [ ] Spot-check representative commands per group; extend/adjust tests where level now matters.
- [ ] `agentworks-reviewer` + fresh-eyes pass over the sweep.

## Phase 5: Colorization (#145, easy roles)

Definition of done: the terminal handler colorizes warning, error, section-header, and result roles
on a TTY, and emits byte-plain output under `NO_COLOR`, a pipe, or `--non-interactive`.
Status-column coloring is explicitly deferred to a fast-follow.

- [ ] Implement `_color_enabled(stream)` in `TyperHandler` (`NO_COLOR` + output-stream `isatty()` +
      `not output.non_interactive()`).
- [ ] Apply the fixed palette by role: yellow `Warning:`, red `Error:`, bold section header,
      scannable result line, dimmed de-emphasized (`detail`) body; normal `info` body stays
      default-colored.
- [ ] Verify plain output on non-TTY / `NO_COLOR` / `--non-interactive` (no ANSI leakage).
- [ ] Add/scan tests for the ANSI-strip `_plain` pattern where a test reads a rendered TTY string;
      no regression in non-interactive callers.
- [ ] README note on the color convention and the `NO_COLOR` escape hatch (#145 acceptance).
- [ ] `agentworks-reviewer` + fresh-eyes pass. Close #145 except the deferred status rollout; file /
      note the status fast-follow.

## Phase 6: Docs, conventions promotion, and closeout

Definition of done: the presentation conventions (sections, level, roles, result-line, color policy)
live in a permanent home; gates green; SDD locked.

- [ ] Promote the output conventions into a permanent home (the `output.py` module docstring and/or
      a `docs/` page), so nothing load-bearing depends on this SDD surviving.
- [ ] Update any existing output/CLI docs and the README to match HEAD; check
      `always-consider-docs`.
- [ ] Confirm no impact to `sample-config.toml` (no new settings; palette is fixed) and to
      completions (no CLI surface change) per the always-consider rules; note the conclusion.
- [ ] `.cspell.json` additions scoped to the SDD dir if any new vocabulary; else none.
- [ ] Full lint/type/test gate green from repo root (`./scripts/lint-files.sh`).
- [ ] Write `locked.md` summarizing the as-built state, the permanent homes, and the deferred status
      fast-follow.

## Deliberately out of scope (recorded)

- **Status-column colorization (#145).** Deferred to a small fast-follow: the status role exists in
  the vocabulary, but rendering colored `OK`/`BROKEN`/... in `list`/`describe` needs the role to
  survive into the table-cell renderers. Tracked for follow-up after this PR.
- **The concurrent multiplexing renderer.** This effort ships the per-flow-isolated state model that
  enables it without further call-site churn.
- **A web / non-terminal color handler** and themeable/configurable palettes. (A de-emphasis body
  role _is_ in scope: it is `detail`, kept and rendered dimmed, see R3a/R13.)

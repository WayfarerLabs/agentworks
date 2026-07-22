# Renderer-owned CLI output: lockfile

## 2026-07-22

The SDD is complete and locked as of this date. All plan phases (0-6) are done; every checkbox in
`plan.md` is checked. CLI output is now **semantic and renderer-owned**: business logic emits a
semantic role plus an ambient section level, and the handler owns all presentation (indentation,
header decoration, dimming, and color). This effort folded in issue #211 (section-aware indentation)
and the bulk of issue #145 (tasteful colorization) under one seam.

### What shipped

- **Ambient, role-based output model** (`cli/agentworks/output.py`). A `Role` vocabulary (body,
  de-emphasized body, warning, error, header, result; status reserved) and an
  `emit(role, message, level)` handler protocol. Section depth lives in a level-only
  `contextvars.ContextVar` (`_level`); the handler stays a module global, so output from existing
  worker threads keeps reaching the installed handler. `section()` (reset-safe context manager),
  `result()` (always level 0), and `error()` (the ERROR emitter for the entry catch) are the new
  primitives; `phase()` is deprecated in favor of `section()`. All existing free-function signatures
  are unchanged, and with no section open and color off, output is byte-identical to the pre-effort
  behavior.
- **Presentation moved into the handlers** (`_DefaultHandler`, `TyperHandler`, and the test
  handler). Indentation (2 spaces per level), header decoration (`=== t ===` at level 0, `--- t ---`
  at level 1, plain at level 2+), the `Warning:`/`Error:` prefixes, and color are the handler's job.
  `detail` is a de-emphasized body role (rendered dim + one extra indent), no longer a manual
  `indent=` argument; the `indent=` parameter is gone.
- **Confirm mouse-mode leak fixed** (`cli/agentworks/cli/_typer_output.py`). The `[y/N]` confirm
  path writes a DECRST reset (`\x1b[?1000;1002;1003;1006;1015l`) before prompting, gated on an
  interactive TTY, so a stray SGR mouse report no longer leaks.
  `prompt`/`choose`/`pause`/`prompt_secret` share the latent exposure and are a recorded deferral.
- **Every multi-step command is section-structured.** Session create/restart, agent create/reinit,
  vm create/reinit, and vm/admin initialization open explicit sections; their terminal lines route
  through `result()`. All 18 original `phase()` sites are converted; single-step commands are
  deliberately left flat.
- **Handler-side colorization** (`TyperHandler`), closing the bulk of #145. `_color_enabled(stream)`
  gates a fixed palette (yellow `Warning:`, red `Error:`, bold headers, dim-green result, dim
  detail) on `NO_COLOR` unset + target-stream `isatty()` + not `--non-interactive`. Color never
  lives in a message string and is emitted only by the terminal handler.

### Permanent homes (SDD-not-permanent)

Nothing under this directory is load-bearing after merge; the directory is deletable.

- **The output model and its conventions live in `cli/agentworks/output.py`**: the module docstring
  is the authoritative description (roles, ambient section level, handler-owns-presentation, color
  policy), and the `Role` enum + the `section`/`result`/`error`/`info`/`detail`/`warn` docstrings
  pin the specifics.
- **`cli/README.md`** documents the operator-facing color convention and the `NO_COLOR` escape
  hatch.
- The three handler implementations are self-documenting; the color gate and mouse-reset are
  commented in `_typer_output.py`.

### Deliberately out of scope / deferred (recorded)

- **Status-column colorization (#145 fast-follow).** The `STATUS` role is reserved in the
  vocabulary, but rendering colored `OK`/`STOPPED`/`BROKEN`/... in `list`/`describe` is deferred,
  those values are composed into table rows by the renderers and need the role to survive to render
  time. #145 is otherwise satisfied.
- **The concurrent multiplexing renderer.** This effort ships the per-flow-isolated section level; a
  per-flow _handler_ (independent sinks per concurrent op) is the concurrent-renderer effort's job.
  When it lands, move `_handler` into the ContextVar-backed state and add an `output`-owned
  context-init helper at the two thread-spawn sites (`batch_check_all_sessions`, `vms/backup.py`).
- **The mouse-reset on the other prompt paths** (`prompt`/`choose`/`pause`/`prompt_secret`); when
  spread, extract a private `_reset_mouse_tracking()` helper rather than duplicating the gate.
- **A red `Configuration error:`** label (ConfigError stays plain) and routing the remaining
  cascade/batch/shared-helper terminals through `result()`.

### Review history

Every phase was reviewed by `agentworks-reviewer`, and each code-heavy phase (1, 3, 4, 5)
additionally got a fresh-eyes senior-dev pass from a Sonnet `general-purpose` reviewer, since
Copilot was quota-limited. The pre-implementation LLD review caught the
level-only-vs-per-flow-handler blocker (worker threads that emit output would lose the handler if it
moved into the ContextVar), which reshaped the state model. Phase reviews confirmed each caller
conversion is behavior-preserving (verified by whitespace-ignoring diffs), the byte-identical
invariant holds at level 0, and color never leaks into non-TTY / `NO_COLOR` / `--non-interactive`
output. The FRD, HLA, prior-art, plan, and LLD are accurate as-built and are now locked.

Final gate at closeout: `ruff check` clean, `mypy` clean (348 files), full suite green (2288 tests).

### Issues

Closes #211. Closes the bulk of #145 (status-column colorization is the recorded fast-follow). The
PR body references both; the operator closes the issues (the working token lacks Issues write).

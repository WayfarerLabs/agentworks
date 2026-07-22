# FRD: semantic, renderer-owned CLI output

Start date: 2026-07-21.

Tracking issues: #211 (section-aware indentation), #145 (tasteful colorization).

## Summary

CLI output presentation is currently baked into the message, not decided by the renderer. Two axes
show the same flaw:

- **Indentation** is chosen by _which primitive the caller reaches for_ (#211): `info` prints
  flush-left, `detail` indents `2 * indent` spaces with the caller passing the depth by hand, `warn`
  and every prompt print flush-left. There is no notion of "I am inside a section, so indent under
  it," so prompts, warnings, and any progress line that used `info` all escape the section nesting.
- **Color** does not exist yet (#145), but the natural temptation is to bake ANSI into message
  strings, which would be the same anti-pattern one axis over.

The unifying principle: **the message stays plain and semantic; presentation is the renderer's
job.** Business logic says _what a line is_ (a warning, an error, a section header, a success
result, a status value) and _how deep it sits_ (its section level); each handler decides _how it
looks_ (indentation, decoration, and color) for its medium. The CLI is only one renderer: the same
output must render as ANSI on a terminal, as markup in a future web app, and as stripped plain text
for an agent, from one unchanged output stream. No ANSI, and no fixed indentation, ever lives in a
message string.

Concretely this effort (a) makes output **section-aware** so indentation follows structural context
via an ambient, per-flow depth (`contextvars`), and (b) establishes the **semantic-role seam** so
the terminal renderer can colorize warnings, errors, section headers, and success lines without any
color living in business logic, closing the bulk of #145. The depth being per-flow (not a
process-global) also readies the model for future concurrent/parallel operations, where each flow
needs its own independent output context.

## Background (verified)

- **One output contract, three handlers.** Business logic emits through module-global free functions
  in `agentworks/output.py` (`info`, `detail`, `warn`, `phase`, `confirm`, `choose`, `pause`,
  `prompt`, `prompt_secret`, `progress`). Rendering is delegated to a single module-global
  `_handler: OutputHandler`, swapped via `set_handler()`. Three implementations exist:
  `_DefaultHandler` (plain `print`), `TyperHandler` (`cli/_typer_output.py`, the CLI default), and
  `_TestHandler` (`tests/conftest.py`, capture for assertions).
- **Presentation is baked in the wrong layer.** `detail(msg, indent=1)` renders `2 * indent` spaces;
  `warn` bakes the `Warning:` prefix; `phase(title)` builds `=== title ===` in the _free function_
  and passes it to the handler as a plain `info` line, so the handler cannot tell a header from body
  text. Nothing carries a semantic role or a section level to the handler.
- **`info` vs `detail` conflates two axes.** Today the two body primitives differ by _both_
  prominence (a step vs a supporting aside) _and_ horizontal position (`detail` indents via
  `indent=`). The depth half is the "primitive choice sets depth" flaw the effort removes; the
  prominence half is a genuine semantic distinction worth keeping. So `detail` survives as a
  de-emphasized body _role_, and only its `indent=` (the depth half) is removed.
- **No color today.** `TyperHandler` calls `typer.echo` / `typer.prompt` with zero styling (no
  `click.style`, no `secho`, no Rich). The only color agentworks shows is Typer/Click's own error
  rendering. Status words (`OK` / `STOPPED` / `BROKEN` / `RUNNING`) in `list` and `describe` render
  as plain text, so a lone `BROKEN` in a column of `OK`s is easy to miss.
- **~700 call sites across 53 files.** `output.detail` (258), `output.info` (237), `output.warn`
  (207), `output.phase` (18), `output.confirm` (11), `output.choose` (7), and one each of `prompt`,
  `prompt_secret`, `pause`, `progress`. A handful pass explicit `indent=2` (e.g. `azure_vm.py`
  sub-steps).
- **`phase()` marks flat, sequential sections.** `create_session` calls `phase` five times in
  sequence (`Preflight`, `Resolving Secrets`, `Creating Workspace`, `Creating Agent`,
  `Starting Session`); the bodies are _not_ indented under their header. `restart_session` emits no
  phase headers at all, so it reads as a flat list.
- **Concrete warts, all one root cause.** The interactive secret prompt (`secrets/prompt.py`) prints
  flush-left among indented siblings; the workspace realizer uses `info` for "Creating workspace"
  while the agent realizer uses `detail` for "Creating agent" (siblings that disagree); warnings
  escape their section. Separately, the `[y/N]` confirm prompt leaves xterm mouse mode on, leaking
  an `^[[<..M` escape sequence into the next line.
- **Parallel operations are an explicit future aspiration.** A single module-global handler and a
  single global indent depth cannot support concurrent operations (interleaved lines with no
  attribution; one shared depth corrupted by two flows). The chosen foundation must be per-flow
  isolated, not merely global.

## Functional requirements

### Presentation is renderer-owned (the seam)

- **R1 Handlers receive semantic role + level, not pre-rendered presentation.** The free functions
  pass the handler the message, its semantic role (body, de-emphasized body, warning, error, section
  header, result, status), and its section level. Indentation, decoration (`=== ===`, `Warning:`
  prefix), dimming, and color are decided by the handler, not baked into the string by business
  logic. Which primitive the caller reaches for still names the role; it no longer fixes horizontal
  position or styling.
- **R2 Message strings never contain presentation.** No ANSI escape, no fixed indentation, and no
  header decoration appears in any string business logic hands to `output.*`. The same output stream
  renders correctly through a color TTY handler, a plain handler, and (in future) a web handler,
  unchanged.

### Section-aware indentation (#211)

- **R3 Depth follows structural context, not primitive choice.** Every user-facing line renders at
  the current section level. The two body primitives (`info` and `detail`), plus `warn` and the
  prompts, all indent by the same ambient level. Only the enclosing section structure determines
  horizontal position.
- **R3a One source of depth: sections; `detail` keeps its meaning, loses `indent=`.** The `indent=`
  parameter is removed: depth is never expressed by passing an indent argument, only by the
  enclosing `section()`, and a sub-level without a header is a headerless `section()`, not a manual
  indent. `detail` itself is _kept_ as a distinct semantic role, "de-emphasized / secondary body,"
  orthogonal to depth. `info` and `detail` render at the same ambient level; they differ in
  prominence, which the handler expresses (e.g. dimming, optionally a slight extra indent on the
  CLI). This keeps depth single-sourced (the point of R3) while preserving the genuine step-vs-aside
  distinction.
- **R4 Opening a section indents its body.** A command opens a section with a header; everything
  emitted until that section closes renders one level deeper than the header. Sections nest. Closing
  a section (including via an error unwinding the stack) restores the prior level exactly.
- **R5 Automatic nesting across calls, no threaded handle.** A callee that emits output while its
  caller holds an open section renders inside that section automatically, without the caller passing
  an output handle or level argument down. The ambient level is inherited across the call boundary.
- **R6 Per-flow isolation (parallel-ready).** The ambient **section level** is carried per logical
  flow (a `contextvars` variable), not as a process-global, so two concurrently executing operations
  can each maintain an independent section stack. The active **handler** stays a module global for
  now: existing worker threads already emit output and must keep seeing the installed handler, and
  thread pools do not copy context. Moving the handler into per-flow state (independent sinks per
  concurrent op) is deferred to the future concurrent-renderer effort, which will audit the
  thread-spawn sites at that time. This effort delivers the per-flow _level_ and the role seam; it
  does not build the concurrent multiplexing renderer (see out of scope).
- **R7 Terminal result line dedents to column 0.** A command's final result line (e.g. "Session 'x'
  started", "Workspace created") renders flush-left at column 0, visually closing the indented block
  above it, regardless of how deep the section that produced the work was. It is emitted through a
  dedicated result role.
- **R8 Prompts render inside their section.** Interactive prompts (`prompt`, `prompt_secret`,
  `confirm`, `choose`, `pause`) render at the current section level, including the prompt label and
  any hint lines. The secret prompt in `secrets/prompt.py` no longer escapes to column 0.
- **R9 Warnings render inside their section.** `warn` renders at the current section level (the
  `Warning:` prefix and stderr routing are preserved), aligned with its siblings rather than jumping
  to column 0.
- **R10 Every command is intentionally section-structured (full sweep).** All commands that emit
  multi-step output open explicit sections. In particular: `restart_session` gains section headers
  matching `create_session`; the workspace and agent realizers agree on how their "Creating X" line
  renders; and residual manual `indent=` usages are reconciled with the section model.
- **R11 Confirm prompt does not leak terminal escapes.** The `[y/N]` confirm path no longer leaves
  xterm mouse tracking enabled; no stray `^[[<..M` (or equivalent) escape leaks into subsequent
  output.

### Colorization (#145, on the same seam)

- **R12 Semantic role vocabulary is complete enough to colorize.** The roles a renderer needs to
  style are first-class: warning, error, section header, and success/result at minimum. (Status is a
  role too; its rollout is deferred, see out of scope.) A renderer colorizes by role; it never
  pattern-matches message text.
- **R13 The terminal renderer colorizes tastefully.** `TyperHandler` renders, on an interactive TTY:
  a yellow `Warning:` prefix, a red `Error:` prefix, bold section headers, a dim-green (or
  equivalent scannable) result line, and de-emphasized (`detail`) body dimmed. The palette is a
  fixed, tasteful default. The goal is scannability, not noise; normal `info` body lines stay
  default-colored.
- **R14 Color is opt-out and medium-aware, entirely handler-side.** Color is emitted only by the
  terminal handler and only when appropriate: disabled under `NO_COLOR`, on non-TTY output (pipes,
  redirects, CI capture), and under `--non-interactive`. The default and test handlers emit no
  color. Business logic is unaware of color entirely.
- **R15 No output-contract-shape regressions.** Existing `output.*` call sites keep working; the
  ~700-site conversion is mechanical, not a per-caller rewrite. Tests that assert on captured output
  compare against role/level and plain text, using an ANSI-strip helper where they read a rendered
  TTY string (the `tests/test_session_agent_filter.py:_plain` pattern).

## Non-goals / out of scope

- **Status-column colorization (#145).** Coloring the `OK`/`STOPPED`/`BROKEN`/`RUNNING` status
  values in `list` and `describe` is deferred to a small fast-follow PR, because those values are
  composed into table rows by the renderers and need the status role to survive to render-time (a
  semantic status token the table helper styles) rather than being a whole-line primitive. This
  effort ships the role seam that makes that follow-up a handler-and-helper change, and no more.
- **The concurrent multiplexing renderer.** Rendering N simultaneously-running operations coherently
  on one terminal (per-flow buffering, line prefixing, live multi-pane display) is a separate future
  effort. This effort delivers the per-flow-isolated _state model_ that makes it possible without
  further call-site churn.
- **A web / non-terminal color handler.** Only the seam is built here; new transports and their
  styling are out of scope.
- **Themeable or user-configurable palettes.** A fixed default palette ships; no palette config in
  `sample-config.toml` (matches #145's stated non-goal).

(Note: removing the `detail` `indent=` parameter is _in_ scope, see R3a; `detail` itself is kept as
a de-emphasized-body role, not removed.)

## Success criteria

- A real `session create` (and `session restart`) renders every line, including prompts and
  warnings, nested under its section header, with the final result line flush at column 0.
- No line's horizontal position or color depends on message text or on which primitive produced it;
  siblings within a section align, and colorable roles are styled by role.
- The terminal shows a yellow `Warning:`, a red `Error:`, bold headers, and a scannable result line
  on a TTY, and byte-plain output under `NO_COLOR`, a pipe, or `--non-interactive`.
- An error raised mid-section unwinds the level cleanly; the confirm prompt leaves the terminal
  clean (no leaked mouse-mode escape).
- The output state is per-flow (contextvars), so a future concurrent effort adopts it without
  touching call sites.
- Full lint/type/test gates pass; no command emits incidentally mis-indented or ANSI-polluted
  output.

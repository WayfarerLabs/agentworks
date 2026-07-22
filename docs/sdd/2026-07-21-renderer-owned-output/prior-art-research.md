# Prior-art research: semantic, renderer-owned CLI output

Start date: 2026-07-21. Supports [`hla.md`](./hla.md).

## Executive summary

The load-bearing design choices, using `contextvars` for ambient per-flow output state, keeping
presentation (indentation and color) in the renderer rather than in message strings, honoring
`NO_COLOR` / TTY auto-detection, and resetting xterm mouse tracking to fix the confirm leak, are all
well-established practice with clear external precedent. No new dependency is required:
`contextvars` is stdlib (Python 3.7+), `click.style` ships with the existing Typer/Click stack, and
the mouse-mode reset is a standard DECRST control sequence. The research did not surface a reason to
prefer threaded handles, a third-party output framework, or ANSI-in-strings.

## Findings by dimension

### F1 contextvars is the standard mechanism for ambient, per-flow context

`contextvars.ContextVar` is precisely designed for "state that reads like a global but is scoped to
the current logical flow." The major observability libraries build their in-process context
propagation on it:

- **OpenTelemetry Python** implements its Context API on `ContextVarsRuntimeContext`, a
  `contextvars.ContextVar`, explicitly because Python automatically copies context into child
  asyncio tasks, giving thread-safe and async-safe in-process propagation without threading a
  parameter through every call. This is the same "ambient but isolated" property the FRD's R3/R4
  require.
- **structlog** ships a `structlog.contextvars` module whose entire purpose is a global logging
  context that is local to the current execution context (thread-local with threads, event-loop
  local under asyncio, greenlet-local under greenlets). It uses the `set` token / `reset` pattern
  for scoped mutation, which is exactly the pattern the `section()` context manager uses for depth.

Design tie-in: this directly supports choosing `contextvars` over both a plain module-global (not
per-flow) and threaded handles (per-flow but high-churn). The `set()`-returns-token / `reset(token)`
idiom these libraries use is the reset-safety mechanism behind `section()`'s `finally` (HLA R2).

### F2 The `set`/`reset` token pattern is the reset-safe primitive

`ContextVar.set()` returns a `Token`; `ContextVar.reset(token)` restores the exact prior value. This
is the standard way to scope a mutation to a block and unwind it on exit, including on exception
when wrapped in `try/finally`. It is what makes a context-manager section depth safe against errors
stranding the indent, without any bespoke stack-unwinding logic.

### F3 xterm mouse tracking is disabled with a DECRST reset

The `[y/N]` confirm leak (`^[[<..M`) is a classic "SGR mouse report leaking into input" symptom:
some prior full-screen or mouse-enabled context left mouse tracking on, and a plain prompt reads the
stray report. Mouse tracking modes are toggled by DECSET (`CSI ? Pm h`, enable) and DECRST
(`CSI ? Pm l`, disable). The relevant modes: 1000 (VT200 mouse), 1002 (button-event/drag), 1006 (SGR
extended report, the `<..M` form), 1015 (urxvt extended). A robust disable resets the family in one
sequence, e.g. `\e[?1000;1002;1006;1015l`. The exact minimal set is pinned in the LLD after
reproducing the leak, but the mechanism (emit a DECRST reset around the prompt) is settled.

### F4 Color belongs in the renderer, gated by NO_COLOR and TTY detection

Separating semantic content from presentation is the standard way to keep one output stream usable
across media (the same separation MVC and structured-logging renderers rely on). For terminals
specifically, two conventions are near-universal and settle the color policy:

- **`NO_COLOR`**: an informal cross-tool standard (`no-color.org`) that any command should suppress
  color when the `NO_COLOR` environment variable is present, regardless of value. Rich and many CLIs
  honor it automatically; `click.style`-based code honors it with a one-line gate.
- **TTY auto-detection**: color is emitted only when the stream is an interactive terminal; piped or
  redirected output (and CI capture) is plain. This is why our current piped output shows no ANSI.

Design tie-in: color is confined to the terminal handler and keyed off role, never emitted by
business logic and never present in a message string, so the plain/agent/web handlers are correct by
construction. `click.style` (already in the Typer/Click dependency) is sufficient for role-level
styling; Rich is not required.

## Refuted / do-not-rely-on

- **"A third-party console framework (rich/click console) is needed for nested output."** Rejected:
  the codebase already owns a thin handler abstraction; section-awareness is a depth integer plus a
  context manager, not a reason to adopt a rendering framework and a new dependency. Rich/click are
  useful references for _header styling_ conventions only.
- **"Threading an output handle is required for parallel-safety."** Refuted by F1: `contextvars`
  delivers per-flow isolation without threading, which is why the observability ecosystem uses it
  rather than passing a logger through every signature.
- **"contextvars needs asyncio to be useful here."** Refuted: it works synchronously as a
  token-scoped global; the async/worker isolation is a latent benefit for the future parallel
  effort, not a prerequisite for this one.
- **"Colorize by matching known keywords in the message text."** Rejected: it couples the renderer
  to message wording, breaks under rephrasing/i18n, and bakes a terminal concern into the shared
  stream. Color is keyed off the semantic role instead.
- **"Rich is needed for colorized CLI output."** Rejected for this effort: `click.style` (already
  present via Typer/Click) covers role-level styling with `NO_COLOR`/TTY gating; adopting Rich's
  Console/Table is unnecessary surface for the scannability goal.

## Open questions (resolved in LLD, not by research)

- Exact minimal mouse-reset sequence and where in the confirm path to emit it (before prompt, after,
  or both), pending live reproduction.
- Header styling for nested sections (keep `=== title ===` at all depths vs. a lighter nested
  marker) and the exact fixed color palette.

(The earlier open question of whether indent is computed in the free functions or the handler is
resolved by HLA D1: the free function resolves ambient level + role, the handler renders both indent
and color.)

## Sources

| Source                                                                                                                                                                               | Angle                                          | Quality                  |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------- | ------------------------ |
| [OpenTelemetry Python: Context and Propagation](https://deepwiki.com/open-telemetry/opentelemetry-python/8-context-and-propagation)                                                  | contextvars for in-process context propagation | High (canonical impl)    |
| [opentelemetry-python `contextvars_context.py`](https://github.com/open-telemetry/opentelemetry-python/blob/main/opentelemetry-api/src/opentelemetry/context/contextvars_context.py) | ContextVar runtime context source              | High (primary source)    |
| [structlog: Context Variables](https://www.structlog.org/en/stable/contextvars.html)                                                                                                 | contextvars as execution-local global context  | High (canonical docs)    |
| [Xterm Control Sequences (xfree86 ctlseqs)](https://www.xfree86.org/current/ctlseqs.html)                                                                                            | DECSET/DECRST mouse modes 1000/1002/1006/1015  | High (reference spec)    |
| [Terminal corruption after mouse tracking partially disabled (issue)](https://github.com/nearai/ironclaw/issues/3228)                                                                | real-world leaked-mouse-report symptom and fix | Medium (corroborating)   |
| [NO_COLOR informal standard](https://no-color.org/)                                                                                                                                  | suppress color when NO_COLOR is set            | High (de-facto standard) |

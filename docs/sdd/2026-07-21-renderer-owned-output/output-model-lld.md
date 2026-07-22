# LLD: output model

Start date: 2026-07-21. Pins the design for [`hla.md`](./hla.md) Phase 0. Terms and requirements
(R1-R15) are defined in [`frd.md`](./frd.md).

This document pins the concrete shapes the implementation follows: the ambient state object, the
`section()` context manager, the role vocabulary, the handler protocol, per-role rendering, the
color policy, and the `detail`/`indent=` migration. Operator decisions folded in: `result()` now (no
`error()` primitive unless needed); `phase()` deprecated (not deleted); header rules `===` / `---` /
plain by depth; `detail` rendered dim + extra indent.

## 1. Ambient state (`agentworks/output.py`)

Only the **section level** is per-flow; the handler stays a module global (as today).

```python
from contextvars import ContextVar

_level: ContextVar[int] = ContextVar("_output_level", default=0)
_handler: OutputHandler = _DefaultHandler()   # module global, unchanged from today

def _current_level() -> int:
    return _level.get()
```

- `get_handler()` / `set_handler()` are **unchanged**: they read/replace the module global
  `_handler`. The conftest fixture's save/restore and `_entry.py`'s `set_handler(TyperHandler())`
  keep working exactly as today, and, crucially, output emitted from an existing worker thread still
  sees the installed handler.
- **Why level-only, not a per-flow handler (YAGNI).** A per-flow handler enables nothing today: one
  handler is installed once at startup, so isolating it per flow only matters once a concurrent
  renderer sets a distinct sink per flow, which is a separate future effort. Moving the handler into
  the `ContextVar` now would also mean initializing output correctly in each spawned thread's
  context (a bare new thread reads the `ContextVar` default and would otherwise lose the installed
  handler). The two spawn sites that emit output today are `batch_check_all_sessions` (a
  `ThreadPoolExecutor`, whose `batch_check_status` calls `output.warn`) and `vms/backup.py` (a
  `threading.Thread` reaching `run_detached` -> `output.warn`); a small `output`-owned context-init
  helper (copy the parent context, set the worker's level) is the right way to handle them, but it
  belongs with the renderer that needs it, alongside that effort's buffering/interleaving decisions,
  not now. So the handler stays a module global; a bare worker thread reads `_level`'s default `0`
  and renders at column 0 with the correct global handler, which is the behavior we want anyway.
- **Per-flow level (R6).** Section depth is isolated per `asyncio` task / `copy_context()` worker
  via the `ContextVar`. Per-flow _handler_ isolation is a clean, localized addition when its
  consumer (the concurrent renderer) exists: move `_handler` into the ContextVar-backed state and
  add the context-init helper at the spawn sites.

## 2. `section()` (`agentworks/output.py`)

```python
from contextlib import contextmanager
from collections.abc import Iterator

@contextmanager
def section(title: str | None = None) -> Iterator[None]:
    level = _current_level()
    if title is not None:
        _handler.emit(Role.HEADER, title, level)
    token = _level.set(level + 1)
    try:
        yield
    finally:
        _level.reset(token)
```

- The header renders at the section's **current** level; the body renders one deeper. Nesting is
  automatic: an inner `section()` reads the already-incremented level.
- **Reset-safe (R4).** `_level.reset(token)` in `finally` restores the exact prior level on normal
  exit and on exception, so a raise mid-section cannot strand the level.
- **Headerless form.** `section(None)` (or `section()`) pushes a level with no header, for a
  sub-block that needs indentation without a `=== ===` line. This is the sanctioned replacement for
  a manual indent (R3a).

## 3. Role vocabulary (`agentworks/output.py`)

```python
from enum import Enum, auto

class Role(Enum):
    BODY = auto()       # info(): a normal body line / step
    DETAIL = auto()     # detail(): de-emphasized / secondary body (dim + extra indent)
    WARNING = auto()    # warn(): non-fatal warning, stderr
    ERROR = auto()      # reserved: error rendering (wired at the entry catch in Phase 5)
    HEADER = auto()     # section() header
    RESULT = auto()     # result(): terminal outcome line, always level 0
    STATUS = auto()     # reserved: list/describe status values (deferred fast-follow)
```

- `ERROR` and `STATUS` are reserved now so the vocabulary is complete, but neither gets a public
  free function in this effort: `ERROR` is emitted by the entry-point catch when color lands (Phase
  5), and `STATUS` is the deferred status-column fast-follow. A public `output.error()` is added
  later only if a caller needs to emit an error line outside the exception path.

## 4. Handler protocol (`agentworks/output.py`)

One `emit` covers every one-shot line (role carries the intent); the interactive and progress
methods keep distinct signatures and all gain `level`.

```python
class OutputHandler(Protocol):
    def emit(self, role: Role, message: str, level: int) -> None: ...
    def confirm(self, message: str, level: int, default: bool = False) -> bool: ...
    def choose(self, message: str, options: list[str], level: int) -> int: ...
    def pause(self, message: str, level: int) -> None: ...
    def prompt(self, label: str, level: int, default: str | None = None) -> str: ...
    def prompt_secret(self, label: str, level: int, hint: str | None = None) -> str: ...
    def progress(self, label: str, level: int, total: int | None = None) -> Progress: ...
```

Free-function to handler mapping (each free function reads `_current_level()`):

| Free function                                        | Handler call                                             |
| ---------------------------------------------------- | -------------------------------------------------------- |
| `info(msg)`                                          | `emit(Role.BODY, msg, level)`                            |
| `detail(msg, indent=1)`                              | `emit(Role.DETAIL, msg, level + indent - 1)` (see sec 7) |
| `warn(msg)`                                          | `emit(Role.WARNING, msg, level)`                         |
| `result(msg)`                                        | `emit(Role.RESULT, msg, 0)`                              |
| `section(title)` (header)                            | `emit(Role.HEADER, title, level)`                        |
| `phase(title)` (deprecated)                          | `emit(Role.HEADER, title, level)` (no push, see sec 8)   |
| `confirm/choose/pause/prompt/prompt_secret/progress` | same-named method, `level` threaded                      |

Indentation, decoration, dimming, and color are the handler's job (R1); no free function pre-renders
any of them.

## 5. Rendering (the three handlers)

Indent unit is **2 spaces per level**. Define `pad(n) = "  " * n`. Per role:

| Role    | Indent         | Decoration                                  | Stream | TTY style     |
| ------- | -------------- | ------------------------------------------- | ------ | ------------- |
| BODY    | `pad(level)`   | none                                        | stdout | default       |
| DETAIL  | `pad(level+1)` | none                                        | stdout | dim           |
| WARNING | `pad(level)`   | `Warning:` prefix                           | stderr | prefix yellow |
| ERROR   | `pad(level)`   | `Error:` prefix                             | stderr | prefix red    |
| HEADER  | `pad(level)`   | by level (below); blank line before lvl 0/1 | stdout | bold          |
| RESULT  | `pad(0)`       | none                                        | stdout | dim green     |

HEADER decoration by level (the operator's rule):

- level 0: `=== {title} ===`
- level 1: `--- {title} ---`
- level 2+: `{title}` (no rule characters)

Notes:

- **DETAIL is dim + one extra indent** (operator decision 4): it renders at `pad(level + 1)`, one
  notch deeper than a sibling `BODY` line, and dimmed on a TTY. The extra indent means `detail`
  stays distinguishable from `info` even with color off (pipes, `NO_COLOR`), which is a deliberate
  robustness property.
- The `=== ===` and `--- ---` rules are **literal text**, not ANSI, so they appear in pipes too
  (matching today's `phase()`); **bold** is the only TTY-only part of a header. Level 2+ headers
  carry no rule, so nesting past two levels reads by indentation plus bold alone.
- `_DefaultHandler` and `_TestHandler` apply the same indentation and decoration (rules, prefixes)
  but no ANSI (`_DefaultHandler`) or none at all (`_TestHandler`, which records structurally, sec
  6).
- Blank line before a header: emitted for level 0 and level 1 only (a top-level or first-nested
  section gets visual separation; deeper headers are kept tight). This preserves the current
  `phase()` "blank line then header" feel at the top level.
- **"Siblings align" is scoped to same-role siblings.** Because DETAIL sits one notch deeper than
  BODY by design, a `detail` line and an `info` line in the same section intentionally do _not_ line
  up. The FRD "siblings align" criterion means same-role siblings; the R10 realizer reconciliation
  ("both render identically") is achieved by giving both the "Creating X" lines the **same** role
  (promote both to `info`), not by relying on default alignment.

### 5a. Interactive and progress rendering (R8, R3)

The prompt and progress paths also render at the ambient level; this is not optional polish, R8
requires prompts (label and hint) inside their section, and R3 covers progress lines.

| Method / line                          | Indent                             | Stream            |
| -------------------------------------- | ---------------------------------- | ----------------- |
| `prompt` / `confirm` / `pause` label   | `pad(level)` prefix on the label   | stdin/stdout      |
| `choose` message                       | `pad(level)`                       | stdout            |
| `choose` options (`{i}) {opt}`)        | `pad(level + 1)`                   | stdout            |
| `choose` `Choice:` / `Invalid` prompts | `pad(level)`                       | stdout            |
| `prompt_secret` label                  | `pad(level)`                       | stderr (as today) |
| `prompt_secret` hint                   | `pad(level + 1)` (was hard `"  "`) | stderr            |
| `Progress.update` / `.done`            | `pad(level + 1)` (was hard `"  "`) | stdout            |

The `prompt_secret` hint and `Progress` lines use `pad(level + 1)`, not `pad(level)`: both were
hardcoded to a 2-space indent, so `pad(level)` would collapse them to column 0 at level 0 and break
the byte-identical invariant. `pad(level + 1)` preserves the 2-space indent (they read as sub-lines,
like DETAIL). The `choose` `Choice:` input and `Invalid` retry lines render at `pad(level)` (0
spaces at level 0, byte-identical) so the whole `choose` block sits inside its section (R8).

- The label indent is applied by prefixing the prompt string handed to `typer.prompt` /
  `click.prompt` / `input`; no ANSI is involved, so it is safe on the prompt path.
- `progress(label, level, total)` captures `level` at creation; the returned `Progress` handle
  stores it and prefixes its `update`/`done` lines with `pad(level)` (the current handles hardcode a
  leading `"  "`, which is replaced by the captured level).

## 6. Test capture (`tests/conftest.py`)

```python
@dataclass
class CapturedOutput:
    lines: list[tuple[Role, int, str]] = field(default_factory=list)  # new: structural
    info: list[str] = ...          # kept: BODY messages
    detail: list[str] = ...        # kept: DETAIL messages
    warnings: list[str] = ...      # kept: WARNING messages
    # confirm/choose/prompt/secret response fields unchanged
```

`_TestHandler.emit(role, message, level)` appends `(role, level, message)` to `lines` and mirrors
the message into the legacy list for `BODY`/`DETAIL`/`WARNING` (and `RESULT` into `info`, so
existing "final line" substring assertions keep passing). Existing tests that read `.info` /
`.detail` / `.warnings` are unaffected (R11/R15); new tests assert on `.lines` for role + level.

The `captured_output` fixture additionally resets `_level` to `0` on teardown (defense in depth): a
test cannot leak a section level into the next, even though `section()`'s reset-token discipline
already makes a leak unreachable through normal use.

## 7. `detail` / `indent=` migration

- **Role kept, parameter removed** (R3a). `detail` stays as the `DETAIL` role; only `indent=` goes.
- **Byte-identical during transition.** With the mapping
  `emit(Role.DETAIL, msg, level + indent - 1)` and DETAIL rendering at `pad(rendered_level + 1)`, a
  legacy call reproduces today's spacing exactly: at `level == 0`, `detail(x)` (`indent=1`) ->
  `pad(0 + 0 + 1) = 2` spaces (today: 2); `detail(x, indent=2)` -> `pad(0 + 1 + 1) = 4` spaces
  (today: 4). So Phase 1 is byte-identical with no section open and color off.
- **Phase 1** keeps `indent` as a deprecated parameter implementing that formula (tree stays green).
  **Phase 4** rewrites the handful of explicit `indent=N>1` sites to nested `section()` blocks,
  carries plain `detail("x")` through unchanged, promotes any clearly-primary line to `info`, then
  deletes the `indent` parameter. The `DETAIL` role and `detail()` itself are permanent.

## 8. `phase()` disposition (deprecated)

`phase(title)` is kept as a deprecated thin wrapper that emits a `HEADER` at the current level with
no level push (it cannot scope a body, which is exactly why `section()` supersedes it). Its
docstring marks it deprecated in favor of `section()`; removal is left to a future effort. All 18
in-repo sites are converted to `with section(...)` during Phases 3-4, so nothing in-repo depends on
it after the sweep.

## 9. Color policy (`cli/_typer_output.py`)

- `click.style` is used (no Rich dependency added); the palette is fixed (no config).
- Gate:
  `_color_enabled(stream) == (os.environ.get("NO_COLOR") is None) and stream.isatty() and not output.non_interactive()`.
  Checked against the target stream (stdout for most roles, stderr for `WARNING`/`ERROR`). When
  false, `click.style` is bypassed and output is byte-plain (R14). `NO_COLOR` is honored by
  presence, any value (the standard).
- **A public accessor, not the private global.** `TyperHandler` must not reach
  `output._non_interactive` across the module boundary. Add `output.non_interactive() -> bool` (the
  trivial getter mirroring the existing `deprecations_suppressed()`) and gate on that.
  `stream.isatty()` (target stream) is deliberately used instead of `is_interactive()` (which
  inspects `sys.stdin`), because color depends on the _output_ stream being a terminal.
- Palette (Phase 5): `Warning:` yellow, `Error:` red, header bold, `RESULT` dim green, `DETAIL` dim.
  `BODY` default. These are `click.style(..., fg=..., bold=..., dim=...)` calls applied only when
  `_color_enabled` is true.

### 9a. ERROR role and `_entry.py` error rendering (Phase 5)

R13 wants a red `Error:` on a TTY, but `_entry.py` renders errors through bespoke
`typer.echo(..., err=True)` calls in several shapes, so the wiring is non-mechanical and is pinned
here:

- **The ERROR role colors its message red on stderr at level 0 and does _not_ auto-add a prefix**
  (unlike `WARNING`, which centralizes its `Warning:` over ~207 uniform callers). `_entry.py`
  deliberately composes several distinct labels, so the entry catch keeps composing the label as
  plain text and routes the composed one-liner through `emit(Role.ERROR, line, 0)`; the handler
  applies red (no ANSI in the composed string). This covers `Error: {e}`,
  `Error: {type(e).__name__}: {e}`, and `Configuration error: {e}` without a one-size prefix
  fighting the varied labels.
- **Ancillary and abort lines stay plain.** `Aborted.`, `Cancelled.`, and the
  `(full traceback written to ...)` / `(could not write ...)` notes are not errors and are not in
  R13's colorization set; they remain plain `typer.echo(..., err=True)`. This bounds the Phase 5
  change to the error one-liner.
- No public `output.error()` free function is added (operator decision 1); the entry catch is the
  only `ERROR` emitter for now.

## 10. Confirm mouse-mode reset (`cli/_typer_output.py`, Phase 2)

- Candidate DECRST reset, written to the prompt's terminal stream **before** reading confirm input:
  `\x1b[?1000;1002;1006;1015l` (disables VT200 / button-event / SGR / urxvt mouse reporting). The
  minimal subset is confirmed against a live reproduction in Phase 2; `1006` (SGR, the `^[[<..M`
  form) is the primary culprit.
- Emitted only when the stream is a TTY (same `isatty` guard as color), so piped/non-interactive
  runs are unaffected. Scoped to the `TyperHandler` confirm path; other handlers and roles are
  untouched.

## Open items deferred to implementation (not decisions)

- Exact minimal mouse-reset subset (pinned after the Phase 2 reproduction).
- Whether `RESULT` mirrors into the test handler's `info` list or gets its own (leaning `info` for
  back-compat; finalize when updating conftest).

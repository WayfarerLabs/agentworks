# HLA: semantic, renderer-owned CLI output

Start date: 2026-07-21.

Companion to [`frd.md`](./frd.md). Prior art in [`prior-art-research.md`](./prior-art-research.md).

## Overview

The output subsystem keeps its current shape (business logic emits through `agentworks/output.py`
free functions; a handler renders) and is corrected along one axis: **the handler receives semantic
role + section level and owns all presentation** (indentation, decoration, color), instead of
business logic pre-rendering indentation and header decoration into the string. The **section
level** is carried as ambient, per-flow state in a `contextvars.ContextVar`, so it is inherited
across ordinary calls (automatic nesting, no threaded handle) and is per-flow-isolated under
concurrency (the parallel-ready foundation); the **handler** stays a module global (existing worker
threads emit output and must keep seeing it). Color rides the same seam: a role the handler styles,
never text the business logic emits.

## Two decisions this design turns on

### D1 The handler owns presentation; free functions resolve ambient state

Today `phase()` builds `=== title ===` and `detail(indent=)` builds `2 * indent` spaces in the _free
function_, then passes an opaque string to the handler. That is the flaw behind both #211 and #145:
the handler cannot re-decorate what it cannot distinguish, and a web/agent renderer inherits a
terminal's indentation and (would inherit) a terminal's color.

The seam is redrawn: the free function's job is to **resolve ambient state** (read the current
section level from the ContextVar; know the role implied by which function was called) and pass
`(role, message, level)` to the handler. The handler's job is to **render** that for its medium:

- `TyperHandler` (TTY): `level * 2` spaces, `=== title ===` (bold) for headers, `Warning:`/`Error:`
  prefixes (colored), a scannable result line, all gated by color policy.
- `_DefaultHandler` / agent: spaces for level, plain decoration, no color.
- future web handler: nested elements / classes; no spaces, no ANSI.

This keeps handlers decoupled from the ContextVar (the free function does the ambient read) while
giving each renderer full control of look. It supersedes the earlier draft's "compute indent in the
free function" lean, which #145 showed to be the same baked-in-presentation mistake one axis over.

### D2 contextvars carries the ambient, per-flow level

Ambient level (R5: no threaded handle) that is also per-flow isolated (R6: parallel-ready) rules out
both a plain module-global (ambient but not per-flow, corrupted by two concurrent flows) and
threading an output handle through ~700 call sites (per-flow but a huge, silent-mis-indent-prone
diff). `contextvars.ContextVar` is the idiomatic Python mechanism for "ambient state that is also
concurrency-scoped" (structlog, OpenTelemetry, Flask/Werkzeug request context all use it): callees
read it with no parameter, and asyncio copies it per task while thread/worker pools run in a
`copy_context()`. See prior-art.

## Components

### 1. The ambient output context (`agentworks/output.py`)

The section level lives in a `ContextVar`; the handler stays a module global (as today):

```text
_level: ContextVar[int] = ContextVar("_output_level", default=0)
_handler: OutputHandler = _DefaultHandler()    # module global, unchanged from today
```

- `get_handler()` / `set_handler()` are unchanged (read/replace the module global `_handler`), so
  the entrypoint contract (`_entry.py` -> `set_handler(TyperHandler())`) and the conftest fixture's
  save/restore keep working, and output from an existing worker thread still sees the installed
  handler.
- A private `_current_level()` reads `_level.get()`; the free functions pass it to the handler.

**Why level-only** (YAGNI): a per-flow handler enables nothing until a concurrent renderer sets a
distinct sink per flow, which is a separate future effort. Carrying the handler in the `ContextVar`
now would also mean initializing output in each spawned thread's context (the two output-emitting
spawn sites today are `batch_check_all_sessions` via `ThreadPoolExecutor` and `vms/backup.py` via
`threading.Thread`, both reaching `output.warn`; a bare new thread reads the ContextVar default and
would lose the installed handler). That is a clean, localized change, move `_handler` into the
ContextVar-backed state plus an `output`-owned context-init helper at the spawn sites, but it
belongs with the renderer that needs it, not now. Meanwhile handler-global gives the behavior we
want for free: a worker reads `_level`'s default `0` and renders at column 0 with the correct
handler. See the LLD.

### 2. The `section` context manager (`agentworks/output.py`)

```text
@contextmanager
def section(title: str | None = None) -> Iterator[None]:
    # 1. if title: emit it with role=HEADER at the CURRENT level (handler decorates)
    # 2. token = _level.set(_current_level() + 1)
    # 3. try: yield  finally: _level.reset(token)
```

- **Header at current level, body one deeper.** The header is emitted with the header _role_ (the
  handler renders `=== title ===` / bold / `<h_>`); the body renders at `level + 1`. Nesting falls
  out naturally.
- **Reset-safe (R4).** `_level.reset(token)` in a `finally` restores the exact prior level even when
  the body raises.
- **`phase()` is converted, not aliased.** A bare `phase()` call has no block to scope the body's
  indent to, whereas `section()`'s `with` block _is_ that scope. The 18 `phase()` sites are
  converted to `with section(...)` blocks. Whether `phase()` is deleted or kept as a deprecated
  no-scope header emitter is pinned in the LLD.

### 3. The role vocabulary + depth-aware primitives (`agentworks/output.py` + handlers)

The free functions map to a semantic role and pass `(role, message, level)` to the handler. There
are **two body roles**, `info` (normal body) and `detail` (de-emphasized / secondary body); both
render at the ambient `level`, differing only in prominence. `detail`'s `indent=` parameter is
removed (D1a below).

- `info` -> body; `detail` -> de-emphasized body. Both at the ambient `level`; the handler expresses
  the de-emphasis (dimming, optionally a slight extra indent on the CLI).
- `warn` -> warning role (handler adds a colored `Warning:` on stderr). The error role is emitted
  only by the `_entry.py` catch (the entry composes the label, the handler colors it red); no public
  `error()` free function is added (LLD sec 9a).
- section header role (from `section()`); result role (component 4); status role (deferred rollout,
  component 6).
- Prompts (`prompt`, `prompt_secret`, `confirm`, `choose`, `pause`) -> rendered at `level`, label,
  hint, options, and cursor included (R8).
- `progress` -> its lines render at `level`.

#### D1a `detail` keeps its meaning, loses `indent=`

`info` vs `detail` conflated two axes: prominence (a step vs a supporting aside) and horizontal
position (`detail`'s `indent=`). Only the depth axis is the "primitive choice sets depth" flaw. So
`indent=` is removed (depth comes only from `section()`), but `detail` is _kept_ as a distinct
de-emphasized-body role, orthogonal to depth. `info` and `detail` render at the same ambient level
and differ in prominence, which the handler expresses (CLI: dim, optionally a slight extra indent;
web: muted/small; agent: plain or dropped). A sub-level without a header is a headerless `section()`
(`with section():`), not a manual indent.

Migration is mechanical because `detail`'s default is already `indent=1`: `detail("x")` (the common
case) maps straight through to the kept role; only the handful of explicit `detail(x, indent=N>1)`
sites, which meant "deeper," become nested `section()` blocks. Lines that are clearly the primary
step rather than an aside are opportunistically promoted `detail` -> `info` during the sweep.

The `OutputHandler` protocol methods gain the `level` the free function supplies and, where a role
is not already implied by the method name, a role marker. The three handlers implement rendering;
`_TestHandler` records role + level + text so tests assert structure, not substrings (R15).

### 4. The result-line role (`agentworks/output.py`)

A dedicated primitive (name pinned in the LLD, e.g. `result()`) emits the terminal line with a
result role at level 0 unconditionally (R7). The TTY handler renders it flush-left and scannable
(dim-green / bold); other handlers render it plain at column 0.

### 5. The confirm mouse-mode fix (`cli/_typer_output.py`)

The `[y/N]` leak (R11) is a terminal-state bug in the same prompt path. The fix emits a
mouse-tracking-disable DECRST reset (the minimal `\e[?1000l\e[?1006l` family, exact sequence pinned
after reproduction) around the confirm prompt. Scoped to the Typer handler.

### 6. Color policy in the terminal handler (`cli/_typer_output.py`)

Color lives only in `TyperHandler`, keyed off role (R12/R13/R14):

- Uses `click.style` (Rich is available transitively but `click.style` is sufficient and lighter for
  role-level styling); palette is a fixed, tasteful default (yellow `Warning:`, red `Error:`, bold
  header, dim-green result).
- A single `_color_enabled()` gate: false under `NO_COLOR`, non-TTY stdout/stderr, or
  `--non-interactive` (read via `output.is_interactive()` and env). When false, `click.style` is
  bypassed so output is byte-plain.
- **Status role rollout is deferred** (see FRD out of scope). The role exists in the vocabulary; the
  table-cell rendering that colors `OK`/`BROKEN`/... in `list`/`describe` is a fast-follow, because
  the status value is composed into rows by the renderers and needs the role to reach render-time.

### 7. Test capture handler (`tests/conftest.py`)

`_TestHandler` / `CapturedOutput` record role + level alongside text (R15). Existing message-list
assertions keep working; role/level is additive. Tests reading a rendered TTY string use the
`_plain` ANSI-strip helper.

## The full sweep (R10)

Mechanical but wide: the 18 `phase()` sites become `with section(...)` blocks (the five-phase
`create_session` is the reference shape); `restart_session` gains matching sections; the workspace
vs agent realizer "Creating X" disagreement is reconciled (both at the same relative position now
that level is ambient); `secrets/prompt.py` stops printing flush-left; `detail("x")` calls carry
over as the kept de-emphasis role, while every explicit `detail(x, indent=N>1)` becomes a nested
`section()`, and the `indent=` parameter is removed. Because the free-function surface stays
backward compatible, an untouched file still renders correctly (column 0, no color) between phases;
the sweep makes structure intentional.

## Data / control flow (worked example: `session create`)

```text
create_session():
  with section("Preflight"):            # header role at level 0; body at level 1
      info("Checking session-template/...")        # body, level 1
  with section("Resolving Secrets"):
      info("Resolved openai-api-key ...")          # body, level 1
      prompt_secret("git-token-...")               # prompt at level 1 (was flush-left)
  with section("Creating Workspace"):
      # workspace realizer, called here, emits info("Creating workspace ...")
      #   -> body role at level 1 automatically (ambient), no handle passed
      detail("VS Code workspace: /path/to/ws")     # de-emphasized body, level 1 (dimmed on TTY)
  with section("Creating Agent"):
      # agent realizer emits its "Creating agent ..." line -> body, level 1, now agreeing
  with section("Starting Session"):
      warn("Socket directory ... recreating")      # warning role, level 1 (yellow on TTY)
  result(f"Session '{name}' started (...)")         # result role, level 0 (dim-green, dedented)
```

## Interfaces changed

- **`agentworks/output.py`**: adds `_level: ContextVar`, `Role`, `section()`, `result()`, a public
  `non_interactive()` accessor, and the `emit(role, message, level)` free-function seam; removes the
  `detail` `indent=` parameter (keeping `detail` as the de-emphasis role);
  `get_handler`/`set_handler` and the module-global `_handler` are unchanged; free functions resolve
  level + role and pass them down. `phase()` deprecated per the LLD.
- **`OutputHandler` protocol + 3 handlers**: collapses one-shot output into
  `emit(role, message, level)`, interactive/progress methods gain `level`; handlers own indentation,
  decoration, and (TyperHandler only) color; `_TestHandler` records role/level.
- **`cli/_typer_output.py`**: color policy + palette; confirm mouse-mode reset.
- **53 caller files**: mechanical conversion to `section()` and the result role.

## Risks and mitigations

- **Wide diff, silent mis-indent.** Mitigated by a backward-compatible surface (untouched files
  render correctly), always-green file-group commits, and role/level-capturing tests.
- **Color leaking into non-TTY / tests.** Mitigated by the single `_color_enabled()` gate
  (`NO_COLOR` + TTY + non-interactive) and the `_plain` ANSI-strip test helper; the default and test
  handlers never emit color.
- **contextvars in a sync CLI.** Works fine synchronously (a well-scoped global with token reset);
  the parallel benefit is latent until a concurrent effort adopts it. No async introduced here.
- **Mouse-mode fix portability.** Reset sequence pinned only after reproducing the leak; scoped to
  the interactive Typer path.

## LLDs to produce (in the plan)

- **output-model-lld.md**: the `_level` ContextVar (level-only) + global handler; `section()`
  semantics + header role; the `(role, message, level)` handler protocol change and the role
  vocabulary (body, de-emphasized body, warning, error, header, result, status);
  `result()`/`error()` naming and behavior; `phase()` disposition; the `detail` `indent=` removal
  (keeping the role) and how the CLI renders de-emphasis (dim vs dim+slight-indent); color palette +
  `_color_enabled()` policy; the confirm DECRST sequence; the `_TestHandler` role/level capture
  shape.

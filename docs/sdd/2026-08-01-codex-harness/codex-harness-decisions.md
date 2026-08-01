# codex harness: pinned decisions and CLI research

**Status:** Pinned **Repo:** `agentworks` **Path:** `cli/agentworks/plugins/codex/`

This effort adds a `codex` session harness in a new opt-in `codex` system plugin, mirroring the
`claude` plugin end to end. It deliberately runs without a full SDD: the pattern it follows is
locked (`docs/sdd/2026-07-07-session-harness/`, ADR 0020, the plugin-system SDD), and the harness
developer guide (`cli/agentworks/capabilities/harness/README.md`) is the contract. This document is
the codex analog of `claude-code-lld.md`: the empirical CLI research and the pinned tool-specific
mechanics, so implementation never relies on recalled flags.

## Verification basis

- **CLI verified against `codex-cli 0.146.0`** (latest stable per npm at research time; the alpha
  channel was deliberately not used), installed via `npm install -g @openai/codex` on Linux arm64
  (first-party `linux-arm64` binary; clean install). Session-lifecycle behavior was exercised end to
  end against a local offline stub provider implementing the Responses wire API, with a throwaway
  `CODEX_HOME`; no account was authenticated.
- **Re-verify at implementation time.** Codex ships fast. Re-run `codex --version`, `codex --help`,
  and `codex resume --help` and reconfirm the pinned flags before landing.

## What the CLI actually provides (verified against 0.146.0)

- **The caller cannot pin a session id.** No `--session-id` analog exists: no flag, no `-c` config
  key (`--strict-config` rejects every candidate spelling), and no field in the app-server
  `ThreadStartParams` schema. Ids are codex-minted UUIDv7, and the session becomes durable only at
  the first prompt submission.
- **On-disk state:** `$CODEX_HOME` (default `~/.codex`; the env var is honored). Sessions persist as
  `sessions/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<session-uuid>.jsonl`; the filename embeds the
  session id verbatim. `codex archive` moves the file to `archived_sessions/`. A `state_5.sqlite`
  `threads` table indexes sessions but is a cache, not the source of truth.
- **The resume boundary equals rollout-file presence** (verified with an abandoned-at-every-stage
  experiment, the analog of the claude one): file present resumes even with the index row deleted;
  file absent fails even with the row present; archived fails with an actionable "run
  `codex unarchive`" error. A failed turn (e.g. a 401) still creates a resumable rollout.
- **Resume is a subcommand**: `codex resume <uuid>` (global, not cwd-scoped for an explicit id). An
  unknown uuid errors cleanly, rc 1, both interactively and via `codex exec resume`. Two traps:
  `codex exec resume <unknown-NAME>` silently starts a NEW session (rc 0), and `resume --last` is
  cwd-scoped, silently filtered by the current `model_provider`, and hijacked by any manual codex
  run in the same directory. The interactive surface hard-refuses without a TTY ("stdin is not a
  terminal").
- **Cross-cwd resume shows a blocking picker** unless suppressed with
  `-c tui.resume_cwd=current|session`. Resume also rewrites the thread's recorded cwd to the
  resume-time cwd.
- **First launch in a new directory shows a blocking workspace-trust prompt.** It is suppressed only
  by a `[projects."<abs path>"] trust_level = "trusted"` entry in `$CODEX_HOME/config.toml` (a
  per-launch `-c` override of the same key does NOT suppress it). Codex also writes to that config
  file itself (trust entries, TUI counters).
- **Unauthenticated interactive launch parks on a login menu forever** and creates no session file.
  Headless login paths: `codex login --with-api-key` (stdin), `--with-access-token`,
  `--device-auth`; or `OPENAI_API_KEY` in the environment. Credentials live in
  `$CODEX_HOME/auth.json`.
- **Pane mechanics transfer:** `sh -c 'echo ...; exec codex ...'` works under the tmux wrapper, and
  codex does not use the alternate screen, so the echoed decision line stays visible above the TUI.
- **No launch-time display name.** Names exist post-hoc (`/rename`, `session_index.jsonl`) and ARE
  addressable by `resume`, but binding one requires already knowing the id, and the
  unknown-name-silently-creates trap makes names unsafe on the resume path.
- Codex takes no lock on a rollout: concurrent resume of one session in two panes is allowed
  silently.

## Pinned decisions

### Addressing: discover-and-store (the guide's rule 1, second form)

The harness stores the codex-minted session id in its (namespaced) state blob under `session_id`,
populated by DISCOVERY, never generated. Resume is by UUID only: never by name, never `--last`
(operator-decided 2026-08-01, matching the research recommendation: `--last` is too implicit for a
harness that must never silently orphan a conversation).

### Fresh launch: deferred discovery, no seed prompt (operator-decided 2026-08-01)

A fresh launch runs plain `codex` with no positional prompt, so no wrapper-authored turn ever
appears in the conversation. `session_id` stays unset until a later op discovers it; until the
human's first submission there is nothing durable to resume, so a restart in that window launching
fresh again loses nothing (the same boundary claude has).

Discovery mechanics (op-time, target-side): the fresh-launch pane command touches a per-session
marker file (under the session user's home, e.g. `~/.agentworks/codex/<session-name>.launch`) before
`exec codex`, giving a same-clock "our launch happened here" anchor. On a later op with no stored
id, the harness probes for rollout files newer than the marker whose `session_meta` cwd is the
session's working directory:

- exactly one candidate: adopt its uuid into `session_id` and resume it;
- zero candidates: launch fresh (and refresh the marker);
- multiple candidates (two codex sessions launched fresh in one workspace dir concurrently): raise a
  typed `StateError` naming the candidate ids, refusing to guess; adopting the wrong id would
  silently splice one session's conversation into another.

### Resume-vs-launch probe

Same shape and exit-code fork as claude-code (`0` resume, `1` fresh, anything else raises rather
than guessing), with the codex glob:

```sh
find "${CODEX_HOME:-$HOME/.codex}/sessions" -name '*-<sid>.jsonl' -print -quit 2>/dev/null | grep -q .
```

The filename is `rollout-<ts>-<uuid>.jsonl`, hence `*-<sid>.jsonl`, not `<sid>.jsonl`.
`archived_sessions/` is deliberately NOT probed: an archived session reports not-resumable and the
harness launches fresh (operator-decided 2026-08-01; auto-unarchive would silently reverse an
explicit operator action). The stored id is replaced by the next discovery, and the archived history
stays recoverable manually.

### Invocation forms

```text
fresh:  sh -c 'echo <msg>; <touch marker>; exec codex [flags] [extra_args...]'
resume: sh -c 'echo <msg>; exec codex resume <sid> -c tui.resume_cwd=current [flags] [extra_args...]'
```

`-c tui.resume_cwd=current` pins the cross-cwd picker off deterministically (the pane has already
`cd`-ed to the workspace dir, so "current" is always the right answer). All generated tokens are
`shlex.quote`d; no generated piece emits `{{word}}`.

### Config vocabulary (v1, all optional, all forwarded unvalidated)

| `harness_config` field  | CLI flag emitted        | Notes                                        |
| ----------------------- | ----------------------- | -------------------------------------------- |
| `model` (str)           | `-m <value>`            | codex-owned values                           |
| `sandbox` (str)         | `-s <value>`            | `read-only` / `workspace-write` / ... drift  |
| `approval_policy` (str) | `-a <value>`            | `untrusted` / `on-request` / `never` drift   |
| `profile` (str)         | `-p <value>`            | note: an unknown profile is silently ignored |
| `extra_args` (list)     | (the tokens themselves) | appended verbatim, last                      |

Choice sets are codex-owned and drift between releases; invalid values surface as codex's own
startup error in the pane (the same rule as claude-code). `extra_args` is the escape hatch.

### Readiness, provisioning, auth

Readiness is `require_commands` for the single binary `codex`; session state stays an op-time
concern. Auth and workspace trust are provisioning concerns and OUT of harness scope for v1 (parity
with claude v1): an unauthenticated pane parks on codex's own login menu, an untrusted directory
parks on its trust prompt, and both are codex's own visible, actionable surfaces. The bundled
`codex` user-install-command is the plugin's declarable (verify the current official install method
at implementation time; do not recall it).

### State stored per session (in the namespaced blob)

`session_id` (discovered codex UUIDv7, absent until discovery) and whatever anchor the discovery
mechanism needs (the marker-file path is derivable, so likely nothing else). The launch cwd is the
session's workspace directory, already known to the harness.

### Out of v1 (recorded)

Thread-name binding for human legibility (safe only post-discovery), auto-unarchive, concurrent
attach protection (codex takes no lock; a future liveness surface is the right home), auth
provisioning (`codex login --with-api-key` fits a future `harness-user-provisioner`), and
`codex exec` headless ops.

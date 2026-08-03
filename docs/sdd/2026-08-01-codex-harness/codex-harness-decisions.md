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
- **The rollout's first JSONL line is its `session_meta`**, serialized as compact JSON (no spaces),
  and its `"cwd"` key records the PHYSICAL working directory: launching from a symlinked directory
  records the canonical path (verified 2026-08-01 against 0.146.0). This is what makes the discovery
  filter's `grep -F '"cwd":"<pwd -P of the workspace>"'` comparison sound.
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

Discovery mechanics (op-time, target-side; redesigned at review 2026-08-01, lead-pinned, replacing
the original name-derived-marker scheme whose count-only adoption could adopt a foreign session and
whose leftover markers could leak a dead namesake's conversation into a recreated session):

- **Stored anchor, nonce marker.** A fresh launch mints a nonce marker filename
  (`~/.agentworks/codex/<session-name>-<nonce>.launch`), stores it in the state blob under
  `discovery_marker`, and the pane command touches that file before `exec codex` (removing the
  previous anchor's file first if the blob held one from an unused earlier fresh launch). The
  adoption resume command removes the consumed marker file.
- **Discovery runs ONLY on a stored anchor.** With `discovery_marker` in the blob and no
  `session_id`, the op probes; a blob holding neither has definitively nothing to discover, so no
  target round-trip happens at all. A brand-new session, or a namesake recreated after a delete, can
  therefore never adopt another session's conversation off a leftover marker file.
- **Marker file missing while its anchor is stored: raise.** Either the fresh pane never ran (the
  tolerated-touch-failure window) or someone removed the file; both break the anchor's account of
  history, so the op raises a typed `StateError` whose hint names the recovery (recreate the marker
  with `touch` if the session has no codex conversation worth keeping; the next launch starts
  fresh).
- **Candidates** are rollout files newer (mtime) than the marker whose `session_meta` cwd (the
  rollout's first JSONL line) equals the session's workspace directory, both sides canonicalized
  target-side (`cd <workspace> && pwd -P`) so logical-vs-physical symlink mismatches cannot exclude
  our own rollout. The workspace path reaches the harness through a `workspace_path` keyword on the
  base harness constructor, threaded from the session node seam. Then:
  - exactly one candidate: adopt its uuid into `session_id`, clear `discovery_marker`, resume it;
  - zero candidates: launch fresh (minting a replacement anchor); a foreign rollout in a DIFFERENT
    directory is benign and must not brick a restart;
  - multiple candidates: raise a typed `StateError` naming the candidate ids, refusing to guess;
    adopting the wrong id would silently splice one session's conversation into another.

#### Known residual windows (v1)

Recorded honestly rather than claimed away; both are narrow, and the failure they leave is either
loud or requires a same-user same-directory race:

- **(a) Same-user, same-cwd foreign fresh launch.** A foreign codex session launched by the same
  launch user in the SAME workspace directory between our marker touch and our discovery yields a
  candidate the cwd filter cannot distinguish from ours: a single such candidate would be adopted
  wrongly (two of them raise). Interim operator guidance: avoid two concurrently-fresh codex
  sessions sharing one agent user and workspace directory; a future launch-to-rollout binding
  mechanism closes this properly.
- **(b) mtime is not creation time.** `find -newer` compares mtime, and codex rollout mtimes advance
  on every turn, so an OLD conversation resumed in the same cwd after our marker touch enters the
  candidate set even though it was created earlier. It surfaces as the loud multiple-candidates
  error, or contributes a wrong single candidate under (a)'s conditions.
- **(c) Workspace rehome between a fresh launch and its discovery.** `workspace rehome` moves the
  workspace path, so a rollout recorded at the old path is excluded by the cwd filter and the next
  op launches fresh, orphaning the undiscovered conversation (recoverable manually via
  `codex resume`). Requires a rehome inside the create-to-first-restart window of a never-restarted
  session, so it is narrower than (a) and (b).

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

### Config vocabulary (all optional; shape-checked here, codex-owned values forwarded unvalidated)

Extended 2026-08-03 (operator-decided) with the network/writable-dirs/web-search knobs and the
strict-config default, after the first real usage showed the `-c` network spelling too arcane and
its drift mode too silent.

| `harness_config` field         | CLI surface emitted                          | Notes                                        |
| ------------------------------ | -------------------------------------------- | -------------------------------------------- |
| `model` (str)                  | `-m <value>`                                 | codex-owned values                           |
| `sandbox` (str)                | `-s <value>`                                 | `read-only` / `workspace-write` / ... drift  |
| `approval_policy` (str)        | `-a <value>`                                 | `untrusted` / `on-request` / `never` drift   |
| `profile` (str)                | `-p <value>`                                 | note: an unknown profile is silently ignored |
| `network` (bool)               | `-c sandbox_workspace_write.network_access=` | both directions forward; key is codex-owned  |
| `writable_dirs` (list)         | one `--add-dir <dir>` each                   | union-merged across inheritance              |
| `web_search` (bool)            | `--search` when true                         | the server-side tool, not sandbox network    |
| `disable_strict_config` (bool) | omits the default `--strict-config`          | see below                                    |
| `extra_args` (list)            | (the tokens themselves)                      | appended verbatim, last                      |

Choice sets are codex-owned and drift between releases; invalid values surface as codex's own
startup error in the pane (the same rule as claude-code); when the rejection kills the pane
instantly, `session create` / `session restart` capture that output into their own error, so the
message still reaches the operator. `extra_args` is the escape hatch.

**`--strict-config` is emitted by default (operator-decided 2026-08-03).** Verified against 0.146.0:
an unknown `-c` config key is SILENTLY ignored by a non-strict codex, so if codex ever renames the
network key, a non-strict session would silently lose network instead of failing. Agentworks owns
the emitted config surface, so strictness is the right default; it also hardens the target user's
own `config.toml`, which is deliberate and documented. `disable_strict_config: true` is the
sanctioned off-switch, for either regression vector: a config codex must tolerate (e.g. written by a
newer codex than the target runs), or a target codex old enough to lack the flag entirely (verified
present in 0.146.0 on both `codex` and `codex resume`; an older binary rejects it as an unknown
argument at launch, and harness readiness probes only that `codex` exists, not its version).

### Readiness, provisioning, auth

Readiness is `require_commands` for the single binary `codex`; session state stays an op-time
concern. Auth and workspace trust are provisioning concerns and OUT of harness scope for v1 (parity
with claude v1): an unauthenticated pane parks on codex's own login menu, an untrusted directory
parks on its trust prompt, and both are codex's own visible, actionable surfaces. The bundled
`codex` user-install-command is the plugin's declarable (verify the current official install method
at implementation time; do not recall it).

### State stored per session (in the namespaced blob)

`session_id` (discovered codex UUIDv7, absent until discovery) and `discovery_marker` (the
`$HOME`-relative nonce marker path the last fresh launch minted, absent once consumed by adoption).
The original "the marker-file path is derivable, so likely nothing else" call is superseded by the
stored-anchor redesign above: deriving the path from the session name is exactly what made the
stale-namesake adoption possible. The launch cwd is the session's workspace directory, threaded to
the harness at construction (`workspace_path`).

### Out of v1 (recorded)

Thread-name binding for human legibility (safe only post-discovery), auto-unarchive, concurrent
attach protection (codex takes no lock; a future liveness surface is the right home), auth
provisioning (`codex login --with-api-key` fits a future `harness-user-provisioner`), and
`codex exec` headless ops.

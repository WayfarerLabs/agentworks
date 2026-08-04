# codex harness: pinned decisions and CLI research

**Status:** Pinned **Repo:** `agentworks` **Path:** `cli/agentworks/plugins/codex/`

This effort adds a `codex` session harness in a new opt-in `codex` system plugin, mirroring the
`claude` plugin end to end. It deliberately runs without a full SDD: the pattern it follows is
locked (`docs/sdd/2026-07-07-session-harness/`, ADR 0020, the plugin-system SDD), and the harness
developer guide (`cli/agentworks/capabilities/harness_integration/README.md`) is the contract. This
document is the codex analog of `claude-code-lld.md`: the empirical CLI research and the pinned
tool-specific mechanics, so implementation never relies on recalled flags.

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
  records the canonical path (verified 2026-08-01 against 0.146.0). This is the fact that makes the
  discovery filter's literal `"cwd":"<pwd -P of the workspace>"` comparison sound. (The comparison
  itself is an `index()` test inside the batched `awk` the probe runs, not a `grep`; the FACT is
  what this bullet pins.) It holds only while codex serializes `session_meta` COMPACTLY: a
  pretty-printed line would match neither this needle nor the `"source":"cli"` one, which costs the
  fallback and never mis-adopts. Re-verify on codex major bumps.
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

The following were verified 2026-08-04 (offline-stub harness driving real turns unauthenticated,
plus operator-supplied rollout specimens from a production incident), for the resume redesign:

- **The `notify` hook.** `notify = ["<program>"]` is a config key (`-c` accepted, `--strict-config`
  validated). Codex invokes the program with ONE argv argument, a JSON payload
  (`{"type":"agent-turn-complete","thread-id":...,"turn-id":...,"cwd":...,"client":...,...}`), after
  EVERY successfully completed turn, in every launch mode (TUI, `exec`, `resume <id>`, the picker,
  picker-esc-new). `thread-id` equals the rollout filename uuid and the id `codex resume` takes.
  Nothing fires for an abandoned session or a failed turn. A `-c notify` override silently and
  completely REPLACES a config.toml notify; a nonexistent notify program is a completely silent
  no-op. The payload's `cwd` is the resume-time cwd, not the session's original.
- **Extra `notify` list elements are passed as leading argv, before the payload** (verified
  2026-08-04 at implementation time against 0.146.0, with a real completed turn against the offline
  stub provider): `notify=["<prog>","<extra>"]` runs `<prog> <extra> <payload>`, so the payload
  lands in `$2`. `--strict-config` accepts the multi-element form and rejects a bare string
  (`invalid type: string, expected a sequence`). This is what lets ONE shared recorder script serve
  every session: `$1` is the session's destination file. The corollary constraint is that the paths
  inside the array must be ABSOLUTE, since codex spawns the program itself with no shell, so the
  generated `-c notify=[...]` word leaves `$HOME` for the launching shell to expand.
- **`-c notify=[...]` is accepted on both resume forms** (reconfirmed 2026-08-04 at implementation
  time): `codex resume <id>` and bare `codex resume` both parse `-c` overrides under
  `--strict-config` BEFORE the interactive TTY check, so an unknown key errors out identically on
  either.
- **Subagents share everything and fire the parent's notify.** A subagent's rollout lives in the
  same sessions tree with the SAME cwd as its parent, its `session_meta.source` is a JSON OBJECT
  (`{"subagent": {...}}`, including the guardian reviewer's `{"subagent":{"other":"guardian"}}`),
  its `thread_source` is `"subagent"`, its `originator` INHERITS the parent's (`codex-tui`), and,
  critically, its completed turns fire the PARENT's notify hook carrying the SUBAGENT's `thread-id`.
  The only in-payload discriminator: real sessions carry a `client` key (`codex-tui` /
  `codex_exec`); subagent payloads carry none. Also: a subagent rollout's `session_meta.session_id`
  is the PARENT's uuid (its own is the `id` field and the filename), so code must never read
  `session_id` off a rollout.
- **`source` is the picker's own discriminator.** Interactive TUI sessions stamp the STRING
  `"source":"cli"` with `"thread_source":"user"`; `exec` stamps `"exec"`; subagents stamp the object
  above. Codex's own resume picker hides non-`cli` sessions by default (`--include-non-interactive`
  reveals exec ones; subagent sessions never appear at all).
- **The bare `codex resume` picker works under the Agentworks pane wrapper** (`$SHELL -lic` +
  `sh -c` + `exec`), accepts `--strict-config`, `-m`/`-s`/`-a`, and repeated `-c` (including
  `notify`), renders the echoed decision line above itself, and its keys are: enter resumes the
  selection, esc starts a NEW session in the same process (inheriting the command line's `-c` flags,
  so notify still binds it), ctrl+c quits.
- **Codex has a hooks subsystem** (`SessionStart` with `source: startup|resume`, `SubagentStart`,
  `SubagentStop`, ...) that would be a strictly better binding surface than `notify` (fires at
  launch, self-labels subagents), but no experiment could get a hook to fire from config alone in
  0.146.0 (plugin-only installation suspected). Recorded as the follow-up lead, not v1.

## Pinned decisions

### Addressing: notify-bound, with source-filtered discovery and the picker (redesigned 2026-08-04)

The integration stores the codex-minted session id in its (namespaced) state blob under
`session_id`. Resume is by UUID only: never by name, never `--last` (operator-decided 2026-08-01:
too implicit for an integration that must never silently orphan a conversation). Fresh launches
still carry no seed prompt (operator-decided 2026-08-01): no wrapper-authored turn ever appears in
the conversation, so nothing durable exists until the human's first submission and a resume in that
window losing nothing.

This section was redesigned 2026-08-04 after a production incident: the original marker-anchored,
mtime-plus-cwd discovery treated every rollout in the workspace as a candidate, and codex SUBAGENTS
write sibling rollouts with the same cwd, so one session that ran subagents produced 14
indistinguishable candidates, a bricked resume, and error hints whose recovery path orphaned the
conversation. The marker scheme is REMOVED, not patched: identity now comes from codex itself, with
inference demoted to a filtered fallback and ambiguity resolved by a human in-band instead of an
error. Three layers:

- **Layer 1, the notify binding (primary).** Every generated launch (fresh, `resume <id>`, picker)
  provisions a recorder script and passes `-c notify=[...]`. On each completed turn codex hands the
  recorder its `thread-id`; the recorder writes it atomically to
  `~/.agentworks/codex/<session-name>.thread` ONLY when the payload carries a `client` key (a
  subagent turn fires the parent's notify with the CHILD's id and no `client` key; recording it
  would splice conversations, the exact failure this redesign kills). The next RESUME reads the
  file, adopts the id into `session_id`, and resumes deterministically; a `create` deletes it
  instead (see create-is-always-fresh below). Last-write-wins on purpose: a picker-esc fresh session
  rebinds to the conversation actually in the pane. The `client` discriminator is undocumented
  (codex-internal `legacy_notify`); re-verify on codex major bumps. The `thread-id` read is
  field-anchored (the payload is split on JSON field and object boundaries, then a whole field is
  matched) and takes the FIRST match, so a value that quotes the needle cannot forge it (JSON
  escapes its quotes) and a `thread-id` nested in a LATER object cannot displace the payload's own.
  Note the exact guarantee: first-match is BYTE ORDER, not nesting depth, so it is the payload's own
  only because 0.146.0 emits that field before any nested object; a future codex nesting one earlier
  would bind that instead, costing a recoverable wrong binding the picker fixes. That anchor is also
  what depends on codex serializing the payload COMPACTLY, as 0.146.0 does (the `"client":` needle
  survives pretty-printing; the anchored field match does not), so a pretty-printed payload would
  record NOTHING: that silently costs the binding and falls back to layers 2/3 rather than recording
  something wrong.
- **Layer 2, source-filtered discovery (the fallback, reached whenever no id is bound: nothing
  recorded and nothing stored, OR a bound id just dropped as archived-or-gone).** Candidates are
  rollouts whose first JSONL line carries BOTH the literal `"source":"cli"` (a subagent's `source`
  is a JSON object, an exec session's is `"exec"`, so both are structurally excluded, matching what
  codex's own picker shows by default) and the session's canonicalized workspace cwd
  (`cd <workspace> && pwd -P`; a rollout's `session_meta.cwd` is immutable across cross-cwd
  resumes). No marker, no mtime window. Exactly one candidate: adopt and resume. Zero: fresh.
  Candidate identity comes from the FILENAME uuid only, NEVER `session_meta.session_id` (the
  parent's uuid in a subagent rollout); the `id` field would also be correct but reading it would
  mean parsing the line, so it is deliberately unused. A candidate whose filename carries no uuid
  therefore cannot be named, and rather than being ignored (which could turn a genuinely ambiguous
  workspace into one confident adoption) it forces layer 3. So does a candidate whose first line
  will not read.
- **Layer 3, the picker (ambiguity is a human decision, not an error).** Multiple candidates launch
  `codex resume` (bare: codex's own cwd-scoped picker, which already hides exec and subagent
  sessions) with our managed flags and the notify recorder attached. The operator picks their
  conversation (or esc for a fresh one); either way the next completed turn binds the id through
  layer 1 and the session self-heals into deterministic resume. The console note and pane echo
  explain exactly that, including what esc does.

All three layers belong to `session resume`. `session create` runs NONE of them and is
unconditionally fresh: see the create-is-always-fresh decision under Failure modes, which is what
keeps both name-derived channels from handing a brand-new session someone else's conversation.

An over-strict layer-2 filter therefore degrades to the picker, and a wholly failed recorder
degrades to layers 2/3: no INFERENCE failure bricks a resume, and none silently orphans. Probes that
could not RUN still raise, deliberately and per rule 4 (no launch target, a recorder file that
exists and will not read, a workspace dir that will not canonicalize, a failed `find`, any non-{0,1}
probe exit): those are refusals to guess, each with a hint naming a recovery the operator can
actually take, not inference outcomes. Legacy state from the marker era (`discovery_marker` blob
keys, `*.launch` files) is ignored and opportunistically cleaned; a pre-redesign session's bound
`session_id` keeps working unchanged.

#### Failure modes (replacing the marker-era "Known residual windows")

- A recorder that never runs (codex ignores a missing notify program SILENTLY) costs determinism,
  not correctness: every op falls through to layers 2/3.
- An operator who overrides `notify` via `extra_args` disables the binding (extra_args is
  deliberately last); documented, and layers 2/3 still hold.
- The wrong-adoption path left is layer 2 finding exactly one candidate that is not this session's
  conversation, since layer 2's whole filter is the workspace directory. Two ways in, and the
  ORDINARY one is the one that matters: (a) **a session deleted and replaced in the same
  workspace.** Its rollout outlives it (nothing prunes codex state, by design), so if it is the only
  cli-source rollout there, the replacement's FIRST resume adopts it. `start`-is-always-fresh does
  not prevent this; it prevents the recorder-channel half (see below), which would have been silent.
  (b) a FOREIGN interactive codex TUI session the same user launched manually in that directory.
  Either way the session has completed no turn of its own, so nothing of its own is lost, and it
  surfaces in the pane as a visibly wrong conversation rather than silently: the adoption leaf names
  the adopted uuid in both the console note and the pane echo, and the picker plus notify rebinding
  recover it. Behavioral hardening (a creation-time floor on candidate age) is a recorded follow-up,
  deliberately not attempted here: it needs new plumbing and is clock-skew sensitive between
  controller and target, and a floor set wrong would exclude the session's OWN rollout and orphan
  it, which is worse than the disease.
- **The recreated namesake and the foreign-workspace conversation, closed by
  create-is-always-fresh** (found at implementation time 2026-08-04, resolved operator-decided the
  same day). Both of this integration's identity channels are name-derived ON THE TARGET: the
  recorder file is `~/.agentworks/codex/<session-name>.thread`, and layer-2 discovery matches the
  workspace directory. Nothing cleans either up when a session is deleted (a harness integration has
  no delete hook), so at CREATE time each would hand a brand-new session someone else's
  conversation: the recorder half gives it a deleted namesake's binding, and the discovery half
  gives it any `cli`-source rollout already in the workspace (a previous session's, or an operator's
  manual codex run). The marker scheme prevented both accidentally, through its "discovery runs ONLY
  on a stored anchor" rule, and the redesign initially dropped that protection.
- **Therefore, pinned: `start` (the `session create` op) is unconditionally FRESH.** It reads no
  recorder file, runs no discovery, and probes no rollout, so it needs no launch target at all; it
  also `rm -f`s any stale `.thread` file, which is what stops the FOLLOWING `resume` from adopting
  the dead namesake's id. Only `resume` ever adopts an id, and adoption is safe there precisely
  because the row has existed continuously since a create that launched fresh and cleared the
  recording. This is the correct semantics for deferred discovery rather than a start/resume
  asymmetry to work around, and it matches the 2026-08-01 decision that a fresh launch carries no
  seed prompt and resumes nothing: a create means a brand-new session row, which by definition owns
  no codex conversation.

### Resume-vs-launch probe (for a bound id)

Unchanged in shape: the `find "${CODEX_HOME:-$HOME/.codex}/sessions" -name '*-<sid>.jsonl'` probe
with the 0/1/raise exit-code fork. `archived_sessions/` is deliberately NOT probed: an archived
session reports not-resumable and the integration starts fresh (operator-decided 2026-08-01;
auto-unarchive would silently reverse an explicit operator action), with the archived history
recoverable manually.

### Invocation forms (redesigned 2026-08-04)

```text
fresh:   sh -c 'echo <msg>; [rm -f <legacy marker>;] rm -f <thread file>; <provision recorder>; exec codex [flags] -c notify=[...] [extra_args...]'
resume:  sh -c 'echo <msg>; [rm -f <legacy marker>;] <provision recorder>; exec codex resume <sid> -c tui.resume_cwd=current [flags] -c notify=[...] [extra_args...]'
picker:  sh -c 'echo <msg>; [rm -f <legacy marker>;] <provision recorder>; exec codex resume -c tui.resume_cwd=current [flags] -c notify=[...] [extra_args...]'
```

`[flags]` is the managed set in its existing order (`--strict-config`, `-m`/`-s`/`-a`/`-p`, `-c`
network, `-c approvals_reviewer`, `--add-dir`, `--search`). The notify override sits after them and
before `extra_args` so an operator `notify` in `extra_args` still wins (codex's last-override-wins),
which is the documented way to switch the binding off. The fresh form also removes any stale
recording: a fresh launch has no conversation bound yet, so a leftover recording would re-report a
dead id on the next op, and on the `create` path that deletion is the second half of the
recreated-namesake guarantee. `session create` always emits the fresh form; the resume and picker
forms are reachable only from `session resume`.

Every decision leaf (resumed-bound, resumed-adopted, picker, fresh, archived-or-gone fresh) carries
its own `launch_note` in the `agw session resume` console output AND a matching pane echo
(operator-decided 2026-08-04: the console must say what is happening), in resume vocabulary.
`-c tui.resume_cwd=current` pins the cross-cwd prompt off (the pane has already `cd`-ed to the
workspace dir). All generated tokens are `shlex.quote`d; no generated piece emits `{{word}}`.

### Config vocabulary (all optional; shape-checked here, codex-owned values forwarded unvalidated)

Extended 2026-08-03 (operator-decided) with the network/writable-dirs/web-search knobs and the
strict-config default, after the first real usage showed the `-c` network spelling too arcane and
its drift mode too silent.

| `harness_integration` field    | CLI surface emitted                          | Notes                                        |
| ------------------------------ | -------------------------------------------- | -------------------------------------------- |
| `model` (str)                  | `-m <value>`                                 | codex-owned values                           |
| `sandbox` (str)                | `-s <value>`                                 | `read-only` / `workspace-write` / ... drift  |
| `approval_policy` (str)        | `-a <value>`                                 | `untrusted` / `on-request` / `never` drift   |
| `profile` (str)                | `-p <value>`                                 | note: an unknown profile is silently ignored |
| `network` (bool)               | `-c sandbox_workspace_write.network_access=` | both directions forward; key is codex-owned  |
| `approvals_reviewer` (str)     | `-c approvals_reviewer="<value>"`            | see the 2026-08-04 note below                |
| `writable_dirs` (list)         | one `--add-dir <dir>` each                   | union-merged across inheritance              |
| `web_search` (bool)            | `--search` when true                         | the server-side tool, not sandbox network    |
| `disable_strict_config` (bool) | omits the default `--strict-config`          | see below                                    |
| `extra_args` (list)            | (the tokens themselves)                      | appended verbatim, last                      |

Choice sets are codex-owned and drift between releases; invalid values surface as codex's own
startup error in the pane (the same rule as claude-code); when the rejection kills the pane
instantly, `session create` / `session resume` capture that output into their own error, so the
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

**`approvals_reviewer` added 2026-08-04 (operator-decided), for the auto-mode story.** Verified
against 0.146.0: the config key exists (`--strict-config` accepts it; there is no dedicated flag, so
the strict-config default is the drift guard), and its enum is `user` / `auto_review` /
`guardian_subagent`. Codex's own app-server schema documents the semantics: it "configures who
approval requests are routed to for review" (sandbox escapes, blocked network access, MCP approval
prompts), defaulting to `user` (the human is prompted); `auto_review` routes them to "a carefully
prompted subagent" applying "a risk-based decision framework"; `guardian_subagent` is accepted as a
legacy alias. The value forwards unvalidated as a quoted TOML string. Live behavior of an actual
escalation under `auto_review` was NOT exercised (needs an authed session); the schema text is the
verification basis.

### Readiness, provisioning, auth

Readiness is `require_commands` for the single binary `codex`; session state stays an op-time
concern. Auth and workspace trust are provisioning concerns and OUT of harness scope for v1 (parity
with claude v1): an unauthenticated pane parks on codex's own login menu, an untrusted directory
parks on its trust prompt, and both are codex's own visible, actionable surfaces. The bundled
`codex` user-install-command is the plugin's declarable (verify the current official install method
at implementation time; do not recall it).

### State stored per session (in the namespaced blob)

`session_id` (the bound codex UUIDv7, absent until the notify recorder or layer-2 discovery binds
one, which only ever happens on a `resume`). The marker-era `discovery_marker` key (2026-08-01
through 2026-08-04) is retired: readers ignore it and the integration deletes it opportunistically
when touching the blob.

The recorder file path is derived from the session name
(`~/.agentworks/codex/<session-name>.thread`). Being name-derived, it is stale-prone in the same way
the retired marker was; what makes it safe is not the path but the two rules around it, both of
which carry their own tests: it is written ONLY with an id codex itself reported for a conversation
live in this session's pane, and it is read ONLY by `resume`, never by `create`, which instead
deletes it (see the create-is-always-fresh decision above). The launch cwd is the session's
workspace directory, threaded to the integration at construction (`workspace_path`).

### Out of v1 (recorded)

Thread-name binding for human legibility (safe only post-discovery), auto-unarchive, concurrent
attach protection (codex takes no lock; a future liveness surface is the right home), auth
provisioning (`codex login --with-api-key` fits a future `harness-user-provisioner`), and
`codex exec` headless ops.

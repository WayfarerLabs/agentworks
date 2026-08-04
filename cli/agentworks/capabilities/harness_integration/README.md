# Harness Integrations

> The detailed companion to the capability overview in [`../README.md`](../README.md), focused on
> the `harness-integration` kind; the architectural record is
> [ADR 0020](../../../../docs/adrs/0020-harness-integration.md). That overview covers the lifecycle,
> readiness stages, and secrets every capability shares. This guide is for both operators and
> developers: the first part (before the Technical Overview) covers the functional details (what a
> integration is, its obligations) that matter to both audiences, and the part after the divider is
> developer-focused, covering the implementation contract and the practices that make the shipped
> integrations robust.

## What Is a Harness Integration?

In the world of agentic engineering, the term "harness" is a bit overloaded but is generally used to
refer to the tooling within which agentic workloads operate. Agentworks **does not** aim to be this
tooling. Rather, Agentworks aims to provide the infrastructure to support whatever tooling the
operator chooses to run.

In the simplest (and default) case, Agentworks just runs a plain shell as the session's workload.
From here, the operator can do whatever they want in terms of configuring the environment and
launching their desired tooling.

However, Agentworks also supports deeper tooling support through a **harness integration**. An
Agentworks harness integration knows how to run a specific harness as the session's workload,
including checking dependencies, configuring it, and implementing its exact start/resume semantics.
This allows tight integration with harnesses such as Claude Code or Codex without confusing the
Agentworks integration layer with the harness it drives.

The initial implementation focuses narrowly on basic configuration and start/resume semantics, but
the integration is designed to grow: richer per-session behavior now, and, as the scope model
described below matures, tooling logic at the user and workspace levels too (auth, rule/skill/hook
publishing, and the like).

And note that regardless of integration, all sessions run inside the standard tmux session. This
provides both access to stdin/stdout/stderr for interactivity as well as the persistent execution
capability. This is all handled automatically by the core Agentworks session logic. The integration
itself has no part in this (or other core operations such as user and workspace management) other
than to tell the core session logic what to run.

## A Note on Scope

The current harness-integration concept is scoped solely to the session (the running
workload/process), and everything an integration does must stay session-local. It may set up state
that belongs to this one session, but it must not cause effects that reach a wider scope: another
session of the same user, the whole user account, the workspace, or the machine.

Auth is the clarifying example. Authenticating a tool in a way that stays session-local (say, an
injected env var only this session sees) is entirely fair game. Authenticating in a way that mutates
shared user state (a login written into the user's home that every one of that user's sessions then
inherits) is not, because it reaches past the session. The same line rules out installing a plugin
into the workspace (every session there would see it) or changing anything machine-wide.

This is a real current limitation, not the end state. Tooling logic legitimately lives at the
machine, user, and workspace scopes too, and a model to support those cleanly is in active design.
It will expand what harness integrations can do; until it lands, keep every integration effect
session-scoped.

## Available Integrations

Three integrations ship today. This list can change, so
`agw resource list --kind harness-integration --include-disabled` is the definitive set on any given
install.

- **`shell`** (built in) is the default. By default it simply opens the configured shell for the
  session's target user (agent or admin user). It can further be configured to run a specific
  command with `resume_command` for a different command on `session resume` versus the initial start
  (via `session create`). From here, an operator can do whatever they want in terms of configuring
  the environment and launching their desired tooling. They're just largely on their own.
- **`claude-code`** (via the `claude` system plugin) drives an interactive Claude Code session. It
  knows how to launch Claude Code and, on resume, how to check for an existing session and reattach
  if found so that the operator experience is seamless and they can pick up right where they left
  off. Limited configuration is supported, expressed in Claude Code's own terms: `permission_mode`
  and `model` map to the `--permission-mode` and `--model` CLI flags, and `extra_args` passes
  additional Claude Code CLI arguments through verbatim.
- **`codex`** (via the `codex` system plugin) drives an interactive Codex session. Like
  `claude-code`, it launches the tool and, on resume, reattaches to the existing conversation
  instead of starting over when a Codex session exists, and offers limited configuration options.
  Because Codex mints its own session ids, it learns which conversation is this session's from Codex
  itself (Codex's `notify` hook), and when it genuinely cannot tell it opens Codex's own session
  picker in the pane rather than guessing or failing.

## Session Resume

Where possible, integrations should support resuming a session on `session resume` rather than
starting over. This is going to mean different things for different tools, but the general idea is
that if a session is interrupted or resumed, the operator should be able to pick up right where they
left off rather than losing their work.

Of course, this is not always possible. Some tools, like a plain shell, do not have any concept of a
session to resume, so the integration simply starts a new workload.

## Integration Obligations

A harness integration knows how to run one harness as a session's workload and bring it back, and
nothing about the machinery around it. It:

- **MUST** produce the command that launches its tool for a given session, to run as the target user
  (an agent, or admin) in the session's workspace.
- **MUST** produce the command that allows the workload to be resumed if the workload is interrupted
  (e.g. process exit, machine restart, manual session stop, etc.); where possible (i.e. for
  "stateful" tools), the integration **SHOULD** resume the existing session rather than starting
  over.
- **MUST** declare the executables its tool needs on the launch target, so Agentworks can verify
  their presence before starting and surface a missing tool as an actionable error.
- **MUST**, for a stateful tool, own a durable session identity and refuse to guess it: mint or
  learn the tool's session id once, store it, and read it back verbatim. An ambiguous match must
  never be silently adopted, since that splices one session's history into another; resolving the
  ambiguity by handing the choice to the operator (in the pane, where they can see the candidates)
  is better than failing the op, and failing the op is better than guessing.
- **MUST** decide resume-versus-launch at op time from the tool's own on-disk state on the launch
  target, so an interrupted session or a resumed VM recovers its prior work rather than silently
  starting over. Agentworks is built to lose running processes and restart, so an integration
  **MUST** recover from durable state alone and **MUST NOT** depend on in-memory continuity.
- While some degree of configuration "understanding" is required, **SHOULD NOT** try to validate
  tool-owned choice sets (like `permission_mode` or `model`) because they drift across the tool's
  releases. The tooling itself will validate those and the existing session mechanism should surface
  those errors back to the operator.
- **MUST NOT** own or touch the tmux session, the Linux user, the workspace, attach/detach, or the
  session lifecycle; it returns a pane command string and lets Agentworks own everything around it.
- **MUST NOT** do anything in its workspace or agent that would interfere with other sessions or
  running processes (beyond normal file modification, which is part of the workspace contract)
  whether under its own user or a different user: it must not kill or signal processes it did not
  start, mutate shared user state, touch another session's tmux, mutate shared tool or system
  config, hold exclusive locks, or delete files it does not own.
- **MUST** keep its effects session-scoped: it may set up state for this one session (for example, a
  session-local env var), but **MUST NOT** cause effects that persist to the whole user, the
  workspace, or the machine, such as an auth flow that writes shared login state or a workspace-wide
  plugin install. A session-local secret (say, for auth) belongs in that session's environment,
  never baked into the launch command, where it would leak into process listings and terminal
  scrollback. Broader-scope provisioning is a separate, forthcoming concern (see
  [A Note on Scope](#a-note-on-scope)).
- **SHOULD** surface its launch decision (resumed, started fresh, adopted by discovery, or handed to
  the operator to disambiguate) to the operator, both in the command's output and as the pane's
  first visible line. Every distinct decision needs its own wording: "something happened" is not a
  decision line, and an operator who cannot tell adoption from a fresh start cannot catch a wrong
  one.

It does not own the tmux session, the user, the workspace, or attach and detach. Agentworks provides
those; the integration only decides what runs in the pane.

## Technical Overview

The preceding sections describe the operator-facing model. The remaining sections cover where a
integration sits in the capability model, its implementation contract, how the session machinery
consumes it, and the practices that make the shipped integrations robust, especially session resume.

A **harness integration** is a harness's runtime adapter: it knows how a session workload (a plain
shell, Claude Code, Codex, ...) is configured, started, and resumed, and what the launch target must
provide for that to work. The session is the rich consuming resource: a session node HOLDS an
integration instance, composes its readiness, and the session manager invokes its ops. The
integration never touches tmux, the database, or the CLI; it validates its config, probes its
target, and returns pane command strings.

Three integrations ship today and serve as references:

- **`shell`** (`shell.py`): the core built-in and default. Operator-authored `command` /
  `resume_command` / `required_commands`. The minimal member: no state, no tool conventions.
  `restart_command` remains a deprecated 0.13.0 input only.
- **`claude-code`** (`agentworks/plugins/claude/harness_integration.py`): the first tool
  integration, shipped as the opt-in `claude` system plugin. The reference for everything stateful:
  durable session identity, resume-vs-launch detection, tool flag mapping, and the plugin packaging.
- **`codex`** (`agentworks/plugins/codex/harness_integration.py`): the second tool integration,
  shipped as the opt-in `codex` system plugin. The reference for the OTHER identity form: the tool
  mints its own session ids, so the integration gets the id FROM the tool (codex's `notify` hook
  reports it after every completed turn) and stores it, falling back to source-filtered discovery of
  the tool's on-disk state and, when that is ambiguous, to codex's own session picker in the pane.

### Where a Harness Integration Sits

The capability ladder, harness-integration edition:

- The **kind** (`"harness-integration"`, `kinds.py`) is fixed by the core: `category="capability"`,
  `miss_policy="error"` (a `session-template` naming an unknown integration fails at finalize),
  `builtin_override="reserved"` (a plugin cannot replace `shell`).
- A **capability** is a `HarnessIntegration` subclass registered in `HARNESS_INTEGRATION_REGISTRY`
  (`__init__.py`), plus a read-only `HarnessIntegrationEntry` registry row so it lists and describes
  like any resource. Core built-ins publish their own rows (`publish_to`); a plugin-seated
  integration's row is published by the plugin machinery with a `system-plugin` origin instead, and
  `publish_to` skips it.
- An **instance** is one integration bound to one session: the merged `harness_integration_config`
  blob plus the session's identity (`session_name`, `vm_name`, `workspace_name`, the agent-or-admin
  target) and its per-session state blob. Constructed fresh per operation by the session node
  factories.
- The **consuming resource** is the `session-template` (it owns the config: in manifests,
  `spec.harness_integration` is one tagged table whose `name` key selects the integration and whose
  remaining keys are that integration's config; the operator-facing shapes, including the TOML
  spelling and the deprecated sibling form, are documented in `docs/guides/resources.md`) and, at
  runtime, the session node that holds the instance.

Layering is a hard rule: this package imports neither `sessions/` nor `orchestration/` (the `target`
type is a local `Protocol` for exactly this reason), and `test_shell_integration.py` asserts it. An
integration depends only on the framework; the session domain depends on the integration.

### The Contract

A new harness integration implements this surface (see `base.py` for the full docstrings):

#### Class Identity

`name` and `description` ClassVars (the registry row), inherited `owner_kind = "session-template"`
(error framing: config errors render as `session-template/<name>`).

#### Validation: Shape and Vocabulary Only

Unknown fields raise `ConfigError` naming the integration and the field(s); each present field is
type-checked. Two rules with teeth:

- **No completeness rules here.** `validate` runs per declared blob, and a child template may
  declare a partial blob that only becomes complete after the inheritance merge. Required-field and
  cross-field rules belong in the second `validate` call the resolver makes on the merged blob (no
  shipped integration has any; the slot exists).
- **Do not validate tool-owned choice sets.** `claude-code` forwards `permission_mode` and `model`
  values verbatim: the valid choices are the tool's and drift between its releases, so a stale
  integration-side enum would reject values a newer CLI accepts. An invalid value surfaces as the
  tool's own startup error in the pane, which is the right place. That promise holds even when the
  workload dies too fast for the pane to ever be attached: `session create` / `session resume`
  detect the instantly-dead pane, capture its output, and fold it into their own error message.

#### Declaring Dependencies: Total and Pure

`dependencies` returns the resource references the config blob implies, secrets above all. Never
raises (malformed fields just omit their edge; `validate` owns the raising). Every shipped
integration returns `()`; the plumbing behind it is live and tested at the framework level: the
session node exposes the integration's declared references through its `config_secret_refs` (what
the preflight sweep predicts resolvability over, with owner/usage framing sourced to the session
template) and derives its bare-name `secret_refs` union from them, with values delivered through
`ctx.secret(name)`. No shipped integration declares a secret yet, so a secret-declaring integration
should expect to be the first real exerciser of that path.

#### Config Inheritance, Decided per Field

When a child template names the SAME integration as its parent chain, the blobs merge through this
hook. The base default is shallow child-wins per key. Each added field requires an explicit merge
policy:

- Scalars (a mode, a model) usually want child-wins: the default is right.
- **Accumulating lists usually want a union.** `shell` overrides `merge_config` to append-dedupe
  `required_commands`, so a child that overrides only `command` cannot silently drop the parent's
  probe list. Note the asymmetry to avoid copying blindly: `claude-code.extra_args` deliberately
  child-wins (an escape hatch is an override, not an accumulation). The selected policy belongs in
  the field's documentation.

The resolver calls `merge_config` on every declared-blob fold, with `base={}` when the lineage
starts (a template with no parents included) or when a child switches to a different integration.
Every implementation must therefore accept an empty base. A different integration's blob never lands
in `base`: the resolver discards accumulated config on an integration switch, so a parent's config
cannot leak across it.

#### Construction: Cheap, No I/O

The base `__init__` binds `(owner_name, config)` and re-runs `validate`; the integration constructor
adds the session identity kwargs and the `state` blob. Nothing else: no probing, no network, no
minting. Anything that needs the world happens in readiness or ops.

#### Readiness: One Probe to Implement

The base owns the whole readiness fork (`_run_readiness`): the loud scope-less error, the
out-of-scope-level skip, the SESSION-level identity guard, the single-fire guard, the admin-vs-agent
target selection, the pending-target defer to runup, and the preflight-tolerated / runup-fatal
missing-transport split. A subclass fills in exactly one slot, `_probe_target`, which should call
the shared `require_commands` helper with the executables the launch target must have on PATH
(`shell`: the merged `required_commands`; `claude-code`: `("claude",)`).

Keep readiness to tool PRESENCE. Session state (is there something to resume?) is an op-time
concern: readiness is read-only and re-runnable by contract, and it runs at command start against a
world the op changes (on resume, the resume decision must see the old process already dead, which
only the op-time probe does).

#### Ops: Returning the Pane Command String

- The return value is a command string, not an execution: the session manager wraps it (template-var
  substitution, then the tmux pane's `$SHELL -lic 'cd <dir> && exec <command>'`) and runs it. Empty
  string means "just the login shell".
- `start` serves `session create`; `resume` serves `session resume`. The orchestrator kills the old
  workload BEFORE calling `resume`, a deliberate ordering guarantee: a stateful integration decides
  resume-vs-launch with the old process dead and its on-disk state settled.
- **`start` runs against a brand-new session row, and that is load-bearing, not trivia.** `create`
  inserts the row after the op, so a `start` call means no prior workload of this session exists.
  Where identity is MINTED (`claude-code`), the two ops can share one decision method and the
  difference is purely caller-side. Where identity is DISCOVERED, they must not: every discovery
  channel keys off something the tool wrote earlier under a name or path Agentworks did not reserve
  (a session name, a workspace directory), so discovering at create time is precisely how a
  brand-new session inherits a deleted namesake's history or a stranger's conversation. `codex` is
  the shipped example: its `start` is unconditionally fresh, probes nothing, and clears the stale
  identity file, and only `resume` ever adopts an id. Asymmetry here is the correct semantics, not a
  wart. Ask which op could legitimately find work to resume, and let the answer shape the split.

#### The Operator-Facing Decision Line

`launch_note` returns a one-line note about what the op decided (`claude-code`: resumed vs started
fresh) and the session manager prints it in the CLI op output. Default `None` keeps `shell` silent.
Pair it with a pane-visible echo (below) so the decision is visible in both places the operator
looks.

#### Per-Session State: The Persisted Blob

`self._state` is a dict the integration reads and mutates in place during ops; the session manager
persists it (inside the row's full blob, below) to the session row's `harness_integration_state`
JSON column after each op. The persistence contract the manager provides (and tests pin):

- On create, the op runs BEFORE the row insert, so state minted during `start` lands with the new
  row atomically.
- On resume, the updated blob is persisted BEFORE the new tmux session is created, so a failed
  launch retried later still sees the same identity.
- A malformed stored blob degrades to `{}` with a warning, never a crash; likewise a stored
  namespace value that is not a dict degrades to empty with a warning at the seam.

The stored blob is NAMESPACED by integration name at the platform seam
(`sessions/nodes._harness_integration_for_template`): the row holds
`{"<integration-name>": {<that integration's keys>}}`, and each instance is handed only its own
namespace as `self._state` (the same object, so in-place mutation keeps the full blob current). An
integration never sees another integration's keys, so if a session's template is re-pointed from one
integration to another, cross-integration key collisions are structurally impossible and the old
integration's namespace survives a switch away and back. Author an integration against `self._state`
alone; still treat wrong-typed values as absent (as `claude-code._session_id` does with its
`isinstance(sid, str)` check), since the blob content is only as trustworthy as the DB it came from.
Pre-namespacing rows carried `claude-code`'s `session_id` at the blob's top level; a legacy hoist
(`HarnessIntegration.hoist_legacy_state`, overridden by `claude-code`) adopts those at the seam and
is compatibility code slated for DELETION on the next major release.

### How the Session Machinery Consumes a Harness Integration

The surrounding wiring supplies the following behavior and debugging boundaries:

- **Enablement gate:** `ensure_harness_integration_enabled` (`__init__.py`) runs at session create
  and resume (`sessions/manager/_create_build.py`, `_lifecycle.py`; resume also covers recovering a
  broken session). A template naming a disabled plugin integration stays ready (listing works);
  USING it raises with an "enable plugin `<name>`" hint. Plain `session attach` never constructs an
  integration, so it does not gate. The node factories do not gate either (they thread no registry);
  an AST drift guard (`cli/tests/sessions/test_harness_integration_gate_drift.py`) enforces that
  every factory caller gates first.
- **Construction point:** `_harness_integration_for_template` (`sessions/nodes.py`) is the single
  place instances are built, and the namespacing seam: it holds the FULL blob (`{}` on a fresh
  create, the stored `row.harness_integration_state` for a live session) and constructs the
  integration with only its own namespace; the session node exposes the full blob
  (`harness_integration_state`) for the manager to persist.
- **Op context:** the manager assembles an op-start `RunContext` (targets, operation scope, scoped
  secrets) per op (`sessions/manager/_create_roll.py`, `_lifecycle.py`). Readiness runs through the
  node's composed `preflight` / `runup` on create; the resume path deliberately builds no runup
  context (its only readiness pass is the pre-kill preflight sweep), and on both paths only the op
  context carries secrets. An integration cannot depend on runup firing before `resume`.
- **Substitution stays outside:** the returned string is passed through `{{session_name}}` /
  `{{workspace_name}}` substitution at the call site. The resulting constraints appear under
  "Building the pane command" below.
- **Display:** `session list` / `session describe` show the resolved integration name by
  re-resolving the template read-only (no instance is built, no gate runs);
  `resource list --kind harness-integration` and `resource describe harness-integration/<name>` show
  the registry row.

### Best Practices

#### Session Resume: The Stateful-Integration Pattern

The `claude-code` integration is the worked example; the pattern generalizes to any harness with
resumable sessions. Five rules, each earned:

1. **Own a durable identity; never derive it.** Mint the tool-side session id once (a v4 uuid where
   the tool accepts one) on the first `start`, store it in the state blob, and read it back verbatim
   forever after. If the tool will not accept a caller-supplied id, the same rule holds in its other
   form: let the tool mint the id, get it FROM THE TOOL, and store THAT. `codex` is the shipped
   example, and the shape it landed on generalizes: **prefer a channel where the tool reports its
   own id over inferring which of its files is yours.** Codex's `notify` hook runs a script after
   every completed turn with the turn's thread id, so the integration provisions a recorder, binds
   the id it records, and resumes deterministically. Inference is the FALLBACK, filtered as narrowly
   as the tool's own state allows (for codex, the `"source":"cli"` stamp plus the session's
   workspace cwd, which is exactly the set codex's own resume picker shows), and genuine ambiguity
   is handed to the human IN BAND rather than raised: several candidates launch codex's own session
   picker in the pane, and the next completed turn binds whatever they chose. That ordering is not
   cosmetic. The marker-and-mtime inference codex shipped first treated every rollout in the
   workspace as a candidate, and codex subagents write sibling rollouts with the same cwd as their
   parent, so one session that ran subagents produced 14 indistinguishable candidates and a bricked
   resume. The manager's persistence contract makes any of these survive restarts. Derivation
   schemes (from session name, cwd, or the tool's own directory layout) are brittle against renames
   and tool-version drift; a stored opaque value the tool itself reported is not.
2. **Decide resume-vs-launch at op time, on the launch target, from the tool's own durable state.**
   Probe for the stored id's artifact (for Claude, the transcript `<sid>.jsonl` under the projects
   dir) over the transport, with the same `$SHELL -lic` environment the pane will get. Do it per op,
   not cached: the world changes between ops, and resume runs with the old process dead precisely so
   this probe sees settled state.
3. **Verify empirically that the probe boundary equals the tool's resume boundary.** The claude work
   ran a controlled experiment (sessions abandoned at every stage) to confirm transcript presence
   and Claude's own resume boundary are the SAME line, which is what makes both failure modes (blind
   resume of nothing; fresh launch colliding with a live id) impossible rather than merely rare.
   Each integration must establish the equivalent result before relying on a probe and record the
   verified tool version (the latest-stable rule requires exercising the real CLI rather than
   recalling flags from memory, with re-verification after major tool updates).
4. **A probe that could not RUN is not a probe that found nothing.** Branch on the exit code
   trichotomy: 0 means resume, 1 means launch fresh, anything else (SSH failure, shell that could
   not start) RAISES a typed `StateError` rather than guessing. Guessing "fresh" launches with a
   reserved id the tool may reject as in-use, and the pane dies opaquely.
5. **Make the decision visible twice, per leaf.** An `echo` as the pane's first line (the operator
   attaching sees which way it went) and `launch_note` (the operator running the CLI op sees it
   too). Every distinct outcome gets its own wording in both places: codex ships five (resumed,
   adopted by discovery, picker, fresh, archived-or-gone), because a decision line that cannot
   distinguish adoption from a fresh start cannot tell the operator a heuristic went wrong.

#### Building the Pane Command

- **Compound commands need a single `sh -c`.** The pane wrapper is `... && exec <returned string>`,
  and `exec` takes one simple command: with a bare `echo ...; exec tool ...` the shell would `exec`
  the `echo` and nothing after the `;` would ever run. Return `sh -c '<inner>'` with the inner
  `shlex.quote`d, and `exec` the tool inside it so the pane process becomes the tool.
- **`shlex.quote` every generated token.** Build an argv token list, quote each, and space-join.
  Never interpolate raw values into shell syntax.
- **Generated pieces must not emit `{{word}}`.** The call-site substitution raises on unknown
  `{{word}}` tokens and substitutes known ones over the entire returned string. Operator-authored
  slices (shell's commands, `extra_args` elements) carry `{{session_name}}` semantics on purpose;
  the code-generated skeleton must stay clear of doubled-brace words. Single braces (`${VAR}`, JSON)
  are untouched.
- **Model the common flags; leave the rest to `extra_args`.** A small optional vocabulary
  (`permission_mode`, `model`) plus a verbatim, appended-last `extra_args` list keeps the
  integration useful without chasing the tool's whole flag surface. Append `extra_args` after the
  managed flags so operators can override or extend.

#### Integration-Owned Files on the Launch Target

Some integrations need a file on the target that is theirs rather than the tool's: a helper script
the tool invokes, a small piece of state the tool reports into. Put it under
**`~/.agentworks/<integration-name>/`** on the launch target (`codex` established this with
`~/.agentworks/codex/`), never in the tool's own config dir (that is shared user state the scope
rule forbids mutating) and never in the workspace (every session there would see it).

Three rules make a per-user file legitimate under the session-scoped effects rule
([A Note on Scope](#a-note-on-scope)):

- **Rewrite it identically on every launch.** If two sessions of the same user would write the same
  bytes, neither can change what the other's file does, and an Agentworks upgrade cannot leave a
  stale copy of the same file behind. Write it atomically (stage, `chmod`, `mv`) so the tool never
  sees a partial one. The corollary of the versioning rule below: an OBSOLETE version (a `-v1` after
  the integration moved to `-v2`) is deliberately left alone, since a running session may still hold
  its path, and nothing prunes it today.
- **Version anything the tool holds a path to.** `codex`'s recorder is `record-thread-v1.sh`: a
  future recorder taking different arguments becomes `-v2`, so an upgrade mid-session cannot reshape
  the contract of a script a running tool is about to invoke.
- **Per-SESSION files are keyed by session name, so treat their contents as suspect.** A session
  name is reusable: a deleted session's file outlives it and the next namesake finds it. Decide
  explicitly which ops may READ such a file (`codex`: only `resume`, never `create`, which deletes
  it), because "the file exists" never means "it belongs to this session".

#### Probing the Launch Target

Run probes as `"$SHELL" -lic '<inner>'` with `check=False`: login+interactive sources the same
dotfiles the pane's shell will (mise activation, PATH fragments), so the probe answers for the
environment the workload actually gets. Prefer shell-neutral inner commands (the transcript probe's
`find ... -print -quit | grep -q .` avoids bash/zsh divergence on unmatched globs).
`require_commands` does all of this for executable checks; reuse it rather than hand-rolling.

#### Testing a Harness Integration

No real tool binary anywhere. The layers, with the shipped tests as templates:

- **Unit (the bulk):** `cli/tests/test_claude_code_integration.py`. Use `_FakeTarget` /
  `_FakeResult` from `cli/tests/conftest.py`: a substring-to-result map standing in for the
  transport, so one fake serves the readiness probe (keyed on `command -v <tool>`) and the detection
  probe (keyed on the stored id) in a single test. Cover: config vocabulary (accepts, unknown-field
  raises, wrong-type raises), both detection directions (probe hit resumes, miss launches fresh,
  other exit raises), flag mapping and `extra_args` quoting, the visible-decision line, state
  minting, and whatever the integration promises about `start` versus `resume` (`claude-code` pins
  that they are symmetric; `codex` pins the opposite, that `start` probes nothing and adopts
  nothing, since that promise is a safety property rather than a convenience).
- **Generated shell text, executed:** `cli/tests/test_codex_integration.py`. An exit-code stub
  proves how a probe's ANSWER is classified; it cannot prove the probe asks the right question, and
  the interesting logic in a stateful integration often lives in the shell text itself (codex's
  filter that keeps subagent rollouts out of the candidate set, the recorder script it provisions,
  the three-layer quoting of a `-c` override). Run that text through a real `sh` against scratch
  fixtures with `$HOME` pointed at a tmp dir: still no tool binary, but a mis-quoted token or an
  inverted filter fails in the test rather than in a pane.
- **Orchestrated:** `cli/tests/sessions/test_claude_code_orchestrated.py`. Real create/resume
  through the orchestrator with stubbed transports: state persisted to the row, pre-existing blob
  read back, substitution does not mangle the returned snippet.
- **Plugin end-to-end:** `cli/tests/plugins/test_claude.py`. Through `build_registry`:
  present-but-disabled row with `system-plugin` origin, gate refusal with the enable hint,
  enablement via `[plugins] system`.
- **Parity guards affected by a new integration:** the harness-integration-kind publisher set
  (`cli/tests/resources/test_harness_integration_kind.py`), builtin-entries parity
  (`cli/tests/test_builtin_entries_parity.py`), and the plugin-framework adapter/kind drift guards.
  Update them deliberately; they exist to make additions loud.

#### Shipping a Harness Integration as a System Plugin

The plugin framework's own guide (`agentworks/plugins/README.md`, "Shipping a plugin") is the
authority on the descriptor, registration mechanics, and the enablement model; the `claude` plugin
(`agentworks/plugins/claude/`) is the paved road. The integration-specific outline:

1. Implement the integration class in the plugin package, and declare it in the descriptor:

   ```python
   PLUGIN = Plugin(
       name="codex",
       description="...",
       capabilities={"harness-integration": (CodexIntegration,)},
       manifests="agentworks.plugins.codex",  # or None if no bundled manifests
   )
   ```

2. Register the module in `_INSTALLED_MODULES` (`agentworks/plugins/__init__.py`). Registration
   seats the class into `HARNESS_INTEGRATION_REGISTRY`, so `harness_integration_for` finds it while
   its row publishes with a `system-plugin` origin.
3. Bundle declarable resources as manifests in the plugin's `manifests/` subdir. The canonical one
   is a `user-install-command` (how the tool's binary gets onto a VM user's PATH), published weak
   (add-if-absent) while the plugin is disabled so templates referencing it still finalize.
4. Everything is present-but-disabled until the operator opts in with
   `[plugins] system = ["<name>"]`; `agw doctor` shows the roster. No other installer machinery
   exists or is needed.

The checklist beyond code, per the repo rules: the `[plugins]` block comment in
`cli/agentworks/sample-config.toml`, the harness-integration section of `docs/guides/resources.md`,
the sample manifest (`cli/agentworks/manifests/samples/session-template.yaml`) if it should
demonstrate the new integration, the harness-integration material in `cli/README.md` (under "Session
Templates"), `.cspell.json` for harness names, and a completions check (today no completer
enumerates integration names, so there is nothing to regenerate unless the change also adds CLI
surface; the rule still requires the check).

### Reserved Directions (Recorded, Not Built)

Known holes the current contract leaves open on purpose, so v1 boundaries read as deliberate:

- **Provisioning.** An integration runs the harness; nothing yet provisions the user it runs as (the
  harness's config files, skills, MCP registration, auth). The sketched shape is a paired future
  multi-scope harness-integration expansion. Until it exists, provisioning is install-commands plus
  operator setup, and auth in particular is out of integration scope.
- **Secrets and integration-owned environment.** The declare-and-receive secret plumbing is in place
  but unexercised (no shipped integration declares a secret). The session template's `env` chain,
  including secret-backed entries, is the supported way to put an env var (an API key, a tool
  config-dir override) into the pane today; what does not exist is a way for an integration to
  contribute env from its OWN config. No design record yet; a first-class surface is future work.
- **Liveness and headless ops.** The integration knows start/resume, not "is the workload healthy"
  and not a non-TTY exec mode. Both are plausible extensions of the op surface; no design record
  yet.

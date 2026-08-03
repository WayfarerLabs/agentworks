# Session Harnesses

> The detailed companion to the capability overview in [`../README.md`](../README.md), focused on
> the `harness` kind; the architectural record is
> [ADR 0020](../../../../docs/adrs/0020-session-harness.md). That overview covers the lifecycle,
> readiness stages, and secrets every capability shares. This guide opens with what a harness is and
> does for an operator selecting one, and then, below the Technical Overview divider, goes deep on
> what is specific to harnesses: the contract a new harness implements, how the session machinery
> consumes it, and the practices that made the shipped harnesses robust (session resume above all).
> The operator sections support harness selection and configuration; the sections after the divider
> cover implementation details and the behavior of the shipped harnesses.

A **harness** decides what an agent session actually runs and how that workload is launched and
restarted. It is what lets the same `session` commands drive a plain shell, an interactive Claude
Code session, or another tool entirely, without requiring a different command surface for each. The
selected harness handles the tool-specific details of getting its workload up and bringing it back.

The `harness` block in a `session-template` names the harness and carries whatever settings that
harness accepts (the operator-facing spelling is documented in `docs/guides/resources.md`). Every
session created from that template runs under the selected harness.

## The Shipped Harnesses

Three harnesses ship today:

- **`shell`** is the default. It runs the configured command, or a login shell when no command is
  configured, without tool-specific handling.
- **`claude-code`** is an interactive Claude Code session, shipped as the opt-in `claude` system
  plugin. It knows how to launch Claude Code and, on restart, how to reattach to the conversation
  that was already going.
- **`codex`** is an interactive Codex session, shipped as the opt-in `codex` system plugin. Like
  `claude-code`, it launches the tool and, on restart, reattaches to the existing session instead of
  starting over.

## Session Resume

For the tool harnesses, a restart is not a fresh start. `claude-code` and `codex` reattach to the
session that was already running rather than beginning a new one, so an interrupted or restarted
session picks up where it left off instead of losing its history. This behavior is built into the
tool harnesses. The mechanism, and the rules a new stateful harness must follow to get it right, are
below the divider.

## What a Harness Must Provide

A harness knows how to run one tool as a session's workload and bring it back, and nothing about the
machinery around it. It:

- **MUST** produce the command that launches its tool for a given session, to run as the target user
  (an agent, or admin) in the session's workspace.
- **MUST** produce the command that brings the workload back on a restart; a stateful tool
  **SHOULD** resume its existing session wherever the tool supports it, while the plain `shell`
  simply relaunches.
- **MUST** declare the executables its tool needs on the launch target, so Agentworks can verify
  their presence before starting and surface a missing tool as an actionable error.
- **MUST**, for a stateful tool, own a durable session identity and refuse to guess it: mint or
  discover the tool's session id once, store it, read it back verbatim, and raise rather than adopt
  an ambiguous match that could splice one session's history into another.
- **MUST** decide resume-versus-launch at op time from the tool's own on-disk state on the launch
  target, so an interrupted session or a restarted VM recovers its prior work rather than silently
  starting over. Agentworks is built to lose running processes and restart, so a harness **MUST**
  recover from durable state alone and **MUST NOT** depend on in-memory continuity.
- **MUST NOT** validate the tool's own choice sets (model names, permission or sandbox modes); those
  drift across the tool's releases, so an unrecognized value is forwarded verbatim and left to
  surface as the tool's own startup error.
- **MUST NOT** own or touch the tmux session, the Linux user, the workspace, attach/detach, or the
  session lifecycle; it returns a pane command string and lets Agentworks own everything around it.
- **MUST NOT** assume the tool is already authenticated or bake credentials into the launch: the
  tool's own accruing login state is out of harness scope today (a future `harness-user-provisioner`
  is the sketched owner), and the harness must not depend on having provisioned it.
- **MUST NOT** do anything in its workspace or agent that would interfere with other sessions or
  running processes, beyond normal file modification, whether under its own user or a different
  user: it must not kill or signal processes it did not start, touch another session's tmux, mutate
  shared tool or system config, hold exclusive locks, or delete files it does not own.
- **SHOULD** surface its launch decision (resumed, started fresh, or adopted by discovery) to the
  operator, both in the command's output and as the pane's first visible line.

It does not own the tmux session, the user, the workspace, or attach and detach. Agentworks provides
those; the harness only decides what runs in the pane.

## Technical Overview

The preceding sections describe the operator-facing model. The remaining sections cover where a
harness sits in the capability model, its implementation contract, how the session machinery
consumes it, and the practices that make the shipped harnesses robust, especially session resume.

A **harness** is a tool's runtime adapter: it knows how a session workload (a plain shell, Claude
Code, Codex, ...) is configured, started, and restarted, and what the launch target must provide for
that to work. The session is the rich consuming resource: a session node HOLDS a harness instance,
composes its readiness, and the session manager invokes its ops. The harness never touches tmux, the
database, or the CLI; it validates its config, probes its target, and returns pane command strings.

Three harnesses ship today and serve as references:

- **`shell`** (`shell.py`): the core built-in and default. Operator-authored `command` /
  `restart_command` / `required_commands`. The minimal member: no state, no tool conventions.
- **`claude-code`** (`agentworks/plugins/claude/harness.py`): the first tool harness, shipped as the
  opt-in `claude` system plugin. The reference for everything stateful: durable session identity,
  resume-vs-launch detection, tool flag mapping, and the plugin packaging.
- **`codex`** (`agentworks/plugins/codex/harness.py`): the second tool harness, shipped as the
  opt-in `codex` system plugin. The reference for the OTHER identity form: the tool mints its own
  session ids, so the harness discovers the id from the tool's on-disk state (scoped by a stored
  launch-marker anchor and the session's workspace cwd) and stores it, refusing to guess when
  discovery is ambiguous.

### Where a Harness Sits

The capability ladder, harness edition:

- The **kind** (`"harness"`, `kinds.py`) is fixed by the core: `category="capability"`,
  `miss_policy="error"` (a `session-template` naming an unknown harness fails at finalize),
  `builtin_override="reserved"` (a plugin cannot replace `shell`).
- A **capability** is a `Harness` subclass registered in `HARNESS_REGISTRY` (`__init__.py`), plus a
  read-only `HarnessEntry` registry row so it lists and describes like any resource. Core built-ins
  publish their own rows (`publish_to`); a plugin-seated harness's row is published by the plugin
  machinery with a `system-plugin` origin instead, and `publish_to` skips it.
- An **instance** is one harness bound to one session: the merged `harness_config` blob plus the
  session's identity (`session_name`, `vm_name`, `workspace_name`, the agent-or-admin target) and
  its per-session state blob. Constructed fresh per operation by the session node factories.
- The **consuming resource** is the `session-template` (it owns the config: in manifests,
  `spec.harness` is one tagged table whose `name` key selects the harness and whose remaining keys
  are that harness's config; the operator-facing shapes, including the TOML spelling and the
  deprecated sibling form, are documented in `docs/guides/resources.md`) and, at runtime, the
  session node that holds the instance.

Layering is a hard rule: this package imports neither `sessions/` nor `orchestration/` (the `target`
type is a local `Protocol` for exactly this reason), and `test_harness_shell.py` asserts it. A
harness depends only on the framework; the session domain depends on the harness.

### The Contract

A new harness implements this surface (see `base.py` for the full docstrings):

#### Class Identity

`name` and `description` ClassVars (the registry row), inherited `owner_kind = "session-template"`
(error framing: config errors render as `session-template/<name>`).

#### Validation: Shape and Vocabulary Only

Unknown fields raise `ConfigError` naming the harness and the field(s); each present field is
type-checked. Two rules with teeth:

- **No completeness rules here.** `validate` runs per declared blob, and a child template may
  declare a partial blob that only becomes complete after the inheritance merge. Required-field and
  cross-field rules belong in the second `validate` call the resolver makes on the merged blob (no
  shipped harness has any; the slot exists).
- **Do not validate tool-owned choice sets.** `claude-code` forwards `permission_mode` and `model`
  values verbatim: the valid choices are the tool's and drift between its releases, so a stale
  harness-side enum would reject values a newer CLI accepts. An invalid value surfaces as the tool's
  own startup error in the pane, which is the right place.

#### Declaring Dependencies: Total and Pure

`dependencies` returns the resource references the config blob implies, secrets above all. Never
raises (malformed fields just omit their edge; `validate` owns the raising). Every shipped harness
returns `()`; the plumbing behind it is live and tested at the framework level: the session node
exposes the harness's declared references through its `config_secret_refs` (what the preflight sweep
predicts resolvability over, with owner/usage framing sourced to the session template) and derives
its bare-name `secret_refs` union from them, with values delivered through `ctx.secret(name)`. No
shipped harness declares a secret yet, so a secret-declaring harness should expect to be the first
real exerciser of that path.

#### Config Inheritance, Decided per Field

When a child template names the SAME harness as its parent chain, the blobs merge through this hook.
The base default is shallow child-wins per key. Each added field requires an explicit merge policy:

- Scalars (a mode, a model) usually want child-wins: the default is right.
- **Accumulating lists usually want a union.** `shell` overrides `merge_config` to append-dedupe
  `required_commands`, so a child that overrides only `command` cannot silently drop the parent's
  probe list. Note the asymmetry to avoid copying blindly: `claude-code.extra_args` deliberately
  child-wins (an escape hatch is an override, not an accumulation). The selected policy belongs in
  the field's documentation.

The resolver calls `merge_config` on every declared-blob fold, with `base={}` when the lineage
starts (a template with no parents included) or when a child switches to a different harness. Every
implementation must therefore accept an empty base. A different harness's blob never lands in
`base`: the resolver discards accumulated config on a harness switch, so a parent's config cannot
leak across it.

#### Construction: Cheap, No I/O

The base `__init__` binds `(owner_name, config)` and re-runs `validate`; the harness constructor
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
world the op changes (on restart, the resume decision must see the old process already dead, which
only the op-time probe does).

#### Ops: Returning the Pane Command String

- The return value is a command string, not an execution: the session manager wraps it (template-var
  substitution, then the tmux pane's `$SHELL -lic 'cd <dir> && exec <command>'`) and runs it. Empty
  string means "just the login shell".
- `start` serves `session create`; `restart` serves `session restart`. The orchestrator kills the
  old workload BEFORE calling `restart`, a deliberate ordering guarantee: a stateful harness decides
  resume-vs-launch with the old process dead and its on-disk state settled.
- `start` and `restart` should be symmetric for a stateful harness (both call one shared decision
  method); the difference between them is caller-side.

#### The Operator-Facing Decision Line

`launch_note` returns a one-line note about what the op decided (`claude-code`: resumed vs started
fresh) and the session manager prints it in the CLI op output. Default `None` keeps `shell` silent.
Pair it with a pane-visible echo (below) so the decision is visible in both places the operator
looks.

#### Per-Session State: The Persisted Blob

`self._state` is a dict the harness reads and mutates in place during ops; the session manager
persists it (inside the row's full blob, below) to the session row's `harness_state` JSON column
after each op. The persistence contract the manager provides (and tests pin):

- On create, the op runs BEFORE the row insert, so state minted during `start` lands with the new
  row atomically.
- On restart, the updated blob is persisted BEFORE the new tmux session is created, so a failed
  launch retried later still sees the same identity.
- A malformed stored blob degrades to `{}` with a warning, never a crash; likewise a stored
  namespace value that is not a dict degrades to empty with a warning at the seam.

The stored blob is NAMESPACED by harness name at the platform seam
(`sessions/nodes._harness_for_template`): the row holds
`{"<harness-name>": {<that harness's keys>}}`, and each instance is handed only its own namespace as
`self._state` (the same object, so in-place mutation keeps the full blob current). A harness never
sees another harness's keys, so if a session's template is re-pointed from one harness to another,
cross-harness key collisions are structurally impossible and the old harness's namespace survives a
switch away and back. Author a harness against `self._state` alone; still treat wrong-typed values
as absent (as `claude-code._session_id` does with its `isinstance(sid, str)` check), since the blob
content is only as trustworthy as the DB it came from. Pre-namespacing rows carried `claude-code`'s
`session_id` at the blob's top level; a legacy hoist (`Harness.hoist_legacy_state`, overridden by
`claude-code`) adopts those at the seam and is compatibility code slated for DELETION on the next
major release.

### How the Session Machinery Consumes a Harness

The surrounding wiring supplies the following behavior and debugging boundaries:

- **Enablement gate:** `ensure_harness_enabled` (`__init__.py`) runs at session create and restart
  (`sessions/manager/_create_build.py`, `_lifecycle.py`; restart also covers recovering a broken
  session). A template naming a disabled plugin harness stays ready (listing works); USING it raises
  with an "enable plugin `<name>`" hint. Plain `session attach` never constructs a harness, so it
  does not gate. The node factories do not gate either (they thread no registry); an AST drift guard
  (`cli/tests/sessions/test_harness_gate_drift.py`) enforces that every factory caller gates first.
- **Construction point:** `_harness_for_template` (`sessions/nodes.py`) is the single place
  instances are built, and the namespacing seam: it holds the FULL blob (`{}` on a fresh create, the
  stored `row.harness_state` for a live session) and constructs the harness with only its own
  namespace; the session node exposes the full blob (`harness_state`) for the manager to persist.
- **Op context:** the manager assembles an op-start `RunContext` (targets, operation scope, scoped
  secrets) per op (`sessions/manager/_create_roll.py`, `_lifecycle.py`). Readiness runs through the
  node's composed `preflight` / `runup` on create; the restart path deliberately builds no runup
  context (its only readiness pass is the pre-kill preflight sweep), and on both paths only the op
  context carries secrets. A harness cannot depend on runup firing before `restart`.
- **Substitution stays outside:** the returned string is passed through `{{session_name}}` /
  `{{workspace_name}}` substitution at the call site. The resulting constraints appear under
  "Building the pane command" below.
- **Display:** `session list` / `session describe` show the resolved harness name by re-resolving
  the template read-only (no instance is built, no gate runs); `resource list --kind harness` and
  `resource describe harness/<name>` show the registry row.

### Best Practices

#### Session Resume: The Stateful-Harness Pattern

The `claude-code` harness is the worked example; the pattern generalizes to any tool with resumable
sessions. Five rules, each earned:

1. **Own a durable identity; never derive it.** Mint the tool-side session id once (a v4 uuid where
   the tool accepts one) on the first `start`, store it in the state blob, and read it back verbatim
   forever after. If the tool will not accept a caller-supplied id, the same rule holds in its other
   form: let the tool mint the id, discover it from the tool's own durable state, and store THAT
   (`codex` is the shipped example: a STORED launch-marker anchor scopes discovery, filtered by the
   session's workspace cwd, and an ambiguous candidate set raises rather than guesses). Know that
   discovery is a heuristic, and say so on its surfaces: codex's adoption `launch_note` names the
   caveat, and its decisions doc records the residual windows honestly, with the operator guidance
   (avoid two concurrently-fresh codex sessions sharing one agent user and workspace directory). The
   manager's persistence contract makes either survive restarts. Derivation schemes (from session
   name, cwd, or the tool's own directory layout) are brittle against renames and tool-version
   drift; a stored opaque value is not.
2. **Decide resume-vs-launch at op time, on the launch target, from the tool's own durable state.**
   Probe for the stored id's artifact (for Claude, the transcript `<sid>.jsonl` under the projects
   dir) over the transport, with the same `$SHELL -lic` environment the pane will get. Do it per op,
   not cached: the world changes between ops, and restart runs with the old process dead precisely
   so this probe sees settled state.
3. **Verify empirically that the probe boundary equals the tool's resume boundary.** The claude work
   ran a controlled experiment (sessions abandoned at every stage) to confirm transcript presence
   and Claude's own resume boundary are the SAME line, which is what makes both failure modes (blind
   resume of nothing; fresh launch colliding with a live id) impossible rather than merely rare.
   Each harness must establish the equivalent result before relying on a probe and record the
   verified tool version (the latest-stable rule requires exercising the real CLI rather than
   recalling flags from memory, with re-verification after major tool updates).
4. **A probe that could not RUN is not a probe that found nothing.** Branch on the exit code
   trichotomy: 0 means resume, 1 means launch fresh, anything else (SSH failure, shell that could
   not start) RAISES a typed `StateError` rather than guessing. Guessing "fresh" launches with a
   reserved id the tool may reject as in-use, and the pane dies opaquely.
5. **Make the decision visible twice.** An `echo` as the pane's first line (the operator attaching
   sees which way it went) and `launch_note` (the operator running the CLI op sees it too).

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
  (`permission_mode`, `model`) plus a verbatim, appended-last `extra_args` list keeps the harness
  useful without chasing the tool's whole flag surface. Append `extra_args` after the managed flags
  so operators can override or extend.

#### Probing the Launch Target

Run probes as `"$SHELL" -lic '<inner>'` with `check=False`: login+interactive sources the same
dotfiles the pane's shell will (mise activation, PATH fragments), so the probe answers for the
environment the workload actually gets. Prefer shell-neutral inner commands (the transcript probe's
`find ... -print -quit | grep -q .` avoids bash/zsh divergence on unmatched globs).
`require_commands` does all of this for executable checks; reuse it rather than hand-rolling.

#### Testing a Harness

No real tool binary anywhere. The layers, with the shipped tests as templates:

- **Unit (the bulk):** `cli/tests/test_harness_claude_code.py`. Use `_FakeTarget` / `_FakeResult`
  from `cli/tests/conftest.py`: a substring-to-result map standing in for the transport, so one fake
  serves the readiness probe (keyed on `command -v <tool>`) and the detection probe (keyed on the
  stored id) in a single test. Cover: config vocabulary (accepts, unknown-field raises, wrong-type
  raises), both detection directions (probe hit resumes, miss launches fresh, other exit raises),
  flag mapping and `extra_args` quoting, the visible-decision line, start/restart symmetry, and
  state minting.
- **Orchestrated:** `cli/tests/sessions/test_claude_code_orchestrated.py`. Real create/restart
  through the orchestrator with stubbed transports: state persisted to the row, pre-existing blob
  read back, substitution does not mangle the returned snippet.
- **Plugin end-to-end:** `cli/tests/plugins/test_claude.py`. Through `build_registry`:
  present-but-disabled row with `system-plugin` origin, gate refusal with the enable hint,
  enablement via `[plugins] system`.
- **Parity guards affected by a new harness:** the harness-kind publisher set
  (`cli/tests/resources/test_harness_kind.py`), builtin-entries parity
  (`cli/tests/test_builtin_entries_parity.py`), and the plugin-framework adapter/kind drift guards.
  Update them deliberately; they exist to make additions loud.

#### Shipping a Harness as a System Plugin

The plugin framework's own guide (`agentworks/plugins/README.md`, "Shipping a plugin") is the
authority on the descriptor, registration mechanics, and the enablement model; the `claude` plugin
(`agentworks/plugins/claude/`) is the paved road. The harness-specific outline:

1. Implement the harness class in the plugin package, and declare it in the descriptor:

   ```python
   PLUGIN = Plugin(
       name="codex",
       description="...",
       capabilities={"harness": (CodexHarness,)},
       manifests="agentworks.plugins.codex",  # or None if no bundled manifests
   )
   ```

2. Register the module in `_INSTALLED_MODULES` (`agentworks/plugins/__init__.py`). Registration
   seats the class into `HARNESS_REGISTRY`, so `harness_for` finds it while its row publishes with a
   `system-plugin` origin.
3. Bundle declarable resources as manifests in the plugin's `manifests/` subdir. The canonical one
   is a `user-install-command` (how the tool's binary gets onto a VM user's PATH), published weak
   (add-if-absent) while the plugin is disabled so templates referencing it still finalize.
4. Everything is present-but-disabled until the operator opts in with
   `[plugins] system = ["<name>"]`; `agw doctor` shows the roster. No other installer machinery
   exists or is needed.

The checklist beyond code, per the repo rules: the `[plugins]` block comment in
`cli/agentworks/sample-config.toml`, the harness section of `docs/guides/resources.md`, the sample
manifest (`cli/agentworks/manifests/samples/session-template.yaml`) if it should demonstrate the new
harness, the harness material in `cli/README.md` (under "Session Templates"), `.cspell.json` for
tool names, and a completions check (today no completer enumerates harness names, so there is
nothing to regenerate unless the change also adds CLI surface; the rule still requires the check).

### Reserved Directions (Recorded, Not Built)

Known holes the current contract leaves open on purpose, so v1 boundaries read as deliberate:

- **Provisioning.** A harness runs the tool; nothing yet provisions the user it runs as (the tool's
  config files, skills, MCP registration, auth). The sketched shape is a paired
  `harness-user-provisioner` capability under the same plugin; the fuller sketch is the
  session-harness SDD's "Target state: the harness as a tool adapter"
  (`docs/sdd/2026-07-07-session-harness/frd.md`). Until it exists, provisioning is install-commands
  plus operator setup, and auth in particular is out of harness scope.
- **Secrets and harness-owned environment.** The declare-and-receive secret plumbing is in place but
  unexercised (no shipped harness declares a secret). The session template's `env` chain, including
  secret-backed entries, is the supported way to put an env var (an API key, a tool config-dir
  override) into the pane today; what does not exist is a way for a harness to contribute env from
  its OWN config. No design record yet; a first-class surface is future work.
- **Liveness and headless ops.** The harness knows start/restart, not "is the workload healthy" and
  not a non-TTY exec mode. Both are plausible extensions of the op surface; no design record yet.

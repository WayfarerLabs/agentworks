# Session harnesses

> The detailed companion to the capability overview in [`../README.md`](../README.md), focused on
> the `harness` kind; the architectural record is
> [ADR 0020](../../../../docs/adrs/0020-session-harness.md). That overview covers the lifecycle,
> readiness stages, and secrets every capability shares. This page opens with what a harness is and
> does for an operator selecting one, and then, below the Technical overview divider, goes deep on
> what is specific to harnesses: the contract a new harness implements, how the session machinery
> consumes it, and the practices that made the shipped harnesses robust (session resume above all).
> Read the operator sections to choose a harness; read on past the divider when you want the
> specifics, whether you are implementing a new harness or you are just curious how the shipped ones
> work.

A **harness** decides what an agent session actually runs and how that workload is launched and
restarted. It is what lets the same `session` commands drive a plain shell, an interactive Claude
Code session, or another tool entirely, without you having to learn a different set of commands for
each: you choose a harness, and agentworks handles the tool-specific details of getting its workload
up and bringing it back.

You select a harness in a `session-template`: its harness block names the harness and carries
whatever settings that harness accepts (the operator-facing spelling is documented in
`docs/guides/resources.md`). Every session created from that template runs under the harness you
chose.

## The shipped harnesses

Three harnesses ship today:

- **`shell`** is a plain login shell, and the default. You hand it the command to run; it runs
  exactly that. Reach for it when the workload is just a script or an interactive shell and no
  tool-specific handling is needed.
- **`claude-code`** is an interactive Claude Code session, shipped as the opt-in `claude` system
  plugin. It knows how to launch Claude Code and, on restart, how to reattach to the conversation
  that was already going.
- **`codex`** is an interactive Codex session, shipped as the opt-in `codex` system plugin. Like
  `claude-code`, it launches the tool and, on restart, reattaches to the existing session instead of
  starting over.

## Session resume

For the tool harnesses, a restart is not a fresh start. `claude-code` and `codex` reattach to the
session that was already running rather than beginning a new one, so an interrupted or restarted
session picks up where it left off instead of losing its history. You do not configure this; it is
how the tool harnesses behave. The mechanism, and the rules a new stateful harness must follow to
get it right, are below the divider.

## Technical overview

Everything above this line is for operators. Everything below it is for engineers implementing or
extending a harness: where a harness sits in the capability model, the contract a new harness
implements, how the session machinery consumes it, and the practices that made the shipped harnesses
robust (session resume above all). If you are choosing and configuring a harness rather than writing
one, you can stop here.

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

### Where a harness sits

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

### The contract

A new harness implements this surface (see `base.py` for the full docstrings):

#### Class identity

`name` and `description` ClassVars (the registry row), inherited `owner_kind = "session-template"`
(error framing: config errors render as `session-template/<name>`).

#### `validate` (classmethod): shape and vocabulary only

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

#### `dependencies` (classmethod): total and pure

The resource references the config blob implies, secrets above all. Never raises (malformed fields
just omit their edge; `validate` owns the raising). Every shipped harness returns `()`; the plumbing
behind it is live and tested at the framework level: the session node exposes the harness's declared
references through its `config_secret_refs` (what the preflight sweep predicts resolvability over,
with owner/usage framing sourced to the session template) and derives its bare-name `secret_refs`
union from them, with values delivered through `ctx.secret(name)`. No shipped harness declares a
secret yet, so a secret-declaring harness should expect to be the first real exerciser of that path.

#### `merge_config` (classmethod): inheritance semantics, decided per field

When a child template names the SAME harness as its parent chain, the blobs merge through this hook.
The base default is shallow child-wins per key. Decide deliberately for every field you add:

- Scalars (a mode, a model) usually want child-wins: the default is right.
- **Accumulating lists usually want a union.** `shell` overrides `merge_config` to append-dedupe
  `required_commands`, so a child that overrides only `command` cannot silently drop the parent's
  probe list. Note the asymmetry to avoid copying blindly: `claude-code.extra_args` deliberately
  child-wins (an escape hatch is an override, not an accumulation). Whichever you choose, say so in
  the field's documentation.

The resolver calls your `merge_config` on every declared-blob fold, with `base={}` when the lineage
starts (a template with no parents included) or when a child switches to your harness from a
different one, so it must be sane with an empty base. A different harness's blob never lands in your
`base`: the resolver discards the accumulated config on a harness switch, so a parent's config
cannot leak across it.

#### Construction: cheap, no I/O

The base `__init__` binds `(owner_name, config)` and re-runs `validate`; the harness constructor
adds the session identity kwargs and the `state` blob. Nothing else: no probing, no network, no
minting. Anything that needs the world happens in readiness or ops.

#### Readiness: implement `_probe_target` only

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

#### Ops: `start` / `restart` return the raw pane command string

- The return value is a command string, not an execution: the session manager wraps it (template-var
  substitution, then the tmux pane's `$SHELL -lic 'cd <dir> && exec <command>'`) and runs it. Empty
  string means "just the login shell".
- `start` serves `session create`; `restart` serves `session restart`. The orchestrator kills the
  old workload BEFORE calling `restart`, a deliberate ordering guarantee: a stateful harness decides
  resume-vs-launch with the old process dead and its on-disk state settled.
- `start` and `restart` should be symmetric for a stateful harness (both call one shared decision
  method); the difference between them is caller-side.

#### `launch_note`: the operator-facing decision line

Return a one-line note about what the op decided (`claude-code`: resumed vs started fresh) and the
session manager prints it in the CLI op output. Default `None` keeps `shell` silent. Pair it with a
pane-visible echo (below) so the decision is visible in both places the operator looks.

#### `state`: the per-session persisted blob

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

### How the session machinery consumes a harness

The wiring, so you know what you get for free and where to look when debugging:

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
  context carries secrets. Do not design a harness that depends on runup firing before `restart`.
- **Substitution stays outside:** the returned string is passed through `{{session_name}}` /
  `{{workspace_name}}` substitution at the call site. Consequences for you are under "Building the
  pane command" below.
- **Display:** `session list` / `session describe` show the resolved harness name by re-resolving
  the template read-only (no instance is built, no gate runs); `resource list --kind harness` and
  `resource describe harness/<name>` show the registry row.

### Best practices

#### Session resume: the stateful-harness pattern

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
3. **Verify, empirically, that your probe boundary equals the tool's resume boundary.** The claude
   work ran a controlled experiment (sessions abandoned at every stage) to confirm transcript
   presence and Claude's own resume boundary are the SAME line, which is what makes both failure
   modes (blind resume of nothing; fresh launch colliding with a live id) impossible rather than
   merely rare. Do the equivalent for your tool before trusting a probe, and record the tool version
   you verified against (the latest-stable rule: exercise the real CLI, do not recall flags from
   memory; re-verify when the tool ships a major update).
4. **A probe that could not RUN is not a probe that found nothing.** Branch on the exit code
   trichotomy: 0 means resume, 1 means launch fresh, anything else (SSH failure, shell that could
   not start) RAISES a typed `StateError` rather than guessing. Guessing "fresh" launches with a
   reserved id the tool may reject as in-use, and the pane dies opaquely.
5. **Make the decision visible twice.** An `echo` as the pane's first line (the operator attaching
   sees which way it went) and `launch_note` (the operator running the CLI op sees it too).

#### Building the pane command

- **Compound commands need a single `sh -c`.** The pane wrapper is `... && exec <your string>`, and
  `exec` takes one simple command: with a bare `echo ...; exec tool ...` the shell would `exec` the
  `echo` and nothing after the `;` would ever run. Return `sh -c '<inner>'` with the inner
  `shlex.quote`d, and `exec` the tool inside it so the pane process becomes the tool.
- **`shlex.quote` every generated token.** Build an argv token list, quote each, and space-join.
  Never interpolate raw values into shell syntax.
- **Generated pieces must not emit `{{word}}`.** The call-site substitution raises on unknown
  `{{word}}` tokens and substitutes known ones, over your WHOLE returned string. Operator-authored
  slices (shell's commands, `extra_args` elements) carry `{{session_name}}` semantics on purpose;
  your code-generated skeleton must stay clear of doubled-brace words. Single braces (`${VAR}`,
  JSON) are untouched.
- **Model the common flags; leave the rest to `extra_args`.** A small optional vocabulary
  (`permission_mode`, `model`) plus a verbatim, appended-last `extra_args` list keeps the harness
  useful without chasing the tool's whole flag surface. Append `extra_args` after the managed flags
  so operators can override or extend.

#### Probing the launch target

Run probes as `"$SHELL" -lic '<inner>'` with `check=False`: login+interactive sources the same
dotfiles the pane's shell will (mise activation, PATH fragments), so the probe answers for the
environment the workload actually gets. Prefer shell-neutral inner commands (the transcript probe's
`find ... -print -quit | grep -q .` avoids bash/zsh divergence on unmatched globs).
`require_commands` does all of this for executable checks; reuse it rather than hand-rolling.

#### Testing a harness

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
- **Parity guards you will trip:** the harness-kind publisher set
  (`cli/tests/resources/test_harness_kind.py`), builtin-entries parity
  (`cli/tests/test_builtin_entries_parity.py`), and the plugin-framework adapter/kind drift guards.
  Update them deliberately; they exist to make additions loud.

#### Shipping a harness as a system plugin

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
nothing to regenerate unless you also add CLI surface; the rule still says look).

### Reserved directions (recorded, not built)

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

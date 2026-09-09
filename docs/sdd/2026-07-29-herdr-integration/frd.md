# Herdr console rendering: functional requirements

**Status:** Draft, gated on a spike and not scheduled (see Sequencing) **Repo:** `agentworks`
**Path:** `cli/agentworks/`

## Background

Named consoles (`agw console`) are Agentworks's answer to "watch many concurrent agent sessions at
once": a persistent, curated view over a VM's sessions, one window per session plus operator-chosen
extra shells. The model is sound and the membership is properly owned (console rows and their shell
layout live in the database, and `console attach` builds the view from them on first attach). The
_rendering_ is the weak part. It is tmux, so it inherits tmux's ergonomics: no mouse-first
interaction, no per-session status at a glance, awkward copy and paste, prefix collisions from
nesting a session's tmux inside the console's tmux, and nothing usable from a phone.

[Herdr](https://herdr.dev) is a terminal multiplexer built specifically for watching AI coding
agents: it renders panes with mouse-first interaction, detects each pane's agent state (working /
blocked / done / idle) and shows it in a live sidebar, sends notifications on state changes,
persists and restores layouts, and renders acceptably on a phone over SSH. It is a single Apache-2.0
Rust binary with no account and no hosted service, and it exposes its full control surface over a
local socket, so an external tool can drive it programmatically. It is, in other words, a good
implementation of exactly the layer Agentworks implements least well.

The two tools otherwise barely overlap. Herdr has no provisioning, no multi-host model, no identity
or isolation primitives, and no secret management (it explicitly punts credentials to the ambient
environment, and disables its own scrollback persistence by default because captured output can
contain secrets). Agentworks is all of those things. The integration opportunity is therefore narrow
and clean: **let herdr render what Agentworks already models.**

### Deployment shapes: a constrained VM-side server is plan A

Two deployment shapes exist, both in scope, and the spike evaluates both.

**Plan A: a rendering-only herdr server on the VM.** The herdr server runs on the VM as the admin
user, exactly where the console's tmux session runs today, and the operator reaches it the way they
reach a tmux console: SSH in and attach, or use herdr's native thin-client mode (`herdr --remote`),
which keeps the server remote while streaming the UI to a local client, bridging the local
clipboard, and maintaining its own connection settings. This shape has the tmux console's durability
profile: closing the laptop lid costs one client connection rather than one connection per pane, the
view exists whether or not the operator's machine does, and phone access works by reaching the VM
over Tailscale exactly as it does for tmux consoles today.

**Plan B, and the on-ramp: a workstation-side server.** Herdr runs on the operator's machine and
every pane runs an `agw` command over the platform's transports. This shape needs nothing installed
on any VM, which makes it the natural spike vehicle and manual-adoption path. Its cost is that the
cockpit lives and dies with the operator's machine: every wake from sleep reconnects one SSH
connection per pane (self-healing with resilient attach, but noisy at scale), and phone access
requires the workstation itself to be awake and reachable.

**An earlier ruling rejected the VM-side shape outright; it is superseded** (maintainer rulings
2026-07-29 and 2026-07-30). The original objections (a second control plane beside the database,
out-of-model admin work, an unauthenticated pane-creating socket as an escalation surface, an
unreviewed plugin ecosystem running as admin) conflated two different propositions. They are
decisive against an _unconstrained, general-purpose_ herdr on the VM, and that remains a non-goal.
They largely dissolve for a _rendering-only_ host under the constraints R7 makes requirements: no
plugins on the VM, the socket and state admin-owned with owner-only access, the version pinned and
installed through the platform's own tool manifests, and the database remaining the sole source of
truth per R2. Under those constraints the residual surface is symmetric with what the platform
already runs: the admin console tmux socket has the same filesystem-permission trust model, and
`console add-shell --admin` already provides admin shells. What is genuinely new is one more
admin-owned stateful process on the VM, which is the price of the durability, and the ruling is that
the durability is worth it.

### The shape

> **A named console is a membership specification; tmux and herdr are alternative renderings of
> it.**

Agentworks continues to own consoles: their membership, their shell layout, their lifecycle, their
persistence. Rendering becomes pluggable. The existing tmux rendering is unchanged and remains the
default. A herdr rendering materializes the same console by driving herdr's control surface, with
one pane per member session and one pane per companion shell, every pane running a command
Agentworks constructed from the database: `agw` commands in the workstation shape, and the same
attach-loop and shell-pane wrappers the tmux console builder produces today in the VM-side shape
(there is no `agw` on the VM). No Agentworks state moves into herdr in either shape, and an operator
who never enables a herdr rendering sees no change whatsoever.

Rendering is a live relationship, not a one-shot export. This follows the existing model rather than
inventing one: today's console mutations (`add-sessions`, `remove-sessions`, `reorder-sessions`,
`add-shell`) update the database and then best-effort sync a running tmux console if one exists
(`_live_best_effort` / `_live_target` in `sessions/multi_console/crud.py`), with `restore-session`
as the explicit repair path when a view has drifted. A herdr rendering is held to the same contract,
so the console command surface drives whichever rendering is live and neither backend is
second-class.

### Scope

In scope (functionally): a herdr rendering of named consoles in both deployment shapes, with the
constrained VM-side server as plan A; the operation set a rendering must support and its best-effort
live-sync semantics; the pane contract and its in-model requirement for both session panes and
companion shells; the first-class companion-shell command; resilient attach behavior; state
visibility; the VM-side constraint set; what Agentworks does and does not delegate to herdr;
graceful degradation when herdr is absent or incompatible; and the operator surface for choosing a
rendering.

Deferred to the HLA and later artifacts: whether the rendering seam becomes a formal capability kind
in the registry sense, the control-surface mechanics (socket protocol versus CLI invocation, and how
the CLI drives a VM-side server over the transport), layout and tab mapping detail, thin-client
versus plain-attach ergonomics, version detection and pinning specifics, the companion-shell
command's exact name and option spelling, and resolution of the open question below.

### Open question the HLA must resolve by spike

Herdr's screen-based state detection reads rendered pane content. In both deployment shapes a pane's
content is a nested tmux client attached to the session, so the harness's output reaches the screen
through an extra multiplexer, and **whether screen detection stays accurate through that nesting is
unverified.** Nesting itself is not a new risk (the tmux console already nests, and an outer herdr
avoids tmux-in-tmux prefix collisions entirely), but detection accuracy through it is.

The spike covers **both deployment shapes**: the workstation shape (panes carrying
`agw session attach` over SSH) because it is the zero-install way to test detection at all, and the
VM-side shape (server as admin beside the sessions, attached plain and via the thin client) because
it is plan A and additionally needs its ergonomics confirmed: attach and detach feel, thin-client
behavior on reconnect, and the operational footprint of the server under the R7 constraints.

For detection itself, the spike must evaluate three paths, in increasing order of both robustness
and effort, and the HLA picks one:

1. **Screen detection unaided.** Cheapest if it works. Claude Code sits in herdr's screen-manifest
   tier, so this is what happens with no effort on our side.
2. **Screen detection with a classification hint.** Herdr documents an environment variable by which
   a wrapped or sandboxed process still classifies correctly, which is precisely our situation. If
   this is what makes nested detection reliable, it is a one-line change to the pane command.
3. **Authoritative self-reporting.** Herdr exposes a reporting call by which a client tells it a
   pane's semantic agent state instead of herdr inferring it, and that reported state is what drives
   its sidebar, roll-ups, and notifications. Agentworks already knows a session's lifecycle state
   authoritatively, and the harness-transcripts effort makes it know considerably more. Reporting is
   therefore both the most robust option and the one that converges with R6's recorded future
   direction, at the cost of a live reporting path rather than a one-shot render.

**This materially lowers the effort's risk**: a negative result on path 1 no longer threatens the
effort, because paths 2 and 3 do not depend on rendered output at all. The FRD nonetheless keeps the
honesty rule from R6: whatever path is chosen must not present inferred state as authoritative, and
must show nothing rather than something wrong.

### Sequencing: this effort is gated, and part of it should ship without it

This FRD records a design that is worth having written down. It is deliberately **not scheduled**
(maintainer ruling, 2026-07-30), because the honest case for building it now is weaker than the case
for using herdr manually first and deciding later.

**Most of herdr's value needs nothing from this effort.** An operator can install herdr today, split
panes, and run `agw session attach <name>` in each one. That already yields the agent-state sidebar,
notifications when a session blocks, mouse-first interaction, better copy and paste, phone access
over SSH, and layout persistence, because all of those are herdr's own features and none of them
require Agentworks to know herdr exists. Herdr saves pane shape across restarts, so a hand-built
layout is built once per console rather than once per sitting.

**What this effort adds on top of manual use is therefore narrow**: building the layout
automatically from console membership instead of by hand, keeping a live view in sync as membership
changes, and rendering companion shells correctly. That is a real but modest margin, weighed against
a permanent obligation to keep two rendering backends at parity against a young project publishing
near-daily releases.

**Two requirements here are independently valuable and should ship separately, ahead of and
independent of any herdr work:**

- **R4's companion-shell command.** It is the one thing that genuinely blocks manual herdr use,
  since no existing command produces a session-scoped shell, and it is useful on its own terms to
  any operator wanting a correctly scoped shell beside a running session.
- **R5's resilient attach.** Self-healing reattach after a detach, network loss, or laptop sleep
  benefits anyone attaching a session by hand, with or without a multiplexer around it.

Neither needs the rendering backend, the capability seam, or herdr itself. Unbundling them is a
small change rather than an SDD-sized effort.

**The recommended sequence** is therefore: run the spike manually across both shapes (workstation
first because it is zero-install, then a hand-run server on one VM to confirm plan A's ergonomics
and detection); ship the companion-shell command and resilient attach as a small standalone change;
use the workstation shape manually against real work; and then decide whether the rendering backend
earns its keep, informed by what actually proved annoying.

**One asymmetry between the shapes bears on that decision.** The manual-use option is a property of
the workstation shape only: it is cheap precisely because every pane is a command the operator can
type. The VM-side shape is awkward to use by hand (its pane commands are socket-path tmux wrappers
no operator should be typing), so plan A is only really usable once the rendering backend builds it.
A passing spike therefore leads more directly to "build it" than the workstation-only framing
suggested, and the gate is the spike rather than a long manual-use trial.

**One factor could bring this forward.** The ephemeral-agents direction makes sessions much
shorter-lived, created and destroyed continuously rather than persisting for weeks. Hand-maintaining
a herdr layout against that rate of change would get painful quickly, which is precisely what the
rendering backend solves. That argues for revisiting this FRD once that direction lands and the real
churn pattern is visible, rather than for building ahead of it now.

## Terminology

- **Console**: an existing Agentworks named console, a persistent curated membership of sessions
  plus operator-chosen extra shells, owned in the database.
- **Rendering**: the concrete multiplexer view built from a console's membership. Today: tmux on the
  VM. Added here: herdr, as a constrained server on the VM (plan A) or on the operator workstation.
- **Deployment shape**: where the herdr server runs. VM-side (plan A): the server runs as the VM
  admin beside the sessions and the operator attaches over SSH, plain or via herdr's thin client.
  Workstation: the server runs on the operator's machine and panes reach sessions through `agw`
  commands.
- **Herdr workspace / pane**: herdr's own containers. A herdr workspace is its project-level view
  holding tabs and panes; a pane is one real terminal process. Note the false cognates: herdr's
  "workspace" is a view, not an Agentworks workspace, and herdr's "session" is its server instance,
  not an Agentworks session.
- **Herdr agent**: a state label herdr attaches to whatever process occupies a pane (working /
  blocked / done / idle / unknown). It is not a durable identity and has no relationship to an
  Agentworks agent.
- **Control surface**: herdr's local, unauthenticated-by-design command interface (its CLI and its
  Unix-domain-socket API) through which an external tool creates workspaces, splits panes, and reads
  state.

## Requirements

### R1: Consoles gain a herdr rendering

- An operator can open an existing named console as a herdr view, in either deployment shape, as an
  alternative to attaching its tmux rendering.
- The herdr view contains one pane per member session, ordered by the console's configured order,
  plus one pane per configured companion shell (R4), so the rendered view is recognizably the same
  console the tmux rendering produces.
- Rendering choice is per-invocation, not a property of the console: the same console can be
  attached as tmux now and opened in herdr later, with no migration and no state conversion.
- The tmux rendering's behavior is unchanged in every respect. It remains the default, and no
  existing command changes its meaning.

### R2: Agentworks owns the console; a rendering owns only pixels

- Console membership, shell layout, ordering, and lifecycle remain database-owned and are never read
  back from a rendering. Herdr holds no authoritative Agentworks state.
- The view is materialized from the console and reconciled toward it; it is never a source of truth.
  Divergence the operator introduces inside herdr (closing a pane, adding their own pane,
  rearranging tabs) is theirs to keep and is never written back to the console.
- **The console command surface drives the live rendering, whichever it is.** The mutations that
  already best-effort sync a running tmux console (`add-sessions`, `remove-sessions`,
  `reorder-sessions`, `add-shell`) do the same for a live herdr view, so an operator's normal
  workflow keeps working without reopening anything. The rendering operations a backend must
  therefore support are: build or rebuild a console's view, add and remove session panes, reorder
  them, add a companion shell pane, restore a session's configured panes, and tear the view down.
- **Live sync is best-effort and the database is authoritative**, exactly as today. A rendering that
  cannot be reached, has been closed, or has drifted is not an error: the mutation still lands in
  the database, the operator is told the sync did not apply, and reconciliation happens on the next
  build or on explicit repair. This is the answer to operators going off script, and it is the
  existing idiom rather than a new one.
- `restore-session` remains the explicit repair path for a view whose panes the operator killed, and
  behaves equivalently in either rendering.
- Whether this operation set is expressed as a formal capability kind in the registry (alongside
  `harness`, `vm-platform`, and the others) or as a narrower internal seam is an HLA decision. The
  functional requirement is that both renderings implement the same operations with the same
  best-effort semantics, so neither is second-class.

### R3: Every pane is built by Agentworks

- The invariant across both deployment shapes: every pane runs a command Agentworks constructed from
  the database at render or reconcile time. No pane embeds operator-maintained SSH invocations,
  hand-copied socket paths, or transport detail that can go stale, and no pane reaches a session by
  a route Agentworks did not construct.
- **Workstation shape**: session panes run an ordinary Agentworks session attach and companion
  shells run the command of R4. This makes the rendering transport-independent (Lima, WSL2, Azure,
  and Proxmox sessions render identically), gives every pane Agentworks's own preflight diagnostics
  instead of an opaque multiplexer failure, and means a rehomed or copied session keeps rendering
  correctly with no view to update.
- **VM-side shape**: there is no `agw` on the VM, so panes run the same attach-loop and shell-pane
  wrapper snippets the tmux console builder produces today, constructed by the CLI from the database
  and delivered when the view is built or reconciled. The correctness properties are preserved by
  reconciliation rather than per-spawn resolution: a rehome or copy moves the session to a different
  VM, and the console's rendering follows it the way a tmux console does today (the view is rebuilt
  from the database, not patched by hand).

### R4: Companion shells are first-class

Companion shells (the extra shell panes a console attaches to a session's window, whether requested
with the `+N` shorthand at console creation or added later with `console add-shell`) are a core part
of the console workflow, not a garnish. Rendering them outside tmux requires promoting them from a
console-internal pane construction to a real command.

- **A companion shell is a specific thing, and the definition is load-bearing.** Today
  `_split_shell_pane` builds a login shell that runs as the session's own Linux user (or as the
  admin for an admin-mode session or an explicitly admin-flagged shell), with its working directory
  at the session's workspace path or a configured subdirectory of it, carrying the session's fully
  resolved pane environment including secrets. The environment reaches the pane through
  console-internal, VM-side machinery: tmux environment flags, the sudoers `env_keep` fragment, and
  `sudo --preserve-env` with the documented fallback when a VM's sudoers does not permit it (ADR
  0017).
- **No existing command is equivalent, so the effort must add one.** `agent shell --workspace` gets
  the right user into the right workspace, which covers part of the need, but it is not
  session-scoped: it carries none of the session's resolved environment or secrets, has no notion of
  a working directory below the workspace root, and does not implement the automatic admin promotion
  that an admin-mode session's panes get. The effort therefore adds a first-class companion-shell
  command that takes a session, an optional relative working directory, and an optional admin flag,
  and delivers a login shell with the same user, directory, and resolved environment a console shell
  pane gets today. Reusing or extending the existing agent-shell implementation path to get there is
  a legitimate answer; the command's own surface is not.
- **The session is the primary argument, and this is functional rather than cosmetic.** Admin-mode
  sessions have no agent at all (`session list --admin` is documented as "only admin-mode sessions
  (no agent)"), so any spelling that makes an agent the primary noun and the session a modifier is
  unusable for exactly the sessions where an operator is most likely to want a companion shell.
  Everything the command needs (the Linux user, the workspace path, the environment scopes, the
  secret set) is derived from the session, so the session is the only argument that is always
  meaningful.
- The recommended spelling is `agw session shell <session> [--cwd <rel>] [--admin]`, which keeps the
  established `<noun> shell` family (`vm shell`, `agent shell`) and gives `--admin` the same meaning
  it has on `console add-shell` (force admin on an agent-mode session; admin-mode sessions promote
  automatically). "Companion shell" stays the prose term in documentation rather than becoming
  command vocabulary, since the code and CLI reference call these shell panes today. Final naming is
  an HLA confirmation, but the primary-noun constraint above is a requirement, not a preference.
- **The command must read as adjacent to the workload, not as the workload.** A companion shell is
  for looking around, running a build, or checking git state beside a running agent; it is never how
  an operator reaches the session's actual work. `session attach` carries that, and the contrast
  between the two commands is what teaches the distinction. Because a bare `shell` does not say so
  on its own, the framing is a documentation requirement rather than a naming one: the command's
  help text states that it opens a shell alongside a session's workload, and the CLI reference and
  top-level docs keep calling these companion shells, so the vocabulary that conveys "not the main
  workload" survives even though the command name stays inside the `<noun> shell` family.
- **Parity with the tmux rendering is the acceptance bar**, including the admin-versus-agent
  distinction, the automatic admin promotion for admin-mode sessions, working directories, and the
  environment and secret delivery. A companion shell that loses the session's environment is a
  regression, not a simplification.
- The command is independently valuable and is not scaffolding for this effort alone: it gives an
  operator a correctly scoped shell against a session without involving a console at all.
- **The VM-side rendering may reuse the existing pane machinery instead of the command.** On the VM
  the shell-pane construction of `_split_shell_pane` (or its refactored equivalent) is already
  correct and local; requiring it to shell out through a workstation command would be indirection
  for its own sake. The command remains required regardless, because the workstation shape and
  manual use have no other way to get a session-scoped shell.
- **Secret handling is unchanged.** Secrets continue to be resolved CLI-side on the operator's
  workstation and delivered to the pane over the transport, exactly as today. This effort adds no
  new secret path, no secret storage, and nothing secret-bearing inside herdr's own state (see R9).
- Where a companion shell cannot be rendered faithfully in a given rendering, the rendering reports
  it rather than silently substituting a lesser shell.

### R5: Panes are resilient

- A pane whose session is not yet running waits for it and attaches when it appears, rather than
  failing and leaving a dead pane. A pane whose attachment drops (detach, network loss, laptop
  sleep, session restart) re-attaches when the session is available again.
- A session that has genuinely ended leaves a pane that says so in place, preserving the last
  visible output for scrollback, and resumes waiting for a subsequent start.
- These are the semantics the console's tmux rendering already provides through its attach wrapper.
  The functional requirement is parity: a herdr-rendered console must be no more fragile than a
  tmux-rendered one. Whether parity is achieved by extending the session attach command itself or by
  a rendering-side wrapper is an HLA decision; extending the command is preferred, because the
  resilience is independently useful to any operator attaching a session by hand.

### R6: Session state is visible, and its provenance is honest

- The rendered view surfaces each session's state so an operator can see at a glance which sessions
  need attention. Where herdr's own detection provides this, the integration uses it rather than
  reimplementing it.
- **Provenance must be honest and is not always the same.** Under screen detection, displayed state
  is herdr's inference from rendered output and the documentation says so plainly. Under
  authoritative reporting (the open question's path 3), it is Agentworks's own signal and may be
  presented as such. What is never acceptable is inferred state presented as fact, or degraded
  detection presented as reliable: showing nothing beats showing wrong.
- Agentworks's own session status (running / stopped / broken) remains the authoritative lifecycle
  signal regardless of which path is chosen, and is unaffected by this effort.
- **Reporting is the convergence point with the transcripts effort.** Herdr accepts a
  client-supplied semantic agent state for a pane, and that reported state drives its sidebar,
  roll-ups, and notifications in place of inference. The harness-transcripts effort
  (`docs/sdd/2026-07-29-harness-transcripts`) gives Agentworks a harness-owned event stream that
  knows a session's state authoritatively. Whether this effort implements reporting now (as the
  spike may recommend) or leaves it as the recorded next step, neither design may foreclose it.

### R7: The VM footprint is scoped, and its constraints are requirements

- **In the workstation shape, nothing on the VM changes.** No component is installed on, provisioned
  into, or run on any VM; no herdr server, socket, plugin, or configuration exists inside any VM's
  trust domain.
- **In the VM-side shape, the footprint is exactly one constrained addition**: the herdr binary and
  its server process, running as the VM admin, present only on VMs where the operator has enabled
  the rendering. The constraints below are what made the shape acceptable (see the deployment-shapes
  ruling), so they are requirements, not recommendations:
  - The herdr version is pinned and installed through the platform's own tool-provisioning
    manifests, so every VM runs a version the integration declares support for and upgrades happen
    through `reinit` like any other tool.
  - No herdr plugins are installed on a VM, and the rendering never requires one. The VM-side server
    is a rendering host, not an extension platform.
  - The server's socket and state directory are admin-owned with owner-only access. An agent user
    must have no read or write path to either; this is the same trust stance the admin console tmux
    socket carries today, and it is what keeps the socket from being an agent-to-admin escalation
    surface.
  - Herdr's automation surface on the VM is used only by Agentworks's own rendering path. Nothing
    exposes it to agent users or to out-of-band tooling.
- Sessions remain tmux-hosted in both shapes, as the harness model requires. This effort does not
  touch the session substrate, session lifecycle, or the isolation model.

### R8: Herdr is optional and its absence is uneventful

- Agentworks remains fully functional with herdr absent. No command's default behavior depends on
  it, and no health check treats its absence as a problem.
- Requesting a herdr rendering without herdr installed produces a clear, actionable message naming
  what is missing and how to install it, in the manner of the platform's existing prerequisite
  handling (the vm-site pattern of self-disabling when host prerequisites are unmet is the model to
  follow).
- Herdr's version is verified before use, with a supported range the integration states explicitly.
  An unsupported version fails clearly rather than producing a half-built view. This matters more
  than usual: herdr is a young, fast-moving project publishing near-daily builds, so the integration
  must treat its control surface as a moving target.
- The integration surface stays deliberately thin, so that herdr becoming unmaintained, changing its
  interface, or being abandoned by the operator costs one rendering and nothing else. The tmux
  rendering must remain a complete, first-class path indefinitely, never a legacy fallback.

### R9: Secrets and captured output

- No credential material flows through the integration. Herdr has no secret management, and this
  effort gives it none: panes inherit whatever environment an ordinary interactive Agentworks attach
  inherits, and nothing is written into herdr's configuration or state.
- Herdr's optional scrollback persistence captures pane content to disk on whichever host runs the
  server, and is therefore secret-bearing. The integration does not enable it in either shape, and
  the documentation states the exposure so an operator who turns it on does so knowingly. The
  VM-side shape raises the stakes: persisted scrollback there would be an on-VM file of everything
  every rendered session printed, so the off default is part of R7's constraint set in spirit.
- Transcripts (the harness-transcripts effort) remain the platform's record of session activity.
  Nothing in a herdr view is treated as a record, and no view content is collected or retained by
  Agentworks.

### R10: Operator surface

- The herdr rendering is reachable through the console command surface, consistent with existing
  conventions and with shell completions covering any new names or options, per the completions
  rule.
- The command reports what it did: which console was rendered, how many session panes were created,
  and anything it skipped or could not render, in keeping with the platform's preference for visible
  rather than silent behavior.
- Operators are never required to interact with herdr's own CLI or socket to use the feature, though
  nothing prevents them from doing so on their own machine.

### R11: Documentation and decision record

- `cli/README.md` documents the herdr rendering alongside the console command reference, including
  the prerequisite, the supported version range, the state-provenance caveat from R6, and the
  scrollback caveat from R9.
- The top-level `README.md` console material notes that consoles can be rendered by more than one
  multiplexer, without turning herdr into a required part of the model narrative.
- The decision to treat rendering as pluggable, and specifically the deployment-shapes ruling (the
  constrained VM-side server as plan A, what "constrained" requires, and why the earlier
  workstation-only ruling was superseded), is captured as an ADR, drafted in this feature directory
  and numbered into `docs/adrs/` at the end of the effort.
- If the effort adds operator-configurable settings, `cli/agentworks/sample-config.toml` gains them
  with comments, per the sample-config rule.
- Per the SDD lifecycle rules, each doc change rides the commits that make its claims true, and
  nothing permanent cites this SDD's path.

## Non-goals

- **An unconstrained herdr on any VM.** Plan A runs herdr on the VM strictly as a rendering host
  under R7's constraint set. What stays out, and would need a new effort and a materially different
  herdr (multi-user awareness, an authenticated control surface): herdr plugins on a VM, the VM-side
  socket or automation surface exposed to anything beyond Agentworks's own rendering path, VM-side
  herdr as a general-purpose multiplexer for out-of-model work, or herdr state treated as
  authoritative anywhere.
- **Replacing tmux for sessions.** Sessions stay tmux-hosted in every shape, and the tmux console
  rendering stays first-class and default.
- **Cross-VM consoles.** The workstation shape makes them reachable (a workstation herdr view is not
  VM-bound the way a VM-hosted rendering is), but consoles are VM-scoped today by their own model,
  and relaxing that is its own effort with its own questions about membership and naming.
- **An Agentworks herdr plugin.** Publishing a plugin so herdr can discover and launch Agentworks
  sessions from its own side is a plausible follow-up, including as a way to reach herdr's user
  base, but it inverts the direction of control and belongs to a separate effort.
- **Adopting herdr's plugin ecosystem.** No community plugin is required, recommended, or vetted by
  this effort.
- **Herdr's native agent resume.** Herdr can store a tool session reference and re-invoke the tool's
  own resume flag on restore. That is wrong here: resume is the harness's job on the VM, and a
  workstation-side resume attempt would target the wrong machine. The integration deliberately does
  not use it.
- **Agentworks-side state detection.** Surfacing per-session harness state in `session list` and
  `session describe` is independently valuable and does not need herdr, but it belongs with the
  harness and transcript work (R6's future direction), not here.
- **Rendering anything other than consoles.** Ad-hoc herdr views over arbitrary session selections,
  workspaces, or VMs are out of scope; the console is the membership model this effort renders.
- **Any hosted, multi-user, or team-facing capability.** Herdr is single-operator and local, and so
  is this integration.
- **Herdr's agent-automation layer**, which is a deliberate exclusion rather than an absent feature
  and therefore gets its reasoning recorded. Herdr ships a designed set of primitives for one
  workload to drive another through its terminal: submit a prompt to a named agent, block until it
  reaches a lifecycle state, read its output back, send key chords, wait on pane output by pattern,
  and subscribe to another pane's state changes. It ships a skill file whose stated purpose is
  teaching a coding agent to delegate to and supervise sibling agents this way, and its
  documentation frames one agent creating work for others as a supported use case. Under this
  effort's design those primitives would technically function against a rendered session pane, since
  typing into the pane reaches the attached harness. They are excluded for two reasons. First,
  everything they carry is terminal-mediated: the channel is a TTY, the protocol is typing and
  screen-reading, and the delivery guarantee is whatever the rendered screen happens to show, which
  is not a foundation Agentworks should put workload coordination on. Second, the direction of
  control is wrong for the platform: programmatic input to a session is properly a harness concern
  (the component that knows the tool it runs), and session-to-session coordination, if Agentworks
  ever wants it, is a platform capability with its own record in the transcript, not an operator's
  multiplexer typing into a terminal out of band. If this capability is wanted, it should be
  designed as an Agentworks feature and not inherited as a side effect of a rendering choice.
- **Herdr's git worktree feature**, in which each worktree becomes its own herdr workspace with its
  own panes. Agentworks already models multiple independent workspace clones of one repository, so
  this overlaps an owned concept rather than filling a gap, and adopting it would put repository
  layout decisions inside the rendering layer.
- **Herdr's layout export and apply**, which serialize and recreate a tab's pane tree. A console is
  already Agentworks's declarative view specification, so a second, herdr-owned layout format
  competing with it is exactly the ownership split R2 exists to prevent. The rendering may use these
  calls internally as an implementation detail; what is out of scope is exposing them as an operator
  surface or treating an exported layout as a source of truth.

## Migration notes

The feature is purely additive. No existing command changes behavior and no console data is
migrated. A VM is touched only when the operator enables the VM-side rendering for it, and then only
through the ordinary provisioning path (a pinned herdr tool manifest applied at `reinit`). Operators
who never enable a herdr rendering see nothing new beyond documentation. A console created before
this effort renders in herdr with no preparation, because the rendering is built from console
membership that already exists.

# Herdr console rendering: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

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

### Why the operator workstation, and not the VM

The tempting alternative is to run a herdr server on the VM as the admin user, replacing the
console's tmux outright. The console already runs as a single user (the admin, reaching each session
through its tmux socket and entering agent users with `sudo --login`), so the isolation model does
not forbid it, and it would make the view durable against laptop sleep in exactly the way tmux
consoles are today.

It is nevertheless the wrong trade (maintainer ruling, 2026-07-29), for reasons that are about
platform coherence rather than mechanics:

- **It creates a second control plane on the VM that Agentworks does not own.** The database is the
  single source of truth for what exists; a herdr server would hold its own workspace, tab, and pane
  state beside it, with its own CLI, socket, and plugin system.
- **It invites work outside the model.** A general-purpose multiplexer running as admin makes bare
  admin shells the path of least resistance, so activity happens outside any session, workspace, or
  agent record: no isolation, no lifecycle, no transcript.
- **It adds a privilege surface.** Herdr's socket is authenticated only by filesystem permissions,
  and anything that can write to it can open a pane as its owner. An admin-owned socket on a VM
  hosting agent users is an escalation path that has to be defended for no functional gain.
- **It moves an unreviewed plugin ecosystem onto the VM.** Herdr plugins are arbitrary executables
  from auto-indexed GitHub repositories; running them as admin on a VM that hosts agents is a
  supply-chain exposure the platform should not take on.

On the operator's workstation none of that applies. Herdr is the operator's own tool on the
operator's own machine, every pane runs an `agw` command, the only route to a VM is through
Agentworks, and the blast radius of operator improvisation is a machine that is already entirely
theirs. The durability difference is smaller than it first appears: the work never lived in the view
(sessions are tmux on the VM and survive regardless), and a resilient attach that waits for a
session and re-attaches on drop, the pattern the console's own attach wrapper already implements,
lets a workstation pane self-heal after a sleep or a network blip.

### The shape

> **A named console is a membership specification; tmux and herdr are alternative renderings of
> it.**

Agentworks continues to own consoles: their membership, their shell layout, their lifecycle, their
persistence. Rendering becomes pluggable. The existing tmux rendering is unchanged and remains the
default. A herdr rendering materializes the same console on the operator's workstation by driving
herdr's local control surface, with one pane per member session, each pane running an ordinary
`agw session attach`. Nothing is installed on the VM, no Agentworks state moves into herdr, and an
operator with no herdr installed sees no change whatsoever.

### Scope

In scope (functionally): a herdr rendering of named consoles, the pane-per-session contract and its
in-model requirement, resilient attach behavior, state visibility, what Agentworks does and does not
delegate to herdr, graceful degradation when herdr is absent or incompatible, and the operator
surface for choosing a rendering.

Deferred to the HLA and later artifacts: whether rendering becomes a formal capability kind, the
control-surface mechanics (socket protocol versus CLI invocation), layout-mapping detail, version
detection and pinning specifics, and resolution of the open question below.

### Open question the HLA must resolve by spike

Herdr's state detection reads rendered pane content. In this design a pane's content is a nested
tmux client attached to the session, so the harness's output reaches the screen through an extra
multiplexer. **Whether herdr's agent-state detection remains accurate through that nesting is
unverified, and it is the integration's primary value-add.** The HLA must resolve it with a spike
before the plan commits, and must record the fallback honestly: if detection does not survive
nesting, the remaining benefits (mouse interaction, layout persistence, notifications, phone access)
are real but materially smaller, and the effort should be re-scoped or dropped rather than shipped
on an assumption. Nesting itself is not a new risk (the tmux console already nests, and an outer
herdr avoids tmux-in-tmux prefix collisions entirely), but detection accuracy through it is.

## Terminology

- **Console**: an existing Agentworks named console, a persistent curated membership of sessions
  plus operator-chosen extra shells, owned in the database.
- **Rendering**: the concrete multiplexer view built from a console's membership. Today: tmux on the
  VM. Added here: herdr on the operator workstation.
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

- An operator can open an existing named console as a herdr view on their workstation, as an
  alternative to attaching its tmux rendering.
- The herdr view contains one pane per member session, ordered by the console's configured order,
  plus the console's extra shells, so the rendered view is recognizably the same console the tmux
  rendering produces.
- Rendering choice is per-invocation, not a property of the console: the same console can be
  attached as tmux now and opened in herdr later, with no migration and no state conversion.
- The tmux rendering's behavior is unchanged in every respect. It remains the default, and no
  existing command changes its meaning.

### R2: Agentworks owns the console; herdr owns only pixels

- Console membership, shell layout, ordering, and lifecycle remain database-owned and are never read
  back from herdr. Herdr holds no authoritative Agentworks state.
- The herdr view is materialized from the console, not synchronized with it. Divergence introduced
  by the operator inside herdr (closing a pane, adding their own pane, rearranging tabs) is theirs
  to keep and is never written back to the console.
- Re-opening a console in herdr reconciles the view toward the console's current membership. What
  reconciliation does with operator-introduced divergence is an HLA decision, but it must be
  predictable and must never silently discard operator work in a pane that is still running.
- Console commands (`create`, `add-sessions`, `remove-sessions`, `reorder-sessions`, `add-shell`,
  `restore-session`, `delete`) keep operating on the console, not on any rendering. A herdr view
  open at the time is stale until reconciled, and that is acceptable and documented.

### R3: Every pane is an Agentworks command

- Session panes run an ordinary Agentworks session attach. They do not run raw SSH, do not address
  tmux sockets directly, and do not encode VM users, socket paths, or transport detail.
- This is what keeps the integration in-model, and it is a requirement rather than an implementation
  preference: it makes the rendering transport-independent (Lima, WSL2, Azure, and Proxmox sessions
  render identically), keeps the database authoritative for where a session actually lives, gives
  every pane Agentworks's own preflight diagnostics instead of an opaque multiplexer failure, and
  ensures a rehomed or copied session keeps rendering correctly with no view to update.
- Extra shells render through the equivalent in-model command for the console's shell entries,
  preserving the existing agent-versus-admin distinction those entries carry.
- No requirement here is satisfied by a pane that reaches a VM by any route other than Agentworks.

### R4: Panes are resilient

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

### R5: Session state is visible, and its provenance is honest

- The rendered view surfaces each session's state so an operator can see at a glance which sessions
  need attention. Where herdr's own detection provides this, the integration uses it rather than
  reimplementing it.
- State shown in the view is understood to be herdr's inference from rendered output, not an
  Agentworks-authoritative fact, and documentation says so plainly. Agentworks's own session status
  (running / stopped / broken) remains the authoritative lifecycle signal and is unaffected.
- Subject to the open question above: if detection does not survive nesting accurately, the
  integration must not present degraded or misleading state as though it were reliable. Showing
  nothing is better than showing wrong.
- **Future direction, not built here.** Herdr supports a deeper integration tier in which a tool
  reports its own lifecycle rather than being screen-scraped. The harness-transcripts effort
  (`docs/sdd/2026-07-29-harness-transcripts`) gives Agentworks a harness-owned event stream that
  already knows a session's state authoritatively. Feeding that stream into the view, so state comes
  from the harness rather than from inference, is the natural convergence of the two efforts and is
  recorded here so neither design forecloses it.

### R6: Nothing on the VM changes

- No component is installed on, provisioned into, or run on any VM by this effort. Herdr is a
  workstation-side tool only.
- No herdr server, socket, plugin, or configuration exists inside any VM's trust domain, so the
  integration adds no privilege surface reachable by an agent user and no supply-chain exposure on
  the VM.
- Sessions remain tmux-hosted, as the harness model requires. This effort does not touch the session
  substrate, session lifecycle, or the isolation model.

### R7: Herdr is optional and its absence is uneventful

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

### R8: Secrets and captured output

- No credential material flows through the integration. Herdr has no secret management, and this
  effort gives it none: panes inherit whatever environment an ordinary interactive Agentworks attach
  inherits, and nothing is written into herdr's configuration or state.
- Herdr's optional scrollback persistence captures pane content to disk on the workstation and is
  therefore secret-bearing. The integration does not enable it, and the documentation states the
  exposure so an operator who turns it on does so knowingly.
- Transcripts (the harness-transcripts effort) remain the platform's record of session activity.
  Nothing in a herdr view is treated as a record, and no view content is collected or retained by
  Agentworks.

### R9: Operator surface

- The herdr rendering is reachable through the console command surface, consistent with existing
  conventions and with shell completions covering any new names or options, per the completions
  rule.
- The command reports what it did: which console was rendered, how many session panes were created,
  and anything it skipped or could not render, in keeping with the platform's preference for visible
  rather than silent behavior.
- Operators are never required to interact with herdr's own CLI or socket to use the feature, though
  nothing prevents them from doing so on their own machine.

### R10: Documentation and decision record

- `cli/README.md` documents the herdr rendering alongside the console command reference, including
  the prerequisite, the supported version range, the state-provenance caveat from R5, and the
  scrollback caveat from R8.
- The top-level `README.md` console material notes that consoles can be rendered by more than one
  multiplexer, without turning herdr into a required part of the model narrative.
- The decision to treat rendering as pluggable, and specifically the workstation-versus-VM ruling
  with its reasoning, is captured as an ADR, drafted in this feature directory and numbered into
  `docs/adrs/` at the end of the effort.
- If the effort adds operator-configurable settings, `cli/agentworks/sample-config.toml` gains them
  with comments, per the sample-config rule.
- Per the SDD lifecycle rules, each doc change rides the commits that make its claims true, and
  nothing permanent cites this SDD's path.

## Non-goals

- **Running herdr on any VM**, as a console backend, a session substrate, or anything else. The
  ruling and its reasoning are recorded in the Background; revisiting it would need a new effort and
  a materially different herdr (multi-user awareness and an authenticated control surface at
  minimum).
- **Replacing tmux anywhere.** Sessions stay tmux-hosted and the tmux console rendering stays
  first-class and default.
- **Cross-VM consoles.** A workstation-side rendering makes them reachable (a herdr view is not
  VM-bound the way a VM-hosted tmux console is), but consoles are VM-scoped today by their own
  model, and relaxing that is its own effort with its own questions about membership and naming.
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
  harness and transcript work (R5's future direction), not here.
- **Rendering anything other than consoles.** Ad-hoc herdr views over arbitrary session selections,
  workspaces, or VMs are out of scope; the console is the membership model this effort renders.
- **Any hosted, multi-user, or team-facing capability.** Herdr is single-operator and local, and so
  is this integration.

## Migration notes

The feature is purely additive. No existing command changes behavior, no console data is migrated,
no VM is touched, and operators who never install herdr see nothing new beyond documentation. A
console created before this effort renders in herdr with no preparation, because the rendering is
built from console membership that already exists.

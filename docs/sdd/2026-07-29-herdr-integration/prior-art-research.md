# Prior art research: herdr and the console rendering question

**Status:** Draft **Research dates:** 2026-07-29 **Researcher:** delegated web research plus in-repo
verification

## Executive summary

This effort began as an open evaluation of Agentworks against [herdr](https://herdr.dev), with three
outcomes on the table: switch to herdr, copy its missing features, or integrate. Research supports
**integrate**, and specifically integrate narrowly, because the two tools overlap in exactly one
place and it is the place Agentworks is weakest.

Herdr is a terminal-native multiplexer purpose-built for watching AI coding agents: mouse-first
panes, per-agent state detection in a live sidebar, layout persistence, notifications, and a
phone-usable TUI over SSH, shipped as a single Apache-2.0 Rust binary with a local control surface.
It has no provisioning, no multi-host model, no identity or isolation primitives, and no secret
management. Agentworks is all of those and renders its multi-session view with tmux. The overlap is
the view; everything else is complementary.

Two research findings changed the design materially. First, herdr's "agent" is a state label on a
live pane process, not a durable identity, so there is no collision with the Agentworks agent model
and no temptation to map one onto the other. Second, herdr's control surface is unauthenticated by
design and its plugin ecosystem is unreviewed arbitrary executables, which is tolerable on an
operator's own workstation and not tolerable as admin on a VM hosting agent users. Together these
pushed the design to workstation-side rendering with Agentworks retaining ownership of everything
stateful.

One question research could not settle, and which the HLA must resolve by spike: whether herdr's
state detection stays accurate when a pane holds a nested tmux client rather than the agent process
directly. It is the integration's primary value-add, so it is the one assumption the design refuses
to make on paper.

## Findings by dimension

### Identity and maturity

- Herdr is `github.com/herdrdev/herdr`, Apache-2.0, written in Rust, repository created 2026-03-27
  (roughly four months old at the time of research). Verified via the GitHub API on 2026-07-29:
  22,375 stars, 1,520 forks, ~55 contributors, 125 open issues, latest tagged release v0.7.5
  (2026-07-21), with preview builds published close to daily.
- Maintained by a single individual (ogulcancelik) working on it full time, funded by sponsorship
  rather than venture capital. No company entity found.
- Reached #1 on GitHub Trending for Rust and #17 overall in late June 2026; discussed on Hacker News
  with broadly positive sentiment from users, and skepticism centered on whether most developers run
  enough parallel agents to need dedicated tooling.
- **Design consequence:** the two risks that matter are bus factor and interface churn. Both are
  addressed by R7's thin-surface requirement and by keeping the tmux rendering permanently
  first-class rather than a fallback. Apache-2.0 means a fork is possible if the project stalls, and
  the near-daily release cadence is why an explicit supported version range is a requirement rather
  than a nicety.

### Product shape and positioning

- Herdr is a local tool, not a hosted service: a background server process plus a client TUI, no
  account, no telemetry, installable via curl script, Homebrew, Nix, mise, PowerShell, or release
  binary. There is no cloud offering and no team or multi-user capability; it reads throughout as a
  single-operator tool.
- Its own positioning is "one terminal for the whole herd", i.e. tmux with awareness of agent state.
  Supported agents are tiered by integration depth: some tools get full lifecycle integration via
  hooks, a middle tier (including Claude Code and Codex) is detected via screen manifests, and a
  third tier is detected but less tested. Any other process runs fine in a pane without state
  tracking.
- **Design consequence:** confirms the integration direction. A local single-operator tool composes
  naturally with a local single-operator CLI, and nothing in herdr's positioning suggests it will
  grow into the provisioning or isolation space Agentworks occupies. Claude Code sitting in the
  screen-manifest tier rather than the hooks tier is precisely what makes the nesting question
  (below) load-bearing.

### Domain model

- Hierarchy: server, then session (a named socket instance, what an operator detaches from and
  reattaches to), then workspace (a project-level container of tabs), then tab, then pane (a real
  terminal process), then optionally one agent per pane.
- A herdr **agent is not an identity**. It is a state label (working, blocked, done, idle, unknown)
  attached to whatever process currently occupies a pane, derived from hooks for integrated tools or
  from rendered screen content otherwise. Nothing accumulates across workloads; there is no entity
  holding credentials, memory, or history.
- Herdr's "native agent resume" stores a tool-supplied session reference string in a pane's saved
  shape and, on restore, re-invokes the tool's own resume flag against it. Conversation state lives
  entirely in the tool's own storage. A missing, stale, or duplicated reference degrades to a plain
  shell in the saved directory.
- What survives a server restart: pane shape (working directory, layout, focus) and any stored
  resume references. What does not: the running processes themselves. Pane history replay exists but
  is disabled by default, explicitly because captured output can contain secrets and tokens.
- **Design consequence:** three requirements come straight from this. There is no mapping between a
  herdr agent and an Agentworks agent, so R2 keeps all authoritative state on the Agentworks side
  rather than attempting synchronization. Native resume is explicitly rejected as a non-goal,
  because resume belongs to the harness on the VM and a workstation-side resume would target the
  wrong machine. And herdr's own caution about scrollback capture is adopted directly as R8.

### Extensibility and control surface

- Two equivalent control paths: the full CLI (workspace, pane, agent, and layout commands, including
  a wait-until-state command), and a local socket API speaking newline-delimited JSON over a Unix
  domain socket or Windows named pipe, exposing workspace, tab, pane, agent, and layout operations
  plus event subscriptions and notification dispatch. Herdr's own documentation describes the CLI as
  being the plugin API.
- **The socket has no authentication.** Access is gated purely by filesystem permissions; it is
  designed for local or SSH-tunneled use and is not a remote API. Anything able to write to it can
  create panes, which is to say execute commands as the socket's owner.
- Plugins are decentralized and unreviewed: any public GitHub repository carrying a manifest file
  and a topic tag is auto-indexed with no submission or review process (416 such repositories at the
  time of research). A plugin is an arbitrary executable in any language, invoked by herdr with
  context injected through environment variables. Herdr explicitly disclaims responsibility for
  plugin trustworthiness.
- **Design consequence:** this is the finding that decided workstation-versus-VM. An unauthenticated
  pane-creating socket owned by the VM admin, on a host whose entire purpose is running semi-trusted
  agent users, is an escalation path with no functional payoff; and an unreviewed plugin ecosystem
  running as admin on that host is a supply-chain exposure the platform should not adopt. On the
  operator's own workstation both concerns are bounded by a trust domain the operator already fully
  owns. It also confirms the integration is mechanically straightforward, since a documented local
  control surface is exactly what a renderer needs.

### Verified Agentworks-side facts

Established by reading the repository rather than from research, and load-bearing for the design:

- `agw session attach` resolves the session from the database, prepares the VM transport, verifies
  the session is genuinely running (erroring distinctly on stopped and broken states), then execs an
  interactive tmux attach against the session's own socket path
  (`cli/agentworks/sessions/manager/_queries.py:499`). This is what makes R3's pane contract
  transport-independent and self-diagnosing.
- The console's tmux rendering already nests multiplexers: its per-window wrapper unsets `TMUX` and
  attaches to the session's tmux socket from inside the console's own tmux session
  (`cli/agentworks/sessions/multi_console/attach.py:67`). Nesting is therefore the established
  model, not a new risk this effort introduces. An outer herdr additionally avoids the prefix
  collision inherent to tmux-inside-tmux, since its keybindings are not tmux's.
- The same wrapper already implements the resilience R4 requires as parity: wait for a session that
  is not yet up, re-attach silently on detach, and report a genuine session end in place while
  preserving scrollback.
- The console runs as the VM admin and enters agent users via `sudo --login` for agent-scoped shell
  panes (`cli/agentworks/sessions/multi_console/tmux_build.py`), which is why a VM-side herdr server
  would not have violated the isolation model. The ruling against it rests on control-plane
  coherence and privilege surface, not on isolation.
- Consoles are VM-scoped by their own model: `console create` errors when the requested sessions
  span more than one VM and asks the operator to pick one (`cli/README.md`). This is why cross-VM
  rendering is called out as reachable-but-deferred rather than delivered.
- The rendering seam is already half-implied by the code's own structure: the console builder is
  named `tmux_build.py`, distinct from the console's model and CRUD modules.

## Refuted or do-not-rely-on

- **"herdr.org is the same product."** A site at herdr.org presents itself as an "AI Agent
  Orchestration Platform" with a generic control-plane pitch, no attributable company or authorship,
  and round demo metrics. It appears unrelated to herdr.dev and should not be treated as a source
  about it.
- **"Herdr is freemium with a paid tier."** One third-party review site claimed this. It contradicts
  the official site, the repository license, and every other source, and the same review listed
  alternative products that do not appear to exist. Herdr is free and Apache-2.0; treat that review
  as unreliable.
- **"Herdr offers agent orchestration"** in the task-routing, work-queue, or supervisory-automation
  sense. It does not, and the marketing-adjacent framing found on aggregator sites overstates it.
  Herdr multiplexes and observes; it does not coordinate.
- **"Herdr could manage secrets for launched agents."** It has no credential store and no secret
  manager integration; its configuration covers terminal, keys, theme, UI, and session behavior
  only. Its sole acknowledgement of secrets is defensive (scrollback persistence off by default).
- **"Herdr's plugin marketplace is curated."** It is not; discovery is a GitHub topic search with no
  review step, as herdr's own documentation states.

## Open questions research did not resolve

1. **Does herdr's state detection survive nested tmux?** The decisive question, recorded as the
   FRD's open question and assigned to an HLA spike. Claude Code is in herdr's screen-manifest tier,
   so detection depends on rendered output; a nested tmux client still renders the harness's output,
   but status lines and redraw behavior may interfere. Unverified either way.
2. **How does herdr behave when a pane's process is a long-lived SSH connection that drops and
   reconnects?** Relevant to R4's resilience parity, and cheap to fold into the same spike.
3. **What is herdr's actual compatibility policy across its near-daily releases?** No documented
   stability guarantee for the socket API was found, which is why R7 requires an explicitly stated
   supported version range rather than a floor.
4. **Would the hooks-tier integration path accept state reported by a harness on a different host?**
   Relevant only to R5's recorded future direction (harness-reported state via transcripts), not to
   this effort.

## Sources

| Source                                                                                                                | Quality                                         | Angle                                                      |
| --------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- | ---------------------------------------------------------- |
| [herdr.dev](https://herdr.dev/) and its docs (quick-start, agents, socket-api, plugins, session-state, configuration) | Primary, authoritative for product behavior     | Feature set, domain model, control surface, secrets stance |
| [github.com/herdrdev/herdr](https://github.com/herdrdev/herdr) plus GitHub API queries, 2026-07-29                    | Primary, authoritative for license and traction | License, age, stars, contributors, release cadence         |
| Raw plugin manifests from two marketplace plugins, plus a GitHub topic search for the plugin tag                      | Primary, concrete                               | Plugin mechanics and ecosystem size                        |
| [Hacker News discussion](https://news.ycombinator.com/item?id=48714802)                                               | Secondary, user sentiment                       | Real-user praise and skepticism, feature gaps requested    |
| Better Stack community writeup and assorted smaller blogs and aggregators                                             | Secondary, promotional in places                | Traction claims, trending position                         |
| stork.ai review                                                                                                       | Low quality, treat as refuted                   | Source of the incorrect freemium claim                     |
| herdr.org                                                                                                             | Unrelated, do not cite                          | Distinct site, see refuted section                         |
| Agentworks repository at the commit this effort branched from                                                         | Primary, authoritative                          | All Agentworks-side facts above                            |

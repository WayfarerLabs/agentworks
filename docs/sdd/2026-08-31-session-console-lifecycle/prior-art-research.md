# Prior-Art Research: Session and Console Lifecycle

- Status: Draft for design review
- Date: 2026-08-31
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)

## Executive Summary

Established runtime tools distinguish durable creation, starting a stopped runtime, restarting a
runtime, and attaching to one that already exists. They also distinguish a resource lifecycle verb
from application-specific continuation. This supports Agentworks adopting start/restart/attach while
keeping harness resume behavior behind the integration boundary.

The current code already contains the persistence seams required for forced-fresh behavior. Claude
and Grok use Agentworks-owned UUID bindings; Codex uses a tool-assigned ID recorded after launch.
The design can rotate known bindings safely without deleting external conversations, while honestly
accepting that a tool-assigned ID may be unavailable after an early launch failure.

## Runtime Lifecycle Vocabulary

### systemd

The systemd `systemctl` reference defines `restart` as restarting named units and starting them when
they are not already running. It separately defines `try-restart` as acting only on running units.
The important precedent is that the verb, not a mode flag, owns whether a running process is
replaced.

**Decision:** Agentworks restart replaces a healthy running session and starts a stopped one. Start
does not gain replacement semantics from `--force-new`; it refuses that combination on a running
session.

Source:

- [systemctl command reference](https://cgit.freedesktop.org/systemd/systemd/tree/man/systemctl.xml?id=99504dd4c13af7516a976fffc0f68e6f26d3faac#n1940)

### Docker

Docker documents `container start` as starting stopped containers. Docker `attach` connects local
standard streams to a running container. Creation, start, and attach are distinct operations even
though some convenience commands compose them.

**Decision:** Agentworks console attach requires an existing runtime. Console create may compose
definition persistence with initial start, but ordinary attach never creates or rebuilds.

Sources:

- [docker container start](https://docs.docker.com/reference/cli/docker/container/start/)
- [docker container attach](https://docs.docker.com/reference/cli/docker/container/attach/)
- [docker container create](https://docs.docker.com/reference/cli/docker/container/create/)

### tmux

Tmux documents `new-session` as creation and `attach-session` as joining an existing session. Its
attach reference explicitly says the target session must already exist and directs creation to
`new-session`. Tmux also exposes `has-session` as a status probe and `kill-session` as teardown.

**Decision:** Console and session managers continue using explicit tmux status, creation, attach,
and kill operations. Agentworks does not preserve tmux's optional create-or-attach convenience as
its own attach contract.

Source:

- [tmux manual](https://man7.org/linux/man-pages/man1/tmux.1.html)

## Harness Continuation

### Claude Code

Claude Code's CLI reference separates starting an interactive session from resuming a selected
session through `--resume`. The selected session ID is an external conversation identity rather than
the Agentworks durable session name.

The current Agentworks Claude integration stores an Agentworks-minted UUID in its own namespace,
probes for its transcript, and chooses `--resume UUID` or `--session-id UUID` at launch.

**Decision:** Ordinary Agentworks start lets the integration preserve this choice. Forced fresh
rotates the Agentworks binding to a new UUID and launches that ID without deleting the prior
conversation.

Sources:

- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)
- `cli/agentworks/plugins/claude/harness_integration.py`

### Grok Build

The in-repository Grok integration follows the same Agentworks-owned UUID pattern as Claude. Its
state parser already treats the persisted UUID as a validation boundary.

**Decision:** Preserve the existing ordinary continuation behavior and use a newly minted canonical
UUID for forced fresh. No shared external-conversation abstraction is introduced merely because two
built-ins have the same binding strategy.

Source:

- `cli/agentworks/plugins/grok/harness_integration.py`

### Codex

The current Codex integration cannot allocate the external thread identifier before launch. It
provisions a notify recorder, adopts the tool-reported UUID later, and has an existing fresh command
that clears the managed recorder before starting bare Codex.

**Decision:** Forced fresh reuses that path after clearing only the integration-owned binding. The
design does not promise retry stability for an identifier the external tool never reported.

Source:

- `cli/agentworks/plugins/codex/harness_integration.py`

## Current Agentworks Prior Art

The current implementation has several useful boundaries to retain:

- session create already calls the integration before row insertion, includes mutated state in the
  inserted row, and inserts before tmux creation;
- session resume already calls the integration after old-runtime teardown and persists the complete
  namespaced state before new tmux creation;
- session status distinguishes stopped, healthy, broken, and legacy socket cases;
- batch stop already separates graceful signaling from later liveness checks;
- console list and describe are database-only;
- console attach already skips pane-secret resolution when tmux exists;
- console building already has one substantial pane/layout implementation.

The parts to retire are ownership mistakes rather than missing primitives:

- core lifecycle replacement is named resume;
- capability `start` and `resume` duplicate a lifecycle distinction integrations should not own;
- mutable `launch_note()` is a second return channel;
- console attach owns first realization and rebuild;
- create is database-only, so a successful create does not mean the runnable console is ready;
- teardown logic is duplicated across direct and cascading paths.

**Decision:** reshape these existing seams rather than add new persisted state or a common runtime
framework.

## Alternatives Considered

### Keep `--no-resume`

Rejected. It creates a double-negative API (`no_resume=False`) and describes an implementation
detail rather than the requested result. `--force-new` and `force_new` state the positive policy.

### Let `start --force-new` replace a running runtime

Rejected. It makes an option silently override the lifecycle verb. Refusal plus guidance to
`restart --force-new` is clearer and preserves the meaning of start.

### Generate a common external conversation ID

Rejected. Claude and Grok accept Agentworks-owned UUIDs, but Codex assigns its own ID after launch
and shell has no conversation identity. A common identifier contract would be false abstraction.

### Make every integration implement graceful stop now

Rejected. No current built-in has demonstrated a better application-level request than core's
generic tmux interrupt. A default-no-op method establishes the boundary without speculative per-tool
commands.

### Persist console runtime status

Rejected. Tmux is the authority for console runtime presence. Persisted status would become stale
and require reconciliation machinery without fixing an operator problem.

### Preserve attach-time creation as a compatibility default

Rejected. It would keep secret resolution and runtime mutation hidden behind attach. The bounded
0.17 `--recreate` wrapper is enough for migration while ordinary attach changes immediately.

## Refuted or Do-Not-Rely-On Claims

- A generated launch command does not prove the external harness resumed successfully.
- A stored conversation binding does not prove its transcript or thread remains usable.
- A persisted PID does not alone prove session health; tmux/socket liveness remains authoritative.
- A console database row does not prove its derived tmux runtime exists.
- Matching lifecycle vocabulary does not imply matching persistence, error recovery, or manager
  implementation.

## Open Implementation Questions

The design intentionally leaves only local implementation choices:

- the smallest private record used to carry a prospective console definition into build planning;
- how the current batch-stop helper is reshaped while preserving one shared grace phase;
- which existing output section names can be reused after the vocabulary rename.

None changes the public contracts or requires another SDD ruling.

## Sources

| Source                       | Quality | Design angle                               |
| ---------------------------- | ------- | ------------------------------------------ |
| systemd systemctl source man | Primary | start/restart verb semantics               |
| Docker CLI reference         | Primary | create/start/attach separation             |
| tmux manual                  | Primary | concrete runtime primitive semantics       |
| Claude Code CLI reference    | Primary | external conversation resume distinction   |
| Current Agentworks source    | Primary | actual persistence and orchestration seams |

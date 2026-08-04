# codex harness integration: locked

**Locked:** 2026-08-04

This effort is complete and locked. It added the `codex` harness integration as an opt-in `codex`
system plugin, following the `claude` plugin's paved road, and hardened the session-resume behavior
that a production incident proved unsound. The pinned decisions and the empirical CLI research live
in `codex-harness-decisions.md`; the integration contract it implements is
`cli/agentworks/capabilities/harness_integration/README.md`.

Per the `sdd` skill, this lockfile is the only artifact in this directory that may change from here.
Record post-lock updates as dated notes below rather than editing the decisions doc.

## What shipped

- **The `codex` plugin** (`cli/agentworks/plugins/codex/`): the integration plus a bundled `codex`
  `user-install-command`, both present-but-disabled until `[plugins] system = ["codex"]`. The
  installer pipes codex's first-party script into `CODEX_NON_INTERACTIVE=1 sh`, because the script
  prompts via `/dev/tty` whenever a controlling TTY exists and provisioning transports from Windows
  controllers force one, which stalled agent provisioning until its timeout.
- **A ten-field config vocabulary**, all optional and forwarded unvalidated (codex owns the choice
  sets): `model`, `sandbox`, `approval_policy`, `profile`, `network`, `approvals_reviewer`,
  `writable_dirs`, `web_search`, `disable_strict_config`, `extra_args`. `--strict-config` is emitted
  by default, since a non-strict codex silently ignores an unknown `-c` key and would leave a
  session quietly without network. The auto-mode pairing an operator actually wants is
  `sandbox: workspace-write` + `approval_policy: on-request` + `approvals_reviewer: auto_review` +
  `network: true`.
- **Notify-bound session resume** (the redesign; see the decisions doc's "Addressing" section).
  Identity comes from codex itself: a recorder script attached through codex's `notify` hook writes
  down the thread id codex reports after each completed turn, recording only payloads that carry a
  `client` key, because a subagent's turn fires the PARENT's hook with the CHILD's id. Where no id
  is bound, discovery falls back to rollouts carrying the literal `"source":"cli"` plus the
  session's canonicalized workspace cwd, and genuine ambiguity opens codex's own session picker in
  the pane instead of raising. `session create` is unconditionally fresh and clears any stale
  recording, so only `session resume` ever adopts an id. Every decision leaf is announced in both
  the `agw session resume` console output and the pane.
- **Per-integration namespacing of the session state blob** (`{"<integration>": {...}}`), so two
  stateful integrations cannot collide on a key name. No migration: pre-namespacing rows are adopted
  lazily by a claude-owned hoist, flagged
  `Compatibility (pre-namespacing harness_state): DELETE on the next major release.`
- **Dead-workload output capture in the core session machinery.** A workload that dies at launch
  (the motivating case: an invalid codex flag value clap rejects in milliseconds) used to take its
  tmux server down and surface as an opaque SSH failure from the socket-grant loop. `create_session`
  now holds the pane with `remain-on-exit`, checks liveness before the grant machinery, and folds
  the workload's own dying output and exit status into the create/resume error. A twice-failed probe
  is treated as unverified, never as death: it neither kills nor blames the template.
- **Docs:** the harness-integration developer guide (contract, resume practices, testing layers,
  plugin packaging, and the rule that an integration with DISCOVERED identity must not share one
  decision path between create and resume), plus `cli/README.md`, `docs/guides/resources.md`, the
  sample session-template, and ADR 0020's namespacing note.

## Delivery

Five PRs on `main`: #360 (plugin, namespacing, developer guide), #379 (the config knobs), #381
(dead-workload capture), #389 (`approvals_reviewer`), and the resume-hardening PR that closes this
effort. Built via the `agentic-dev-process`: lead-pinned decisions, delegated implementation, and
both reviewers (`agentworks-reviewer` plus a cold generic pass) on every commit cycle, with Copilot
triaged on each PR.

The review cycles earned their keep and are worth recording, because each caught a class of bug the
others missed. The project reviewer caught documentation that claimed behavior the code did not
have, repeatedly and including in this lockfile's own subject matter. The cold generic pass caught a
TOML-injection hole in a `-c` value and the `merge_config` list-laundering bug (fixed in `shell`
too, where it originated). Copilot caught the same injection independently and a stale comment. And
the verification passes that execute generated shell text against real fixtures caught what
exit-code stubs structurally cannot: a quoted `*` in a `case` pattern that made every workspace look
empty, and a greedy `sed` that took the last `thread-id` rather than the first.

## Known limits (shipped deliberately, tracked)

- **Discovery is a heuristic and says so.** Its residual windows are enumerated in the decisions
  doc. The reachable one: a session deleted and replaced in the same workspace can have the dead
  session's conversation adopted by the replacement's first resume, announced with the adopted uuid.
  Closing it needs a creation-time floor on candidates, which is clock-skew sensitive between
  controller and target and could orphan a live conversation if wrong;
  [#397](https://github.com/WayfarerLabs/agentworks/issues/397) carries the design.
- **Two undocumented codex contracts are load-bearing** and must be re-verified on any codex major
  bump: the `notify` payload's `client` key (the only in-payload discriminator between a real
  session's turn and a subagent's) and the payload's field ORDER (the recorder takes the first
  `thread-id` in byte order, which is the payload's own only because 0.146.0 emits it before any
  nested object). Both failure modes are recoverable: a wrong binding lands in the picker.
- **No codex-side session name is set.** Codex has no launch-time `--name`, and every binding path
  requires the id first; [#394](https://github.com/WayfarerLabs/agentworks/issues/394) would add one
  for picker legibility and manual recovery, gated on
  [#395](https://github.com/WayfarerLabs/agentworks/issues/395) (upstream openai/codex#14482).
- **Auth and workspace trust are provisioning concerns, out of scope here** (parity with claude v1):
  an unauthenticated pane parks on codex's login menu and an untrusted directory on its trust
  prompt, both codex's own visible surfaces. The eventual home is a harness provisioner; see the
  capability README's planned-capabilities section.
- **Codex's hooks subsystem** (`SessionStart` with `source: startup|resume`, `SubagentStart`,
  `SubagentStop`) would be a strictly better identity-binding surface than `notify`: it fires at
  launch rather than after a turn, and self-labels subagents. No experiment could get a hook to fire
  from config alone in 0.146.0 (plugin-only installation suspected). Tracked in #395.

# Runnable status inspection: locked

**Locked:** 2026-09-04

This effort is complete in PR #736. The lock takes effect when that PR lands on `main`; until then,
this file records the final reviewed and operator-accepted implementation state.

## What shipped

- Plain `vm list`, `session list`, and `console list` remain local inventory operations. Each
  accepts an explicit `--status` option for live observation, refuses `--names-only --status`, and
  renders status only when requested. The hidden 0.18 `session list --no-status` compatibility
  spelling is CLI-local, warns, and is scheduled for removal in 0.19 by issue #720.
- VM, session, and console describe operations report live status by default without activating,
  repairing, starting, stopping, or persisting managed state. Expected observation failures retain
  configured facts and report `unknown` rather than inferring absence.
- VM observation uses the existing version-1 platform status operation, performs shared setup once,
  calls a site-local platform serially, and allows bounded concurrency only across independent
  sites. Provider-native timeouts are bounded where their existing clients permit it; status does
  not use guest transport or lifecycle gates.
- Session observation classifies the exact tmux and managed-process facts through one resource-owned
  authority. Selected list rows are batched once per VM; singular probes use the same classifier.
  Guest calls are non-interactive, use no forced TTY, receive finite stdin, make one attempt, and
  have a ten-second call-site timeout.
- Console observation enumerates exact canonical and staging tmux session names once per selected VM
  and isolates a failed VM as `unknown` without discarding successful rows from other VMs. Its
  singular describe path selects one row from that same observer.
- Human status commands announce external work before dispatch and summarize unknown rows by VM or
  provider site. JSON remains presentation-free and distinguishes status not requested from
  requested-but-unknown using the documented version-1 fields. No capability contract or schema
  version changed.
- VM, session, and console list tables use the same alignment helper; VM keeps its existing
  name-only cap without truncating other fields.

## Safety and implementation boundaries

Status is an observation, not reconciliation. Tests instrument provider, activation, secret,
transport, repair, and database-write seams to prove that plain inventory and requested observation
do not acquire lifecycle side effects. Empty and fully filtered selections dispatch no external
work, and partial target failure cannot convert `unknown` to `stopped` or suppress healthy rows.

The implementation deliberately retains three resource-owned observers rather than introducing a
generic runnable abstraction. VM provider state, session tmux/process state, and console tmux state
have different evidence and vocabularies. They share bounded dispatch and presentation mechanisms
only where the behavior is genuinely common.

Windows live testing exposed two defects that the initial automated work did not catch. The session
batch format had eight value slots for nine values, producing malformed frames where the parser
required ten total fields. Correcting the frame made the already-fast batch result usable. Later,
concurrent no-input SSH probes intermittently timed out after complete remote output because the
Windows client retained inherited console stdin. Supplying explicit empty stdin eliminated the fault
in 80 of 80 otherwise-identical probes while preserving the established no-TTY, one-attempt,
ten-second policy. A retry experiment was discarded rather than masking the stream-lifecycle defect.

## Verification and review

The final production checkpoint is `7af53bf5d99ed739d01836c3d7cf88f546dec413`, based on
`origin/main` at `aae1b2cd6939add6944a14f34af05f6f3e838edf`. Verification recorded:

- 8,383 non-integration Python tests with one platform-specific skip;
- focused observer, side-effect, timeout, parser, failure-isolation, CLI, completion, projection,
  transport-input, table-layout, and describe suites;
- Ruff check and format plus strict mypy for the changed modules;
- file lint, locked-SDD, Rulesync drift, diff, and release-residual gates;
- a fresh isolated wheel build, install, CLI-help, status-grammar, and completion smoke pass; and
- hosted Python 3.12, 3.13, and 3.14, Python checks, file lint, Rulesync, locked-SDD, Website,
  CodeQL, and aggregate CI success at the production checkpoint.

Private project-values, Muntz, and cold correctness/security reviews converged cleanly. Three
authorized published implementation feedback/fix rounds completed. The final public review verified
that the shared renderer retained one alignment rule without absorbing VM-only configuration.

The operator's Windows passes covered plain lists, all three status lists and describe commands,
human and JSON vocabularies, grammar conflicts, the hidden compatibility flag, session and console
batching across multiple VMs, partial target failure, recovery on a subsequent observation, and an
unchanged logical database fingerprint. The final finite-stdin build then eliminated the reproduced
intermittent console-status timeout, and the operator reported no remaining live-testing issue.

## Permanent homes and accepted limits

The operator contract lives in `cli/command-reference.md`, `cli/README.md`,
`docs/guides/runnable-status.md`, `docs/guides/session-status.md`, and
`docs/guides/upgrading-to-0.18.md`. Resource-owned status modules, transport call sites, completion
metadata, and behavioral tests carry the executable contract. Nothing in this SDD directory is
required to operate or maintain the feature.

Issue #720 owns removal of `session list --no-status` together with the two lifecycle compatibility
wrappers in 0.19. Provider APIs without a native cancellable timeout retain their documented
provider-specific limit; bounded orchestration prevents one target from serially blocking unrelated
targets. No other in-scope implementation, review, migration, or documentation finding remains.

The operator owns merging PR #736. The effort lead does not merge it.

-- agw-ns-onboard-disco

# Runnable Status Inspection: Functional Requirements

- Status: Design
- Date: 2026-09-03
- Target release: 0.18.0
- Predecessor: `docs/sdd/2026-08-31-session-console-lifecycle/`

## Purpose

Make runtime status a consistent, explicit inspection choice for Agentworks' three runnable resource
families: VMs, sessions, and consoles. An ordinary list should answer what Agentworks has configured
without contacting providers or guests. An operator who asks for live status should get a read-only
observation with prompt progress, finite concurrency, hard guest-probe bounds, and honest partial
results. Provider calls use provider-native bounds where their APIs support them.

This effort also closes two inconsistencies exposed by the session and console lifecycle work:

1. `session list` currently performs remote status checks by default, while VM and console lists do
   not.
2. VM and session describe include live status, but console describe exposes only configured state.

"Runnable" is a product-level grouping in this document. It does not introduce a common runtime
type, status enum, manager, or capability contract. Each resource keeps the states and observation
authority that match its runtime.

## Personas and user stories

- As an operator listing resources during routine navigation or shell completion, I want a fast,
  local result that cannot start a VM, resolve provider credentials, repair state, or wait on a
  remote host.
- As an operator investigating fleet state, I want the same `--status` spelling on VM, session, and
  console lists, with visible progress before slow work begins.
- As an operator inspecting one resource, I want describe to report its live state without changing
  that state, and to preserve configured facts when observation is unavailable.
- As an automation author, I want machine output to distinguish status that was not requested from a
  requested observation that was inconclusive.
- As a maintainer, I want internal names to say `running`, not translate an `OK` implementation term
  into `running` at every output boundary.

## Settled product decisions

1. `agw vm list`, `agw session list`, and `agw console list` are local inventory commands by
   default. `--status` opts into live observation.
2. Local inventory may read the state database and the local config/manifests needed to preserve
   existing non-status columns. It performs no provider call, guest transport call, secret
   resolution, activation, repair, or lifecycle mutation.
3. `agw vm describe`, `agw session describe`, and `agw console describe` remain focused inspections
   and include a live status observation by default. All three use a non-activating observation
   path.
4. A requested observation is best effort across a list. One unavailable VM, provider, credential,
   or transport does not erase the local inventory rows for other resources.
5. Status vocabularies remain resource-specific. Shared CLI grammar does not imply a shared
   `RunnableStatus` abstraction.
6. Human tables include a status column only when status was requested. Machine records keep a
   stable field shape and carry an explicit not-requested value.
7. `--status` and `--names-only` are mutually exclusive. Silently ignoring an explicit status
   request would be misleading, and completion must remain local and fast.
8. R23 owns the existing `session list --no-status` compatibility spelling and its release window.
   The shim never re-enables the old default-live behavior or reaches a service policy.
9. `SessionStatus.OK` is renamed to `SessionStatus.RUNNING`, with matching variables, comments,
   tests, and documentation. No persisted database value or external capability version changes.
10. This effort adds no standalone `status` commands and no generic runnable service. The list and
    describe questions are sufficient for the current product need.

## Functional requirements

### Standard list grammar

- **R1.** VM, session, and console list shall each accept a valueless `--status` option with the
  meaning "include current live runtime status."
- **R2.** Without `--status`, each list shall assemble and render its existing inventory facts using
  local state only. The service shall not contact a provider or guest, resolve a secret, open an
  activation gate, perform recovery, or update persisted runtime state.
- **R3.** With `--status`, each list shall first select and validate its inventory rows, then
  perform live observation only for those rows under the resource-specific execution policy in R9.
  Status shall not change which rows match a filter or their ordering.
- **R4.** `--status` combined with `--names-only` shall fail as a usage error before config,
  database, provider, secret, or transport work. Existing `--names-only` behavior remains a local
  one-name-per-line completion surface.
- **R5.** Human mode without `--status` shall omit the status column entirely. Human mode with
  `--status` shall include it and summarize inconclusive observations after the table without
  replacing successful rows. VM, session, and console list tables shall use one shared alignment
  mechanism so appending `STATUS` cannot produce a domain-specific column-spacing defect.

### Observation safety and progress

- **R6.** A status observation shall be read-only with respect to Agentworks and the managed
  runtime. It shall not start or stop a VM, create or destroy tmux state, repair Tailscale, migrate
  legacy runtime state, update PID or boot evidence, or write an observed value to the database.
- **R7.** Human list and describe commands shall print a concise progress line before the first
  external status operation. List progress shall include the number of selected resources and the
  number of distinct VM or provider boundaries when known.
- **R8.** Machine output shall remain one clean JSON document on stdout. Presentation suppression
  shall prevent human progress and diagnostic prose from entering the JSON stream.
- **R9.** List observation shall batch or parallelize at the natural authority boundary with finite
  concurrency. Session and console guest calls shall have a 10-second, one-attempt bound and shall
  not inherit operator stdin when they carry no fact input. VM calls shall use provider-native
  bounds where supported; a provider SDK without cancellable timeout support may still block after
  progress has been shown. Once per-boundary dispatch begins, a failure shall mark only that
  boundary's rows unknown and allow other boundaries to finish. A shared VM registry, preflight, or
  credential-resolution failure before dispatch may mark every selected VM row unknown.
- **R10.** Expected operational failures, including unreachable guests, unavailable credentials,
  unsupported local backends, and provider lookup failures, shall produce unknown live status.
  Invalid CLI input, corrupt local invariants, and unexpected programming failures shall retain
  typed failure behavior rather than being mislabeled as unknown.

### Resource-specific status contracts

- **R11.** VM live status shall use `running`, `stopped`, `deallocated`, or `unknown`. It shall come
  from the selected VM platform's read-only status operation and shall remain distinct from
  provisioning and initialization state. A stopped or deallocated VM may additionally retain the
  existing `manual` or `idle` disposition.
- **R12.** Session live status shall use `running`, `stopped`, `residual`, `broken`, or `unknown`.
  `running` means the exact managed tmux session is present. `residual` means its dedicated tmux
  server remains but the managed session does not. `broken` means stored same-boot process evidence
  remains live while tmux is unreachable. `unknown` means a requested observation could not prove
  another state. Persisted `PID_STOPPED` evidence does not bypass requested live observation or
  override observed tmux state. After both the exact session and dedicated server are
  authoritatively absent, `PID_STOPPED` is sufficient supporting evidence for `stopped`. A row
  without a canonical runtime locator becomes `unknown`.
- **R13.** Console live status shall use `running`, `stopped`, `residual`, or `unknown`. `running`
  means the exact canonical tmux session is present and its reserved staging session is absent.
  `stopped` means both managed names are absent. `residual` means the staging name is present,
  whether the canonical name is present or absent. `unknown` means the authoritative tmux
  session-name enumeration was inconclusive.
- **R14.** Session and console observations shall use the canonical VM transport directly and shall
  not activate the VM. Their tmux targets shall preserve exact-name selection. A stopped or
  unreachable VM therefore yields unknown, not an automatic start.
- **R15.** VM observation may run the platform preflight and resolve only credentials declared for
  the read-only provider status operation. It shall not run authenticated runup tests or enter an
  activation gate.

### Focused describe

- **R16.** VM, session, and console describe shall expose the same resource-specific live status
  meaning as `list --status`.
- **R17.** Session describe shall stop using the activation-gated session-operation boundary for
  status. Console describe shall add a live observation without loading console build inputs or
  resolving pane secrets. VM describe shall retain its no-activation provider observation.
- **R18.** When an expected live observation fails, describe shall still return the selected
  resource's locally available facts with status `unknown`. Human output shall report the
  observation problem without converting it into a false stopped state. JSON shall remain limited to
  its closed safe facts.

### Machine output

- **R19.** `session.list` JSON v1 shall retain its required `status` field and its existing consumer
  meaning for `unavailable`: live status was not available in that record. The 0.18 producer shall
  emit `unavailable` when `--status` was not requested, one of the four conclusive live states when
  it was, and `unknown` when a requested observation was inconclusive. This producer invariant
  distinguishes the two cases without redefining the v1 consumer vocabulary.
- **R20.** `console.list` JSON v1 shall add a `status` field with `unavailable` when live status was
  not requested and the console state vocabulary when it was. `console.describe` shall add the same
  field but never emit `unavailable`, because describe always requests observation.
- **R21.** `vm.list` JSON v1 shall add nullable `observed_status` and `status_disposition` fields
  matching VM describe. A null observed status means list status was not requested. A requested
  inconclusive observation emits `unknown`. Disposition remains null unless a stopped or deallocated
  observation supports `manual` or `idle`.
- **R22.** Additive fields shall retain the JSON v1 envelope, command identities, collection order,
  and existing field meanings. The behavior change to session list default observation shall be
  documented as a 0.18.0 CLI migration.

### Compatibility and collateral

- **R23.** In 0.18.0, `session list --no-status` shall remain accepted as a hidden no-op, emit the
  ordinary suppressible deprecation message, and conflict with `--status`. The message shall point
  to plain `session list`. In 0.19.0, the option shall be removed from parsing and compatibility
  tests.
- **R24.** Bash, zsh, and PowerShell completion shall offer `--status` for all three list commands,
  omit deprecated `--no-status`, and retain names-only completion without status work.
- **R25.** CLI help, command reference, session status guidance, runnable lifecycle guidance, JSON
  schemas, release notes, and the 0.18 upgrade guide shall teach the inventory-versus-observation
  split in the same change. Historical locked SDDs remain unchanged.
- **R26.** The vm-platform and harness-integration capability contracts remain version 1. No method
  is added or renamed in either contract.

## Quality requirements

- **Q1.** Tests shall prove the negative safety boundary by instrumenting provider, activation,
  secret, transport, repair, and database-write seams for list without status and for requested
  read-only observation.
- **Q2.** Session and console batch parsers shall treat transport failure, malformed facts, mixed
  streams, and missing exact names conservatively. Session probes use exact targets. Console probes
  compare validated canonical and staging names by exact equality against one authoritative tmux
  session enumeration. Absence is reported only from an authoritative tmux result.
- **Q3.** Tests shall cover partial success across multiple VMs/providers, timeout behavior, Windows
  non-interactive probe transport, human and JSON projection, filters, empty results, and names-only
  conflicts.
- **Q4.** Tests assert behavior, structure, values, ordering, and side effects. They shall not pin
  authored progress, warning, help, or deprecation prose.
- **Q5.** List command modules remain thin and preserve lazy imports and fast help generation.
- **Q6.** The implementation leaves no internal `OK` session-status vocabulary and no live-list
  boolean named `no_status`.

## Acceptance criteria

1. Plain VM, session, and console lists return without any provider or guest status call and omit a
   human status column.
2. The same lists with `--status` show resource-appropriate current state, visible progress, and
   honest unknown rows when only part of the fleet can be reached.
3. No list or describe status observation activates, repairs, or mutates a VM, session, console, or
   persisted runtime record.
4. Console list and describe distinguish healthy canonical tmux state, stopped state, staging
   residue, and inconclusive observation.
5. Session list defaults to JSON `unavailable`, requested inconclusive checks emit `unknown`, and VM
   and console additive fields follow R20-R21.
6. All three describe commands include non-activating live status and preserve local facts when an
   expected observation is unavailable.
7. `--status` completion and help are consistent across all three resources; `--status` with
   `--names-only` fails before work; compatibility follows R23.
8. Focused tests, the full non-integration suite, Ruff, format, strict mypy, repository guards,
   installed-wheel smoke tests, and capability-appropriate live tests pass before merge intent.

## Out of scope

- A generic Runnable protocol, shared runtime-status enum, registry kind, or capability.
- Standalone `vm status`, `session status`, or `console status` commands.
- Persisting observations, status history, polling/watch mode, health checks, resource usage, or
  readiness convergence.
- Starting stopped VMs to answer session or console status.
- New vm-platform batching methods or capability version changes.
- Changes to lifecycle start, stop, restart, attach, create, or delete semantics beyond replacing
  shared status helpers with truthfully named equivalents.

## Rulings

- 2026-09-03: The operator approved one SDD and implementation PR, up to three design feedback/fix
  rounds, followed by up to three implementation feedback/fix rounds if design converges.
- 2026-09-03: The operator set 0.18.0 as the feature release and 0.19.0 as the compatibility-removal
  release, correcting the one-release offset found after the predecessor lifecycle PR.

# R5 Resolved Spec and Drift Surfaces: Low-Level Design

- Status: Implemented and verified; closeout validation pending
- Date: 2026-08-29
- Last revised: 2026-08-30
- Requirements: R5 in `frd.md`
- Architecture: `hla.md`, R5 resolved declaration and lifecycle-evidence projections
- Storage contract: `store-contract.md`
- CLI grammar: `docs/sdd/2026-08-10-cli-grammar/`
- Delivery vehicle: one draft-to-ready PR containing this checkpoint and the implementation

## Decision summary

R5 extends existing inspection commands rather than adding another command family. Focused
`agw resource show KIND/NAME` gains resolved values and value provenance for the four inheriting
template kinds plus `admin-template`. Existing `agw vm describe`, `workspace describe`,
`agent describe`, and `session describe` gain one `instance_state` object containing current
declaration, persisted instance overlay, lifecycle evidence, and comparisons that the available
evidence can actually prove. Current producers always include this object; additive JSON v1
compatibility allows older producers to omit it. `graph show` remains the relationship surface and
`doctor` remains the fleet-wide integrity and drift surface.

One shared presentation-free projector converts typed resolved models and `LayeredResolution`
provenance into closed JSON facts. The generic resource-show service reaches domain resolution
through an optional kind protocol and imports no VM, workspace, agent, or session reducer. Live
describe collectors use strict inspection resolvers: a selected template that no longer exists is
unresolved, never a built-in default with the overlay folded over it.

Each describe collector gathers its database row, related rows, finalized registry, desired overlay,
current resolution, and lifecycle records in one SQLite snapshot. Describe commands use their
existing writable command-owned database, whose nested `snapshot()` calls compose with that
transaction without opening another SQLite snapshot. The snapshot closes before any provider, SSH,
secret, prompt, transport, or live-status work.

The repository gains closed inspection reads for one owner and for the fleet. They classify each row
independently as recognized, unconsumed, or malformed and report whether its database owner exists.
They expose no raw payload escape hatch. Domain codecs still decode recognized payloads. This lets
doctor report an orphan, malformed known record, unsupported known payload version, or well-formed
future record without one bad row hiding the rest.

Doctor reuses its existing non-migrating Database group. It opens the current schema read-only,
holds one snapshot for counts and the fleet inspection batch, derives the configured SSH identity at
most once per run, and compares every VM in memory. A config, manifest, registry, record, or key
failure reduces only the checks that require that input. It does not suppress independent database
integrity facts.

No database migration, drift remediation, new applied-state synthesis, password-protected-key
restriction, adjacent-public-key fallback, or ssh-agent rule is introduced.

## CLI grammar and command ownership

The CLI grammar gives the four verbs distinct jobs:

- `show` is a factual projection of one focused registry resource;
- `describe` synthesizes the domain facts for one concrete live object;
- `doctor` diagnoses fleet health, readiness, integrity, and drift; and
- `graph show` owns dependency and usage relationships.

R5 therefore adds no `vm show`, `workspace show`, `agent show`, or `session show` command. The
existing command IDs and argument grammar remain unchanged:

```console
agw resource show vm-template/NAME
agw resource show admin-template/NAME
agw resource show workspace-template/NAME
agw resource show agent-template/NAME
agw resource show session-template/NAME

agw vm describe NAME
agw workspace describe NAME
agw agent describe NAME
agw session describe NAME

agw doctor
```

`resource show` answers what a newly declared instance would receive from one template. A per-kind
`describe` answers what one database-backed instance currently declares, what instance-specific
layer it stores, which successful configuration-snapshot slices were recorded by lifecycle work, and
whether comparable facts match. A slice can include separate evidence that its corresponding work
succeeded. Doctor answers the same structural questions across the fleet and adds integrity
diagnostics. Describe does not duplicate graph traversal or turn into a general health command.

## Shared resolved-spec facts

### Closed carriers

A presentation-free module shared by resource show and live describe owns these conceptual records:

```text
ResolvedSpec
  status: resolved
  spec: JsonObject
  provenance: tuple[ResolvedPathProvenance, ...]

UnresolvedSpec
  status: unresolved
  selection: ResourceIdentity
  reason: missing-selection | instance-spec-unavailable | registry-unavailable

ResolvedPathProvenance
  path: tuple[str | int, ...]
  sources: tuple[ResolvedValueSource, ...]

ResolvedValueSource
  role: defaulted | inherited | declared | overlaid
  resource_kind: str
  resource_name: str
```

The public JSON field is `spec`, matching the operator-facing vocabulary established by `--spec`.
Internal Pydantic model names are not exposed. JSON paths are arrays of string or integer segments,
never dotted strings. A string segment names a field or map key. An integer segment names a position
in the final effective list. This stays unambiguous when map keys contain punctuation and never
renders authored list-item values as identity.

Sources retain their fold order. More than one layer can truthfully contribute to an append or
deduplicated value. The projector maps existing `LayerSourceKind` values as follows:

| Layer source           | Projected role |
| ---------------------- | -------------- |
| `DEFAULT`              | `defaulted`    |
| `INSTANCE`             | `overlaid`     |
| selected template row  | `declared`     |
| any other template row | `inherited`    |

The selected template identity is the explicit name or the domain's real selected default. The role
is a projection only. Merge and provenance accumulation retain their existing vocabulary and
semantics.

### Complete projection

The projector serializes the resolved typed value in Pydantic JSON mode, omits only the framework
`name`, and validates the result through the same finite `JsonValue` boundary used by resource
declarations. It includes nulls and empty maps or lists because they are material parts of the fully
resolved spec.

Every material value remains reachable through provenance. Projection walks the final value in
model, map, and list order and emits scalar leaves, empty containers, final list positions, and a
container path only when one exact provenance record still owns its whole surviving subtree. A map
or list assembled from multiple layers is represented by its truthful descendant paths rather than
by a misleading longest-prefix source for the composite container. A replaced object attributes all
surviving descendants to the replacement source while discarded descendants remain absent. The five
inspection resolvers seed default provenance for every resolved top-level field except `name`,
including nulls and empty collections. More-specific merge provenance continues to override those
prefixes. This corrects an inspection gap without changing resolved values or merge policy.

Session inspection uses the ordinary use-view resolver, including the real shell default. The
finalize-only total view remains an internal graph-building accommodation and is not operator
inspection truth.

## Template `resource show`

An optional runtime-checkable kind protocol provides one closed hook:

```text
ResolvedSpecKind.resolve_for_show(registry, name) -> ResolvedSpec
```

`vm-template`, `admin-template`, `workspace-template`, `agent-template`, and `session-template`
implement it. Each handler imports and calls its own domain resolver, then invokes the shared
projector. Generic `resources.show` checks the protocol and does not import or switch on domain
kinds. Although `admin-template` has no inheritance chain, it participates because it is a
separately selectable pre-create VM declaration and operators need to distinguish its authored
values from model defaults. A live VM resolves and displays its selected admin template and admin
instance layer through `vm describe`.

The existing `resource.show` JSON fields retain their order, names, nullability, and meaning. Hooked
kinds append one optional tagged field:

```json
{
  "resolution": {
    "status": "resolved",
    "spec": {
      "cpus": 4,
      "memory": 8,
      "disk": 50,
      "swap": 4
    },
    "provenance": [
      {
        "path": ["cpus"],
        "sources": [
          {
            "role": "defaulted",
            "resource_kind": "vm-template",
            "resource_name": "dev"
          }
        ]
      }
    ]
  }
}
```

Non-hook kinds omit `resolution`; they do not add a global null. `declaration` remains the
normalized loaded manifest envelope, including `inherits`. `resolution.spec` is the concrete
post-inheritance input to an instance. The human renderer adds a Resolved spec section only when the
same fact exists.

The hook never resolves a secret, prompts, probes a provider, or reads the database. Configured
resource names such as secret references appear because they are declaration facts. Resolved secret
values never do.

## Live-instance description

### Current-producer JSON addition

Each existing per-kind JSON v1 description retains all current fields and appends one tagged
`instance_state` object inside its existing top-level kind object. Current producers always include
it; older JSON v1 producers may omit the additive field. Its conceptual shape is:

```json
{
  "instance_state": {
    "declarations": {},
    "lifecycle_evidence": [],
    "comparisons": [],
    "unconsumed_records": [],
    "issues": []
  }
}
```

Each defined lifecycle-evidence fact is independently tagged `recorded`, `not-recorded`, or
`unavailable`. There is no aggregate state that could imply the complete resolved spec was captured
or converged. Workspace, agent, and session currently define no lifecycle-evidence slots and
therefore return an empty list. Their human presentation says that no lifecycle evidence is
recorded. Their paths, Linux users, rows, runtime status, repair results, and integration state are
not promoted into lifecycle evidence.

One declaration slot is:

```text
DeclarationSlot
  name: str
  selection: ResourceIdentity
  instance_spec: AbsentInstanceSpec | PresentInstanceSpec | UnavailableInstanceSpec
  current: ResolvedSpec | UnresolvedSpec

PresentInstanceSpec
  status: present
  recorded_at: str
  spec: JsonObject

UnavailableInstanceSpec
  status: unavailable
  reason: malformed | unsupported-version
```

An absent instance layer is represented structurally as `{ "status": "absent" }`. It does not add
ceremony to ordinary human output beyond a compact fact. A present layer shows its canonical typed
partial spec and timestamp. A malformed or unsupported stored desired record is `unavailable`, which
also makes current resolution unresolved; lifecycle evidence and sibling facts remain visible.

R5 intentionally displays operator-authored declaration values, including explicitly inline
environment values, when an operator invokes `show` or `describe`. Fully resolved inspection would
otherwise be incomplete and could not explain effective behavior. Human and machine output from
these commands is therefore sensitive and permanent guidance says to handle it accordingly.
Configured secret references may appear, but no secret lookup occurs and no resolved secret value
may enter the projection. Lifecycle result lines remain value-free because they are mutation
receipts, not explicit inspection requests.

Human live-instance descriptions render each structured instance spec, current resolved spec, and
lifecycle-evidence value object as safe block YAML with declaration order retained and non-ASCII
content escaped. This matches the human `resource show` configuration-document convention without
changing JSON v1. Comparison differences remain compact JSON because they are paired values inside
one diagnostic line rather than standalone configuration documents.

VM descriptions contain `vm` and `admin` declaration slots. Workspace, agent, and session contain
one slot named for their kind. The VM slot resolves its selected VM template plus VM overlay; the
admin slot resolves its selected admin template plus admin overlay. The composite VM desired record
is read and decoded once, then split into these two presentation slots.

If an explicitly selected template is gone, `current.status` is `unresolved` and identifies the
missing selection. Inspection does not apply an overlay to an invented default. In particular, it
does not use the workspace runtime compatibility fallback. Stored overlay and lifecycle evidence
remain visible, and comparisons that require current intent are omitted with a structural issue.

### Snapshot boundary

The structural collector performs these steps in one explicit `Database.snapshot()`:

1. read the owner and related database rows used by the existing description;
2. build and finalize the ordinary request registry, whose database publication nests into the
   already-held writable transaction without starting another SQLite snapshot;
3. read and domain-decode the desired overlay;
4. resolve current declarations and provenance with the decoded overlay;
5. inspect applied records for the owner; and
6. derive every row-backed comparison input.

R5 adds no registry mode or transaction-state flag. The existing `Database.snapshot()` selects a
composable transaction on writable command databases, so the registry publisher's inner snapshot
increments transaction depth but issues no second `BEGIN`. Read-only callers retain their strict
no-nesting rule; the live describe commands are not read-only callers.

Ordinary registry finalization may perform its existing cheap, offline host-readiness checks inside
the snapshot. By contract those checks use only OS, tool-presence, and passed configuration facts;
they never use the network, secrets, or prompting. The structural snapshot closes before provider
status, secret preview or resolution, prompts, SSH transport, or runtime-resource measurement. VM
describe then performs its existing bounded live observations with the retained registry and appends
their existing issues. Session describe performs its existing status/PID observation, including any
PID refresh, before taking the authoritative structural snapshot used for the returned DTO. The
runtime status remains explicitly observed state outside the structural snapshot.

Workspace and agent describe commands load configuration before entering their tolerant manager
path. After that succeeds, an expected `ConfigError` or `ValidationError` during registry
construction does not suppress the database-backed description, related rows, desired overlay, or
empty lifecycle-evidence slots. Instead, the current declaration is unresolved with reason
`registry-unavailable`, and an issue identifies the affected slot. Configuration loading failures
and unexpected programming or infrastructure failures still propagate. This does not make either
command contact a provider or resolve a secret.

### VM lifecycle evidence and comparisons

VM exposes two recognized lifecycle-evidence slots:

- `hardware-request`: when the private version-1 `hardware-provenance` marker exists, the
  same-snapshot VM row supplies the CPU, memory, disk, and swap request, while the slice supplies
  operation and timestamp;
- `ssh-identity`: the slice supplies verified fingerprint or explicit unverifiable evidence,
  private-key reference, operation, and timestamp.

Hardware marker absence is `not-recorded` regardless of the VM row values. With a marker present,
CPU, memory, disk, and swap must each be an exact integer before the row can serve as recorded
request evidence. A null or wrongly typed column yields an unavailable hardware-request fact and
bounded database-damage issue; comparison is omitted. Valid row facts plus a resolved current VM
declaration yield `match` or `drift` by comparing the four hardware fields. The drift fact lists
field names plus recorded and current numeric values. An unresolved declaration leaves the valid
recorded request visible but omits comparison with an issue; it is neither drift nor unverifiable.
The request is what Agentworks expected the successful create to produce. It does not claim
provider-observed realized hardware or detect provider normalization or inconsistency.

The existing JSON v1 `vm.provisioned_resources` field remains the recorded provisioning request and
does not claim provider observation. Human VM describe labels the corresponding persisted fact
`Requested` rather than `Resources`.

SSH comparison keeps the existing four states: `not-recorded`, `unverifiable`, `match`, and `drift`.
The VM domain splits its current helper into a pure comparison over a preloaded applied slice and a
previously derived current identity, plus the existing convenience wrapper used by lifecycle gates.
A recognized legacy private envelope that exposes no fingerprint remains `unverifiable` and usable,
including password-protected formats already supported by transport. A missing, unreadable, or
malformed configured key produces a bounded inspection issue and no comparison. It is not drift. The
code never consults an adjacent public key or ssh-agent.

The comparison list contains only comparisons whose state is actually known. The absence of one is
explained by `issues`; R5 does not invent a fifth comparison state. Field-level comparison
differences use `recorded` and `current` operands. They do not call lifecycle evidence `applied`.

## Closed repository inspection

The physical table intentionally permits future record types and applied keys. Existing typed reads
skip a well-formed applied key unknown to this release, which is correct for lifecycle consumers but
insufficient for inspection. R5 adds two consumer-shaped operations over a shared private query:

```text
inspect_owner_state(instance_kind, instance_name) -> InstanceStateInspection
inspect_all_instance_state() -> InstanceStateInspection
```

The fleet read selects every `instance_records` row once in deterministic kind, owner, type, and key
order. SQL remains inside the repository. Owner existence is computed with bounded batch work in the
same snapshot, either in that statement or by one owner-name read per live kind. It never performs a
query per record or owner. The singular method adds an exact typed owner predicate rather than
exposing arbitrary filters.

`InstanceStateInspection` contains closed tuples of:

- recognized desired overlays and applied slices, paired with owner-existence status;
- unconsumed metadata: safe kind, owner, record type, record key, payload version, timestamp, and
  owner-existence status; and
- malformed observations containing safe identity metadata and a closed value-free diagnostic, never
  payload JSON, a decoder exception, or its traceback.

Each row is decoded independently. A malformed row becomes one observation and does not abort the
batch. Common envelope validation applies to every row. Current record types additionally enforce
their known discriminator rules. A well-formed future record type or unknown applied key is
unconsumed, not corrupt. A recognized key with a payload version newer than its domain codec is
version skew and unconsumed by this release. A recognized payload at a supported version that
violates its closed domain shape is malformed. Domain payload decoding stays outside
`agentworks.db`; the repository does not import VM or overlay codecs.

Malformed metadata from a future record type projects the generic `record-malformed` issue. It is
not labeled as malformed applied state and cannot change the status of a known lifecycle-evidence
slot merely because its record key resembles a current applied-state key.

Live describe projects future record metadata as the `unconsumed_records` sibling of
`lifecycle_evidence`, because a future record type does not necessarily describe lifecycle evidence.
A focused describe has already established its owner and therefore does not project owner-existence
or orphan facts. Fleet doctor treats owner absence as an independent fact, so an orphan can also be
malformed or unconsumed and doctor may report both. The read never exposes raw future payloads, adds
a generic record API, or changes lossless partial replacement.

## Doctor behavior

Doctor remains non-migrating. A missing database stays a normal empty state. An older schema reports
the existing pending-migration guidance and does not attempt R5 reads. A newer schema reports the
existing downgrade/version-skew failure. R5 does not reconsider migration policy.

For a current schema, the Database group reuses its existing read-only connection and one snapshot
for contents, VM rows, and `inspect_all_instance_state()`. It does not open a new sidecar or broaden
R5 into consolidating every historical doctor database open. The configured private SSH identity is
derived at most once outside the snapshot, using the bounded native parser, and the result is
compared against all recognized VM SSH slices in memory.

Doctor emits presentation-neutral `HealthCheck` facts, with optional tagged instance-state metadata
so human and JSON renderers consume the same result. Its machine fact type for these comparisons is
`lifecycle-comparison`; the persisted record type remains the private `applied-state` discriminator.
The intended dispositions are:

| Fact                                                       | Doctor disposition         |
| ---------------------------------------------------------- | -------------------------- |
| matching recorded SSH identity                             | OK                         |
| recorded unverifiable SSH identity                         | INFO                       |
| absent SSH identity evidence                               | WARN                       |
| SSH identity drift                                         | FAIL                       |
| orphan record                                              | FAIL                       |
| malformed common envelope or known payload                 | FAIL                       |
| well-formed unconsumed record or unsupported known version | INFO                       |
| current configured identity unavailable                    | one explicit coverage WARN |

Lifecycle-record integrity runs even when config or manifest loading failed. A config or key failure
suppresses only current SSH comparisons. Registry publication failure suppresses graph-dependent
checks under the existing degraded-mode contract, while database counts, orphan checks, malformed
record checks, and unconsumed metadata remain available. One record's failure never suppresses a
healthy record's result.

R5 does not establish a general rule for whether every command may proceed with an incomplete
resource graph. Doctor already owns degraded inspection and gets the narrow behavior above. Other
commands retain their current failure boundaries.

## Failure and integrity behavior

| Condition                                    | Inspection outcome                                              |
| -------------------------------------------- | --------------------------------------------------------------- |
| No desired overlay                           | Instance spec visibly absent                                    |
| Missing selected template                    | Current unresolved; stored spec and lifecycle evidence retained |
| No recognized lifecycle evidence             | Empty evidence list; human output says none recorded            |
| Hardware marker absent                       | Hardware request not recorded, even though row values exist     |
| Hardware marker present and values equal     | Recorded request matches current declaration                    |
| Hardware marker present and values differ    | Drift with recorded/current field-level numeric facts           |
| Verified SSH identities equal                | Match                                                           |
| Verified SSH identities differ               | Drift                                                           |
| Either accepted SSH identity is unverifiable | Unverifiable, never drift                                       |
| Configured SSH key unreadable or invalid     | Bounded issue; comparison omitted                               |
| Known supported payload malformed            | Database-damage issue; other rows retained                      |
| Malformed future record type                 | Generic record-malformed issue; known slots remain independent  |
| Known key uses unsupported version           | Visible version skew/unconsumed metadata                        |
| Well-formed future key or record type        | Visible unconsumed metadata                                     |
| Owner row absent                             | Orphan database-damage issue                                    |
| Database schema is old                       | Existing non-migrating migration guidance                       |
| Config or registry unavailable in doctor     | Independent database inspection continues                       |

## Human presentation

Human resource show adds one Resolved spec section with the effective object followed by compact
path/source provenance. Human describe adds compact Current declaration, Instance spec, Lifecycle
evidence, and Comparison sections after existing persisted identity facts and before remote
observation details. Current declaration retains the complete effective spec but omits exhaustive
per-leaf Value sources. JSON describe and both human and JSON template `resource show` retain full
provenance. The simple case remains readable: absent overlays render as one short fact, not an empty
tree, and a resource kind with no lifecycle evidence says `not recorded` once.

Human doctor keeps its existing grouped check layout. It does not print payloads, complete specs,
fingerprints unless already part of the safe fact contract, or secret values. Machine and human
renderers use the same DTOs; tests assert facts and structure rather than authored prose.

## Validation

### Resolved projection

- Exactly the five selectable instance-template kinds implement the resource-show hook.
- VM CPU, memory, disk, and swap defaults are visible before create with defaulted provenance.
- Parent/child scalar, map-key, final-list-position, duplicate-list-contributor, nested-object, and
  subtree-replacement provenance remain truthful and ordered.
- Null and empty collection defaults receive provenance.
- Session use-view shell defaults and nested capability config resolve correctly.
- Non-hook resource JSON omits `resolution`, and all pre-R5 fields remain unchanged.
- Secret resolution, prompting, provider access, and adjacent-public-key reads remain unreachable.

### Live describe

- VM and admin slots resolve independently from one decoded composite overlay.
- Workspace, agent, and session expose their persisted overlay and strict current resolution.
- Workspace and agent preserve database facts and report `registry-unavailable` when expected
  operator-data failures prevent current resolution.
- A removed selected template is unresolved and never receives default-plus-overlay synthesis.
- All structural DB facts come from one snapshot, with no nested read transaction.
- Provider, SSH, secret, prompt, and runtime observation occur outside that snapshot.
- Hardware marker absence, match, multi-field drift, and unresolved-current cases are distinct.
- SSH not-recorded, unverifiable, match, drift, and unavailable-current cases are distinct.
- Password-protected native OpenSSH fixtures remain verifiable without prompting; accepted legacy
  formats remain unverifiable without becoming unusable.
- Existing JSON v1 fields and human facts remain intact; current producers always include tagged
  `instance_state`, while older JSON v1 producers may omit it.

### Repository and doctor

- One owner and fleet inspection return deterministic recognized, unconsumed, malformed, and orphan
  facts without raw payloads.
- One malformed row does not hide any sibling row.
- Unknown well-formed keys and record types are unconsumed, while malformed known fields fail.
- Unsupported known versions use version-skew guidance rather than corruption guidance.
- Doctor uses one applied-state batch query and one configured-key derivation, never one per VM.
- Doctor continues record integrity checks when config, manifests, registry, or current key fail.
- Human and JSON doctor facts agree without prose-policing assertions.

Focused unit and manager tests run during implementation. The final exact head runs the full Python
and repository gates, isolated-home CLI acceptance for human and JSON v1 surfaces, and live VM
validation of match, deliberate drift, and password-protected-key behavior under the integration
testing protocol.

## Deliberate boundaries

R5 does not:

- add a new command, database migration, generic record query, or raw payload projection;
- claim that any current database row alone proves lifecycle evidence;
- synthesize lifecycle evidence for historic instances;
- claim that the recorded hardware request is provider-observed realized hardware;
- remediate drift or make workspace repair converge;
- resolve secret values for inspection;
- hold a database transaction across remote or interactive work;
- decide which ssh-agent identity applies;
- reject a key format that existing transport accepts merely because R5 cannot fingerprint it; or
- establish a degraded-resource-graph policy for commands other than doctor.

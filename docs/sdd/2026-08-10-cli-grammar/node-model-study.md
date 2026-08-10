# CLI Grammar Rework, Cross-Plane Node Model Study

- Status: Architecture reconnaissance input; no design ruling implied
- Date: 2026-08-10
- Basis: `docs/cli-grammar-seed` at `284b447b`; merge-base `4d010d42`; mainline comparison
  `origin/main` at `e2bf898e`

## Existing models

The declaration registry is config-only. Finalization creates a frozen `DependencyGraph`; later
registration fails. Each graph node is a registry `(kind, name)` pair with immutable dependency
edges, enablement and readiness state, and capability implementation stamps. Configuration origin
remains on the registry resource row and can be joined into an inspection view; it is not retained
inside the graph node. References preserve their usage, relationship type, and declaring resource.

The registry currently exposes these resource kinds:

`admin-template`, `agent-template`, `apt-package`, `apt-source`, `git-credential`,
`git-credential-provider`, `harness-integration`, `named-console-template`, `secret`,
`secret-backend`, `secret-source`, `session-template`, `system-install-command`,
`user-install-command`, `vm-platform`, `vm-site`, `vm-template`, and `workspace-template`.

Live VMs, workspaces, agents, sessions, and consoles are database-backed instances. They have
domain-specific describe records and renderers, not registry nodes. The command-scoped orchestration
`Node` graph is also not a unified inventory: it omits consoles, secrets, held capabilities, and
whole-system state outside the current command. It does model command-local pending VM, workspace,
agent, and session nodes, which reinforces that it exists for execution ordering rather than
inventory. Reusing it would publish an incomplete truth.

## Candidate node universe

The proposed shared read model can be complete only if it represents all of the following without
mutating the registry graph.

### Declaration nodes and edges

- Every frozen registry resource.
- Its direct `USES` and `INHERITS` references, including relationship usage and declaration origin.

### Live-to-declaration selection edges

- VM to VM site, VM template, and admin template.
- Workspace to workspace template.
- Agent to agent template.
- Session to session template.
- Console to the currently effective singleton `named-console-template/default`.

These edges explain why an instance has its current configuration. They are not ordinary runtime
containment and should have a distinct relation kind. Consoles do not persist a template selector:
their layout is re-read from the singleton default during relevant operations. That edge is an
effective-current relation, not historical provenance and not an explicit selection.

### Live-to-live edges

- Workspace to VM.
- Agent to VM.
- Session to workspace and optional agent; VM is derivable through workspace.
- Agent-to-workspace grants, including whether they are explicit or implied by a session.
- Console to VM.
- Console membership in session order, including shell-pane metadata.

Grants and ordered membership carry more structure than a bare pair of node IDs. The design must
choose whether that structure is edge metadata or becomes an addressable relationship object.

### Derived views

Current configuration can derive a secret-to-session relationship by following template roots and
registry closure. Effective environment is a projection over an anchor and its relationships; it is
not itself a graph node. Both facts are useful, but neither is a direct stored edge. A renderer that
mixes them with direct facts must label their provenance and freshness.

## Available seams

- Declaration cards already have an inspection service for identity, origin, readiness, and
  kind-specific facts.
- Each live domain already has a typed describe record and human/JSON projector. Their fact sets and
  live probes are materially different.
- The immutable, eagerly built `GuideView` is a local safety precedent: construct a closed snapshot
  with only approved facts, then render from it.
- Registry origins are strong file-and-location records. Database instances generally expose
  `created_at`, but no equivalent definition of origin.

## Missing abstractions

- A typed identity spanning declaration and live kinds.
- A canonical relation record with direction, kind, provenance, ordering, and optional metadata.
- A live-kind describe dispatch boundary.
- A generic `KIND/NAME` target resolver and completion source.
- A definition of origin for database instances.
- Traversal semantics and deterministic graph renderers.
- A policy for live probes: generic describe can retain them, omit them for snapshot determinism, or
  make them an explicit mode, but cannot leave the choice implicit.

## Architecture options for HLA

Both credible options must not register database rows into `Registry`, mutate `Registry.graph`, or
present the incomplete orchestration graph as system inventory. Both keep direct facts distinct from
derived/effective edges and keep environment as a projection over eligible live anchors.

### Option A: request-scoped inspection snapshot

Build an immutable view from the already-finalized registry and a defined database read, then adapt
existing domain records into it. Renderers consume only the view. This offers one explicit
consistency and side-effect boundary and follows the local `GuideView` precedent, but risks a large
parallel fact model and duplicated domain query logic.

### Option B: composed domain-query facade

Resolve generic identities centrally but dispatch cards and relation collection to existing domain
query services. The facade normalizes only shared identity and relation records. This reuses mature
domain facts and live probes, but those services are not uniformly read-only today: session
description can enter an activation gate and start a VM, and session status collection can repair
persisted PID state. Consistency across several reads is also weaker. This option therefore requires
new side-effect-free fact collectors or an explicit operator-approved activation and state-repair
contract; calling the facade read-only does not make its dependencies so.

Either option is narrower than a living graph service. The HLA compares fact duplication,
consistency, activation and repair side effects, live-probe behavior, testability, and extension
ownership. The operator approves the selected boundary as part of HLA review before implementation
planning.

## Decisions the HLA must resolve before approval

1. Does day-one graph output contain only direct stored/declared relationships, or also derived
   secret reachability and implicit configuration selections?
2. Are template-selection edges first-class and visible by default?
3. What does origin mean for a live database instance: creation time, database provenance, both, or
   no origin field?
4. Are grants and ordered console membership rich edges or addressable relationship nodes?
5. Does unified describe retain each domain's current live probes, or render snapshot facts only?
6. Is the effective singleton named-console-template relationship visible, and how is its
   non-historical freshness labeled?
7. Does the public grammar call both declaration types and live instance types `kind`, while code
   retains narrower `resource kind` and `instance kind` terms?
8. What consistency boundary applies if registry configuration and database contents change while a
   generic inspection view is being assembled?
9. Which architecture option owns the shared read boundary, and may describe and graph choose
   different options if their consistency and probe requirements differ?

# Spike findings (FRD R11)

**Date:** 2026-07-17 **Verdict:** all four bets hold; proceed to HLA. Spike code: [`spike/`](spike/)
(11 tests, green; run instructions in its README).

The spike implemented the `Node` protocol, node adapters over REAL agentworks types (capability
instances via `ProxmoxPlatform` and `GitHubCredentialProvider`, a `ResolvedVMTemplate` absorbing
`preflight_vm_template`, a live VM from a real `VMRow`, pending workspace/agent/vm nodes, a stub
harness), a memoized walker, and the orchestrator-side helpers, against a REAL config, registry, and
`Resolver` (env-var backend, no stubs). Scenarios: `vm create` and `session create --new-agent`,
with expected values read off the imperative managers at HEAD (cited in the tests); executing real
provisioning is impossible in a spike, so the oracle is code-derived, not a live diff.

## Bet 1: one thin readiness contract fits dissimilar nodes. HOLDS

The readiness contract landed cleanly on real types.

> Post-spike revision (2026-07-17): the design later SPLIT this into `Readiness`
> (`preflight`/`runup` only) and `Node` (`Readiness` + `key`/`deps`), because capability instances
> turned out NOT to belong on the graph, an inline instance has no unique key (FRD R1/R2). The
> spike's `Node` protocol below, with `key`/`deps`/`preflight`/`runup` all on one object, conflated
> the consuming resource and its held instance in the thin case. The finding that the readiness
> CONTRACT fits dissimilar things survives verbatim; it just resolves as "the consuming-resource
> node has the contract and composes its held instance," not "the instance is a node."

```python
@runtime_checkable
class Node(Protocol):  # spike's shape; later split into Readiness + Node
    @property
    def key(self) -> str: ...
    def deps(self) -> tuple[Node, ...]: ...
    def secret_refs(self) -> tuple[str, ...]: ...
    def preflight(self, ctx: RunContext) -> None: ...
    def runup(self, ctx: RunContext) -> None: ...
```

- **Capability readiness adapted in ~20 lines.** The spike's `CapabilityInstanceNode` keyed from
  `owner_kind/owner_name`, took secret refs from re-running the pure `validate_config`, and
  delegated readiness. In the shipped model this is the CONSUMING-RESOURCE node's job (it holds the
  instance and composes its readiness); the instance itself keeps only `Readiness`. Either way the
  contract fit with almost no code.
- **The free-function readiness absorbed cleanly.** `preflight_vm_template` became the vm-template
  node's `preflight` verbatim. One seam: the node holds a resolver because today's function predicts
  through one; under R5's central prediction that constructor argument disappears. Recorded for the
  HLA, not fixed in the spike.
- **Two declaration surfaces, one adapter concern.** Capability instances declare secrets via
  `validate_config` references; the resolved template declares via `referenced_resources()`. The
  node adapters normalize both into `secret_refs()`. This is a concrete instance of the
  reference-graph-to-node-graph translation rule the HLA must pin (first-consumer review note 6):
  the rule is adapter work, and it is small but real.
- **`RunContext` needed ZERO changes.** Preflight ran on `RunContext(config=...)`, runup on
  `RunContext(config=..., secrets=resolver)` after a real prompt-free resolve. (Post-spike revision,
  2026-07-17: the design later ADDED a static `OperationScope` field and turned targets/secrets into
  gated accessors, see the HLA context section. The spike's narrower claim still holds, the harness
  SDD's PER-INVOCATION threaded `OperationIdentity` is superseded, but it is superseded by one
  operation-scope object on the context, not by keeping the context identity-free.)

Not exercised, honestly: platform/provider runups (they probe real endpoints; runup timing was
proven on the harness stub instead) and ops (out of protocol by design).

## Bet 2: identity is intrinsic, injected for leaves. HOLDS

The pending agent node carries its own chain (`name`, `vm_name`, `workspace_name`) from
construction; the harness stub was constructed WITH its session name (orchestrator injection, FRD
R3) and its probe record shows both arriving at the leaf: the injected `s1` plus the target's
intrinsic `dev`/`box`/`ws1`. No identity object, no `level` enum, no per-context threading, and no
graph-walking by the node.

## Bet 3: the memoized walk reproduces the hand-rolled fan-outs. HOLDS

- `vm create`: the walk from the pending VM yields exactly the imperative Preflight-phase set
  (vm-template, the site's platform, each git-credential provider, dependencies before the VM), and
  the central `secret_union` yields exactly the one-resolve-pass union (Tailscale key, API token,
  PAT).
- `session create --new-agent`: the git-credential provider enters the plan ONLY through agent ->
  agent-template -> provider edges; the "command" names none of it. The hand-rolled ephemeral fold
  is reproduced as ordinary graph behavior. (Precise HEAD mechanism, corrected 2026-07-17: the
  session inlines `resolve_git_credential_providers` + a per-provider `preflight`, then threads
  `_resolve_git_tokens(...)` as `git_tokens` into the nested `create_agent`, which suppresses its
  own phases via `own_root = platform is None`. `_preflight_resolve_agent_git` is `create_agent`'s
  OWN-root helper and the session path bypasses it; an earlier draft of this doc misattributed the
  fold to it.)
- Memoization reproduces `bind_platforms`' by-site dedup: two VMs, one platform visit. Cycles are
  loud errors.

## Bet 4: realization-order unwind reproduces today's rollback. HOLDS

The minimal shared state is an ORDERED LIST of realized nodes, nothing more (this answers the
first-consumer review's note 1: the record unwind reads is a list the orchestrator appends to; no
`Plan` class emerged even in code). Reverse-order teardown reproduced both oracles: `create_vm`'s
rollback (just the VM row) and `_rollback_ephemerals`' agent-then-workspace order (reverse of
workspace-then-agent creation). Failure injected before the session realizes unwinds exactly the two
ephemerals.

## The `to_create` dissolution, demonstrated

The required-commands float worked with no existence signal on the context: the harness defers when
its target node is PENDING, probes at preflight when the target is realized (the earlier-failure win
for existing agents), fires exactly once (the fired-once guard fell out of recording the probe), and
raises loudly when the target is MISSING rather than pending (anti-silent-skip, FRD R3). The harness
SDD's `to_create` and fired-once machinery are fully covered by pending-ness plus one `if`.

## Surprises worth carrying into the HLA

1. **Prompt-backend prediction is optimistic by design.** `preview_resolution` reports an
   interactive backend as resolvable without probing (probing would BE the prompt), so with the
   default chain every secret predicts resolvable. The spike pinned `backends = ["env-var"]` to get
   a deterministic oracle. Consequence for R5: central prediction inherits exactly today's
   semantics, including this; doctor parity is unaffected, but the HLA should state that
   prediction's meaning is unchanged by centralization.
2. **The walker wants multi-root.** `bind_platforms`-style batch commands root the walk at many
   nodes; `walk(*roots)` with shared memoization handled it naturally. The helper's signature should
   be multi-root from day one.
3. **Node keys did real work.** Memoization, cycle reporting, and the unwind log all keyed off
   `kind/name` strings. The HLA should pin the key convention (it is the node-graph analog of the
   registry's `(kind, name)`).

## What the spike does NOT claim

No real provisioning ran; the oracles are read off the manager code, and the behavior-parity
requirement (R7) is still carried by the real suite during migration, not by this spike. Ops,
rollback SIDE EFFECTS (only ordering was validated), prompt flows, and doctor were out of scope. The
spike is throwaway; nothing may import it.

# Orchestration-layer spike (FRD R11)

Throwaway code validating the FRD's load-bearing bets on real agentworks types. Not production code;
nothing under `cli/agentworks/` is touched. Findings:
[`../spike-findings.md`](../spike-findings.md).

Run from `cli/`:

```sh
uv run pytest ../docs/sdd/2026-07-16-orchestration-layer/spike/ -q
```

- `spike.py`: the `Node` protocol, node adapters over real types, pending nodes, the memoized
  walker, and the orchestrator-side helpers.
- `test_spike.py`: the two FRD scenarios (`vm create`, `session create --new-agent`) asserted
  against oracles read off the imperative managers at HEAD.

This directory is deleted (or kept as frozen reference) when the SDD locks; nothing permanent may
depend on it.

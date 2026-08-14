# Pre-Wave-1 Baseline

Measured by the effort lead at `main` = `6771c02a` (the wave 0 merge), before the first wave 1
deletion. FRD R4.1 asks the reassessment for retrospective numbers; these are the "before" side.
They are evidence, not targets: the test of this pass is fewer concepts, paths, and contracts, and
any of these counts could move the wrong way for a good reason.

| Measure                                                | Value   |
| ------------------------------------------------------ | ------- |
| `cli/agentworks` Python lines                          | 83,151  |
| `cli/tests` Python lines                               | 119,584 |
| `website` Python + JS lines (excluding `node_modules`) | 7,394   |
| Collected tests (`pytest tests/ -m 'not integration'`) | 8,160   |
| Full suite wall time (default xdist workers)           | 68.6 s  |
| Always-on rule bytes delivered per session             | 33,863  |

Reproduce, from the repository root:

```bash
find cli/agentworks -name '*.py' | xargs wc -l | tail -1
find cli/tests -name '*.py' | xargs wc -l | tail -1
find website \( -name '*.py' -o -name '*.mjs' -o -name '*.js' \) ! -path '*/node_modules/*' | xargs wc -l | tail -1
(cd cli && uv run pytest tests/ -m 'not integration' -q)
cat CLAUDE.md $(ls .claude/rules/*.md | grep -v cli-conventions) | wc -c
```

Notes on two of the measures:

- **Always-on rule bytes** counts what every session actually receives at launch, which is why it
  excludes `cli-conventions.md` (deliberately narrow `paths:`) and includes `CLAUDE.md`. Wave 0
  changed what this measures, not just its value: before PR #515 only `CLAUDE.md` and the four
  `always-consider-*` rules loaded unconditionally, so the twelve broad rules were carried in the
  count but not in the context. Wave 2 must drive this number down (FRD R3.2), and the comparison is
  honest only against this post-wave-0 basis.
- **Suite wall time** is the default parallel run on this workstation. It is a rough figure for
  comparison against a later run on the same machine, not a portable benchmark.

The `findings.md` window arithmetic was taken at `fe83aaf7`; the two `cli` line counts here match it
exactly, so nothing landed between that basis and wave 0 that moves them.

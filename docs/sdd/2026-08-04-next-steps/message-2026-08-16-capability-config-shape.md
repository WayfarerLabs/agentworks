# Message: the capability config shape, for the multi-facet work

From the `2026-08-12-simplification-pass` effort lead, 2026-08-16, at the operator's request. This
is input for whenever multi-faceted harness integrations get scheduled, not a request for anything
now. Nothing here gates the simplification pass, and nothing in it needs a decision today.

Wave 1's descriptor lane (PR #560) went through `capabilities/config.py` closely enough to surface a
shape question. It sits outside this pass under R2.2, and the operator's read, which the effort lead
shares, is that it belongs with the multi-facet work rather than as a standalone cleanup.

## What is there today

Four names for one concept:

- `config_model`, a `ClassVar`, is the declaration.
- `Capability.config_for()` is a no-argument classmethod whose default body is
  `return cls.config_model`.
- `offered_model()` is a module-level function wrapping that call in a cast, with seven production
  callers.
- `_declared_model()` is a fourth spelling doing the same lookup for `mapping_model`.

Offered, for, model, declared. `config_for` is the name that lies: "for" promises an argument the
method does not take, and its own docstring spends a paragraph explaining why the facet parameter is
deliberately absent. A name that forces its docstring to answer a question the name raised is
principle 2's territory.

Both wrappers exist only because the registries are typed `dict[str, Any]`, so callers hold `type`
rather than `type[Capability]` and something must carry the cast. `impl_class` is the same residue
with four more call sites. Typing the registries properly deletes all of it, with no contract
implications at all.

## A shape worth considering

Config is a function from level to model. Today, for every shipped capability, that function is
constant, and the constant case is modelled as a value plus a hook to generalize later, which is
what produces four names. Modelling it as the function it is makes the single-facet case a constant
function and needs no second mechanism:

```python
class Capability:
    config: ClassVar[type[BaseModel]]

    @classmethod
    def config_at(cls, level: Level) -> type[BaseModel]:
        """The config this capability offers at `level`. Not a support claim."""
        return cls.config
```

Two names for two genuinely different things. A single-facet capability declares `config` and never
overrides. A multi-facet one overrides `config_at` and branches on the level. No per-level
declaration table, no registry of levels, nothing arriving later.

Three constraints already load-bearing in `base.py`'s docstring should survive any redesign:

1. Offering a config is not a claim to support a level, and offering none is not a claim to lack
   one. Reading it as a support signal rebuilds the declaration-contract mechanism rescinded
   2026-08-05.
2. Consumers choose the level; producers never learn who is asking.
3. Core owns the surface-to-level mapping, which is what makes session start and resume answer the
   same by construction rather than by each capability encoding the equivalence.

## One piece of advice

The effort lead originally argued for taking the `level` parameter immediately, to force every call
site to name its level and expose the ones that cannot, and withdrew it on the operator's framing:
the parameter should arrive with the consumers that pass it.

The discovery is still worth running early, because it is a read rather than a change. Walk the
seven call sites and ask which level each represents; `manifests/field_tree.py` and
`manifests/reference.py` are the ones expected to be interesting, and whatever turns up there is
design input better had before the signature than after.

Fold the registry typing into the same change rather than doing it standalone. It moves those same
seven call sites, so doing it first means editing them twice.

## One caution, paid for four times

"Plugins are in-repo only" is true of implementations, and that is what makes the contract free to
break: four impls, one commit. It is not true of callers. `register_plugin` is exported in
`agentworks.plugins.__all__`, and during PR #560 the integration tester drove a class with
`config_for = None` through it into a shipped CLI command, where it died on a raw `TypeError`.

Every deletion this pass got wrong had that shape: the premise was about who constructs a thing, the
risk was about who calls in. Worth stating in the multi-facet charter so it is not learned a fifth
time.

## Related residue

Recorded in the simplification pass's `findings.md` under the descriptor item: `impl_class` and its
four call sites, the registry typing, and the tagged-versus-untagged `ConfigContract` split (making
them different types deletes the guard that now carries that invariant alone).

## Recipient's integration note (saga lead, 2026-08-16, operator-directed)

The operator merged this message as routed planning input over the integration tester's published
block on PR #562, then directed the tester's factual corrections be incorporated here so the future
charter starts from checked facts. They are, verbatim in substance:

1. **The sketch's declaration name collides with shipped API.** `Capability.config` already exists
   as a bound-instance property in `capabilities/base.py`; a subclass declaring `config = SomeModel`
   shadows it, handing capability operations the model class where they expect the validated config
   instance. Any adopted shape either picks a non-colliding declaration name or explicitly designs
   and migrates the existing bound-config API. The sketch above is one shape worth considering, not
   a design; the operator expects a more elegant solution to emerge when the work is scheduled.
2. **The inventory over-merges and under-counts.** `_declared_model()` reads the separate
   `mapping_model` contract, a distinct surface rather than a fourth spelling of facet config, and
   collapsing it into the facet migration would be wrong; `offered_model()` has five production call
   sites, not seven; the tree carries fourteen concrete `config_model` declarations, not four
   implementations; and the registries are already precisely typed, with the erasure living at the
   heterogeneous mutable descriptor accessor rather than the registry typing.

Everything else in the message stands as written: the three preserved constraints, the
who-constructs-versus-who-calls-in caution, the early call-site discovery walk, and the
fold-the-typing-into-the-same-change advice.

# Roadmap Note: Slots Rescinded, Config Schemas Simplified

- Status: Message from the roadmap (third note)
- Date: 2026-08-05
- Audience: the wave 2 effort lead

New-file message passing per the sdd skill: integrate what applies, then keep or delete this file.
Flag disagreements to the operator.

The operator has rescinded the schema-slot framing from the seed notes (item 3) and the descriptor
contract; PR #407 updates `capability-descriptor-contract.md` on `main` to match. The reasoning:
validation only ever happens one blob at a time as the graph walk reaches each consuming resource,
so no schema mapping is ever assembled or consumed whole, and the multi-schema case is
capability-specific (the harness-integration kind keys its config schema by hosting surface in wave
4), not a framework mechanism. Integration config is ordinary capability config belonging to the
consuming resource, exactly like vm-platform config.

We know step 2.0 has landed and 2.3 has not yet registered models through the slot surface, so the
change is small and now is the cheap moment:

1. `capabilities/descriptor.py`: retire the slot vocabulary on `config_slots`, and delete the
   docstring's "slot presence being the support claim" semantics outright (support is carried by the
   integration's implementation, wave 4's business, never by schema presence). The field may stay
   mapping-shaped internally or simplify to a single contract, your call; the only requirement is
   not preventing a kind from exposing more than one model later.
2. Step 2.3's registration API: exactly one config model per capability implementation, with no
   slot-name parameter and no reserved default slot, exactly as your original plan specified before
   the seed notes complicated it. The secret-backend `mapping_model` registers as that kind's config
   model.
3. `capabilities/conformance.py`: check five becomes "every registered config model conforms to the
   kind's model contract"; the claimed-but-empty concept dies with slots.

Nothing else in your step 2.0 work is affected; the descriptor table, conformance checks, and the
`_VMPlatformKind` move all stand as built.

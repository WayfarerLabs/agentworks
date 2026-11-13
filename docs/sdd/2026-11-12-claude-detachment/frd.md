# Claude conversation detachment detection

Scope: standalone issue [877](https://github.com/WayfarerLabs/agentworks/issues/877), revised v1
accepted by the operator on 2026-11-12. No pre-existing SDD governs this work. The operator
additionally requested a reusable field marker for instance-only config.

- Detect explicit continuation and parked-job pointers tied to the stored conversation.
- Refuse normal starts when a full candidate UUID can be identified, with the stored UUID, candidate
  UUID and an exact instance `force_uuid` recovery setting.
- Warn and retain existing launch policy when only missing history or an unresolved short job
  identifier suggests detachment. Unreadable detection evidence must not manufacture a refusal.
- Allow `force_uuid` to adopt an exact existing transcript or live background conversation on
  creation and subsequent starts. Validate UUID syntax, suppress detection while forced, and warn on
  every use. Persist the adopted UUID only after successful verification.
- Reject instance-only fields in template declarations using a shared config mechanism and
  declaration provenance. Omission is permitted; explicitly setting even null is a declaration.
- Preserve strict resume and explicit fresh-launch semantics. Conflicting force-new and forced
  adoption must fail before changing state.
- Never delete or stop Claude conversations, infer candidates by recency, or auto-follow pointers.

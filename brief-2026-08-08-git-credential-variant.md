# Task brief: git-credential variant restructure, survey first

- Date: 2026-08-08
- Requester: the roadmap lead (operator ruling, 2026-08-08; see `target-state.md`'s
  variant-modeling contract and its companion rulings, and the release mapping in `phasing.md`:
  this lands before the 0.14.0 cut)
- Reviewer: the roadmap lead
- Disposition of this file: **delete it before the PR goes ready.** It is a dispatch brief, not an
  artifact; nothing in it should need promoting, because the authoritative spec already lives in
  the roadmap documents and `cli/agentworks/capabilities/README.md`.

## Task 1 (do this first, and report before restructuring): survey for siblings

Sweep every config model surface (core kinds, capability configs, plugin configs, settings) for
other places the variant-modeling contract should apply, so this change fixes the class, not one
instance. Hunt the shapes that history says hide here:

- presence or absence of an optional block or key selecting a mechanism or code path (the old
  `vm_host` / `service_principal` disease);
- boolean or stringly fields that are really mode selectors with arm-specific siblings;
- fields whose meaning changes based on another field's value (the cross-field constraint schema
  cannot state);
- places where a known-coming variant (like minting) would force a breaking restructure if not
  shaped now, while 0.14 is still uncut.

Classify each candidate with the shape test (do the required field sets differ?) and report the
inventory with per-item verdicts (restructure now / leave alone with reason / defer with trigger)
to the roadmap lead BEFORE starting task 2. The report decides whether this PR carries one
restructure or several.

## Task 2: the git-credential one-arm union

Per the recorded spec (`target-state.md` variant contract companion rulings; the modeling rule in
`cli/agentworks/capabilities/README.md`; the PR #444 pattern as the playbook):

- The credential's token acquisition becomes a one-arm discriminated union (stored), defaulting to
  the stored arm per the omission-history rule, with the scalar shorthand kept as the stored arm's
  spelling so existing `token: <name>` manifests keep working.
- Ambition ceiling: do NOT build the minted arm; it arrives additively later with the minting work.
  The boundary ruling stands: minting parameters (scopes, repos, permissions) are credential-domain
  creation specifications, never secret-source or per-secret-mapping content.
- Everything rides per the established pattern: schema emission with proper discriminator
  structure, extraction agreement for the defaulted and shorthand spellings, samples and
  describe-kind, retirement errors with exact rewrites for any written old shape that changes, the
  upgrade guide entry, sample-config and completions if touched.
- Gates and process as always: full gate order, the union-required/no-new-arm-default invariants
  pinned, definition of done is the #444 acceptance shape applied to this kind.

Any candidate from task 1 the roadmap lead rules in scope joins this PR as its own commit(s),
following the same pattern.

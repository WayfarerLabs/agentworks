# Migration strategy

Status: implementation rollout for [accepted migration groups A-D](migration-inventory.md);
marketplace sources are deferred.

## Starting state

Snapshot `3ad615680ee8e1ef56c864170deaf62392578661` on 2026-11-13 has six interpolating field
declarations across five models. Eligibility is implicit in calls from the env adapter, Shell
integration and native argument helper. The shared renderer already owns syntax and context
projection. Models and stored declarations retain authored text. There is no dedicated templating
guide topic.

The audit recommends eleven new declarations in installer, dotfiles, Codex writable-dir and Shell
required-command fields. A shared native marketplace-source field remains a choice; broader source,
artifact and infrastructure interpretation is held separately. These counts describe declarations,
not resource kinds or runtime call sites.

## Target state

Annotations define eligible values in each adopting model. A shared model-aware adapter reads that
declaration at an explicit runtime boundary. Schema and field documentation derive from the same
annotation. Existing and agreed new consumers use that pattern; unmarked fields retain their own
literal interpretation.

No persisted schema, database migration, manifest tag or operator opt-in flag is required. For
example, an already supported plaintext env entry keeps its exact source:

```yaml
env:
  TOOL_CONFIG_DIR: "${agw:user_home_dir}/.config/tool"
```

Only its implementation eligibility declaration changes. An agreed new target field, such as Codex
`writable_dirs`, gains reserved-namespace interpretation. Document `$${agw:NAME}` escaping for users
who need literal namespace-looking text there.

## Sequence

1. Complete the design and record the operator's field-list disposition.
2. Add the marker and its schema/documentation projections, with a synthetic model proving
   propagation through the actual supported shapes.
3. Migrate env end to end, then Shell and native extra arguments. Remove replaced eligibility
   branches as each consumer moves; do not ship a documentation-only marker.
4. Adopt agreed new field groups with their runtime context and destination handling.
5. Update permanent architecture, generated reference presentation, author guidance and the
   dedicated guide together with the code. Review built-in examples where useful.
6. Complete private review and standard independent PR validation before merge.

All steps are work units within one feature PR. Intermediate commits are private working states, not
independently ready releases. The initial draft is for design feedback.

## Safeguards

- Render from original selected declarations for each invocation; never store rendered usernames,
  paths or context facts back into reusable templates or instance specs.
- Compare old and new existing-consumer behavior, including losers, literal secrets, unselected
  commands, aliases, warning suppression, doctor and offline preview.
- Preserve path conventions intentionally. Profile PATH serialization and admin dotfiles `cd`
  quoting need concrete treatment in their adoption work; a marker alone is insufficient.
- Preserve authored list merge/deduplication rules. Do not normalize or deduplicate rendered paths
  as an incidental behavior change.
- Keep owner-templated resource defaults separate. This work does not change graph edges, source
  packaging, controller paths, remote architecture discovery or SSH transport.
- Existing alias removal text in code/docs still differs from issue #878's two-minor decision.
  Resolve that alignment explicitly with the operator's release policy; do not infer a new cutoff
  from this SDD or extend aliases to newly annotated fields.

Before merge, ordinary code correction or revert of private commits is sufficient. After release,
preserve accepted config spellings and use the normal compatibility pattern for any later removal
rather than leaving two competing interpolation systems.

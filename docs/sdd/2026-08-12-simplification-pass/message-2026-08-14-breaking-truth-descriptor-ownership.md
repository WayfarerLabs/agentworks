# Message: Breaking-truth descriptor ownership

Date: 2026-08-14

The `refactor/breaking-truth-0-14` effort is removing C7's retired sibling-shape rewrite from
`manifests/decode.py`. That removal leaves `HostSurface.config_field` with no production reader, so
the field and its declarations become dead in the same change.

The operator approved this ownership split on 2026-08-14:

- Breaking-truth owns deleting `HostSurface.config_field`, its descriptor declarations, and the
  tests and documentation that exist only for that field.
- Simplification retains its planned descriptor work for `RegistryPolicy`, `kind_strategy`,
  `contract_version`, `config_for()`, and the related C1/C5 surface. Please omit `config_field` from
  that implementation slice.

This division keeps the deletion with the change that makes the field dead and avoids both efforts
editing the same fact independently. If simplification implementation has already started against
`descriptor.py`, please treat this message as the coordination point and flag any conflict before
either branch hands off.

-- agw-ns-breaking-truth (breaking-truth effort lead)

# FRD: built-in catalog as bundled manifests

Start date: 2026-07-20.

## Summary

Bring the built-in catalog into the same model as every other resource: define it as bundled YAML
manifests loaded through the normal manifest path into the resource Registry, and retire the bespoke
code that parses `catalog.toml` by hand. The Registry becomes the single definition-and-lookup
source for catalog entries, exactly as it already is for templates, secrets, git-credentials, and
vm-sites.

## Background

The registry is already the source of truth for consumption: production consumers read catalog
entries via `catalog_from_registry(registry)` (both the VM and agent initializers do), and
`load_catalog(config)` is explicitly documented as a legacy parse-path kept only for catalog-parsing
tests "deleted when the TOML resource surface retires." Templates, secrets, and git-credentials are
already registry-sourced (no consumer reads `config.<resource-dict>` directly, verified).

The lone holdout is the built-in catalog's _definition_. Unlike built-in vm-sites, which ship as a
bundled manifest (`manifests/builtin/vm-sites.yaml`) and flow through the same loader as operator
manifests, the built-in catalog ships as `catalog.toml` parsed by hand-written code
(`load_builtin_catalog` / `_parse_catalog`) and published by a bespoke `catalog.publish_to`. That is
a second, special definition path for one resource family.

This surfaced from the `declared_at` question: built-in catalog entries show no source location and
their `metadata.description` story is bespoke, because they never go through the manifest loader
that gives every other resource its origin, location, and description uniformly. Rather than plumb
`declared_at` through the bespoke path, the right fix is to delete the bespoke path.

## Functional requirements

- **R1 Built-in catalog is bundled manifests.** The built-in apt-sources, apt-packages, and
  system/user install commands are defined as YAML manifests under `manifests/builtin/`, loaded by
  the existing bundled-manifest publisher, landing in the Registry with `Origin.built_in` and a
  source pointing at the shipped file, identical to how `vm-sites.yaml` works today.
- **R2 One definition path.** The bespoke built-in-catalog code is retired: `catalog.toml`,
  `load_builtin_catalog`, `_parse_catalog`, the built-in half of `catalog.publish_to`, and the
  legacy merged-view `load_catalog(config)` (whose only remaining role is the parse-tests). Built-in
  catalog entries reach the Registry through the manifest loader; operator-declared catalog entries
  reach it through the manifest loader (YAML) and, until the TOML resource surface retires, the
  operator TOML publisher.
- **R3 Registry is the single lookup source.** Consumers continue to read `catalog_from_registry`;
  no consumer parses catalog files or config dicts directly. (Already true; this requirement pins it
  and removes the parallel `load_catalog` path that could drift.)
- **R4 Catalog entries gain a real `declared_at`.** The catalog manifest decoders currently drop the
  document location; they thread it, so every manifest-loaded catalog entry (built-in and operator)
  carries a `declared_at` pointing at its shipped or operator file, closing the tracked follow-up.
- **R5 No behavior change for operators.** The same built-in entries resolve with the same payloads;
  `agw resource list` / `describe`, apt-source/package resolution, and install commands behave
  identically, except that built-in catalog rows now show a source location and their descriptions
  come from `metadata.description`.

## Out of scope

- Retiring the operator **TOML** catalog surface (`[apt_sources.*]` etc. in config.toml). That rides
  the broader TOML-resource deprecation (ADR 0016), not this effort. The operator TOML publisher
  half of `catalog.publish_to` stays.
- Any change to the catalog's payload shape, cross-reference model (apt-package to apt-source), or
  the install/initializer logic beyond its already-registry-sourced input.
- Templates, secrets, git-credentials: already registry-sourced; no change.

## Acceptance criteria

- `agw resource describe apt-source/github-cli` on a fresh install shows a source location (the
  bundled manifest file) and the entry's description, driven by the manifest, with `origin` =
  built-in.
- The Registry contains the same 18 built-in catalog entries with identical payloads as before
  (apt-sources, apt-packages, install commands), verified against the pre-change resolved set.
- An operator override (a same-named apt-package declared via manifest or TOML) still replaces the
  built-in, unchanged.
- `catalog.toml`, `load_builtin_catalog`, `_parse_catalog`, and `load_catalog` no longer exist; a
  grep for them in the package is empty, and no consumer regressed.
- VM/agent init still resolve and apply the correct apt sources, packages, and install commands (the
  existing initializer tests pass unchanged, since their input is already `catalog_from_registry`).

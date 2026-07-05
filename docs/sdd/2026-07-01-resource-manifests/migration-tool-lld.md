# Migration tool LLD: `agw resource migrate` and `agw resource sample`

Phase 4 design, revised 2026-07-05 with the maintainer for the dual-path era. The migration tool is
NOT a one-time cutover converter: it is a recurring mover ("any time you want to move resources from
TOML to YAML"), usable incrementally and repeatedly. That reframing drives every choice below.

Both commands live under `agw resource` because their object is resources; the `config.toml` edit
that migration performs is a side effect on the source, not the subject.
(`agw config init / edit / sample` continue to own the settings file, which is permanent under
config-is-config; they are already in their final home.)

## `agw resource migrate`

```text
agw resource migrate [SELECTOR]... [--layout per-kind|single|per-resource]
                     [--toml comment|delete] [--dry-run] [--yes]
```

### Selectors

- No selectors: migrate every operator-declared resource currently sourced from TOML.
- `KIND` (e.g. `vm-template`): every TOML-declared resource of that kind.
- `KIND/NAME` (e.g. `vm-template/small`): that one resource. The token splits at the FIRST `/`; a
  name containing `/` (possible for non-secret kinds, whose names are pass-through) cannot be
  addressed individually -- select its kind or use the bare form.
- Overlapping selectors (`secret secret/foo`) union: each matched resource migrates exactly once.
- Selectors use registry kind identifiers (lower-kebab), not TOML section names. The section-to-kind
  mapping is `decode.KIND_SECTIONS`, shared with the manifest decoder -- one source of truth.
- An EXPLICIT selector matching nothing in the TOML is a hard error before anything is written --
  the operator named something specific, so silence would mislead (the error for a known kind says
  "no TOML-declared resources of kind X"; the resource may already be YAML-declared or
  auto-declared, neither of which is migratable). The BARE form with nothing left to migrate reports
  "nothing to migrate" and exits 0, keeping scripted re-runs idempotent.
  `resource migrate secret-backend` is still an ERROR (nonzero exit, like any explicit selector that
  cannot match) but with a tailored message: those sections are warned no-ops with no manifest
  successor; a bare run offers to drop them instead (below).
- The migratable set is exactly the operator-declared rows whose origin is the TOML config file.
  Auto-declared and built-in rows never match a selector; YAML-declared rows are already migrated.

### Layouts (`--layout`, default `per-kind`)

| Layout         | Target for a `vm-template` named `small`       |
| -------------- | ---------------------------------------------- |
| `per-kind`     | `resources/vm-templates.yaml` (multi-document) |
| `single`       | `resources/resources.yaml` (multi-document)    |
| `per-resource` | `resources/vm-template/small.yaml`             |

- Per-kind filenames pluralize the kind with a plain `s` (matching the bundled
  `secret-backends.yaml` convention).
- Per-resource layout REFUSES names that are not filename-safe (`/`, `\`, leading dots, anything
  outside a conservative safe set -- non-secret names are pass-through and may contain such
  characters), pointing at the per-kind layout. No sanitization: a mangled filename would break the
  "filenames are convention" story from the other direction.
- Filenames are convention only -- the loader walks everything and does not care -- so `--layout` is
  operator ergonomics, not semantics. Operators are free to reorganize afterwards.
- Documents are emitted in TOML declaration order.

### Append-only YAML

The tool NEVER parses or rewrites existing YAML. If a target file already exists (previous run,
hand-written, either), new documents are appended as text with a `---` separator, preceded by a
newline when the existing file lacks a trailing one. Operator YAML, once written, is operator-owned;
the tool only adds. This single property is what makes incremental and repeated runs safe to reason
about, and it removes any need for an overwrite `--force`.

### TOML edit (`--toml`, default `comment`)

Under dual-path, a resource declared in both sources is a hard load error citing both locations, so
"migrate and leave the TOML alone" would break the very next `agw` invocation. Editing the TOML is
therefore mandatory; the flag only chooses how:

- `comment` (default): the migrated section is commented out in place, preceded by a marker line
  `# migrated to resources/<file>` (one marker per section). Reversal = uncomment AND delete the
  corresponding YAML documents (uncommenting alone recreates the cross-source duplicate error); the
  operator's own comments inside the section survive as comments.
- `delete`: the migrated sections are removed. Comments inside them go with them (their content now
  lives in YAML; no comment transplantation).

Mechanics, either mode:

- tomlkit round-trip: comments and formatting of every surviving section are preserved.
- Timestamped backup of the original `config.toml` to `paths.backups` before ANYTHING is written,
  manifests included, so every partial state is recoverable from it.
- The rewrite is atomic (write-new-then-rename).
- `[secret_backends.*]` sections (warned no-ops) are dropped with a note in the summary on any run
  that rewrites the TOML. A bare run whose only remaining deprecated TOML is these sections still
  offers the drop -- otherwise the tool could never silence that residue ("nothing to migrate" would
  short-circuit every invocation).
- Multi-section resources (`[x]` + `[x.env]` and similar) are treated as one unit -- all sections
  belonging to a selected resource are commented/deleted together -- including when the sections are
  non-contiguous in the file (each is edited where it sits).
- Supported declaration shapes: standard `[section.name]` header tables (and their sub-sections).
  Dotted-key or inline-table declarations under a parent header (`[secrets]` holding
  `foo = { ... }`) are REFUSED with their file/line and a hand-migration hint: "commented out in
  place" has no faithful rendering for a key buried in a shared table, and guessing would risk the
  surviving config.

### Verification (the trust feature)

`build_registry` is pure, so after a real run the tool rebuilds from the rewritten TOML plus
manifests and verifies the finalized registry is identical to the pre-migration one. The comparison
is KEYED by `(kind, name)` -- iteration order legitimately changes when rows move between
publishers, so an ordered comparison would false-positive on every partial migration -- and rows are
normalized recursively for declaration location and origin variant, including the
first-matching-reference attribution locations inside auto-declared rows, whose referencers may have
moved (normalization shared with the decode-parity tests). On success it prints
`verified: registry unchanged (N resources)`.

On mismatch (a migrator bug by definition): restore `config.toml` from the backup, remove the files
AND directories the run created (per-resource layout creates kind directories), truncate appended
files to their recorded pre-run lengths, and exit with an error carrying the first difference. Every
run self-checks; no run can leave a silently-different registry.

### Flow

1. Load the current config + registry. A broken config fails here -- including the cross-source
   duplicate state an interrupted run leaves. Recovery from that state is manual, from the pre-write
   backup (restore `config.toml`, delete the YAML documents the preview listed, or hand-finish the
   TOML edit). Deliberate: an auto-resume path was considered and rejected as YAGNI for a crash
   window this small; the mandatory backup plus the loud duplicate error make manual recovery
   mechanical, and the migration-strategy doc carries the operator-facing version of this guidance.
2. Resolve selectors to a concrete resource list. Explicit selectors matching nothing error; the
   bare form with nothing left reports "nothing to migrate" and exits 0 (after offering the
   `[secret_backends.*]` drop if those sections remain).
3. Print the plan: documents to be written per target file (created vs appended), TOML sections to
   be commented/deleted, `[secret_backends.*]` drops if any.
4. `--dry-run`: stop after the preview, plus print the full YAML documents and the TOML diff. Writes
   nothing.
5. Confirm (skipped by `--yes`), then: back up, write/append manifests, rewrite TOML, verify.

### Renames and special cases

- `git_credentials.<name>.type` emits as `spec.provider` (the manifest surface never had `type`).
- `description` moves from the section body to `metadata.description`.
- The `admin` singleton (`[admin.config]` / `[admin.env]` / sub-sections) emits as one
  `admin-template` document named `default`, and `[named_console]` as one
  `named-console-template/default`, matching the envelope restrictions.
- Field mapping is mechanical (section fields minus name/description become `spec`); correctness is
  enforced by the verification step and the golden tests, both of which round-trip through
  `load_manifests` -- the loader and migrator cannot disagree.

## `agw resource sample`

```text
agw resource sample [KIND] [--write FILENAME]
```

- No arguments: print commented sample manifests for every manifest-declarable kind (multi-document)
  to stdout. With `KIND`: just that kind's sample.
- Samples ship bundled with the app (like `sample-config.toml`), one per kind, and are FULLY
  commented out: every line carries a leading `#`, so written samples are inert text the loader
  ignores -- `--write` can never create a duplicate or a live resource by accident, and running it
  twice just appends more comments. The operator uncomments and edits to activate. The loader
  guarantee stays real rather than vacuous: the test mechanically strips the `#` prefixes and loads
  the result through the real loader.
- `--write FILENAME`: instead of stdout, write to `<resources-dir>/FILENAME`. Rules:
  - Relative paths only, resolved under the resources directory; escaping it is an error.
  - Must end `.yaml` / `.yml` (otherwise the loader would never pick it up; error with that hint).
  - Parent subdirectories are created.
  - If the file exists, the sample is APPENDED as a new document with a `---` separator -- same
    append-only rule as the migrator; the tool never rewrites existing YAML.
- `agw config sample` is unchanged and stays TOML: it documents the settings file. The YAML teaching
  surface is this command.

## Completions

- `resource migrate` selectors complete via a NEW cross-product completer: the candidate list is
  kind identifiers plus `kind/name` pairs read from the operator's TOML. The existing dynamic
  completers are flat per-parameter name lists (workspaces, sessions), so this is new plumbing on
  the same machinery, not reuse of it.
- `--layout` / `--toml` enum values, `resource sample` kind argument, and both new subcommands enter
  the static tree.
- Deliberate CLI-shape divergence, for the record: `resource describe` keeps its two-positional
  `KIND NAME` grammar; `migrate` uses composite `KIND/NAME` tokens because selectors are variadic
  and mixed-granularity. Aligning `describe` is not this SDD's business.

## Test surface (plan carries the checkboxes)

Golden migration of a maximal config; selector filtering (kind, kind/name, unknown, overlap dedupe,
explicit-selector-matching-nothing errors, bare nothing-to-migrate exits 0); all three layouts plus
the per-resource unsafe-name refusal; append to existing files including one lacking a trailing
newline; comment vs delete modes including markers, multi-section and non-contiguous units, and the
dotted-key / inline-table refusal; `[secret_backends.*]` drop note including the bare-run-only case;
`type` to `provider` rename; backup creation and its before-any-write ordering; dry-run writes
nothing; verification success, partial-migration verification, and mismatch-rollback (files, created
directories, append truncation); `resource sample` stdout, kind filter, `--write` create + append +
traversal/suffix refusals; un-commented samples load clean through the loader.

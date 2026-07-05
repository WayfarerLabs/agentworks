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
agw resource migrate [SELECTOR]... [--all] [--layout per-kind|single|per-resource]
                     [--toml comment|delete] [--dry-run [--full]] [--yes]
```

### Selectors

- Selectors are REQUIRED unless `--all` is passed; a bare invocation errors with a hint (maintainer
  ruling, 2026-07-05: "migrate everything" must be an explicit opt-in, never the accident of typing
  nothing). `--all` and selectors together is also an error.
- `--all`: migrate every operator-declared resource currently sourced from TOML.
- `KIND` (e.g. `vm-template`): every TOML-declared resource of that kind.
- `KIND/NAME` (e.g. `vm-template/small`): that one resource. The token splits at the FIRST `/`, and
  the grammar is unambiguous because `/` is strictly disallowed in resource names (maintainer
  ruling, 2026-07-05, enforced source-independently at `Registry.add` -- a breaking tightening for
  configs carrying slash-bearing quoted names, called out in the Phase 5 release notes).
- Overlapping selectors (`secret secret/foo`) union: each matched resource migrates exactly once.
- Selectors use registry kind identifiers (lower-kebab), not TOML section names. The section-to-kind
  mapping is `decode.KIND_SECTIONS`, shared with the manifest decoder -- one source of truth.
- An EXPLICIT selector matching nothing in the TOML is a hard error before anything is written --
  the operator named something specific, so silence would mislead (the error for a known kind says
  "no TOML-declared resources of kind X"; the resource may already be YAML-declared or
  auto-declared, neither of which is migratable). `--all` with nothing left to migrate reports
  "nothing to migrate" and exits 0, keeping scripted re-runs idempotent.
  `resource migrate secret-backend` is still an ERROR (nonzero exit, like any explicit selector that
  cannot match) but with a tailored message: those sections are warned no-ops with no manifest
  successor; an `--all` run drops them instead (below).
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
- Per-resource layout REFUSES names that are not filename-safe (spaces, `\`, leading dots --
  anything outside a conservative safe set; `/` is already banned at load, but non-secret names are
  otherwise pass-through), pointing at the per-kind layout. No sanitization: a mangled filename
  would break the "filenames are convention" story from the other direction.
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
  that rewrites the TOML. An `--all` run whose only remaining deprecated TOML is these sections
  still offers the drop -- otherwise the tool could never silence that residue ("nothing to migrate"
  would short-circuit every invocation).
- Multi-section resources (`[x]` + `[x.env]` and similar) are treated as one unit -- all sections
  belonging to a selected resource are commented/deleted together -- including when the sections are
  non-contiguous in the file (each is edited where it sits).
- Supported declaration shapes: standard `[section.name]` header tables (and their sub-sections).
  Dotted-key or inline-table declarations under a parent header (`[secrets]` holding
  `foo = { ... }`) -- and top-level assignment shapes (`secrets = { foo = ... }`, which load fine
  but have no header to comment) -- are REFUSED with their file/line and a hand-migration hint:
  "commented out in place" has no faithful rendering for a key buried in a shared table, and
  guessing would risk the surviving config. Refusal happens even on a bare run: silently skipping a
  discoverable resource would report a complete migration that left rows behind.

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
2. Resolve selectors to a concrete resource list. Explicit selectors matching nothing error; `--all`
   with nothing left reports "nothing to migrate" and exits 0 (after offering the
   `[secret_backends.*]` drop if those sections remain).
3. Print the plan: documents to be written per target file (created vs appended), TOML sections to
   be commented/deleted, `[secret_backends.*]` drops if any.
4. `--dry-run`: stop after the preview -- the SUMMARY (which resources go where, files
   created/appended, TOML mode) by default, since a whole-config content dump is unusable as a first
   answer (maintainer ruling, 2026-07-05); `--full` adds the YAML documents and the TOML diff.
   Writes nothing either way.
5. Confirm (skipped by `--yes`), then: back up, write/append manifests, rewrite TOML, verify.

### Renames and special cases

- `git_credentials.<name>.type` emits as `spec.provider` (the manifest surface never had `type`),
  and provider-owned fields (azdo's `org`) emit nested under `spec.provider_config` while kind-owned
  `token` stays top-level -- the YAML shape diverges from flat TOML by design (provider_config
  pattern, 2026-07-05).
- `description` moves from the section body to `metadata.description`.
- The `admin` singleton (`[admin.config]` / `[admin.env]` / sub-sections) emits as one
  `admin-template` document named `default`, and `[named_console]` as one
  `named-console-template/default`, matching the envelope restrictions.
- Field mapping is mechanical (section fields minus name/description become `spec`); correctness is
  enforced by the verification step and the golden tests, both of which round-trip through
  `load_manifests` -- the loader and migrator cannot disagree.

## `agw resource sample`

```text
agw resource sample (KIND | --all) [--write FILENAME]
```

- `KIND` prints that kind's sample; `--all` prints every manifest-declarable kind's (mutually
  exclusive; a bare invocation is an error, mirroring `resource migrate` -- extended to this command
  by maintainer ruling, 2026-07-05, so no surface dumps everything by accident).
- Samples ship bundled with the app (like `sample-config.toml`), one per kind, and are FULLY
  commented out: every line carries a leading `#`, so written samples are inert text the loader
  ignores -- `--write` can never create a duplicate or a live resource by accident, and running it
  twice just appends more comments. The operator uncomments and edits to activate. The guarantee
  stays real rather than vacuous: the test mechanically strips one `#` per line, loads the result
  through the real loader, and builds a full registry over the whole uncommented set. One deliberate
  exception (maintainer ruling, 2026-07-05): the secret-backend sample is PROSE-ONLY -- no
  config-bearing provider ships yet, so there is nothing real to declare, and an uncommentable
  document would teach a lie; it gains a real document when such a provider lands (pinned by a test
  that flips that day).
- Comment convention, pinned (2026-07-05): document lines are `#` + the YAML line
  (`#apiVersion: ...`, `#  name: ...`), prose lines are `##`. This DELIBERATELY diverges from
  `sample-config.toml`'s hash-space-is-prose convention, for two mechanical reasons the TOML surface
  doesn't face: stripping one `#` must yield valid YAML (from `## prose` it leaves `# prose`, a YAML
  comment; from `# prose` it would leave live content), and hash-space cannot distinguish prose from
  indented document lines (`#  name: default` is a document line with YAML indentation). The
  strip-one-`#` rule is what keeps the uncomment story and the loader-guarantee test trivially
  mechanical.
- `--write FILENAME`: instead of stdout, write to `<resources-dir>/FILENAME`. Rules:
  - Relative paths only, resolved under the resources directory; escaping it is an error.
  - Must end `.yaml` / `.yml` (otherwise the loader would never pick it up; error with that hint).
  - Parent subdirectories are created.
  - If the file exists, the sample is APPENDED -- same append-only rule as the migrator; the tool
    never rewrites existing YAML. No `---` separator is involved (discovered at implementation): the
    samples are fully commented out, so a separator would create an empty null document that the
    loader would reject; appending comment text to a manifest file cannot change what it declares.
- `agw config sample` is unchanged and stays TOML: it documents the settings file. The YAML teaching
  surface is this command.

## Completions

- `resource migrate` selectors complete via a NEW cross-product completer: each operator-origin
  registry row emits both its bare kind and its `kind/name` selector form (implementation choice:
  the source is `agw resource list --origin operator --names-only`, which also includes
  YAML-declared rows that are already migrated -- selecting one produces the clear already-migrated
  error, and that beats growing CLI surface solely to filter completion candidates). The existing
  dynamic completers are flat per-parameter name lists, so this is new plumbing on the same
  machinery, not reuse of it.
- `--layout` / `--toml` enum values and the `resource sample` kind argument complete statically
  (click.Choice; the sample kinds are known at generation time), and both new subcommands enter the
  static tree.
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

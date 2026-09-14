# Agent artifacts: source capture and codec

This LLD implements the approved [FRD](frd.md) and [HLA](hla.md) acquisition and normalized-input
boundary. It does not define native placement, routing results or DB ownership.

## API and immutable content

`capture_artifacts(bundles, origin, limits=...)` accepts an ordered sequence of bundle names and
resolved `ArtifactBundle` values, plus the consuming owner's `ArtifactOrigin`. Each bundle has four
type maps: hints, rules, skills and agents. It returns one `ArtifactGroup` with the actual
`ArtifactOwner` and four immutable maps keyed by canonical artifact name. Capture fills bundle and
entry provenance without changing the origin owner.

`resolve_bundle` uses ordinary resource layers and merge machinery for `inherits`. Type maps merge
by key; their entry models use whole-value replacement. Inherited losers are discarded before
capture, so their sources are not acquired or recorded as replacement payloads.

Capture composes selected resolved bundles in reference order. A later same-type/key definition
replaces the whole earlier entry within that owner, including skill members. Replacements retain
compact losing origin, acquisition provenance and content digest, without full bodies or recursive
chains. Changed content warns; identical content stays quiet while updating winning provenance.
Traversal uses hints, rules, skills, then agents, preserving map insertion order. Replacing an entry
retains its key position; there is no precedence across types.

`ArtifactInputs` is the integration boundary for setup and session context: a local group plus
immutable deferred groups keyed by original owner. Deferral selects entries without changing their
owner. Different scopes retain distinct groups even when type and key match. `.groups()` traverses
deferred groups then local; `.items()` traverses their type maps without storing a parallel flat
representation. Native adapters own faithful placement, aggregation or explicit collision refusal.

The model uses frozen dataclasses, tuples and defensive immutable map copies. `ArtifactContent`
contains type, name, description, normalized instruction text and complete `ArtifactMember` values.
Each member has a relative path, bytes, executable intent and its text-classification result.
Metadata and native persona options use canonical JSON strings; the `metadata` and `native_options`
properties return independent ordinary dictionaries for native generation. An integration cannot
modify another consumer's nested metadata through the shared input.

`content.digest` hashes length-framed logical fields and sorted member paths, executable intent and
bytes. Source location, requested revision, resolved revision and timestamps do not participate.
`input.origin_identity` hashes the actual owner, artifact type and canonical key. Producer and
bundle remain provenance and do not create namespaces. `input.identity` combines logical identity
with the content digest for freshness. Identical content from a new winning source retains its input
identity, and current capture provenance remains authoritative over retained setup evidence. Core
derives the facet from the component: admin and agent use user; VM, workspace and session use their
matching facets.

Single-file rules and personas retain their captured member and mode in addition to their logical
text. Inline hints and rules have no source members. Skills retain the entire selected package.
Acquisition provenance remains separate from these identities.

Native package cleanup boundaries are outside this capture codec. The integration's shared skill
renderer supplies an exact optional `ArtifactFile.package_root`, retained on each
`OwnedArtifactFile` through publication and retirement checkpoints. The root contains that file and
must remain strictly within an owning publication root. Omitting this optional field authorizes file
retirement only; neither the capture codec nor cleanup infers a package root from path spelling. For
ordering, a file with native skill identity and basename `SKILL.md` identifies an entrypoint:
publication places it before supporting members, and retirement keeps it until those members retire.
Deeper entrypoints retire before shallower ones. Required inner-parent cleanup precedes dropping its
file record, which can remain after the file itself was removed. Permission denial on the final
package-root removal is the sole optional failure: the guarded operation checks directory identity
and peeks for emptiness, then warns and permits the file checkpoint to complete. Failed inner-parent
cleanup, unsafe paths, failed checks and transport failures remain retryable.

Codex's native adapter refuses skill entrypoints outside `<skill-root>/<package>/SKILL.md` in its
known discovery roots, including nested entrypoints within otherwise valid packages. Its bounded
inventory counts package members and directories against the existing 512-entry limit, including
proposed files and implied directories before publication. This is a Codex delivery support limit;
capture still permits ordinary supporting members within its independent package limits. Proposed
members and implied directories receive the same entrypoint-layout checks as existing files. The
shared `MAX_CODEX_PERSONA_BYTES` constant bounds Agentworks-rendered persona TOML at 32 MiB; it is
an Agentworks rendering/inventory budget, not a native Codex product limit.

Native discovery requests travel as JSON through the transport's existing `input_data` stdin channel
with TTY allocation disabled. The login-shell command remains fixed-size as packages grow; bounded
inventory metadata still returns through captured stdout for workstation validation.

## Source acquisition

`package_sources.py` extends the shared source facility beside `sources.py`. It reuses `SourceRef`
parsing and invoking-workstation home/cwd semantics, without changing dotfiles behavior. The strict
artifact validator rejects embedded HTTPS credentials and accepts only a single optional `ref` query
parameter. It is also used by declaration validation, before persistence. Diagnostics name the
consuming owner and bundle entry without quoting source values or subprocess stderr.

A `PackageCapture` context represents one capture operation. It owns private temporary acquisition
storage and removes it on both success and failure. Local files and directories are read on the
workstation. Local snapshots compare device, inode, mode, size and modification/change timestamps
before and after reading, then recheck every visited file and directory. Observed concurrent
mutation rejects the capture. Links, special files and local `.git` metadata are refused.

On POSIX, capture opens every ancestor from the filesystem root with descriptor-relative
`O_NOFOLLOW` traversal. Package traversal and its final recheck use these anchored directory
handles, including for individual source files. Renaming a checked parent cannot redirect a later
read; changed parent identities reject the capture. Handles are bounded by ancestry and package
depth and close on success or failure.

Windows has no Python `dir_fd` equivalent. A private Windows helper opens each ancestor from the
volume root using `CreateFileW` with `FILE_FLAG_OPEN_REPARSE_POINT` and
`FILE_FLAG_BACKUP_SEMANTICS`, rejects all reparse points, and retains the handles while their paths
are used. `FILE_SHARE_READ` alone prevents replacement and write access that could turn a held
directory into a reparse point. A conflicting existing handle fails acquisition. This is the
containment boundary for Windows pathname enumeration; there is no unchecked path-only fallback. See
Microsoft's
[CreateFileW sharing and reparse semantics](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).
The same metadata, complete-package, mutation, and content limits apply on both platforms.

Git acquisition creates an empty bare repository and fetches the selected reference with depth one.
The same repository/reference pair is resolved once per operation; all its selected entries use that
immutable commit. VM and admin preparation share one context and its combined limits, while
retaining separate origins and saved captures. Agent, workspace and session-local setup each own
their capture operation. Default references select remote HEAD. Reinitialization deliberately
reacquires a movable reference; a commit reference remains pinned. Provenance records a
credential-free repository, requested reference, selected path and resolved commit.

Git members come from `ls-tree` and `cat-file` object reads. There is no checkout, archive
extraction, filter execution, attribute substitution or bundled script execution. Export-ignored
members remain present. Source hooks and recursive submodule acquisition are disabled. Workstation
credential helpers remain available with interactive stdin disabled; guest authentication is
unrelated. Selected symlinks, submodules and Git LFS pointers are errors, including when a link is
selected directly as the source. Local capture also rejects unresolved Git LFS pointers.

All members must have contained UTF-8, NFC paths. Absolute paths, traversal, backslashes, control
characters, punctuation that is not portable, reserved device names, trailing dots/spaces and `.git`
components are refused. Validation checks case-folded directory prefixes as well as file names,
duplicate paths and file/directory collisions. Skills select one explicit package root; capture does
not discover other skills recursively.

## Text and metadata

Text suffixes are the recovered closed set: `.md`, `.txt`, `.py`, `.sh`, `.bash`, `.zsh`, `.ps1`,
`.js`, `.mjs`, `.cjs`, `.ts`, `.json`, `.jsonc`, `.yaml`, `.yml` and `.toml`. Designated text
requires UTF-8 without NUL and converts CRLF and lone CR to LF. Unknown formats remain opaque,
including ASCII-only PDFs and scripts without filename extensions. Source files are never rewritten.

Each entry's `preserve_bytes` list accepts relative paths or glob patterns. Matching supporting
members retain their exact bytes even with a recognized suffix. A skill pattern matching `SKILL.md`
is rejected; its entrypoint always normalizes. Hints, rules and persona instruction bodies always
use normalized text regardless of their source filename.

Rules accept plain Markdown or a leading YAML metadata mapping containing only `description`.
Descriptions are optional nonempty strings of at most 1,024 characters; the map key supplies the
name. Inline and file sources parse identically. Normalized rule content stores description,
canonical metadata and body separately; native renderers consume the body. Persisted inline rules
validate their separated metadata and body, while file rules additionally compare against retained
source members. Hints remain literal text, including YAML-looking content.

Skills require root `SKILL.md` frontmatter with standard name/description and a nonempty instruction
body. The name matches the map key and the explicitly selected directory. Standard optional
metadata, including license, compatibility, allowed tools and string-valued metadata maps, is
retained. Persona Markdown requires name, description and instructions; the name matches the
containing agents-map key. Its optional `native_options` maps integration names to native option
objects. Other top-level persona fields are rejected. Rule and persona mappings reject duplicate and
non-string keys at every depth. Native integrations validate their own supported option schemas.
Hooks and MCP configuration are not accepted in this delivery.

`frontmatter.py` shares the bounded metadata loader between capture and native inventory. It rejects
YAML anchors/aliases and bounds size and nesting before constructing values. Capture adds its body,
standard metadata and source-specific validation separately. Metadata must be finite JSON, with
bounded canonical encoding. Capture produces the same source-independent model for all source
readers; future core producers can construct that model directly.

## Bounds and persisted representation

Default operation limits are 120 seconds, 4,096 members, 16 MiB per member, 64 MiB total content,
128 MiB acquisition storage, 32 path components and 4,096 characters per relative member path. Local
directory enumeration has a separate bounded entry budget. Git process output is spooled privately,
monitored with acquisition storage, and read into memory only within its limit. A limit or process
failure terminates acquisition, removes staging and raises an error. No prior snapshot is returned
as a fresh capture.

`encode_inputs` returns a version-3 JSON object containing the owner and four type maps. Entries
contain content, origins, provenance, input identity, compact replacement evidence and an ordinal
within their type map. Ordinals preserve declared order through the DB serializer, which sorts JSON
object keys. Decode requires contiguous unique ordinals and restores map insertion order. Internal
content type/name and origin owner/entry must agree with the containing group and map key. It
validates that object through the same decoder before returning, so a writer cannot save a capture
that a subsequent reader rejects. Owning acquisition also performs this validation before returning
buffered inputs that can cause native effects. Direct persisted writers retain their own validation
boundary and leave the previous record unchanged on refusal. Member bytes use strict base64,
including designated text; the text flag records normalization. Version 2 remains readable with its
original literal-rule and persona-metadata interpretation; `CapturedArtifacts.codec_version` retains
that interpretation when another component is updated or a backup is canonicalized. Fresh captures
use version 3 and strict authoring validation. Content identities remain stable until explicit
refresh. The enclosing applied-state payload uses the highest component codec version, so older
readers recognize a newer payload rather than treating it as corruption. `decode_inputs` accepts
only supported versions, rejects extra fields and wrong primitive types, bounds the input structure
before decoding, validates relative paths and metadata, and checks total member/byte limits and
recomputed content/input identities. Capture and decoding use the same entrypoint parser: stored
metadata, body, native options and identity fields must match the retained members, and forbidden
execution metadata is rejected even when all hashes are consistent. Provenance is checked as a
credential-free Git repository with its selection, ref and resolved commit, an absolute workstation
path, or inline content. These checks do not contact the source. The owning state layer
distinguishes an unsupported domain version from corrupt content. Doctor reports an unsupported
domain version as uninterpreted evidence, not corruption, and backup retains its uninterpreted
payload. The state writer refuses to overwrite every unsupported capture version, including version
1; owning reinitialization is not an upgrade path. Independent native file ownership records remain
preserved. There is no DB access, native invocation or source reacquisition in the codec.

Names share one canonical pattern across declarations, normalized content and persisted type-map
keys. Explicit typed map access retains the fixed hints/rules/skills/agents traversal and key order.
Inspection exposes current contributing bundle IDs as `declared_bundles`, in selection order with
the last selected. It retains one row per owner/type/key and keeps current declaration selection
separate from captured origin and replacement digests. It does not acquire sources or invent
replacement history before capture.

## Verification

Local tests exercise complete skill packages, CRLF and lone-CR normalization, opaque PDFs,
byte-preserved text fixtures, executable identity, immutable metadata, source-independent identity,
versioned grouped round trips and corrupt state. Composition tests cover whole-entry inheritance
without acquiring discarded sources, same-owner replacement, canonical names, compact provenance,
quiet identical replacements, original-owner deferral and cross-scope equal-name preservation.
Lifecycle tests cover provenance-only refresh, entrypoint-first publication and cleanup retry after
permission failures. Unsupported captures remain uninterpreted in backup and refuse replacement.
Filesystem fixtures exercise path collisions, links, special files, metadata directories, mutation
and operation bounds. Local Git fixtures exercise shared revision capture, pinned commits, refresh,
selected links/submodules/LFS, export attributes, filters/hooks, credential redaction, failure and
temporary-storage cleanup. Public Git grammar is unchanged by test transport redirection; tests use
no model or backend service.

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
must remain strictly within an owning publication root. Missing legacy roots authorize only file
retirement; neither the capture codec nor cleanup infers a package root from path spelling. For
retirement ordering only, a legacy owned file with native skill identity and basename `SKILL.md`
identifies an entrypoint whose owned supporting members retire first. Deeper entrypoints precede
shallower ones, preserving retryable discovery without granting directory-pruning authority.

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

Skills require root `SKILL.md` frontmatter with standard name/description and a nonempty instruction
body. The name matches the map key and the explicitly selected directory. Standard optional
metadata, including license, compatibility, allowed tools and string-valued metadata maps, is
retained. Persona Markdown requires name, description and instructions; the name matches the
containing agents-map key. Its optional `native_options` maps integration names to native option
objects. Native integrations validate their own supported option schemas. Hooks and MCP
configuration are not accepted in this delivery.

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

`encode_inputs` returns a version-2 JSON object containing the owner and four type maps. Entries
contain content, origins, provenance, input identity, compact replacement evidence and an ordinal
within their type map. Ordinals preserve declared order through the DB serializer, which sorts JSON
object keys. Decode requires contiguous unique ordinals and restores map insertion order. Internal
content type/name and origin owner/entry must agree with the containing group and map key. It
validates that object through the same decoder before returning, so a writer cannot save a capture
that a subsequent reader rejects. Owning acquisition also performs this validation before returning
buffered inputs that can cause native effects. Direct persisted writers retain their own validation
boundary and leave the previous record unchanged on refusal. Member bytes use strict base64,
including designated text; the text flag records normalization. `decode_inputs` accepts only the
supported version, rejects extra fields and wrong primitive types, bounds the input structure before
decoding, validates relative paths and metadata, and checks total member/byte limits and recomputed
content/input identities. Capture and decoding use the same entrypoint parser: stored metadata,
body, native options and identity fields must match the retained members, and forbidden execution
metadata is rejected even when all hashes are consistent. Provenance is checked as a credential-free
Git repository with its selection, ref and resolved commit, an absolute workstation path, or inline
content. These checks do not contact the source. The owning state layer distinguishes an unsupported
domain version from corrupt content. Doctor reports an unsupported domain version as uninterpreted
evidence, not corruption, and backup retains its uninterpreted payload. Owning reinitialization may
replace the unsupported version-1 draft capture, while independent native owned-file records remain
available for guarded update and cleanup. Unsupported future versions cannot be overwritten by this
path. Declaration syntax makes a clean break with the former flat map. There is no DB access, native
invocation or source reacquisition in the codec.

## Verification

Local tests exercise complete skill packages, CRLF and lone-CR normalization, opaque PDFs,
byte-preserved text fixtures, executable identity, immutable metadata, source-independent identity,
versioned grouped round trips and corrupt state. Composition tests cover whole-entry inheritance
without acquiring discarded sources, same-owner replacement, canonical names, compact provenance,
quiet identical replacements, original-owner deferral and cross-scope equal-name preservation.
Lifecycle tests cover provenance-only refresh and cleanup with unsupported draft captures.
Filesystem fixtures exercise path collisions, links, special files, metadata directories, mutation
and operation bounds. Local Git fixtures exercise shared revision capture, pinned commits, refresh,
selected links/submodules/LFS, export attributes, filters/hooks, credential redaction, failure and
temporary-storage cleanup. Public Git grammar is unchanged by test transport redirection; tests use
no model or backend service.

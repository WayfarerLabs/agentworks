# Agent artifacts: source capture and codec

This LLD implements the approved [FRD](frd.md) and [HLA](hla.md) acquisition and normalized-input
boundary. It does not define native placement, routing results or DB ownership.

## API and immutable content

`capture_artifacts(bundles, origin, limits=...)` accepts an ordered sequence of bundle names and
ordered entry maps, plus the consuming owner's `ArtifactOrigin`. It returns a tuple of
`ArtifactInput` values. The caller resolves ordinary bundle resources and supplies actual owner
identity. Capture fills bundle and entry addresses without changing the origin seed.

The model uses frozen dataclasses and tuples. `ArtifactContent` contains type, name, description,
normalized instruction text and complete `ArtifactMember` values. Each member has a relative path,
bytes, executable intent and its text-classification result. Metadata and native persona options use
canonical JSON strings; the `metadata` and `native_options` properties return independent ordinary
dictionaries for native generation. An integration cannot modify another consumer's nested metadata
through the shared input.

`content.digest` hashes length-framed logical fields and sorted member paths, executable intent and
bytes. Source location, requested revision, resolved revision and timestamps do not participate.
`origin.identity` hashes the consuming component, resource kind/name, producer and bundle/entry
address. `input.identity` combines that address with the content digest. Core derives the facet from
the component: admin and agent use user; VM, workspace and session use their matching facets.

Single-file rules and personas retain their captured member and mode in addition to their logical
text. Inline hints and rules have no source members. Skills retain the entire selected package.
Acquisition provenance remains separate from these identities.

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

Git acquisition creates an empty bare repository and fetches the selected reference with depth one.
The same repository/reference pair is resolved once per operation; all its selected entries use that
immutable commit. Default references select remote HEAD. Reinitialization deliberately reacquires a
movable reference; a commit reference remains pinned. Provenance records a credential-free
repository, requested reference, selected path and resolved commit.

Git members come from `ls-tree` and `cat-file` object reads. There is no checkout, archive
extraction, filter execution, attribute substitution or bundled script execution. Export-ignored
members remain present. Source hooks and recursive submodule acquisition are disabled. Workstation
credential helpers remain available; guest authentication is unrelated. Selected symlinks,
submodules and Git LFS pointers are errors, including when a link is selected directly as the
source.

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
body. The name matches the explicitly selected directory. Standard optional metadata, including
license, compatibility, allowed tools and string-valued metadata maps, is retained. Persona Markdown
requires name, description and instructions. Its optional `native_options` maps integration names to
native option objects. Native integrations validate their own supported option schemas. Hooks and
MCP configuration are not accepted in this delivery.

Frontmatter rejects YAML anchors/aliases and bounds size and nesting before loading. Metadata must
be finite JSON, with bounded canonical encoding. Capture produces the same source-independent model
for all source readers; future core producers can construct that model directly.

## Bounds and persisted representation

Default operation limits are 120 seconds, 4,096 members, 16 MiB per member, 64 MiB total content,
128 MiB acquisition storage and 32 path components. Local directory enumeration has a separate
bounded entry budget. Git process output is spooled privately, monitored with acquisition storage,
and read into memory only within its limit. A limit or process failure terminates acquisition,
removes staging and raises an error. No prior snapshot is returned as a fresh capture.

`encode_inputs` returns a version-1 JSON object containing inputs, origins, provenance and content.
Member bytes use strict base64, including designated text; the text flag records normalization.
`decode_inputs` accepts only the supported version, rejects extra fields and wrong primitive types,
bounds the input structure before decoding, validates relative paths and metadata, and checks total
member/byte limits and recomputed content/input identities. Capture and decoding use the same
entrypoint parser: stored metadata, body, native options and identity fields must match the retained
members, and forbidden execution metadata is rejected even when all hashes are consistent.
Provenance is checked as a credential-free Git repository with its selection, ref and resolved
commit, an absolute workstation path, or inline content. These checks do not contact the source.
Unknown versions and corrupt state produce a credential-free error for the caller to frame with
owning-state context. There is no DB access, native invocation or source reacquisition in the codec.

## Verification

Local tests exercise complete skill packages, CRLF and lone-CR normalization, opaque PDFs,
byte-preserved text fixtures, executable identity, immutable metadata, source-independent identity,
versioned round trips and corrupt state. Filesystem fixtures exercise path collisions, links,
special files, metadata directories, mutation and operation bounds. Local Git fixtures exercise
shared revision capture, pinned commits, refresh, selected links/submodules/LFS, export attributes,
filters/hooks, credential redaction, failure and temporary-storage cleanup. Public Git grammar is
unchanged by test transport redirection; tests use no model or backend service.

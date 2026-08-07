# LLD: the sample and describe renderer (step 2.8)

Date: 2026-08-06

Covers plan step 2.8: the renderer over `iter_field_docs`, `agw resource sample` rendering live from
the registry, the field-reference surface, the authored prose layer, and FR19's contributed-sample
validation.

**This is the renderer half of the document the plan calls `emission-and-renderer-lld.md`.** Step
2.7 wrote the emission half separately as `emission-lld.md`, so this file cross-references it rather
than restating it; the pair is what the plan's single name refers to. Nothing about emission is
re-decided here except the two things 2.7 explicitly handed over: the `agw resource schema` NAME
(section 2) and the uncomment contract (section 6.4).

## 1. What the renderer derives from

One authored source (the model), and one ordered walk over it (`iter_field_docs`). The renderer adds
two things the walk cannot know:

- **which spec model a kind's document actually has**, once the capability union is spliced in
  (section 4);
- **authored prose**, which is not a fact about any field (section 3).

Everything else, including every field's type, requiredness, default, description, constraints,
reference semantics, and union alternatives, is read off the stream. There is no per-kind branch
anywhere in the renderer and no table of kinds.

Emission is the SIBLING derivation, not a consumer: `manifests/emit.py` derives JSON Schema from
`model_json_schema` over the same models. The renderer does not read emitted schema and emission
does not read `FieldDoc`. They share exactly one thing, and it is neither's output: the spliced spec
model (section 4).

## 2. The naming call, settled here

Two surfaces, two names, and the plan left both open for this step to close.

**`agw resource schema` KEEPS the name 2.7 shipped provisionally.** It prints and writes JSON
Schema: a machine artifact for an editor, named after the artifact it produces. Nothing in the
onboarding child's topic-content contract competes for the word (their vocabulary is topics, guide,
describe, blocks), and their `FieldReference` block is fed by the service functions in section 7,
not by this command.

**The field-reference surface is `agw resource describe-kind`.**

```text
agw resource describe-kind secret              # a declarable kind's field reference
agw resource describe-kind vm-platform         # a capability kind: its implementations
agw resource describe-kind vm-platform/lima    # one implementation's config fields
```

The reasoning, in order of weight:

1. **`agw resource describe` is taken, and by a different question.** It describes one declared
   RESOURCE (`secret/npm-token`): origin, inbound references, live instances. What 2.8 adds
   describes a TYPE. Overloading one verb so that the presence of a slash decides between "this row"
   and "this schema" would make the argument shape carry the meaning, which is exactly the kind of
   blur principle "names tell the truth" exists to refuse.
2. **`<resource> <verb>-<object>` is the documented CLI shape**
   (`.claude/rules/cli-conventions.md`), already carried by `console add-sessions`,
   `agent grant-workspaces`, and their siblings. `describe` operating on `kind` spells
   `describe-kind`, and it reads as what it is.
3. **One grammar for the argument.** `KIND` or `KIND/NAME`, the same token shape
   `resource describe`, `resource edit`, and `resource migrate` selectors already take, and the same
   address the registry uses for a capability row. So `agw resource describe vm-platform/lima` (the
   row) and `agw resource describe-kind vm-platform/lima` (its config schema) name the same thing
   two ways, which is a pair an operator can predict rather than two vocabularies.

Rejected: `resource fields` (says less than it does: prose and alternatives are not fields);
`resource explain` (vague, and unrelated to any existing verb); a `resource kind describe` subgroup
(the conventions rule refuses a multi-word group for a small verb family).

## 3. The authored prose layer

The onboarding child's topic-content contract (`onboarding-topic-content-contract-message.md`,
ACCEPTED in plan 2.8) fixes what wave 2 authors: the PROSE ONLY (`title`, `summary`, `Overview`),
colocated beside the thing it documents. The envelope (topic slugs, `anchor`, the block vocabulary,
`related_topics`, the catalog, duplicate-slug validation) is theirs, and this step builds none of
it.

### 3.1 `TopicProse`, and why `summary` is not a new field

```python
# agentworks/topics.py  (a leaf module, like declared_resource.py)
@dataclass(frozen=True)
class TopicProse:
    title: str
    overview: str
```

Two fields, not three, and the missing one is the point. The contract's `summary` is "one short
authored paragraph for indexes and reference introductions", and every thing wave 2 documents
ALREADY declares exactly that, once:

- a resource kind declares `ResourceKind.description`, the one operator-facing line
  `agw resource kinds` prints;
- a capability implementation declares `Capability.description` (or, for the Protocol kind, the same
  class attribute), the line its registry row carries.

Restating it inside `TopicProse` would create two authored strings for one fact, which is precisely
the drift FR13 exists to kill, and the smaller of the two would go stale first. So `summary` IS
`description`, the mapping is documented on `TopicProse`, and where an existing description read
like a table cell rather than an introduction it was rewritten in place, which improves
`agw resource kinds` at the same time.

`title` is the display title (`"VM sites"`), used as the heading of `describe-kind` and available to
the guide's topic pages. `overview` is voice-neutral markdown, shared verbatim by a reference
surface and a teaching surface; teaching register belongs in the onboarding effort's own `Teaching`
block, not here. Prose is inert: no placeholders, no templating. If it ever grows dynamic content it
adopts the guide's locked-down template vocabulary rather than a second dialect.

Prose carries NO field lists. Every field fact on both surfaces comes from the stream, so a prose
paragraph enumerating fields would be the one thing in this design that can drift.

### 3.2 Colocation, and the two attachment points

- **A kind's prose rides its kind strategy**, beside `description` in the domain package that owns
  the kind (`vms/kinds.py`, `secrets/kinds.py`, ...). It is a REQUIRED member of the `ResourceKind`
  protocol, not an optional one: every kind the app defines is describable, and making it required
  is what stops a fourteenth kind from shipping undocumented. mypy is the enforcement.
- **An implementation's prose rides its class**, as `prose: ClassVar[TopicProse | None] = None` on
  `Capability`, read through one accessor (`topics.prose_of`) that also answers for the
  `secret-backend` Protocol kind, whose implementations derive from no base. OPTIONAL here, and
  deliberately: the contract says a participant with no useful content contributes nothing, and a
  plugin author must be able to register a capability without writing an essay. Every implementation
  shipped in-tree declares one anyway.

Topic data does NOT go on the capability-kind descriptor in wave 2: the contract says the descriptor
"may transport" it without owning it, and wave 2 has no reader, so under the descriptor's
minimal-by-rule discipline it stays absent. Colocation is enough for onboarding's catalog to
collect.

## 4. The spliced spec model, shared with emission

`iter_field_docs(VMSiteDecl)` yields `platform` as a `CapabilityBlock` (`name` plus
`extra="allow"`), which is the right thing for the ROW and says nothing useful to an operator: the
fields they may write inside that table belong to the selected platform. Emission already solves
this by SUBCLASSING the row with the capability union spliced onto the naming field
(`emission-lld.md` section 3.4). The renderer needs the identical model, so the splice moves to
`manifests/spec_model.py` and both callers read it:

```python
spec_model(kind) -> type[BaseModel]   # the row, or the row with its capability union spliced in
```

One authority for "what a kind's spec actually looks like". Two independent splices would be two
answers to that question, and the failure would be silent: a sample teaching a shape the schema does
not describe. Emission's behavior is unchanged (the moved code is the same code, and its `$defs`
naming is pinned by `tests/manifests/test_emit.py`).

Three more per-kind facts moved with it, because both surfaces need all of them and neither should
own them: `declarable_kinds()` (which replaced the two spellings of one set, emission's
`emittable_kinds` and the sample surface's `SAMPLE_KINDS`), `row_model(kind)`, and
`metadata_model(kind)`. The metadata model gained one change on the way: its fields are in
DECLARATION order rather than sorted, because `name` is what a document's metadata block opens with
and a rendered sample that led with `expires` would teach an order nobody writes. Emitted schema
sees the same reordering, where property order is presentational and an editor's completion list
follows it.

**The root-model hop is collapsed by the renderer, not by the foundation.** The union is an
`AgwRootModel`, because that is what the error bridge frames against, so the stream reads `platform`
-> `platform.root` -> the arms. `root` is a mechanism, never a key an operator writes, so the
reference collector merges a nested root model's `root` doc into its parent field and drops the
segment. It stays in the collector rather than in `iter_field_docs` because 2.1's stream is landed,
reviewed, and shared; if a second presenter ever needs the same normalization, that is the trigger
to promote it.

## 5. One collector, two presenters

```text
iter_field_docs -> field_tree.py -> reference.py -> skeleton.py  (commented YAML)
                   (the tree)       (the record)   describe.py  (terminal)
```

`field_tree.py` turns the flat stream into the tree and derives the per-field facts;
`reference.py` names things, finds their models, and attaches their prose. Two modules rather than
one because the collector crossed 500 lines, and this is where it splits cleanly: everything below
the line knows about fields and nothing else, and everything above it knows about kinds.

`SchemaReference` is the service record BOTH presenters and the guide read:

- `target` (`"vm-site"`, `"vm-platform/lima"`), `kind`, `implementation`, `category`;
- `title` / `summary` / `overview`, the prose of section 3;
- `metadata` and `spec`, each a tuple of `FieldEntry` (empty `metadata` for a capability target: an
  implementation's config has no document envelope of its own);
- `alternatives`, the implementations of a capability kind, each with its own summary.

`FieldEntry` is a `FieldDoc` plus the tree and the presentation facts both presenters derive
identically, computed once here rather than twice:

- `children`, so a nested block renders as a block;
- `alternatives` and `rendered`, for a discriminated union;
- `writable`, which is `required` MINUS the owner-templated case (section 6.3);
- `type_label`, `render_type(doc.annotation)`, which a presenter may replace;
- `sample_value`, the value the skeleton writes (section 6.2).

The record is presentation-free in the sense the contract requires: it carries `FieldDoc` verbatim,
no markdown, no ANSI, no CLI vocabulary, and no pre-rendered text. `type_label` is a convenience
computed from the exported `render_type`, and every consumer can ignore it and keep the annotation.

Two things the tree needs that the stream does not carry:

- **the element of a collection of blocks.** `iter_field_docs` streams `env` and then
  `env.<key>.value`, with nothing at `env.<key>`: rightly, since the element is not a field anyone
  declared. A tree has to have it or the element's fields hang off the collection and render one
  indent too shallow, which in YAML is a different document. The collector synthesizes that node
  when a doc arrives whose parent path ends in a placeholder segment.
- **an alternative's summary.** The arm's `ModelDoc.description` describes the CONFIG MODEL ("Where
  a Lima site's `limactl` runs"); what an operator choosing between platforms wants is what the
  IMPLEMENTATION is ("Lima VMs, local or on a remote host via SSH"), which is its `description`. The
  collector reads the implementation where the arms are capability configs and falls back to the arm
  model's docstring where they are not.

**The capability registry is read through `capabilities/config.py`, never through the descriptor's
accessor.** `registered_implementations` / `registered_implementation` were added there for this,
and `tests/resources/test_graph_guard.py` is why: reaching `<descriptor>.registry()` from a new
module is a banned pattern, and the right answer was the one step 2.3 already took, which is to keep
the sanctioned read at one call site rather than let it become two. The guard caught this, which is
what a guard is for.

## 6. The commented-YAML skeleton

### 6.1 The document shape

One document per kind, fully commented, exactly the shipped convention: every line starts with `#`,
document lines are `#` + the YAML, prose lines are `##` plus a space. Stripping one leading `#` per
line turns the document lines into YAML and the prose lines into ordinary YAML comments. `--all`
concatenates, with `#---` between documents.

The header carries the kind identifier, the kind's summary, and its overview, and not the authored
title: the document below it says `kind: vm-site` already, and an operator scrolling a `--all` dump
is looking for the identifier. The title is the field reference's heading.

```yaml
## kind: vm-site
##
## Configured places to create VMs (a platform plus its settings)
##
## <overview, wrapped>
##
## Uncomment the document lines (delete one leading `#`) and edit.
##
#apiVersion: agentworks/v1
#kind: vm-site
#metadata:
#  # What this resource is called: ... (string, required)
#  name: my-vm-site
#  # One operator-facing line saying what this resource is for ...
#  # (string or null, optional)
#  # description: <string>
#spec:
#  # The vm-platform backing this site ... (table, required)
#  # One of: lima, wsl2, proxmox, azure-vm, aws-ec2. Shown here: lima.
#  # `agw resource describe-kind vm-platform/wsl2` prints another one's fields.
#  platform:
#    # The platform this config is for. (one of: lima, required)
#    name: lima
#    # The SSH host running `limactl` for a REMOTE-Lima site. (string or null, optional)
#    # vm_host: me@gpu-box
```

### 6.2 Required is written; optional is a comment inside the document

**Only the fields an operator MUST write are live document lines. Every optional field renders as a
commented suggestion at its own indent**, description above it, value beside it.

This is the one structural change from the hand-written samples, and it is what makes the rendered
set honest:

- **it loads.** An uncommented skeleton carries exactly the required fields, so the FR13 test (every
  kind renders, loads through the manifest path, and builds a registry) is a property of the design
  rather than of hand-curation.
- **it cannot dangle.** Today's `admin-template` sample writes `git_credentials: [github]` and its
  own prose warns that loading FAILS unless you also declare that resource. An optional reference
  field that stays a comment references nothing, so the cross-sample coupling that warning is about
  is gone.
- **it still shows everything.** Every field appears, with its type, its requiredness, its default
  or example, and its description. That is FR10's complete skeleton; the only difference is which
  lines are live.

The VALUE a line carries is, in order: the field's first `examples` entry, the one value a closed
field can hold (a union arm's tag), its declared default where that default says anything, and
finally a type-shaped placeholder (`<string>`, `[<string>]`, `{<key>: <string>}`). A required field
with no example and no sensible placeholder is caught by the load test, which is what turns "author
an example" from advice into a gate. Values are YAML-dumped in FLOW style, so a short list or table
sits on the key's own line (`apt: [zsh, ripgrep]`), which is how the hand-written samples wrote one
and what an operator edits in place.

An EMPTY default is skipped in favor of the placeholder, and `false` and `0` are not: `repos: []` is
the honest default and teaches nothing, while `repos: [<string>]` says what may go there, and
`enabled: false` is the value the field takes rather than an absence. `worth_showing` is that rule,
spelled once and shared by the value and by the parenthetical naming it.

### 6.2.1 `metadata.name`, the one value the collector derives

The name is the one field where the ROW and the DOCUMENT disagree, and the document is what is being
described. Two corrections, both discovered by the load test rather than by reading:

- **it is always required.** The envelope demands `metadata.name` of every document whatever the row
  says, and a kind that DEFAULTS its name (`admin-template`) does so for the row the framework
  synthesizes. Left alone, a rendered admin-template commented out the only key in its metadata
  block and produced `metadata:` with nothing under it, which the loader rejects.
- **its example is per-kind**: `default` where the envelope accepts only that name (read from
  `envelope.only_default_name`, promoted so that rule has one authority and two readers), the
  declared default where a kind declares one, and `my-<kind>` otherwise.

Both are attached to the RECORD rather than handled in the skeleton, so the field reference and the
guide see the same field the sample writes.

### 6.3 An owner-templated field is not the operator's to write

A field carrying a `default_template` (`GitHubConfig.token` -> `git-token-{owner_name}`) is REQUIRED
to pydantic and OPTIONAL to the operator: the model fills it from the owner when it is omitted, and
emission already stopped emitting such a field as `required` (`emission-lld.md` section 2.1). The
skeleton follows the same rule for the same reason, and says what the omission resolves to:

```yaml
#    # The secret holding this credential's personal access token.
#    # (string, optional, defaults to the resource named `git-token-<this resource's
#    # name>`, names a secret)
#    # token: <string>
```

### 6.4 The uncomment contract and the modeline

The body convention is UNCHANGED, so the shipped guidance stays true and 2.7's `--write` detail line
needs no edit: the modeline is a real single-`#` comment stamped as a file header by `write_sample`,
it is not part of `sample_text`, and the operator is told to uncomment the DOCUMENT lines. The
mechanical test strips one `#` per line over `sample_text` only, which never contains the header.

Considered and rejected: making the whole file bulk-strippable by spelling the header `##`. That is
not a modeline (yaml-language-server matches `# yaml-language-server:`), so it would trade a real
editor association for a mechanical convenience nobody has asked for.

## 7. The service functions, and what the guide calls

Every fact both surfaces show is reachable without a CLI:

```python
from agentworks.manifests.reference import describable_targets, kind_reference, reference_for
from agentworks.manifests.samples import sample_text, write_sample
from agentworks.manifests.skeleton import skeleton_text
```

`reference_for("vm-platform/lima")` and `skeleton_text(...)` are what the onboarding effort's
`FieldReference` and `Sample` blocks call. Neither loads config, neither builds a registry, and
neither constructs a capability: they read the code registries and the models. Rendering an
implementation therefore works for a DISABLED one (enablement is a property of a registry ROW, and
nothing here reads rows), which is pinned by a test rather than left as a consequence.

The presenters share one text transform, `plain_text`, which turns the RST-style double backticks a
model's attribute docstrings use into single ones. It is on the presenter side by design: markdown
consumers (emitted schema descriptions, the guide's topic pages) render a double-backtick span
correctly, and only a plain-text reader sees them as noise, so the record keeps the author's text.

## 8. Live means the plugins are seated (a 2.7 defect, fixed here)

`agw resource schema vm-site` on today's branch emits `lima` and `wsl2` and nothing else, while the
command's own help says a plugin's capability appears once the plugin is installed. Verified by
running it, not by reading the code: the shipped plugins seat their implementations as an import
side effect of `agentworks.plugins`, and the only importer is `bootstrap.build_registry`, which a
registry-free surface never calls.

So the shared spec-model assembly calls one named step
(`plugins.registration.seat_installed_plugins()`) before reading a capability registry. It is
import-idempotent, costs nothing on a path that already built a registry, and it is what makes "live
from the registry, plugins included" true for the sample, the field reference, AND the emitted
schema.

**No emission test changed, and that is the finding.** Every one of them derived its expectation
from the same live registry the emitter read, so a union missing three platforms agreed with a set
of platform names missing the same three. Whether they passed depended on whether some earlier test
in the run had imported `agentworks.plugins`. The new pin names the three plugins literally, so it
fails if seating regresses instead of agreeing with the regression.

## 9. FR19: contributed manifests validate through the one regime

Issue #214 asked whether a plugin-contributed sample should warn or error on an unknown key. FR12's
closed-world direction answers it: hard error, and the same one.

There is no second validation path to remove. `plugins/publish.py` already routes a plugin's bundled
manifests through `manifests/package.publish_manifest_package`, which calls the operator loader
(`load_manifests`) and therefore the same envelope, the same kind model, and the same
`extra="forbid"`. What 2.8 adds is the PIN: a fixture plugin package whose bundled manifest carries
an unknown spec key, asserted to fail with the same message an operator's manifest produces, so a
future "just warn for contributed content" shortcut breaks a test that says why.

## 10. The tests that replace the sample pins

Deleting the bundled YAML retires the strip-one-`#` corpus, and the renderer tests take over the
coverage rather than reducing it. The CLI-contract pins in `tests/manifests/test_samples.py` (kind
selection, `--all`, the capability-kind refusal, `--write` create-then-append, path refusals) carry
through the swap unchanged; what is REPLACED is only what pinned file content.

- every declarable kind renders, and the rendered set uncomments, loads through `load_manifests`,
  and builds a full registry (the shipped guarantee, now over generated text);
- the whole `--all` set does the same as ONE file, which the per-file corpus never proved;
- the skeleton is fully commented, and commented text is inert through the loader;
- fixture-schema unit tests over models the app does not ship: requiredness, defaults, examples,
  nested blocks, collections, a discriminated union with one arm rendered and the rest listed, an
  owner-templated field, and a root-model config;
- a DISABLED capability renders, pinned against a fixture plugin that is not enabled in config;
- `test_declare_once_end_to_end.py` gains the two arms it reserved: the fixture platform's field
  reaches the rendered sample and the field reference with no edit anywhere else. The sample arm is
  the ALTERNATIVES line rather than a rendered body, because the skeleton renders one arm and the
  fixture is not the first registered one; its negative twin (no mention when unseated) is what
  keeps that from being a coincidence.

The secret kind's prose points at `secret-backend/onepassword` for what a `backend_mappings` value
may hold, which is the one place a rendered document still hands an operator to another surface
rather than describing the shape inline. That is section 11's un-built splice, not an oversight.

## 11. Deliberately not built

- **The map-keyed splice for `backend_mappings`.** 2.7 escalated it (the descriptor has no record of
  where a map-keyed capability is hosted, so building it needs a descriptor-contract change, which
  is the roadmap's artifact). Unchanged here: `secret.backend_mappings` renders as the open table it
  is, and the secret kind's prose points at `agw resource describe-kind secret-backend/onepassword`
  for what a mapping may hold. The queued cost is the roadmap lead's.
- **A topic catalog, slugs, `anchor`, `related_topics`, the block vocabulary.** The onboarding
  child's, per the accepted contract.
- **Prose for anything a plugin author writes.** `prose` is optional on an implementation.
- **A `--format json` on `describe-kind`.** The service record IS the machine surface; a second
  serialization with no consumer would be speculative.

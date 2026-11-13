# Model-declared runtime interpolation: architecture

Status: implementation architecture for [the accepted requirements](frd.md).

## Model declaration

Add a small immutable `Interpolated()` marker to the public `agentworks.schema` vocabulary. It
declares eligibility rather than evaluating values. Scalar fields and string elements can opt in
alongside their existing constraints and merge metadata.

Keep the schema package a leaf. It must not acquire imports of managed state, resource registries,
transports or runtime invocation objects. Existing schema shape inspection and registration checks
validate marker placement.

## Shared interpretation

Keep `agentworks.interpolation` as the pure one-pass substitution engine. A model-aware adapter
reads a field's interpolation declaration and applies that engine to selected authored values with
explicitly supplied context and an owning field location. Unmarked values pass through without
namespace validation.

Components adopt the adapter at their existing interpretation boundary. The annotation then controls
eligibility within that boundary; a marker is not a hook that intercepts all ordinary Python
attribute access. Core and plugins share this pattern, without a central registry of plugin names or
a second list of eligible fields.

Runtime consumers retain selection and context responsibility. That separation is necessary because
the same env model serves VM, user and session scopes, and Shell has two alternative launch
declarations. Eagerly rendering the complete loaded config would validate discarded or unselected
input and choose identities too early.

## Derived operator surfaces

Extend the existing `FieldDoc` stream for resource explanation and generated samples. Emit
interpolation metadata from the marker into JSON Schema through the normal model schema path.
Neither surface owns an independently maintained eligibility inventory.

Env's bare-string shorthand must derive interpolation metadata from the field it folds into. Its
current schema path creates a plain scalar separately, so decorating only `PlaintextEnvEntry.value`
is insufficient. Keep the scalar and explicit object spellings consistent without adding a second
annotation to maintain.

## Runtime flows

| Consumer               | Selection before rendering                                      | Context and destination                                                                            |
| ---------------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Env                    | Merge scopes and remove managed-identity collisions and secrets | Invocation context; literal env values, followed by delivery safety validation                     |
| Shell                  | Choose command or resume command for launch intent              | Session context; shell source, retaining preflight and launch checks                               |
| Native argument fields | Resolve inherited authored list                                 | Bound session context; argv data rendered before planning and quoting                              |
| Agreed setup fields    | Select effective setup resource and applicable action           | Actual admin/agent identity; checks, source commands or path data handled by their owning consumer |

Render results are separate invocation values, not mutations of registry models or stored
declarations. Repeated preflight/launch may each render from the source declaration; neither renders
a previous result.

## Adoption and compatibility

The [inventory](migration-inventory.md) records existing-support migration and accepted groups A-D.
The [migration strategy](migration-strategy.md) describes rollout without stored-state changes. New
candidates are deliberately distinct from already supported fields so a design review cannot
accidentally approve a behavior expansion.

Keep compatibility aliases in their existing source-owned adapter. Session scope is known at the env
consumption boundary, not in the shared plaintext model. Annotation adoption does not extend alias
eligibility or change deprecation policy.

## Documentation and delivery

Use one feature branch and PR for the SDD and implementation. Initially the PR is a draft design
checkpoint with `review-requested`; implementation continues after operator acceptance. Permanent
documentation changes ride with their supporting code. The old completed SDD stays locked; this
effort records its own requirements and implementation evidence.

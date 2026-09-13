# Agent artifacts

`artifact-bundle` is an ordinary declared resource. Owning scopes select ordered bundles through
`artifacts.bundles`; an authored list replaces an inherited list. Resource loading and inspection
validate declarations without fetching sources.

`capture.py` acquires workstation files/trees or immutable Git objects through `package_sources.py`.
It normalizes designated UTF-8 text to LF, preserves binary bytes and executable intent, and emits
the frozen values in `model.py`. Skills are complete standard Agent Skills packages. Agents are
agent personas. Sources are edge concerns: routing and integration APIs consume the same normalized
inputs regardless of their source.

`codec.py` validates the bounded lossless wire representation. `state.py` records one common core
capture per owner component in the existing instance-state store. `routing.py` reads that capture
and the selected integration's reusable results along one actual VM/user/workspace/session graph.
Handling an input for one user does not consume the VM result for another user.

`application.py` defines integration results and whole-file ownership. `publication.py` validates
plugin output and uses the shared guarded `NativeFiles` transport utility. Native formats and
policy live in integration adapters. Existing unowned files are never adopted merely because
their bytes match. Changed managed files are retained and diagnosed, with evidence for retry.

`session.py` prepares the launch context, stages a new private run before old-runtime teardown,
then retires obsolete owned files after teardown. No artifact bookkeeping changes VM deletion.
`agw artifacts show` projects declarations, captures and recorded delivery without claiming to
observe the target filesystem or prove that a model obeyed the supplied content.

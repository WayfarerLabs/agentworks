Fix configuration errors at their source. For runtime failures, record the named resource, typed
error, and a redacted reproduction before changing state.

When workstation examination is inside the current envelope, run `agw doctor --output json` as a
diagnostic operation without asking again. Use its framed checks to select a narrower verification
surface. Doctor output is evidence, not authorization to install tools, edit configuration, start a
VM, or apply another repair.

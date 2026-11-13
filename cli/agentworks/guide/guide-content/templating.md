---
description: Use managed user and resource context in fields that support interpolation.
index-order: 46
---

# Runtime templating

Use `${agw:NAME}` references to make reusable configuration follow its consuming user or resource.
For example, this env declaration follows the target user's home directory:

```yaml
env:
  TOOL_CONFIG_DIR: "${agw:user_home_dir}/.config/tool"
```

## Find a supported field

Interpolation is opt-in for each configuration field. `agw resource explain` and
`agw resource sample` identify interpolated values, including individual list elements. Use the
reference for the component being configured:

```sh
agw resource explain session-template
agw resource explain harness-integration/shell
agw resource explain harness-integration/codex
agw resource explain user-install-command
```

The invocation supplies the facts it knows. A session knows its workspace; VM and user setup do not
acquire workspace or session context just because a reference asks for it. An unavailable value
produces a located error. Rendering does not read the workstation's environment or the target user's
shell variables.

<!-- agw:include path="_guide_sources/docs/arch/runtime-context.md" heading="Context values" -->

## Inspect before use

`agw env show --session NAME` previews winning plaintext env values from managed state without a VM
connection. It keeps secrets redacted and does not inspect a running process's environment. Use the
command's `--help` for other resource selections.

Changing a reusable template affects future invocations and applicable setup operations. Inspect its
inherited values and affected resources before editing it; retaining the current configuration is a
valid choice. Rendering this guide performs none of those operations.

<!-- agw:include path="_guide_sources/docs/arch/runtime-context.md" heading="Syntax and literal text" -->

<!-- agw:include path="_guide_sources/docs/arch/runtime-context.md" heading="Secrets" -->

<!-- agw:include path="_guide_sources/docs/arch/runtime-context.md" heading="Commands and shell quoting" -->

# Runtime context templates: delivery and lifecycle

## Desired state

Templates and stored instance specs retain `${agw:NAME}` expressions. Rendering occurs when an
invocation is prepared, using that invocation's consuming identity and selected resource context. No
DB schema conversion or background configuration rewrite is required.

```yaml
env:
  TOOL_CONFIG_DIR: "${agw:user_home_dir}/.config/tool"
  WORKTREE_LABEL: "${agw:workspace_name}"
```

For a resource named `reviewer` running as `agt-reviewer`, the config directory is
`/home/agt-reviewer/.config/tool`. Another agent or the VM admin receives its own managed home.
`WORKTREE_LABEL` requires workspace context.

Existing processes retain the environment with which they were started. A new shell/exec or session
start/restart recomputes its env from the effective desired declarations. Setup uses its executing
identity and only the resource context available to that setup operation.

## Interpretation boundaries

- `${agw:...}` is the only Agentworks namespace; shell expressions and Go templates remain literal
  input for their own consumers.
- Escape the namespace as `$${agw:NAME}` when plaintext must contain the placeholder literally.
- Secret contents are opaque: interpolation and unescaping do not apply to them. Changing an entry
  between plaintext and secret forms deliberately changes this interpretation.
- Env values are literal data. Command strings obey shell-source quoting rules, including when a
  workspace path contains spaces or apostrophes.
- Offline `env show` previews managed plaintext values and redacted secrets; it does not claim to
  observe a process's live environment or shell startup effects.

## Complete delivery

Ship parser, context projection, all env/command callers, behavior tests, permanent documentation,
and the full SDD in PR #867. There is one active syntax and one shared rendering contract. The
permanent reference stands alone when this SDD directory is removed.

Validation includes planned identities, discarded definitions, opaque secrets, command literal-data
handling, and errors before destructive restart. Mark the locally validated implementation ready for
the normal review and integration-testing pipeline. Wait for the integration report before starting
the next authorized final review iteration.

# Runtime context templates: prior art

Research date: 2026-11-06. Sources are upstream documentation, not third-party tutorials. Findings
inform a proposed design; they do not authorize an environment lookup feature.

## Findings

### Dev Containers: distinguish the source

Dev Containers uses `${localEnv:NAME}` and `${containerEnv:NAME}` to distinguish host values from
existing container values. Its configuration also exposes a `userEnvProbe` choice, with distinct
login and interactive shell modes. This is the closest match to Agentworks' workstation versus
target distinction. Adopt explicit source naming if environment references are added; do not infer
the source from a bare variable name.

Sources:
[environment variables](https://code.visualstudio.com/remote/advancedcontainers/environment-variables),
[upstream specification](https://github.com/devcontainers/spec/blob/main/docs/specs/devcontainerjson-reference.md).

### systemd: context facts and environment expansion are different

systemd's `Environment=` supports specifier expansion without shell variable expansion. Its `%h`
specifier refers to the service manager user's home, and is not changed by `User=`. The useful
lesson is to specify whose context a placeholder describes, not to copy that identity choice:
Agentworks' `user_home_dir` must describe the selected consuming identity.

Sources:
[Environment=](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Environment=),
[specifiers](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#Specifiers).

### Docker Compose: define phase and escaping

Compose substitutes `$VAR` and `${VAR}` and uses `$$` to preserve a dollar sign for later
processing. Its interpolation happens before per-file merging, and unresolved values can become
empty strings with a warning. These are useful comparison points, not Agentworks requirements.
Agentworks should render winning entries after merging and reject unavailable references. A `$$`
spelling for lookup could mislead readers who expect Compose's deferral behavior.

Sources:
[interpolation specification](https://docs.docker.com/reference/compose-file/interpolation/),
[interpolation sources](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).

### GNU envsubst: lookup does not require evaluation

GNU `envsubst` replaces `$VARIABLE` or `${VARIABLE}` from its process environment. It deliberately
does not implement shell commands or shell default-value expressions. This supports keeping a
substitution language small and treating environment lookup as named data access.

Source:
[envsubst invocation](https://www.gnu.org/software/gettext/manual/html_node/envsubst-Invocation.html).

## Internal evidence

Snapshot: `c8d4dfb3cb042eea2ae5ac12b06624faf703400b`.

| Location                                                      | Observed behavior                                            | Design implication                                                                      |
| ------------------------------------------------------------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `cli/agentworks/sessions/manager/_constants.py:7`             | Double braces, two known names                               | Keep working spellings; define escaping and malformed syntax explicitly                 |
| `cli/agentworks/sessions/manager/_env.py:120`                 | String substitution and session-only env preprocessing       | Move parsing out of the session manager; stop preprocessing before the final env merge  |
| `cli/agentworks/env/compose.py:37`                            | Shared env rendering for execution                           | Render winning plaintext here through a common pure renderer                            |
| `cli/agentworks/env/show.py:73`                               | Independent display assembly                                 | Reuse plaintext rendering while preserving provenance and redaction                     |
| `cli/agentworks/capabilities/harness_integration/base.py:194` | Literal argv quoting hides braces from manager interpolation | Preserve workload literals; command interpolation is broader than the shell integration |
| `cli/agentworks/sessions/manager/_create_roll.py:348`         | Manager interpolates returned launch commands                | Audit all integrations, including intentionally expanded `extra_args`                   |
| `cli/agentworks/harness_setup/inputs.py:67`                   | Setup consumes `compose_env` too                             | Define setup context, not just session context                                          |
| `cli/agentworks/agents/initializer.py:121`                    | Managed agent home uses `/home/<linux_user>`                 | Use the provisioning model, not the workstation's home                                  |
| `cli/agentworks/vms/initializer/driver.py:140`                | Managed admin home uses `/home/<admin_username>`             | Centralize the managed-home rule across setup and runtime                               |

## Claims rejected or not established

- `$$` is not an established cross-tool spelling for reading the remote shell environment.
- A string delivered through SSH env injection is not shell source. Sending `$HOME/file` does not
  ask the remote shell to expand it inside the env value.
- A target user's shell environment is not a unique timeless map. Invocation mode and startup phase
  must be specified. A previous workload's mutated environment is another source entirely.
- systemd's `%h` must not be described as the home of its configured `User=` identity.
- Dev Containers' probing option is evidence that startup mode matters, not a reason to add the same
  set of options to Agentworks.
- None of the surveyed tools requires introducing a general expression engine for this use case.

## Open questions

- Q1 from the FRD: which environment does the operator intend to reference?
- Is there a current use case beyond `user_home_dir` that needs ambient environment lookup, such as
  appending to the target user's configured `PATH` or reading `XDG_CONFIG_HOME`?
- If target lookup is desired, must offline inspection show an unresolved expression, or should a
  separately requested live inspection be introduced? A stored snapshot cannot silently claim to
  describe the next shell invocation.

## Source assessment

| Source                                        | Quality                                         | Angle                                               |
| --------------------------------------------- | ----------------------------------------------- | --------------------------------------------------- |
| Dev Containers specification and VS Code docs | Primary specification and product documentation | Explicit local/target sources and startup mode      |
| systemd manuals                               | Primary reference                               | Context specifiers and identity semantics           |
| Docker Compose docs                           | Primary reference                               | Expansion order, missing values, escaping           |
| GNU gettext manual                            | Primary reference                               | Limited interpolation without shell evaluation      |
| Agentworks source at the pinned snapshot      | Direct implementation evidence                  | Consumers, timing, homes, and literal-data handling |

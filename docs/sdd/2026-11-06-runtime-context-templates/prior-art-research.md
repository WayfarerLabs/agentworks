# Runtime context templates: prior art

Research date: 2026-11-06. Sources are upstream documentation. The chosen design uses a reserved
`${agw:NAME}` namespace and explicit managed context; it does not read ambient environments.

## Namespaced substitution

VS Code uses `${variableName}` for predefined context and `${env:Name}` or `${config:Name}` for
named sources. This provides a familiar convention for clearly scoped substitution. Agentworks
adopts the namespace-and-name shape, with `agw` identifying its finite context catalogue; it does
not adopt the additional VS Code lookup or command-evaluation facilities.

Source:
[VS Code variables reference](https://code.visualstudio.com/docs/reference/variables-reference).

Dev Containers distinguishes `${localEnv:NAME}` and `${containerEnv:NAME}` and explicitly models
shell probing. The source distinction reinforces why managed context should not silently imply
workstation or target environment lookup.

Sources:
[environment variables](https://code.visualstudio.com/remote/advancedcontainers/environment-variables),
[upstream specification](https://github.com/devcontainers/spec/blob/main/docs/specs/devcontainerjson-reference.md).

## Syntax choice

Double-brace placeholders overlap with Go-template fields and control actions used by tools such as
Docker and gh. Choosing `${agw:NAME}` keeps Agentworks substitution separate from those downstream
languages. A reserved namespace also lets ordinary shell `$NAME` and `${NAME}` expressions pass
through, while Agentworks can reject mistakes within its own syntax.

Sources:

- [Docker formatting](https://docs.docker.com/engine/cli/formatting/)
- [gh formatting](https://cli.github.com/manual/gh_help_formatting)
- [Go templates](https://pkg.go.dev/text/template)

## Escaping and limited interpretation

Docker Compose uses dollar-based substitution and `$$` for literal dollars. Agentworks follows the
familiar extra-dollar escape convention only for its reserved namespace; it does not transform
ordinary shell dollar syntax or adopt Compose's broader interpolation language.

Source: [Compose interpolation](https://docs.docker.com/reference/compose-file/interpolation/).

GNU `envsubst` performs limited process-environment substitution without shell command evaluation.
The useful principle is that substitution can remain small and data-oriented. Its ambient source is
not part of the Agentworks contract.

Source:
[envsubst invocation](https://www.gnu.org/software/gettext/manual/html_node/envsubst-Invocation.html).

Python's standard `string.Template` interprets ordinary `$NAME` and `${NAME}` expressions. Used
unchanged, it would also consume shell references that Agentworks needs to preserve. The design
requires recognition of the reserved prefix rather than broad dollar substitution.

Source: [Python template strings](https://docs.python.org/3/library/string.html#template-strings).

## Identity and context

systemd distinguishes specifier expansion from shell environment expansion. Its `%h` refers to the
service manager user's home, independent of `User=`. Agentworks deliberately binds its context to
the consuming workload/setup user, so the identity must be explicit in its own contract.

Sources:

- [Environment=](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Environment=)
- [specifiers](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#Specifiers)

## Internal design constraints

- `env/identity.py` already supplies the shared `ResourceContext` model.
- `env/merge.py` defines precedence; rendering follows selection of the effective map.
- `env/compose.py` and `env/show.py` need the same plaintext interpretation without requiring secret
  resolution for inspection.
- Runtime access, sessions, console shells, and harness setup are concrete consumers of that map.
- Managed initialization uses `/home/<Linux username>`; context projection shares that rule instead
  of consulting local `HOME` or issuing an inspection-time SSH probe.
- Commands and literal argv are different destinations. Render declared template fields in their
  owning integration and quote structured argv values once; arbitrary automatic shell quoting cannot
  be inferred from a string fragment.

## Claims not adopted

- There is no universal standard for the names of Agentworks resource-context variables.
- No general expression engine, remote environment probe, or managed-env dependency graph is
  required to expose `user_home_dir`.
- A template-looking string arriving from a secret is data, not an expression to evaluate.
- A managed-config preview is not an observation of a remote process's ambient environment.

## Source assessment

| Source                                 | Quality                            | Design contribution                                   |
| -------------------------------------- | ---------------------------------- | ----------------------------------------------------- |
| VS Code and Dev Containers references  | Primary product/spec documentation | Namespace shape and explicit source identity          |
| Docker and gh manuals; Go package docs | Primary references                 | Downstream brace syntax                               |
| Docker Compose docs                    | Primary reference                  | Explicit literal-dollar escaping                      |
| GNU gettext manual                     | Primary reference                  | Bounded substitution without shell evaluation         |
| Python library reference               | Primary reference                  | Scope of standard dollar templates                    |
| systemd manuals                        | Primary reference                  | Context ownership and environment distinction         |
| Agentworks code                        | Direct implementation evidence     | Existing shared context, consumers, and managed homes |

# Shell completions

This package generates the shell completion scripts that `agw completion show|install` emit. It is
the mechanism; `cli/README.md` has the operator-facing story of installing and removing them.

## How generation works

`generate(shell)` in `__init__.py` is the single entry point. It builds the command tree once,
hashes it, and hands both to one of the three shell backends.

1. **The tree comes from Typer.** `build_spec(app)` in `spec.py` walks the Click command object that
   Typer produces and returns a `CommandSpec` tree (`name`, `help`, `params`, `subcommands`), with
   each leaf parameter a `ParamSpec`. Adding a command, option, or argument to the CLI flows into
   this tree automatically; nothing here needs editing for the tree itself.

   The walk is duck-typed against `Protocol` shims rather than `isinstance` checks, because Typer
   vendors its own copy of Click. Testing against the real `click` package would match nothing and
   silently produce an empty tree.

2. **Dynamic values are not baked in.** `DYNAMIC_COMPLETIONS` in `spec.py` maps a
   `(command path, parameter name)` pair to an abstract completer id such as `workspaces`, `vms`, or
   `resource_kinds`. Only that id reaches the generated script. Each backend then renders the id
   into a snippet, and for every completer but one that snippet calls back into the CLI at
   completion time (`agw workspace list --names-only` and friends), so completions always reflect
   current state rather than state frozen at install time. The exception is `files`, which renders
   to each shell's native filesystem completion (`compgen -f`, `_files`, `CompleteFilename`) because
   the shell already knows the answer. This table is where the package stops inferring and starts
   asking you to say what you meant.

3. **Callbacks into a live CLI are kept safe.** Database-backed completers pass a hidden
   `--completion-probe` flag, which puts the CLI in completion mode: the database opens read-only
   and refuses to migrate rather than doing work behind an operator who only pressed Tab.
   `is_legacy_database_completion` recognizes the marker-free command shapes that shipped before
   0.14, so scripts installed by an older version still take that safe path; `_app.py` gates that
   recognizer on the completion-shaped stream pair (stdin a tty, stderr not) so an ordinary
   interactive run of the same command is never mistaken for a completion probe.

   Resource-list completers are registry-backed instead. Their `agw resource list --names-only`
   callback finalizes configuration but never opens state, so they stay available when the database
   is absent or unusable and do not pass the probe marker.

   `spec.py` drops `hidden=True` parameters from the tree, which is why `--completion-probe` itself
   never completes.

4. **The version stamp catches staleness.** `completion_version(spec)` hashes the tree, and every
   generated script carries the result in a header comment. `agw doctor` compares the stamp in each
   installed script against a freshly computed hash and tells the operator to reinstall when they
   differ.

## The three backends

Each backend is a module-level function with the same `(spec, version) -> str` signature, which is
what lets `generate` treat them interchangeably. The shared shape is a convention, not a declared
protocol.

| Backend         | Emits                                         | Dynamic completers live in                  |
| --------------- | --------------------------------------------- | ------------------------------------------- |
| `bash.py`       | one `_agentworks` function plus `complete -F` | `DYNAMIC_SNIPPETS`                          |
| `zsh.py`        | a `#compdef` script built from `_arguments`   | `DYNAMIC_FUNCTIONS`, `COMPLETER_FUNC_NAMES` |
| `powershell.py` | a `Register-ArgumentCompleter -Native` block  | `DYNAMIC_SNIPPETS`                          |

zsh needs a named function per completer, which is why it carries two tables where the others carry
one. Bash and PowerShell dispatch on two command levels; only zsh recurses to arbitrary depth, so a
third-level subgroup would need work in those two backends before it completed correctly.

`install.py` writes the generated script to each shell's standard location and drops an `agw` alias
beside the `agentworks` one, because both bash-completion and zsh's `compinit` load by file name and
would never reach the alias declared inside the file. PowerShell is the only shell whose profile is
edited.

## Changing a CLI command

The tree itself needs nothing, but four things are hand-maintained and the tests in
`cli/tests/test_completions.py` are what catch them:

- **A new or renamed top-level group** must be reflected in that test module's `EXPECTED_GROUPS`.
  The drift is deliberate: the pin exists so a new group is a decision, not an accident.
- **A parameter that should complete names** is silent until you add it to `DYNAMIC_COMPLETIONS`.
  Nothing infers it. Keys use the dotted path without the root app name (`vm.shell`) and the Python
  parameter name, not the option spelling.
- **A new completer id** must be added to all three backends. The generators skip an unknown id
  silently, so the parity tests are the only thing standing between you and a completer that does
  nothing.
- **A new database-backed completer** must join `DATABASE_BACKED_DYNAMIC_COMPLETIONS` and pass
  `--completion-probe` in its snippet, or completion can trip migration paths.
- **A new resource-list completer** must join `RESOURCE_LIST_DYNAMIC_COMPLETIONS` and keep its
  callback marker-free, because that names-only path is registry-only.

Any ordinary list command backing a completer also owes `--names-only` per the `cli-conventions`
rule: one name per line, no header, no formatting, and no round-trips that make pressing Tab slow. A
dedicated name-stream-only list form is the narrow exception and does not need a second presentation
flag. `agw resource list` is the one deliberate output divergence: it emits `kind/name`, because two
kinds can publish the same name, and every backend slices the prefix off shell-side. A
registry-backed completer that forgets the slice emits `kind/name` candidates.

Guide topic completion is intentionally package-only: `agw guide list` emits auto-discovered
first-party concept shells and packaged release-note topics without loading operator state. Resource
and kind completion use their command-owned list surfaces instead. The reserved `list` positional is
offered beside topics only in the first guide argument position; once selected, it terminates topic
completion.

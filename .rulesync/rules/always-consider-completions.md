# Always Consider Completions

The agentworks cli offers completions for several different shells.

We use a custom completion mechanism where the command tree is extracted from
Typer (the cli framework we use) and then we merge the dynamic path elements
(like workspaces) into the completion tree. This allows us to provide
completions for dynamic elements that are not known at compile time.

Any time the cli interface is modified, we need to make sure that the
completions are up-to-date. The implementation can be found in
`./cli/agentworks/completions/`.

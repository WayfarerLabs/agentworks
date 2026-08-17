# Keep Collateral in Sync

Behavior here has collateral. When a change outdates any of it, updating it is part of that change
rather than follow-up work. Read the relevant collateral before you start, because it carries
context you need, and check it again before you finish, because by then you are the one who outdated
it.

| Collateral    | Update it when                                                                   | Where it lives                                                               |
| ------------- | -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Docs          | behavior, usage, or any fact a doc states changes                                | `docs/`, `cli/README.md`, the doc nearest the code                           |
| Sample config | a setting is added, renamed, or retired, or its comments go stale                | `cli/agentworks/sample-config.toml`                                          |
| Completions   | the command tree, a command's options, or a parameter's completable names change | `cli/agentworks/completions/`, whose README covers the hand-maintained parts |
| Guide topics  | a retained concept shell, workflow, command signpost, or release link goes stale | the first-party package that owns that Markdown shell                        |

This is the app's collateral, not the development process's. SDD artifacts move under the `sdd`
skill's rules instead, and the `development-process` rule is what tells you to know whether an SDD
governs your work at all.

Sample config carries upkeep beyond the trigger above: its comments and its organization stay
complete, accurate, clear, and easy to use, not merely non-stale.

A retained guide shell stays current in its teaching, examples, agent-only context, imported
sections, links, and command signposts. Cover the ordinary operator path and useful next step, but
signpost current commands instead of restating command-owned resource, schema, graph, or runtime
facts. Guide rendering is static and instructional: it does not load operator state, resolve or
expose secrets, inspect the workstation, connect to a VM, perform remote work, mutate state, or
imply that rendering authorized any operation. When guidance suggests work that may fall outside the
operator's current instruction, its reviewed prose states the scope and impact, expected result, and
useful refusal alternative. No schema or prose-policing test substitutes for that review.

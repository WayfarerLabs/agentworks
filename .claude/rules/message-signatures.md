# Message Signatures

Many actors, human and agent, publish messages here through shared identities: PR comments and
reviews, issue comments, task briefs, and review reports often all arrive under one account. This is
the normal condition of agentic engineering, where credentials and identities are routinely shared
across sessions, so the account name carries no provenance at all, and provenance must not depend on
guessing from writing style. Every outward-facing message, meaning anything a person or another
agent will read outside your own session's conversation, ends with a signature identifying its
author.

- **Agentworks workloads** sign with the session name from the `AGENTWORKS_SESSION` environment
  variable, plus a short role descriptor when the name alone does not convey it. Example:

  ```text
  -- agw-test-codex (agentworks integration-test session)
  ```

- **Other environments** sign with their own appropriate identifier: a CI job name, a harness
  session id, or whatever stable identity that environment provides.
- **Humans** writing under their own accounts need no signature; their identity is the account.

Git commits are already covered by author identity plus the session trailer convention and need no
additional signature. When one session posts in several roles (for example, authoring work and
relaying a review), the role descriptor is what disambiguates; keep it honest and current.

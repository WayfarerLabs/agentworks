---
description: "Sign every outward-facing message with your session identity"
---

# Message Signatures

Many actors, human and agent, publish messages here through shared identities: PR comments and
reviews, issue comments, task briefs, and review reports often all arrive under one account. This is
the normal condition of agentic engineering, where credentials and identities are routinely shared
across sessions, so the account name carries no provenance at all, and provenance must not depend on
guessing from writing style. Every outward-facing message ends with a signature identifying its
author. Outward-facing means an authored communication published through a shared identity or
channel where others will read it: PR comments and reviews, issue comments, task briefs, posted
review reports, and the like. Ordinary repository artifacts (code, docs, config), generated output,
CI logs, and raw tool output are not messages and take no signature; their provenance belongs to
version control and the systems that produce them.

- **Agentworks workloads** sign with the session name from the `AGENTWORKS_SESSION` environment
  variable, read fresh at posting time, plus a short role descriptor when the name alone does not
  convey it. The leading `--` is the classic mail sig-dash convention, kept deliberately. Example:

  ```text
  -- agw-test-codex (agentworks integration-test session)
  ```

  If `AGENTWORKS_SESSION` is unset, sign with an honest plain-language label for what you are (for
  example `-- unidentified agentworks workload (integration tester)`) rather than guessing a name or
  leaving the message unsigned.

- **Other environments** sign with their own appropriate identifier: a CI job name, a harness
  session id, or whatever stable identity that environment provides.
- **Humans** writing under their own accounts need no signature; their identity is the account.

The boundary is the session: a subagent's report returned to its invoking session is conversation,
not an outward message, and needs no signature; the signature attaches when content leaves the
session, and whoever posts it signs as themselves. Git commits need no signature line, but an agent
session's commits must carry a session trailer. In an Agentworks workload that trailer is
`Agentworks-Session: <session name>` with the name read from `AGENTWORKS_SESSION`, mandatory
whenever the variable is set; harness-added trailers (this history's `Claude-Session: <url>`) may
ride along but do not substitute for it. When `AGENTWORKS_SESSION` is absent, the environment's own
stable harness or session trailer satisfies the requirement on its own; never invent a session name.
Outside Agentworks, use the environment's own stable session identifier as the trailer. Author
identity plus the trailer is the commit-side equivalent of the signature. When one session posts in
several roles (for example, authoring work and relaying a review), the role descriptor is what
disambiguates; keep it honest and current.

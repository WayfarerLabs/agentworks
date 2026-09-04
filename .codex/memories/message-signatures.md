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
  variable, read fresh at posting time, or, for a delegate acting on its own, with the identifier
  its charter gave it (below), plus a short role descriptor when the name alone does not convey it.
  The leading `--` is the classic mail sig-dash convention, kept deliberately. Example:

  ```text
  -- agw-test-codex (agentworks integration-test session)
  ```

  Where that selection yields no name, sign with an honest plain-language label for what you are
  (for example `-- unidentified agentworks workload (integration tester)`) rather than guessing a
  name or leaving the message unsigned.

- **Other environments** sign with their own appropriate identifier: a CI job name, a harness
  session id, or whatever stable identity that environment provides.
- **Humans** writing under their own accounts need no signature; their identity is the account.

The boundary is the session: a subagent's report returned to its invoking session is conversation,
not an outward message, and needs no signature; the signature attaches when content leaves the
session, and whoever posts it signs as themselves. A delegate that commits, pushes, or posts on its
own rather than handing back does exactly that: it signs and trails as itself, not as its lead,
since the point is to say which worker produced the thing. A delegate inherits its lead's
environment, so `AGENTWORKS_SESSION` names the lead and nothing in that environment names the
delegate. The lead supplies one, in the charter: a complete identifier the delegate uses verbatim
rather than a fragment it joins to something else. State it in the launch prompt. A lead with a
namespace for that delegate's scratch and fixtures has the obvious value to hand it. Nothing is
invented and nothing is composed, because the lead assigns and the delegate reads.

Which name a workload uses is one selection, and it decides both surfaces together, the signature
and the `Agentworks-Session` trailer. A delegate acting on its own uses the identifier its charter
gave it. Everything else uses `AGENTWORKS_SESSION`. A delegate given no identifier says so in its
hand-off and uses an honest label for what it is on both surfaces, exactly as an unidentified
workload does, rather than borrowing the lead's name it was never given. One value under the
existing key keeps an effort's commits findable as a set while a delegate's stay distinguishable
within it, which is why a lead composing that value usually builds it from its own session name:

```text
Agentworks-Session: agw-ns-instance-model/dev-3
```

Most delegates never hit this, because most report back and the lead is what speaks outward. Git
commits need no signature line, but an agent session's commits must carry a session trailer. In an
Agentworks workload that trailer is `Agentworks-Session: <name>`, taking whichever name the
selection above yields and mandatory whenever it yields one; harness-added trailers (this history's
`Claude-Session: <url>`) may ride along but do not substitute for it. Where that selection yields no
name, the environment's own stable harness or session trailer satisfies the requirement on its own;
never invent a session name. Outside Agentworks, use the environment's own stable session identifier
as the trailer. Author identity plus the trailer is the commit-side equivalent of the signature.
When one session posts in several roles (for example, authoring work and relaying a review), the
role descriptor is what disambiguates; keep it honest and current.

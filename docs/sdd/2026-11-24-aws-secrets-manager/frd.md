# AWS Secrets Manager secrets

## Status and authority

This is a draft requirements proposal awaiting authenticated operator acceptance in Agention
thread 5. Repository presence does not establish acceptance or designate the governing SDD.

The operator requested Azure, AWS, GCP, and macOS Keychain support and directed in Agention thread
3, message 2:

> Include it but each of these should be shipped individually

The continuing authenticated session goal is:

> work with the operator here and in Agention to define the work to be done and then get any PRs
> merge-ready via our standard agentic-dev-process, with up to 3 feedback/fix rounds per PR that you
> open. This goal can't complete until all secret work is confirmed done by the operator and
> everything is confirmed working.

## Proposed requirements

- Add read-only AWS Secrets Manager lookup through the existing opt-in AWS plugin. Each backend
  ships in a separate PR; this effort does not depend on Azure implementation landing first.
- Operators can declare multiple sources, order them with existing sources, and select each source's
  region and optional native AWS profile. Authentication uses native AWS credentials; source
  manifests contain no credential values or recursive Agentworks secret references.
- Each mapping selects a secret name or complete ARN and optionally a version ID or staging label.
  Omitting a version selector follows AWS's current-version semantics.
- Values preserve complete exact text, including multiline content and line endings. Native binary
  secret values are usable only when they decode as strict UTF-8. Reject NUL-containing values. JSON
  stored in a secret remains complete JSON text; field extraction is outside this proposal unless
  the operator selects it.
- Existing inspection, preview, verification, source precedence, and consuming commands operate
  through the current secret-backend contract. Registration, validation, readiness, static
  inspection, client factory construction, and context entry perform no provider or credential work.
- Preview obeys its operator-impact ceiling and returns no plaintext. Authentication paths that need
  operator action must respect that ceiling and terminal-access policy. Agentworks launches no
  automatic AWS login. Unattended authentication remains usable without terminal input.
- Only confirmed secret or requested-version absence permits source fallback. Authentication,
  authorization, malformed values, transport errors, and other service failures remain failures.
- Secret values and provider-native diagnostics do not escape through logs, representations,
  preview, exception messages or chains, or cleanup errors, including debug logging.
- Document Agentworks-specific configuration and link to official AWS authentication, permissions,
  and service documentation. Do not duplicate the provider's manuals.
- The backend performs no writes, fixture creation, permission changes, or secret rotation. AWS SSM
  Parameter Store and custom service endpoints are outside this effort.

## Proposed acceptance

Behavioral tests demonstrate SDK lifecycle boundaries, native authentication policy, version
selection, exact value handling, safe preview, source fallback versus failures, log containment, and
plugin enablement. Validate the real shipped CLI through the standard published integration tester
after the PR is ready, as directed in Agention thread 3. Report any unavailable live coverage
explicitly. A PR is merge-ready only after required checks, a complete independent PR-level review,
and the live-testing disposition. The overall secret goal remains open until the operator confirms
all requested work is done and working.

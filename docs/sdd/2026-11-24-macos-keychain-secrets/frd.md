# macOS Keychain secrets

## Status and authority

This proposal awaits authenticated operator acceptance and designation in Agention thread 6.
Implementation has not started. The operator included macOS Keychain in the secret work and directed
in thread 3, message 2:

> Include it but each of these should be shipped individually

The continuing authenticated goal requires merge-ready PRs through the standard development process,
with up to three feedback/fix rounds per opened PR. Overall completion requires the operator to
confirm that all requested secret work is done and working.

## Proposed requirements

- Provide an opt-in native macOS Keychain secret backend through the existing secret framework.
  Non-macOS hosts can load and inspect its configuration but cannot use the native backend.
- Each source selects one traditional file-based keychain by path. Omitting the path selects the
  current user's native default keychain, which is distinct from the whole search list. Existing
  source precedence composes multiple keychains when needed.
- Each mapping supplies an explicit, nonempty service and account for a generic-password item. No
  implicit mapping, Internet-password records, certificates, or cryptographic keys are included.
- Preserve complete exact UTF-8 text, including whitespace and line endings; reject invalid UTF-8
  and NUL. No plaintext fallback store or automatic third-party keyring selection is permitted.
- Registration, validation, readiness, static inspection, client factory construction, and context
  entry perform no native keychain operations or authentication.
- Zero-impact preview prevents native authentication UI, returns no plaintext, and reports
  indeterminate when broader permitted interaction could allow progress. Allowed-impact preview and
  resolution may display native unlock/access UI and change authentication state. This does not
  authorize item creation, modification, deletion, keychain creation, or configuration changes.
- The backend reads no terminal input. Global policy for terminal input remains distinct from native
  authentication UI under the existing framework contract.
- Only confirmed item absence permits source fallback. A missing, invalid, locked, or inaccessible
  source keychain, cancellation, timeout, denied access, malformed payload, or unknown native
  failure must not masquerade as an absent password item.
- Values and native diagnostics remain contained in logs, representations, preview, exceptions,
  subprocess output handling, and cleanup. Native operations have a bounded source-turn lifetime.
- Permanent documentation explains Agentworks setup, preview effects, default-keychain selection,
  and authentication state changes, linking to Apple documentation for native administration.
- Data-protection/iCloud keychains and Apple Passwords access are outside this proposal. Support for
  those stores would need distinct requirements and access-control analysis.

## Proposed acceptance

Portable tests establish registration, offline behavior, schema/mapping validation, process protocol
validation, source deadlines, cleanup, value containment, and typed outcomes. A live macOS run of
the shipped CLI must establish the native ABI, exact bytes, absence versus denied access, locked
keychains, UI suppression, cancellation, and worker termination behavior. Linux tests cannot
substitute for that evidence. Record the tested macOS and Python versions and any remaining limits.
Required gates, private reviews, published PR-level review, and live-test disposition support merge
readiness; operator confirmation remains part of the overall goal.

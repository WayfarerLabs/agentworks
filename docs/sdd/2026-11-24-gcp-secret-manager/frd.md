# GCP Secret Manager secrets

## Status and authority

This proposal awaits authenticated operator acceptance and designation in Agention thread 7.
Implementation has not started. The operator requested GCP support and directed in thread 3, message
2 that each backend ship individually. The continuing authenticated goal requires merge-ready PRs
through the standard development process, with at most three feedback/fix rounds per opened PR.
Overall completion requires operator confirmation that all secret work is done and working.

## Proposed requirements

- Add read-only Secret Manager lookup through the existing opt-in GCP plugin, independently of
  Azure, AWS, and macOS Keychain delivery.
- Each source selects an explicit project and optionally a regional location. Support global and
  regional secrets with the corresponding native service endpoints.
- Each mapping selects a secret and optional native numeric version or alias. Omission uses
  `latest`, meaning the most recently created version. Never substitute the latest enabled version
  or automatically fall back from a disabled/destroyed version.
- Preserve complete exact UTF-8 text, including whitespace and line endings. Validate native CRC32C
  integrity before reporting availability or returning a value. Reject missing/invalid payloads,
  missing/mismatched checksums, invalid UTF-8, and NUL. No JSON field extraction is included in this
  proposal.
- Preserve ADC selection order: an environment-selected credential file, otherwise local gcloud ADC,
  otherwise attached-service-account identity. Broken, rejected, or unsupported selected credentials
  fail without choosing another identity. Credential values remain outside manifests; source config
  contains no recursive Agentworks secret reference.
- Support service-account JSON, stored user/workforce credentials, attached identity, supported
  service-account impersonation, and non-executable AWS/file/URL federation. Supported credential
  and token files are regular files; terminal/device/pipe sources are not supported.
- Explicitly reject executable credential suppliers, certificate-command authentication, GDCH, and
  unknown credential formats. Do not automatically launch login or repurpose the final-secret broker
  as an authentication challenge. Broader authentication would need a separate accepted requirement.
- Registration, validation, readiness, static inspection, factory construction, and context entry
  perform no provider or credential work. Resources belong to the selected operation and close
  reliably after it.
- Preview uses supported unattended authentication and returns no plaintext. Global terminal policy
  remains intact. Authentication, permission, version-state, transport, malformed-value, and unknown
  failures do not become absence; only confirmed absence permits source fallback.
- Contain values and provider-native diagnostics in logs, representations, preview, exceptions, and
  cleanup, including debug logging. Use finite HTTP phases and bounded retries; do not claim a
  source-wide wall deadline without implementing that guarantee across every phase.
- Document Agentworks configuration, endpoint/version behavior, and the supported ADC envelope,
  linking to Google documentation. Do not create, modify, delete, list, or rotate provider secrets
  or change permissions.

## Proposed acceptance

Tests establish configuration/schema behavior, global/regional routing, version semantics, ADC
precedence and supported-provider boundaries, offline lifecycle, checksum/text validation,
diagnostic containment, and resolver fallback. Required gates and all private review lanes must
clear the handed-off head. A complete published PR-level review and standard integration tester
exercise the real shipped CLI against authorized GCP fixtures. Record actual workstation,
authentication and endpoint coverage and remaining gaps; unit doubles are not live confirmation.
Operator confirmation remains part of the overall secret goal.

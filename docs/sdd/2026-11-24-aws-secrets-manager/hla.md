# AWS Secrets Manager backend architecture

## Status

Proposed response to the draft requirements. Requirements acceptance is pending in Agention thread
5; implementation has not started.

## Integration and lifetime

Register `aws-secrets-manager` as a version-3 secret-backend capability of the existing `aws`
plugin. Keep its EC2 capability and plugin enablement behavior. Reuse the framework's source
registry, per-source mapping validation, source-first resolution, preview, and closed results. Do
not change the backend contract or depend on the Azure feature branch.

A source binds one region and optional named AWS profile. A mapping selects one secret by name or
complete ARN, with an optional version ID or staging label. A string shorthand selects the current
version. Missing mappings are not applicable; there is no implicit name convention.

Use the already declared official Boto3 dependency. Create a dedicated SDK session and client inside
the selected preview or resolution method. Factory construction and context entry remain
resource-free. Close the operation-owned client before returning, including failure and cancellation
paths. Reuse it only within that selected method's source turn.

Use finite service connection/read timeouts and bounded retries. Credential-process execution, if
allowed, also has a finite timeout and guaranteed cleanup. These bounds are not a source-wide
deadline. Do not expose a deadline setting without enforcing it across credentials, retries,
requests, and cleanup.

## Native authentication and impact policy

Retain native credential-provider order and explicit-profile precedence. Policy checks belong where
a provider is reached: skipping a blocked provider could select a different identity. Never
automatically invoke an AWS login command or substitute another identity after a selected credential
fails.

At zero-impact preview, progress through unattended native credentials and cached sessions. A
reached credential process or MFA prompt returns impact-limited indeterminate before it can request
operator action. Do not treat every authentication failure as indeterminate: broken, missing,
rejected, or expired credentials are definitive failures unless a concrete reached action boundary
could permit progress.

At maximum-impact preview or resolution, a configured credential process may run through an
operation-owned, bounded runner with no inherited terminal input or controlling terminal.
Out-of-band provider UI remains permitted by the framework's impact semantics. The current
interaction broker requests the final declared secret, not an MFA code; repurposing it would
mislabel the interaction. A reached native SDK MFA prompt therefore fails authentication without
reading a terminal. Operators authenticate externally or provide an already usable session.

Implement these guards per SDK session, including role `source_profile` chains created through the
SDK's profile-provider builder. Avoid global patches and provider removal. Behavioral tests must
exercise the real installed credential chain with isolated configuration, including nested profiles,
because mocks of the session factory cannot prove these boundaries.

## Values, failures, and containment

Use `GetSecretValue` for preview and resolution. Preview validates the returned value, discards it,
and reports only availability. Resolution returns only the complete validated text. SDK binary
responses are bytes; strict UTF-8 decoding must not apply a second Base64 decode. Preserve empty
text, whitespace, and line endings; reject NUL, invalid UTF-8, missing payloads, and ambiguous or
malformed response shapes.

Map structured service error codes and SDK exception classes to existing closed results. Only
`ResourceNotFoundException` permits fallback. Permission, decryption, invalid request, credential,
transport, and unknown errors remain failures. Never project native exception text or retain it as
an outward exception chain. Keep provider diagnostics contained throughout credentials, service
work, and cleanup, including caller-installed child log handlers under debug logging.

## Collateral and validation

Update the secret-system README, retained secret guide shell, plugin metadata, and sample plugin
comments in the implementation PR. Put AWS setup, permissions, and credential-provider detail behind
links to official AWS documentation. Do not add plugin topic discovery merely to give this backend a
dedicated guide topic.

Test typed models and schema parity, operation lifecycle, exact values, provider failure taxonomy,
logging, real native credential policy, plugin enablement, and resolver fallback. Run required local
gates and all three private quality lanes before a ready handoff. Published PR-level review and the
standard integration tester remain independent acceptance evidence.

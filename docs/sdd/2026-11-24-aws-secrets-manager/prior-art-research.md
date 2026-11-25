# AWS Secrets Manager prior art

## Research boundary

Research performed on 2026-11-24 against protected main `1c13df6c1136a147b107ddea59d75369ea6115fa`,
the locked SDK, and official AWS documentation. No cloud calls, operator AWS configuration reads, or
credential inspection were needed. The requirements remain a proposal awaiting acceptance in
Agention thread 5.

## Service addressing and values

AWS's
[GetSecretValue API](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html)
accepts a secret name or ARN and optional version selectors. Its payload is a string or binary
value. Service errors distinguish resource absence from invalid requests and decryption failures.
Design consequence: preserve native selectors and classify structured errors; do not infer absence
from exception wording or treat an unusable secret as a fallback candidate.

The
[Boto3 GetSecretValue reference](https://docs.aws.amazon.com/boto3/latest/reference/services/secretsmanager/client/get_secret_value.html)
specifies the current version when selectors are omitted, full ARNs for cross-account access, and
the `GetSecretValue` permission plus decryption permission where applicable. The documented Python
response shape represents binary payloads as bytes. Design consequence: use the SDK response shape,
preserve whole values, decode bytes exactly once as UTF-8, and link to IAM documentation rather than
performing permission discovery or requesting listing permissions.

## Native authentication and interaction

The [Boto3 credential guide](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html)
documents environment credentials, profiles, role sessions, workload identity, cached SSO, and
metadata providers. It also documents a blocking MFA prompt for configured role profiles. Design
consequence: use the native chain and explicit-profile semantics, while guarding reached interaction
boundaries inside the operation. A session factory mock is insufficient evidence.

The installed SDK matches `cli/uv.lock`: Boto3 1.43.111 and Botocore 1.43.112. Inspection of
`botocore/credentials.py` found that the native process provider inherits stdin, waits without a
timeout, and can include stderr in an exception. A role's source profile creates a nested provider
chain through `ProfileProviderBuilder`. The native resolver continues only when a provider returns
no credentials. Design consequence: guard direct and nested providers, stop with a private sentinel
at an impact boundary, and retain ordering. Removing a provider could silently change the
authenticated identity.

These internals are corroborated by the upstream
[Botocore credential implementation](https://github.com/boto/botocore/blob/develop/botocore/credentials.py).
The implementation must be tested against the installed locked version rather than assume the moving
upstream branch is identical. Its process-provider injection seam and role prompter allow
session-local policy; no global patch is necessary. Authentication integration tests must also
cover nested profile chains and earlier successful credentials bypassing later providers.

## Diagnostic containment

Installed `botocore/parsers.py` logs raw response bodies at debug level, and credential refresh logs
exceptions with tracebacks. A secrets response can therefore leak before the backend catches an
error. Design consequence: contain provider logger branches during the entire synchronous operation,
including authentication, existing child handlers, and cleanup. Return only the framework's closed
result types, without native error messages or chained exceptions.

## Existing integration pattern

Protected main already supports multiple capabilities in one vendor plugin and recursively discovers
first-party package-local guide shells. The AWS plugin currently contributes only EC2; no new plugin
index or guide extension mechanism is necessary. The secret backend contract requires offline
descriptions and resource-free factory/context entry; SDK client creation is deferred because it can
resolve credentials immediately.

## Refuted claims

- A native Boto3 session is always unattended: configured role MFA and credential processes
  disprove this.
- Removing unsafe providers preserves authentication: native chain fallthrough can choose another
  identity.
- A top-level process-provider guard covers role chains: nested source-profile resolvers disprove
  this.
- Catching SDK exceptions contains all diagnostics: response and refresh logging happen earlier.
- The existing broker can request an MFA code: it asks for the routed declared secret's final value,
  so reusing it would misrepresent the request.
- AWS needs Azure's guide implementation to land first: protected main already discovers plugin
  guide shells.

## Open questions

- The operator must accept the proposed requirements and whether JSON field extraction belongs in
  the mapping contract.
- A real integration run must verify the shipped CLI against bounded AWS fixtures; synthetic SDK
  tests do not prove service permissions or live authentication.
- The process runner's cross-platform isolation and cleanup need behavioral validation. Arbitrary
  operator-configured helpers are not a provider sandbox; out-of-band UI under allowed impact
  remains possible.

## Source assessment

| Source                                          | Quality and angle              | Design use                                                |
| ----------------------------------------------- | ------------------------------ | --------------------------------------------------------- |
| AWS service API reference                       | Primary service contract       | Selectors, value shapes, error taxonomy                   |
| Boto3 operation and credential guides           | Primary SDK behavior           | Native auth, current version, permissions                 |
| Locked installed Botocore source                | Exact dependency behavior      | Nested chains, prompting, processes, logging              |
| Upstream Botocore source                        | Primary, moving implementation | Corroboration; not a replacement for locked-version tests |
| Protected-main backend contract and plugin code | Exact integration contract     | Lifecycle, closed results, registration, guide discovery  |

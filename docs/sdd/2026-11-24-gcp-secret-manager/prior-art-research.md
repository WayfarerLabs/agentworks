# GCP Secret Manager prior art

## Research boundary

Research on 2026-11-24 used protected main `1c13df6c1`, official Google documentation, and released
SDK sources. No live service, operator credential file, or installed dependency was changed.
Requirements and the supported ADC envelope await acceptance in Agention thread 7.

## Existing integration and packages

The existing opt-in GCP plugin contributes Compute and guest tooling. Adding a secret capability
requires descriptor composition and models, not another plugin framework. Source config cannot reuse
GCE's recursive service-account secret reference.

PyPI metadata reported
[Secret Manager 2.32.0](https://pypi.org/pypi/google-cloud-secret-manager/json) and
[google-crc32c 1.9.0](https://pypi.org/pypi/google-crc32c/json) as stable releases. Both fit the
project's Python floor. The Secret Manager dependency does not itself install the checksum library.
Design consequence: add direct declarations for both required packages; avoid unrelated Google
dependency upgrades.

## ADC selection and execution

[Google's ADC guide](https://docs.cloud.google.com/docs/authentication/application-default-credentials)
documents credential selection from an environment-selected file, local ADC, then attached identity.
Plain ADC is not an automatic browser-login operation. Preserve selection order and fail a selected
unusable credential rather than switching identities.

The released
[Google-auth source](https://files.pythonhosted.org/packages/db/4c/fa42116a48bab3f7a143cf5042ecff7df9c8b73f8a376203cd534d1dc966/google_auth-2.56.3.tar.gz)
shows generic loading can perform external-account project discovery and refresh before returning.
Its executable provider inherits stdin in its nominally unattended mode. Local Cloud SDK project
discovery can invoke gcloud without a subprocess timeout. Design consequence: select and parse the
credential file once, use public typed loaders, and avoid project discovery when the source already
supplies the project.

That same released source supplies public constructors for the proposed service-account,
stored-user/workforce, identity-pool, AWS and impersonated forms. Stored-user loading leaves
interactive reauthentication disabled. The impersonation loader supports specified source forms; do
not assume it accepts every ADC type. Executable providers, certificate sources, GDCH and unknown
formats are explicit unsupported selections in this proposal. File suppliers need regular file
enforcement during refresh, not only an initial path check.

## SDK transport and diagnostics

The
[Secret Manager released source](https://files.pythonhosted.org/packages/09/02/472bdf6b83f78a9dfce9530811f3df0a1f5e3cb83ac631d2d82037fc21ea/google_cloud_secret_manager-2.32.0.tar.gz)
shows the generated client can discover certificate providers before choosing a supplied transport.
An explicit REST transport configures mutual TLS only when given a certificate callback. Its public
access callable avoids high-level certificate discovery while preserving SDK request, response and
exception handling. Design consequence: provide credentials and endpoint directly, without a
certificate-command callback or generic client auto-discovery.

SDK REST responses and auth refresh responses can appear in debug logging. Containment must cover
provider logger branches, existing child handlers, credentials and cleanup, not only the exception
returned to the backend. Finite request timeouts do not impose a hard wall across multiple refresh
phases and retries; report that distinction rather than promise a deadline.

## Addresses, integrity, and values

The
[global access API](https://docs.cloud.google.com/secret-manager/docs/reference/rest/v1/projects.secrets.versions/access)
uses project/secret/version resources. The
[regional example](https://docs.cloud.google.com/secret-manager/regional-secrets/access-secret-version-rs)
adds location and uses the matching regional endpoint. Design consequence: pair paths and hosts
explicitly rather than infer regional support from accepting a longer path.

[Integrity guidance](https://docs.cloud.google.com/secret-manager/docs/data-integrity) uses CRC32C
to check payloads. The SDK carries bytes; REST's Base64 representation does not mean SDK bytes need
another Base64 decode. Validate integrity before strict UTF-8 decoding, including the distinction
between missing checksum and present zero for valid empty data. Preview uses the same validation
before discarding plaintext.

## Version and error semantics

The
[version state reference](https://docs.cloud.google.com/secret-manager/docs/reference/rest/v1/projects.secrets.versions#State)
defines disabled and destroyed versions as inaccessible. The
[access guide](https://docs.cloud.google.com/secret-manager/docs/access-secret-version) defines
`latest` by creation and distinguishes alias consistency from numbered-version access. Design
consequence: never scan for a usable version or turn inaccessible state into absence.

[Google's error guidance](https://google.aip.dev/193) separates not-found and permission errors,
including permission checks that conceal existence. Use typed status evidence and fail
permission/state/unknown errors conservatively. Research did not establish a formal promise of one
exact error code for every destroyed-version access case; live testing must confirm what the service
actually returns.

## Refuted assumptions and open evidence

- Generic ADC loading is purely local: external project lookup and gcloud discovery disprove it.
- An executable provider's `interactive=False` flag guarantees no stdin: released source inherits
  it.
- Every non-executable file supplier is safe terminal input: device/pipe paths disprove it.
- A prebuilt SDK transport prevents all high-level certificate discovery: constructor ordering
  disproves it, although that callback need not execute.
- Successful native access is enough for preview availability: checksum/text validity must also be
  proven.
- `latest` means latest enabled: it means most recently created, including inaccessible states.

Open work includes operator acceptance, implementation-level credential/file/transport tests, and
real shipped CLI evidence for supported authentication, regional routing, and failure cases.

## Source assessment

| Source                                 | Quality and angle                                                        |
| -------------------------------------- | ------------------------------------------------------------------------ |
| Google service and authentication docs | Primary API contracts, addressing, identity, values and state            |
| Released Google-auth source            | Exact public-loader and execution behavior at the existing lower bound   |
| Released Secret Manager source         | Exact transport, certificate and response behavior at researched version |
| PyPI metadata                          | Primary distribution/version/dependency facts; recheck at installation   |
| Protected-main contracts and plugins   | Exact integration, lifecycle, source and result boundaries               |

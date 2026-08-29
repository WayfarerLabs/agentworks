# R3 Applied State and SSH Identity: Prior-Art Research

- Status: Design input for R3
- Date: 2026-08-28
- Scope: non-interactive SSH public-identity derivation, fingerprint compatibility, and identity
  selection boundaries

## Executive summary

OpenSSH's native private-key envelope already contains one or more public-key blobs before its
encrypted private section. The first public blob can therefore be extracted from a
password-protected `openssh-key-v1` file without decrypting the private material, spawning a
passphrase prompt, or consulting a sibling public-key file. OpenSSH fingerprints the serialized
public blob with SHA-256, base64-encodes the digest, removes trailing padding, and prefixes it with
`SHA256:`. Agentworks can reproduce that operation with Python's standard library.

The same result cannot be assumed for legacy PEM and PKCS8 private-key envelopes. Those formats do
not share OpenSSH's exposed public-list contract, and extracting their public half may require
decrypting the private material. R3 should preserve those transport paths and record their identity
as unverifiable rather than treating password protection as mismatch or lack of support.

OpenSSH may offer agent-held and default identities in addition to an explicitly configured identity
file unless `IdentitiesOnly` restricts it. R3 therefore proves and records the configured transport
identity only. It does not claim that identity was the sole key offered or accepted, and it does not
resolve ssh-agent selection.

## Findings and design consequences

### The OpenSSH envelope exposes its public identity before encryption

The OpenSSH private-key format is an armored binary envelope beginning with the NUL-terminated
`openssh-key-v1` magic. Its binary body then carries three SSH strings for cipher, KDF, and KDF
options, a 32-bit public-key count, that many SSH public-key strings, and finally one encrypted
private section. Encryption covers the private list, not the preceding public list.

OpenSSH's current parser follows the same boundary. Its public-only path removes the armor, checks
the magic, skips the cipher and KDF strings, reads the key count, and parses the public key without
asking for a passphrase. The shipped implementation currently accepts exactly one key per private
file.

Design consequence: Agentworks will implement a bounded, public-only parser for the native OpenSSH
envelope and require exactly one public blob. It will not decrypt or inspect the private section.
This supports both protected and unprotected native OpenSSH files through the same code path.

Sources:
[OpenSSH private-key format](https://github.com/openssh/openssh-portable/blob/master/PROTOCOL.key),
[OpenSSH public-only private-key parser](https://github.com/openssh/openssh-portable/blob/master/sshkey.c#L3046-L3092).

### SSH strings have a small, deterministic framing contract

RFC 4251 defines `uint32` as four bytes in network byte order. An SSH `string` is one such length
followed by exactly that many bytes, with no terminator. Public-key blobs use this framing and begin
with their algorithm identifier.

Design consequence: the parser needs only a cursor, a bounded `uint32` reader, and a bounded SSH
string reader. Every declared length must fit inside the decoded envelope. The parser will cap the
input and public-key count before allocation or iteration, reject truncation and trailing armor
garbage, and return a typed invalid outcome rather than a partial blob.

Sources:
[RFC 4251, data type representations](https://www.rfc-editor.org/rfc/rfc4251.html#section-5),
[RFC 4253, public-key encoding](https://www.rfc-editor.org/rfc/rfc4253.html#section-6.6).

### OpenSSH fingerprints the serialized public blob

OpenSSH serializes the plain public key, hashes those bytes with the selected digest, and uses
SHA-256 by default. Its base64 representation prefixes the algorithm name and a colon, then removes
trailing `=` padding.

Design consequence: the persisted fingerprint is `SHA256:` plus standard base64 without padding of
`sha256(public_blob)`. Tests will compare Agentworks' result with `ssh-keygen -l -E sha256` for
generated public keys, but runtime code will not spawn `ssh-keygen`.

Sources:
[OpenSSH fingerprint implementation](https://github.com/openssh/openssh-portable/blob/master/sshkey.c#L924-L981),
[OpenBSD ssh-keygen manual](https://man.openbsd.org/ssh-keygen.1).

### Fingerprinting a private path can consult adjacent public state

The `ssh-keygen -l` manual says that the tool tries to find a matching public-key file when asked to
fingerprint a private or public path. That behavior is unsuitable for R3 because a stale sibling
file is exactly one failure mode the applied-state comparison must detect.

Design consequence: runtime derivation never invokes `ssh-keygen -l` on the configured private path
and never appends `.pub`. Public/private comparison parses the configured public file independently
and compares it with the public blob extracted from the configured private file.

Source: [OpenBSD ssh-keygen manual, `-l`](https://man.openbsd.org/ssh-keygen.1).

### Legacy encrypted envelopes do not share the native exposed-public contract

OpenSSH can write legacy PEM private keys, but the native `openssh-key-v1` format's separate public
list is not a general PEM or PKCS8 property. This is an inference from the formats' different
envelope contracts, not a claim that no library could derive their identity after decryption.

Design consequence: a recognized non-OpenSSH private-key armor remains usable by the existing SSH
transport but is classified as non-interactively unverifiable. R3 does not run a command that may
prompt and does not add a cryptography dependency. A malformed file claiming to be native OpenSSH is
invalid rather than unverifiable because its advertised envelope contract could not be parsed.

OpenSSH names PEM and PKCS8 as its two non-native private-key disk formats. OpenSSL's generic
private reader handles traditional and PKCS8 keys, both encrypted and unencrypted. The standardized
PKCS8 labels are `PRIVATE KEY` and `ENCRYPTED PRIVATE KEY`; traditional algorithm-specific labels
include `RSA PRIVATE KEY`, `DSA PRIVATE KEY`, and `EC PRIVATE KEY`. Traditional PEM encryption is
signaled by `Proc-Type: 4,ENCRYPTED` and `DEK-Info` headers before the base64 encrypted body.

Design consequence: R3 recognizes exactly those five matching begin/end pairs within its input cap,
then returns unverifiable without parsing the body or headers. The installed SSH client remains the
authority on legacy validity and accepted variants. This prevents R3 from becoming a second legacy
parser that could reject a password-protected key the transport already accepts. Other labels remain
invalid because they are neither the native format R3 can prove nor a legacy family the current
transport is expected to understand.

Sources:
[OpenSSH private-key format](https://github.com/openssh/openssh-portable/blob/master/PROTOCOL.key),
[OpenSSH private-key disk formats](https://github.com/openssh/openssh-portable/blob/master/sshkey.h),
[RFC 7468 textual encodings](https://www.rfc-editor.org/rfc/rfc7468.html),
[OpenSSL PEM private-key documentation](https://docs.openssl.org/3.4/man3/PEM_read_bio_PrivateKey/),
and [OpenBSD ssh-keygen manual, key formats](https://man.openbsd.org/ssh-keygen.1).

### Configured identity is not necessarily selected identity

OpenSSH may use agent-held identities unless `IdentitiesOnly` restricts the offer set. Agent-held
keys can therefore make a connection succeed even when the explicitly configured private file is not
the key the server accepts.

Design consequence: R3's fingerprint names the identity configured through
`operator.ssh_private_key`, not the sole identity offered or accepted. R3 neither adds
`IdentitiesOnly=yes` nor inspects ssh-agent. A fingerprint match proves that the configured file
matches the recorded configured identity. It does not prove which key completed authentication.

Sources: [OpenBSD ssh_config manual](https://man.openbsd.org/ssh_config.5),
[OpenBSD ssh-agent manual](https://man.openbsd.org/ssh-agent.1).

## Refuted or do-not-rely-on claims

- Do not use a sibling `.pub` file as evidence of the configured private identity. It can be stale
  or independently replaced.
- Do not treat successful SSH as proof that the configured `-i` identity was accepted. An agent or
  default identity may have succeeded.
- Do not treat a passphrase-protected key as unsupported. Native OpenSSH files expose their public
  identity outside the encrypted section.
- Do not treat all malformed private files as drift. A known protected legacy envelope is
  unverifiable; a malformed claimed-native envelope is invalid; file access failure is unavailable.
- Do not decrypt private material merely to produce diagnostic state. R3 does not need that proof
  badly enough to collect a passphrase or add a private-key parsing dependency.

## Open questions deliberately left unresolved

- Which identity an ssh-agent or OpenSSH's default identity search ultimately offers and the server
  accepts.
- Whether a future effort should add `IdentitiesOnly=yes` or record server-observed authentication
  evidence.
- Whether future support should derive public identities from legacy encrypted PEM or PKCS8 through
  a passphrase-aware facility.

## Sources

| Source                                             | Quality                                         | Angle used                                                     |
| -------------------------------------------------- | ----------------------------------------------- | -------------------------------------------------------------- |
| OpenSSH `PROTOCOL.key`                             | Primary implementation specification            | Native envelope order and encryption boundary                  |
| OpenSSH `sshkey.c`                                 | Primary implementation source                   | Public-only parsing and fingerprint formatting                 |
| RFC 4251                                           | Standards-track primary source                  | SSH integer and string framing                                 |
| RFC 4253                                           | Standards-track primary source                  | Public-key blob shape                                          |
| OpenBSD `ssh-keygen(1)`                            | Primary tool documentation                      | Default hash, legacy formats, and sibling public-file behavior |
| OpenBSD `ssh_config(5)` and `ssh-agent(1)`         | Primary client documentation                    | Agent/default identity selection boundary                      |
| OpenSSH `sshkey.h`, RFC 7468, and OpenSSL PEM docs | Primary implementation and format documentation | Recognized legacy envelope families                            |

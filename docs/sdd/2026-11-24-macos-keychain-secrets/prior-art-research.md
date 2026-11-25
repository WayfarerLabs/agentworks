# macOS Keychain prior art

## Boundary and conclusion

Research on 2026-11-24 used protected main `1c13df6c1`, Apple documentation, and Apple's published
Security source at `db15acbe6a7f257a859ad9a3bb86097bfe0679d9`. No keychains or operator credentials
were read. The one-file-keychain proposal awaits acceptance in Agention thread 6. A small native
worker is justified by a process-local UI flag and exact byte handling.

## Store selection

Apple's [TN3137](https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains)
distinguishes file-based and data-protection keychains. Their APIs and access models differ;
generic-password access in a file keychain does not promise Apple Passwords/iCloud access.

Passing no keychain to
[SecKeychainFindGenericPassword](<https://developer.apple.com/documentation/security/seckeychainfindgenericpassword(_:_:_:_:_:_:_:_:)>)
searches a list. A source instead obtains one retained reference via an explicit file or native
default. Apple's
[default-keychain implementation](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/StorageManager.cpp#L571)
does not create a keychain when no default exists. Design consequence: one keychain per source, with
ordinary source precedence composing multiple sources.

## Interaction control

The
[public SecItem header](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/keychain/headers/SecItem.h#L1016)
restricts the old no-authentication-UI option to data-protection items. The file-keychain shim does
not consume the newer authentication context/UI keys. Those keys are not evidence that this backend
can prevent file-keychain dialogs.

The native interaction flag is stored in
[process-local Globals](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/Globals.h#L53).
It selects
[unlock and item-access credentials](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/Globals.cpp#L56),
which the native password retrieval path consumes. Design consequence: disable interaction only
inside a dedicated worker and leave the CLI's setting untouched. Each worker handles one batch and
exits, so no restoration is needed inside that process. Allowed native authentication may change
shared unlock/access state; never promise rollback or relock on cleanup.

## Exact bytes and absence evidence

The
[direct legacy lookup implementation](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/SecKeychain.cpp#L815)
accepts service/account lengths and returns password length plus data. Empty identifiers omit
filters, so require both identifiers nonempty. Copy data by length, then free it with the native
content-free function and release retained references. Strict UTF-8 decoding preserves exact text
without trimming or NUL termination.

[Keychain status](<https://developer.apple.com/documentation/security/seckeychaingetstatus(_:_:)>)
must be checked after obtaining a reference: an open call alone can succeed for an absent file. The
[unlock implementation](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/SecKeychain.cpp#L196)
supports prompting without a supplied password. Preflight and explicit allowed unlock help
distinguish unusable sources from absent items.

Do not overstate that evidence. The
[SecItem shim](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/SecItem.cpp#L5200)
can turn an unsuccessful iterator into item-not-found, and the
[underlying cursor](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/KCCursor.cpp#L182)
catches some database activation failures. Direct lookup preserves later access/decryption errors,
but native corruption/removal races still need observation. Do not treat an error from opening,
checking, or unlocking a source as ordinary item absence.

## Alternatives and rejected assumptions

| Approach                      | Finding and design consequence                                                                                                                                        |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Direct native API with ctypes | Small interface, no added dependency; exact ABI, lengths, ownership and native tests are necessary. Existing terminal/platform bindings provide lazy-import patterns. |
| PyObjC Security               | Its wrapper handles bytes and native freeing but adds three Darwin-only distributions. A supported option, not necessary for the selected small interface.            |
| security CLI                  | Apple's printing code hex-encodes non-printable password bytes and appends a newline. Removing a newline cannot recover exact native data.                            |
| SecItem authentication flags  | No demonstrated file-keychain no-UI enforcement; data-protection behavior cannot be assumed to transfer.                                                              |

Primary sources for the alternatives are
[Apple's CLI printing code](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/SecurityTool/macOS/keychain_find.c#L408)
and the
[PyObjC wrapper](https://github.com/ronaldoussoren/pyobjc/blob/main/pyobjc-framework-Security/Modules/_Security.m#L338).

## Open evidence

Linux can establish portable registration, typed protocol validation, deadlines, cleanup and
simulated outcomes. It cannot establish the Security ABI, real UI suppression, native status
taxonomy, application ACL identity, or prompt cleanup after worker termination. Live macOS tests
must use disposable keychains, record versions, and cover absent/locked/denied/invalid sources,
exact UTF-8 and empty values, cancellation, concurrent state changes, and cleanup.

## Source assessment

| Source                                      | Quality and use                                                                                                                              |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Apple API documentation and TN3137          | Primary API/store contract; some pages describe newer stores separately.                                                                     |
| Pinned Apple Security source                | Primary implementation evidence for file-keychain routing, UI credentials, output and errors; runtime macOS behavior still needs validation. |
| PyObjC source                               | Primary wrapper implementation; alternative assessment only.                                                                                 |
| Protected-main plugin and backend contracts | Exact portable registration, lifecycle, preview and closed-result requirements.                                                              |

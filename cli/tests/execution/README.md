# Buffered transport proof checks

`wsl_unc_hold_probe.py` is a native Windows experiment for an existing disposable WSL2 distro. It
requires the distro to be stopped, an exact matching `--target` and `--confirm-target`, its actual
`--idle-seconds`, and a total `--budget-seconds`. Supply `--unrelated` only for a second installed
distro that is already running. The probe derives a hold duration of three idle timeouts and opens
only `/etc/os-release` through both WSL UNC aliases. It first measures whether the target stops
without an open handle, then measures retention and release. It uses fixed `/bin/true` for starts
and terminates only the confirmed target. JSON Lines report bounded observations and cleanup; the
final strict dispatch drain conclusion is always `UNKNOWN`.

Run local checks from `cli/`:

```console
uv run pytest tests/execution
```

The bootstrap tests run synthetic Linux commands locally. Native tests use controlled REST
responses, local worker processes and fixture-owned loopback HTTPS servers. These are not evidence
of live SSH or Proxmox compatibility. The fresh-process import test makes the retired execution
modules unavailable rather than assuming pytest's already-loaded modules establish independence. It
discovers and imports every supplied execution module, so a combined checkout exercises SSH
automatically and any failed import fails the check. A standalone checkout does not claim coverage
of an absent carrier.

`test_target_identity.py` exercises the private owned identity composer with the real local account
helper and synthetic identity, refusal, deadline, termination and operation-coordination cases. Its
sudo and demotion checks validate plan selection only; they do not invoke those transitions or
establish destination acceptance.

## Combined-tree integration run

The integration tester can merge pinned transport and SSH commits into a disposable local branch.
Record both input SHAs, the combined SHA and any conflict resolutions; install and verify that exact
tree. Neither origin branch needs to merge before testing. Resolve contract-affecting conflicts with
the transport owner, not by silently changing the candidate during validation.

Use the tester's authorized inventory and budget. This harness neither discovers credentials nor
authorizes VM creation, elevation or deletion. QGA executes as root unless the proof composition
explicitly demotes it; report the identity actually tested. No production RunContext is involved.

Construct the concrete carrier using already-resolved connection values, then call:

```python
from tests.execution.conformance import check_buffered_contract

# carrier is the explicitly authorized SSH or Proxmox carrier under test.
observations = check_buffered_contract(carrier, seconds_per_case=15)
for observation in observations:
    print(observation)
```

For native delivery, construct `ProxmoxConnection` with an HTTPS origin whose hostname matches the
node certificate and an independently trusted cluster `ca_bundle=Path(...)` when system trust is
insufficient. Verification cannot be disabled. Confirm trusted-CA success and wrong-CA/wrong-host
refusal without publishing credentials. Do not install certificates or change test-bed networking
unless the tester's existing authority permits it; report an unavailable trusted route explicitly.

Run the same vectors against both carriers. A failed measurement raises rather than generating a
passing observation, including under optimized Python. Results contain case names and safe evidence,
not payloads or credentials. The 255 case accepts truthful SSH ambiguity, never guessed success.

The vectors cover literal arguments, binary source/input separation, explicit Bash, environment/cwd,
EOF with an unusable TMPDIR, exits 0/1/255 and sensitive reflection suppression with deliberate
exit 37. The distinctive sensitive exit rejects an outer shell that merely consumes input and exits
zero; it is controlled-case evidence, not authentication against arbitrary account startup behavior.
`Observation.reported_exit` records raw remote command-chain completion, not proof that the prepared
bootstrap or application ran. Captured output must pass guest framing checks; suppressed/discarded
output provides no such evidence. The vectors do not alone prove no staging or the whole carrier
contract. Also run the carrier-specific deadline, dropped observation, interruption, truncation and
cleanup cases. Deadline expiry stops local observation, not guest execution; ordinary guest process
trees can survive and this proof has no cancellation handle or reaper. Use bounded harmless
workloads, independently verify owned descendants terminate, and never use a command-pattern kill as
proof of complete cleanup. Never replay a possibly dispatched case to obtain a prettier result.
Record tool versions, workstation OS and VM-platform versions.

Linux prerequisite checks include Bash 5.1 or newer, GNU base64/env (including
`--default-signal=PIPE`), `/dev/fd`, and getent/id for destination account-shell selection. The
initial slice refuses login/interactive startup. MacOS/Windows workstations, other guest shells,
identity/elevation, live streams and terminals require their own evidence. An affected code or
contract change requires retesting the corresponding observations.

The eight vectors use fixed interpreters, not `Shell.USER_DEFAULT`. Also exercise the supported
destination-account default shell through each carrier. Independently record the destination UID and
account-shell path, then prepare a harmless `/bin/cat` script with `Shell.USER_DEFAULT`, finite
binary stdin and `env={"SHELL": "/does/not/exist"}`. Require byte-exact output, complete guest
streams, no framing/bootstrap failure, observed completion zero and no carrier failure. The invalid
environment hint must not replace the real account lookup. The buffered proof supports sh/bash
account paths only; do not change an account shell to manufacture a pass. Account startup hooks
remain distinct from the requested script interpreter and need separately scoped evidence.

Record exact guest Bash/coreutils versions, not just executable presence. Locally, the fault tests
have measured Bash 5.2.15; a documented 5.1 minimum is not evidence of a 5.1 run. Near-limit input
checks must account for the complete encoded envelope: 262,144 bytes at preparation. Native delivery
has separate 65,536-byte limits for its input field and complete serialized HTTP body, including
bootstrap argv and JSON escaping. Record pre-dispatch refusal separately from provider acceptance,
and preserve actual boundary values rather than generalizing one raw-stdin example. Close the report
with independent process, provider-record and test-resource cleanup observations, including the
disposition of any unfinished provisioning from the run.

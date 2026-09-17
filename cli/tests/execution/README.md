# Buffered transport proof checks

Run local checks from `cli/`:

```console
uv run pytest tests/execution
```

The bootstrap tests run synthetic Linux commands locally. Native tests use controlled REST responses
and local worker processes. These are not evidence of live SSH or Proxmox compatibility. The
fresh-process import test makes the retired execution modules unavailable rather than assuming
pytest's already-loaded modules establish independence.

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

Run the same vectors against both carriers. A failed measurement raises rather than generating a
passing observation, including under optimized Python. Results contain case names and safe evidence,
not payloads or credentials. The 255 case accepts truthful SSH ambiguity, never guessed success.

The vectors cover literal arguments, binary source/input separation, explicit Bash, environment/cwd,
EOF with an unusable TMPDIR, exits 0/1/255 and sensitive reflection suppression. They do not alone
prove no staging or the whole carrier contract. Also run the carrier-specific deadline, dropped
observation, interruption, truncation and cleanup cases. Never replay a possibly dispatched case to
obtain a prettier result. Record tool versions, workstation OS and VM-platform versions.

Linux prerequisite checks include Bash, GNU base64/env (including `--default-signal=PIPE`),
`/dev/fd`, and getent/id for destination account-shell selection. The initial slice refuses
login/interactive startup. MacOS/Windows workstations, other guest shells, identity/elevation, live
streams and terminals require their own evidence. An affected code or contract change requires
retesting the corresponding observations.

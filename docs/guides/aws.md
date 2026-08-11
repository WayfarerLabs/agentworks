# Using AWS with Agentworks

Agentworks provisions EC2 through the opt-in `aws` system plugin. Enable it before using an
`aws-ec2` site or a VM template that requests the optional guest `aws-cli` install command:

```toml
[plugins]
system = ["aws"]
```

## Optional guest AWS CLI

Add `aws-cli` to a template's `system_install_commands` when a guest needs the AWS CLI v2. The
installer accepts only `x86_64` and `aarch64`, downloads AWS's current official v2 archive and its
detached signature, checks the embedded AWS CLI Team public key's full fingerprint, and verifies the
archive before extraction. It uses a private temporary directory and GnuPG home, then installs or
updates explicitly at `/usr/local/aws-cli` with the launcher in `/usr/local/bin`.

On initialization and reinitialization, the runner requires both an executable public launcher at
`/usr/local/bin/aws` and Agentworks' durable completion marker at
`/usr/local/aws-cli/.agentworks-v2-complete`. The command also requires the managed internal
`/usr/local/aws-cli/v2/current/bin/aws` executable. A completed managed installation therefore skips
the 120-second installer transport without running the relatively heavy CLI.

These are structural completion checks: Agentworks checks both executable paths and the marker, but
does not execute or hash the managed CLI to detect arbitrary later byte-content corruption. A
missing marker, launcher, internal executable, or executable bit enters the installer's explicit
recovery path.

Before a managed repair, Agentworks removes the old marker. It recreates the marker only after the
verified official installer exits successfully and unprivileged executable checks pass for both the
public launcher and internal binary. A failed update or malformed zero-exit installer result
therefore leaves the marker absent so the next initialization retries. If no managed layout exists,
the command checks `aws --version` and leaves a valid external AWS CLI v2 on `PATH` alone. AWS CLI
v1 and partial managed layouts continue through the verified v2 install or `--update`. Unsupported
architectures, installer failure, key fingerprint changes, and invalid signatures stop
initialization without writing the completion marker.

The command is guest tooling, not a credential mechanism. EC2 provisioning continues to use boto3
and the host's configured AWS credential chain. The guest installer never runs `aws configure`,
writes an AWS credentials or profile file, or changes host authentication.

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

An existing `aws-cli/2.` executable is left alone. An AWS CLI v1 executable has the same `aws` name,
so it is deliberately upgraded through the verified v2 path instead. Unsupported architectures, key
fingerprint changes, and invalid signatures stop initialization without installing the archive.

The command is guest tooling, not a credential mechanism. EC2 provisioning continues to use boto3
and the host's configured AWS credential chain. The guest installer never runs `aws configure`,
writes an AWS credentials or profile file, or changes host authentication.

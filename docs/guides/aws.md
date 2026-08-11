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

The resource deliberately declares no completion checks, so every initialization and
reinitialization runs the verified recipe. Each run downloads and verifies the official archive; an
`aws` executable already on `PATH`, whether v1 or v2, never short-circuits the recipe. If
`/usr/local/aws-cli` is absent, the command performs the official fresh install. If that managed
path exists, including as a partial installation, it invokes the official installer with `--update`.

Unsupported architectures, installer failure, key fingerprint changes, and invalid signatures stop
initialization. The private temporary directory is cleaned on success and failure.

The command is guest tooling, not a credential mechanism. EC2 provisioning continues to use boto3
and the host's configured AWS credential chain. The guest installer never runs `aws configure`,
writes an AWS credentials or profile file, or changes host authentication.

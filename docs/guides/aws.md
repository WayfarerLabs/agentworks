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
updates an Agentworks-owned layout at `/usr/local/lib/agentworks/aws-cli`. The public launchers are
the absolute links `/usr/local/bin/aws` and `/usr/local/bin/aws_completer`, targeting the
corresponding executables under `/usr/local/lib/agentworks/aws-cli/v2/current/bin`.

The resource deliberately declares no completion checks, so every initialization and
reinitialization runs the verified recipe. Each run downloads and verifies the official archive; an
`aws` executable already on `PATH`, whether v1 or v2, never short-circuits the recipe. If the
managed directory, both exact launcher links, and both current executables are complete, the command
invokes the official installer with `--update`. If all three managed entries are absent, it performs
the official fresh install.

Every other owned state is incomplete. This includes missing or non-executable current artifacts,
missing launchers, exact dangling launchers into the reserved namespace, and a non-directory entry
at the reserved install path. The command downloads and verifies the archive first, then removes
only the reserved install entry and exact owned launcher links before performing a fresh install.
Removing the incomplete layout before invoking the installer also repairs a same-version layout that
the official installer would otherwise leave unchanged.

Agentworks never claims an existing launcher merely because of its name. A launcher is owned only
when it is a symbolic link whose raw target exactly matches the corresponding absolute target in the
reserved install directory. A regular file, directory, or link to any other target is a collision.
The command fails before download, verification, temporary-directory creation, or system mutation,
leaving the collision, the other launcher, and the managed directory unchanged.

Unsupported architectures, installer failure, key fingerprint changes, and invalid signatures stop
initialization. The private temporary directory is cleaned on success and failure.

The command is guest tooling, not a credential mechanism. EC2 provisioning continues to use boto3
and the host's configured AWS credential chain. The guest installer never runs `aws configure`,
writes an AWS credentials or profile file, or changes host authentication.

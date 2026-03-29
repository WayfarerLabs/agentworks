# 2. Use Debian as the VM base image

Date: 2026-03-05

## Status

Accepted

## Context

Agentworks provisions VMs across multiple platforms (Lima, Azure, WSL2) and manages their full
lifecycle: system packages, user tooling, shell configuration, agent isolation, and tool integrity.
This management layer depends heavily on assumptions about the OS: package manager commands, file
paths, init systems, user management tools, and available system utilities.

We could either support multiple distributions (bring-your-own) or standardize on one. Supporting
multiple distros would mean conditional logic throughout the init system (apt vs dnf vs apk,
systemd vs openrc, /usr/sbin vs /sbin, etc.), broader test matrices, and weaker guarantees about
what works.

## Decision

Agentworks is an opinionated platform and that very much applies here.

We standardize on Debian 12 (Bookworm) as the single supported base image across all platforms.
The bootstrap script, initializer, and all system-level tooling assume Debian.

While we may eventually support additional distros, this will be a big deal and a significant
departure from the original vision of agentworks. A wholesale switch to a "better" distro is more
likely than incurring the complexity of multi-distro support.

## Consequences

- The init system, catalog, and agent provisioning can be written against a single, known target.
  No conditional package manager logic, no distro detection, no compatibility shims.
- Third-party apt sources (GitHub CLI, HashiCorp, mise, Node.js) all publish Debian packages,
  giving us broad tool coverage without workarounds.
- Debian stable provides long support cycles, conservative updates, and timely security patches.
  VMs are predictable across reinit cycles.
- Cloud images are available for all our platforms: Debian publishes official images for Lima
  (qcow2), Azure (marketplace), and WSL2 (importable rootfs).
- Tradeoff: users cannot bring their own distro. This is intentional. The value of agentworks
  comes from the management layer on top of the OS, and that layer works because the OS is known.
- Tradeoff: Debian stable packages can lag behind. This is mitigated by mise for user-level tools,
  third-party apt sources for specific system packages, and user install commands for anything else.

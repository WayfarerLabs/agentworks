"""Render the root package-service script for one adjacent transition."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from .remote import REMOTE_ROOT

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .journal import UpgradePair

_DEBIAN_MIRROR = "https://deb.debian.org/debian"
_SECURITY_MIRROR = "https://security.debian.org/debian-security"


def render_upgrade_script(
    pair: UpgradePair,
    *,
    target_suites: Sequence[str],
) -> str:
    """Render a deterministic script whose action is selected by argv."""
    directory = f"{REMOTE_ROOT}/{pair.dirname}"
    security_suites = " ".join(suite for suite in target_suites if "security" in suite)
    ordinary_suites = " ".join(suite for suite in target_suites if "security" not in suite)
    if not ordinary_suites:
        raise ValueError("target policy must include at least one ordinary Debian suite")
    source_document = _deb822_stanza(_DEBIAN_MIRROR, ordinary_suites)
    if security_suites:
        source_document += "\n" + _deb822_stanza(_SECURITY_MIRROR, security_suites)

    q_directory = shlex.quote(directory)
    q_source_document = shlex.quote(source_document)
    return f"""#!/bin/bash
set -euo pipefail
umask 077
action=${{1:?upgrade action required}}
exec 9>{q_directory}/lock
flock -n 9 || {{ echo 'another Agentworks upgrade process owns this journal' >&2; exit 75; }}
export DEBIAN_FRONTEND=noninteractive
export APT_LISTCHANGES_FRONTEND=none

case "$action" in
  source-update)
    apt-get update
    apt-get -y full-upgrade
    dpkg --audit
    ;;
  switch-sources)
    archive={q_directory}/sources-before
    archive_marker="$archive/.archive-complete"
    archive_created=false
    if [ ! -e "$archive_marker" ]; then
      if [ -e "$archive" ]; then
        echo 'incomplete original-source archive requires repair' >&2
        exit 76
      fi
      install -d -m 0700 "$archive"
      if [ -f /etc/apt/sources.list ]; then
        cp -a /etc/apt/sources.list "$archive/sources.list"
      fi
      if [ -d /etc/apt/sources.list.d ]; then
        cp -a /etc/apt/sources.list.d "$archive/sources.list.d"
      fi
      : > "$archive_marker"
      sync -f "$archive_marker"
      archive_created=true
    fi
    : > /etc/apt/sources.list
    for path in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
      [ -f "$path" ] || continue
      if [ "$archive_created" = false ] && [ "$path" = /etc/apt/sources.list.d/debian.sources ]; then
        continue
      fi
      disabled="$path.agentworks-disabled"
      [ ! -e "$disabled" ] || {{ echo "disabled-source collision: $disabled" >&2; exit 76; }}
      mv "$path" "$disabled"
    done
    printf '%s\\n' {q_source_document} > /etc/apt/sources.list.d/debian.sources
    apt-get update
    : > {q_directory}/.switch-sources-complete
    sync -f {q_directory}/.switch-sources-complete
    ;;
  minimal-upgrade)
    apt-get -y upgrade --without-new-pkgs
    dpkg --audit
    ;;
  full-upgrade)
    apt-get -y full-upgrade
    dpkg --audit
    ;;
  *)
    echo "unsupported upgrade action: $action" >&2
    exit 64
    ;;
esac
"""


def _deb822_stanza(uri: str, suites: str) -> str:
    return f"""Types: deb
URIs: {uri}
Suites: {suites}
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""

"""Successful synthetic clients identify the family supported on the actual host."""

from __future__ import annotations

import os


def client_banner(version: str = "9.9p1") -> str:
    prefix = "OpenSSH_for_Windows_" if os.name == "nt" else "OpenSSH_"
    return prefix + version

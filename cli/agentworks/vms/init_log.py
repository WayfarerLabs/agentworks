"""Init log writer for VM initialization.

Captures init output to a structured log file for troubleshooting.
Log files are written to ~/.local/share/agentworks/logs/.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

LOG_DIR = Path.home() / ".local" / "share" / "agentworks" / "logs"


def log_path_for_vm(vm_name: str) -> Path:
    """Return the log path for a VM init, using current timestamp."""
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return LOG_DIR / f"vm-init-{vm_name}-{timestamp}.log"


def find_init_logs(vm_name: str) -> list[Path]:
    """Find all init log files for a VM, newest first."""
    if not LOG_DIR.exists():
        return []
    logs = sorted(LOG_DIR.glob(f"vm-init-{vm_name}-*.log"), reverse=True)
    return logs


def delete_init_logs(vm_name: str) -> int:
    """Delete all init log files for a VM. Returns count deleted."""
    logs = find_init_logs(vm_name)
    for log in logs:
        log.unlink(missing_ok=True)
    return len(logs)


class InitLogger:
    """Captures init step output to a log file.

    Usage:
        logger = InitLogger("my-vm")
        logger.step("Installing packages")
        logger.output("apt-get install -y ...")
        logger.warning("Some package failed")
        logger.close()
    """

    def __init__(self, vm_name: str) -> None:
        self.vm_name = vm_name
        self.path = log_path_for_vm(vm_name)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buf = io.StringIO()
        self._warnings: list[str] = []
        self._write_header()

    def _write_header(self) -> None:
        ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._buf.write(f"# VM Init Log: {self.vm_name}\n")
        self._buf.write(f"# Started: {ts}\n\n")

    def step(self, name: str) -> None:
        """Log the start of an init step."""
        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._buf.write(f"--- [{ts}] {name} ---\n")

    def output(self, text: str) -> None:
        """Log command output."""
        if text:
            self._buf.write(text)
            if not text.endswith("\n"):
                self._buf.write("\n")

    def warning(self, msg: str) -> None:
        """Record a warning (also written to the log)."""
        self._warnings.append(msg)
        self._buf.write(f"WARNING: {msg}\n")

    def error(self, msg: str) -> None:
        """Log a fatal error."""
        self._buf.write(f"ERROR: {msg}\n")

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    @property
    def has_warnings(self) -> bool:
        return len(self._warnings) > 0

    def close(self) -> None:
        """Flush the log to disk."""
        ts = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._buf.write(f"\n# Finished: {ts}\n")
        if self._warnings:
            self._buf.write(f"# Warnings: {len(self._warnings)}\n")
            for w in self._warnings:
                self._buf.write(f"#   - {w}\n")
        self.path.write_text(self._buf.getvalue())

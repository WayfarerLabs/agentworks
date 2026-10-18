"""Core-selected stage entry using the same fixed parent as prepared loading."""

from __future__ import annotations

from ._file_stage_guest import main as _stage_main


def main(nonce: str) -> int:
    return _stage_main(nonce, fixed_scratch_root=True)

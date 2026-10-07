"""Fixture-only POSIX evidence for the runtime-prerequisite experiment.

These cases exercise real shell object checks on Linux and macOS. They do not
claim native macOS prompt behavior or platform acceptance.
"""

from __future__ import annotations

import sys

import pytest

if sys.platform == "win32":  # pragma: no cover - platform guard
    pytest.skip("The runtime-prerequisite experiment requires POSIX shell checks", allow_module_level=True)

import dataclasses
import os
import shlex
import shutil
import stat
from pathlib import Path

from tests.execution.runtime_prerequisite_probe import (
    PrerequisiteObservation,
    PrerequisiteState,
    observe_runtime_prerequisite,
)


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _emulated_runtime(path: Path, state: PrerequisiteState, *, before_frame: str = "") -> Path:
    return _executable(
        path,
        '[ "$1" = -I ]\n'
        '[ "$2" = -S ]\n'
        '[ "$3" = -B ]\n'
        '[ "$4" = -c ]\n' + before_frame + f'printf \'AGW_RUNTIME_1:%s:{state.value}:%s\\n\' "$6" "$7"\n',
    )


def _fixture_probe(candidates: tuple[Path, ...], shim: Path, *, timeout: float = 5.0) -> PrerequisiteObservation:
    return observe_runtime_prerequisite(
        timeout=timeout,
        fixture_candidates=candidates,
        fixture_system_shim=shim,
    )


def _tree(path: Path) -> set[Path]:
    return {entry.relative_to(path) for entry in path.rglob("*")}


def _assert_process_gone(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_actual_python_311_is_ready_without_startup_environment_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = shutil.which("python3.11")
    if python is None:
        pytest.skip("This host has no Python 3.11 interpreter for the compatibility experiment")

    startup = tmp_path / "startup.py"
    startup.write_text(f"open({os.fspath(tmp_path / 'startup-ran')!r}, 'w').close()\n")
    injected = tmp_path / "injected"
    injected.mkdir()
    (injected / "sitecustomize.py").write_text(f"open({os.fspath(tmp_path / 'sitecustomize-ran')!r}, 'w').close()\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONHOME", os.fspath(tmp_path / "bad-home"))
    monkeypatch.setenv("PYTHONPATH", os.fspath(injected))
    monkeypatch.setenv("PYTHONSTARTUP", os.fspath(startup))
    monkeypatch.setenv("PYTHONINSPECT", "1")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", os.fspath(tmp_path / "cache"))
    before = _tree(tmp_path)

    candidate = Path(python).resolve()
    observation = _fixture_probe((candidate,), tmp_path / "absent-shim")

    assert observation == PrerequisiteObservation(PrerequisiteState.READY, candidate)
    assert _tree(tmp_path) == before


def test_fixed_shell_and_runtime_do_not_inherit_startup_or_loader_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup_marker = tmp_path / "shell-startup-ran"
    startup = tmp_path / "shell-startup"
    startup.write_text(f"touch {shlex.quote(os.fspath(startup_marker))}\n")
    hostile_environment = {
        "AGENTWORKS_ENV_CANARY": "inherited",
        "BASH_ENV": os.fspath(startup),
        "BASHOPTS": "extdebug",
        "BASH_XTRACEFD": "9",
        "DYLD_INSERT_LIBRARIES": os.fspath(tmp_path / "absent.dylib"),
        "ENV": os.fspath(startup),
        "LD_PRELOAD": os.fspath(tmp_path / "absent.so"),
        "SHELLOPTS": "xtrace",
    }
    for name, value in hostile_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(
        "BASH_FUNC_printf%%",
        f'() {{ touch {shlex.quote(os.fspath(startup_marker))}; builtin printf "$@"; }}',
    )
    monkeypatch.setenv("BASH_FUNC_agentworks_canary%%", "() { :; }")
    environment_checks = """[ "${AGENTWORKS_ENV_CANARY+x}" != x ]
[ "${BASH_ENV+x}" != x ]
[ "${ENV+x}" != x ]
[ "${LD_PRELOAD+x}" != x ]
[ "${DYLD_INSERT_LIBRARIES+x}" != x ]
[ "${PATH-}" = /usr/bin:/bin ]
[ "${LANG-}" = C ]
[ "${LC_ALL-}" = C ]
! command -v agentworks_canary >/dev/null 2>&1
"""
    candidate = _emulated_runtime(
        tmp_path / "candidate",
        PrerequisiteState.READY,
        before_frame=environment_checks,
    )

    observation = _fixture_probe((candidate,), tmp_path / "absent-shim")

    assert observation == PrerequisiteObservation(PrerequisiteState.READY, candidate)
    assert not startup_marker.exists()


@pytest.mark.parametrize("kind", ["broken-symlink", "not-executable", "directory"])
def test_first_existing_unusable_entry_is_selected(kind: str, tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    if kind == "broken-symlink":
        candidate.symlink_to(tmp_path / "absent-target")
    elif kind == "not-executable":
        candidate.write_text("not executable")
    else:
        candidate.mkdir()

    observation = _fixture_probe((candidate,), tmp_path / "absent-shim")

    assert observation == PrerequisiteObservation(PrerequisiteState.UNUSABLE, candidate)


def test_absent_explicit_candidate_is_the_only_candidate(tmp_path: Path) -> None:
    fallback_marker = tmp_path / "fallback-ran"
    fallback = _emulated_runtime(
        tmp_path / "fallback",
        PrerequisiteState.READY,
        before_frame=f"touch {shlex.quote(os.fspath(fallback_marker))}\n",
    )
    explicit = tmp_path / "absent-explicit"

    observation = observe_runtime_prerequisite(
        explicit,
        fixture_candidates=(fallback,),
        fixture_system_shim=tmp_path / "absent-shim",
    )

    assert observation == PrerequisiteObservation(PrerequisiteState.MISSING, None)
    assert not fallback_marker.exists()


def test_missing_fixed_entry_allows_the_next_candidate(tmp_path: Path) -> None:
    second = _emulated_runtime(tmp_path / "second", PrerequisiteState.READY)

    observation = _fixture_probe((tmp_path / "absent-first", second), tmp_path / "absent-shim")

    assert observation == PrerequisiteObservation(PrerequisiteState.READY, second)


def test_selected_bad_entry_does_not_fall_back_to_good_entry(tmp_path: Path) -> None:
    first = tmp_path / "not-executable"
    first.write_text("bad")
    fallback_marker = tmp_path / "fallback-ran"
    second = _emulated_runtime(
        tmp_path / "good",
        PrerequisiteState.READY,
        before_frame=f"touch {shlex.quote(os.fspath(fallback_marker))}\n",
    )

    observation = _fixture_probe((first, second), tmp_path / "absent-shim")

    assert observation == PrerequisiteObservation(PrerequisiteState.UNUSABLE, first)
    assert not fallback_marker.exists()


@pytest.mark.parametrize("alias_kind", ["direct", "symlink-chain", "hardlink"])
def test_system_shim_alias_is_never_invoked(alias_kind: str, tmp_path: Path) -> None:
    invoked = tmp_path / "shim-invoked"
    shim = _executable(
        tmp_path / "system-python3",
        f"touch {shlex.quote(os.fspath(invoked))}\nexit 91\n",
    )
    if alias_kind == "direct":
        candidate = shim
    elif alias_kind == "symlink-chain":
        intermediate = tmp_path / "shim-link"
        intermediate.symlink_to(shim)
        candidate = tmp_path / "candidate-link"
        candidate.symlink_to(intermediate)
    else:
        candidate = tmp_path / "candidate-hardlink"
        os.link(shim, candidate)

    observation = _fixture_probe((candidate,), shim)

    assert observation == PrerequisiteObservation(PrerequisiteState.SHIM, candidate)
    assert not invoked.exists()


def test_only_known_system_path_reports_shim_without_invoking_it(tmp_path: Path) -> None:
    invoked = tmp_path / "shim-invoked"
    shim = _executable(tmp_path / "system-python3", f"touch {shlex.quote(os.fspath(invoked))}\n")

    observation = _fixture_probe((tmp_path / "absent",), shim)

    assert observation == PrerequisiteObservation(PrerequisiteState.SHIM, shim)
    assert not invoked.exists()


def test_no_candidate_or_system_path_reports_missing(tmp_path: Path) -> None:
    observation = _fixture_probe((tmp_path / "absent",), tmp_path / "absent-shim")

    assert observation == PrerequisiteObservation(PrerequisiteState.MISSING, None)


def test_synthetic_old_version_frame_is_not_claimed_as_real_old_python_evidence(tmp_path: Path) -> None:
    synthetic = _emulated_runtime(tmp_path / "synthetic-old-python", PrerequisiteState.UNSUPPORTED)

    observation = _fixture_probe((synthetic,), tmp_path / "absent-shim")

    assert observation == PrerequisiteObservation(PrerequisiteState.UNSUPPORTED, synthetic)


def test_candidate_path_with_spaces_and_shell_metacharacters_is_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expansion_marker = tmp_path / "expanded"
    monkeypatch.chdir(tmp_path)
    candidate = _emulated_runtime(
        tmp_path / "python $(touch expanded); 'literal'\nname",
        PrerequisiteState.READY,
    )

    observation = _fixture_probe((candidate,), tmp_path / "absent shim; false")

    assert observation == PrerequisiteObservation(PrerequisiteState.READY, candidate)
    assert not expansion_marker.exists()


def test_stderr_is_discarded_from_a_valid_structured_observation(tmp_path: Path) -> None:
    canary = "raw-stderr-canary-96f3"
    synthetic = _emulated_runtime(
        tmp_path / "synthetic-old-python",
        PrerequisiteState.UNSUPPORTED,
        before_frame=f"printf '%s' {shlex.quote(canary)} >&2\n",
    )

    observation = _fixture_probe((synthetic,), tmp_path / "absent-shim")

    assert observation.state is PrerequisiteState.UNSUPPORTED
    assert canary not in repr(observation)
    assert {field.name for field in dataclasses.fields(observation)} == {"state", "selected_path"}


@pytest.mark.parametrize(
    "body",
    [
        "exit 0\n",
        "printf 'not-a-frame\\n'\n",
        "printf 'AGW_RUNTIME_1:00000000000000000000000000000000:ready:%s\\n' \"$7\"\n",
        'printf \'AGW_RUNTIME_1:%s:ready:%s\\nextra\' "$6" "$7"\n',
    ],
)
def test_absent_or_invalid_frame_is_observation_unknown(body: str, tmp_path: Path) -> None:
    candidate = _executable(tmp_path / "candidate", body)

    observation = _fixture_probe((candidate,), tmp_path / "absent-shim")

    assert observation == PrerequisiteObservation(PrerequisiteState.OBSERVATION_UNKNOWN, None)


def test_real_process_failure_is_not_misclassified_as_missing(tmp_path: Path) -> None:
    candidate = _executable(tmp_path / "candidate", "printf 'provider-secret-error' >&2\nexit 42\n")

    observation = _fixture_probe((candidate,), tmp_path / "absent-shim")

    assert observation == PrerequisiteObservation(PrerequisiteState.OBSERVATION_UNKNOWN, None)
    assert "provider-secret-error" not in repr(observation)


def test_timeout_is_unknown_and_kills_and_reaps_the_owned_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    candidate = _executable(
        tmp_path / "candidate",
        f"printf '%s' \"$$\" > {shlex.quote(os.fspath(pid_file))}\ntrap '' TERM\nexec /bin/sleep 60\n",
    )

    observation = _fixture_probe((candidate,), tmp_path / "absent-shim", timeout=0.5)

    assert observation == PrerequisiteObservation(PrerequisiteState.OBSERVATION_UNKNOWN, None)
    _assert_process_gone(int(pid_file.read_text()))

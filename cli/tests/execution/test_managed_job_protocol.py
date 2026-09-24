"""Behavioral and adversarial coverage for private managed-job fact bytes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.execution import _managed_job_wire as wire
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_job_protocol import (
    MAX_MANAGED_JOB_FACT_BYTES,
    BoundaryEmptyFact,
    ManagedJobFactError,
    StreamDisposition,
    StreamEndFact,
    StreamName,
    WorkloadWaitFact,
    decode_managed_job_fact,
    encode_managed_job_fact,
    fact_matches_receipt,
    managed_launch_receipt_sha256,
)
from agentworks.execution._managed_runs import (
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution.models import Shell


def _receipt() -> ManagedRunReceipt:
    run = ManagedRunIdentity("1" * 32)
    return ManagedRunReceipt(
        run,
        run.unit_name,
        ManagedRunSpec(
            ManagedTargetIdentity(
                ManagedTargetKind.VM,
                "vm-one",
                f"v1:{'a' * 64}",
                "00000000-0000-4000-8000-000000000001",
            ),
            IdentityExpectation(1001, 1001, (1001, 1002)),
            ManagedShellIdentity(Shell.USER_DEFAULT, "/bin/bash", login=True),
            ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-7"),
            ManagedRunLifetime.INDEPENDENT,
        ),
    )


def _facts() -> tuple[ManagedRunReceipt | WorkloadWaitFact | StreamEndFact | BoundaryEmptyFact, ...]:
    receipt = _receipt()
    digest = managed_launch_receipt_sha256(receipt)
    return (
        receipt,
        WorkloadWaitFact(receipt.identity, receipt.unit_name, digest, exit_code=0),
        WorkloadWaitFact(receipt.identity, receipt.unit_name, digest, signal=15),
        StreamEndFact(
            receipt.identity,
            receipt.unit_name,
            digest,
            StreamName.STDOUT,
            3,
            hashlib.sha256(b"abc").hexdigest(),
            StreamDisposition.COMPLETE_CAPTURE,
        ),
        StreamEndFact(
            receipt.identity,
            receipt.unit_name,
            digest,
            StreamName.STDERR,
            0,
            hashlib.sha256(b"").hexdigest(),
            StreamDisposition.TRUNCATED_CAPTURE,
        ),
        BoundaryEmptyFact(receipt.identity, receipt.unit_name, digest),
        StreamEndFact(
            receipt.identity,
            receipt.unit_name,
            digest,
            StreamName.STDOUT,
            0,
            hashlib.sha256(b"").hexdigest(),
            StreamDisposition.DISCARDED,
        ),
        StreamEndFact(
            receipt.identity,
            receipt.unit_name,
            digest,
            StreamName.STDERR,
            0,
            hashlib.sha256(b"").hexdigest(),
            StreamDisposition.SUPPRESSED,
        ),
    )


@pytest.mark.parametrize("fact", _facts())
def test_each_fact_roundtrips_canonically_within_bound(fact: object) -> None:
    encoded = encode_managed_job_fact(fact)  # type: ignore[arg-type]
    assert len(encoded) <= MAX_MANAGED_JOB_FACT_BYTES
    assert decode_managed_job_fact(encoded) == fact
    assert encode_managed_job_fact(decode_managed_job_fact(encoded)) == encoded
    assert b"/bin/bash" in encoded if type(fact) is ManagedRunReceipt else b"/bin/bash" not in encoded


def test_post_launch_facts_bind_exact_receipt_and_independent_observations() -> None:
    receipt, wait, _signal, stdout, stderr, empty, discarded, suppressed = _facts()
    assert isinstance(receipt, ManagedRunReceipt)
    for fact in (wait, stdout, stderr, empty, discarded, suppressed):
        assert fact_matches_receipt(fact, receipt)
        assert not fact_matches_receipt(
            fact, replace(receipt, spec=replace(receipt.spec, shell=ManagedShellIdentity(None, None)))
        )
    assert isinstance(wait, WorkloadWaitFact)
    assert isinstance(stdout, StreamEndFact)
    assert isinstance(stderr, StreamEndFact)
    assert isinstance(empty, BoundaryEmptyFact)
    assert stdout.disposition is StreamDisposition.COMPLETE_CAPTURE
    assert stderr.disposition is StreamDisposition.TRUNCATED_CAPTURE
    assert isinstance(discarded, StreamEndFact)
    assert isinstance(suppressed, StreamEndFact)
    assert discarded.disposition is StreamDisposition.DISCARDED
    assert suppressed.disposition is StreamDisposition.SUPPRESSED
    assert wait.exit_code == 0


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("version", 2),
        ("version", True),
        ("kind", "unknown"),
        ("run_id", "F" * 32),
        ("unit", "agw-managed-" + "2" * 32 + ".service"),
        ("receipt_sha256", "A" * 64),
        ("exit_code", True),
        ("exit_code", -1),
        ("signal", 15),
    ],
)
def test_wait_rejects_malformed_or_contradictory_fields(field: str, replacement: object) -> None:
    value = json.loads(encode_managed_job_fact(_facts()[1]))
    value[field] = replacement
    with pytest.raises(ManagedJobFactError):
        decode_managed_job_fact(_wire(value))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("stream", "stdin"),
        ("disposition", "open"),
        ("retained_bytes", True),
        ("retained_bytes", -1),
        ("retained_sha256", "x" * 64),
    ],
)
def test_stream_rejects_invalid_closed_disposition(field: str, replacement: object) -> None:
    value = json.loads(encode_managed_job_fact(_facts()[3]))
    value[field] = replacement
    with pytest.raises(ManagedJobFactError):
        decode_managed_job_fact(_wire(value))


def test_zero_length_stream_requires_empty_digest() -> None:
    value = json.loads(encode_managed_job_fact(_facts()[4]))
    value["retained_sha256"] = "a" * 64
    with pytest.raises(ManagedJobFactError):
        decode_managed_job_fact(_wire(value))


@pytest.mark.parametrize("fact_index", [6, 7])
@pytest.mark.parametrize("field", ["retained_bytes", "retained_sha256"])
def test_discard_and_suppression_cannot_claim_retained_bytes(fact_index: int, field: str) -> None:
    value = json.loads(encode_managed_job_fact(_facts()[fact_index]))
    value[field] = 1 if field == "retained_bytes" else hashlib.sha256(b"x").hexdigest()
    with pytest.raises(ManagedJobFactError):
        decode_managed_job_fact(_wire(value))


def test_old_ambiguous_retention_field_is_not_accepted() -> None:
    value = json.loads(encode_managed_job_fact(_facts()[3]))
    value["retention"] = "complete"
    with pytest.raises(ManagedJobFactError):
        decode_managed_job_fact(_wire(value))


@pytest.mark.parametrize(
    "change", ["missing", "extra", "duplicate", "duplicate_same", "noisy", "space", "noncanonical_int", "oversize"]
)
def test_wire_rejects_noncanonical_or_boundedness_violation(change: str) -> None:
    encoded = encode_managed_job_fact(_facts()[5])
    value = json.loads(encoded)
    if change == "missing":
        del value["unit"]
        candidate = _wire(value)
    elif change == "extra":
        value["source"] = "secret"
        candidate = _wire(value)
    elif change == "duplicate":
        candidate = encoded[:-1] + b',"unit":"duplicate"}'
    elif change == "duplicate_same":
        candidate = encoded[:-1] + b',"version":1}'
    elif change == "noisy":
        candidate = encoded + b"\nnoise"
    elif change == "space":
        candidate = b" " + encoded
    elif change == "noncanonical_int":
        candidate = encoded.replace(b'"version":1', b'"version":1.0')
    else:
        candidate = encoded + b" " * MAX_MANAGED_JOB_FACT_BYTES
    with pytest.raises(ManagedJobFactError):
        decode_managed_job_fact(candidate)


@pytest.mark.parametrize("nested", ["target", "workload", "shell", "owner"])
def test_receipt_rejects_unexpected_nested_field(nested: str) -> None:
    value = json.loads(encode_managed_job_fact(_receipt()))
    value[nested]["application"] = "not allowed"
    with pytest.raises(ManagedJobFactError):
        decode_managed_job_fact(_wire(value))


def test_receipt_size_bound_limits_group_list() -> None:
    receipt = _receipt()
    oversized = replace(
        receipt, spec=replace(receipt.spec, workload=IdentityExpectation(1001, 1001, tuple(range(1001, 4001))))
    )
    with pytest.raises(ManagedJobFactError):
        encode_managed_job_fact(oversized)


def test_cross_run_binding_fails() -> None:
    receipt = _receipt()
    other = ManagedRunIdentity("2" * 32)
    fact = BoundaryEmptyFact(other, other.unit_name, managed_launch_receipt_sha256(receipt))
    assert not fact_matches_receipt(fact, receipt)


def test_typed_fact_constructors_refuse_invalid_evidence() -> None:
    receipt = _receipt()
    digest = managed_launch_receipt_sha256(receipt)
    with pytest.raises(ManagedJobFactError):
        WorkloadWaitFact(receipt.identity, receipt.unit_name, digest, exit_code=0, signal=9)
    with pytest.raises(ManagedJobFactError):
        StreamEndFact(
            receipt.identity,
            receipt.unit_name,
            digest,
            StreamName.STDOUT,
            1,
            hashlib.sha256(b"x").hexdigest(),
            StreamDisposition.DISCARDED,
        )
    with pytest.raises(ManagedJobFactError):
        BoundaryEmptyFact(receipt.identity, "agw-managed-" + "2" * 32 + ".service", digest)


@pytest.mark.parametrize(
    ("fact_index", "field_path"),
    [
        (0, ("kind",)),
        (0, ("target", "kind")),
        (0, ("shell", "requested")),
        (0, ("owner", "kind")),
        (0, ("lifetime",)),
        (0, ("receipt_namespace",)),
        (3, ("stream",)),
        (3, ("disposition",)),
    ],
)
def test_wire_rejects_equal_str_subclasses(fact_index: int, field_path: tuple[str, ...]) -> None:
    class StringSubclass(str):
        pass

    value = json.loads(encode_managed_job_fact(_facts()[fact_index]))
    parent = value
    for field in field_path[:-1]:
        parent = parent[field]
    field = field_path[-1]
    parent[field] = StringSubclass(parent[field])
    with pytest.raises(wire.ManagedJobWireError):
        wire.encode_fact(value)


def test_wire_rejects_str_subclass_field_name() -> None:
    class StringSubclass(str):
        pass

    value = wire.decode_fact(encode_managed_job_fact(_facts()[5]))
    value[StringSubclass("kind")] = value.pop("kind")
    with pytest.raises(wire.ManagedJobWireError):
        wire.encode_fact(value)


@pytest.mark.parametrize("interpreter", [Path(sys.executable), Path("/usr/bin/python3.11")])
def test_exact_portable_source_roundtrips_every_fact(interpreter: Path) -> None:
    if not interpreter.is_file():
        pytest.skip("Python 3.11 compatibility interpreter is unavailable")
    new_unicode_receipt = replace(
        _receipt(), spec=replace(_receipt().spec, shell=ManagedShellIdentity(Shell.SH, "/bin/\U0001fae8"))
    )
    encoded = [encode_managed_job_fact(fact) for fact in (*_facts(), new_unicode_receipt)]
    script = (
        build_helper_modules("_agw_managed_job", ("_helper_identity", "_managed_job_wire"))
        + """
import json
import sys

module = sys.modules["_agw_managed_job._managed_job_wire"]
assert "agentworks" not in sys.modules
facts = [bytes.fromhex(item) for item in json.load(sys.stdin)]
results = [module.encode_fact(module.decode_fact(item)).hex() for item in facts]
digest = module.launch_sha256(module.decode_fact(facts[0]))
print(json.dumps({"facts": results, "digest": digest,
    "new_unicode_path": module.canonical_shell_path("/bin/\U0001fae8"),
    "control_path": module.canonical_shell_path("/bin/\\x80"),
    "agentworks_loaded": "agentworks" in sys.modules}))
"""
    )
    result = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", script],
        input=json.dumps([item.hex() for item in encoded]).encode(),
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "facts": [item.hex() for item in encoded],
        "digest": managed_launch_receipt_sha256(_receipt()),
        "new_unicode_path": True,
        "control_path": False,
        "agentworks_loaded": False,
    }


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("name", "bad/name"),
        ("incarnation", "V1:" + "a" * 64),
        ("boot_id", "00000000-0000-4000-8000-000000000001 "),
    ],
)
def test_portable_codec_rejects_invalid_nested_target(field: str, replacement: str) -> None:
    value = wire.decode_fact(encode_managed_job_fact(_receipt()))
    target = value["target"]
    assert isinstance(target, dict)
    target[field] = replacement
    with pytest.raises(wire.ManagedJobWireError):
        wire.encode_fact(value)


def _wire(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")

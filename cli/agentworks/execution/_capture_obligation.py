"""Token-only cleanup identity for both streams of one DIRECT capture."""

from __future__ import annotations

import json
from dataclasses import dataclass

from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import ValidationError

from ._helper_identity import decode_identity
from ._helper_launcher import IdentityMode, IdentityPlan, _validate_plan
from ._prepared_helper_obligation import (
    PreparedHelperObligation,
    decode_prepared_helper_obligation,
    encode_prepared_helper_obligation,
)

CAPTURE_OBLIGATION_KIND = "direct-capture"


@dataclass(frozen=True, slots=True, repr=False)
class CaptureObligation:
    preparation: PreparedHelperObligation
    workload: IdentityPlan
    stderr_token: bytes

    @property
    def tokens(self) -> tuple[bytes, bytes]:
        return self.preparation.token, self.stderr_token


def encode_capture_obligation(binding: CaptureObligation) -> bytes:
    """Admit the exact immutable identity at the durable cleanup boundary."""
    try:
        identity = _validate_plan(binding.workload)
        if (
            type(binding.stderr_token) is not bytes
            or len(binding.stderr_token) != 16
            or binding.tokens[0] == binding.tokens[1]
        ):
            raise ValueError
        value = json.loads(encode_prepared_helper_obligation(binding.preparation))
        value["tokens"] = {"stdout": value.pop("token"), "stderr": binding.stderr_token.hex()}
        value["workload"] = {
            "mode": binding.workload.mode.value,
            "identity": {
                "euid": identity.euid,
                "egid": identity.egid,
                "groups": list(identity.groups),
            },
        }
        data = json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode("ascii")
        if len(data) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            raise ValueError
        return data
    except (ValueError, TypeError, AttributeError, ValidationError):
        raise ValidationError("Invalid DIRECT capture cleanup identity") from None


def decode_capture_obligation(data: bytes) -> CaptureObligation:
    """Validate persisted cleanup bytes without granting activation or read authority."""
    try:
        value = json.loads(data)
        if type(value) is not dict or set(value) != {"version", "target", "bootstrap", "runtime", "workload", "tokens"}:
            raise ValueError
        workload, tokens = value.pop("workload"), value.pop("tokens")
        if (
            type(workload) is not dict
            or set(workload) != {"mode", "identity"}
            or type(tokens) is not dict
            or set(tokens) != {"stdout", "stderr"}
        ):
            raise ValueError
        value["token"] = tokens["stdout"]
        preparation = decode_prepared_helper_obligation(
            json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")
        )
        result = CaptureObligation(
            preparation,
            IdentityPlan(decode_identity(workload["identity"]), IdentityMode(workload["mode"])),
            bytes.fromhex(tokens["stderr"]),
        )
        if encode_capture_obligation(result) != data:
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
        raise ValidationError("Invalid DIRECT capture cleanup identity") from None

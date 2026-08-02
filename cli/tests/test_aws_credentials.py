"""Which AWS credential a site gets, the caching around it, and the
no-fallback guarantee (the azure-vm reference shape, copied to EC2).

- SELECTION: a site declaring a ``credentials`` table gets exactly that
  credential, built from the secret access key the RunContext delivers,
  optionally layered with STS AssumeRole, and NEVER falls back to boto3's
  ambient default chain when it fails. A site declaring none gets the ambient
  chain, unchanged.
- CACHING: one session build per platform instance on either path, and one
  client per (service, region), reused across ops.

boto3 is faked in-process (``tests/_aws_fakes.py``): no live AWS, no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import ConfigError
from agentworks.plugins.aws.network import EC2Error
from agentworks.plugins.aws.platform import DEFAULT_SECRET_ACCESS_KEY, EC2Platform
from tests._aws_fakes import client_error, install_fakes

if TYPE_CHECKING:
    from agentworks.db import VMRow

_CONFIG = {"region": "us-east-1"}
_CREDS = {"access_key_id": "AKIAEXAMPLE", "access_key_secret": "aws-secret"}


def _fake_vm() -> Any:
    return SimpleNamespace(
        name="vm1",
        admin_username="agentworks",
        platform_metadata={"instance_id": "i-123", "region": "us-east-1", "backend_name": "vm1"},
    )


class _Secrets:
    """A SecretReader over a fixed mapping, as the boundary delivers."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        return self._values[name]


def _platform() -> EC2Platform:
    return EC2Platform("aws-site", dict(_CONFIG))


def _creds_platform(*, name_field: bool = True) -> EC2Platform:
    creds = dict(_CREDS) if name_field else {k: v for k, v in _CREDS.items() if k != "access_key_secret"}
    return EC2Platform("aws-site", {**_CONFIG, "credentials": creds})


def _ctx(name: str = "aws-secret", value: str = "secret-value") -> RunContext:
    return RunContext(secrets=_Secrets({name: value}))  # type: ignore[arg-type]


class TestCredentialSelection:
    def test_no_credentials_table_uses_the_ambient_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A site with no ``credentials`` table builds an ambient session (no
        keys handed in), and never receives an explicit access key. The
        zero-key assertion is the regression tripwire for existing ambient
        operators."""
        rec = install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        assert _platform().status(vm, RunContext()) is VMStatus.RUNNING

        (session,) = rec.sessions
        assert "aws_access_key_id" not in session
        assert session == {"region_name": "us-east-1"}

    def test_credentials_build_from_the_delivered_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A declared credentials table is built from the site's access key id
        plus the secret access key ``ctx.secret`` DELIVERS (the config carries
        the name, the context the value), and the ambient chain is never
        consulted."""
        rec = install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        assert _creds_platform().status(vm, _ctx()) is VMStatus.RUNNING

        (session,) = rec.sessions
        assert session["aws_access_key_id"] == "AKIAEXAMPLE"
        assert session["aws_secret_access_key"] == "secret-value"
        assert "aws_session_token" not in session

    def test_credentials_secret_name_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting ``credentials.access_key_secret`` reads the default name."""
        rec = install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        platform = _creds_platform(name_field=False)
        platform.status(vm, _ctx(name=DEFAULT_SECRET_ACCESS_KEY, value="default-named"))

        assert rec.sessions[0]["aws_secret_access_key"] == "default-named"

    def test_assume_role_wires_refreshable_credentials(self) -> None:
        """The assume-role path builds AUTO-REFRESHING credentials, not a
        one-shot assume frozen on temporary keys that would fail with
        ExpiredToken once a long op outlives the role's session duration. The
        pin asserts the wiring (the session's credentials are deferred,
        refreshable, assume-role-method) against real botocore, with no network
        and no simulated clock: constructing the fetcher and the deferred
        credentials makes no live call."""
        from botocore.credentials import DeferredRefreshableCredentials

        from agentworks.plugins.aws.platform import _build_explicit_session, _Credentials

        creds = _Credentials(
            access_key_id="AKIAEXAMPLE",
            secret_name="aws-secret",
            assume_role_arn="arn:aws:iam::111122223333:role/agw",
        )
        session = _build_explicit_session(creds, "secret-value", "aws-site", "us-east-1")
        resolved = session.get_credentials()
        assert isinstance(resolved, DeferredRefreshableCredentials)
        assert resolved.method == "assume-role"

    def test_empty_resolved_secret_is_typed_not_a_silent_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A backend that resolves the secret to an empty string is a reachable
        state ``validate`` cannot catch (the name is well-formed; only the
        value is empty). It surfaces as the platform's typed error naming the
        site and the secret, no session is built, and nothing is cached, so a
        retry re-resolves rather than reusing an unusable credential."""
        rec = install_fakes(monkeypatch)
        platform = _creds_platform()

        with pytest.raises(EC2Error) as exc:
            platform._get_session(_ctx(value=""))

        assert "aws-site" in str(exc.value)
        assert exc.value.hint is not None and "aws-secret" in exc.value.hint
        assert rec.sessions == []  # no fallback to an ambient session
        assert platform._session_cached is None

    def test_rejection_at_the_op_never_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the server rejects the configured credential mid-op, the op
        raises the wrapped error and the ONLY session ever built is the
        explicit one (never a silent ambient retry)."""
        rec = install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        def _reject(**_kw: object) -> dict[str, object]:
            raise client_error("UnauthorizedOperation", "denied", "StartInstances")

        monkeypatch.setattr("tests._aws_fakes._FakeEC2.start_instances", lambda self, **kw: _reject(**kw))

        with pytest.raises(EC2Error):
            _creds_platform().start(vm, _ctx())

        (session,) = rec.sessions
        assert session["aws_access_key_id"] == "AKIAEXAMPLE"

    def test_context_without_resolved_secrets_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A context assembled before the resolve boundary (or for inspection)
        is the accessor's typed ConfigError, the same refusal every capability
        gets, rather than a crash or a silent ambient fallback."""
        rec = install_fakes(monkeypatch)

        with pytest.raises(ConfigError, match="resolved secrets"):
            _creds_platform()._get_session(RunContext())

        assert rec.sessions == []


class TestSessionCaching:
    def test_one_session_and_client_per_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple ops on one instance build the session once and the ec2
        client once (keyed by region); a second instance builds its own."""
        rec = install_fakes(monkeypatch)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        platform = _creds_platform()
        assert platform.status(vm, _ctx()) is VMStatus.RUNNING
        platform.start(vm, _ctx())
        platform.stop(vm, _ctx())

        assert len(rec.sessions) == 1
        assert set(platform._clients) == {("ec2", "us-east-1")}

        second = _creds_platform()
        second.status(vm, _ctx())
        assert len(rec.sessions) == 2

    def test_second_region_builds_its_own_client_not_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A VM whose stored region differs from another's builds a client per
        region, while the region-independent session still builds once."""
        install_fakes(monkeypatch)
        vm_a: VMRow = _fake_vm()  # type: ignore[assignment]
        vm_b: VMRow = _fake_vm()  # type: ignore[assignment]
        vm_b.platform_metadata = {**vm_b.platform_metadata, "region": "eu-west-1"}
        platform = _creds_platform()

        platform.status(vm_a, _ctx())
        platform.status(vm_b, _ctx())

        assert set(platform._clients) == {("ec2", "us-east-1"), ("ec2", "eu-west-1")}

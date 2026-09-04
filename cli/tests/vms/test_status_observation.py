"""VM list status observation and projection contracts."""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.db import VMStatus
from agentworks.errors import ConnectivityError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms.manager import inspect
from agentworks.vms.manager._status import observe_vm_statuses, project_vm_status


@pytest.mark.parametrize(
    ("status", "operator_stopped", "expected"),
    [
        (VMStatus.RUNNING, False, ("running", None)),
        (VMStatus.STOPPED, True, ("stopped", "manual")),
        (VMStatus.STOPPED, False, ("stopped", "idle")),
        (VMStatus.DEALLOCATED, True, ("deallocated", "manual")),
        (VMStatus.UNKNOWN, False, ("unknown", None)),
    ],
)
def test_vm_status_projection(
    status: VMStatus,
    operator_stopped: bool,
    expected: tuple[str, str | None],
) -> None:
    assert project_vm_status(status, operator_stopped=operator_stopped) == expected


def test_plain_vm_listing_never_calls_status_observer(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    monkeypatch.setattr(
        inspect,
        "observe_vm_statuses",
        lambda *_args, **_kwargs: pytest.fail("plain inventory reached provider status"),
    )

    row = inspect.vm_listing(db).vms[0]

    assert row.observed_status is None
    assert row.status_disposition is None


def test_vm_listing_joins_requested_status_without_changing_order(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("alpha", site="site-a", hostname="alpha")
    db.insert_vm("beta", site="site-b", hostname="beta")
    db.set_operator_stopped("beta", True)
    monkeypatch.setattr(
        inspect,
        "observe_vm_statuses",
        lambda *_args, **_kwargs: {
            "alpha": VMStatus.RUNNING,
            "beta": VMStatus.DEALLOCATED,
        },
    )

    rows = inspect.vm_listing(
        db,
        object(),  # type: ignore[arg-type]
        include_status=True,
        interaction=TtyInteractionPolicy.REFUSE,
    ).vms

    assert [(row.name, row.observed_status, row.status_disposition) for row in rows] == [
        ("alpha", "running", None),
        ("beta", "deallocated", "manual"),
    ]


def test_vm_listing_status_column_follows_explicit_render_request(
    db,  # noqa: ANN001
    captured_output,  # noqa: ANN001
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    plain = inspect.vm_listing(db)

    inspect.render_vm_listing(plain)
    assert "STATUS" not in captured_output.info[0]

    captured_output.info.clear()
    observed = inspect.VMListing(vms=(replace(plain.vms[0], observed_status="running"),))
    inspect.render_vm_listing(observed, include_status=True)
    assert "STATUS" in captured_output.info[0]


def test_vm_observer_isolates_dispatched_failure_and_serializes_shared_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        SimpleNamespace(name="alpha", site="one", operator_stopped=False),
        SimpleNamespace(name="beta", site="one", operator_stopped=False),
        SimpleNamespace(name="gamma", site="two", operator_stopped=False),
    ]
    calls: list[str] = []

    class _Platform:
        def status(self, row: object, _ctx: object) -> VMStatus:
            name = row.name  # type: ignore[attr-defined]
            calls.append(name)
            if name == "beta":
                raise ConnectivityError("provider unavailable")
            return VMStatus.RUNNING if name == "alpha" else VMStatus.STOPPED

    sites: dict[str, object] = {}

    def live_node(_db: object, _config: object, _registry: object, row: object, *, site_nodes: dict[str, object]):
        site_name = row.site  # type: ignore[attr-defined]
        site = sites.setdefault(site_name, SimpleNamespace(platform=_Platform()))
        site_nodes.setdefault(site_name, site)
        return SimpleNamespace(row=row, site=site)

    class _Resolver:
        values: dict[str, str] = {}

        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def register_name(self, _name: str) -> None: ...

        def resolve(self) -> None: ...

    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("agentworks.vms.nodes.live_vm_node", live_node)
    monkeypatch.setattr("agentworks.orchestration.walk.walk", lambda *nodes: nodes)
    monkeypatch.setattr("agentworks.orchestration.secrets.secret_union", lambda _nodes: ())
    monkeypatch.setattr("agentworks.orchestration.readiness.preflight_all", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agentworks.secrets.resolver.Resolver", _Resolver)
    monkeypatch.setattr("agentworks.vms.manager._status._platform_ops_ctx", lambda *_args: object())

    class _DB:
        def get_setting(self, _key: str) -> None:
            return None

    result = observe_vm_statuses(
        _DB(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        rows,  # type: ignore[arg-type]
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert result == {
        "alpha": VMStatus.RUNNING,
        "beta": VMStatus.UNKNOWN,
        "gamma": VMStatus.STOPPED,
    }
    assert calls.index("alpha") < calls.index("beta")


def test_vm_observer_shared_registry_failure_leaves_complete_unknown_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        SimpleNamespace(name="alpha", site="one", operator_stopped=False),
        SimpleNamespace(name="beta", site="two", operator_stopped=False),
    ]
    monkeypatch.setattr(
        "agentworks.bootstrap.load_request_registry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectivityError("registry unavailable")),
    )

    result = observe_vm_statuses(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        rows,  # type: ignore[arg-type]
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert result == {
        "alpha": VMStatus.UNKNOWN,
        "beta": VMStatus.UNKNOWN,
    }


def test_vm_observer_precomputes_database_context_before_worker_threads(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real SQLite connection never crosses the status executor boundary."""
    db.insert_vm("alpha", site="one", hostname="alpha")
    db.insert_vm("beta", site="two", hostname="beta")
    rows = [db.get_vm("alpha"), db.get_vm("beta")]
    assert all(row is not None for row in rows)
    owner_thread = threading.get_ident()
    provider_threads: list[int] = []

    class _Platform:
        def status(self, _row: object, _ctx: object) -> VMStatus:
            provider_threads.append(threading.get_ident())
            return VMStatus.RUNNING

    def live_node(
        _db: object,
        _config: object,
        _registry: object,
        row: object,
        *,
        site_nodes: dict[str, object],
    ) -> object:
        site = SimpleNamespace(platform=_Platform())
        site_nodes.setdefault(row.site, site)  # type: ignore[attr-defined]
        return SimpleNamespace(row=row, site=site)

    class _Resolver:
        values: dict[str, str] = {}

        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def register_name(self, _name: str) -> None: ...

        def resolve(self) -> None: ...

    def platform_context(*_args: object) -> object:
        assert threading.get_ident() == owner_thread
        return object()

    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("agentworks.vms.nodes.live_vm_node", live_node)
    monkeypatch.setattr("agentworks.orchestration.walk.walk", lambda *nodes: nodes)
    monkeypatch.setattr("agentworks.orchestration.secrets.secret_union", lambda _nodes: ())
    monkeypatch.setattr("agentworks.orchestration.readiness.preflight_all", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agentworks.secrets.resolver.Resolver", _Resolver)
    monkeypatch.setattr("agentworks.vms.manager._status._platform_ops_ctx", platform_context)
    monkeypatch.setattr(
        "agentworks.orchestration.activation.activation_gate",
        lambda *_args, **_kwargs: pytest.fail("status observation activated a VM"),
    )
    monkeypatch.setattr(
        "agentworks.transports.transport",
        lambda *_args, **_kwargs: pytest.fail("VM status observation opened a guest transport"),
    )
    changes_before = db._conn.total_changes  # noqa: SLF001

    result = observe_vm_statuses(
        db,
        object(),  # type: ignore[arg-type]
        rows,  # type: ignore[arg-type]
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert result == {"alpha": VMStatus.RUNNING, "beta": VMStatus.RUNNING}
    assert provider_threads and all(thread != owner_thread for thread in provider_threads)
    assert db._conn.total_changes == changes_before  # noqa: SLF001

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import provision_memory


class _FakeBackend:
    def __init__(
        self,
        settings: Any,
        *,
        allow_schema_changes: bool,
        operation_timeout_seconds: float,
        failure: Exception | None = None,
    ) -> None:
        self.settings = settings
        self.allow_schema_changes = allow_schema_changes
        self.operation_timeout_seconds = operation_timeout_seconds
        self.failure = failure
        self.ready_calls = 0
        self.close_calls = 0

    async def ensure_ready(self) -> None:
        self.ready_calls += 1
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.close_calls += 1


def _install_fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: Exception | None = None,
) -> list[_FakeBackend]:
    instances: list[_FakeBackend] = []

    def factory(
        settings: Any,
        *,
        allow_schema_changes: bool,
        operation_timeout_seconds: float,
    ) -> _FakeBackend:
        backend = _FakeBackend(
            settings,
            allow_schema_changes=allow_schema_changes,
            operation_timeout_seconds=operation_timeout_seconds,
            failure=failure,
        )
        instances.append(backend)
        return backend

    monkeypatch.setattr(provision_memory, "TablestoreMemoryBackend", factory)
    return instances


def _captured_json(
    capsys: pytest.CaptureFixture[str],
) -> tuple[dict[str, object], str, str]:
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.out, captured.err


def test_run_provisions_with_fake_backend_and_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = SimpleNamespace(memory_backend="tablestore")
    instances = _install_fake_backend(monkeypatch)

    exit_code = provision_memory.run(settings)

    payload, _stdout, stderr = _captured_json(capsys)
    assert exit_code == 0
    assert payload == {
        "ok": True,
        "backend": "tablestore",
        "table": provision_memory.MEMORY_TABLE,
        "index": provision_memory.MEMORY_INDEX,
        "embedding_dimension": provision_memory.EMBEDDING_DIMENSION,
    }
    assert stderr == ""
    assert len(instances) == 1
    assert instances[0].settings is settings
    assert instances[0].allow_schema_changes is True
    assert instances[0].operation_timeout_seconds == 15.0
    assert instances[0].ready_calls == 1
    assert instances[0].close_calls == 1


def test_run_redacts_configuration_failure_inside_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "configuration-secret-sentinel"
    backend_constructed = False

    def fail_from_env() -> None:
        raise ValueError(secret)

    def forbidden_backend(*_args: Any, **_kwargs: Any) -> None:
        nonlocal backend_constructed
        backend_constructed = True
        raise AssertionError("backend must not be constructed")

    monkeypatch.setattr(provision_memory.Settings, "from_env", fail_from_env)
    monkeypatch.setattr(
        provision_memory,
        "TablestoreMemoryBackend",
        forbidden_backend,
    )

    exit_code = provision_memory.run()

    payload, stdout, stderr = _captured_json(capsys)
    assert exit_code == 1
    assert payload == {"ok": False, "reason": "memory_schema_unavailable"}
    assert secret not in stdout
    assert secret not in stderr
    assert stderr == ""
    assert backend_constructed is False


def test_run_redacts_unexpected_configuration_failure_inside_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "unexpected-configuration-secret-sentinel"

    def fail_from_env() -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(provision_memory.Settings, "from_env", fail_from_env)

    exit_code = provision_memory.run()

    payload, stdout, stderr = _captured_json(capsys)
    assert exit_code == 1
    assert payload == {"ok": False, "reason": "memory_schema_unavailable"}
    assert secret not in stdout
    assert secret not in stderr
    assert stderr == ""


def test_run_redacts_backend_failure_and_always_closes_fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "backend-secret-sentinel"
    failure = provision_memory.MemoryError(secret)
    instances = _install_fake_backend(monkeypatch, failure=failure)

    exit_code = provision_memory.run(SimpleNamespace(memory_backend="tablestore"))

    payload, stdout, stderr = _captured_json(capsys)
    assert exit_code == 1
    assert payload == {"ok": False, "reason": "memory_schema_unavailable"}
    assert secret not in stdout
    assert secret not in stderr
    assert stderr == ""
    assert len(instances) == 1
    assert instances[0].ready_calls == 1
    assert instances[0].close_calls == 1


def test_run_redacts_unexpected_backend_failure_and_still_closes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "unexpected-backend-secret-sentinel"
    instances = _install_fake_backend(monkeypatch, failure=RuntimeError(secret))

    exit_code = provision_memory.run(SimpleNamespace(memory_backend="tablestore"))

    payload, stdout, stderr = _captured_json(capsys)
    assert exit_code == 1
    assert payload == {"ok": False, "reason": "memory_schema_unavailable"}
    assert secret not in stdout
    assert secret not in stderr
    assert stderr == ""
    assert len(instances) == 1
    assert instances[0].ready_calls == 1
    assert instances[0].close_calls == 1


def test_run_rejects_non_tablestore_configuration_without_backend_or_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend_constructed = False

    def forbidden_backend(*_args: Any, **_kwargs: Any) -> None:
        nonlocal backend_constructed
        backend_constructed = True
        raise AssertionError("backend must not be constructed")

    monkeypatch.setattr(
        provision_memory,
        "TablestoreMemoryBackend",
        forbidden_backend,
    )

    exit_code = provision_memory.run(SimpleNamespace(memory_backend="inmem"))

    payload, stdout, stderr = _captured_json(capsys)
    assert exit_code == 1
    assert payload == {"ok": False, "reason": "memory_schema_unavailable"}
    assert "MEMORY_BACKEND" not in stdout
    assert stderr == ""
    assert backend_constructed is False

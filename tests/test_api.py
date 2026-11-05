from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import landrop.api.app as api
from landrop.api.app import ServiceConfig, TransferManager, create_app
from landrop.transfer import TransferCancelled, TransferError, TransferResult

TOKEN = "a" * 48
HEADERS = {"X-LANDrop-Token": TOKEN}


def make_client(manager: TransferManager | None = None, client=("127.0.0.1", 4567)):
    return TestClient(create_app(TOKEN, manager), client=client)


def wait_for_status(client: TestClient, task_id: str, expected: set[str], timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/transfers/{task_id}", headers=HEADERS).json()
        if snapshot["status"] in expected:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"任务未进入 {expected}，最后状态: {snapshot}")


def base_payload(local_path, **overrides) -> dict:
    payload = {
        "host": "192.168.1.23",
        "username": "lin",
        "remote_dir": "/incoming",
        "local_path": str(local_path),
        "auth_method": "password",
        "password": "not-returned",
    }
    payload.update(overrides)
    return payload


def test_health_requires_session_token() -> None:
    with make_client() as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers={"X-LANDrop-Token": "wrong"}).status_code == 401
        response = client.get("/health", headers=HEADERS)
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}


def test_non_loopback_requests_are_rejected() -> None:
    with make_client(client=("192.0.2.10", 4567)) as client:
        assert client.get("/health", headers=HEADERS).status_code == 403


def test_health_accepts_bearer_token() -> None:
    with make_client() as client:
        assert client.get("/health", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_untrusted_origin_is_rejected() -> None:
    with make_client() as client:
        response = client.get("/health", headers={**HEADERS, "Origin": "https://evil.example"})
        assert response.status_code == 403
        allowed = client.get("/health", headers={**HEADERS, "Origin": "tauri://localhost"})
        assert allowed.status_code == 200


def test_service_config_rejects_missing_environment(monkeypatch) -> None:
    monkeypatch.delenv("LANDROP_SERVICE_PORT", raising=False)
    monkeypatch.delenv("LANDROP_SERVICE_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="启动参数缺失"):
        ServiceConfig.from_environment()


def test_service_config_validates_token_and_port(monkeypatch) -> None:
    monkeypatch.setenv("LANDROP_SERVICE_PORT", "45678")
    monkeypatch.setenv("LANDROP_SERVICE_TOKEN", "c" * 48)
    assert ServiceConfig.from_environment() == ServiceConfig(45678, "c" * 48)

    monkeypatch.setenv("LANDROP_SERVICE_PORT", "70000")
    with pytest.raises(RuntimeError, match="端口超出范围"):
        ServiceConfig.from_environment()


def test_transfer_api_creates_reports_and_cancels_task(tmp_path, monkeypatch) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")

    def fake_upload_file(**kwargs):
        kwargs["progress"](4, 7)
        kwargs["progress"](7, 7)
        return TransferResult(source, "/remote/payload.bin", 7, "f" * 64, 0)

    monkeypatch.setattr(api, "upload_file", fake_upload_file)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            response = client.post("/api/transfers", json=base_payload(source), headers=HEADERS)
            assert response.status_code == 202
            task_id = response.json()["id"]
            assert "not-returned" not in response.text

            snapshot = wait_for_status(client, task_id, {"succeeded"})
            assert snapshot["result"]["sha256"] == "f" * 64
            assert snapshot["sent"] == 7
            assert snapshot["request"]["username"] == "lin"

            events = client.get(f"/api/transfers/{task_id}/events", headers=HEADERS)
            assert events.status_code == 200
            assert '"phase": "completed"' in events.text
            assert client.post(f"/api/transfers/{task_id}/cancel", headers=HEADERS).json()["status"] == "succeeded"
    finally:
        manager.close()


def test_task_snapshot_never_contains_password(tmp_path, monkeypatch) -> None:
    source = tmp_path / "secret.bin"
    source.write_bytes(b"x")

    def fake_upload_file(**kwargs):
        return TransferResult(source, "/remote/secret.bin", 1, "e" * 64, 0)

    monkeypatch.setattr(api, "upload_file", fake_upload_file)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            response = client.post("/api/transfers", json=base_payload(source), headers=HEADERS)
            task_id = response.json()["id"]
            wait_for_status(client, task_id, {"succeeded"})
            body = client.get(f"/api/transfers/{task_id}", headers=HEADERS).json()
            assert "not-returned" not in str(body)
            assert "password" not in body
            assert "password" not in body["request"]
    finally:
        manager.close()


def test_running_task_can_be_cancelled_and_keeps_partial(tmp_path, monkeypatch) -> None:
    source = tmp_path / "big.bin"
    source.write_bytes(b"0" * 1024)
    started = threading.Event()

    def fake_upload_file(**kwargs):
        cancel = kwargs["cancel"]
        started.set()
        for _ in range(500):
            if cancel.is_set():
                raise TransferCancelled("传输已取消，已保留远端临时文件")
            time.sleep(0.005)
        raise AssertionError("取消标志未被传递")

    monkeypatch.setattr(api, "upload_file", fake_upload_file)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            task_id = client.post("/api/transfers", json=base_payload(source), headers=HEADERS).json()["id"]
            assert started.wait(2), "传输任务未启动"
            response = client.post(f"/api/transfers/{task_id}/cancel", headers=HEADERS)
            assert response.status_code == 200
            snapshot = wait_for_status(client, task_id, {"cancelled"})
            assert "临时文件" in snapshot["message"]
    finally:
        manager.close()


def test_transfer_failure_is_mapped_to_error_state(tmp_path, monkeypatch) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"data")

    def failing_upload(**kwargs):
        raise TransferError("远端目标文件已存在")

    monkeypatch.setattr(api, "upload_file", failing_upload)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            task_id = client.post("/api/transfers", json=base_payload(source), headers=HEADERS).json()["id"]
            snapshot = wait_for_status(client, task_id, {"failed"})
            assert snapshot["error"] == "远端目标文件已存在"
            assert snapshot["phase"] == "failed"
    finally:
        manager.close()


def test_unexpected_error_does_not_leak_details(tmp_path, monkeypatch) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"data")

    def crashing_upload(**kwargs):
        raise RuntimeError("socket exploded at /home/lin/secret")

    monkeypatch.setattr(api, "upload_file", crashing_upload)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            task_id = client.post("/api/transfers", json=base_payload(source), headers=HEADERS).json()["id"]
            snapshot = wait_for_status(client, task_id, {"failed"})
            assert snapshot["error"] == "RuntimeError"
            assert "secret" not in str(snapshot)
    finally:
        manager.close()


def test_failed_task_can_be_retried_with_new_task(tmp_path, monkeypatch) -> None:
    source = tmp_path / "retry.bin"
    source.write_bytes(b"retry")
    attempts: list[str] = []

    def flaky_upload(**kwargs):
        attempts.append(kwargs["username"])
        if len(attempts) == 1:
            raise TransferError("连接中断")
        return TransferResult(source, "/remote/retry.bin", 5, "d" * 64, 0)

    monkeypatch.setattr(api, "upload_file", flaky_upload)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            first = client.post("/api/transfers", json=base_payload(source), headers=HEADERS).json()
            wait_for_status(client, first["id"], {"failed"})

            response = client.post(
                f"/api/transfers/{first['id']}/retry",
                headers=HEADERS,
                json={"password": "not-returned"},
            )
            assert response.status_code == 202
            second = response.json()
            assert second["id"] != first["id"]
            snapshot = wait_for_status(client, second["id"], {"succeeded"})
            assert snapshot["result"]["sha256"] == "d" * 64
    finally:
        manager.close()


def test_retry_requires_finished_task(tmp_path, monkeypatch) -> None:
    source = tmp_path / "running.bin"
    source.write_bytes(b"x")
    release = threading.Event()

    def blocking_upload(**kwargs):
        release.wait(3)
        return TransferResult(source, "/remote/running.bin", 1, "a" * 64, 0)

    monkeypatch.setattr(api, "upload_file", blocking_upload)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            task_id = client.post("/api/transfers", json=base_payload(source), headers=HEADERS).json()["id"]
            response = client.post(
                f"/api/transfers/{task_id}/retry", headers=HEADERS, json={"password": "not-returned"}
            )
            assert response.status_code == 400
            release.set()
            wait_for_status(client, task_id, {"succeeded"})
    finally:
        release.set()
        manager.close()


def test_event_stream_resumes_from_last_event_id(tmp_path, monkeypatch) -> None:
    source = tmp_path / "events.bin"
    source.write_bytes(b"payload")

    def fake_upload_file(**kwargs):
        kwargs["progress"](3, 7)
        kwargs["progress"](7, 7)
        return TransferResult(source, "/remote/events.bin", 7, "b" * 64, 0)

    monkeypatch.setattr(api, "upload_file", fake_upload_file)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            task_id = client.post("/api/transfers", json=base_payload(source), headers=HEADERS).json()["id"]
            wait_for_status(client, task_id, {"succeeded"})

            full = client.get(f"/api/transfers/{task_id}/events", headers=HEADERS)
            ids = [line for line in full.text.splitlines() if line.startswith("id: ")]
            assert len(ids) >= 2

            resumed = client.get(
                f"/api/transfers/{task_id}/events",
                headers={**HEADERS, "Last-Event-ID": ids[-2].removeprefix("id: ")},
            )
            assert resumed.status_code == 200
            resumed_ids = [line for line in resumed.text.splitlines() if line.startswith("id: ")]
            assert resumed_ids == ids[-1:]

            invalid = client.get(
                f"/api/transfers/{task_id}/events",
                headers={**HEADERS, "Last-Event-ID": "abc"},
            )
            assert invalid.status_code == 400
    finally:
        manager.close()


def test_transfer_api_validates_payload_and_task_ids(tmp_path) -> None:
    manager = TransferManager(max_workers=1)
    with make_client(manager) as client:
        assert client.get("/api/transfers/missing", headers=HEADERS).status_code == 404
        invalid = client.post("/api/transfers", json={"host": "host", "unexpected": True}, headers=HEADERS)
        assert invalid.status_code == 422
        assert client.get(
            "/api/transfers/missing/events", headers={**HEADERS, "Last-Event-ID": "-1"}
        ).status_code == 404
        assert client.post("/api/transfers/missing/retry", headers=HEADERS, json={}).status_code == 404
    manager.close()


def test_password_task_requires_password_or_stored_credential(tmp_path, monkeypatch) -> None:
    source = tmp_path / "need-password.bin"
    source.write_bytes(b"x")
    monkeypatch.setattr(api.credentials, "load_password", lambda username: None)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            payload = base_payload(source)
            payload.pop("password")
            response = client.post("/api/transfers", json=payload, headers=HEADERS)
            assert response.status_code == 400
            assert "密码" in response.json()["detail"]
    finally:
        manager.close()


def test_config_round_trip_through_api(tmp_path, monkeypatch) -> None:
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: config.validate())

    with make_client() as client:
        current = client.get("/api/config", headers=HEADERS)
        assert current.status_code == 200
        assert current.json()["port"] == 22

        payload = {
            "host": "192.168.1.9",
            "port": 2222,
            "username": "lin",
            "remote_dir": "/srv/drop",
            "auth_method": "key",
            "private_key_path": "",
            "hotkey_modifiers": ["ctrl", "shift"],
            "hotkey_key": "f9",
        }
        updated = client.put("/api/config", json=payload, headers=HEADERS)
        assert updated.status_code == 200
        assert updated.json()["hotkey_key"] == "f9"

        assert client.put("/api/config", json={**payload, "port": 0}, headers=HEADERS).status_code == 422
        assert config_file.exists() is False


def test_config_api_normalizes_hotkey(monkeypatch) -> None:
    saved: list[api.AppConfig] = []
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", saved.append)

    payload = {
        "host": "192.168.1.9",
        "username": "lin",
        "remote_dir": "/srv/drop",
        "auth_method": "key",
        "private_key_path": "",
        "hotkey_modifiers": ["ALT", "ctrl", "alt"],
        "hotkey_key": "F9",
    }

    with make_client() as client:
        response = client.put("/api/config", json=payload, headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["hotkey_modifiers"] == ["ctrl", "alt"]
    assert response.json()["hotkey_key"] == "f9"
    assert saved[0].hotkey_modifiers == ("ctrl", "alt")


def test_config_api_rejects_unknown_hotkey_modifier(monkeypatch) -> None:
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: None)

    payload = {
        "host": "192.168.1.9",
        "username": "lin",
        "remote_dir": "/srv/drop",
        "auth_method": "key",
        "private_key_path": "",
        "hotkey_modifiers": ["ctrl", "hyper"],
        "hotkey_key": "space",
    }

    with make_client() as client:
        response = client.put("/api/config", json=payload, headers=HEADERS)

    assert response.status_code == 400
    assert "不支持的热键修饰键" in response.json()["detail"]


def test_config_api_rejects_hotkey_without_modifier(monkeypatch) -> None:
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: None)

    payload = {
        "host": "192.168.1.9",
        "username": "lin",
        "remote_dir": "/srv/drop",
        "auth_method": "key",
        "private_key_path": "",
        "hotkey_modifiers": [],
        "hotkey_key": "space",
    }

    with make_client() as client:
        response = client.put("/api/config", json=payload, headers=HEADERS)

    assert response.status_code == 400
    assert "修饰键" in response.json()["detail"]

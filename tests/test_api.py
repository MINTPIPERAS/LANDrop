from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import landrop.api.app as api
from landrop.api.app import ServiceConfig, TransferManager, create_app
from landrop.config import DeviceConfig, ScheduleConfig
from landrop.transfer import (
    TransferCancelled,
    TransferError,
    TransferResult,
    TreeFileResult,
    TreeResult,
)

TOKEN = "a" * 48
HEADERS = {"X-LANDrop-Token": TOKEN}


@pytest.fixture(autouse=True)
def isolate_log_directory(monkeypatch, tmp_path):
    """Keep config-save side effects (log files) inside the test's tmp dir."""
    monkeypatch.setattr(api.logs, "default_log_dir", lambda: tmp_path / "logs")
    yield
    api.logs.shutdown_logging()


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
    monkeypatch.setattr(api.credentials, "load_password", lambda host, port, username: None)
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


def test_config_api_persists_devices(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(
        api, "save_config", lambda config: captured.__setitem__("config", config)
    )

    with make_client() as client:
        payload = {
            "devices": [
                {
                    "id": "home",
                    "name": "家里台式机",
                    "host": "192.168.1.20",
                    "port": 2222,
                    "username": "lin",
                    "remote_dir": "/data",
                    "auth_method": "password",
                    "private_key_path": "",
                }
            ],
            "active_device_id": "home",
        }
        response = client.put("/api/config", json=payload, headers=HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert [device["id"] for device in body["devices"]] == ["home"]
        assert body["active_device_id"] == "home"
        # Flat fields mirror the active device for older readers.
        assert body["host"] == "192.168.1.20"
        assert body["port"] == 2222

    saved = captured["config"]
    assert isinstance(saved, api.AppConfig)
    assert saved.devices[0].name == "家里台式机"


def test_config_api_rejects_duplicate_device_ids(monkeypatch) -> None:
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: None)

    with make_client() as client:
        payload = {
            "devices": [
                {"id": "dup", "host": "one"},
                {"id": "dup", "host": "two"},
            ]
        }
        response = client.put("/api/config", json=payload, headers=HEADERS)

        assert response.status_code == 400
        assert "重复" in response.json()["detail"]


def test_directory_transfer_reports_partial_results(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "batch"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"aaaa")

    def fake_upload_tree(**kwargs):
        kwargs["file_progress"]("a.txt", 1, 2)
        kwargs["progress"](10, 10)
        return TreeResult(
            local_root=folder,
            remote_root="/incoming",
            files=(
                TreeFileResult("a.txt", "/incoming/a.txt", 4, True),
                TreeFileResult(
                    "b.txt", "/incoming/b.txt", 6, False, "远端目标文件已存在", "target_exists"
                ),
            ),
            ok_count=1,
            failed_count=1,
            skipped=(),
            size=10,
        )

    monkeypatch.setattr(api, "upload_tree", fake_upload_tree)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            response = client.post(
                "/api/transfers", json=base_payload(folder, recursive=True), headers=HEADERS
            )
            assert response.status_code == 202

            snapshot = wait_for_status(
                client, response.json()["id"], {"partial", "succeeded", "failed"}
            )

            assert snapshot["status"] == "partial"
            assert snapshot["request"]["recursive"] is True
            assert snapshot["result"]["ok_count"] == 1
            assert snapshot["result"]["failed_count"] == 1
            assert snapshot["result"]["files"][1]["error_code"] == "target_exists"
    finally:
        manager.close()


def test_directory_transfer_succeeds_when_every_file_lands(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "all-good"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"a")

    def fake_upload_tree(**kwargs):
        kwargs["file_progress"]("a.txt", 1, 1)
        kwargs["progress"](1, 1)
        return TreeResult(
            local_root=folder,
            remote_root="/incoming",
            files=(TreeFileResult("a.txt", "/incoming/a.txt", 1, True),),
            ok_count=1,
            failed_count=0,
            skipped=(),
            size=1,
        )

    monkeypatch.setattr(api, "upload_tree", fake_upload_tree)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            response = client.post(
                "/api/transfers", json=base_payload(folder, recursive=True), headers=HEADERS
            )
            snapshot = wait_for_status(
                client, response.json()["id"], {"partial", "succeeded", "failed"}
            )

            assert snapshot["status"] == "succeeded"
            assert snapshot["files_done"] == 1
            assert snapshot["files_total"] == 1
            assert snapshot["result"]["failed_count"] == 0
    finally:
        manager.close()


def test_recursive_transfer_requires_a_folder(tmp_path) -> None:
    source = tmp_path / "one.bin"
    source.write_bytes(b"x")

    with make_client() as client:
        response = client.post(
            "/api/transfers", json=base_payload(source, recursive=True), headers=HEADERS
        )

        assert response.status_code == 400
        assert "文件夹" in response.json()["detail"]


def test_failed_task_exposes_the_error_code(tmp_path, monkeypatch) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"x")

    def fake_upload_file(**kwargs):
        raise TransferError("远端目标文件已存在", code="target_exists")

    monkeypatch.setattr(api, "upload_file", fake_upload_file)
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            response = client.post("/api/transfers", json=base_payload(source), headers=HEADERS)
            snapshot = wait_for_status(client, response.json()["id"], {"failed"})

            assert snapshot["error_code"] == "target_exists"
            assert snapshot["error"] == "远端目标文件已存在"
    finally:
        manager.close()


def test_scan_endpoint_summarizes_a_folder(tmp_path) -> None:
    root = tmp_path / "incoming"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.bin").write_bytes(b"12345")
    (root / "empty").mkdir()

    with make_client() as client:
        response = client.post("/api/scan", json={"path": str(root)}, headers=HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["total_files"] == 1
        assert body["total_size"] == 5
        assert body["empty_dirs"] == 1
        assert body["skipped"] == []

        missing = client.post(
            "/api/scan", json={"path": str(tmp_path / "missing")}, headers=HEADERS
        )
        assert missing.status_code == 400


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


def test_logs_endpoint_lists_files(tmp_path, monkeypatch) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "landrop.log").write_text("abcd", encoding="utf-8")
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig(log_dir=str(log_dir)))

    with make_client() as client:
        response = client.get("/api/logs", headers=HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["dir"] == str(log_dir)
        assert body["total_size"] == 4
        assert body["files"][0]["name"] == "landrop.log"


def test_logs_open_endpoint_uses_the_configured_directory(tmp_path, monkeypatch) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig(log_dir=str(tmp_path)))
    monkeypatch.setattr(
        api.logs, "open_directory", lambda directory: opened.append(directory) or True
    )

    with make_client() as client:
        response = client.post("/api/logs/open", headers=HEADERS)

        assert response.json() == {"opened": True}
    assert opened == [tmp_path]


def test_saving_config_reconfigures_logging_and_purges(tmp_path, monkeypatch) -> None:
    configured: list[str] = []
    purged: list[tuple[Path, int]] = []
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: None)
    monkeypatch.setattr(
        api.logs,
        "configure_logging",
        lambda value="", **kwargs: configured.append(value) or tmp_path,
    )
    monkeypatch.setattr(
        api.logs,
        "purge",
        lambda directory, days: purged.append((directory, days)) or [],
    )

    with make_client() as client:
        payload = {"log_dir": str(tmp_path), "log_retention_days": 7}
        response = client.put("/api/config", json=payload, headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["log_retention_days"] == 7

    assert configured == [str(tmp_path)]
    assert purged == [(tmp_path, 7)]


def test_saving_config_rejects_a_relative_log_directory(monkeypatch) -> None:
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: None)

    with make_client() as client:
        response = client.put(
            "/api/config", json={"log_dir": "relative/logs"}, headers=HEADERS
        )

        assert response.status_code == 400
        assert "绝对路径" in response.json()["detail"]


def schedule_payload(source: Path, device: dict, **overrides) -> dict:
    values = {
        "id": "s1",
        "name": "备份",
        "enabled": True,
        "source_path": str(source),
        "recursive": False,
        "device_id": device["id"],
        "remote_dir": "",
        "overwrite": False,
        "mode": "daily",
        "time": "09:00",
        "date": "",
        "weekdays": [],
    }
    values.update(overrides)
    return values


def key_device(**overrides) -> dict:
    values = {
        "id": "d1",
        "name": "家里",
        "host": "192.168.1.9",
        "port": 22,
        "username": "lin",
        "remote_dir": "/incoming",
        "auth_method": "key",
        "private_key_path": "",
    }
    values.update(overrides)
    return values


def test_saving_config_syncs_system_tasks(tmp_path, monkeypatch) -> None:
    synced: list[list] = []
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: None)
    monkeypatch.setattr(
        api.scheduler, "sync_tasks", lambda schedules: synced.append(list(schedules)) or []
    )
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    device = key_device()

    with make_client() as client:
        payload = {"devices": [device], "schedules": [schedule_payload(source, device)]}
        response = client.put("/api/config", json=payload, headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["schedules"][0]["id"] == "s1"

    assert len(synced) == 1
    assert synced[0][0].id == "s1"


def test_schedule_with_missing_source_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: None)
    device = key_device()

    with make_client() as client:
        payload = {
            "devices": [device],
            "schedules": [schedule_payload(tmp_path / "missing.bin", device)],
        }
        response = client.put("/api/config", json=payload, headers=HEADERS)

        assert response.status_code == 400
        assert "来源不存在" in response.json()["detail"]


def test_password_schedule_requires_a_saved_credential(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: None)
    monkeypatch.setattr(api.credentials, "has_password", lambda host, port, user: False)
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    device = key_device(auth_method="password")

    with make_client() as client:
        payload = {"devices": [device], "schedules": [schedule_payload(source, device)]}
        response = client.put("/api/config", json=payload, headers=HEADERS)

        assert response.status_code == 400
        assert "保存密码" in response.json()["detail"]


def test_schedule_status_reports_next_run(tmp_path, monkeypatch) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    config = api.AppConfig(
        devices=(DeviceConfig(id="d1", host="h", username="u", auth_method="key"),),
        schedules=(
            ScheduleConfig(
                id="s1", source_path=str(source), device_id="d1", mode="daily", time="00:00"
            ),
        ),
    )
    monkeypatch.setattr(api, "load_config", lambda: config)

    with make_client() as client:
        response = client.get("/api/schedules", headers=HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["schedules"][0]["id"] == "s1"
        assert body["schedules"][0]["next_run_at"]
        assert isinstance(body["schedules"][0]["missed"], bool)
        assert body["schedules"][0]["last_run_at"] == ""
        assert body["schedules"][0]["enabled"] is True
        assert "system_tasks" in body


def test_run_schedule_endpoint_dispatches(tmp_path, monkeypatch) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    config = api.AppConfig(
        devices=(DeviceConfig(id="d1", host="h", username="u", auth_method="key"),),
        schedules=(
            ScheduleConfig(id="s1", source_path=str(source), device_id="d1", mode="daily"),
        ),
    )

    class FakeTask:
        def snapshot(self) -> dict:
            return {"id": "task-1", "status": "queued"}

    monkeypatch.setattr(api, "load_config", lambda: config)
    monkeypatch.setattr(
        api.scheduler, "dispatch_schedule", lambda manager, cfg, item: FakeTask()
    )

    with make_client() as client:
        response = client.post("/api/schedules/s1/run", headers=HEADERS)
        assert response.status_code == 202
        assert response.json()["id"] == "task-1"

        missing = client.post("/api/schedules/nope/run", headers=HEADERS)
        assert missing.status_code == 404


def test_run_endpoint_creates_a_real_transfer_task(tmp_path, monkeypatch) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"abc")
    config = api.AppConfig(
        devices=(
            DeviceConfig(id="d1", host="h", username="u", auth_method="key", remote_dir="/incoming"),
        ),
        schedules=(
            ScheduleConfig(id="s1", source_path=str(source), device_id="d1", mode="daily"),
        ),
    )
    recorded: list[str] = []
    monkeypatch.setattr(api, "load_config", lambda: config)
    monkeypatch.setattr(
        api.scheduler, "save_run_result", lambda sid, status, when=None: recorded.append(status)
    )
    monkeypatch.setattr(
        api,
        "upload_file",
        lambda **kwargs: TransferResult(source, "/incoming/a.bin", 3, "f" * 64, 0),
    )

    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            response = client.post("/api/schedules/s1/run", headers=HEADERS)
            assert response.status_code == 202

            snapshot = wait_for_status(
                client, response.json()["id"], {"succeeded", "partial", "failed"}
            )
            assert snapshot["status"] == "succeeded"
            assert snapshot["source"] == "schedule"
    finally:
        manager.close()

    # The dispatch records "running" synchronously; the watcher adds the outcome.
    assert recorded[0] == "running"


def test_cli_schedule_argument_parsing() -> None:
    assert api._schedule_argument([]) is None
    assert api._schedule_argument(["--run-schedule", "s1"]) == "s1"
    assert api._schedule_argument(["--run-schedule"]) == ""


def test_list_transfers_returns_tasks_and_applies_limit(tmp_path, monkeypatch) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"x")
    monkeypatch.setattr(
        api,
        "upload_file",
        lambda **kwargs: TransferResult(source, "/remote/payload.bin", 1, "f" * 64, 0),
    )
    manager = TransferManager(max_workers=1)
    try:
        with make_client(manager) as client:
            first = client.post("/api/transfers", json=base_payload(source), headers=HEADERS).json()
            second = client.post("/api/transfers", json=base_payload(source), headers=HEADERS).json()

            listed = client.get("/api/transfers", headers=HEADERS)
            assert listed.status_code == 200
            tasks = listed.json()["tasks"]
            assert {task["id"] for task in tasks} >= {first["id"], second["id"]}
            assert tasks[0]["created_at"] >= tasks[-1]["created_at"]
            assert tasks[0]["source"] in {"manual", "schedule"}
            assert tasks[0]["verified"] == 0
            assert tasks[0]["verify_total"] == 0

            limited = client.get("/api/transfers?limit=1", headers=HEADERS).json()["tasks"]
            assert len(limited) == 1
    finally:
        manager.close()


def test_saving_config_preserves_scheduler_run_state(tmp_path, monkeypatch) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    current = api.AppConfig(
        devices=(
            DeviceConfig(id="d1", host="h", username="u", auth_method="key", remote_dir="/in"),
        ),
        schedules=(
            ScheduleConfig(
                id="s1", source_path=str(source), device_id="d1", mode="daily", time="09:00",
                last_run_at="2026-11-09T09:00:05", last_run_status="succeeded",
            ),
        ),
    )
    saved: dict[str, object] = {}
    monkeypatch.setattr(api, "load_config", lambda: current)
    monkeypatch.setattr(
        api, "save_config", lambda config: saved.__setitem__("config", config)
    )
    monkeypatch.setattr(api.scheduler, "sync_tasks", lambda schedules: [])

    with make_client() as client:
        payload = {
            "devices": [
                {
                    "id": "d1", "host": "h", "username": "u", "auth_method": "key",
                    "remote_dir": "/in", "private_key_path": "",
                }
            ],
            "schedules": [
                {
                    "id": "s1", "source_path": str(source), "device_id": "d1",
                    "mode": "daily", "time": "09:00", "enabled": True,
                    "last_run_at": "", "last_run_status": "",
                }
            ],
        }
        response = client.put("/api/config", json=payload, headers=HEADERS)

        assert response.status_code == 200
        body = response.json()["schedules"][0]
        # The stale client values must not erase what the scheduler recorded.
        assert body["last_run_at"] == "2026-11-09T09:00:05"
        assert body["last_run_status"] == "succeeded"

    config = saved["config"]
    assert isinstance(config, api.AppConfig)
    assert config.schedules[0].last_run_at == "2026-11-09T09:00:05"


def test_finished_one_shot_stays_disabled(tmp_path, monkeypatch) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    current = api.AppConfig(
        devices=(
            DeviceConfig(id="d1", host="h", username="u", auth_method="key", remote_dir="/in"),
        ),
        schedules=(
            ScheduleConfig(
                id="s1", source_path=str(source), device_id="d1", mode="once",
                time="09:00", date="2026-11-01", enabled=False,
                last_run_at="2026-11-01T09:00:07", last_run_status="succeeded",
            ),
        ),
    )
    saved: dict[str, object] = {}
    monkeypatch.setattr(api, "load_config", lambda: current)
    monkeypatch.setattr(api, "save_config", lambda config: saved.__setitem__("config", config))
    monkeypatch.setattr(api.scheduler, "sync_tasks", lambda schedules: [])

    with make_client() as client:
        payload = {
            "devices": [
                {
                    "id": "d1", "host": "h", "username": "u", "auth_method": "key",
                    "remote_dir": "/in", "private_key_path": "",
                }
            ],
            "schedules": [
                {
                    "id": "s1", "source_path": str(source), "device_id": "d1",
                    "mode": "once", "time": "09:00", "date": "2026-11-01", "enabled": True,
                }
            ],
        }
        response = client.put("/api/config", json=payload, headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["schedules"][0]["enabled"] is False

    config = saved["config"]
    assert isinstance(config, api.AppConfig)
    assert config.schedules[0].enabled is False


def test_past_one_shot_schedule_is_rejected(tmp_path, monkeypatch) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    monkeypatch.setattr(api, "load_config", lambda: api.AppConfig())
    monkeypatch.setattr(api, "save_config", lambda config: None)
    monkeypatch.setattr(api.scheduler, "sync_tasks", lambda schedules: [])

    with make_client() as client:
        payload = {
            "devices": [
                {
                    "id": "d1", "host": "h", "username": "u", "auth_method": "key",
                    "remote_dir": "/in", "private_key_path": "",
                }
            ],
            "schedules": [
                {
                    "id": "s1", "source_path": str(source), "device_id": "d1",
                    "mode": "once", "time": "09:00", "date": "2026-11-01", "enabled": True,
                }
            ],
        }
        response = client.put("/api/config", json=payload, headers=HEADERS)

        assert response.status_code == 400
        assert "已经过去" in response.json()["detail"]

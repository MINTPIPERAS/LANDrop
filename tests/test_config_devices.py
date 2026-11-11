"""Multiple saved devices: migration, canonicalization and validation."""

from __future__ import annotations

import json

import pytest

from landrop.config import AppConfig, DeviceConfig, load_config, save_config


def device(**overrides) -> DeviceConfig:
    values = {
        "id": "a",
        "name": "家里台式机",
        "host": "192.168.1.20",
        "port": 22,
        "username": "lin",
        "remote_dir": "/data",
        "auth_method": "password",
        "private_key_path": "",
    }
    values.update(overrides)
    return DeviceConfig(**values)


def test_flat_config_migrates_into_one_device(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "host": "192.168.1.20",
                "port": 2222,
                "username": "alice",
                "remote_dir": "/srv/drop",
                "auth_method": "password",
                "private_key_path": "",
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert len(config.devices) == 1
    migrated = config.devices[0]
    assert migrated.id == "default"
    assert migrated.name == "alice@192.168.1.20"
    assert migrated.host == "192.168.1.20"
    assert migrated.port == 2222
    assert migrated.remote_dir == "/srv/drop"
    assert config.active_device_id == "default"


def test_empty_config_has_no_devices() -> None:
    config = AppConfig().normalized()

    assert config.devices == ()
    assert config.active_device_id == ""


def test_multiple_devices_round_trip_and_mirror_active(tmp_path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig(
        devices=(device(id="a", host="192.168.1.20"), device(id="b", host="nas.lan", port=2222)),
        active_device_id="b",
    )

    save_config(config, path)
    loaded = load_config(path)

    assert [item.id for item in loaded.devices] == ["a", "b"]
    assert loaded.active_device_id == "b"
    # Flat fields keep mirroring the active device for older readers.
    assert loaded.host == "nas.lan"
    assert loaded.port == 2222


def test_unknown_active_id_falls_back_to_first_device() -> None:
    config = AppConfig(
        devices=(device(id="a"), device(id="b")),
        active_device_id="missing",
    ).normalized()

    assert config.active_device_id == "a"
    assert config.host == "192.168.1.20"


def test_active_device_overrides_stale_flat_fields() -> None:
    config = AppConfig(
        host="stale-host",
        username="stale-user",
        devices=(device(id="a", host="fresh-host", username="fresh-user"),),
        active_device_id="a",
    ).normalized()

    assert config.host == "fresh-host"
    assert config.username == "fresh-user"


def test_dict_devices_are_coerced() -> None:
    config = AppConfig(devices=({"id": "a", "host": "192.168.1.9"},))

    assert isinstance(config.devices[0], DeviceConfig)
    assert config.devices[0].host == "192.168.1.9"


def test_duplicate_device_ids_are_rejected() -> None:
    config = AppConfig(devices=(device(id="same"), device(id="same")))

    with pytest.raises(ValueError, match="设备 ID 重复"):
        config.validate()


def test_device_port_is_validated() -> None:
    with pytest.raises(ValueError, match="端口"):
        AppConfig(devices=(device(port=0),)).validate()


def test_device_auth_method_is_validated() -> None:
    with pytest.raises(ValueError, match="认证方式"):
        AppConfig(devices=(device(auth_method="agent"),)).validate()


def test_device_private_key_must_exist(tmp_path) -> None:
    missing = tmp_path / "id_ed25519"
    with pytest.raises(ValueError, match="私钥"):
        AppConfig(
            devices=(device(auth_method="key", private_key_path=str(missing)),)
        ).validate()


def test_device_private_key_may_be_omitted() -> None:
    AppConfig(devices=(device(auth_method="key", private_key_path=""),)).validate()


def test_password_device_does_not_require_a_key() -> None:
    AppConfig(
        devices=(device(auth_method="password", private_key_path="C:/keys/missing"),)
    ).validate()


def test_malformed_hotkey_still_migrates_devices(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"host": "192.168.1.7", "hotkey_modifiers": [], "hotkey_key": ""}),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.hotkey_key == "space"
    assert len(config.devices) == 1
    assert config.active_device_id == "default"

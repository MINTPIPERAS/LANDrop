from __future__ import annotations

import json

import pytest

from landrop.config import (
    AppConfig,
    load_config,
    normalize_key,
    normalize_modifiers,
    save_config,
)


def test_defaults_are_valid() -> None:
    config = AppConfig()
    config.validate()
    assert config.port == 22
    assert config.auth_method == "key"


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_rejects_invalid_ports(port: int) -> None:
    with pytest.raises(ValueError, match="端口"):
        AppConfig(port=port).validate()


def test_round_trip_config(tmp_path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig(
        host="192.168.1.20",
        username="alice",
        remote_dir="/home/alice/Downloads",
        hotkey_modifiers=("ctrl", "shift"),
    )

    save_config(config, path)

    assert load_config(path) == config


def test_rejects_plaintext_password(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"password": "secret"}), encoding="utf-8")

    with pytest.raises(ValueError, match="明文密码"):
        load_config(path)


def test_missing_config_returns_defaults(tmp_path) -> None:
    assert load_config(tmp_path / "missing.json") == AppConfig()


def test_hotkey_modifiers_are_deduplicated_and_ordered() -> None:
    assert normalize_modifiers(["alt", "ctrl", "alt"]) == ("ctrl", "alt")
    assert normalize_modifiers(("SHIFT", "ctrl")) == ("ctrl", "shift")
    assert normalize_modifiers([]) == ()


def test_hotkey_rejects_unknown_modifier() -> None:
    with pytest.raises(ValueError, match="不支持的热键修饰键"):
        normalize_modifiers(["ctrl", "hyper"])


def test_hotkey_key_is_normalized() -> None:
    assert normalize_key("  F9 ") == "f9"
    assert normalize_key("K") == "k"


@pytest.mark.parametrize("key", ["", "   ", "a b", "ctrl+c", "a,b"])
def test_hotkey_key_rejects_separators(key: str) -> None:
    with pytest.raises(ValueError):
        normalize_key(key)


def test_hotkey_requires_at_least_one_modifier() -> None:
    with pytest.raises(ValueError, match="至少需要一个修饰键"):
        AppConfig(hotkey_modifiers=()).normalized()


def test_config_round_trip_preserves_hotkey(tmp_path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig(hotkey_modifiers=("shift", "ctrl"), hotkey_key="F9")

    save_config(config, path)

    assert load_config(path).hotkey_modifiers == ("ctrl", "shift")
    assert load_config(path).hotkey_key == "f9"


def test_malformed_hotkey_falls_back_without_blocking_config(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"host": "192.168.1.7", "hotkey_modifiers": [], "hotkey_key": ""}),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.host == "192.168.1.7"
    assert config.hotkey_modifiers == ("ctrl", "alt")
    assert config.hotkey_key == "space"


def test_non_list_hotkey_modifiers_are_rejected(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hotkey_modifiers": "ctrl"}), encoding="utf-8")

    with pytest.raises(ValueError, match="热键修饰键配置无效"):
        load_config(path)

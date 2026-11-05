"""Configuration loading and validation for LANDrop."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Sequence

AuthMethod = Literal["password", "key"]

# Modifier names accepted by the Tauri global-shortcut plugin, in display order.
HOTKEY_MODIFIERS = ("ctrl", "alt", "shift", "super")


def normalize_modifiers(modifiers: Sequence[str]) -> tuple[str, ...]:
    """Return unique modifiers in canonical order, rejecting unknown names."""
    if isinstance(modifiers, str):
        raise ValueError("热键修饰键配置无效")
    seen: list[str] = []
    for modifier in modifiers:
        if not isinstance(modifier, str):
            raise ValueError("热键修饰键配置无效")
        name = modifier.strip().lower()
        if name not in HOTKEY_MODIFIERS:
            raise ValueError(f"不支持的热键修饰键: {modifier}")
        if name not in seen:
            seen.append(name)
    return tuple(sorted(seen, key=HOTKEY_MODIFIERS.index))


def normalize_key(key: str) -> str:
    """Normalize a hotkey main key; the shortcut plugin parses names case-insensitively."""
    if not isinstance(key, str):
        raise ValueError("热键按键配置无效")
    name = key.strip().lower()
    if not name:
        raise ValueError("热键按键不能为空")
    if len(name) > 32:
        raise ValueError("热键按键名称过长")
    if any(character.isspace() or character in "+," for character in name) or any(
        ord(character) < 32 for character in name
    ):
        raise ValueError("热键按键不能包含空白、加号或控制字符")
    return name


@dataclass(frozen=True, slots=True)
class AppConfig:
    host: str = ""
    port: int = 22
    username: str = ""
    remote_dir: str = ""
    auth_method: AuthMethod = "key"
    private_key_path: str = ""
    hotkey_modifiers: tuple[str, ...] = ("ctrl", "alt")
    hotkey_key: str = "space"

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH 端口必须在 1 到 65535 之间")
        if self.auth_method not in ("password", "key"):
            raise ValueError("认证方式必须是 password 或 key")
        if self.auth_method == "key" and self.private_key_path:
            if not Path(self.private_key_path).expanduser().is_file():
                raise ValueError("指定的 SSH 私钥文件不存在")

    def normalized(self) -> "AppConfig":
        """Return a copy with the hotkey canonicalized for the shortcut plugin."""
        modifiers = normalize_modifiers(self.hotkey_modifiers)
        if not modifiers:
            # A bare key would be captured globally and break typing everywhere.
            raise ValueError("热键至少需要一个修饰键")
        return replace(self, hotkey_modifiers=modifiers, hotkey_key=normalize_key(self.hotkey_key))

    def with_fallback_hotkey(self) -> "AppConfig":
        """Keep a malformed hotkey from blocking an otherwise valid config."""
        try:
            return self.normalized()
        except ValueError:
            defaults = AppConfig()
            return replace(
                self,
                hotkey_modifiers=defaults.hotkey_modifiers,
                hotkey_key=defaults.hotkey_key,
            )


def config_path() -> Path:
    """Return a per-user config path without depending on the current directory."""
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "LANDrop" / "config.json"
    return Path.home() / ".config" / "landrop" / "config.json"


def load_config(path: Path | None = None) -> AppConfig:
    target = path or config_path()
    if not target.exists():
        return AppConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("配置文件内容必须是 JSON 对象")
        if "password" in raw:
            raise ValueError("配置文件包含不支持的明文密码，请移除该字段")
        if "hotkey_modifiers" in raw and not isinstance(raw["hotkey_modifiers"], list):
            raise ValueError("热键修饰键配置无效")
        config = AppConfig(**raw)
        config.validate()
        return config.with_fallback_hotkey()
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"配置文件格式无效: {exc}") from exc


def save_config(config: AppConfig, path: Path | None = None) -> None:
    config = config.normalized()
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False
        ) as temp:
            temp.write(data)
            temp_name = temp.name
        os.replace(temp_name, target)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)

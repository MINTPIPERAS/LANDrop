"""Configuration loading and validation for LANDrop."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal, Sequence

AuthMethod = Literal["password", "key"]

# Identifier used when a v0.1 flat configuration is migrated into a device.
DEFAULT_DEVICE_ID = "default"

SCHEDULE_MODES = ("once", "daily", "weekly")
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

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


def parse_schedule_time(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` into (hour, minute), rejecting everything else."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("时间格式必须是 HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError("时间格式必须是 HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("时间必须在 00:00 到 23:59 之间")
    return hour, minute


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """One timed transfer. ``last_run_*`` is runtime state stored with the rule."""

    id: str
    name: str = ""
    enabled: bool = True
    source_path: str = ""
    recursive: bool = False
    device_id: str = ""
    remote_dir: str = ""
    overwrite: bool = False
    mode: str = "once"
    time: str = "09:00"
    date: str = ""
    weekdays: tuple[int, ...] = ()
    last_run_at: str = ""
    last_run_status: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.weekdays, tuple):
            object.__setattr__(self, "weekdays", tuple(int(day) for day in self.weekdays))

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("定时任务 ID 不能为空")
        if self.mode not in SCHEDULE_MODES:
            raise ValueError("定时模式必须是 once、daily 或 weekly")
        parse_schedule_time(self.time)
        if self.mode == "once":
            try:
                date.fromisoformat(self.date)
            except ValueError as exc:
                raise ValueError("一次性定时任务需要有效的日期") from exc
        if self.mode == "weekly":
            if not self.weekdays:
                raise ValueError("每周任务至少要选择一天")
            if any(not 0 <= day <= 6 for day in self.weekdays):
                raise ValueError("星期取值必须在 0 到 6 之间")
        if not self.source_path.strip():
            raise ValueError("定时任务的来源路径不能为空")


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """One saved remote machine.

    ``id`` is stable for the lifetime of the entry because schedules reference
    devices by id. ``name`` is display-only and may be empty.
    """

    id: str
    name: str = ""
    host: str = ""
    port: int = 22
    username: str = ""
    remote_dir: str = ""
    auth_method: AuthMethod = "key"
    private_key_path: str = ""

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("设备 ID 不能为空")
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH 端口必须在 1 到 65535 之间")
        if self.auth_method not in ("password", "key"):
            raise ValueError("认证方式必须是 password 或 key")
        if self.auth_method == "key" and self.private_key_path:
            if not Path(self.private_key_path).expanduser().is_file():
                raise ValueError("指定的 SSH 私钥文件不存在")


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
    devices: tuple[DeviceConfig, ...] = ()
    active_device_id: str = ""
    log_dir: str = ""
    log_retention_days: int = 14
    schedules: tuple[ScheduleConfig, ...] = ()
    close_to_tray: bool = True

    def __post_init__(self) -> None:
        # Accept plain dicts from JSON and from the API payload, and always
        # store tuples so equality and round-trips are stable.
        object.__setattr__(
            self,
            "devices",
            tuple(
                device if isinstance(device, DeviceConfig) else DeviceConfig(**device)
                for device in self.devices
            ),
        )
        object.__setattr__(
            self,
            "schedules",
            tuple(
                schedule
                if isinstance(schedule, ScheduleConfig)
                else ScheduleConfig(**schedule)
                for schedule in self.schedules
            ),
        )

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH 端口必须在 1 到 65535 之间")
        if self.auth_method not in ("password", "key"):
            raise ValueError("认证方式必须是 password 或 key")
        if self.auth_method == "key" and self.private_key_path:
            if not Path(self.private_key_path).expanduser().is_file():
                raise ValueError("指定的 SSH 私钥文件不存在")
        seen: set[str] = set()
        for device in self.devices:
            device.validate()
            if device.id in seen:
                raise ValueError(f"设备 ID 重复: {device.id}")
            seen.add(device.id)
        schedule_ids: set[str] = set()
        for schedule in self.schedules:
            schedule.validate()
            if schedule.id in schedule_ids:
                raise ValueError(f"定时任务 ID 重复: {schedule.id}")
            schedule_ids.add(schedule.id)
            if schedule.device_id not in seen:
                raise ValueError(
                    f"定时任务「{schedule.name or schedule.id}」引用的设备不存在"
                )
        if not 0 <= self.log_retention_days <= 3650:
            raise ValueError("日志保留天数必须在 0 到 3650 之间")
        if self.log_dir.strip() and not Path(self.log_dir).expanduser().is_absolute():
            raise ValueError("日志目录必须是绝对路径")

    def normalized(self) -> "AppConfig":
        """Return a copy with the hotkey canonicalized and devices reconciled.

        The flat connection fields mirror the active device so older builds and
        the documented config shape keep working.
        """
        modifiers = normalize_modifiers(self.hotkey_modifiers)
        if not modifiers:
            # A bare key would be captured globally and break typing everywhere.
            raise ValueError("热键至少需要一个修饰键")
        devices, active_id = self._canonical_devices()
        changes: dict[str, object] = {
            "hotkey_modifiers": modifiers,
            "hotkey_key": normalize_key(self.hotkey_key),
            "devices": devices,
            "active_device_id": active_id,
        }
        active = next((device for device in devices if device.id == active_id), None)
        if active is not None:
            changes.update(
                host=active.host,
                port=active.port,
                username=active.username,
                remote_dir=active.remote_dir,
                auth_method=active.auth_method,
                private_key_path=active.private_key_path,
            )
        return replace(self, **changes)

    def _canonical_devices(self) -> tuple[tuple[DeviceConfig, ...], str]:
        devices = list(self.devices)
        if not devices and self.host.strip():
            # v0.1 wrote a single flat connection; promote it to one device.
            devices = [
                DeviceConfig(
                    id=DEFAULT_DEVICE_ID,
                    name=f"{self.username}@{self.host}" if self.username else self.host,
                    host=self.host,
                    port=self.port,
                    username=self.username,
                    remote_dir=self.remote_dir,
                    auth_method=self.auth_method,
                    private_key_path=self.private_key_path,
                )
            ]
        known = {device.id for device in devices}
        active = self.active_device_id.strip()
        if active not in known:
            active = devices[0].id if devices else ""
        return tuple(devices), active

    def with_fallback_hotkey(self) -> "AppConfig":
        """Keep a malformed hotkey from blocking an otherwise valid config."""
        try:
            return self.normalized()
        except ValueError:
            defaults = AppConfig()
            repaired = replace(
                self,
                hotkey_modifiers=defaults.hotkey_modifiers,
                hotkey_key=defaults.hotkey_key,
            )
            # Device migration is independent of the hotkey, so still apply it.
            return repaired.normalized()


def app_data_dir() -> Path:
    """Per-user application data directory, independent of the working directory."""
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "LANDrop"
    return Path.home() / ".config" / "landrop"


def config_path() -> Path:
    """Return the config file path without depending on the current directory."""
    return app_data_dir() / "config.json"


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

"""Timed transfers: schedule math, Windows task registration, headless runs.

Three paths can execute a schedule, and they must never overlap:

- the in-app loop (the service is running, progress visible in the UI);
- ``landrop-service.exe --run-schedule <id>`` started by Task Scheduler;
- the explicit "run now" button.

The first two contend on ``run.lock``: the running service holds it for its whole
lifetime, so the scheduled process skips a run instead of duplicating it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Callable, Sequence

from landrop import credentials, hostkeys
from landrop.config import (
    WEEKDAY_NAMES,
    AppConfig,
    ScheduleConfig,
    app_data_dir,
    load_config,
    parse_schedule_time,
    save_config,
)
from landrop.transfer import TransferCancelled, TransferError, upload_file, upload_tree

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
DUE_GRACE = timedelta(minutes=5)
TASK_PREFIX = "LANDrop-"
RUN_LOCK_NAME = "run.lock"
TERMINAL_STATUSES = ("succeeded", "partial", "failed", "cancelled")

# Serializes read-modify-write of config.json between the API and the scheduler.
_CONFIG_LOCK = threading.Lock()

if os.name == "nt":  # pragma: no cover - platform import
    import msvcrt


def config_lock() -> threading.Lock:
    return _CONFIG_LOCK


# --------------------------------------------------------------------------- #
# Schedule math
# --------------------------------------------------------------------------- #


def _scheduled_datetime(day: date, schedule: ScheduleConfig) -> datetime:
    hour, minute = parse_schedule_time(schedule.time)
    return datetime.combine(day, clock_time(hour, minute))


def _previous_occurrence(schedule: ScheduleConfig, now: datetime) -> datetime | None:
    if schedule.mode == "once":
        try:
            day = date.fromisoformat(schedule.date)
        except ValueError:
            return None
        candidate = _scheduled_datetime(day, schedule)
        return candidate if candidate <= now else None
    if schedule.mode == "daily":
        today = _scheduled_datetime(now.date(), schedule)
        return today if today <= now else today - timedelta(days=1)
    if schedule.mode == "weekly":
        for offset in range(0, 8):
            day = now.date() - timedelta(days=offset)
            if day.weekday() not in schedule.weekdays:
                continue
            candidate = _scheduled_datetime(day, schedule)
            if candidate <= now:
                return candidate
    return None


def next_run_at(schedule: ScheduleConfig, now: datetime | None = None) -> datetime | None:
    """The next moment this schedule fires, or ``None`` for a used-up once task."""
    current = now or datetime.now()
    if schedule.mode == "once":
        try:
            day = date.fromisoformat(schedule.date)
        except ValueError:
            return None
        candidate = _scheduled_datetime(day, schedule)
        return candidate if candidate > current else None
    if schedule.mode == "daily":
        candidate = _scheduled_datetime(current.date(), schedule)
        return candidate if candidate > current else candidate + timedelta(days=1)
    if schedule.mode == "weekly":
        for offset in range(0, 8):
            day = current.date() + timedelta(days=offset)
            if day.weekday() not in schedule.weekdays:
                continue
            candidate = _scheduled_datetime(day, schedule)
            if candidate > current:
                return candidate
    return None


def _last_run(schedule: ScheduleConfig) -> datetime | None:
    if not schedule.last_run_at:
        return None
    try:
        return datetime.fromisoformat(schedule.last_run_at)
    except ValueError:
        return None


def due_at(schedule: ScheduleConfig, now: datetime | None = None) -> datetime | None:
    """The latest scheduled moment that has not been recorded as started."""
    current = now or datetime.now()
    occurrence = _previous_occurrence(schedule, current)
    if occurrence is None:
        return None
    last = _last_run(schedule)
    if last is not None and last >= occurrence:
        return None
    return occurrence


def is_missed(schedule: ScheduleConfig, now: datetime | None = None) -> bool:
    """True when a scheduled moment slipped past the in-app grace window."""
    current = now or datetime.now()
    occurrence = due_at(schedule, current)
    return occurrence is not None and current - occurrence > DUE_GRACE


# --------------------------------------------------------------------------- #
# Windows Task Scheduler
# --------------------------------------------------------------------------- #


def task_name(schedule_id: str) -> str:
    return f"{TASK_PREFIX}{schedule_id[:8]}"


def _executable() -> Path:
    return Path(sys.executable)


def task_command(schedule: ScheduleConfig) -> str:
    return f'"{_executable()}" --run-schedule {schedule.id}'


def _system_short_date(day: date) -> str:
    """Format a date the way this Windows locale's ``schtasks /SD`` expects.

    English systems want MM/DD/YYYY while Chinese ones want YYYY/MM/DD, so ask
    the system for its short-date format instead of guessing.
    """
    fallback = day.strftime("%Y/%m/%d")
    if os.name != "nt":
        return fallback
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - non-Windows
        return fallback

    class SystemTime(ctypes.Structure):
        _fields_ = [
            ("wYear", wintypes.WORD),
            ("wMonth", wintypes.WORD),
            ("wDayOfWeek", wintypes.WORD),
            ("wDay", wintypes.WORD),
            ("wHour", wintypes.WORD),
            ("wMinute", wintypes.WORD),
            ("wSecond", wintypes.WORD),
            ("wMilliseconds", wintypes.WORD),
        ]

    system_time = SystemTime(day.year, day.month, 0, day.day, 0, 0, 0, 0)
    buffer = ctypes.create_unicode_buffer(64)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetDateFormatW.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SystemTime),
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    kernel32.GetDateFormatW.restype = ctypes.c_int
    written = kernel32.GetDateFormatW(
        0,  # LOCALE_USER_DEFAULT
        0x00000001,  # DATE_SHORTDATE
        ctypes.byref(system_time),
        None,
        buffer,
        len(buffer),
    )
    return buffer.value if written else fallback


def build_create_args(schedule: ScheduleConfig) -> list[str]:
    args = [
        "schtasks",
        "/Create",
        "/TN",
        task_name(schedule.id),
        "/TR",
        task_command(schedule),
        "/F",
        "/SC",
    ]
    if schedule.mode == "once":
        day = date.fromisoformat(schedule.date)
        args += ["ONCE", "/SD", _system_short_date(day), "/ST", schedule.time]
    elif schedule.mode == "daily":
        args += ["DAILY", "/ST", schedule.time]
    else:
        days = ",".join(
            WEEKDAY_NAMES[day].upper() for day in sorted(set(schedule.weekdays))
        )
        args += ["WEEKLY", "/D", days, "/ST", schedule.time]
    return args


def build_delete_args(schedule_id: str) -> list[str]:
    return ["schtasks", "/Delete", "/TN", task_name(schedule_id), "/F"]


def _decode_console(data: bytes) -> str:
    """Console tools answer in the OEM code page, not necessarily UTF-8."""
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _run_schtasks(args: list[str]) -> tuple[int, str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=20,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return completed.returncode, _decode_console(completed.stdout + completed.stderr)


def system_tasks_supported() -> bool:
    """Only a packaged sidecar may register tasks; tests/dev opt in explicitly."""
    if os.name != "nt":
        return False
    return bool(getattr(sys, "frozen", False)) or os.environ.get(
        "LANDROP_ENABLE_SCHTASKS"
    ) == "1"


def _managed_task_names(runner: Callable[[list[str]], tuple[int, str]]) -> set[str]:
    code, output = runner(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if code != 0:
        return set()
    names: set[str] = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        name = line.split(",")[0].strip().strip('"')
        if name.startswith(TASK_PREFIX):
            names.add(name)
    return names


def sync_tasks(
    schedules: Sequence[ScheduleConfig],
    runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> list[dict[str, object]]:
    """Mirror the enabled schedules into Windows Task Scheduler."""
    runner = runner or _run_schtasks
    if not system_tasks_supported():
        return [
            {
                "id": schedule.id,
                "task": task_name(schedule.id),
                "ok": False,
                "error": "开发模式不注册系统任务",
            }
            for schedule in schedules
        ]

    desired = {task_name(item.id): item for item in schedules if item.enabled}
    statuses: list[dict[str, object]] = []

    for name in sorted(_managed_task_names(runner) - set(desired)):
        code, output = runner(["schtasks", "/Delete", "/TN", name, "/F"])
        if code != 0:
            logger.warning("删除系统任务 %s 失败：%s", name, output.strip()[:300])

    for schedule in schedules:
        if not schedule.enabled:
            statuses.append(
                {"id": schedule.id, "task": task_name(schedule.id), "ok": True, "error": None}
            )
            continue
        code, output = runner(build_create_args(schedule))
        statuses.append(
            {
                "id": schedule.id,
                "task": task_name(schedule.id),
                "ok": code == 0,
                "error": None if code == 0 else output.strip()[:500],
            }
        )
        if code != 0:
            logger.warning("注册系统任务失败 %s：%s", schedule.id, output.strip()[:300])
    return statuses


def delete_task(schedule_id: str, runner: Callable[[list[str]], tuple[int, str]] | None = None) -> None:
    if not system_tasks_supported():
        return
    (runner or _run_schtasks)(build_delete_args(schedule_id))


# --------------------------------------------------------------------------- #
# Running schedules
# --------------------------------------------------------------------------- #


def _record_run(config: AppConfig, schedule_id: str, status: str, when: datetime) -> AppConfig:
    updated: list[ScheduleConfig] = []
    for item in config.schedules:
        if item.id == schedule_id:
            item = replace(
                item,
                last_run_at=when.isoformat(timespec="seconds"),
                last_run_status=status,
                # A one-shot rule is finished once it has run.
                enabled=False if item.mode == "once" else item.enabled,
            )
        updated.append(item)
    return replace(config, schedules=tuple(updated))


def save_run_result(schedule_id: str, status: str, when: datetime | None = None) -> None:
    with _CONFIG_LOCK:
        try:
            config = load_config()
        except ValueError:
            logger.warning("无法读取配置，未记录定时任务结果", exc_info=True)
            return
        save_config(_record_run(config, schedule_id, status, when or datetime.now()))


def _resolve(schedule: ScheduleConfig, config: AppConfig):
    device = next((item for item in config.devices if item.id == schedule.device_id), None)
    if device is None:
        raise ValueError("定时任务引用的设备不存在")
    password = None
    if device.auth_method == "password":
        password = credentials.load_password(device.host, device.port, device.username)
        if password is None:
            raise ValueError("该设备没有已保存的密码，无法无人值守执行")
    return device, password


def dispatch_schedule(manager, config: AppConfig, schedule: ScheduleConfig):
    """Queue a schedule through the normal transfer manager (progress in the UI)."""
    from landrop.api.app import TransferRequest  # local import avoids a cycle

    device, password = _resolve(schedule, config)
    target = schedule.remote_dir.strip() or device.remote_dir.strip()
    if not target:
        raise ValueError("该设备与定时任务都没有设置目标目录")
    request = TransferRequest(
        host=device.host,
        port=device.port,
        username=device.username,
        remote_dir=target,
        local_path=schedule.source_path,
        auth_method=device.auth_method,
        password=password,
        private_key_path=device.private_key_path or None,
        overwrite=schedule.overwrite,
        recursive=schedule.recursive,
    )
    task = manager.start(request, source="schedule")
    logger.info("定时任务派发 %s → 任务 %s", schedule.name or schedule.id, task.task_id)
    save_run_result(schedule.id, "running")
    threading.Thread(
        target=_watch_result, args=(manager, task, schedule.id), daemon=True
    ).start()
    return task


def _watch_result(manager, task, schedule_id: str) -> None:
    while True:
        time.sleep(2)
        try:
            current = manager.get(task.task_id)
        except Exception:  # noqa: BLE001 - the watcher must not die silently
            return
        if current is None:
            return
        with current.condition:
            status = current.status
        if status in TERMINAL_STATUSES:
            try:
                save_run_result(schedule_id, status)
            except Exception:  # noqa: BLE001
                logger.exception("记录定时任务结果失败")
            return


def run_once(schedule_id: str) -> int:
    """Headless execution used by ``--run-schedule``; returns a process exit code."""
    try:
        config = load_config()
    except ValueError as exc:
        logger.error("定时任务无法读取配置：%s", exc)
        return 2

    schedule = next((item for item in config.schedules if item.id == schedule_id), None)
    if schedule is None:
        logger.error("找不到定时任务 %s", schedule_id)
        return 2

    label = schedule.name or schedule.id
    try:
        device, password = _resolve(schedule, config)
    except (ValueError, credentials.CredentialError) as exc:
        logger.error("定时任务 %s 无法执行：%s", label, exc)
        save_run_result(schedule_id, "failed")
        return 3

    known_hosts = hostkeys.known_hosts_path()
    status = "failed"
    try:
        if schedule.recursive:
            tree = upload_tree(
                host=device.host,
                port=device.port,
                username=device.username,
                local_path=schedule.source_path,
                remote_dir=schedule.remote_dir.strip() or device.remote_dir,
                auth_method=device.auth_method,
                password=password,
                private_key_path=device.private_key_path or None,
                known_hosts_path=known_hosts if known_hosts.is_file() else None,
                overwrite=schedule.overwrite,
            )
            status = "partial" if tree.failed_count else "succeeded"
        else:
            upload_file(
                host=device.host,
                port=device.port,
                username=device.username,
                local_path=schedule.source_path,
                remote_dir=schedule.remote_dir.strip() or device.remote_dir,
                auth_method=device.auth_method,
                password=password,
                private_key_path=device.private_key_path or None,
                known_hosts_path=known_hosts if known_hosts.is_file() else None,
                overwrite=schedule.overwrite,
            )
            status = "succeeded"
    except TransferCancelled:
        status = "cancelled"
    except (TransferError, ValueError) as exc:
        logger.error("定时任务 %s 执行失败：%s", label, exc)
        status = "failed"
    except Exception:  # noqa: BLE001 - log and record, never crash silently
        logger.exception("定时任务 %s 执行异常", label)
        status = "failed"

    save_run_result(schedule_id, status)
    if schedule.mode == "once":
        delete_task(schedule.id)
    logger.info("定时任务 %s 执行结束：%s", label, status)
    return 0 if status in ("succeeded", "partial") else 1


class ScheduleLoop(threading.Thread):
    """Fires due schedules while the service is running."""

    def __init__(self, manager, interval: float = TICK_SECONDS) -> None:
        super().__init__(name="landrop-scheduler", daemon=True)
        self._manager = manager
        self._interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the loop alive
                logger.exception("定时调度线程出错")

    def tick(self, now: datetime | None = None) -> None:
        try:
            config = load_config()
        except ValueError:
            logger.warning("定时调度无法读取配置", exc_info=True)
            return
        current = now or datetime.now()
        for schedule in config.schedules:
            if not schedule.enabled:
                continue
            occurrence = due_at(schedule, current)
            if occurrence is None:
                continue
            if current - occurrence > DUE_GRACE:
                # Too late to fire automatically; the UI offers a manual re-run.
                continue
            try:
                dispatch_schedule(self._manager, config, schedule)
            except (ValueError, credentials.CredentialError) as exc:
                logger.error("定时任务 %s 无法派发：%s", schedule.name or schedule.id, exc)
                save_run_result(schedule.id, "failed", current)


# --------------------------------------------------------------------------- #
# Run lock
# --------------------------------------------------------------------------- #


def run_lock_path() -> Path:
    return app_data_dir() / RUN_LOCK_NAME


class RunLock:
    """Exclusive lock shared by the GUI service and headless scheduled runs."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or run_lock_path()
        self._handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+b")
            if handle.tell() == 0:
                handle.write(b"L")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            if handle is not None:
                handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            logger.debug("释放运行锁失败", exc_info=True)
        finally:
            self._handle.close()
            self._handle = None

"""Schedule math, Windows task mirroring and headless execution."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from landrop import scheduler
from landrop.config import AppConfig, DeviceConfig, ScheduleConfig
from landrop.transfer import TransferError, TransferResult


def schedule(**overrides) -> ScheduleConfig:
    values = {
        "id": "s1",
        "name": "备份",
        "enabled": True,
        "source_path": "C:/src/a.bin",
        "device_id": "d1",
        "mode": "once",
        "time": "09:00",
        "date": "2026-11-10",
    }
    values.update(overrides)
    return ScheduleConfig(**values)


def config_with(*schedules: ScheduleConfig) -> AppConfig:
    return AppConfig(
        devices=(
            DeviceConfig(
                id="d1", host="192.168.1.9", username="lin",
                auth_method="key", remote_dir="/incoming",
            ),
        ),
        schedules=tuple(schedules),
    )


def test_next_run_for_a_one_shot_task() -> None:
    now = datetime(2026, 11, 6, 8, 0)

    assert scheduler.next_run_at(schedule(date="2026-11-06", time="09:00"), now) == datetime(
        2026, 11, 6, 9, 0
    )
    assert scheduler.next_run_at(schedule(date="2026-11-05"), now) is None


def test_next_run_for_daily_rolls_to_tomorrow() -> None:
    assert scheduler.next_run_at(
        schedule(mode="daily", date="", time="09:00"), datetime(2026, 11, 6, 8, 0)
    ) == datetime(2026, 11, 6, 9, 0)
    assert scheduler.next_run_at(
        schedule(mode="daily", date="", time="09:00"), datetime(2026, 11, 6, 10, 0)
    ) == datetime(2026, 11, 7, 9, 0)


def test_next_run_for_weekly_picks_the_next_matching_weekday() -> None:
    # 2026-11-06 is a Friday.
    assert datetime(2026, 11, 6).weekday() == 4

    result = scheduler.next_run_at(
        schedule(mode="weekly", date="", weekdays=(0,)),
        datetime(2026, 11, 6, 10, 0),
    )

    assert result == datetime(2026, 11, 9, 9, 0)  # Monday


def test_due_at_ignores_occurrences_already_recorded() -> None:
    daily = schedule(mode="daily", date="", time="09:00")
    now = datetime(2026, 11, 6, 10, 0)

    assert scheduler.due_at(daily, now) == datetime(2026, 11, 6, 9, 0)
    ran = replace(daily, last_run_at="2026-11-06T09:00:03")
    assert scheduler.due_at(ran, now) is None


def test_missed_one_shot_stays_due_but_is_flagged() -> None:
    stale = schedule(date="2026-11-01")
    now = datetime(2026, 11, 6, 10, 0)

    assert scheduler.due_at(stale, now) == datetime(2026, 11, 1, 9, 0)
    assert scheduler.is_missed(stale, now) is True
    assert scheduler.is_missed(schedule(date="2026-11-06", time="09:59"), now) is False


def test_task_names_are_ascii_and_stable() -> None:
    assert scheduler.task_name("abcdef123456") == "LANDrop-abcdef12"


def test_create_args_map_each_mode() -> None:
    from datetime import date as date_type

    once = scheduler.build_create_args(schedule(date="2026-11-06", time="07:30"))
    assert "ONCE" in once
    # The short-date format follows the OS locale (2026/11/06 vs 11/06/2026).
    assert once[once.index("/SD") + 1] == scheduler._system_short_date(
        date_type(2026, 11, 6)
    )
    assert once[once.index("/ST") + 1] == "07:30"

    daily = scheduler.build_create_args(schedule(mode="daily", date="", time="06:15"))
    assert "DAILY" in daily and daily[daily.index("/ST") + 1] == "06:15"

    weekly = scheduler.build_create_args(
        schedule(mode="weekly", date="", weekdays=(2, 0))
    )
    assert weekly[weekly.index("/D") + 1] == "MON,WED"


def test_sync_registers_enabled_and_removes_stale_tasks(monkeypatch) -> None:
    monkeypatch.setenv("LANDROP_ENABLE_SCHTASKS", "1")
    calls: list[list[str]] = []

    def runner(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        if len(args) > 1 and args[1] == "/Query":
            return 0, '"LANDrop-old12345","N/A",""\n"MicrosoftEdgeUpdate","N/A",""\n'
        return 0, "SUCCESS"

    statuses = scheduler.sync_tasks([schedule(enabled=True)], runner=runner)

    assert statuses == [
        {"id": "s1", "task": "LANDrop-s1", "ok": True, "error": None}
    ]
    creates = [call for call in calls if call[1] == "/Create"]
    assert len(creates) == 1
    deletes = [call for call in calls if call[1] == "/Delete"]
    assert deletes and deletes[0][3] == "LANDrop-old12345"


def test_sync_skips_disabled_rules(monkeypatch) -> None:
    monkeypatch.setenv("LANDROP_ENABLE_SCHTASKS", "1")
    calls: list[list[str]] = []

    def runner(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        return 0, '"LANDrop-s1","N/A",""\n'

    statuses = scheduler.sync_tasks([schedule(enabled=False)], runner=runner)

    assert statuses[0]["ok"] is True
    assert not [call for call in calls if call[1] == "/Create"]
    assert [call for call in calls if call[1] == "/Delete"]


def test_sync_is_a_no_op_in_development(monkeypatch) -> None:
    monkeypatch.delenv("LANDROP_ENABLE_SCHTASKS", raising=False)

    statuses = scheduler.sync_tasks([schedule()])

    assert statuses[0]["ok"] is False
    assert "开发模式" in str(statuses[0]["error"])


def test_run_once_records_success_and_retires_a_one_shot(tmp_path, monkeypatch) -> None:
    saved: dict[str, object] = {}
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    config = config_with(schedule(source_path=str(source)))

    monkeypatch.setattr(scheduler, "load_config", lambda: config)
    monkeypatch.setattr(
        scheduler, "save_config", lambda updated: saved.__setitem__("config", updated)
    )
    monkeypatch.setattr(
        scheduler,
        "upload_file",
        lambda **kwargs: TransferResult(source, "/incoming/a.bin", 1, "h" * 64, 0),
    )
    monkeypatch.setattr(
        scheduler, "delete_task", lambda *args, **kwargs: saved.__setitem__("deleted", True)
    )

    assert scheduler.run_once("s1") == 0

    updated = saved["config"]
    assert isinstance(updated, AppConfig)
    assert updated.schedules[0].last_run_status == "succeeded"
    assert updated.schedules[0].last_run_at
    # A one-shot rule must not fire a second time.
    assert updated.schedules[0].enabled is False
    assert saved.get("deleted") is True


def test_run_once_records_failures(tmp_path, monkeypatch) -> None:
    saved: dict[str, object] = {}
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    config = config_with(schedule(source_path=str(source)))

    monkeypatch.setattr(scheduler, "load_config", lambda: config)
    monkeypatch.setattr(
        scheduler, "save_config", lambda updated: saved.__setitem__("config", updated)
    )

    def failing(**kwargs):
        raise TransferError("远端目标文件已存在", code="target_exists")

    monkeypatch.setattr(scheduler, "upload_file", failing)

    assert scheduler.run_once("s1") == 1
    updated = saved["config"]
    assert isinstance(updated, AppConfig)
    assert updated.schedules[0].last_run_status == "failed"


def test_run_once_refuses_password_devices_without_stored_credentials(monkeypatch) -> None:
    config = AppConfig(
        devices=(DeviceConfig(id="d1", host="h", username="u", auth_method="password"),),
        schedules=(schedule(),),
    )
    recorded: list[str] = []
    monkeypatch.setattr(scheduler, "load_config", lambda: config)
    monkeypatch.setattr(scheduler, "save_run_result", lambda sid, status, when=None: recorded.append(status))
    monkeypatch.setattr(scheduler.credentials, "load_password", lambda host, port, user: None)

    assert scheduler.run_once("s1") == 3
    assert recorded == ["failed"]


def test_run_once_reports_missing_schedule(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "load_config", lambda: config_with())

    assert scheduler.run_once("missing") == 2


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 文件锁")
def test_run_lock_is_exclusive(tmp_path) -> None:
    path = tmp_path / "run.lock"
    first = scheduler.RunLock(path)
    second = scheduler.RunLock(path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_loop_dispatches_a_schedule_inside_the_grace_window(monkeypatch) -> None:
    config = config_with(schedule(mode="daily", date="", time="09:00"))
    monkeypatch.setattr(scheduler, "load_config", lambda: config)
    dispatched: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "dispatch_schedule",
        lambda manager, cfg, item: dispatched.append(item.id),
    )

    scheduler.ScheduleLoop(manager=object()).tick(datetime(2026, 11, 6, 9, 1))

    assert dispatched == ["s1"]


def test_loop_skips_occurrences_outside_the_grace_window(monkeypatch) -> None:
    config = config_with(schedule(mode="daily", date="", time="09:00"))
    monkeypatch.setattr(scheduler, "load_config", lambda: config)
    dispatched: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "dispatch_schedule",
        lambda manager, cfg, item: dispatched.append(item.id),
    )

    scheduler.ScheduleLoop(manager=object()).tick(datetime(2026, 11, 6, 12, 0))

    assert dispatched == []


def test_record_run_keeps_daily_rules_enabled() -> None:
    original = config_with(schedule(mode="daily", date="", time="09:00"))

    updated = scheduler._record_run(
        original, "s1", "succeeded", datetime(2026, 11, 6, 9, 0)
    )

    assert updated.schedules[0].enabled is True
    assert updated.schedules[0].last_run_status == "succeeded"
    assert updated.schedules[0].last_run_at == "2026-11-06T09:00:00"

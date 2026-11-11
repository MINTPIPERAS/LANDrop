"""Log file rotation, retention and directory handling."""

from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from landrop import logs


@pytest.fixture(autouse=True)
def reset_logger():
    yield
    logs.shutdown_logging()


def test_default_directory_can_be_overridden(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(logs, "default_log_dir", lambda: tmp_path / "default")

    assert logs.resolve_log_dir("") == tmp_path / "default"
    assert logs.resolve_log_dir("   ") == tmp_path / "default"
    assert logs.resolve_log_dir(str(tmp_path / "custom")) == tmp_path / "custom"


def test_validate_rejects_a_relative_directory() -> None:
    with pytest.raises(ValueError, match="绝对路径"):
        logs.validate_log_dir("relative/logs")


def test_validate_creates_the_directory_and_cleans_the_probe(tmp_path) -> None:
    target = tmp_path / "logs" / "nested"

    resolved = logs.validate_log_dir(str(target))

    assert resolved == target
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_validate_rejects_an_unusable_path(tmp_path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="不可写"):
        logs.validate_log_dir(str(blocker / "logs"))


def test_handler_rotates_when_the_file_grows(tmp_path) -> None:
    handler = logs.DailyRotatingFileHandler(tmp_path / "landrop.log", max_bytes=200)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record_logger = logging.getLogger("landrop.rotate-test")
    record_logger.addHandler(handler)
    record_logger.setLevel(logging.INFO)
    try:
        for _ in range(20):
            record_logger.info("x" * 40)
    finally:
        record_logger.removeHandler(handler)
        handler.close()

    rotated = list(tmp_path.glob("landrop.log.*"))
    assert rotated
    assert (tmp_path / "landrop.log").stat().st_size <= 400


def test_handler_rotates_on_a_new_day(tmp_path, monkeypatch) -> None:
    class FakeDate(date):
        fixed = date(2026, 1, 1)

        @classmethod
        def today(cls) -> "FakeDate":
            return cls(cls.fixed.year, cls.fixed.month, cls.fixed.day)

    monkeypatch.setattr(logs, "date", FakeDate)
    handler = logs.DailyRotatingFileHandler(tmp_path / "landrop.log")
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("landrop", logging.INFO, __file__, 1, "first", None, None)
    handler.emit(record)

    FakeDate.fixed = date(2026, 1, 2)
    second = logging.LogRecord("landrop", logging.INFO, __file__, 2, "second", None, None)
    handler.emit(second)
    handler.close()

    assert (tmp_path / "landrop.log.2026-01-01").read_text(encoding="utf-8").strip() == "first"
    assert (tmp_path / "landrop.log").read_text(encoding="utf-8").strip() == "second"


def test_purge_removes_only_expired_rotated_files(tmp_path) -> None:
    old = tmp_path / "landrop.log.2020-01-01"
    recent = tmp_path / "landrop.log.2026-11-01"
    current = tmp_path / "landrop.log"
    unrelated = tmp_path / "service.log"
    for path in (old, recent, current, unrelated):
        path.write_text("x", encoding="utf-8")

    expired = time.time() - timedelta(days=30).total_seconds()
    os.utime(old, (expired, expired))

    removed = logs.purge(tmp_path, retention_days=7)

    assert removed == [old]
    assert not old.exists()
    assert recent.exists() and current.exists() and unrelated.exists()


def test_purge_is_disabled_when_retention_is_zero(tmp_path) -> None:
    old = tmp_path / "landrop.log.2020-01-01"
    old.write_text("x", encoding="utf-8")
    expired = time.time() - timedelta(days=365).total_seconds()
    os.utime(old, (expired, expired))

    assert logs.purge(tmp_path, retention_days=0) == []
    assert old.exists()


def test_legacy_log_is_migrated_once(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / "service.log"
    legacy.write_text("old records", encoding="utf-8")
    monkeypatch.setattr(logs, "legacy_log_file", lambda: legacy)
    directory = tmp_path / "logs"

    migrated = logs.migrate_legacy_log(directory)

    assert migrated == directory / "service.log"
    assert not legacy.exists()
    assert migrated.read_text(encoding="utf-8") == "old records"
    assert logs.migrate_legacy_log(directory) is None


def test_describe_reports_files_and_total_size(tmp_path) -> None:
    (tmp_path / "landrop.log").write_text("abcd", encoding="utf-8")
    (tmp_path / "landrop.log.2026-11-01").write_text("ef", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    info = logs.describe(tmp_path)

    names = [entry["name"] for entry in info["files"]]
    assert names == ["landrop.log", "landrop.log.2026-11-01", "notes.txt"]
    assert info["total_size"] == 4 + 2 + 7
    assert info["dir"] == str(tmp_path)


def test_describe_tolerates_a_missing_directory(tmp_path) -> None:
    info = logs.describe(tmp_path / "missing")

    assert info["files"] == []
    assert info["total_size"] == 0


def test_configure_logging_writes_records(tmp_path) -> None:
    directory = logs.configure_logging(str(tmp_path))

    assert directory == tmp_path
    logging.getLogger("landrop.test").info("传输完成 3 个文件")

    content = (tmp_path / "landrop.log").read_text(encoding="utf-8")
    assert "传输完成 3 个文件" in content

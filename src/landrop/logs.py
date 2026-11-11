"""Application log file: daily + size rotation, configurable directory."""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from pathlib import Path

LOG_NAME = "landrop.log"
LOGGER_NAME = "landrop"
MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 14
ROTATED_PREFIX = LOG_NAME + "."


def _app_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "LANDrop"
    return Path.home() / ".config" / "landrop"


def default_log_dir() -> Path:
    return _app_root() / "logs"


def legacy_log_file() -> Path:
    """The single file v0.1 wrote; migrated into the log directory on first run."""
    return _app_root() / "service.log"


def resolve_log_dir(configured: str = "") -> Path:
    value = configured.strip()
    return Path(value).expanduser() if value else default_log_dir()


def validate_log_dir(configured: str = "") -> Path:
    """Ensure the directory is absolute, creatable and writable.

    Raises ``ValueError`` so the API can surface a precise message.
    """
    directory = resolve_log_dir(configured)
    if configured.strip() and not directory.is_absolute():
        raise ValueError("日志目录必须是绝对路径")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".landrop-probe-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"日志目录不可写：{directory}") from exc
    return directory


class DailyRotatingFileHandler(logging.Handler):
    """Append to ``landrop.log``, rotating on date change or at ``max_bytes``.

    The standard library's ``TimedRotatingFileHandler`` cannot also cap file
    size, so this keeps both rules in one small handler. Failures while writing
    are swallowed: logging must never break a transfer.
    """

    def __init__(self, path: Path, *, max_bytes: int = MAX_FILE_BYTES, encoding: str = "utf-8") -> None:
        super().__init__()
        self.path = path
        self.max_bytes = max_bytes
        self.encoding = encoding
        self._stream = None
        self._date: date | None = None

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.path, "a", encoding=self.encoding)
        self._date = date.today()

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            finally:
                self._stream = None

    def _rotation_name(self, day: date) -> Path:
        base = f"{self.path.name}.{day.isoformat()}"
        candidate = self.path.with_name(base)
        counter = 1
        while candidate.exists():
            candidate = self.path.with_name(f"{base}.{counter}")
            counter += 1
        return candidate

    def _rotate(self) -> None:
        target = self._rotation_name(self._date or date.today())
        self._close_stream()
        try:
            os.replace(self.path, target)
        except OSError:
            # Keep appending to the existing file rather than losing records.
            pass
        self._open()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._stream is None:
                self._open()
            elif self._date != date.today():
                self._rotate()
            elif self.max_bytes and self._stream.tell() >= self.max_bytes:
                self._rotate()
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            self._close_stream()
        finally:
            self.release()
            super().close()


_active_handler: DailyRotatingFileHandler | None = None


def configure_logging(
    configured: str = "",
    *,
    level: int = logging.INFO,
    max_bytes: int = MAX_FILE_BYTES,
) -> Path | None:
    """Install (or replace) the file handler. Returns the directory, or None.

    The handler sits on the root logger so uvicorn's own startup errors land in
    the file too; the ``landrop`` logger is lowered to INFO so successful
    transfers are recorded. The root logger's level is left alone — propagated
    records are not gated by ancestor levels.
    """
    global _active_handler
    directory = resolve_log_dir(configured)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = DailyRotatingFileHandler(directory / LOG_NAME, max_bytes=max_bytes)
    except OSError:
        return None

    root = logging.getLogger()
    if _active_handler is not None:
        root.removeHandler(_active_handler)
        _active_handler.close()
    _active_handler = handler
    root.addHandler(handler)
    logging.getLogger(LOGGER_NAME).setLevel(level)
    return directory


def shutdown_logging() -> None:
    """Detach the file handler; used when the service stops or tests finish."""
    global _active_handler
    root = logging.getLogger()
    if _active_handler is not None:
        root.removeHandler(_active_handler)
        _active_handler.close()
        _active_handler = None


def purge(directory: Path, retention_days: int) -> list[Path]:
    """Delete rotated files older than ``retention_days`` (0 keeps them)."""
    if retention_days <= 0 or not directory.is_dir():
        return []
    cutoff = time.time() - retention_days * 86400
    removed: list[Path] = []
    for entry in directory.iterdir():
        if not entry.is_file() or not entry.name.startswith(ROTATED_PREFIX):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed.append(entry)
        except OSError:
            continue
    return removed


def migrate_legacy_log(directory: Path) -> Path | None:
    """Move the v0.1 ``service.log`` into the log directory once."""
    legacy = legacy_log_file()
    if not legacy.is_file():
        return None
    target = directory / legacy.name
    if target.exists():
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.replace(legacy, target)
    except OSError:
        return None
    return target


def describe(directory: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    total = 0
    if directory.is_dir():
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_file():
                continue
            try:
                stats = entry.stat()
            except OSError:
                continue
            total += stats.st_size
            files.append(
                {"name": entry.name, "size": stats.st_size, "modified": int(stats.st_mtime)}
            )
    return {
        "dir": str(directory),
        "default_dir": str(default_log_dir()),
        "files": files,
        "total_size": total,
    }


def open_directory(directory: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(str(directory))  # type: ignore[attr-defined]
    except OSError:
        return False
    return True

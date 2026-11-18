"""Remote folder naming and validation for directory transfers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

DATE_TOKEN = "{date}"
MAX_NAME_LENGTH = 255
_INVALID_CHARACTERS = frozenset('<>:"/\\|?*')
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def _local_folder_name(local_path: str | Path) -> str:
    name = Path(local_path).expanduser().name.strip()
    return name or "folder"


def default_remote_name(local_path: str | Path, *, today: date | None = None) -> str:
    """Return the default ``YYYY-MM-DD_<local folder>`` name."""
    day = (today or date.today()).isoformat()
    return validate_remote_name(f"{day}_{_local_folder_name(local_path)}")


def resolve_remote_name(
    raw: str | None, local_path: str | Path, *, today: date | None = None
) -> str:
    """Resolve a user-provided name or template into one safe path segment.

    An empty value falls back to :func:`default_remote_name`. The ``{date}``
    token is replaced with today's local date each time this is called, which
    lets recurring schedules create a new folder per run.
    """
    day = (today or date.today()).isoformat()
    name = (raw or "").strip()
    if not name:
        name = f"{day}_{_local_folder_name(local_path)}"
    else:
        name = name.replace(DATE_TOKEN, day)
    return validate_remote_name(name)


def validate_remote_name(name: str) -> str:
    """Validate a single remote folder path segment and return it trimmed."""
    name = name.strip()
    if not name:
        raise ValueError("远端文件夹名不能为空")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError("远端文件夹名不能超过 255 个字符")
    if name in {".", ".."}:
        raise ValueError("远端文件夹名不能是 . 或 ..")
    if "/" in name or "\\" in name:
        raise ValueError("远端文件夹名不能包含 / 或 \\")
    if any(ord(character) < 32 for character in name):
        raise ValueError("远端文件夹名不能包含控制字符")
    if any(character in _INVALID_CHARACTERS for character in name):
        raise ValueError('远端文件夹名不能包含 < > : " | ? *')
    if name.endswith("."):
        raise ValueError("远端文件夹名不能以点结尾")
    if name.split(".", 1)[0].upper() in _RESERVED_NAMES:
        raise ValueError("远端文件夹名不能使用 Windows 保留名称")
    return name
